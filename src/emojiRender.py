"""彩色 emoji 光栅化：只使用系统彩色 emoji 字体。

Web 端通过 QQHE_EMOJI_CACHE 指定持久缓存目录；桌面端默认写到
output/temp/emoji_render。缓存目录按字体组合分子目录，字体变化后自动失效。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_TARGET_PX = 160
# CBDT 字体只支持原生字号（Noto 为 109），逐个尝试直到 FreeType 接受。
_FONT_SIZES = (160, 137, 128, 109, 96, 72, 64)
# 判断渲染结果是否为彩色：RGB 通道最大差值超过该阈值即视为有颜色。
_CHROMA_THRESHOLD = 20


def _cache_root() -> Path:
    raw = os.environ.get("QQHE_EMOJI_CACHE", "").strip()
    base = Path(raw) if raw else Path("output/temp/emoji_render")
    base.mkdir(parents=True, exist_ok=True)
    return base


@lru_cache(maxsize=1)
def _fontconfig_emoji() -> str:
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "emoji"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    path = result.stdout.strip()
    return path if path and Path(path).is_file() else ""


@lru_cache(maxsize=1)
def font_candidates() -> tuple[str, ...]:
    """按优先级返回可用的彩色 emoji 字体路径。"""
    paths: list[Path] = []
    configured = os.environ.get("QQHE_EMOJI_FONT", "").strip()
    if configured:
        paths.append(Path(configured))
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        paths.append(Path(windir) / "Fonts" / "seguiemj.ttf")
    elif sys.platform == "darwin":
        paths.append(Path("/System/Library/Fonts/Apple Color Emoji.ttc"))
    else:
        fontconfig = _fontconfig_emoji()
        if fontconfig:
            paths.append(Path(fontconfig))
        paths.append(Path("/usr/share/fonts/noto/NotoColorEmoji.ttf"))
        paths.append(Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"))

    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen or not Path(resolved).is_file():
            continue
        seen.add(resolved)
        result.append(resolved)
    return tuple(result)


@lru_cache(maxsize=None)
def _load_font(path: str) -> tuple[ImageFont.FreeTypeFont | None, int]:
    for size in _FONT_SIZES:
        try:
            font = ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
        return font, size
    return None, 0


def _render(font: ImageFont.FreeTypeFont, size: int, char: str) -> Image.Image | None:
    canvas = max(size * 2, 96)
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((0, 0), char, font=font, embedded_color=True)
    bbox = image.getbbox()
    if bbox is None:
        return None
    image = image.crop(bbox)
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return square.resize((_TARGET_PX, _TARGET_PX), Image.Resampling.LANCZOS)


def _has_color(image: Image.Image) -> bool:
    raw = image.tobytes()
    for index in range(0, len(raw), 4):
        if raw[index + 3] == 0:
            continue
        red, green, blue = raw[index], raw[index + 1], raw[index + 2]
        if max(red, green, blue) - min(red, green, blue) > _CHROMA_THRESHOLD:
            return True
    return False


def _save(image: Image.Image, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".png")
    os.close(fd)
    try:
        image.save(tmp_name, format="PNG")
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def emoji_png(char: str) -> str | None:
    """把单个 emoji（可含 ZWJ/肤色序列）渲染为 PNG，返回文件路径。"""
    name = "emoji_u" + "_".join(format(ord(c), "x") for c in char) + ".png"
    candidates = font_candidates()
    font_key = hashlib.sha1("|".join(candidates).encode()).hexdigest()[:8]
    target = _cache_root() / font_key / name
    if target.is_file():
        return str(target)

    fallback: Image.Image | None = None
    for path in candidates:
        font, size = _load_font(path)
        if font is None:
            continue
        try:
            image = _render(font, size, char)
        except Exception:
            continue
        if image is None:
            continue
        if _has_color(image):
            _save(image, target)
            return str(target)
        if fallback is None:
            fallback = image

    if fallback is None:
        return None
    _save(fallback, target)
    return str(target)
