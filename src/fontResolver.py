"""解析 PDF 使用的中文字体。

顺序：QQHE_FONT_PATH → 仓库 resources/fonts/ → 系统字体。
`fontName = auto` 时按平台挑选系统默认中文字体；所有候选都会先用
reportlab 实际加载一次，排除 CFF/PostScript 轮廓的 TTC（reportlab 不支持）。
找不到时返回 None 并附带警告，由调用方降级到内置 STSong，而不是中断导出。
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLED_DIR = ROOT / "resources" / "fonts"
FONT_SUFFIXES = {".ttf", ".ttc"}

AUTO_NAMES = {"", "auto", "system", "default"}

# 找不到指定字体时按顺序尝试的字体族与文件名
FALLBACK_FAMILIES = (
    "simhei",
    "simsun",
    "microsoft yahei",
    "sarasa gothic sc",
    "wenquanyi micro hei",
    "dengxian",
    "noto sans cjk sc",
    "arial unicode ms",
    "segoe ui",
)
FALLBACK_FILES = (
    "simhei.ttf",
    "simsun.ttc",
    "msyh.ttc",
    "msyh.ttf",
    "arialuni.ttf",
    "segoeui.ttf",
)

_STYLE_PENALTY = (
    "italic",
    "bold",
    "light",
    "black",
    "medium",
    "thin",
    "semibold",
    "extralight",
    "demibold",
)


def _is_font(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in FONT_SUFFIXES


@functools.lru_cache(maxsize=None)
def _embeddable(path: str, index: int) -> bool:
    """reportlab 只能嵌入 TrueType 轮廓；用它实际试探一次。"""
    try:
        from reportlab.pdfbase.ttfonts import TTFont

        TTFont("__font_probe__", path, subfontIndex=index)
        return True
    except Exception:
        return False


def _style_rank(path: str) -> int:
    name = Path(path).name.lower()
    if "regular" in name:
        return 0
    return sum(2 for word in _STYLE_PENALTY if word in name)


def _system_dirs() -> list[Path]:
    if sys.platform == "win32":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        dirs = [windir / "Fonts"]
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            dirs.append(Path(localappdata) / "Microsoft" / "Windows" / "Fonts")
        return dirs
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ]
    return [
        Path.home() / ".local" / "share" / "fonts",
        Path.home() / ".fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]


def _fontconfig_pairs(name: str) -> list[tuple[str, int]]:
    if not shutil.which("fc-list"):
        return []
    if name in {"sans-serif", "serif", "monospace"}:
        command = ["fc-match", "-f", "%{file}\t%{index}\n", f"{name}:lang=zh-cn"]
    else:
        command = ["fc-list", "-f", "%{file}\t%{index}\n", f":family={name}"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []

    pairs: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        path, _, raw_index = line.partition("\t")
        path = path.strip()
        if not _is_font(Path(path)):
            continue
        try:
            index = int(raw_index.strip() or 0)
        except ValueError:
            index = 0
        pairs.append((path, index))
    pairs.sort(key=lambda pair: _style_rank(pair[0]))
    return pairs


def _directory_pairs(names: list[str]) -> list[tuple[str, int]]:
    targets: set[str] = set()
    for name in names:
        lowered = name.replace("_", " ").lower()
        if Path(lowered).suffix in FONT_SUFFIXES:
            targets.add(lowered)
        else:
            targets.update({f"{lowered}.ttf", f"{lowered}.ttc"})

    pairs: list[tuple[str, int]] = []
    for base in _system_dirs():
        if not base.is_dir():
            continue
        for target in sorted(targets):
            candidate = base / target
            if _is_font(candidate):
                pairs.append((str(candidate), 0))
        try:
            for path in base.rglob("*"):
                if path.name.lower() in targets and _is_font(path):
                    pairs.append((str(path), 0))
        except OSError:
            continue
    return pairs


def _candidates(name: str) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    bundled = BUNDLED_DIR / f"{name}.ttf"
    if _is_font(bundled):
        pairs.append((str(bundled), 0))
    pairs.extend(_fontconfig_pairs(name))
    pairs.extend(_directory_pairs([name]))
    return pairs


def _first_usable(names: tuple[str, ...] | list[str]) -> tuple[str, int] | None:
    seen: set[str] = set()
    for name in names:
        for path, index in _candidates(name):
            key = f"{path}#{index}"
            if key in seen:
                continue
            seen.add(key)
            if _embeddable(path, index):
                return str(Path(path).resolve()), index
    return None


def _system_names() -> list[str]:
    if sys.platform == "win32":
        return ["msyh", "microsoft yahei", "simhei", "dengxian", "simsun"]
    if sys.platform == "darwin":
        return ["pingfang", "hiragino sans gb", "heiti sc", "songti sc"]
    return [
        "sans-serif",
        "sarasa gothic sc",
        "wenquanyi micro hei",
        "dengxian",
        "simhei",
        "noto sans cjk sc",
    ]


def resolve_font(name: str) -> tuple[str | None, int, str | None]:
    """返回 (字体路径, TTC 子字体索引, 警告)；找不到时路径为 None，不抛异常。"""
    configured = os.environ.get("QQHE_FONT_PATH", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve()), 0, None

    if name.strip().lower() in AUTO_NAMES:
        chosen = _first_usable(_system_names())
        if chosen:
            return chosen[0], chosen[1], None
        return None, 0, "未找到可用于 PDF 的中文 TrueType 字体，将使用内置 STSong"

    chosen = _first_usable([name])
    if chosen:
        return chosen[0], chosen[1], None

    for fallback in FALLBACK_FAMILIES:
        if fallback.lower() == name.lower():
            continue
        chosen = _first_usable([fallback])
        if chosen:
            return chosen[0], chosen[1], f"未找到字体 {name}，已改用 {Path(chosen[0]).name}"

    chosen = _first_usable(FALLBACK_FILES)
    if chosen:
        return chosen[0], chosen[1], f"未找到字体 {name}，已改用 {Path(chosen[0]).name}"

    return None, 0, f"未找到字体 {name}，将使用内置 STSong，中文排版可能略有差异"
