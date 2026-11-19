"""聊天图片按需回源：先用本地缓存，未命中再从设备拉取单个文件。"""

from __future__ import annotations

import io
import re
import shlex
import threading
import time
from pathlib import Path

from PIL import Image

from . import adb
from .settings import MEDIA_CACHE_DIR, ensure_dirs, load_config

CACHE_DIR = MEDIA_CACHE_DIR
FAIL_TTL = 300
MAX_CONCURRENT = 3

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_fail_until: dict[str, float] = {}
_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT)
_EXTENSIONS = ("jpg", "png", "gif", "webp", "bmp", "bin")
_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "bin": "application/octet-stream",
}


def media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")


def _lock_for(md5: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(md5)
        if lock is None:
            lock = threading.Lock()
            _locks[md5] = lock
        return lock


def _cached(md5: str) -> Path | None:
    for ext in _EXTENSIONS:
        path = CACHE_DIR / f"{md5}.{ext}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _device_path(md5: str) -> str:
    from src.dataParsing.protoDataParsing import crc64

    filename = "Cache_" + hex(crc64("chatimg:" + md5)).replace("0x", "")
    return f"{adb.DEFAULT_CHATIMG}/{filename[-3:]}/{filename}"


def _sniff_extension(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").lower()
    except OSError:
        return "bin"
    return {"jpeg": "jpg", "mpo": "jpg"}.get(fmt, fmt or "bin")


def ensure(md5: str) -> Path | None:
    """返回本地缓存的图片路径；未命中时通过 adb 拉取单个文件。"""
    md5 = md5.upper()
    if not _MD5_RE.match(md5):
        return None
    ensure_dirs()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cached(md5)
    if cached is not None:
        return cached
    with _lock_for(md5):
        cached = _cached(md5)
        if cached is not None:
            return cached
        if _fail_until.get(md5, 0) > time.time():
            return None
        cfg = load_config()
        if not cfg.is_adb:
            return None
        remote = _device_path(md5)
        with _semaphore:
            try:
                data = adb._run(
                    cfg,
                    ["exec-out", f"cat {shlex.quote(remote)}"],
                    timeout=30,
                )
            except adb.AdbError:
                data = b""
        if len(data) < 100:
            _fail_until[md5] = time.time() + FAIL_TTL
            return None
        ext = _sniff_extension(data)
        if ext == "bin":
            _fail_until[md5] = time.time() + FAIL_TTL
            return None
        target = CACHE_DIR / f"{md5}.{ext}"
        tmp = CACHE_DIR / f"{md5}.tmp"
        tmp.write_bytes(data)
        tmp.replace(target)
        _fail_until.pop(md5, None)
        return target
