from __future__ import annotations

from typing import Any

from .message_relations import (
    extract_mentioned_ids,
    extract_reply_message_id,
    extract_reply_sender_id,
)


TARGET_BOT = "TARGET_BOT"
TARGET_UNCLEAR = "TARGET_UNCLEAR"
TARGET_OTHERS = "TARGET_OTHERS"


def infer_message_target(
    event: Any,
    *,
    bot_self_id: str,
    recent_group_msgs: list[dict],
    window: int = 5,
) -> str:
    bot_self_id = str(bot_self_id or "").strip()
    if not bot_self_id:
        return TARGET_UNCLEAR

    try:
        mentioned_ids, is_at_bot = extract_mentioned_ids(
            getattr(event, "message", []) or [],
            bot_self_id=bot_self_id,
        )
        if is_at_bot:
            return TARGET_BOT
        if any(mentioned_id != bot_self_id for mentioned_id in mentioned_ids):
            return TARGET_OTHERS
    except Exception:
        pass

    reply = getattr(event, "reply", None)
    reply_sender_id = extract_reply_sender_id(reply)
    if reply_sender_id and reply_sender_id == bot_self_id:
        return TARGET_BOT

    reply_to_msg_id = extract_reply_message_id(event)
    if reply_to_msg_id:
        for msg in list(recent_group_msgs or [])[-max(1, int(window)):]:
            if not isinstance(msg, dict):
                continue
            is_bot = bool(msg.get("is_bot")) or str(msg.get("user_id", "") or "") == bot_self_id
            if not is_bot:
                continue
            if str(msg.get("message_id", "") or "") == reply_to_msg_id:
                return TARGET_BOT
        return TARGET_OTHERS

    return TARGET_UNCLEAR
