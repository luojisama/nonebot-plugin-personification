from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .db import connect_sync, get_db_path


_MIGRATION_NAMESPACE = "__migration__"
_MIGRATION_KEY = "json_to_sqlite_v1"
_KV_ROOT_KEY = "__root__"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return data


def _backup_json(path: Path) -> None:
    backup_path = path.with_suffix(path.suffix + ".bak")
    if backup_path.exists():
        return
    shutil.copy2(path, backup_path)


def _kv_set(conn: Any, namespace: str, data: Any) -> None:
    conn.execute(
        """
        INSERT INTO kv_store(namespace, key, value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(namespace, key)
        DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (namespace, _KV_ROOT_KEY, json.dumps(data, ensure_ascii=False), time.time()),
    )


def _table_empty(conn: Any, table_name: str) -> bool:
    row = conn.execute(f"SELECT COUNT(1) AS cnt FROM {table_name}").fetchone()
    return not row or int(row["cnt"] if hasattr(row, "__getitem__") else row[0]) == 0


def migrate_all(data_dir: str | Path, logger: Any) -> None:
    data_path = Path(data_dir)
    db_path = get_db_path()
    try:
        with connect_sync(db_path) as conn:
            marker = conn.execute(
                "SELECT value FROM kv_store WHERE namespace=? AND key=?",
                (_MIGRATION_NAMESPACE, _MIGRATION_KEY),
            ).fetchone()
            if marker:
                return

            try:
                _migrate_kv_namespaces(conn, data_path)
                _migrate_chat_history(conn, data_path)
                _migrate_session_histories(conn, data_path)
                _migrate_user_personas(conn, data_path)
                _migrate_user_tasks(conn, data_path)
                conn.execute(
                    """
                    INSERT INTO kv_store(namespace, key, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(namespace, key)
                    DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (_MIGRATION_NAMESPACE, _MIGRATION_KEY, json.dumps({"done": True}), time.time()),
                )
                conn.commit()
            except Exception as exc:
                logger.warning(f"[migration] JSON -> SQLite 迁移失败，已跳过: {exc}")
    except Exception as exc:
        logger.warning(f"[migration] 打开 SQLite 失败，已跳过迁移: {exc}")


def _migrate_kv_namespaces(conn: Any, data_dir: Path) -> None:
    namespaces = ("whitelist", "group_config", "proactive_state", "requests", "friend_requests", "runtime_config")
    for namespace in namespaces:
        path = data_dir / f"{namespace}.json"
        if not path.exists():
            continue
        if not _table_empty(conn, "kv_store"):
            existing = conn.execute(
                "SELECT 1 FROM kv_store WHERE namespace=? AND key=?",
                (namespace, _KV_ROOT_KEY),
            ).fetchone()
            if existing:
                continue
        _backup_json(path)
        _kv_set(conn, namespace, _load_json(path, {} if namespace != "whitelist" else []))


def _migrate_chat_history(conn: Any, data_dir: Path) -> None:
    path = data_dir / "chat_history.json"
    if not path.exists():
        return
    if not _table_empty(conn, "group_messages"):
        return
    raw = _load_json(path, {})
    if not isinstance(raw, dict):
        return
    _backup_json(path)
    metadata: dict[str, Any] = {}
    for group_id, group_data in raw.items():
        if not isinstance(group_data, dict):
            continue
        group_meta = {k: v for k, v in group_data.items() if k != "messages"}
        metadata[str(group_id)] = group_meta
        for msg in group_data.get("messages", []) if isinstance(group_data.get("messages"), list) else []:
            if not isinstance(msg, dict):
                continue
            conn.execute(
                """
                INSERT INTO group_messages(
                    group_id, user_id, nickname, content, is_bot,
                    reply_to_msg_id, reply_to_user_id, mentioned_ids, is_at_bot,
                    message_id, source_kind, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(group_id),
                    str(msg.get("user_id", "") or ""),
                    str(msg.get("nickname", "") or ""),
                    str(msg.get("content", "") or ""),
                    1 if bool(msg.get("is_bot")) else 0,
                    msg.get("reply_to_msg_id"),
                    msg.get("reply_to_user_id"),
                    json.dumps(msg.get("mentioned_ids", []), ensure_ascii=False),
                    1 if bool(msg.get("is_at_bot")) else 0,
                    str(msg.get("message_id", "") or "") or None,
                    str(msg.get("source_kind", "bot" if msg.get("is_bot") else "user") or "user"),
                    float(msg.get("time", time.time()) or time.time()),
                ),
            )
    _kv_set(conn, "chat_history", metadata)


def _migrate_session_histories(conn: Any, data_dir: Path) -> None:
    path = data_dir / "session_histories.json"
    if not path.exists():
        return
    if not _table_empty(conn, "session_messages"):
        return
    raw = _load_json(path, {})
    if not isinstance(raw, dict):
        return
    _backup_json(path)
    for session_id, messages in raw.items():
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            metadata = {k: v for k, v in msg.items() if k not in {"role", "content", "timestamp"}}
            content = msg.get("content", "")
            conn.execute(
                """
                INSERT INTO session_messages(session_id, role, content, is_summary, timestamp, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(session_id),
                    str(msg.get("role", "") or ""),
                    json.dumps(content, ensure_ascii=False),
                    1 if str(msg.get("content", "")).startswith("【对话历史摘要】") else 0,
                    float(msg.get("timestamp", time.time()) or time.time()),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )


def _migrate_user_personas(conn: Any, data_dir: Path) -> None:
    path = data_dir / "user_personas.json"
    if not path.exists():
        return
    if not _table_empty(conn, "user_personas"):
        return
    raw = _load_json(path, {})
    if not isinstance(raw, dict):
        return
    _backup_json(path)
    personas = raw.get("personas", {})
    histories = raw.get("histories", {})
    if isinstance(personas, dict):
        for user_id, entry in personas.items():
            if not isinstance(entry, dict):
                continue
            conn.execute(
                """
                INSERT INTO user_personas(user_id, persona, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET persona=excluded.persona, updated_at=excluded.updated_at
                """,
                (
                    str(user_id),
                    str(entry.get("data", "") or ""),
                    float(entry.get("time", 0) or 0),
                ),
            )
    if isinstance(histories, dict):
        for user_id, items in histories.items():
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                text = str(item or "").strip()
                if not text:
                    continue
                conn.execute(
                    "INSERT INTO persona_histories(user_id, content, created_at) VALUES (?, ?, ?)",
                    (str(user_id), text, float(index)),
                )


def _migrate_user_tasks(conn: Any, data_dir: Path) -> None:
    path = data_dir / "user_tasks.json"
    if not path.exists():
        return
    if not _table_empty(conn, "user_tasks"):
        return
    raw = _load_json(path, {})
    if not isinstance(raw, dict):
        return
    _backup_json(path)
    for user_key, user_data in raw.items():
        if not isinstance(user_data, dict):
            continue
        user_id = str(user_key).removeprefix("user_")
        for task in user_data.get("scheduled_tasks", []) if isinstance(user_data.get("scheduled_tasks"), list) else []:
            if not isinstance(task, dict):
                continue
            conn.execute(
                """
                INSERT INTO user_tasks(
                    task_id, user_id, description, cron, action, params, active, created_at,
                    last_executed_at, last_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, task_id) DO UPDATE SET
                    description=excluded.description,
                    cron=excluded.cron,
                    action=excluded.action,
                    params=excluded.params,
                    active=excluded.active,
                    created_at=excluded.created_at,
                    last_executed_at=excluded.last_executed_at,
                    last_status=excluded.last_status
                """,
                (
                    str(task.get("id", "") or ""),
                    user_id,
                    str(task.get("description", "") or ""),
                    str(task.get("cron", "") or ""),
                    str(task.get("action", "") or ""),
                    json.dumps(task.get("params", {}), ensure_ascii=False),
                    1 if bool(task.get("active", True)) else 0,
                    _parse_task_time(task.get("created_at")),
                    _parse_task_time(task.get("last_executed")),
                    "sent" if str(task.get("last_executed", "") or "").strip() else "pending",
                ),
            )


def _parse_task_time(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except Exception:
            continue
    return None
