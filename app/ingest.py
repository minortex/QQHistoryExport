"""把一次解析作业的产物写入 ChatDocument，并搬运媒体文件。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import store
from .settings import MEDIA_DIR

MEDIA_DIRS = ("images", "videos", "voices", "emoticons", "senders", "files", "temp")


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


def ingest(work_dir: Path, conv: dict, ts_from: int, ts_to: int, emit) -> dict:
    """读取 work_dir/output/chatData.txt，替换 [ts_from, ts_to] 区间并入库。"""
    output_dir = work_dir / "output"
    chat_data = output_dir / "chatData.txt"
    if not chat_data.is_file():
        raise RuntimeError(f"解析产物不存在：{chat_data}")

    total = sum(1 for _ in chat_data.open("rb"))

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
    emit(
        {
            "stage": "ingest",
            "progress": 100,
            "message": f"写入 {stats['count']} 条消息，会话共 {stats['total']} 条",
        }
    )
    return stats
