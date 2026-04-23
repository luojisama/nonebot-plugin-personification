from __future__ import annotations

from collections import defaultdict
from typing import Any

from .context_policy import stringify_history_content
from .group_relation_edges import summarize_relation_edges


def summarize_group_relationships(
    messages: list[dict[str, Any]],
    *,
    trigger_msg_id: str = "",
    trigger_user_id: str = "",
    bot_self_id: str = "",
) -> str:
    if not messages:
        return ""

    speaker_names: dict[str, str] = {}
    message_by_id: dict[str, dict[str, Any]] = {}
    activity_counts: dict[str, int] = defaultdict(int)
    directed_edges: dict[tuple[str, str], int] = defaultdict(int)
    speaker_recent_targets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    turn_pairs: dict[tuple[str, str], int] = defaultdict(int)

    def _is_human_message(msg: dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        if bool(msg.get("is_bot")):
            return False
        source_kind = str(msg.get("source_kind", "") or "").strip().lower()
        return source_kind not in {"bot", "plugin", "system"}

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        user_id = str(msg.get("user_id", "") or "").strip()
        speaker = str(
            msg.get("nickname")
            or msg.get("speaker")
            or msg.get("user_name")
            or msg.get("role")
            or user_id
            or "未知"
        ).strip()
        message_id = str(msg.get("message_id", "") or "").strip()
        if user_id:
            speaker_names[user_id] = speaker
            if user_id != str(bot_self_id or "").strip() and _is_human_message(msg):
                activity_counts[user_id] += 1
        if message_id:
            message_by_id[message_id] = msg

    def _resolve_name(user_id: str) -> str:
        return speaker_names.get(user_id, user_id or "未知")

    for msg in messages:
        if not isinstance(msg, dict) or not _is_human_message(msg):
            continue
        src = str(msg.get("user_id", "") or "").strip()
        if not src or src == str(bot_self_id or "").strip():
            continue
        reply_to_user = str(msg.get("reply_to_user_id", "") or "").strip()
        if not reply_to_user:
            reply_to_msg = str(msg.get("reply_to_msg_id", "") or "").strip()
            if reply_to_msg:
                referenced = message_by_id.get(reply_to_msg)
                if isinstance(referenced, dict):
                    reply_to_user = str(referenced.get("user_id", "") or "").strip()
        if reply_to_user and reply_to_user != src:
            directed_edges[(src, reply_to_user)] += 2
            speaker_recent_targets[src][reply_to_user] += 2
        for uid in list(msg.get("mentioned_ids") or [])[:4]:
            target = str(uid or "").strip()
            if target and target != src and target != str(bot_self_id or "").strip():
                directed_edges[(src, target)] += 1
                speaker_recent_targets[src][target] += 1

    previous_human: dict[str, Any] | None = None
    for msg in messages:
        if not isinstance(msg, dict) or not _is_human_message(msg):
            continue
        current_uid = str(msg.get("user_id", "") or "").strip()
        if not current_uid or current_uid == str(bot_self_id or "").strip():
            continue
        if previous_human is not None:
            previous_uid = str(previous_human.get("user_id", "") or "").strip()
            if previous_uid and previous_uid != current_uid:
                pair = tuple(sorted((previous_uid, current_uid)))
                turn_pairs[pair] += 1
        previous_human = msg

    trigger_msg = message_by_id.get(str(trigger_msg_id or "").strip()) if trigger_msg_id else None
    current_target_parts: list[str] = []
    if isinstance(trigger_msg, dict):
        reply_to_user = str(trigger_msg.get("reply_to_user_id", "") or "").strip()
        if reply_to_user:
            current_target_parts.append(f"当前更像在接 {_resolve_name(reply_to_user)} 的话")
        mentioned_ids = [str(uid or "").strip() for uid in list(trigger_msg.get("mentioned_ids") or []) if str(uid or "").strip()]
        if mentioned_ids:
            current_target_parts.append(
                "当前提及 " + " / ".join(_resolve_name(uid) for uid in mentioned_ids[:3])
            )

    strongest_edges = sorted(
        directed_edges.items(),
        key=lambda item: (-item[1], _resolve_name(item[0][0]), _resolve_name(item[0][1])),
    )[:3]
    active_speakers = sorted(
        activity_counts.items(),
        key=lambda item: (-item[1], _resolve_name(item[0])),
    )[:4]

    lines: list[str] = ["## 最近群聊互动关系"]
    if active_speakers:
        lines.append(
            "- 最近活跃："
            + "；".join(f"{_resolve_name(uid)}({count})" for uid, count in active_speakers)
        )
    if strongest_edges:
        lines.append(
            "- 主要承接："
            + "；".join(
                f"{_resolve_name(src)} -> {_resolve_name(dst)} x{weight}"
                for (src, dst), weight in strongest_edges
            )
        )
    strongest_turn_pairs = sorted(
        turn_pairs.items(),
        key=lambda item: (-item[1], _resolve_name(item[0][0]), _resolve_name(item[0][1])),
    )[:2]
    if strongest_turn_pairs:
        lines.append(
            "- 最近来回最密："
            + "；".join(
                f"{_resolve_name(left)} <-> {_resolve_name(right)} x{weight}"
                for (left, right), weight in strongest_turn_pairs
            )
        )
    if current_target_parts:
        lines.append("- " + "；".join(current_target_parts))
    if trigger_user_id:
        related_targets = sorted(
            speaker_recent_targets.get(str(trigger_user_id or "").strip(), {}).items(),
            key=lambda item: (-item[1], _resolve_name(item[0])),
        )[:3]
        if related_targets:
            lines.append(
                "- 说话人最近主要在接："
                + "；".join(f"{_resolve_name(dst)}({weight})" for dst, weight in related_targets)
            )
    if len(lines) <= 1:
        persistent = summarize_relation_edges(
            messages[-1].get("group_id", "") if messages else "",
            trigger_user_id=trigger_user_id,
            bot_self_id=bot_self_id,
        )
        return persistent or ""
    persistent = summarize_relation_edges(
        messages[-1].get("group_id", "") if messages else "",
        trigger_user_id=trigger_user_id,
        bot_self_id=bot_self_id,
    )
    if persistent:
        lines.append(persistent)
    lines.append("理解规则：先判断谁在接谁、谁在 cue 谁，再决定要不要插话；不要把插件播报误当成真人态度。")
    return "\n".join(lines)


__all__ = ["summarize_group_relationships"]
