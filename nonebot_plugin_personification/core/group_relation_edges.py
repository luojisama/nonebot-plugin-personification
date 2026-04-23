from __future__ import annotations

import math
import time
from typing import Any

from .db import connect_sync


_EDGE_WEIGHTS = {
    "reply": 2.2,
    "quote": 1.4,
    "mention": 1.0,
    "turn": 0.7,
    "repeat": 1.2,
}
_DECAY_HALF_LIFE_HOURS = 18.0


def _normalize_user_id(value: Any) -> str:
    return str(value or "").strip()


def _is_human_source(source_kind: Any, *, is_bot: bool) -> bool:
    if is_bot:
        return False
    return str(source_kind or "user").strip().lower() not in {"bot", "plugin", "system"}


def upsert_group_relation_edge(
    conn: Any,
    *,
    group_id: str,
    src_user_id: str,
    dst_user_id: str,
    edge_kind: str,
    weight: float,
    last_seen_at: float,
    sample_msg_id: str = "",
) -> None:
    src = _normalize_user_id(src_user_id)
    dst = _normalize_user_id(dst_user_id)
    if not group_id or not src or not dst or src == dst:
        return
    edge = str(edge_kind or "").strip().lower() or "other"
    conn.execute(
        """
        INSERT INTO group_relation_edges(
            group_id, src_user_id, dst_user_id, edge_kind, weight, last_seen_at, sample_msg_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id, src_user_id, dst_user_id, edge_kind)
        DO UPDATE SET
            weight = group_relation_edges.weight + excluded.weight,
            last_seen_at = MAX(group_relation_edges.last_seen_at, excluded.last_seen_at),
            sample_msg_id = CASE
                WHEN excluded.sample_msg_id != '' THEN excluded.sample_msg_id
                ELSE group_relation_edges.sample_msg_id
            END
        """,
        (
            str(group_id),
            src,
            dst,
            edge,
            float(weight),
            float(last_seen_at),
            str(sample_msg_id or ""),
        ),
    )


def update_relation_edges_from_message(
    conn: Any,
    *,
    group_id: str,
    user_id: str,
    is_bot: bool,
    source_kind: str,
    reply_to_user_id: str = "",
    reply_to_msg_id: str = "",
    mentioned_ids: list[str] | None = None,
    message_id: str = "",
    content: str = "",
    timestamp: float | None = None,
) -> None:
    src = _normalize_user_id(user_id)
    if not src or not _is_human_source(source_kind, is_bot=is_bot):
        return
    ts = float(timestamp or time.time())

    target = _normalize_user_id(reply_to_user_id)
    if target:
        upsert_group_relation_edge(
            conn,
            group_id=group_id,
            src_user_id=src,
            dst_user_id=target,
            edge_kind="reply",
            weight=_EDGE_WEIGHTS["reply"],
            last_seen_at=ts,
            sample_msg_id=message_id,
        )
    elif reply_to_msg_id:
        row = conn.execute(
            """
            SELECT user_id
            FROM group_messages
            WHERE group_id=? AND message_id=?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (str(group_id), str(reply_to_msg_id)),
        ).fetchone()
        referenced_user = _normalize_user_id(row["user_id"] if row and hasattr(row, "__getitem__") else "")
        if referenced_user:
            upsert_group_relation_edge(
                conn,
                group_id=group_id,
                src_user_id=src,
                dst_user_id=referenced_user,
                edge_kind="quote",
                weight=_EDGE_WEIGHTS["quote"],
                last_seen_at=ts,
                sample_msg_id=message_id,
            )

    for mentioned in list(mentioned_ids or [])[:4]:
        dst = _normalize_user_id(mentioned)
        if not dst:
            continue
        upsert_group_relation_edge(
            conn,
            group_id=group_id,
            src_user_id=src,
            dst_user_id=dst,
            edge_kind="mention",
            weight=_EDGE_WEIGHTS["mention"],
            last_seen_at=ts,
            sample_msg_id=message_id,
        )

    previous = conn.execute(
        """
        SELECT user_id, content, timestamp
        FROM group_messages
        WHERE group_id=? AND user_id<>? AND is_bot=0 AND source_kind NOT IN ('bot', 'plugin', 'system')
        ORDER BY timestamp DESC
        LIMIT 6
        """,
        (str(group_id), src),
    ).fetchall()
    if previous:
        last_user = _normalize_user_id(previous[0]["user_id"] if hasattr(previous[0], "__getitem__") else "")
        if last_user:
            upsert_group_relation_edge(
                conn,
                group_id=group_id,
                src_user_id=src,
                dst_user_id=last_user,
                edge_kind="turn",
                weight=_EDGE_WEIGHTS["turn"],
                last_seen_at=ts,
                sample_msg_id=message_id,
            )
        normalized_content = str(content or "").strip()
        if normalized_content:
            for row in previous[:4]:
                other_user = _normalize_user_id(row["user_id"] if hasattr(row, "__getitem__") else "")
                other_content = str(row["content"] if hasattr(row, "__getitem__") else "").strip()
                other_ts = float(row["timestamp"] if hasattr(row, "__getitem__") else 0 or 0)
                if (
                    other_user
                    and other_content
                    and other_content == normalized_content
                    and ts - other_ts <= 900
                ):
                    upsert_group_relation_edge(
                        conn,
                        group_id=group_id,
                        src_user_id=src,
                        dst_user_id=other_user,
                        edge_kind="repeat",
                        weight=_EDGE_WEIGHTS["repeat"],
                        last_seen_at=ts,
                        sample_msg_id=message_id,
                    )
                    upsert_group_relation_edge(
                        conn,
                        group_id=group_id,
                        src_user_id=other_user,
                        dst_user_id=src,
                        edge_kind="repeat",
                        weight=_EDGE_WEIGHTS["repeat"],
                        last_seen_at=ts,
                        sample_msg_id=message_id,
                    )


def _decayed_weight(weight: float, last_seen_at: float, *, now_ts: float) -> float:
    age_seconds = max(0.0, now_ts - float(last_seen_at or 0))
    if age_seconds <= 0:
        return float(weight)
    decay_factor = 0.5 ** (age_seconds / (_DECAY_HALF_LIFE_HOURS * 3600.0))
    return float(weight) * decay_factor


def summarize_relation_edges(
    group_id: str,
    *,
    trigger_user_id: str = "",
    bot_self_id: str = "",
    limit: int = 3,
) -> str:
    if not str(group_id or "").strip():
        return ""
    now_ts = time.time()
    with connect_sync() as conn:
        rows = conn.execute(
            """
            SELECT src_user_id, dst_user_id, edge_kind, weight, last_seen_at
            FROM group_relation_edges
            WHERE group_id=?
            ORDER BY last_seen_at DESC
            LIMIT 200
            """,
            (str(group_id),),
        ).fetchall()
        if not rows:
            return ""

        names: dict[str, str] = {}
        name_rows = conn.execute(
            """
            SELECT user_id, nickname
            FROM group_messages
            WHERE group_id=? AND nickname<>''
            ORDER BY timestamp DESC
            LIMIT 200
            """,
            (str(group_id),),
        ).fetchall()
        for row in name_rows:
            user_id = _normalize_user_id(row["user_id"] if hasattr(row, "__getitem__") else "")
            nickname = str(row["nickname"] if hasattr(row, "__getitem__") else "").strip()
            if user_id and nickname and user_id not in names:
                names[user_id] = nickname

    bot_id = _normalize_user_id(bot_self_id)
    strongest: list[tuple[float, str, str, str]] = []
    trigger_targets: dict[str, float] = {}
    for row in rows:
        src = _normalize_user_id(row["src_user_id"] if hasattr(row, "__getitem__") else "")
        dst = _normalize_user_id(row["dst_user_id"] if hasattr(row, "__getitem__") else "")
        if not src or not dst:
            continue
        if bot_id and (src == bot_id or dst == bot_id):
            continue
        decayed = _decayed_weight(
            float(row["weight"] if hasattr(row, "__getitem__") else 0.0),
            float(row["last_seen_at"] if hasattr(row, "__getitem__") else 0.0),
            now_ts=now_ts,
        )
        if decayed <= 0.15:
            continue
        strongest.append((decayed, src, dst, str(row["edge_kind"] if hasattr(row, "__getitem__") else "other")))
        if src == _normalize_user_id(trigger_user_id):
            trigger_targets[dst] = trigger_targets.get(dst, 0.0) + decayed

    if not strongest:
        return ""

    strongest.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))

    def _name(user_id: str) -> str:
        return names.get(user_id, user_id or "未知")

    lines = ["## 近时段关系边摘要"]
    lines.append(
        "- 最近活跃承接："
        + "；".join(
            f"{_name(src)} -> {_name(dst)} ({kind}, {score:.1f})"
            for score, src, dst, kind in strongest[: max(1, int(limit))]
        )
    )
    if trigger_targets:
        top_targets = sorted(trigger_targets.items(), key=lambda item: (-item[1], item[0]))[:3]
        lines.append(
            "- 当前说话人近期主要在接："
            + "；".join(f"{_name(dst)}({score:.1f})" for dst, score in top_targets)
        )
    return "\n".join(lines)


__all__ = [
    "summarize_relation_edges",
    "update_relation_edges_from_message",
    "upsert_group_relation_edge",
]
