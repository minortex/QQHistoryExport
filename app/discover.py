"""扫描 QQ 主库与 slowtable，列出可导出的会话。

只做只读查询，不修改原始数据库。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .crypto import decode
from .settings import AppConfig

TABLE_RE = re.compile(r"^mr_(troop|friend)_([0-9A-F]{32})_New$")


@dataclass
class ConversationInfo:
    key: str
    kind: str
    uin: str
    name: str
    remark: str
    count_main: int
    count_slow: int
    total: int
    resolved: bool = True

    def to_json(self) -> dict:
        return asdict(self)


def _connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "select name from sqlite_master where type='table' "
        "and (name like 'mr_troop_%_New' or name like 'mr_friend_%_New')"
    ).fetchall()
    return [r[0] for r in rows]


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f'select count(*) from "{table}"').fetchone()[0])
    except sqlite3.DatabaseError:
        return 0


def _decode_rows(conn: sqlite3.Connection, sql: str, key: str) -> list[tuple[str, ...]]:
    rows = conn.execute(sql).fetchall()
    return [tuple(decode(v, key) for v in row) for row in rows]


def _load_names(conn: sqlite3.Connection, key: str) -> dict[str, tuple[str, str, str]]:
    """md5(uin) -> (kind, uin, 显示名)。"""
    names: dict[str, tuple[str, str, str]] = {}
    for uin, name, remark in _decode_rows(conn, "select troopuin, troopname, '' from TroopInfoV2", key):
        if uin:
            names[hashlib.md5(uin.encode()).hexdigest().upper()] = ("troop", uin, name)
    for uin, name, remark in _decode_rows(conn, "select uin, name, remark from Friends", key):
        if uin:
            display = remark or name or uin
            names[hashlib.md5(uin.encode()).hexdigest().upper()] = ("friend", uin, display)
    return names


def discover(cfg: AppConfig) -> list[ConversationInfo]:
    if not cfg.ready:
        return []

    main = _connect(cfg.db_path)
    slow = None
    if cfg.slow_db_path and Path(cfg.slow_db_path).is_file():
        slow = _connect(cfg.slow_db_path)
    try:
        names = _load_names(main, cfg.key)
        main_tables = set(_table_names(main))
        slow_tables = set(_table_names(slow)) if slow else set()

        result: list[ConversationInfo] = []
        for table in sorted(main_tables | slow_tables):
            m = TABLE_RE.match(table)
            if not m:
                continue
            kind = "troop" if m.group(1) == "troop" else "friend"
            digest = m.group(2)
            info = names.get(digest)
            resolved = info is not None
            if info:
                kind, uin, name = info
            else:
                uin, name = digest, f"未识别会话 {digest[:8]}"
            count_main = _count(main, table) if table in main_tables else 0
            count_slow = _count(slow, table) if slow and table in slow_tables else 0
            total = count_main + count_slow
            if total <= 0:
                continue
            result.append(
                ConversationInfo(
                    key=f"{kind}:{uin}",
                    kind=kind,
                    uin=uin,
                    name=name or uin,
                    remark="",
                    count_main=count_main,
                    count_slow=count_slow,
                    total=total,
                    resolved=resolved,
                )
            )
        result.sort(key=lambda c: (-c.total, c.name))
        return result
    finally:
        main.close()
        if slow:
            slow.close()
