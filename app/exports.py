"""导出任务：一次只跑一个，前端轮询进度。"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import export, store
from .settings import EXPORT_CACHE_DIR

PDF_PROGRESS_RE = re.compile(r"生成进度：(\d+)%")


@dataclass
class ExportTask:
    id: str
    conv_key: str
    conv_name: str
    format: str
    target: str
    status: str = "running"
    progress: int = 0
    message: str = "准备中"
    error: str = ""
    created: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "convKey": self.conv_key,
            "convName": self.conv_name,
            "format": self.format,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "created": self.created,
        }


class ExportManager:
    MAX_TASKS = 20

    def __init__(self) -> None:
        EXPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, ExportTask] = {}
        self._lock = threading.Lock()

    def get(self, task_id: str) -> ExportTask | None:
        return self._tasks.get(task_id)

    def running(self) -> ExportTask | None:
        return next((task for task in self._tasks.values() if task.status == "running"), None)

    def _target(self, task_id: str, filename: str, fmt: str) -> Path:
        expected = {"html": ".html", "pdf": ".pdf", "md": ".md"}[fmt]
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(filename or "").name).strip(" .")
        if not name:
            name = f"export{expected}"
        if not name.lower().endswith(expected):
            name += expected
        directory = EXPORT_CACHE_DIR / task_id
        return directory / name

    def start(
        self,
        conv: dict,
        fmt: str,
        filename: str,
        ts_from: int,
        ts_to: int,
        sender: str = "",
        ids: list[int] | None = None,
    ) -> ExportTask:
        if fmt not in ("html", "pdf", "md"):
            raise ValueError("导出格式只支持 html、pdf 或 md")
        if ids is None and ts_from >= ts_to:
            raise ValueError("时间范围不合法")
        with self._lock:
            if self.running() is not None:
                raise RuntimeError("已有导出任务在运行，请稍候")
            task_id = uuid.uuid4().hex[:12]
            target = self._target(task_id, filename, fmt)
            task = ExportTask(
                id=task_id,
                conv_key=conv["key"],
                conv_name=conv.get("name") or conv["key"],
                format=fmt,
                target=str(target),
            )
            self._tasks[task.id] = task
            for old in sorted(self._tasks.values(), key=lambda item: item.created)[:-self.MAX_TASKS]:
                if old.status != "running":
                    self._tasks.pop(old.id, None)
                    old_dir = Path(old.target).parent
                    if old_dir.name == old.id:
                        shutil.rmtree(old_dir, ignore_errors=True)
            threading.Thread(
                target=self._run,
                args=(task, conv, ts_from, ts_to, sender, ids),
                daemon=True,
            ).start()
            return task

    def _run(
        self,
        task: ExportTask,
        conv: dict,
        ts_from: int,
        ts_to: int,
        sender: str,
        ids: list[int] | None = None,
    ) -> None:
        total = (
            len(set(ids))
            if ids
            else max(1, store.count_messages(conv["key"], ts_from, ts_to, sender))
        )

        def progress(done: int, expected: int) -> None:
            task.progress = min(99, int(done / max(expected, 1) * 100))
            task.message = f"{done}/{expected} 条"

        def pdf_log(line: str) -> None:
            match = PDF_PROGRESS_RE.search(line)
            if match:
                task.progress = 30 + int(int(match.group(1)) * 0.7)
                task.message = line
            elif "开始生成" in line or "出错" in line:
                task.message = line

        staging = EXPORT_CACHE_DIR / f".{task.id}.part"
        try:
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            work = staging / Path(task.target).name
            if task.format == "html":
                task.message = f"生成 HTML（{total} 条）"
                export.export_html(conv, ts_from, ts_to, work, sender, progress, ids)
                self._bundle_html(work)
            elif task.format == "md":
                task.message = f"生成 Markdown（{total} 条）"
                export.export_markdown(conv, ts_from, ts_to, work, sender, progress, ids)
            else:
                task.message = f"排版 PDF（{total} 条）"
                export.export_pdf(conv, ts_from, ts_to, work, sender, progress, pdf_log, ids)
            os.replace(staging, Path(task.target).parent)
            task.status = "completed"
            task.progress = 100
            task.message = f"已导出 {total} 条"
        except Exception as exc:  # noqa: BLE001 - 任务失败要留给前端
            shutil.rmtree(staging, ignore_errors=True)
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.message = "导出失败"

    @staticmethod
    def _bundle_html(path: Path) -> None:
        assets = path.with_name(path.stem + "_assets")
        if not assets.is_dir():
            return
        archive = path.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(path, path.name)
            for child in sorted(assets.rglob("*")):
                if child.is_file():
                    bundle.write(child, child.relative_to(path.parent).as_posix())
        path.unlink()
        shutil.rmtree(assets)


exports = ExportManager()
