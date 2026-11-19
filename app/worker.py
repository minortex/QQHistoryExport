"""单个导入作业的执行进程。

独立进程运行，避免 reportlab/Pillow/解析器的全局状态污染 API 进程，
取消作业时直接杀掉进程组。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import traceback
from pathlib import Path

PROGRESS_RE = re.compile(r"解析进度：(\d+)%")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.worker <job_dir>", file=sys.stderr)
        return 2
    job_dir = Path(sys.argv[1]).resolve()
    events_file = job_dir / "events.jsonl"

    def emit(event: dict) -> None:
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()

    adb_temp: Path | None = None

    try:
        spec = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        from dataclasses import replace

        from .settings import RAW_DIR, ROOT, AppConfig, parser_configs

        cfg = AppConfig(**spec["config"])
        conv = spec["conv"]
        ts_from = int(spec["ts_from"])
        ts_to = int(spec["ts_to"])
        work = job_dir / "work"
        (work / "output").mkdir(parents=True, exist_ok=True)
        resources = work / "resources"
        if not resources.exists():
            os.symlink(ROOT / "resources", resources)
        os.chdir(work)
        sys.path.insert(0, str(ROOT))

        from src.dataParsing import parsing
        from src.errcode.errcode import ErrCode

        from . import ingest

        via_adb = cfg.is_adb
        if via_adb:
            from . import adb

            raw_dir = RAW_DIR / conv["key"].replace(":", "_") / f"{ts_from}-{ts_to}"
            raw_main = raw_dir / "main.db"
            raw_slow = raw_dir / "slow.db"
            ready = raw_dir / ".ready"
            if spec.get("refresh") or not ready.is_file():
                emit({"stage": "prepare", "progress": 1, "message": "通过 adb 导出该时间窗"})
                cfg, adb_temp = adb.materialize(cfg, conv, emit, ts_from, ts_to)
                raw_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(cfg.db_path, raw_main)
                if cfg.slow_db_path and Path(cfg.slow_db_path).is_file():
                    shutil.move(cfg.slow_db_path, raw_slow)
                elif raw_slow.is_file():
                    raw_slow.unlink()
                ready.touch()
                emit({"stage": "prepare", "progress": 34, "message": "会话数据库已缓存"})
            cfg = replace(
                cfg,
                db_path=str(raw_main),
                slow_db_path=str(raw_slow) if raw_slow.is_file() else "",
                images_path="",
                lazy_images=cfg.need_images,
                need_voice=False,
                need_video=False,
            )

        emit({"stage": "parse", "progress": 35 if via_adb else 0, "message": "开始解析"})
        last: list[str] = []

        def on_log(line: str) -> None:
            last.append(line)
            del last[:-20]
            match = PROGRESS_RE.search(line)
            if match:
                percent = int(match.group(1))
                emit(
                    {
                        "stage": "parse",
                        "progress": 35 + int(percent * 0.45) if via_adb else int(percent * 0.8),
                    }
                )
            elif "错误" in line or "失败" in line:
                emit({"stage": "parse", "log": line})

        emoji_config = ""
        if via_adb:
            from . import adb

            try:
                data = adb._run(
                    cfg,
                    ["exec-out", f"cat {shlex.quote(adb.DEFAULT_EMOJI_CONFIG)}"],
                    timeout=30,
                )
            except adb.AdbError:
                data = b""
            if data:
                emoji_file = work / "face_config.json"
                emoji_file.write_bytes(data)
                emoji_config = str(emoji_file)

        mode = "group" if conv["kind"] == "troop" else "friend"
        configs = parser_configs(cfg, conv["uin"], mode, ts_from, ts_to, emoji_config)
        err = ErrCode("func", on_log)
        parse = parsing.QQParse(configs, err)
        ok = parse.procDb()
        if not ok:
            raise RuntimeError("解析未完成：" + " | ".join(last[-3:]))

        emit({"stage": "ingest", "progress": 80, "message": "写入消息库"})
        stats = ingest.ingest(work, conv, ts_from, ts_to, emit, cfg, via_adb)
        shutil.rmtree(work / "output", ignore_errors=True)
        if via_adb:
            from . import cache

            cache.prune_raw(conv["key"])
            cache.sweep_cache(emit=emit)
        emit({"stage": "done", "progress": 100, "result": stats})
        return 0
    except BaseException as exc:  # noqa: BLE001 - 需要把失败写进事件流
        emit(
            {
                "stage": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        traceback.print_exc()
        return 1
    finally:
        if adb_temp is not None:
            shutil.rmtree(adb_temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
