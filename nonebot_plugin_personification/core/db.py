from __future__ import annotations

import asyncio
import hashlib
import json
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
    CREATE TABLE IF NOT EXISTS avatar_relation_evidence (
        group_id         TEXT NOT NULL,
        left_user_id     TEXT NOT NULL,
        right_user_id    TEXT NOT NULL,
        relation         TEXT NOT NULL,
        confidence       REAL NOT NULL DEFAULT 0,
        evidence_tags    TEXT NOT NULL DEFAULT '[]',
        asset_kinds      TEXT NOT NULL DEFAULT '[]',
        left_avatar_hash TEXT NOT NULL DEFAULT '',
        right_avatar_hash TEXT NOT NULL DEFAULT '',
        schema_version   INTEGER NOT NULL DEFAULT 1,
        observed_at      REAL NOT NULL DEFAULT 0,
        expires_at       REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (group_id, left_user_id, right_user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_avatar_relation_evidence_group
        ON avatar_relation_evidence(group_id, observed_at DESC)
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
    CREATE TABLE IF NOT EXISTS user_policy_state (
        user_id TEXT PRIMARY KEY,
        auto_stage INTEGER NOT NULL DEFAULT 0,
        auto_tier TEXT NOT NULL DEFAULT 'allow',
        auto_expires_at REAL NOT NULL DEFAULT 0,
        violation_count INTEGER NOT NULL DEFAULT 0,
        last_violation_at REAL NOT NULL DEFAULT 0,
        manual_mode TEXT NOT NULL DEFAULT 'inherit',
        manual_expires_at REAL NOT NULL DEFAULT 0,
        reason_code TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        updated_by TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_policy_state_active
        ON user_policy_state(auto_tier, auto_expires_at, manual_mode, manual_expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS user_policy_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL,
        event_kind TEXT NOT NULL,
        surface TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        intent TEXT NOT NULL DEFAULT '',
        severity TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        reason_code TEXT NOT NULL DEFAULT '',
        counts_violation INTEGER NOT NULL DEFAULT 0,
        resulting_tier TEXT NOT NULL DEFAULT 'allow',
        resulting_expires_at REAL NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        classifier_version TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_policy_events_user
        ON user_policy_events(user_id, created_at DESC, id DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS user_policy_evidence (
        idempotency_key TEXT PRIMARY KEY,
        ciphertext TEXT NOT NULL,
        key_version INTEGER NOT NULL DEFAULT 1,
        expires_at REAL NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_policy_evidence_expiry
        ON user_policy_evidence(expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS user_policy_closures (
        user_id TEXT NOT NULL,
        channel_key TEXT NOT NULL,
        event_key TEXT NOT NULL,
        claimed_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (user_id, channel_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_policy_closures_expiry
        ON user_policy_closures(expires_at)
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
    CREATE TABLE IF NOT EXISTS token_usage_hourly_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket_hour TEXT NOT NULL,
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
    CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_hour_bucket
        ON token_usage_hourly_ledger(bucket_hour, group_id, user_id, model, purpose)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_token_usage_hour ON token_usage_hourly_ledger(bucket_hour)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_token_usage_hour_group
        ON token_usage_hourly_ledger(group_id, bucket_hour)
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
    CREATE TABLE IF NOT EXISTS qzone_publish_operations (
        operation_id TEXT PRIMARY KEY,
        bot_id TEXT NOT NULL,
        period TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'post',
        payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL CHECK (
            status IN ('reserved', 'dispatching', 'succeeded', 'definite_failure', 'unknown')
        ),
        fence_token INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        reserved_at REAL NOT NULL DEFAULT 0,
        dispatch_started_at REAL NOT NULL DEFAULT 0,
        lease_expires_at REAL NOT NULL DEFAULT 0,
        completed_at REAL NOT NULL DEFAULT 0,
        remote_id TEXT NOT NULL DEFAULT '',
        remote_time REAL NOT NULL DEFAULT 0,
        result_code TEXT NOT NULL DEFAULT '',
        resolution_source TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qzone_publish_ops_status
        ON qzone_publish_operations(period, status, lease_expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qzone_publish_ops_payload
        ON qzone_publish_operations(bot_id, kind, payload_hash, status)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_qzone_publish_ops_remote
        ON qzone_publish_operations(bot_id, remote_id)
        WHERE remote_id <> '' AND status = 'succeeded'
    """,
    """
    CREATE TABLE IF NOT EXISTS qzone_monthly_usage (
        period TEXT PRIMARY KEY,
        confirmed_count INTEGER NOT NULL DEFAULT 0,
        forward_count INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
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
        managed_by TEXT NOT NULL DEFAULT 'manual',
        updated_at REAL NOT NULL,
        PRIMARY KEY(scope, group_id, term)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meme_dictionary_scope
        ON meme_dictionary(scope, group_id, updated_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS meme_senses (
        sense_id TEXT PRIMARY KEY,
        scope TEXT NOT NULL DEFAULT 'public',
        group_id TEXT NOT NULL DEFAULT '',
        term TEXT NOT NULL,
        meaning TEXT NOT NULL,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        game_context_json TEXT NOT NULL DEFAULT '{}',
        version_context TEXT NOT NULL DEFAULT '',
        usage_context TEXT NOT NULL DEFAULT '',
        safe_usage TEXT NOT NULL DEFAULT '',
        risk_level TEXT NOT NULL DEFAULT 'low',
        status TEXT NOT NULL DEFAULT 'observed',
        confidence REAL NOT NULL DEFAULT 0,
        source_count INTEGER NOT NULL DEFAULT 0,
        platform_count INTEGER NOT NULL DEFAULT 0,
        auto_managed INTEGER NOT NULL DEFAULT 1,
        first_seen_at REAL NOT NULL,
        last_verified_at REAL NOT NULL DEFAULT 0,
        reverify_after REAL NOT NULL DEFAULT 0,
        expires_at REAL NOT NULL DEFAULT 0,
        manual_locked INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meme_senses_lookup
        ON meme_senses(scope, group_id, term, status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meme_senses_review
        ON meme_senses(status, reverify_after, expires_at, updated_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS meme_evidence_claims (
        claim_id TEXT PRIMARY KEY,
        sense_id TEXT NOT NULL,
        packet_id TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL,
        content_id TEXT NOT NULL,
        canonical_url TEXT NOT NULL DEFAULT '',
        content_type TEXT NOT NULL DEFAULT '',
        discussion_id TEXT NOT NULL DEFAULT '',
        evidence_type TEXT NOT NULL DEFAULT '',
        author_fingerprint TEXT NOT NULL DEFAULT '',
        quote TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL DEFAULT '',
        media_fingerprint TEXT NOT NULL DEFAULT '',
        source_cluster_id TEXT NOT NULL,
        extractor_version TEXT NOT NULL DEFAULT '',
        model_route TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        source_quality REAL NOT NULL DEFAULT 0,
        published_at REAL NOT NULL DEFAULT 0,
        retrieved_at REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        FOREIGN KEY(sense_id) REFERENCES meme_senses(sense_id) ON DELETE CASCADE,
        UNIQUE(sense_id, source_cluster_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meme_evidence_sense
        ON meme_evidence_claims(sense_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meme_evidence_source
        ON meme_evidence_claims(source_cluster_id, platform, content_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS meme_learning_events (
        event_id TEXT PRIMARY KEY,
        sense_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        old_status TEXT NOT NULL DEFAULT '',
        new_status TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT 'system',
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        FOREIGN KEY(sense_id) REFERENCES meme_senses(sense_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meme_learning_events_sense
        ON meme_learning_events(sense_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS data_transfer_tasks (
        task_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        package_id TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        bot_id TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_transfer_journal (
        journal_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        package_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        bot_id TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        backup_path TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_creator_tasks (
        task_id TEXT PRIMARY KEY,
        created_by TEXT NOT NULL,
        request_text TEXT NOT NULL,
        suggested_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        phase TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1,
        context_json TEXT NOT NULL DEFAULT '{}',
        question_json TEXT NOT NULL DEFAULT '{}',
        artifact_digest TEXT NOT NULL DEFAULT '',
        artifact_path TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        lease_until REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        completed_at REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tool_creator_tasks_status
        ON tool_creator_tasks(status, updated_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_creator_events (
        task_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        phase TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        PRIMARY KEY (task_id, seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tool_creator_events_task
        ON tool_creator_events(task_id, seq)
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_installations (
        installation_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        server_name TEXT NOT NULL,
        server_title TEXT NOT NULL DEFAULT '',
        server_version TEXT NOT NULL,
        package_type TEXT NOT NULL,
        package_identifier TEXT NOT NULL,
        command TEXT NOT NULL,
        args_json TEXT NOT NULL DEFAULT '[]',
        env_json TEXT NOT NULL DEFAULT '{}',
        secret_names_json TEXT NOT NULL DEFAULT '[]',
        name_prefix TEXT NOT NULL,
        desired_enabled INTEGER NOT NULL DEFAULT 1,
        observed_status TEXT NOT NULL DEFAULT 'stopped',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        last_error TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_installations_identity
        ON mcp_installations(source_url, server_name, server_version, package_identifier)
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_tool_policies (
        installation_id TEXT NOT NULL,
        remote_name TEXT NOT NULL,
        registered_name TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        parameters_json TEXT NOT NULL DEFAULT '{}',
        output_schema_json TEXT NOT NULL DEFAULT '{}',
        annotations_json TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 0,
        risk_level TEXT NOT NULL DEFAULT 'admin',
        side_effect TEXT NOT NULL DEFAULT 'unknown',
        publisher_read_only INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,
        PRIMARY KEY (installation_id, remote_name),
        FOREIGN KEY (installation_id) REFERENCES mcp_installations(installation_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_tool_registered_name
        ON mcp_tool_policies(registered_name)
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_builtin_platforms (
        installation_id TEXT NOT NULL,
        platform TEXT NOT NULL CHECK (platform IN ('bilibili', 'douyin', 'tieba', 'xiaoheihe')),
        desired_enabled INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 0,
        config_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (installation_id, platform),
        FOREIGN KEY (installation_id) REFERENCES mcp_installations(installation_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mcp_builtin_platforms_enabled
        ON mcp_builtin_platforms(installation_id, desired_enabled, platform)
    """,
    """
    CREATE TABLE IF NOT EXISTS qq_outbound_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        part_index INTEGER NOT NULL CHECK (part_index >= 0),
        bot_id TEXT NOT NULL,
        conversation_kind TEXT NOT NULL CHECK (conversation_kind IN ('group', 'private')),
        conversation_id TEXT NOT NULL,
        message_id TEXT DEFAULT NULL,
        user_target TEXT NOT NULL DEFAULT '',
        surface TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'unknown')),
        preview TEXT NOT NULL DEFAULT '',
        content_hmac TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        recalled_at REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_qq_outbound_operation_part
        ON qq_outbound_ledger(operation_id, part_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qq_outbound_scope
        ON qq_outbound_ledger(bot_id, conversation_kind, conversation_id, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qq_outbound_message
        ON qq_outbound_ledger(bot_id, conversation_kind, conversation_id, message_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qq_outbound_status
        ON qq_outbound_ledger(status, recalled_at, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS qq_recall_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        outbound_operation_id TEXT NOT NULL UNIQUE,
        bot_id TEXT NOT NULL,
        conversation_kind TEXT NOT NULL CHECK (conversation_kind IN ('group', 'private')),
        conversation_id TEXT NOT NULL,
        requester_user_id TEXT NOT NULL DEFAULT '',
        actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'admin')),
        trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('agent', 'admin_command')),
        status TEXT NOT NULL CHECK (
            status IN ('dispatching', 'succeeded', 'partial', 'definite_failure', 'unknown')
        ),
        total_count INTEGER NOT NULL CHECK (total_count >= 0),
        recalled_count INTEGER NOT NULL DEFAULT 0 CHECK (recalled_count >= 0),
        error_code TEXT NOT NULL DEFAULT '',
        requested_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qq_recall_scope
        ON qq_recall_operations(bot_id, conversation_kind, conversation_id, requested_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS qq_recall_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recall_operation_id INTEGER NOT NULL,
        ledger_id INTEGER NOT NULL UNIQUE,
        message_id TEXT NOT NULL,
        part_index INTEGER NOT NULL CHECK (part_index >= 0),
        status TEXT NOT NULL CHECK (
            status IN ('dispatching', 'succeeded', 'definite_failure', 'unknown')
        ),
        error_code TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL,
        FOREIGN KEY(recall_operation_id) REFERENCES qq_recall_operations(id) ON DELETE CASCADE,
        FOREIGN KEY(ledger_id) REFERENCES qq_outbound_ledger(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qq_recall_items_operation
        ON qq_recall_items(recall_operation_id, part_index DESC)
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


def _ensure_mcp_tool_policy_schema(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "mcp_tool_policies")
    if not columns:
        return
    additions = {
        "title": "TEXT NOT NULL DEFAULT ''",
        "output_schema_json": "TEXT NOT NULL DEFAULT '{}'",
        "annotations_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE mcp_tool_policies ADD COLUMN {name} {declaration}")


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


_QZONE_PUBLISH_V2_COLUMNS = {
    "operation_id",
    "bot_id",
    "period",
    "kind",
    "payload_hash",
    "payload_json",
    "status",
    "fence_token",
    "created_at",
    "updated_at",
    "reserved_at",
    "dispatch_started_at",
    "lease_expires_at",
    "completed_at",
    "remote_id",
    "remote_time",
    "result_code",
    "resolution_source",
    "detail",
}


def _create_qzone_publish_v2_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE qzone_publish_operations (
            operation_id TEXT PRIMARY KEY,
            bot_id TEXT NOT NULL,
            period TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'post',
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK (
                status IN ('reserved', 'dispatching', 'succeeded', 'definite_failure', 'unknown')
            ),
            fence_token INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            reserved_at REAL NOT NULL DEFAULT 0,
            dispatch_started_at REAL NOT NULL DEFAULT 0,
            lease_expires_at REAL NOT NULL DEFAULT 0,
            completed_at REAL NOT NULL DEFAULT 0,
            remote_id TEXT NOT NULL DEFAULT '',
            remote_time REAL NOT NULL DEFAULT 0,
            result_code TEXT NOT NULL DEFAULT '',
            resolution_source TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}'
        )
        """
    )


def _ensure_qzone_publish_schema(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "qzone_publish_operations")
    if not columns:
        return
    if _QZONE_PUBLISH_V2_COLUMNS.issubset(columns):
        return

    rows = conn.execute("SELECT * FROM qzone_publish_operations").fetchall()
    conn.execute("ALTER TABLE qzone_publish_operations RENAME TO qzone_publish_operations_v1")
    _create_qzone_publish_v2_table(conn)
    now = float(conn.execute("SELECT unixepoch('now')").fetchone()[0])
    for row in rows:
        operation_id = str(row["operation_id"] or "")[:96]
        old_status = str(row["status"] or "unknown").strip().lower()
        status = {
            "committed": "succeeded",
            "released": "definite_failure",
            "expired": "definite_failure",
            "reserved": "unknown",
            "unknown": "unknown",
        }.get(old_status, "unknown")
        reserved_at = float(row["reserved_at"] or 0)
        completed_at = float(row["completed_at"] or 0)
        created_at = reserved_at or completed_at or now
        updated_at = completed_at or reserved_at or now
        legacy_hash = hashlib.sha256(f"legacy:{operation_id}".encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO qzone_publish_operations(
                operation_id, bot_id, period, kind, payload_hash, payload_json,
                status, fence_token, created_at, updated_at, reserved_at,
                dispatch_started_at, lease_expires_at, completed_at, remote_id,
                remote_time, result_code, resolution_source, detail
            ) VALUES (?, '', ?, ?, ?, '{"legacy":true}', ?, 1, ?, ?, ?, 0, 0, ?, '', 0, ?, 'migration_v2', '{"migration":"v1"}')
            """,
            (
                operation_id,
                str(row["period"] or ""),
                str(row["kind"] or "post")[:32],
                legacy_hash,
                status,
                created_at,
                updated_at,
                reserved_at,
                completed_at,
                f"legacy_{old_status}"[:64],
            ),
        )
    conn.execute("DROP TABLE qzone_publish_operations_v1")


def _migrate_qzone_monthly_usage(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM kv_store WHERE namespace=? AND key=?",
        ("qzone_post_state", "__root__"),
    ).fetchone()
    if row is None:
        return
    try:
        state = json.loads(row["value"] or "{}")
    except Exception:
        return
    if not isinstance(state, dict):
        return
    period = str(state.get("period") or "").strip()
    if len(period) != 7 or period[4:5] != "-" or not period.replace("-", "").isdigit():
        return
    try:
        confirmed_count = max(0, int(state.get("count", 0) or 0))
        forward_count = max(0, int(state.get("forward_count", 0) or 0))
    except Exception:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO qzone_monthly_usage(period, confirmed_count, forward_count, updated_at)
        VALUES (?, ?, ?, unixepoch('now'))
        """,
        (period, confirmed_count, forward_count),
    )


def _migrate_legacy_meme_senses(conn: sqlite3.Connection) -> None:
    if not _table_columns(conn, "meme_senses"):
        return
    rows = conn.execute("SELECT * FROM meme_dictionary WHERE managed_by!='auto'").fetchall()
    for row in rows:
        scope = str(row["scope"] or "public")
        group_id = str(row["group_id"] or "")
        term = str(row["term"] or "")
        existing_manual = conn.execute(
            """SELECT 1 FROM meme_senses WHERE scope=? AND group_id=? AND term=?
               AND manual_locked=1 LIMIT 1""",
            (scope, group_id, term),
        ).fetchone()
        if existing_manual is not None:
            continue
        identity = f"legacy\0{scope}\0{group_id}\0{term}".encode("utf-8")
        sense_id = "legacy_" + hashlib.sha256(identity).hexdigest()[:32]
        updated_at = float(row["updated_at"] or 0)
        conn.execute(
            """
            INSERT OR IGNORE INTO meme_senses(
                sense_id,scope,group_id,term,meaning,aliases_json,game_context_json,
                version_context,usage_context,safe_usage,risk_level,status,confidence,
                source_count,platform_count,auto_managed,first_seen_at,last_verified_at,
                reverify_after,expires_at,manual_locked,revision,updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '{}', '', '', ?, ?, 'manual_locked', ?, 0, 0, 0, ?, ?, 0, 0, 1, 1, ?)
            """,
            (
                sense_id,
                scope,
                group_id,
                term,
                str(row["meaning"] or ""),
                str(row["aliases"] or "[]"),
                str(row["safe_usage"] or ""),
                str(row["risk_level"] or "low"),
                float(row["confidence"] or 0),
                updated_at,
                updated_at,
                updated_at,
            ),
        )


def _ensure_meme_dictionary_schema(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "meme_dictionary")
    if columns and "managed_by" not in columns:
        conn.execute("ALTER TABLE meme_dictionary ADD COLUMN managed_by TEXT NOT NULL DEFAULT 'manual'")


def init_db_sync(data_dir: str | Path) -> Path:
    global _db_path
    _db_path = Path(data_dir) / DB_FILENAME
    with connect_sync(_db_path) as conn:
        # 先迁移老表 schema，避免 DDL 循环里 CREATE INDEX 引用新列时
        # 老表（缺该列）抛 OperationalError。
        _ensure_group_style_schema(conn)
        _ensure_group_message_schema(conn)
        _ensure_qzone_publish_schema(conn)
        _ensure_mcp_tool_policy_schema(conn)
        _ensure_meme_dictionary_schema(conn)
        conn.commit()  # 让迁移立即可见，下面的 DDL CREATE INDEX 才能引用新列
        for ddl in DDL_STATEMENTS:
            conn.execute(ddl)
        _migrate_qzone_monthly_usage(conn)
        _migrate_legacy_meme_senses(conn)
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
