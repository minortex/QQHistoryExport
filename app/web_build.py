"""启动 Web 服务前保证前端 dist 与源码一致。

`uv run qqhe` 会先对比 web/src 等源码的内容哈希与 dist 里的构建清单：
不一致就自动 `npm run build`，避免服务端一直跑旧前端。
没有 web 源码（例如只装了 wheel）时直接使用随包发布的 dist。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DIST_DIR = WEB_DIR / "dist"
MANIFEST = DIST_DIR / ".qqhe-build.json"
INDEX = DIST_DIR / "index.html"

_EXTRA_SOURCES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "vite.config.js",
    "tsconfig.json",
    "tsconfig.node.json",
    "env.d.ts",
)


def _source_files() -> list[Path]:
    files: list[Path] = []
    for name in _EXTRA_SOURCES:
        path = WEB_DIR / name
        if path.is_file():
            files.append(path)
    for dirname in ("src", "public"):
        base = WEB_DIR / dirname
        if base.is_dir():
            files.extend(
                path for path in sorted(base.rglob("*")) if path.is_file()
            )
    return files


def source_hash() -> str:
    digest = hashlib.sha256()
    for path in _source_files():
        digest.update(path.relative_to(WEB_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_npm(npm: str, args: list[str]) -> None:
    subprocess.run(
        [npm, *args],
        cwd=WEB_DIR,
        check=True,
        shell=(os.name == "nt"),
    )


def ensure_web_dist(log=print) -> bool:
    """确保 dist 是最新构建；返回是否执行了构建。"""
    if not WEB_DIR.is_dir():
        return False

    digest = source_hash()
    if INDEX.is_file() and MANIFEST.is_file():
        try:
            built = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            built = {}
        if built.get("source_hash") == digest:
            return False

    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "前端源码比 dist 新，但找不到 npm。请安装 Node.js 后重新运行，"
            "或先手动执行 cd web && npm run build。"
        )

    if not (WEB_DIR / "node_modules").is_dir():
        log("安装前端依赖…")
        _run_npm(npm, ["ci"] if (WEB_DIR / "package-lock.json").is_file() else ["install"])

    log("前端源码有更新，正在构建 Web UI…")
    _run_npm(npm, ["run", "build"])
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            {"source_hash": digest, "built_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log("前端构建完成。")
    return True
