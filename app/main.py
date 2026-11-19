"""FastAPI 入口：会话发现、导入作业、消息查询与媒体静态服务。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import adb, avatars, cache, media_cache, picker, store
from .exports import exports
from .discover import discover
from .jobs import TERMINAL, apply_events, jobs
from .settings import (
    EXPORT_DIR,
    MEDIA_DIR,
    ROOT,
    config_from_request,
    ensure_dirs,
    load_config,
    public_config,
    save_config,
)

app = FastAPI(title="QQ 聊天记录导出")
ensure_dirs()
store.init_db()
cache.sweep_cache()


def _imported_keys() -> set[str]:
    return {c["key"] for c in store.list_conversations()}


async def _scan_conversations() -> list[dict]:
    """每次调用都重新扫描，不做缓存；只有前端手动触发时才会走到这里。"""
    cfg = load_config()
    if not cfg.ready:
        return []
    loader = adb.discover if cfg.is_adb else discover
    items = await asyncio.to_thread(loader, cfg)
    imported = _imported_keys()
    return [{**item.to_json(), "imported": item.key in imported} for item in items]


@app.get("/api/state")
def api_state() -> dict:
    cfg = load_config()
    return {
        "config": public_config(cfg),
        "conversations": store.list_conversations(),
        "jobs": [job.to_json() for job in jobs.running()],
    }


@app.post("/api/setup")
async def api_setup(payload: dict) -> dict:
    cfg = config_from_request(payload)
    kc_path = str(payload.get("kcPath") or "").strip()
    try:
        if cfg.is_adb:
            await asyncio.to_thread(adb.check, cfg)
            if kc_path.startswith("adb:") and not cfg.key:
                cfg.key = adb.read_device_file(cfg, adb.device_path(kc_path)).strip()
        else:
            if kc_path and not kc_path.startswith("adb:"):
                if not Path(kc_path).is_file():
                    raise HTTPException(400, f"kc 文件不存在：{kc_path}")
            if not Path(cfg.db_path).is_file():
                raise HTTPException(400, f"数据库不存在：{cfg.db_path}")
            if cfg.slow_db_path and not Path(cfg.slow_db_path).is_file():
                raise HTTPException(400, f"slowtable 不存在：{cfg.slow_db_path}")
    except adb.AdbError as exc:
        raise HTTPException(400, f"adb 连接失败：{exc}") from exc
    if not cfg.key:
        raise HTTPException(400, "缺少解密密钥（kc 文件或手动填入）")
    save_config(cfg)
    return {"config": public_config(cfg)}


@app.get("/api/discover")
async def api_discover() -> dict:
    return {"conversations": await _scan_conversations()}


@app.post("/api/import")
async def api_import(payload: dict) -> dict:
    key = str(payload.get("key") or "")
    if not key:
        raise HTTPException(400, "缺少会话 key")
    conv = next((c for c in await _scan_conversations() if c["key"] == key), None)
    if conv is None:
        raise HTTPException(404, "会话不存在，请先刷新列表")
    if not conv.get("resolved", True):
        raise HTTPException(
            400,
            "该会话在 Friends/TroopInfoV2 中找不到对应 QQ 号，解析器无法定位会话，暂不支持导入",
        )
    cfg = load_config()
    ts_to = int(payload.get("tsTo") or time.time())
    ts_from = int(payload.get("tsFrom") or (ts_to - 86400))
    if ts_from >= ts_to:
        raise HTTPException(400, "时间范围不合法")
    try:
        job = jobs.start(
            {"key": conv["key"], "kind": conv["kind"], "uin": conv["uin"], "name": conv["name"]},
            cfg,
            ts_from,
            ts_to,
            bool(payload.get("refresh")),
            bool(payload.get("foreground", True)),
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return job.to_json()


@app.post("/api/foreground")
def api_foreground(payload: dict) -> dict:
    key = str(payload.get("key") or "")
    if not key:
        raise HTTPException(400, "缺少会话 key")
    job = jobs.foreground(key)
    return {"job": job.to_json() if job else None}


@app.post("/api/export")
def api_export(payload: dict) -> dict:
    key = str(payload.get("key") or "")
    if not key:
        raise HTTPException(400, "缺少会话 key")
    conv = store.get_conversation(key)
    if conv is None:
        raise HTTPException(404, "会话尚未导入")
    fmt = str(payload.get("format") or "html").lower()
    sender = str(payload.get("sender") or "")
    ids: list[int] | None = None
    if "ids" in payload:
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(400, "没有选择要导出的消息")
        try:
            ids = sorted({int(item) for item in raw_ids})
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "消息 ID 不合法") from exc
        span = store.selection_span(key, ids)
        if span is None:
            raise HTTPException(400, "所选消息不存在")
        ts_from = span["min_ts"]
        ts_to = max(span["max_ts"] + 1, ts_from + 1)
    else:
        ts_to = int(payload.get("tsTo") or time.time())
        ts_from = int(payload.get("tsFrom") or 0)
        if ts_to <= ts_from:
            raise HTTPException(400, "时间范围不合法")
        if store.count_messages(key, ts_from, ts_to, sender) == 0:
            raise HTTPException(400, "该区间没有可导出的消息")
    directory = str(payload.get("target") or "")
    if not directory:
        raise HTTPException(400, "缺少保存路径")
    try:
        task = exports.start(conv, fmt, directory, ts_from, ts_to, sender, ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return task.to_json()


@app.get("/api/export/{task_id}")
def api_export_status(task_id: str) -> dict:
    task = exports.get(task_id)
    if task is None:
        raise HTTPException(404, "导出任务不存在")
    return task.to_json()


@app.post("/api/save-path")
def api_save_path(payload: dict) -> dict:
    fmt = str(payload.get("format") or "html").lower()
    suffixes = {"html": ".html", "pdf": ".pdf", "md": ".md"}
    if fmt not in suffixes:
        raise HTTPException(400, "不支持的导出格式")
    suffix = suffixes[fmt]
    name = Path(str(payload.get("filename") or f"export{suffix}")).name
    if not name.lower().endswith(suffix):
        name += suffix
    try:
        path = picker.save_file(name, suffix, str(EXPORT_DIR))
    except RuntimeError as exc:
        raise HTTPException(501, f"无法打开保存对话框：{exc}") from exc
    return {"path": path or ""}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")
    return apply_events(job).to_json()


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel(job_id: str) -> dict:
    if not jobs.cancel(job_id):
        raise HTTPException(409, "作业无法取消")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str, request: Request) -> StreamingResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")

    async def stream():
        offset = 0
        while True:
            if await request.is_disconnected():
                break
            size = job.events_path.stat().st_size if job.events_path.exists() else 0
            if size > offset:
                with job.events_path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                offset += len(chunk)
                for raw in chunk.decode("utf-8", "ignore").splitlines():
                    if raw.strip():
                        yield f"data: {raw}\n\n"
            if job.status in TERMINAL and offset >= size:
                yield f"data: {json.dumps({'stage': 'status', 'status': job.status, 'error': job.error, 'loaded': job.loaded})}\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/conversations")
def api_conversations() -> dict:
    return {"conversations": store.list_conversations()}


def _require_conversation(conv_key: str) -> dict:
    conv = store.get_conversation(conv_key)
    if conv is None:
        raise HTTPException(404, "会话尚未导入")
    return conv


@app.get("/api/conversations/{conv_key}/senders")
def api_senders(conv_key: str) -> dict:
    _require_conversation(conv_key)
    return {"senders": store.list_senders(conv_key)}


@app.get("/api/conversations/{conv_key}/stats")
def api_stats(
    conv_key: str,
    ts_from: int = 0,
    ts_to: int = 0,
    sender: str = "",
) -> dict:
    _require_conversation(conv_key)
    ts_to = ts_to or int(time.time())
    return {"count": store.count_messages(conv_key, ts_from, ts_to, sender)}


@app.get("/api/conversations/{conv_key}/days")
def api_days(
    conv_key: str,
    sender: str = "",
) -> dict:
    conv = _require_conversation(conv_key)
    ts_from = conv["min_ts"] or 0
    ts_to = conv["max_ts"] or int(time.time())
    return {
        "days": store.message_days(conv_key, ts_from, ts_to, sender),
        "minDate": ts_from,
        "maxDate": ts_to,
    }


@app.get("/api/conversations/{conv_key}/buckets")
def api_buckets(
    conv_key: str,
    date: str,
    unit: str = "hour",
    hour: int | None = None,
) -> dict:
    """按小时/分钟聚合某一天的消息数，供日期轴悬停卡片使用。"""
    _require_conversation(conv_key)
    if unit not in ("hour", "minute"):
        raise HTTPException(400, "unit 只支持 hour 或 minute")
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(400, "date 格式应为 YYYY-MM-DD") from exc
    start = int(day.timestamp())
    end = int((day + timedelta(days=1)).timestamp()) - 1
    if unit == "minute":
        if hour is None or not 0 <= hour <= 23:
            raise HTTPException(400, "minute 聚合需要 0-23 的 hour")
        start += hour * 3600
        end = start + 3599
    return {"buckets": store.message_buckets(conv_key, start, end, unit)}


@app.get("/api/conversations/{conv_key}/ids")
def api_message_ids(
    conv_key: str,
    ts_from: int = 0,
    ts_to: int = 0,
    sender: str = "",
) -> dict:
    """返回时间区间内的消息 ID，供前端做区间勾选。"""
    conv = _require_conversation(conv_key)
    ts_to = ts_to or (int(conv["max_ts"]) + 1)
    return {"ids": store.message_ids(conv_key, ts_from, ts_to, sender)}


@app.get("/api/conversations/{conv_key}/messages")
def api_messages(
    conv_key: str,
    ts_from: int = 0,
    ts_to: int = 0,
    sender: str = "",
    before_ts: int = 0,
    before_id: int = 0,
    after_ts: int = 0,
    after_id: int = 0,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    conv = _require_conversation(conv_key)
    ts_to = ts_to or int(time.time())
    if after_ts or after_id:
        messages, has_newer = store.fetch_messages_after(
            conv_key,
            ts_from,
            ts_to,
            sender,
            limit=limit,
            after_ts=after_ts,
            after_id=after_id,
        )
        return {
            "messages": messages,
            "hasMore": False,
            "hasNewer": has_newer,
            "covered": store.range_covered(conv_key, ts_from, ts_to),
            "tsFrom": ts_from,
            "tsTo": ts_to,
        }
    messages, has_more = store.fetch_messages(
        conv_key,
        ts_from,
        ts_to,
        sender,
        limit=limit,
        before_ts=before_ts,
        before_id=before_id,
    )
    return {
        "messages": messages,
        "hasMore": has_more,
        "hasNewer": int(conv["max_ts"] or 0) > ts_to,
        "covered": store.range_covered(conv_key, ts_from, ts_to),
        "tsFrom": ts_from,
        "tsTo": ts_to,
    }


@app.get("/lazy/{conv_key}/{md5}")
def api_lazy_media(conv_key: str, md5: str):
    path = media_cache.ensure(md5, media_cache.conversation_kind(conv_key))
    if path is None:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return FileResponse(
        path,
        media_type=media_cache.media_type(path),
        headers={"Cache-Control": "public, max-age=2592000"},
    )


@app.get("/avatars/{uin}.jpg")
def api_avatar(uin: str):
    cfg = load_config()
    path = avatars.ensure(uin) if cfg.need_avatars else avatars.path_for(uin)
    if path is None or not path.is_file():
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=300"})
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800"},
    )


app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

_dist = ROOT / "web" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
else:

    @app.get("/")
    def _no_dist() -> dict:
        return {"error": "前端未构建，请先在 web/ 下执行 npm install && npm run build"}
