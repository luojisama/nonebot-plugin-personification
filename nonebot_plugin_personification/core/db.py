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
        thread_id        TEXT NOT NULL DEFAULT '',
        source_kind      TEXT NOT NULL DEFAULT 'user',
        sender_role      TEXT NOT NULL DEFAULT '',
        timestamp   REAL    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_group_messages_group
        ON group_messages(group_id, timestamp)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_group_messages_thread
        ON group_messages(group_id, thread_id, timestamp)
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_threads (
        thread_id      TEXT PRIMARY KEY,
        group_id       TEXT NOT NULL,
        topic_summary  TEXT NOT NULL DEFAULT '',
        participants   TEXT NOT NULL DEFAULT '[]',
        created_at     REAL NOT NULL,
        last_active_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conversation_threads_group
        ON conversation_threads(group_id, last_active_at DESC)
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
    """
    CREATE TABLE IF NOT EXISTS token_usage_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket_day TEXT NOT NULL,
        group_id TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        purpose TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        call_count INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_bucket
        ON token_usage_ledger(bucket_day, group_id, user_id, model, purpose)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_token_usage_day ON token_usage_ledger(bucket_day)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_token_usage_group ON token_usage_ledger(group_id, bucket_day)
    """,
    """
    CREATE TABLE IF NOT EXISTS group_style_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        style_text TEXT NOT NULL DEFAULT '',
        style_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_group_style_gid_ts
        ON group_style_snapshots(group_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS webui_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        action TEXT NOT NULL,
        qq TEXT NOT NULL DEFAULT '',
        device_id TEXT NOT NULL DEFAULT '',
        target TEXT NOT NULL DEFAULT '',
        ip_hash TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '{}',
        outcome TEXT NOT NULL DEFAULT 'ok'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_webui_audit_ts ON webui_audit_log(ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_webui_audit_action ON webui_audit_log(action, ts DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_runtime_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        level TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        context TEXT NOT NULL DEFAULT '{}',
        trace_id TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plugin_runtime_logs_ts
        ON plugin_runtime_logs(ts DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plugin_runtime_logs_level
        ON plugin_runtime_logs(level, ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plugin_runtime_logs_trace
        ON plugin_runtime_logs(trace_id, ts DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS reply_turn_traces (
        trace_id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        session_type TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        stages TEXT NOT NULL DEFAULT '[]',
        outcome TEXT NOT NULL DEFAULT '',
        diagnosis_code TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reply_turn_traces_ts
        ON reply_turn_traces(ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reply_turn_traces_target
        ON reply_turn_traces(session_type, group_id, user_id, ts DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS proactive_diagnostics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        scope TEXT NOT NULL,            -- 'private' / 'group_idle' / 'qzone'
        target TEXT NOT NULL DEFAULT '', -- user_id / group_id / ''
        outcome TEXT NOT NULL,          -- 'sent' / 'skip_X' (X = reason code)
        detail TEXT NOT NULL DEFAULT '{}',
        next_eligible_at REAL DEFAULT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_proactive_diag_ts ON proactive_diagnostics(ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_proactive_diag_scope ON proactive_diagnostics(scope, ts DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_health_stats (
        provider_name    TEXT PRIMARY KEY,
        sample_count     INTEGER NOT NULL DEFAULT 0,
        success_count    INTEGER NOT NULL DEFAULT 0,
        failure_count    INTEGER NOT NULL DEFAULT 0,
        avg_latency_ms   REAL NOT NULL DEFAULT 0,
        last_request_at  REAL DEFAULT NULL,
        last_success_at  REAL DEFAULT NULL,
        last_failure_at  REAL DEFAULT NULL,
        last_error_kind  TEXT NOT NULL DEFAULT '',
        last_seen_at     REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_provider_health_seen ON provider_health_stats(last_seen_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS meme_dictionary (
        term TEXT NOT NULL,
        aliases TEXT NOT NULL DEFAULT '[]',
        meaning TEXT NOT NULL DEFAULT '',
        tone TEXT NOT NULL DEFAULT '[]',
        risk_level TEXT NOT NULL DEFAULT 'low',
        examples TEXT NOT NULL DEFAULT '[]',
        scope TEXT NOT NULL DEFAULT 'public',
        group_id TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        evidence_message_ids TEXT NOT NULL DEFAULT '[]',
        safe_usage TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL,
        PRIMARY KEY(scope, group_id, term)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meme_dictionary_scope
        ON meme_dictionary(scope, group_id, updated_at DESC)
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
    # 共享连接可能被事件循环与线程池并发使用，写锁竞争时等待而不是立刻抛
    # "database is locked"。
    conn.execute("PRAGMA busy_timeout=5000")
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
    if not columns:
        return  # table not created yet; DDL loop will create it with all columns
    if "image_count" not in columns:
        conn.execute("ALTER TABLE group_messages ADD COLUMN image_count INTEGER NOT NULL DEFAULT 0")
    if "visual_summary" not in columns:
        conn.execute("ALTER TABLE group_messages ADD COLUMN visual_summary TEXT NOT NULL DEFAULT ''")
    if "sender_role" not in columns:
        conn.execute("ALTER TABLE group_messages ADD COLUMN sender_role TEXT NOT NULL DEFAULT ''")
    if "thread_id" not in columns:
        conn.execute("ALTER TABLE group_messages ADD COLUMN thread_id TEXT NOT NULL DEFAULT ''")


def _ensure_group_style_schema(conn: sqlite3.Connection) -> None:
    """旧版 group_style_snapshots 以 group_id 为 PK 仅存最新；新版改多行 + created_at 索引保留历史。
    若检测到旧 schema（无 id 列）则 DROP 重建。旧表无写入方，无数据丢失风险。
    """
    columns = _table_columns(conn, "group_style_snapshots")
    if not columns:
        return
    if "id" not in columns or "created_at" not in columns:
        conn.execute("DROP TABLE IF EXISTS group_style_snapshots")
        conn.execute(
            """
            CREATE TABLE group_style_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                style_text TEXT NOT NULL DEFAULT '',
                style_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_style_gid_ts ON group_style_snapshots(group_id, created_at DESC)"
        )


def init_db_sync(data_dir: str | Path) -> Path:
    global _db_path
    _db_path = Path(data_dir) / DB_FILENAME
    with connect_sync(_db_path) as conn:
        # 先迁移老表 schema，避免 DDL 循环里 CREATE INDEX 引用新列时
        # 老表（缺该列）抛 OperationalError。
        _ensure_group_style_schema(conn)
        _ensure_group_message_schema(conn)
        conn.commit()  # 让迁移立即可见，下面的 DDL CREATE INDEX 才能引用新列
        for ddl in DDL_STATEMENTS:
            conn.execute(ddl)
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
