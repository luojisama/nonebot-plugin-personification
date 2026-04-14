from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from nonebot import logger

from .context_policy import sanitize_message_content, stringify_history_content
from .db import connect_sync
from .group_context import render_group_context_structured
from .memory_store import get_memory_store

SESSION_HISTORY_LIMIT = 100
GROUP_SESSION_PREFIX = "group_"
PRIVATE_SESSION_PREFIX = "private_"

_plugin_config: Any = None
_compress_tool_caller: Any = None
_compress_tasks: Dict[str, asyncio.Task[None]] = {}

# 向后兼容导出，历史已不再保存在内存 dict 中。
chat_histories: Dict[str, List[Dict[str, Any]]] = {}


def init_session_store(plugin_config: Any, compress_tool_caller: Any = None) -> None:
    global _plugin_config, _compress_tool_caller
    _plugin_config = plugin_config
    _compress_tool_caller = compress_tool_caller


def load_session_histories() -> Dict[str, List[Dict[str, Any]]]:
    with connect_sync() as conn:
        rows = conn.execute("SELECT DISTINCT session_id FROM session_messages").fetchall()
    return {
        str(row["session_id"]): get_session_messages(str(row["session_id"]))
        for row in rows
    }


def _get_compress_threshold() -> int:
    if _plugin_config is not None:
        return int(getattr(_plugin_config, "personification_compress_threshold", 100))
    return 100


def _get_keep_recent() -> int:
    if _plugin_config is not None:
        return int(getattr(_plugin_config, "personification_compress_keep_recent", 20))
    return 20


def _get_history_max_len() -> int:
    if _plugin_config is not None:
        value = int(getattr(_plugin_config, "personification_history_len", 120))
        return max(20, min(value, 400))
    return 120


def _get_message_expire_hours() -> float:
    if _plugin_config is not None:
        value = getattr(_plugin_config, "personification_message_expire_hours", 24.0)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 24.0
    return 24.0


def _get_group_message_expire_hours() -> float:
    if _plugin_config is not None:
        raw = getattr(_plugin_config, "personification_group_context_expire_hours", 6.0)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 6.0
    return 6.0


def build_group_session_id(group_id: str) -> str:
    return f"{GROUP_SESSION_PREFIX}{group_id}"


def build_private_session_id(user_id: str) -> str:
    return f"{PRIVATE_SESSION_PREFIX}{user_id}"


def is_private_session_id(session_id: str) -> bool:
    return session_id.startswith(PRIVATE_SESSION_PREFIX)


def ensure_session_history(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    if legacy_session_id and legacy_session_id != session_id:
        with connect_sync() as conn:
            conn.execute(
                "UPDATE session_messages SET session_id=? WHERE session_id=?",
                (session_id, legacy_session_id),
            )
            conn.commit()
    return get_session_messages(session_id)


def get_session_history_limit(session_id: str) -> int:
    return _get_compress_threshold()


def trim_session_history(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    return ensure_session_history(session_id, legacy_session_id=legacy_session_id)


def _is_summary_message(msg: Dict) -> bool:
    return bool(msg.get("is_summary")) or (
        msg.get("role") == "system" and str(msg.get("content", "")).startswith("【对话历史摘要】")
    )


def _dedupe_summary_messages(messages: List[Dict]) -> List[Dict]:
    summaries = [msg for msg in messages if _is_summary_message(msg)]
    non_summaries = [msg for msg in messages if not _is_summary_message(msg)]
    if not summaries:
        return non_summaries
    return [summaries[-1]] + non_summaries


def _filter_expired_messages(
    messages: List[Dict],
    expire_hours: float,
    *,
    keep_recent: bool = True,
    keep_summary: bool = True,
) -> List[Dict]:
    if expire_hours <= 0:
        return messages
    now_ts = time.time()
    expire_seconds = expire_hours * 3600
    keep_recent_count = _get_keep_recent() if keep_recent else 0
    result: List[Dict] = []
    recent_indices = set(range(max(0, len(messages) - keep_recent_count), len(messages)))
    for idx, msg in enumerate(messages):
        if _is_summary_message(msg):
            if keep_summary:
                result.append(msg)
            continue
        if idx in recent_indices:
            result.append(msg)
            continue
        msg_ts = float(msg.get("timestamp", 0) or 0)
        if not msg_ts or now_ts - msg_ts <= expire_seconds:
            result.append(msg)
    return result


def _deserialize_session_row(row: Any) -> Dict[str, Any]:
    try:
        content = json.loads(row["content"])
    except Exception:
        content = row["content"]
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "id": row["id"],
        "role": row["role"],
        "content": content,
        "timestamp": float(row["timestamp"] or 0),
        "is_summary": bool(row["is_summary"]),
        **metadata,
    }


def _fetch_session_messages_sync(session_id: str) -> List[Dict[str, Any]]:
    with connect_sync() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, is_summary, timestamp, metadata
            FROM session_messages
            WHERE session_id=?
            ORDER BY timestamp ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
    return [_deserialize_session_row(row) for row in rows]


def get_session_messages(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    if legacy_session_id and legacy_session_id != session_id:
        ensure_session_history(session_id, legacy_session_id=legacy_session_id)
    raw_messages = _fetch_session_messages_sync(session_id)
    if is_private_session_id(session_id):
        expire_hours = _get_message_expire_hours()
        if expire_hours <= 0:
            return raw_messages
        return _filter_expired_messages(raw_messages, expire_hours)

    expire_hours = _get_group_message_expire_hours()
    if expire_hours <= 0:
        return _dedupe_summary_messages(raw_messages)
    filtered = _filter_expired_messages(raw_messages, expire_hours, keep_recent=True, keep_summary=True)
    return _dedupe_summary_messages(filtered)


def _render_structured_speakers(messages: List[Dict[str, Any]]) -> str:
    structured = render_group_context_structured(messages)
    if structured:
        return structured

    lines: List[str] = []
    for msg in messages:
        speaker = str(msg.get("speaker") or msg.get("role") or "未知")
        content = stringify_history_content(msg.get("content", ""))[:120]
        if not content:
            continue
        lines.append(f"[对话][{speaker}|uid={msg.get('user_id', '') or ''}|普通发言] {content}")
    return "\n".join(lines)


def _build_compress_prompt(messages: List[Dict]) -> str:
    return (
        "以下是一段聊天历史，请压缩为背景摘要（150字以内）。\n"
        "如果是群聊，必须保留：谁提出了什么请求 谁回应了谁 当前结论 Bot是否已介入。\n"
        "用昵称区分不同发言人，不要丢失回复、提及和对话指向关系。\n"
        "只输出摘要文本，不要任何前缀。\n\n"
        f"{_render_structured_speakers(messages)}"
    )


def _replace_history_with_summary_sync(session_id: str, summary_text: str, recent: List[Dict[str, Any]]) -> None:
    with connect_sync() as conn:
        conn.execute("DELETE FROM session_messages WHERE session_id=?", (session_id,))
        conn.execute(
            """
            INSERT INTO session_messages(session_id, role, content, is_summary, timestamp, metadata)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (
                session_id,
                "system",
                json.dumps(f"【对话历史摘要】{summary_text}", ensure_ascii=False),
                time.time(),
                json.dumps({}, ensure_ascii=False),
            ),
        )
        for msg in recent:
            metadata = {k: v for k, v in msg.items() if k not in {"id", "role", "content", "timestamp", "is_summary"}}
            conn.execute(
                """
                INSERT INTO session_messages(session_id, role, content, is_summary, timestamp, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(msg.get("role", "") or ""),
                    json.dumps(msg.get("content", ""), ensure_ascii=False),
                    1 if bool(msg.get("is_summary")) else 0,
                    float(msg.get("timestamp", time.time()) or time.time()),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
        conn.commit()


async def _run_compress(session_id: str) -> None:
    try:
        history = _fetch_session_messages_sync(session_id)
        threshold = _get_compress_threshold()
        keep = _get_keep_recent()
        if len(history) < threshold:
            return
        to_compress = history[:-keep] if keep > 0 else history
        recent = history[-keep:] if keep > 0 else []
        if not to_compress:
            return

        summary_text = ""
        caller = _compress_tool_caller
        if caller is not None:
            try:
                prompt = _build_compress_prompt(to_compress)
                response = await caller.chat_with_tools(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    use_builtin_search=False,
                )
                summary_text = str(response.content or "").strip()
            except Exception as exc:
                logger.warning(f"[session_store] compress LLM call failed for {session_id}: {exc}")
        if not summary_text:
            summary_text = f"（此前 {len(to_compress)} 条历史已省略）"
        await asyncio.to_thread(_replace_history_with_summary_sync, session_id, summary_text, recent)
    except Exception as exc:
        logger.warning(f"[session_store] compress error for {session_id}: {exc}")
    finally:
        _compress_tasks.pop(session_id, None)


def _schedule_compress(session_id: str) -> None:
    if session_id in _compress_tasks:
        return
    try:
        task = asyncio.create_task(_run_compress(session_id))
    except RuntimeError:
        return
    _compress_tasks[session_id] = task


def append_session_message(
    session_id: str,
    role: str,
    content: Any,
    legacy_session_id: Optional[str] = None,
    **metadata: Any,
) -> List[Dict]:
    if legacy_session_id and legacy_session_id != session_id:
        ensure_session_history(session_id, legacy_session_id=legacy_session_id)

    sanitized_content = sanitize_message_content(content)
    timestamp = time.time()
    safe_metadata = {key: value for key, value in metadata.items() if value is not None}
    with connect_sync() as conn:
        conn.execute(
            """
            INSERT INTO session_messages(session_id, role, content, is_summary, timestamp, metadata)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (
                session_id,
                role,
                json.dumps(sanitized_content, ensure_ascii=False),
                timestamp,
                json.dumps(safe_metadata, ensure_ascii=False),
            ),
        )
        rows = conn.execute(
            "SELECT id FROM session_messages WHERE session_id=? ORDER BY timestamp ASC, id ASC",
            (session_id,),
        ).fetchall()
        max_len = _get_history_max_len()
        if len(rows) > max_len:
            stale_ids = [int(row["id"]) for row in rows[:-max_len]]
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(
                f"DELETE FROM session_messages WHERE id IN ({placeholders})",
                tuple(stale_ids),
            )
        conn.commit()

    try:
        memory_store = get_memory_store()
        if session_id.startswith(GROUP_SESSION_PREFIX):
            group_id = session_id.removeprefix(GROUP_SESSION_PREFIX)
            memory_store.append_group_message(
                group_id=group_id,
                role=role,
                content=sanitized_content,
                metadata=safe_metadata,
            )
        elif legacy_session_id and not str(legacy_session_id).startswith(PRIVATE_SESSION_PREFIX):
            memory_store.append_group_message(
                group_id=str(legacy_session_id),
                role=role,
                content=sanitized_content,
                metadata=safe_metadata,
            )
    except Exception:
        pass

    current = get_session_messages(session_id)
    if len(current) >= _get_compress_threshold():
        _schedule_compress(session_id)
    return current


def save_session_histories() -> None:
    return None


async def clear_all_session_histories() -> int:
    tasks = list(_compress_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _compress_tasks.clear()
    with connect_sync() as conn:
        row = conn.execute("SELECT COUNT(DISTINCT session_id) AS cnt FROM session_messages").fetchone()
        count = int(row["cnt"] if row else 0)
        conn.execute("DELETE FROM session_messages")
        conn.commit()
    return count
