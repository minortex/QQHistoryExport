"""导出任务：一次只跑一个，前端轮询进度。

导出直接写入用户选择的文件夹：先在同目录的隐藏 staging 目录生成，
完成后原子改名为最终文件；取消或失败只删除 staging，不留下半成品。
"""

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
from .settings import load_config

PDF_PROGRESS_RE = re.compile(r"生成进度：(\d+)%")
STAMP_FORMAT = "%Y%m%d-%H%M%S"
SUFFIXES = {"html": ".html", "pdf": ".pdf", "md": ".md"}


class ExportCancelled(Exception):
    """用户取消了导出任务。"""


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"找不到可用的文件名：{path}")


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
    stop: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "convKey": self.conv_key,
            "convName": self.conv_name,
            "format": self.format,
            "output": self.target,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "created": self.created,
        }


class ExportManager:
    MAX_TASKS = 20

    def __init__(self) -> None:
        self._tasks: dict[str, ExportTask] = {}
        self._lock = threading.Lock()

    def get(self, task_id: str) -> ExportTask | None:
        return self._tasks.get(task_id)

    def running(self) -> ExportTask | None:
        return next((task for task in self._tasks.values() if task.status == "running"), None)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.status != "running":
            return False
        task.stop.set()
        task.message = "正在取消…"
        return True

    def _target(self, directory: Path, conv_name: str, fmt: str) -> Path:
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", conv_name).strip(" .") or "chat"
        base = f"{stem}_{time.strftime(STAMP_FORMAT)}"
        return _unique_path(directory / f"{base}{SUFFIXES[fmt]}")

    def start(
        self,
        conv: dict,
        fmt: str,
        ts_from: int,
        ts_to: int,
        sender: str = "",
        ids: list[int] | None = None,
    ) -> ExportTask:
        if fmt not in SUFFIXES:
            raise ValueError("导出格式只支持 html、pdf 或 md")
        if ids is None and ts_from >= ts_to:
            raise ValueError("时间范围不合法")
        directory = load_config().effective_export_dir()
        if not directory.is_dir():
            raise ValueError(f"导出文件夹不存在：{directory}")
        with self._lock:
            if self.running() is not None:
                raise RuntimeError("已有导出任务在运行，请稍候")
            target = self._target(directory, conv.get("name") or conv["key"], fmt)
            task = ExportTask(
                id=uuid.uuid4().hex[:12],
                conv_key=conv["key"],
                conv_name=conv.get("name") or conv["key"],
                format=fmt,
                target=str(target),
            )
            self._tasks[task.id] = task
            for old in sorted(self._tasks.values(), key=lambda item: item.created)[:-self.MAX_TASKS]:
                if old.status != "running":
                    self._tasks.pop(old.id, None)
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

        def check() -> None:
            if task.stop.is_set():
                raise ExportCancelled

        def progress(done: int, expected: int) -> None:
            check()
            task.progress = min(99, int(done / max(expected, 1) * 100))
            task.message = f"{done}/{expected} 条"

        def pdf_log(line: str) -> None:
            check()
            match = PDF_PROGRESS_RE.search(line)
            if match:
                task.progress = 30 + int(int(match.group(1)) * 0.7)
                task.message = line
            elif "开始生成" in line or "出错" in line:
                task.message = line

        target = Path(task.target)
        staging = target.parent / f".{task.id}.part"
        try:
            check()
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            work = staging / target.name
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
            check()
            produced = work if work.is_file() else work.with_suffix(".zip")
            if not produced.is_file():
                raise RuntimeError("导出未生成文件")
            final = _unique_path(target.with_suffix(produced.suffix))
            os.replace(produced, final)
            shutil.rmtree(staging, ignore_errors=True)
            task.target = str(final)
            task.status = "completed"
            task.progress = 100
            task.message = f"已导出 {total} 条"
        except ExportCancelled:
            shutil.rmtree(staging, ignore_errors=True)
            task.status = "cancelled"
            task.message = "已取消"
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
