"""聊天图片按需回源：本地缓存 → 设备原图 → 图片 CDN → 设备缩略图。"""

from __future__ import annotations

import io
import re
import shlex
import threading
import time
from pathlib import Path

import requests
from PIL import Image

from . import adb
from .settings import MEDIA_CACHE_DIR, ensure_dirs, load_config

CACHE_DIR = MEDIA_CACHE_DIR
FAIL_TTL = 30
MAX_CONCURRENT = 3
HTTP_TIMEOUT = (5, 20)
HTTP_UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/122.0.6261.120 Mobile Safari/537.36 V1_AND_SQ_8.8.90_1_HD"
)
# 群图/好友图的 CDN 直链，md5 就是 PicRec 里的 md5（chatimg:<md5> 的来源）
_CDN_URLS = {
    "troop": "http://gchat.qpic.cn/gchatpic_new/0/0-0-{md5}/0",
    "friend": "http://c2cpicdw.qpic.cn/offpic_new/0/{md5}/0",
}

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


def conversation_kind(conv_key: str) -> str:
    """troop:123 / troop_123 → troop（前端把冒号换成了下划线）。"""
    for sep in (":", "_"):
        if sep in conv_key:
            return conv_key.split(sep, 1)[0]
    return conv_key


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


def _device_path(md5: str, protocol: str = "chatimg") -> str:
    from src.dataParsing.protoDataParsing import crc64

    root = {
        "chatimg": adb.DEFAULT_CHATIMG,
        "chatraw": adb.DEFAULT_CHATRAW,
        "chatthumb": adb.DEFAULT_CHATTHUMB,
    }[protocol]
    filename = "Cache_" + hex(crc64(f"{protocol}:{md5}")).replace("0x", "")
    return f"{root}/{filename[-3:]}/{filename}"


def _sniff_extension(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").lower()
    except OSError:
        return "bin"
    return {"jpeg": "jpg", "mpo": "jpg"}.get(fmt, fmt or "bin")


def _fetch_http(md5: str, kind: str) -> bytes:
    """按 md5 从图片 CDN 直接取原图，不需要登录态。"""
    template = _CDN_URLS.get(kind)
    if not template:
        return b""
    try:
        response = requests.get(
            template.format(md5=md5),
            headers={"User-Agent": HTTP_UA},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException:
        return b""
    if response.status_code != 200:
        return b""
    return response.content


def _fetch_device(md5: str, protocol: str = "chatimg") -> bytes:
    cfg = load_config()
    if not cfg.is_adb:
        return b""
    with _semaphore:
        try:
            return adb._run(
                cfg,
                ["exec-out", f"cat {shlex.quote(_device_path(md5, protocol))}"],
                timeout=30,
            )
        except adb.AdbError:
            return b""


def ensure(md5: str, kind: str = "") -> Path | None:
    """返回本地缓存路径；未命中时依次尝试设备文件与图片 CDN。"""
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
        data = _fetch_device(md5, "chatimg")
        ext = _sniff_extension(data) if len(data) >= 100 else "bin"
        if ext == "bin":
            data = _fetch_device(md5, "chatraw")
            ext = _sniff_extension(data) if len(data) >= 100 else "bin"
        if ext == "bin":
            with _semaphore:
                data = _fetch_http(md5, kind)
            ext = _sniff_extension(data) if data else "bin"
        if ext == "bin":
            # 原图被清理且 CDN 已过期时，退而使用手机里残留的缩略图
            data = _fetch_device(md5, "chatthumb")
            ext = _sniff_extension(data) if len(data) >= 100 else "bin"
        if ext == "bin":
            _fail_until[md5] = time.time() + FAIL_TTL
            return None
        target = CACHE_DIR / f"{md5}.{ext}"
        tmp = CACHE_DIR / f"{md5}.tmp"
        tmp.write_bytes(data)
        tmp.replace(target)
        _fail_until.pop(md5, None)
        return target
