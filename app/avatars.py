"""QQ 头像按需拉取与本地缓存。

头像取自 qlogo（100×100 JPEG）。缓存目录 data/avatars/<uin>.jpg，
同一 uin 的并发请求去重；上游限流时短暂负缓存并降低并发，避免反复打爆。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import requests

from .settings import AVATAR_DIR, ensure_dirs

AVATAR_URLS = (
    "https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100",
    "https://q.qlogo.cn/headimg_dl?dst_uin={uin}&spec=100",
)
FAIL_TTL = 300
MAX_CONCURRENT = 4

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_fail_until: dict[str, float] = {}
_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT)
_session = requests.Session()
_session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) qqhistory-export",
        "Accept": "image/avif,image/webp,image/jpeg,*/*",
    }
)


def valid_uin(uin: str) -> bool:
    return uin.isdigit() and 4 <= len(uin) <= 12


def path_for(uin: str) -> Path:
    return AVATAR_DIR / f"{uin}.jpg"


def _lock_for(uin: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(uin)
        if lock is None:
            lock = threading.Lock()
            _locks[uin] = lock
        return lock


def _is_jpeg(data: bytes) -> bool:
    return len(data) > 100 and data[:2] == b"\xff\xd8"


def _download(uin: str, timeout: float) -> bytes | None:
    for template in AVATAR_URLS:
        for attempt in range(2):
            try:
                response = _session.get(template.format(uin=uin), timeout=timeout)
            except requests.RequestException:
                time.sleep(0.15)
                continue
            if (
                response.status_code == 200
                and response.headers.get("Content-Type", "").startswith("image/")
                and _is_jpeg(response.content)
            ):
                return response.content
            if response.status_code in (403, 429) or response.status_code >= 500:
                time.sleep(0.3)
                continue
            break
    return None


def ensure(uin: str, timeout: float = 8.0) -> Path | None:
    """返回本地头像路径；缺失时联网拉取。失败返回 None。"""
    if not valid_uin(uin):
        return None
    ensure_dirs()
    target = path_for(uin)
    if target.is_file() and target.stat().st_size > 0:
        return target
    with _lock_for(uin):
        if target.is_file() and target.stat().st_size > 0:
            return target
        if _fail_until.get(uin, 0) > time.time():
            return None
        with _semaphore:
            content = _download(uin, timeout)
        if content is None:
            _fail_until[uin] = time.time() + FAIL_TTL
            return None
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
        _fail_until.pop(uin, None)
        return target
