"""导入作业管理：串行执行、事件流、取消。"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .settings import JOBS_DIR, ROOT, AppConfig, ensure_dirs

TERMINAL = {"completed", "failed", "cancelled"}


def is_running(job: "Job") -> bool:
    return job.status not in TERMINAL


@dataclass
class Job:
    id: str
    conv: dict
    status: str = "pending"
    stage: str = "prepare"
    progress: int = 0
    message: str = ""
    error: str = ""
    loaded: int = 0
    foreground: bool = False
    suspended: bool = False
    created: float = field(default_factory=time.time)
    events_path: Path = Path()
    work_dir: Path = Path()
    process: subprocess.Popen | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "convKey": self.conv.get("key", ""),
            "convName": self.conv.get("name", ""),
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "loaded": self.loaded,
            "foreground": self.foreground,
            "suspended": self.suspended,
            "created": self.created,
        }


class JobManager:
    MAX_RUNNING = 2

    def __init__(self) -> None:
        ensure_dirs()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def running(self) -> list[Job]:
        return [job for job in self._jobs.values() if job.status not in TERMINAL]

    def active_for(self, conv_key: str) -> Job | None:
        for job in self.running():
            if job.conv.get("key") == conv_key:
                return job
        return None

    @staticmethod
    def _last_stage(job: Job) -> str:
        stage = ""
        try:
            for line in job.events_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("stage"):
                    stage = event["stage"]
        except OSError:
            pass
        return stage

    def _suspend(self, job: Job) -> None:
        if job.suspended or not is_running(job) or job.process is None:
            return
        if self._last_stage(job) == "ingest":
            return
        try:
            os.killpg(job.process.pid, signal.SIGSTOP)
        except ProcessLookupError:
            return
        job.suspended = True

    def _resume(self, job: Job) -> None:
        if not job.suspended or job.process is None:
            return
        try:
            os.killpg(job.process.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        job.suspended = False

    def foreground(self, conv_key: str) -> Job | None:
        """把指定会话设为前台：恢复它的任务，暂停其他会话的任务。"""
        with self._lock:
            mine = self.active_for(conv_key)
            for job in self.running():
                if job is not mine:
                    job.foreground = False
                    self._suspend(job)
            if mine is not None:
                self._resume(mine)
                mine.foreground = True
            return mine

    def start(
        self,
        conv: dict,
        cfg: AppConfig,
        ts_from: int,
        ts_to: int,
        refresh: bool = False,
        foreground: bool = True,
    ) -> Job:
        with self._lock:
            same = self.active_for(conv["key"])
            if same is not None:
                raise RuntimeError(f"该会话已有导入任务在运行：{conv.get('name', same.id)}")
            if foreground:
                for running in self.running():
                    running.foreground = False
                    self._suspend(running)
            elif len(self.running()) >= self.MAX_RUNNING:
                raise RuntimeError("后台导入任务较多，请稍后再试")
            job_id = uuid.uuid4().hex[:12]
            work_dir = JOBS_DIR / job_id
            (work_dir / "work").mkdir(parents=True, exist_ok=True)
            events_path = work_dir / "events.jsonl"
            events_path.touch()

            job = Job(
                id=job_id,
                conv=conv,
                events_path=events_path,
                work_dir=work_dir,
                foreground=foreground,
            )
            spec = {
                "conv": conv,
                "config": cfg.to_json(),
                "ts_from": int(ts_from),
                "ts_to": int(ts_to),
                "refresh": bool(refresh),
            }
            (work_dir / "job.json").write_text(
                json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._jobs[job_id] = job

            log_file = (work_dir / "worker.log").open("ab")
            job.process = subprocess.Popen(
                [sys.executable, "-m", "app.worker", str(work_dir)],
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            job.status = "running"
            threading.Thread(target=self._watch, args=(job, log_file), daemon=True).start()
            return job

    def _watch(self, job: Job, log_file) -> None:
        proc = job.process
        assert proc is not None
        code = proc.wait()
        log_file.close()
        shutil.rmtree(job.work_dir / "work", ignore_errors=True)
        job.foreground = False
        events = read_events(job.events_path)
        done = next((e for e in events if e.get("stage") == "done"), None)
        failed = next((e for e in reversed(events) if e.get("stage") == "failed"), None)
        if done and code == 0:
            job.status = "completed"
            job.progress = 100
            job.stage = "done"
            job.loaded = int(done.get("result", {}).get("count", 0))
            job.message = f"导入完成：{job.loaded} 条"
        elif job.status == "cancelled":
            job.stage = "cancelled"
        else:
            job.status = "failed"
            job.stage = "failed"
            job.error = (failed or {}).get("error") or f"worker 退出码 {code}，详见 {job.work_dir}/worker.log"

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.process is None or job.status in TERMINAL:
            return False
        job.status = "cancelled"
        job.foreground = False
        try:
            if job.suspended:
                os.killpg(job.process.pid, signal.SIGCONT)
                job.suspended = False
            os.killpg(job.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return True


def read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def apply_events(job: Job) -> Job:
    """把事件流折叠成作业状态，供前端轮询使用。"""
    for event in read_events(job.events_path):
        stage = event.get("stage")
        if stage:
            job.stage = stage
        if "progress" in event:
            job.progress = max(job.progress, int(event["progress"]))
        if event.get("message"):
            job.message = event["message"]
        if stage == "failed":
            job.error = event.get("error", "导入失败")
            job.status = "failed"
    return job


jobs = JobManager()


def _resume_suspended_on_exit() -> None:
    for job in jobs.running():
        if job.suspended:
            jobs._resume(job)


atexit.register(_resume_suspended_on_exit)
