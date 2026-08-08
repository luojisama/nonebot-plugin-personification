from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .embedding_index import cosine_similarity, embed_text, tokenize


THREAD_ATTACH_THRESHOLD = 0.45
ACTIVE_THREAD_LOOKBACK_SECONDS = 6 * 3600
RECENT_THREAD_LIMIT = 50
PLUGIN_COMMAND_ATTACH_SECONDS = 30.0
PLUGIN_FOLLOWUP_ATTACH_SECONDS = 60.0
HUMAN_DIALOGUE_ATTACH_SECONDS = 120.0
HUMAN_DIALOGUE_RECORD_LIMIT = 8


@dataclass(frozen=True)
class ThreadAssignment:
    thread_id: str
    score: float
    reason: str
    is_new: bool = False


def _json_loads_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item or "").strip()]


def _new_thread_id(group_id: str) -> str:
    return f"thr_{group_id}_{uuid.uuid4().hex[:12]}"


def _keyword_overlap(left: str, right: str) -> float:
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _time_proximity(now_ts: float, last_active_at: float) -> float:
    if last_active_at <= 0:
        return 0.0
    age = max(0.0, now_ts - last_active_at)
    return max(0.0, min(1.0, math.exp(-age / 1800.0)))


def _thread_rows(conn: sqlite3.Connection, group_id: str, now_ts: float) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT thread_id, group_id, topic_summary, participants, created_at, last_active_at
        FROM conversation_threads
        WHERE group_id=? AND last_active_at>=?
        ORDER BY last_active_at DESC
        LIMIT ?
        """,
        (str(group_id), now_ts - ACTIVE_THREAD_LOOKBACK_SECONDS, RECENT_THREAD_LIMIT),
    ).fetchall()


def _recent_thread_messages(conn: sqlite3.Connection, group_id: str) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT thread_id, user_id, content, mentioned_ids, timestamp
        FROM group_messages
        WHERE group_id=? AND thread_id<>''
        ORDER BY timestamp DESC
        LIMIT 120
        """,
        (str(group_id),),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        thread_id = str(row["thread_id"] or "")
        if not thread_id:
            continue
        grouped.setdefault(thread_id, []).append(row)
    return grouped


def _reply_thread(conn: sqlite3.Connection, group_id: str, reply_to_msg_id: str) -> str:
    if not reply_to_msg_id:
        return ""
    row = conn.execute(
        """
        SELECT thread_id FROM group_messages
        WHERE group_id=? AND message_id=? AND thread_id<>''
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (str(group_id), str(reply_to_msg_id)),
    ).fetchone()
    return str(row["thread_id"] or "") if row else ""


def _recent_source_row(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    source_kind: str,
    now_ts: float,
    max_age_seconds: float,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT thread_id, user_id, message_id, source_kind, timestamp
        FROM group_messages
        WHERE group_id=? AND LOWER(TRIM(source_kind))=? AND timestamp>=? AND thread_id<>''
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (str(group_id), str(source_kind), float(now_ts - max_age_seconds)),
    ).fetchone()


def _row_mentions(row: sqlite3.Row) -> tuple[str, ...]:
    return tuple(_json_loads_list(row["mentioned_ids"]))


def _row_relation_targets(row: sqlite3.Row) -> tuple[str, ...]:
    reply_to_user_id = str(row["reply_to_user_id"] or "").strip()
    return tuple(
        dict.fromkeys(
            item
            for item in (reply_to_user_id, *_row_mentions(row))
            if item
        )
    )


def _carried_human_thread(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    user_id: str,
    now_ts: float,
) -> tuple[str, tuple[str, ...]]:
    rows = conn.execute(
        """
        SELECT thread_id, user_id, reply_to_user_id, mentioned_ids, is_bot, source_kind,
               message_id, timestamp
        FROM group_messages
        WHERE group_id=? AND timestamp>=?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (
            str(group_id),
            float(now_ts - HUMAN_DIALOGUE_ATTACH_SECONDS),
            HUMAN_DIALOGUE_RECORD_LIMIT,
        ),
    ).fetchall()
    ordered = list(reversed(rows))
    current_user_id = str(user_id or "").strip()
    for anchor_index in range(len(ordered) - 1, -1, -1):
        anchor = ordered[anchor_index]
        if bool(anchor["is_bot"]) or str(anchor["source_kind"] or "").strip().lower() != "user":
            continue
        anchor_user_id = str(anchor["user_id"] or "").strip()
        targets = _row_relation_targets(anchor)
        if len(targets) != 1 or not anchor_user_id:
            continue
        target_user_id = targets[0]
        if target_user_id == anchor_user_id or current_user_id not in {anchor_user_id, target_user_id}:
            continue
        pair = {anchor_user_id, target_user_id}
        valid = True
        for row in ordered[anchor_index + 1:]:
            if bool(row["is_bot"]) or str(row["source_kind"] or "").strip().lower() != "user":
                valid = False
                break
            if str(row["user_id"] or "").strip() not in pair:
                valid = False
                break
            if any(target not in pair for target in _row_relation_targets(row)):
                valid = False
                break
        thread_id = str(anchor["thread_id"] or "").strip()
        if valid and thread_id:
            return thread_id, tuple(pair)
    return "", ()


def assign_thread_for_message(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    user_id: str,
    content: str,
    message_id: str = "",
    reply_to_msg_id: str = "",
    reply_to_user_id: str = "",
    mentioned_ids: list[str] | None = None,
    source_kind: str = "user",
    timestamp: float | None = None,
) -> ThreadAssignment:
    now_ts = float(timestamp or time.time())
    normalized_group_id = str(group_id)
    normalized_user_id = str(user_id or "")
    normalized_content = str(content or "").strip()
    normalized_mentions = [str(item or "").strip() for item in list(mentioned_ids or []) if str(item or "").strip()]
    normalized_source_kind = str(source_kind or "user").strip().lower() or "user"
    explicit_participants = tuple(
        dict.fromkeys(
            item
            for item in (normalized_user_id, str(reply_to_user_id or "").strip(), *normalized_mentions)
            if item
        )
    )

    replied_thread_id = _reply_thread(conn, normalized_group_id, str(reply_to_msg_id or "").strip())
    if replied_thread_id:
        _upsert_thread(
            conn,
            thread_id=replied_thread_id,
            group_id=normalized_group_id,
            user_id=normalized_user_id,
            participant_ids=explicit_participants,
            content=normalized_content,
            timestamp=now_ts,
        )
        return ThreadAssignment(replied_thread_id, 1.0, "reply_to_match")

    if normalized_source_kind == "plugin_command":
        thread_id = _new_thread_id(normalized_group_id)
        _upsert_thread(
            conn,
            thread_id=thread_id,
            group_id=normalized_group_id,
            user_id=normalized_user_id,
            participant_ids=explicit_participants,
            content=normalized_content,
            timestamp=now_ts,
            is_new=True,
        )
        return ThreadAssignment(thread_id, 1.0, "new_plugin_episode", is_new=True)

    if normalized_source_kind == "plugin":
        command_row = _recent_source_row(
            conn,
            group_id=normalized_group_id,
            source_kind="plugin_command",
            now_ts=now_ts,
            max_age_seconds=PLUGIN_COMMAND_ATTACH_SECONDS,
        )
        if command_row is not None:
            thread_id = str(command_row["thread_id"] or "").strip()
            if thread_id:
                _upsert_thread(
                    conn,
                    thread_id=thread_id,
                    group_id=normalized_group_id,
                    user_id=normalized_user_id,
                    participant_ids=(*explicit_participants, str(command_row["user_id"] or "")),
                    content=normalized_content,
                    timestamp=now_ts,
                )
                return ThreadAssignment(thread_id, 1.0, "plugin_command_episode")

    has_explicit_target = bool(str(reply_to_user_id or "").strip() or normalized_mentions)
    if normalized_source_kind == "user" and not has_explicit_target:
        carried_thread_id, carried_participants = _carried_human_thread(
            conn,
            group_id=normalized_group_id,
            user_id=normalized_user_id,
            now_ts=now_ts,
        )
        if carried_thread_id:
            _upsert_thread(
                conn,
                thread_id=carried_thread_id,
                group_id=normalized_group_id,
                user_id=normalized_user_id,
                participant_ids=(*explicit_participants, *carried_participants),
                content=normalized_content,
                timestamp=now_ts,
            )
            return ThreadAssignment(carried_thread_id, 1.0, "carried_human_dialogue")

        plugin_row = _recent_source_row(
            conn,
            group_id=normalized_group_id,
            source_kind="plugin",
            now_ts=now_ts,
            max_age_seconds=PLUGIN_FOLLOWUP_ATTACH_SECONDS,
        )
        if plugin_row is not None:
            thread_id = str(plugin_row["thread_id"] or "").strip()
            if thread_id:
                _upsert_thread(
                    conn,
                    thread_id=thread_id,
                    group_id=normalized_group_id,
                    user_id=normalized_user_id,
                    participant_ids=explicit_participants,
                    content=normalized_content,
                    timestamp=now_ts,
                )
                return ThreadAssignment(thread_id, 1.0, "plugin_episode_followup")

    thread_rows = _thread_rows(conn, normalized_group_id, now_ts)
    if not thread_rows:
        thread_id = _new_thread_id(normalized_group_id)
        _upsert_thread(
            conn,
            thread_id=thread_id,
            group_id=normalized_group_id,
            user_id=normalized_user_id,
            participant_ids=explicit_participants,
            content=normalized_content,
            timestamp=now_ts,
            is_new=True,
        )
        return ThreadAssignment(thread_id, 1.0, "new_group_thread", is_new=True)

    recent_by_thread = _recent_thread_messages(conn, normalized_group_id)
    query_embedding = embed_text(normalized_content) if len(thread_rows) > 1 else []
    best: ThreadAssignment | None = None
    for row in thread_rows:
        thread_id = str(row["thread_id"] or "")
        participants = set(_json_loads_list(row["participants"]))
        recent_rows = recent_by_thread.get(thread_id, [])[:8]
        recent_text = "\n".join(str(item["content"] or "") for item in recent_rows[:5])
        recent_speakers = {str(item["user_id"] or "") for item in recent_rows if str(item["user_id"] or "")}

        mention_match = 1.0 if participants.intersection(normalized_mentions) else 0.0
        if reply_to_user_id and str(reply_to_user_id) in participants:
            mention_match = max(mention_match, 0.8)
        semantic_similarity = cosine_similarity(query_embedding, embed_text(recent_text)) if query_embedding else 0.0
        keyword_overlap = _keyword_overlap(normalized_content, recent_text)
        speaker_continuity = 1.0 if normalized_user_id and normalized_user_id in recent_speakers else 0.0
        proximity = _time_proximity(now_ts, float(row["last_active_at"] or 0))

        score = (
            0.20 * mention_match
            + 0.20 * semantic_similarity
            + 0.10 * keyword_overlap
            + 0.10 * speaker_continuity
            + 0.05 * proximity
        )
        if best is None or score > best.score:
            best = ThreadAssignment(thread_id, score, "scored_match")

    if best and best.score >= THREAD_ATTACH_THRESHOLD:
        _upsert_thread(
            conn,
            thread_id=best.thread_id,
            group_id=normalized_group_id,
            user_id=normalized_user_id,
            participant_ids=explicit_participants,
            content=normalized_content,
            timestamp=now_ts,
        )
        return best

    thread_id = _new_thread_id(normalized_group_id)
    _upsert_thread(
        conn,
        thread_id=thread_id,
        group_id=normalized_group_id,
        user_id=normalized_user_id,
        participant_ids=explicit_participants,
        content=normalized_content,
        timestamp=now_ts,
        is_new=True,
    )
    return ThreadAssignment(thread_id, best.score if best else 0.0, "new_thread", is_new=True)


def _upsert_thread(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    group_id: str,
    user_id: str,
    participant_ids: tuple[str, ...] | list[str] = (),
    content: str,
    timestamp: float,
    is_new: bool = False,
) -> None:
    existing = conn.execute(
        "SELECT participants, topic_summary, created_at FROM conversation_threads WHERE thread_id=?",
        (str(thread_id),),
    ).fetchone()
    participants: list[str] = []
    topic_summary = str(content or "").strip()[:120]
    created_at = float(timestamp)
    if existing:
        participants = _json_loads_list(existing["participants"])
        old_summary = str(existing["topic_summary"] or "").strip()
        if old_summary:
            topic_summary = old_summary
        created_at = float(existing["created_at"] or timestamp)
    for participant_id in (user_id, *participant_ids):
        normalized_participant = str(participant_id or "").strip()
        if normalized_participant and normalized_participant not in participants:
            participants.append(normalized_participant)
    conn.execute(
        """
        INSERT INTO conversation_threads(thread_id, group_id, topic_summary, participants, created_at, last_active_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            topic_summary=excluded.topic_summary,
            participants=excluded.participants,
            last_active_at=excluded.last_active_at
        """,
        (
            str(thread_id),
            str(group_id),
            topic_summary if topic_summary else ("新对话线程" if is_new else ""),
            json.dumps(participants, ensure_ascii=False),
            created_at,
            float(timestamp),
        ),
    )


__all__ = [
    "ThreadAssignment",
    "assign_thread_for_message",
]
