"""可重建缓存的容量管理。

预算只覆盖能从手机/上游重新取回的内容：raw 窗口、按需图片缓存、头像。
消息库与数据目录里的 media/ 不参与淘汰。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .settings import AVATAR_DIR, CACHE_LIMIT_MB, MEDIA_CACHE_DIR, RAW_DIR

CACHE_DIRS = (RAW_DIR, MEDIA_CACHE_DIR, AVATAR_DIR)


def _files() -> list[tuple[float, int, Path]]:
    entries: list[tuple[float, int, Path]] = []
    for root in CACHE_DIRS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
    return entries


def cache_bytes() -> int:
    return sum(size for _, size, _ in _files())


def _prune_empty() -> None:
    for root in CACHE_DIRS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass


def sweep_cache(limit_mb: int | None = None, emit=None) -> int:
    """按 mtime 淘汰最旧的缓存文件，直到总量不超过预算；返回释放字节数。"""
    budget = max(0, CACHE_LIMIT_MB if limit_mb is None else limit_mb) * 1024 * 1024
    entries = sorted(_files(), key=lambda item: item[0])
    total = sum(size for _, size, _ in entries)
    if total <= budget:
        return 0
    freed = 0
    for _, size, path in entries:
        try:
            path.unlink()
        except OSError:
            continue
        freed += size
        total -= size
        if total <= budget:
            break
    _prune_empty()
    message = f"缓存清理 {freed / 1024 / 1024:.1f} MB，当前 {total / 1024 / 1024:.1f} MB"
    if emit is not None:
        emit({"stage": "cache", "message": message})
    else:
        print(f"[qqhe] {message}", file=sys.stderr)
    return freed


def prune_raw(conv_key: str, keep: int = 2) -> int:
    """只保留该会话最近的若干个原始窗口，其余立即删除；返回释放字节数。"""
    base = RAW_DIR / conv_key.replace(":", "_")
    if not base.is_dir():
        return 0
    windows = sorted(
        (path for path in base.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )
    freed = 0
    for window in windows[:-keep] if keep > 0 else windows:
        for path in window.rglob("*"):
            if path.is_file():
                try:
                    freed += path.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(window, ignore_errors=True)
    return freed
