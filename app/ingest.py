"""把一次解析作业的产物写入 ChatDocument，并搬运媒体文件。"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

from . import adb, store
from .settings import MEDIA_DIR

MEDIA_DIRS = ("images", "videos", "voices", "emoticons", "senders", "files", "temp")
LAZY_FACE_RE = re.compile(r"lazyface:([0-9]+)/([0-9a-f]{8,64})")
LAZY_EMOJI_RE = re.compile(r"lazyemoji:([0-9]+)")
SYSFACE_LAYOUT = (("static", "png"), ("gif", "gif"), ("apng", "png"))


def media_dir_for(conv_key: str) -> Path:
    return MEDIA_DIR / conv_key.replace(":", "_")


def _merge_move(src: Path, dst: Path) -> None:
    """递归合并目录，同名文件以新产物为准。"""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _merge_move(item, target)
            item.rmdir()
        else:
            target.unlink(missing_ok=True)
            shutil.move(str(item), str(target))


def _move_media(output_dir: Path, conv_key: str, emit) -> None:
    target = media_dir_for(conv_key)
    for name in MEDIA_DIRS:
        src = output_dir / name
        if src.exists():
            _merge_move(src, target / name)
    emit({"stage": "ingest", "message": "媒体文件已搬运"})


def _read_emoticon(cfg, ep_id: str, e_id: str, via_adb: bool) -> bytes:
    """读取手机或本地 .emotionsm/<epId>/<eId> 原始字节。"""
    if via_adb:
        remote = f"{adb.DEFAULT_EMOTICON_ROOT}/{ep_id}/{e_id}"
        try:
            return adb._run(cfg, ["exec-out", f"cat {shlex.quote(remote)}"], timeout=30)
        except adb.AdbError:
            return b""
    root = cfg.effective_emoticon_path()
    if not root:
        return b""
    path = Path(root) / ep_id / e_id
    try:
        return path.read_bytes() if path.is_file() else b""
    except OSError:
        return b""


def _image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _read_sysface(cfg, qsid: str, via_adb: bool) -> bytes:
    """读取手机 qq_emoticon_res 里该表情的图片（静态优先）。"""
    if not via_adb:
        return b""
    for folder, ext in SYSFACE_LAYOUT:
        remote = f"{adb.DEFAULT_SYSFACE_ROOT}/{folder}/s{qsid}.{ext}"
        try:
            data = adb._run(cfg, ["exec-out", f"cat {shlex.quote(remote)}"], timeout=30)
        except adb.AdbError:
            continue
        if data:
            return data
    return b""


def _materialize_lazy_media(
    chat_data: Path, output_dir: Path, cfg, via_adb: bool, emit
) -> None:
    """把 lazyface/lazyemoji 引用换成 output 下从手机取回的真实文件。"""
    text = chat_data.read_text(encoding="utf-8")
    faces = sorted(set(LAZY_FACE_RE.findall(text)))
    emojis = sorted(set(LAZY_EMOJI_RE.findall(text)))
    if not faces and not emojis:
        return
    # 解析器包在 src 下，运行期由 worker 注入 sys.path
    from src.dataParsing.protoDataParsing import decodeEmoticonFile  # noqa: PLC0415

    replacements: dict[str, str] = {}

    face_dir = output_dir / "emoticons" / "emoticon2"
    for ep_id, e_id in faces:
        data, ext = decodeEmoticonFile(_read_emoticon(cfg, ep_id, e_id, via_adb))
        if not data:
            continue
        face_dir.mkdir(parents=True, exist_ok=True)
        rel = f"emoticons/emoticon2/{e_id}.{ext}"
        (face_dir / f"{e_id}.{ext}").write_bytes(data)
        replacements[f"lazyface:{ep_id}/{e_id}"] = rel

    emoji_dir = output_dir / "emoticons" / "emoticon1" / "new"
    for qsid in emojis:
        data = _read_sysface(cfg, qsid, via_adb)
        ext = _image_extension(data)
        if not ext:
            continue
        emoji_dir.mkdir(parents=True, exist_ok=True)
        rel = f"emoticons/emoticon1/new/s{qsid}.{ext}"
        (emoji_dir / f"s{qsid}.{ext}").write_bytes(data)
        replacements[f"lazyemoji:{qsid}"] = rel

    lines = []
    for line in text.splitlines():
        if "lazyface:" in line or "lazyemoji:" in line:
            for token, rel in replacements.items():
                line = line.replace(token, f"output/{rel}")
            if "lazyface:" in line or "lazyemoji:" in line:
                line = _lazy_media_error(line)
        lines.append(line)
    chat_data.write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit(
        {
            "stage": "ingest",
            "message": (
                f"表情媒体已提取：大表情 {sum(1 for t in replacements if t.startswith('lazyface:'))}/{len(faces)}"
                f"，QQ表情 {sum(1 for t in replacements if t.startswith('lazyemoji:'))}/{len(emojis)}"
            ),
        }
    )


def _lazy_media_error(line: str) -> str:
    """仍未被替换的惰性表情引用退化为占位文本。"""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line

    def walk(node):
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        path = str((node.get("c") or {}).get("path") or "")
        if path.startswith("lazyface:"):
            return {"t": "err", "c": {"text": "[大表情]", "type": "media"}}
        if path.startswith("lazyemoji:"):
            return {"t": "err", "c": {"text": "[QQ表情]", "type": "text"}}
        return {key: walk(value) for key, value in node.items()}

    return json.dumps(walk(obj), ensure_ascii=False)


def _read_senders(senders_file: Path) -> list[tuple[str, str, str]]:
    if not senders_file.is_file():
        return []
    data = json.loads(senders_file.read_text(encoding="utf-8"))
    result = []
    for uin, value in data.items():
        name = value[0] if value else ""
        avatar = value[1] if len(value) > 1 else ""
        if avatar.startswith("output/"):
            avatar = avatar[len("output/"):]
        result.append((str(uin), name or "", avatar))
    return result


def ingest(work_dir: Path, conv: dict, ts_from: int, ts_to: int, emit, cfg,
           via_adb: bool) -> dict:
    """读取 work_dir/output/chatData.txt，替换 [ts_from, ts_to] 区间并入库。"""
    output_dir = work_dir / "output"
    chat_data = output_dir / "chatData.txt"
    if not chat_data.is_file():
        raise RuntimeError(f"解析产物不存在：{chat_data}")

    total = sum(1 for _ in chat_data.open("rb"))
    _materialize_lazy_media(chat_data, output_dir, cfg, via_adb, emit)

    links: dict[str, str] = {}

    def messages():
        with chat_data.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = int(obj.get("i") or 0)
                sender = str(obj.get("s") or "")
                mtype = str(obj.get("t") or "")
                content = obj.get("c")
                if isinstance(content, dict):
                    url = content.get("url")
                    path = str(content.get("path") or "")
                    if url and path.startswith("lazy:"):
                        # 记录 QQ 直链，payload 里不必再带长 URL
                        links[path[len("lazy:"):]] = str(url)
                        content = {k: v for k, v in content.items() if k != "url"}
                        obj["c"] = content
                        line = json.dumps(obj, ensure_ascii=False)
                # 解析器把媒体写到 output/ 下，这里改写成相对于会话媒体目录的路径
                yield ts, sender, mtype, line.replace('"output/', '"')

    senders = _read_senders(output_dir / "senders" / "senders.json")
    _move_media(output_dir, conv["key"], emit)

    def on_progress(done: int) -> None:
        emit(
            {
                "stage": "ingest",
                "done": done,
                "total": total,
                "progress": 80 + int(done / max(total, 1) * 20),
            }
        )

    stats = store.replace_range(
        conv, messages(), senders, ts_from, ts_to, on_progress
    )
    store.save_media_links(links)
    emit(
        {
            "stage": "ingest",
            "progress": 100,
            "message": f"写入 {stats['count']} 条消息，会话共 {stats['total']} 条",
        }
    )
    return stats
