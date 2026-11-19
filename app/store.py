"""ChatDocument：按时间窗加载的会话、消息、发送者与覆盖区间。"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta

from .settings import LIBRARY_DB, ensure_dirs

SCHEMA = """
create table if not exists conversation(
  key text primary key,
  kind text not null,
  uin text not null,
  name text not null,
  msg_count integer not null default 0,
  min_ts integer not null default 0,
  max_ts integer not null default 0,
  imported_at integer not null default 0
);
create table if not exists message(
  id integer primary key autoincrement,
  conv_key text not null,
  ts integer not null,
  sender text not null,
  type text not null,
  payload text not null
);
create index if not exists idx_msg_conv_ts on message(conv_key, ts, id);
create index if not exists idx_msg_conv_sender_ts on message(conv_key, sender, ts, id);
create table if not exists sender(
  conv_key text not null,
  uin text not null,
  name text not null default '',
  avatar text not null default '',
  msg_count integer not null default 0,
  primary key(conv_key, uin)
);
create table if not exists window(
  conv_key text not null,
  ts_from integer not null,
  ts_to integer not null,
  primary key(conv_key, ts_from, ts_to)
);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(LIBRARY_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    return conn


def _day_bounds(ts_from: int, ts_to: int) -> tuple[int, int]:
    start = datetime.fromtimestamp(ts_from).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(ts_to).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((end + timedelta(days=1)).timestamp()) - 1


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 旧库没有覆盖区间：整会话导入过的数据视为已覆盖（按天取整）
        rows = conn.execute(
            "select c.key, c.min_ts, c.max_ts from conversation c "
            "where c.msg_count > 0 and not exists "
            "(select 1 from window w where w.conv_key = c.key)"
        ).fetchall()
        conn.executemany(
            "insert or ignore into window(conv_key, ts_from, ts_to) values(?,?,?)",
            ((row["key"], *_day_bounds(int(row["min_ts"]), int(row["max_ts"]))) for row in rows),
        )
        # 把已有区间也按天取整，避免日期选择器的日边界总是落在覆盖范围之外
        by_conv: dict[str, list[tuple[int, int]]] = {}
        for row in conn.execute("select conv_key, ts_from, ts_to from window"):
            by_conv.setdefault(row["conv_key"], []).append(
                _day_bounds(int(row["ts_from"]), int(row["ts_to"]))
            )
        if by_conv:
            conn.execute("delete from window")
            for key, ranges in by_conv.items():
                conn.executemany(
                    "insert into window(conv_key, ts_from, ts_to) values(?,?,?)",
                    ((key, start, end) for start, end in _merge_windows(ranges)),
                )


def _merge_windows(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def replace_range(
    conv: dict,
    messages,
    senders: list[tuple[str, str, str]],
    ts_from: int,
    ts_to: int,
    on_progress=None,
) -> dict:
    """重写 [ts_from, ts_to] 区间的消息，并把该区间并入已覆盖范围。"""
    conn = connect()
    inserted = 0
    batch: list[tuple] = []
    try:
        conn.execute("begin immediate")
        names: dict[str, str] = {}
        avatars: dict[str, str] = {}
        for row in conn.execute(
            "select uin, name, avatar from sender where conv_key=?", (conv["key"],)
        ):
            names[row["uin"]] = row["name"]
            avatars[row["uin"]] = row["avatar"]
        for uin, name, avatar in senders:
            if name:
                names[uin] = name
            if avatar:
                avatars[uin] = avatar
        conn.execute(
            "delete from message where conv_key=? and ts between ? and ?",
            (conv["key"], ts_from, ts_to),
        )
        for ts, sender, mtype, payload in messages:
            batch.append((conv["key"], ts, sender, mtype, payload))
            inserted += 1
            if len(batch) >= 2000:
                conn.executemany(
                    "insert into message(conv_key, ts, sender, type, payload) values(?,?,?,?,?)",
                    batch,
                )
                batch.clear()
                if on_progress:
                    on_progress(inserted)
        if batch:
            conn.executemany(
                "insert into message(conv_key, ts, sender, type, payload) values(?,?,?,?,?)",
                batch,
            )
        sender_counts = {
            row["sender"]: int(row["c"])
            for row in conn.execute(
                "select sender, count(*) as c from message where conv_key=? group by sender",
                (conv["key"],),
            )
        }
        conn.execute("delete from sender where conv_key=?", (conv["key"],))
        conn.executemany(
            "insert into sender(conv_key, uin, name, avatar, msg_count) values(?,?,?,?,?)",
            (
                (conv["key"], uin, names.get(uin, ""), avatars.get(uin, ""), count)
                for uin, count in sender_counts.items()
            ),
        )
        stats = conn.execute(
            "select count(*) as c, min(ts) as lo, max(ts) as hi from message where conv_key=?",
            (conv["key"],),
        ).fetchone()
        total = int(stats["c"])
        min_ts = int(stats["lo"] or 0)
        max_ts = int(stats["hi"] or 0)
        conn.execute(
            "insert into conversation(key, kind, uin, name, msg_count, min_ts, max_ts, imported_at) "
            "values(?,?,?,?,?,?,?,?) "
            "on conflict(key) do update set kind=excluded.kind, uin=excluded.uin, name=excluded.name, "
            "msg_count=excluded.msg_count, min_ts=excluded.min_ts, max_ts=excluded.max_ts, "
            "imported_at=excluded.imported_at",
            (
                conv["key"],
                conv["kind"],
                conv["uin"],
                conv["name"],
                total,
                min_ts,
                max_ts,
                int(time.time()),
            ),
        )
        windows = [
            (int(row["ts_from"]), int(row["ts_to"]))
            for row in conn.execute(
                "select ts_from, ts_to from window where conv_key=?", (conv["key"],)
            )
        ]
        windows.append(_day_bounds(ts_from, ts_to))
        conn.execute("delete from window where conv_key=?", (conv["key"],))
        conn.executemany(
            "insert into window(conv_key, ts_from, ts_to) values(?,?,?)",
            ((conv["key"], start, end) for start, end in _merge_windows(windows)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "count": inserted,
        "total": total,
        "min_ts": min_ts,
        "max_ts": max_ts,
    }


def range_covered(conv_key: str, ts_from: int, ts_to: int) -> bool:
    with connect() as conn:
        rows = conn.execute(
            "select ts_from, ts_to from window where conv_key=?", (conv_key,)
        ).fetchall()
    return any(int(row["ts_from"]) <= ts_from and int(row["ts_to"]) >= ts_to for row in rows)


def list_conversations() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select key, kind, uin, name, msg_count, min_ts, max_ts, imported_at "
            "from conversation order by max_ts desc"
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from conversation where key=?", (key,)).fetchone()
    return dict(row) if row else None


def _where(conv_key: str, ts_from: int, ts_to: int, sender: str) -> tuple[str, list]:
    sql = "from message where conv_key=? and ts between ? and ?"
    args: list = [conv_key, ts_from, ts_to]
    if sender:
        sql += " and sender=?"
        args.append(sender)
    return sql, args


def count_messages(conv_key: str, ts_from: int, ts_to: int, sender: str = "") -> int:
    where, args = _where(conv_key, ts_from, ts_to, sender)
    with connect() as conn:
        return int(conn.execute("select count(*) " + where, args).fetchone()[0])


def message_days(
    conv_key: str, ts_from: int, ts_to: int, sender: str = ""
) -> list[dict]:
    where, args = _where(conv_key, ts_from, ts_to, sender)
    sql = (
        "select date(ts, 'unixepoch', 'localtime') as day, count(*) as c "
        + where
        + " group by day order by day"
    )
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"date": r["day"], "count": int(r["c"])} for r in rows]


def fetch_messages(
    conv_key: str,
    ts_from: int,
    ts_to: int,
    sender: str = "",
    limit: int = 100,
    before_ts: int = 0,
    before_id: int = 0,
) -> tuple[list[dict], bool]:
    """取窗口内最新的一批；before_ts/before_id 用于向上翻更早。"""
    where, args = _where(conv_key, ts_from, ts_to, sender)
    if before_ts:
        where += " and (ts < ? or (ts = ? and id < ?))"
        args.extend([before_ts, before_ts, before_id])
    sql = (
        "select id, ts, sender, type, payload "
        + where
        + " order by ts desc, id desc limit ?"
    )
    args.append(limit + 1)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    messages = [
        {
            "id": r["id"],
            "ts": r["ts"],
            "sender": r["sender"],
            "type": r["type"],
            "payload": json.loads(r["payload"]),
        }
        for r in rows
    ]
    return messages, has_more


def iter_messages(
    conv_key: str, ts_from: int, ts_to: int, sender: str = ""
):
    """按时间升序流式遍历区间内的消息（导出用）。"""
    where, args = _where(conv_key, ts_from, ts_to, sender)
    sql = "select id, ts, sender, type, payload " + where + " order by ts asc, id asc"
    conn = connect()
    try:
        for row in conn.execute(sql, args):
            yield {
                "id": row["id"],
                "ts": int(row["ts"]),
                "sender": row["sender"],
                "type": row["type"],
                "payload": json.loads(row["payload"]),
            }
    finally:
        conn.close()


def list_senders(conv_key: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select uin, name, avatar, msg_count from sender "
            "where conv_key=? order by msg_count desc",
            (conv_key,),
        ).fetchall()
    return [dict(r) for r in rows]
