"""运行时路径与应用配置。

数据目录（消息库、媒体、缓存）整体可重建，默认放系统临时目录，
用 QQHE_DATA_DIR 覆盖；配置与导出产物放在持久目录，不与缓存混在一起。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = ROOT / "resources"
LEGACY_DATA_DIR = ROOT / "data"


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


DEFAULT_DATA_DIR = Path(tempfile.gettempdir()) / "qqhe-history-export"
_CUSTOM_DATA_DIR = bool(os.environ.get("QQHE_DATA_DIR", "").strip())
DATA_DIR = _env_path("QQHE_DATA_DIR", DEFAULT_DATA_DIR)
CONFIG_DIR = _env_path("QQHE_CONFIG_DIR", Path.home() / ".config" / "qqhe")
LIBRARY_DB = DATA_DIR / "library.sqlite3"
CONFIG_FILE = CONFIG_DIR / "config.json"
MEDIA_DIR = DATA_DIR / "media"
AVATAR_DIR = DATA_DIR / "avatars"
JOBS_DIR = DATA_DIR / "jobs"
RAW_DIR = DATA_DIR / "raw"
MEDIA_CACHE_DIR = DATA_DIR / "media_cache"
CACHE_LIMIT_MB = _env_int("QQHE_CACHE_LIMIT_MB", 500)


def _migrate_legacy() -> None:
    """旧版本把配置和库放在项目 data/ 下：使用默认数据目录时搬一次。"""
    if _CUSTOM_DATA_DIR or LEGACY_DATA_DIR.resolve() == DATA_DIR.resolve():
        return
    legacy_config = LEGACY_DATA_DIR / "config.json"
    if not CONFIG_FILE.exists() and legacy_config.is_file():
        shutil.copy2(legacy_config, CONFIG_FILE)
    legacy_db = LEGACY_DATA_DIR / "library.sqlite3"
    if not LIBRARY_DB.exists() and legacy_db.is_file():
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{legacy_db}{suffix}")
            if not source.is_file():
                continue
            target = Path(f"{LIBRARY_DB}{suffix}")
            try:
                source.replace(target)
            except OSError:
                shutil.move(str(source), str(target))
    for name in ("media", "avatars", "media_cache"):
        target_root = DATA_DIR / name
        legacy_root = LEGACY_DATA_DIR / name
        if not legacy_root.is_dir():
            continue
        target_root.mkdir(parents=True, exist_ok=True)
        for child in legacy_root.iterdir():
            target = target_root / child.name
            if target.exists():
                continue
            try:
                child.replace(target)
            except OSError:
                if child.is_dir():
                    shutil.copytree(child, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, target)


def ensure_dirs() -> None:
    for path in (
        DATA_DIR,
        MEDIA_DIR,
        AVATAR_DIR,
        JOBS_DIR,
        RAW_DIR,
        MEDIA_CACHE_DIR,
        CONFIG_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
    _migrate_legacy()


@dataclass
class AppConfig:
    db_path: str = ""
    slow_db_path: str = ""
    key: str = ""
    self_qq: str = ""
    media_root: str = ""
    images_path: str = ""
    voice_path: str = ""
    video_path: str = ""
    need_images: bool = True
    need_voice: bool = False
    need_video: bool = False
    need_qq_emoji: bool = True
    qq_emoji_ver: str = "new"
    need_market_face: bool = True
    need_java_deser: bool = True
    need_avatars: bool = True
    lazy_images: bool = False
    adb_serial: str = ""
    adb_sqlite: str = ""
    export_dir: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def has_key(self) -> bool:
        return bool(self.key)

    @property
    def is_adb(self) -> bool:
        return self.db_path.startswith("adb:")

    @property
    def ready(self) -> bool:
        if not self.db_path or not self.key:
            return False
        if self.is_adb:
            return True
        return Path(self.db_path).is_file()

    def effective_images_path(self) -> str:
        return _first_existing(self.images_path, self._derive("chatpic/chatimg"))

    def effective_voice_path(self) -> str:
        return _first_existing(self.voice_path, self._derive(f"{self.self_qq}/ptt"))

    def effective_video_path(self) -> str:
        return _first_existing(self.video_path, self._derive("shortvideo"))

    def effective_export_dir(self) -> Path:
        raw = self.export_dir.strip()
        if raw:
            return Path(raw).expanduser()
        downloads = Path.home() / "Downloads"
        return downloads if downloads.is_dir() else Path.home()

    def effective_emoticon_path(self) -> str:
        return _first_existing(self._derive(".emotionsm"))

    def _derive(self, suffix: str) -> str:
        if not self.media_root:
            return ""
        return str(Path(self.media_root) / "Tencent" / "MobileQQ" / suffix)


def _first_existing(*paths: str) -> str:
    for raw in paths:
        if raw and Path(raw).is_dir():
            return raw
    return ""


def read_kc_file(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    return text.strip()


def load_config() -> AppConfig:
    ensure_dirs()
    if not CONFIG_FILE.is_file():
        return AppConfig()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    known = {f for f in AppConfig.__dataclass_fields__}
    return AppConfig(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: AppConfig) -> None:
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(CONFIG_FILE)


def config_from_request(payload: dict) -> AppConfig:
    """从前端表单构造配置，密钥可直接给 key，也可给 kcPath。"""
    cfg = load_config()
    db_path = str(payload.get("dbPath") or "").strip()
    slow_db_path = str(payload.get("dbstPath") or "").strip()
    kc_path = str(payload.get("kcPath") or "").strip()
    key = str(payload.get("key") or "").strip()
    media_root = str(payload.get("mediaRoot") or "").strip()

    if payload.get("selfQQ"):
        cfg.self_qq = str(payload["selfQQ"]).strip()
    if db_path:
        cfg.db_path = db_path
    if slow_db_path:
        cfg.slow_db_path = slow_db_path
    if kc_path and not kc_path.startswith("adb:"):
        cfg.key = read_kc_file(kc_path)
    elif key:
        cfg.key = key
    if media_root:
        cfg.media_root = media_root
    if payload.get("adbSerial"):
        cfg.adb_serial = str(payload["adbSerial"]).strip()
    if payload.get("adbSqlite"):
        cfg.adb_sqlite = str(payload["adbSqlite"]).strip()
    if payload.get("imagesPath"):
        cfg.images_path = str(payload["imagesPath"]).strip()
    if payload.get("voicePath"):
        cfg.voice_path = str(payload["voicePath"]).strip()
    if payload.get("videoPath"):
        cfg.video_path = str(payload["videoPath"]).strip()

    field_map = {
        "needImages": "need_images",
        "needVoice": "need_voice",
        "needVideo": "need_video",
        "needQQEmoji": "need_qq_emoji",
        "needMarketFace": "need_market_face",
        "needJavaDeser": "need_java_deser",
        "needAvatars": "need_avatars",
    }
    for key, field in field_map.items():
        if key in payload:
            setattr(cfg, field, bool(payload[key]))
    if payload.get("emojiVer"):
        cfg.qq_emoji_ver = str(payload["emojiVer"])

    if cfg.db_path and not cfg.self_qq:
        name = cfg.db_path.rstrip("/").rsplit("/", 1)[-1]
        cfg.self_qq = name[:-3] if name.endswith(".db") else name
    return cfg


def public_config(cfg: AppConfig) -> dict:
    """给前端的配置视图，密钥只暴露是否已设置。"""
    return {
        "dbPath": cfg.db_path,
        "dbstPath": cfg.slow_db_path,
        "mediaRoot": cfg.media_root,
        "imagesPath": cfg.images_path,
        "voicePath": cfg.voice_path,
        "videoPath": cfg.video_path,
        "selfQQ": cfg.self_qq,
        "hasKey": cfg.has_key,
        "ready": cfg.ready,
        "needImages": cfg.need_images,
        "needVoice": cfg.need_voice,
        "needVideo": cfg.need_video,
        "needQQEmoji": cfg.need_qq_emoji,
        "qqEmojiVer": cfg.qq_emoji_ver,
        "needMarketFace": cfg.need_market_face,
        "needJavaDeser": cfg.need_java_deser,
        "needAvatars": cfg.need_avatars,
        "isAdb": cfg.is_adb,
        "adbSerial": cfg.adb_serial,
        "adbSqlite": cfg.adb_sqlite,
        "dataDir": str(DATA_DIR),
        "exportDir": str(cfg.effective_export_dir()),
        "cacheLimitMB": CACHE_LIMIT_MB,
    }


def parser_configs(
    cfg: AppConfig,
    target_qq: str,
    mode: str,
    ts_from: int | None = None,
    ts_to: int | None = None,
    emoji_config: str = "",
) -> dict:
    """构造现有解析器需要的 configs（键名与 src/dataParsing 保持一致）。"""
    configs = {
        "mode": mode,
        "selfQQ": cfg.self_qq,
        "targetQQ": target_qq,
        "needSlowtable": bool(cfg.slow_db_path and Path(cfg.slow_db_path).is_file()),
        "dbPath": cfg.db_path,
        "dbstPath": cfg.slow_db_path,
        "key": cfg.key,
        "needQQEmoji": cfg.need_qq_emoji,
        "QQEmojiVer": cfg.qq_emoji_ver,
        "emojiConfigPath": emoji_config,
        "needImages": cfg.need_images,
        "imagesPath": cfg.effective_images_path(),
        "needVoice": cfg.need_voice,
        "voicePath": cfg.effective_voice_path(),
        "needVideo": cfg.need_video,
        "videoPath": cfg.effective_video_path(),
        "emoticonPath": cfg.effective_emoticon_path(),
        "needMarketFace": cfg.need_market_face,
        "needJavaDeser": cfg.need_java_deser,
        "lazyImages": cfg.lazy_images,
    }
    if ts_from is not None and ts_to is not None:
        configs["timeFrom"] = int(ts_from)
        configs["timeTo"] = int(ts_to)
    return configs
