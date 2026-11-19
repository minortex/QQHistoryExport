"""通过 adb 直接从手机读取 QQ 数据库。

- 会话发现：在设备上用 sqlite3 只读查询，不拷贝数据库。
- 导入：只读 ATTACH 在线数据库，把「单个会话表 + 元数据表」导出到设备 /tmp（tmpfs），
  拉回本机系统临时目录后使用，结束即删除；不在任何一方留下持久副本。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

from .crypto import xor_bytes
from .discover import TABLE_RE, ConversationInfo
from .settings import AppConfig

DEFAULT_DEVICE_SQLITE = "/data/data/com.termux/files/usr/bin/sqlite3"
DEVICE_TMP_ROOT = "/tmp/qqhistory"
DEFAULT_CHATIMG = (
    "/data/media/0/Android/data/com.tencent.mobileqq/Tencent/MobileQQ/chatpic/chatimg"
)
META_TABLES = ("Friends", "TroopInfoV2", "TroopMemberInfo")


class AdbError(RuntimeError):
    pass


def is_adb_path(path: str) -> bool:
    return path.startswith("adb:")


def device_path(path: str) -> str:
    if path.startswith("adb://"):
        return path[len("adb://") :]
    if path.startswith("adb:"):
        return path[len("adb:") :]
    return path


def _adb_bin() -> str:
    return shutil.which("adb") or "adb"


def _base(cfg: AppConfig) -> list[str]:
    base = [_adb_bin()]
    if cfg.adb_serial:
        return base + ["-s", cfg.adb_serial]
    try:
        out = subprocess.run(
            base + ["devices", "-l"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdbError(f"无法执行 adb：{exc}") from exc
    for line in out.splitlines()[1:]:
        match = re.search(r"transport_id:(\d+)", line)
        if match:
            return base + ["-t", match.group(1)]
    raise AdbError("没有已连接的 adb 设备")


def _run(cfg: AppConfig, args: list[str], timeout: float = 300, stdin: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            _base(cfg) + args, input=stdin, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdbError(f"adb 调用失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise AdbError(detail or f"adb 退出码 {proc.returncode}")
    return proc.stdout


def shell(cfg: AppConfig, command: str, timeout: float = 300) -> str:
    return _run(cfg, ["shell", command], timeout=timeout).decode("utf-8", "replace").strip()


def pull(cfg: AppConfig, remote: str, local: Path, timeout: float = 1800) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    _run(cfg, ["pull", remote, str(local)], timeout=timeout)


def push(cfg: AppConfig, local: Path, remote: str, timeout: float = 120) -> None:
    _run(cfg, ["push", str(local), remote], timeout=timeout)


def read_device_file(cfg: AppConfig, path: str) -> str:
    return _run(cfg, ["exec-out", f"cat {shlex.quote(path)}"], timeout=60).decode(
        "utf-8", "replace"
    )


def _sqlite(cfg: AppConfig) -> str:
    return cfg.adb_sqlite or DEFAULT_DEVICE_SQLITE


def _sqlite_json(cfg: AppConfig, db: str, sql: str, timeout: float = 300) -> list[dict]:
    command = f"{shlex.quote(_sqlite(cfg))} -readonly -json {shlex.quote(db)}"
    out = _run(cfg, ["shell", command], timeout=timeout, stdin=sql.encode())
    text = out.decode("utf-8", "replace").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdbError(f"sqlite JSON 输出解析失败：{text[:200]}") from exc


def _decode_cell(kind: str, hex_value: str | None, key: str) -> str:
    if not hex_value:
        return ""
    raw = bytes.fromhex(hex_value)
    if kind == "blob":
        return xor_bytes(raw, key).decode("utf-8", "replace")
    try:
        cipher = raw.decode("utf-8")
    except UnicodeDecodeError:
        return xor_bytes(raw, key).decode("utf-8", "replace")
    return "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(cipher))


def check(cfg: AppConfig) -> None:
    """连接与依赖检查；失败抛 AdbError。"""
    shell(cfg, "true", timeout=30)
    version = shell(cfg, f"{shlex.quote(_sqlite(cfg))} -version", timeout=30)
    if not re.match(r"^\d+\.\d+", version):
        raise AdbError(f"设备上找不到可用的 sqlite3：{version[:100]}")
    for label, path in (("主库", cfg.db_path), ("slowtable", cfg.slow_db_path)):
        if not path or not is_adb_path(path):
            continue
        remote = device_path(path)
        if shell(cfg, f"test -f {shlex.quote(remote)} && echo yes", timeout=30) != "yes":
            raise AdbError(f"{label}不存在：{remote}")


def _tables(cfg: AppConfig, db: str) -> set[str]:
    rows = _sqlite_json(
        cfg,
        db,
        "select name from sqlite_master where type='table' "
        "and (name like 'mr_troop_%_New' or name like 'mr_friend_%_New');",
    )
    return {row["name"] for row in rows}


def _counts(cfg: AppConfig, db: str, tables: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for start in range(0, len(tables), 300):
        chunk = tables[start : start + 300]
        parts = [
            f"select '{name}' as t,count(*) as c from \"{name}\"" for name in chunk
        ]
        rows = _sqlite_json(cfg, db, " union all ".join(parts) + ";", timeout=600)
        for row in rows:
            result[row["t"]] = int(row["c"])
    return result


def _names(cfg: AppConfig, db: str) -> dict[str, tuple[str, str, str]]:
    names: dict[str, tuple[str, str, str]] = {}
    queries = (
        (
            "troop",
            "select typeof(troopuin) as tu,hex(troopuin) as u,"
            "typeof(troopname) as tn,hex(troopname) as n from TroopInfoV2;",
        ),
        (
            "friend",
            "select typeof(uin) as tu,hex(uin) as u,"
            "typeof(name) as tn,hex(name) as n,"
            "typeof(remark) as tr,hex(remark) as r from Friends;",
        ),
    )
    for kind, sql in queries:
        for row in _sqlite_json(cfg, db, sql):
            uin = _decode_cell(row["tu"], row["u"], cfg.key)
            if not uin:
                continue
            name = _decode_cell(row["tn"], row["n"], cfg.key)
            if kind == "friend" and row.get("r"):
                remark = _decode_cell(row["tr"], row["r"], cfg.key)
                name = remark or name
            names[hashlib.md5(uin.encode()).hexdigest().upper()] = (kind, uin, name)
    return names


def discover(cfg: AppConfig) -> list[ConversationInfo]:
    main = device_path(cfg.db_path)
    slow = device_path(cfg.slow_db_path) if cfg.slow_db_path else ""

    main_tables = _tables(cfg, main)
    slow_tables = _tables(cfg, slow) if slow else set()
    names = _names(cfg, main)
    main_counts = _counts(cfg, main, sorted(main_tables)) if main_tables else {}
    slow_counts = (
        _counts(cfg, slow, sorted(slow_tables)) if slow and slow_tables else {}
    )

    result: list[ConversationInfo] = []
    for table in sorted(main_tables | slow_tables):
        match = TABLE_RE.match(table)
        if not match:
            continue
        kind = "troop" if match.group(1) == "troop" else "friend"
        digest = match.group(2)
        info = names.get(digest)
        resolved = info is not None
        if info:
            kind, uin, name = info
        else:
            uin, name = digest, f"未识别会话 {digest[:8]}"
        count_main = main_counts.get(table, 0)
        count_slow = slow_counts.get(table, 0)
        if count_main + count_slow <= 0:
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
                total=count_main + count_slow,
                resolved=resolved,
            )
        )
    result.sort(key=lambda item: (-item.total, item.name))
    return result


def _ensure_dummy_friend(db: Path, key: str, table: str, emit) -> None:
    """解析器在群模式下要求 Friends 里至少有一张聊天表存在，补一张空表。"""
    import sqlite3

    from .crypto import decode

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("select uin from Friends limit 1").fetchone()
        if not row:
            return
        uin = decode(row[0], key)
        if not uin:
            return
        digest = hashlib.md5(uin.encode()).hexdigest().upper()
        name = f"mr_friend_{digest}_New"
        exists = conn.execute(
            "select 1 from sqlite_master where type='table' and name=?", (name,)
        ).fetchone()
        if exists:
            return
        conn.execute(f'create table "{name}" as select * from "{table}" limit 0')
        conn.commit()
        emit({"stage": "prepare", "message": "补充好友表占位（解析器前置条件）"})
    finally:
        conn.close()


def materialize(
    cfg: AppConfig,
    conv: dict,
    emit,
    ts_from: int,
    ts_to: int,
) -> tuple[AppConfig, Path | None]:
    """在设备端读取在线数据库，只导出指定时间窗内的会话消息。

    临时文件只写设备 /tmp（tmpfs）和本机系统临时目录，结束后删除。
    """
    import tempfile

    main = device_path(cfg.db_path)
    slow = device_path(cfg.slow_db_path) if cfg.slow_db_path else ""
    suffix = "troop" if conv["kind"] == "troop" else "friend"
    digest = hashlib.md5(conv["uin"].encode()).hexdigest().upper()
    table = f"mr_{suffix}_{digest}_New"

    emit({"stage": "prepare", "progress": 3, "message": "检查设备上的会话表"})
    main_has = bool(
        _sqlite_json(
            cfg,
            main,
            f"select count(*) as c from sqlite_master where name='{table}';",
        )[0]["c"]
    )
    slow_has = False
    if slow:
        slow_has = bool(
            _sqlite_json(
                cfg,
                slow,
                f"select count(*) as c from sqlite_master where name='{table}';",
            )[0]["c"]
        )
    if not main_has and not slow_has:
        raise AdbError(f"设备上找不到会话表 {table}")

    local_dir = Path(tempfile.mkdtemp(prefix="qqhistory-adb-"))
    tmp = f"{DEVICE_TMP_ROOT}/{uuid.uuid4().hex[:8]}"
    sqlite = _sqlite(cfg)
    remote_main = f"{tmp}/main.db"
    remote_slow = f"{tmp}/slow.db"
    local_main = local_dir / "main.db"
    local_slow = local_dir / "slow.db"

    meta_tables = {
        row["name"]
        for row in _sqlite_json(
            cfg,
            main,
            "select name from sqlite_master where type='table' and name in "
            f"({','.join(repr(n) for n in META_TABLES)});",
        )
    }

    def compact_sql(source: str, statements: list[str]) -> str:
        body = "; ".join(statements)
        return (
            f"ATTACH 'file:{source}?mode=ro' AS src; BEGIN; {body}; COMMIT;"
        )

    meta_statements = [
        f"CREATE TABLE {name} AS SELECT * FROM src.{name}" for name in sorted(meta_tables)
    ]
    window = f' WHERE time >= {int(ts_from)} AND time <= {int(ts_to)}'
    main_statements = list(meta_statements)
    if main_has:
        main_statements.append(
            f'CREATE TABLE "{table}" AS SELECT * FROM src."{table}"{window}'
        )
    slow_statements = (
        [f'CREATE TABLE "{table}" AS SELECT * FROM src."{table}"{window}']
        if slow_has
        else []
    )

    lines = ["set -e", f"mkdir -p '{tmp}'"]
    lines.append(
        f'"{sqlite}" "{remote_main}" "{compact_sql(main, main_statements)}"'
    )
    if slow and slow_statements:
        lines.append(
            f'"{sqlite}" "{remote_slow}" "{compact_sql(slow, slow_statements)}"'
        )

    script_local = local_dir / "prepare.sh"
    script_local.write_text("\n".join(lines) + "\n", encoding="utf-8")
    remote_script = f"{tmp}/prepare.sh"
    try:
        push(cfg, script_local, remote_script)
        emit({"stage": "prepare", "progress": 20, "message": "在设备 /tmp 导出会话表"})
        shell(cfg, f"sh '{remote_script}'", timeout=1800)
        emit({"stage": "prepare", "progress": 28, "message": "拉取紧凑数据库"})
        pull(cfg, remote_main, local_main)
        if slow and slow_statements:
            pull(cfg, remote_slow, local_slow)
    finally:
        try:
            shell(cfg, f"rm -f '{tmp}'/* && rmdir '{tmp}'", timeout=60)
        except AdbError:
            pass

    if conv["kind"] == "troop" and local_main.is_file():
        _ensure_dummy_friend(local_main, cfg.key, table, emit)

    emit({"stage": "prepare", "progress": 35, "message": "数据准备完成"})
    materialized = replace(
        cfg,
        db_path=str(local_main),
        slow_db_path=str(local_slow) if local_slow.is_file() else "",
        need_images=cfg.need_images,
        lazy_images=cfg.need_images,
        images_path="",
        need_voice=False,
        need_video=False,
    )
    return materialized, local_dir
