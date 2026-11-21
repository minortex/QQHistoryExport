"""把选中区间的消息导出为 HTML、PDF 或 Markdown。

HTML：图片/头像/表情全部内联为 base64，单文件即可分享；超过 12 MB 的
音视频/附件才落到 `<名字>_assets/`。页面自带右侧日期轴与当前日期浮标。
PDF：复用现有 reportlab 排版器，按它要求的 output/ 目录结构临时组装后运行。
Markdown：纯文本，只保留文字与媒体占位符，不生成或引用任何图片。
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from . import avatars, media_cache, store
from .settings import AVATAR_DIR, DATA_DIR, MEDIA_CACHE_DIR, MEDIA_DIR, ROOT

MEDIA_KINDS = ("images", "videos", "voices", "emoticons", "files", "temp", "senders")
SYSTEM_TYPES = ("tip", "revoke", "nudge")
PLACEHOLDER_AVATAR = "senders/placeholder.jpg"
EMBED_LIMIT = 12 * 1024 * 1024


def media_root(conv_key: str) -> Path:
    return MEDIA_DIR / conv_key.replace(":", "_")


def _block_list(payload: dict) -> list[dict]:
    content = payload.get("c")
    if not content:
        return []
    if isinstance(content, list):
        return content
    return [{"t": payload.get("t"), "c": content}]


def _text(block: dict) -> str:
    content = block.get("c") or {}
    return str(content.get("m") or content.get("text") or "")


def _is_system(payload: dict) -> bool:
    kind = payload.get("t")
    if kind in SYSTEM_TYPES:
        return True
    if kind == "uns":
        block = _block_list(payload)[0] if _block_list(payload) else {}
        return (block.get("c") or {}).get("type") == "tip"
    return False


def _human_size(size) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _data_uri(source: Path) -> str:
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    payload = base64.b64encode(source.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


class _Assets:
    """媒体引用解析：默认 base64 内联，超限时才复制到 assets 目录。"""

    def __init__(self, conv_key: str, base: Path) -> None:
        self.conv_key = conv_key
        self.base = base
        self.prefix = base.name
        self.media_dir = media_root(conv_key)
        self.refs: dict[str, str] = {}

    def _source(self, path: str) -> Path | None:
        if path.startswith("lazy:"):
            md5 = path[5:]
            cached = next(iter(MEDIA_CACHE_DIR.glob(f"{md5}.*")), None)
            return cached or media_cache.ensure(
                md5, media_cache.conversation_kind(self.conv_key), store.media_link(md5)
            )
        if path.startswith("avatars/"):
            candidate = AVATAR_DIR / Path(path).name
            return candidate if candidate.is_file() else None
        candidate = self.media_dir / path
        return candidate if candidate.is_file() else None

    def _url(self, source: Path, relative: Path) -> str:
        if source.stat().st_size <= EMBED_LIMIT:
            return _data_uri(source)
        target = self.base / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return f"{self.prefix}/{relative.as_posix()}"

    def resolve(self, raw: str | None) -> str:
        path = str(raw or "").strip()
        if not path:
            return ""
        if path in self.refs:
            return self.refs[path]
        source = self._source(path)
        if source is None:
            ref = ""
        elif path.startswith("lazy:"):
            ref = self._url(source, Path("media") / "lazy" / source.name)
        else:
            ref = self._url(source, Path("media") / path)
        self.refs[path] = ref
        return ref

    def avatar(self, uin: str, stored: str) -> str:
        if stored:
            ref = self.resolve(stored)
            if ref:
                return ref
        source = avatars.ensure(uin)
        if source is not None and source.is_file():
            return self._url(source, Path("avatars") / source.name)
        return ""


def _render_block(block: dict, assets: _Assets) -> str:
    kind = block.get("t") or ""
    content = block.get("c") or {}
    if kind == "m":
        return f'<div class="text">{html.escape(_text(block))}</div>'
    if kind == "reply":
        return (
            '<div class="reply"><span class="reply-label">引用</span>'
            f'{html.escape(_text(block))}</div>'
        )
    if kind in ("img", "marketface", "qqemoji"):
        rel = assets.resolve(content.get("path"))
        if not rel:
            lazy = str(content.get("path") or "").startswith("lazy:")
            hint = "图片未缓存，连接手机后重试" if lazy else "图片不可用"
            return f'<div class="missing">[{hint}]</div>'
        label = html.escape(str(content.get("name") or "图片"))
        css = "emoji" if kind == "qqemoji" else "image"
        return f'<a href="{rel}" target="_blank"><img class="{css}" src="{rel}" alt="{label}"></a>'
    if kind == "voice":
        rel = assets.resolve(content.get("path"))
        if not rel:
            return '<div class="missing">[语音不可用]</div>'
        return f'<audio controls preload="none" src="{rel}"></audio>'
    if kind == "video":
        rel = assets.resolve(content.get("path"))
        if not rel:
            return '<div class="missing">[视频不可用]</div>'
        poster = assets.resolve(content.get("thumb"))
        poster_attr = f' poster="{poster}"' if poster else ""
        return f'<video controls preload="metadata" src="{rel}"{poster_attr}></video>'
    if kind == "file":
        rel = assets.resolve(content.get("path"))
        name = html.escape(str(content.get("name") or content.get("path") or "文件"))
        size = _human_size(content.get("size"))
        size_html = f" <em>{size}</em>" if size else ""
        if rel:
            return f'<a class="file" href="{rel}" download>📎 {name}{size_html}</a>'
        return f'<div class="missing">📎 {name}{size_html}</div>'
    if kind in ("uns", "err"):
        return f'<div class="missing">{html.escape(_text(block))}</div>'
    return f'<div class="missing">[{html.escape(kind or "未支持")}]</div>'


STYLE = """
:root { --bg:#f6f7f9; --surface:#fff; --border:#e3e5e8; --fg:#202124; --muted:#5f6368;
        --accent:#1a73e8; --accent-weak:#e8f0fe; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.6 "Noto Sans CJK SC","PingFang SC",Roboto,sans-serif; }
main { max-width: 900px; margin: 0 auto; padding: 24px 20px 60px; }
header { margin-bottom: 18px; }
header h1 { font-size: 18px; margin: 0 0 4px; }
header .meta { color: var(--muted); font-size: 12px; }
.day { display:flex; justify-content:center; margin: 18px 0 10px; scroll-margin-top: 16px; }
.day span { background: var(--border); color: var(--muted); font-size: 11px;
            padding: 2px 10px; border-radius: 999px; }
.msg { display:flex; gap:10px; margin: 8px 0; }
.msg.self { flex-direction: row-reverse; }
.avatar { width:34px; height:34px; border-radius:50%; object-fit:cover; background:var(--border);
          flex:none; }
.avatar.blank { display:flex; align-items:center; justify-content:center; color:var(--muted);
                font-size:13px; }
.body { min-width:0; max-width: 76%; }
.meta { display:flex; gap:8px; font-size:11px; color:var(--muted); margin: 0 4px 2px; }
.msg.self .meta { justify-content: flex-end; }
.bubble { background: var(--surface); border-radius: 12px; padding: 7px 11px;
          box-shadow: 0 1px 2px rgba(60,64,67,.12); display:flex; flex-direction:column; gap:5px; }
.msg.self .bubble { background: var(--accent-weak); box-shadow:none; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
.reply { background: #f1f3f4; border-left:3px solid var(--accent); border-radius:6px;
         padding: 4px 8px; color: var(--muted); font-size: 12px; }
.reply-label { color: var(--accent); margin-right: 4px; }
.image { max-width: 320px; max-height: 320px; border-radius: 8px; display:block; }
.image.emoji { max-width: 120px; max-height: 120px; }
audio { max-width: 280px; }
video { max-width: 360px; border-radius: 8px; }
.file { color: var(--accent); text-decoration: none; }
.missing { color: var(--muted); font-size: 12px; }
.system { text-align:center; color: var(--muted); font-size:12px; margin: 6px 0; }
footer { margin-top: 28px; color: var(--muted); font-size: 11px; text-align:center; }

.rail { position: fixed; right: 8px; top: 50%; transform: translateY(-50%);
        display:flex; flex-direction:column; gap:4px; max-height: 78vh;
        padding:4px 2px; z-index:5; }
.rail-years { display:flex; flex-direction:column; gap:1px; padding-bottom:4px;
              border-bottom:1px solid var(--border); flex:none; }
.rail-year-btn { border:0; background:none; padding:1px 5px; border-radius:4px;
                 color:var(--muted); font-size:10px; line-height:1.25; text-align:left;
                 cursor:pointer; opacity:.7; }
.rail-year-btn.active { color: var(--accent); font-weight:600; opacity:1; }
.rail-days { position: relative; display:flex; flex-direction:column; gap:2px; min-height:0;
             overflow-y:auto; scrollbar-width:thin; }
.rail-days::-webkit-scrollbar { width: 3px; }
.rail-days::-webkit-scrollbar-thumb { background: var(--border); border-radius:3px; }
.rail-year { position: sticky; top: 0; background: var(--bg); color: var(--muted);
             font-size: 10px; font-weight: 600; padding: 3px 5px 1px; opacity: .75; }
.rail-item { display:flex; align-items:center; gap:6px; text-decoration:none;
             color:var(--muted); font-size:10px; line-height:1.25; opacity:.55;
             padding:1px 5px; border-radius:4px; font-variant-numeric: tabular-nums;
             white-space: nowrap; }
.rail-item i { width:8px; height:1px; background:currentColor; flex:none; }
.rail-item.active { color: var(--accent); opacity:1; font-weight:600; }
.rail-item.active i { width:14px; height:2px; }
.day-pill { position: fixed; top: 10px; left:50%; padding:3px 12px; border-radius:999px;
            background: var(--surface); color: var(--muted); font-size:12px; z-index:6;
            box-shadow: 0 1px 4px rgba(0,0,0,.12); opacity:0;
            transform: translateX(-50%) translateY(-8px); transition: .2s ease;
            pointer-events:none; }
.day-pill.show { opacity:1; transform: translateX(-50%) translateY(0); }
@media (max-width: 980px) { .rail { display:none; } .day-pill { display:none; } }
"""

SCRIPT = """
(() => {
  const items = [...document.querySelectorAll('.rail-item')];
  const days = [...document.querySelectorAll('.day')];
  const pill = document.getElementById('dayPill');
  const railDays = document.getElementById('railDays');
  const yearButtons = [...document.querySelectorAll('.rail-year-btn')];
  if (!items.length || !days.length) return;
  for (const button of yearButtons) {
    button.addEventListener('click', () => {
      const header = railDays && railDays.querySelector(`[data-year="${button.dataset.year}"]`);
      if (!header) return;
      railDays.scrollTop = header.offsetTop;
      const item = header.nextElementSibling;
      const anchor = item && item.getAttribute('href');
      const target = anchor && document.querySelector(anchor);
      if (target) target.scrollIntoView();
    });
  }
  let frame = 0;
  const update = () => {
    frame = 0;
    let current = days[0];
    for (const day of days) {
      if (day.getBoundingClientRect().top <= 90) current = day;
      else break;
    }
    const id = current.id.replace(/^d-/, '');
    for (const item of items) item.classList.toggle('active', item.dataset.day === id);
    for (const button of yearButtons) {
      button.classList.toggle('active', button.dataset.year === id.slice(0, 4));
    }
    if (pill) {
      const label = current.querySelector('span');
      pill.textContent = label ? label.textContent : '';
      pill.classList.toggle('show', window.scrollY > 160);
    }
  };
  addEventListener('scroll', () => {
    if (!frame) frame = requestAnimationFrame(update);
  }, { passive: true });
  update();
})();
"""


def _message_total(conv_key: str, ts_from: int, ts_to: int, sender: str,
                   ids: list[int] | None) -> int:
    if ids:
        return len(set(ids))
    return store.count_messages(conv_key, ts_from, ts_to, sender)


def _message_source(conv_key: str, ts_from: int, ts_to: int, sender: str,
                    ids: list[int] | None):
    if ids:
        return store.iter_messages_by_ids(conv_key, ids)
    return store.iter_messages(conv_key, ts_from, ts_to, sender)


def export_html(
    conv: dict,
    ts_from: int,
    ts_to: int,
    target: Path,
    sender: str = "",
    progress=None,
    ids: list[int] | None = None,
) -> Path:
    base = target.with_name(target.stem + "_assets")
    assets = _Assets(conv["key"], base)
    senders = {item["uin"]: item for item in store.list_senders(conv["key"])}
    total = _message_total(conv["key"], ts_from, ts_to, sender, ids)
    rows: list[str] = []
    day_links: list[tuple[str, str]] = []
    last_day = ""
    names: dict[str, str] = {}
    for index, message in enumerate(
        _message_source(conv["key"], ts_from, ts_to, sender, ids), 1
    ):
        payload = message["payload"]
        ts = message["ts"]
        day_key = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day_key != last_day:
            stamp = time.localtime(ts)
            day_id = f"d-{day_key}"
            rows.append(
                f'<div class="day" id="{day_id}">'
                f'<span>{time.strftime("%Y年%m月%d日", stamp)}</span></div>'
            )
            day_links.append((day_id, f"{stamp.tm_mon}/{stamp.tm_mday}"))
            last_day = day_key
        stamp = time.strftime("%H:%M", time.localtime(ts))
        if _is_system(payload):
            text = " ".join(_text(block) for block in _block_list(payload)) or payload.get("t", "")
            rows.append(f'<div class="system">{html.escape(text)}</div>')
        else:
            uin = message["sender"]
            info = senders.get(uin) or {}
            name = names.setdefault(uin, info.get("name") or uin)
            avatar = assets.avatar(uin, info.get("avatar") or "")
            if avatar:
                avatar_html = f'<img class="avatar" src="{avatar}" alt="{html.escape(name)}">'
            else:
                avatar_html = f'<div class="avatar blank">{html.escape(name[:1])}</div>'
            body = "".join(_render_block(block, assets) for block in _block_list(payload))
            own = " self" if uin == str(conv.get("uin")) else ""
            rows.append(
                f'<div class="msg{own}">{avatar_html}<div class="body">'
                f'<div class="meta"><span>{html.escape(name)}</span><span>{stamp}</span></div>'
                f'<div class="bubble">{body}</div></div></div>'
            )
        if progress:
            progress(index, total)
    rail_parts: list[str] = []
    years: list[str] = []
    current_year = ""
    for day_id, label in day_links:
        year = day_id[2:6]
        if year != current_year:
            rail_parts.append(f'<div class="rail-year" data-year="{year}">{year}</div>')
            years.append(year)
            current_year = year
        rail_parts.append(
            f'<a class="rail-item" href="#{day_id}" data-day="{day_id[2:]}"><i></i>'
            f"<span>{label}</span></a>"
        )
    rail_years = "".join(
        f'<button class="rail-year-btn" data-year="{year}">{year}</button>' for year in years
    )
    rail = (
        f'<div class="rail-years">{rail_years}</div>'
        f'<div class="rail-days" id="railDays">{"".join(rail_parts)}</div>'
    )
    title = html.escape(conv.get("name") or conv["key"])
    span = (
        f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_from))} ~ '
        f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_to))}'
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{STYLE}</style></head>
<body><main>
<header><h1>{title}</h1><div class="meta">{span} · {total} 条 · {len(day_links)} 天 · 导出于 {time.strftime("%Y-%m-%d %H:%M")}</div></header>
{''.join(rows)}
<footer>由 QQHistoryExport 生成</footer>
</main>
<nav class="rail" aria-label="日期索引">{rail}</nav>
<div class="day-pill" id="dayPill"></div>
<script>{SCRIPT}</script>
</body></html>
"""
    target.write_text(document, encoding="utf-8")
    return target


def _md_block(block: dict) -> str:
    """把单个消息块转成纯文本 Markdown，不引用任何媒体文件。"""
    kind = block.get("t") or ""
    content = block.get("c") or {}
    if kind == "m":
        return _text(block)
    if kind == "reply":
        text = _text(block)
        return "\n".join(f"> 引用：{line}" if i == 0 else f"> {line}"
                         for i, line in enumerate(text.splitlines() or [""]))
    labels = {
        "img": "[图片]",
        "marketface": "[表情]",
        "qqemoji": "[表情]",
        "voice": "[语音]",
        "video": "[视频]",
    }
    if kind in labels:
        return labels[kind]
    if kind == "file":
        name = str(content.get("name") or content.get("path") or "文件")
        size = _human_size(content.get("size"))
        return f"[文件：{name}{'，' + size if size else ''}]"
    if kind in ("uns", "err"):
        return _text(block)
    return f"[{kind or '未支持'}]"


def export_markdown(
    conv: dict,
    ts_from: int,
    ts_to: int,
    target: Path,
    sender: str = "",
    progress=None,
    ids: list[int] | None = None,
) -> Path:
    senders = {item["uin"]: item for item in store.list_senders(conv["key"])}
    total = _message_total(conv["key"], ts_from, ts_to, sender, ids)
    span = (
        f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_from))} ~ '
        f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_to))}'
    )
    lines: list[str] = [
        f"# {conv.get('name') or conv['key']}",
        "",
        f"- 区间：{span}",
        f"- 消息：{total} 条",
        f"- 导出：{time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    last_day = ""
    names: dict[str, str] = {}
    for index, message in enumerate(
        _message_source(conv["key"], ts_from, ts_to, sender, ids), 1
    ):
        payload = message["payload"]
        ts = message["ts"]
        day_key = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day_key != last_day:
            lines.extend([f"## {day_key}", ""])
            last_day = day_key
        stamp = time.strftime("%H:%M", time.localtime(ts))
        if _is_system(payload):
            text = " ".join(_text(block) for block in _block_list(payload)) or payload.get("t", "")
            lines.extend([f"> {stamp} {text}", ""])
        else:
            uin = message["sender"]
            info = senders.get(uin) or {}
            name = names.setdefault(uin, info.get("name") or uin)
            content = "\n\n".join(
                part for part in (_md_block(block) for block in _block_list(payload)) if part
            )
            lines.extend([f"**{name}** {stamp}", "", content or "[空消息]", ""])
        if progress:
            progress(index, total)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def _prefix_paths(value: Any, resolve) -> Any:
    """把库里相对媒体目录的路径还原成解析器期望的 output/ 路径，并复制文件。"""
    if isinstance(value, dict):
        return {key: _prefix_paths(item, resolve) for key, item in value.items()}
    if isinstance(value, list):
        return [_prefix_paths(item, resolve) for item in value]
    if isinstance(value, str):
        if value.startswith("lazy:"):
            return resolve(value) or ""
        head = value.split("/", 1)[0]
        if head in MEDIA_KINDS and value != PLACEHOLDER_AVATAR:
            return resolve(value) or ""
    return value


def _placeholder_avatar(work: Path) -> None:
    from PIL import Image

    target = work / "output" / PLACEHOLDER_AVATAR
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (225, 228, 232)).save(target, "JPEG")


def export_pdf(
    conv: dict,
    ts_from: int,
    ts_to: int,
    target: Path,
    sender: str = "",
    progress=None,
    log=None,
    ids: list[int] | None = None,
) -> Path:
    total = _message_total(conv["key"], ts_from, ts_to, sender, ids)
    work = Path(tempfile.mkdtemp(prefix="qqhe-pdf-", dir=DATA_DIR))
    media_dir = media_root(conv["key"])
    copied: set[str] = set()

    def copy_file(source: Path, rel: str) -> bool:
        if rel in copied:
            return True
        if not source.is_file():
            return False
        destination = work / "output" / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, destination)
        except OSError:
            return False
        copied.add(rel)
        return True

    def resolve(path: str) -> str:
        if path.startswith("lazy:"):
            match = next(iter(MEDIA_CACHE_DIR.glob(f"{path[5:]}.*")), None)
            if match is None:
                match = media_cache.ensure(
                    path[5:], media_cache.conversation_kind(conv["key"]), store.media_link(path[5:])
                )
            if match is None:
                return ""
            rel = f"images/{match.name}"
            return f"output/{rel}" if copy_file(match, rel) else ""
        return f"output/{path}" if copy_file(media_dir / path, path) else ""

    try:
        output = work / "output"
        (output / "senders").mkdir(parents=True, exist_ok=True)
        (work / "config").mkdir(parents=True, exist_ok=True)
        config_src = ROOT / "config" / "GeneratePDF_ReportLab_config.ini"
        config_text = config_src.read_text(encoding="utf-8")
        config_text = config_text.replace("emptyAvatarShow = false", "emptyAvatarShow = true")
        (work / "config" / config_src.name).write_text(config_text, encoding="utf-8")
        os.symlink(ROOT / "resources", work / "resources")
        _placeholder_avatar(work)

        senders_doc: dict[str, list[str]] = {}
        for item in store.list_senders(conv["key"]):
            uin = str(item["uin"])
            avatar = str(item.get("avatar") or "")
            source: Path | None = None
            if avatar:
                candidate = media_dir / avatar
                if candidate.is_file():
                    source = candidate
            if source is None:
                for suffix in (".jpg", ".png", ".webp"):
                    candidate = AVATAR_DIR / f"{uin}{suffix}"
                    if candidate.is_file():
                        source = candidate
                        break
            if source is None:
                source = avatars.ensure(uin)
            if source is not None:
                relative = f"senders/{uin}{source.suffix or '.jpg'}"
                destination = output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                path = f"output/{relative}"
            else:
                path = f"output/{PLACEHOLDER_AVATAR}"
            senders_doc[uin] = [item.get("name") or uin, path]
        (output / "senders" / "senders.json").write_text(
            json.dumps(senders_doc, ensure_ascii=False), encoding="utf-8"
        )

        with (output / "chatData.txt").open("w", encoding="utf-8") as handle:
            for index, message in enumerate(
                _message_source(conv["key"], ts_from, ts_to, sender, ids), 1
            ):
                payload = dict(message["payload"])
                payload["i"] = message["ts"]
                payload["s"] = message["sender"]
                payload = _prefix_paths(payload, resolve)
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                if progress:
                    progress(index, total)

        os.environ["QQHE_EMOJI_CACHE"] = str(DATA_DIR / "emoji_cache")
        cwd = os.getcwd()
        os.chdir(work)
        try:
            from src.generate.GeneratePDF_ReportLab import GenerateInit  # type: ignore[import-not-found]

            GenerateInit(log or (lambda _line: None)).run()
        finally:
            os.chdir(cwd)
        produced = output / "chatData.pdf"
        if not produced.is_file() or produced.stat().st_size == 0:
            raise RuntimeError("PDF 生成失败，详见导出日志")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(target))
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)
