from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from .paths import get_data_dir as _get_data_dir

try:
    import aiosqlite  # type: ignore
except Exception:  # pragma: no cover - runtime fallback when dependency is unavailable
    aiosqlite = None


DB_FILENAME = "personification.db"

_db_path: Path | None = None
_db_lock = asyncio.Lock()
_db: Any = None


DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS kv_store (
        namespace TEXT NOT NULL,
        key       TEXT NOT NULL,
        value     TEXT NOT NULL,
        updated_at REAL NOT NULL DEFAULT (unixepoch('now')),
        PRIMARY KEY (namespace, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT    NOT NULL,
        role       TEXT    NOT NULL,
        content    TEXT    NOT NULL,
        is_summary INTEGER NOT NULL DEFAULT 0,
        timestamp  REAL    NOT NULL,
        metadata   TEXT    NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_messages_session
        ON session_messages(session_id, timestamp)
    """,
    """
    CREATE TABLE IF NOT EXISTS group_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    TEXT    NOT NULL,
        user_id     TEXT    NOT NULL DEFAULT '',
        nickname    TEXT    NOT NULL DEFAULT '',
        content     TEXT    NOT NULL,
        image_count INTEGER NOT NULL DEFAULT 0,
        visual_summary TEXT NOT NULL DEFAULT '',
        is_bot      INTEGER NOT NULL DEFAULT 0,
        reply_to_msg_id  TEXT DEFAULT NULL,
        reply_to_user_id TEXT DEFAULT NULL,
        mentioned_ids    TEXT NOT NULL DEFAULT '[]',
        is_at_bot        INTEGER NOT NULL DEFAULT 0,
        message_id       TEXT DEFAULT NULL,
        source_kind      TEXT NOT NULL DEFAULT 'user',
        timestamp   REAL    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_group_messages_group
        ON group_messages(group_id, timestamp)
    """,
    """
    CREATE TABLE IF NOT EXISTS group_relation_edges (
        group_id      TEXT NOT NULL,
        src_user_id   TEXT NOT NULL,
        dst_user_id   TEXT NOT NULL,
        edge_kind     TEXT NOT NULL,
        weight        REAL NOT NULL DEFAULT 0,
        last_seen_at  REAL NOT NULL DEFAULT 0,
        sample_msg_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (group_id, src_user_id, dst_user_id, edge_kind)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_group_relation_edges_group
        ON group_relation_edges(group_id, last_seen_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS user_personas (
        user_id    TEXT    PRIMARY KEY,
        persona    TEXT    NOT NULL DEFAULT '',
        updated_at REAL    NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_histories (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_persona_histories_user
        ON persona_histories(user_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS user_tasks (
        task_id    TEXT    NOT NULL,
        user_id    TEXT    NOT NULL,
        description TEXT   NOT NULL DEFAULT '',
        cron       TEXT    NOT NULL,
        action     TEXT    NOT NULL DEFAULT '',
        params     TEXT    NOT NULL DEFAULT '{}',
        active     INTEGER NOT NULL DEFAULT 1,
        created_at REAL    NOT NULL,
        last_executed_at REAL DEFAULT NULL,
        last_status TEXT NOT NULL DEFAULT 'pending',
        PRIMARY KEY (user_id, task_id)
    )
    """,
)


def resolve_db_path(plugin_config: Any | None = None) -> Path:
    return Path(_get_data_dir(plugin_config)) / DB_FILENAME


def get_db_path() -> Path:
    global _db_path
    if _db_path is None:
        _db_path = resolve_db_path(None)
    return _db_path


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_sync(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or get_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    return _configure_connection(conn)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    columns: set[str] = set()
    for row in rows:
        name = row["name"] if hasattr(row, "__getitem__") else row[1]
        columns.add(str(name))
    return columns


def _ensure_group_message_schema(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "group_messages")
    if "image_count" not in columns:
        conn.execute("ALTER TABLE group_messages ADD COLUMN image_count INTEGER NOT NULL DEFAULT 0")
    if "visual_summary" not in columns:
        conn.execute("ALTER TABLE group_messages ADD COLUMN visual_summary TEXT NOT NULL DEFAULT ''")


def init_db_sync(data_dir: str | Path) -> Path:
    global _db_path
    _db_path = Path(data_dir) / DB_FILENAME
    with connect_sync(_db_path) as conn:
        for ddl in DDL_STATEMENTS:
            conn.execute(ddl)
        _ensure_group_message_schema(conn)
        conn.commit()
    return _db_path


async def init_db(data_dir: str | Path) -> Path:
    path = await asyncio.to_thread(init_db_sync, data_dir)
    if aiosqlite is not None:
        await get_db()
    return path


class _AsyncConnectionWrapper:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._op_lock = asyncio.Lock()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        async with self._op_lock:
            return await asyncio.to_thread(self._conn.execute, sql, params)

    async def commit(self) -> None:
        async with self._op_lock:
            await asyncio.to_thread(self._conn.commit)

    async def close(self) -> None:
        async with self._op_lock:
            await asyncio.to_thread(self._conn.close)


async def get_db() -> Any:
    global _db
    async with _db_lock:
        if _db is not None:
            return _db
        path = get_db_path()
        if aiosqlite is not None:
            conn = await aiosqlite.connect(path)
            conn.row_factory = sqlite3.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            _db = conn
            return _db
        _db = _AsyncConnectionWrapper(connect_sync(path))
        return _db


async def close_db() -> None:
    global _db
    async with _db_lock:
        if _db is None:
            return
        await _db.close()
        _db = None
