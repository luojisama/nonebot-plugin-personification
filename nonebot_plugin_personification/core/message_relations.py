from __future__ import annotations

from typing import Any


def extract_reply_sender_id(reply: Any) -> str:
    sender = getattr(reply, "sender", None)
    if sender is None and isinstance(reply, dict):
        sender = reply.get("sender")
    if isinstance(sender, dict):
        return str(sender.get("user_id", "") or "").strip()
    if sender is not None:
        return str(getattr(sender, "user_id", "") or "").strip()
    return ""


def extract_reply_message_id(event: Any) -> str:
    reply = getattr(event, "reply", None)
    if reply is not None:
        value = getattr(reply, "message_id", None)
        if value is None and isinstance(reply, dict):
            value = reply.get("message_id")
        return str(value or "").strip()
    return str(getattr(event, "reply_to_message_id", "") or "").strip()


def extract_event_message_id(event: Any) -> str:
    return str(getattr(event, "message_id", "") or "").strip()


def extract_mentioned_ids(message: Any, *, bot_self_id: str = "") -> tuple[list[str], bool]:
    mentioned_ids: list[str] = []
    is_at_bot = False
    for seg in message or []:
        seg_type = getattr(seg, "type", None)
        seg_data = getattr(seg, "data", None)
        if seg_type is None and isinstance(seg, dict):
            seg_type = seg.get("type")
            seg_data = seg.get("data")
        if seg_type != "at":
            continue
        qq = str((seg_data or {}).get("qq", "")).strip()
        if not qq or qq == "all":
            continue
        mentioned_ids.append(qq)
        if bot_self_id and qq == bot_self_id:
            is_at_bot = True
    return mentioned_ids, is_at_bot


def build_event_relation_metadata(
    event: Any,
    *,
    bot_self_id: str = "",
    source_kind: str = "user",
) -> dict[str, Any]:
    reply = getattr(event, "reply", None)
    mentioned_ids, is_at_bot = extract_mentioned_ids(
        getattr(event, "message", []) or [],
        bot_self_id=bot_self_id,
    )
    return {
        "message_id": extract_event_message_id(event) or None,
        "reply_to_msg_id": extract_reply_message_id(event) or None,
        "reply_to_user_id": extract_reply_sender_id(reply) or None,
        "mentioned_ids": mentioned_ids,
        "is_at_bot": is_at_bot,
        "source_kind": source_kind,
    }


def extract_send_message_id(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, (str, int)):
        return str(result).strip()
    if isinstance(result, list):
        for item in result:
            value = extract_send_message_id(item)
            if value:
                return value
        return ""
    if isinstance(result, dict):
        for key in ("message_id", "msg_id", "id", "messageId"):
            value = result.get(key)
            if value:
                return str(value).strip()
        data = result.get("data")
        if isinstance(data, dict):
            return extract_send_message_id(data)
        return ""
    for key in ("message_id", "msg_id", "id", "messageId"):
        value = getattr(result, key, None)
        if value:
            return str(value).strip()
    data = getattr(result, "data", None)
    if data is not None:
        return extract_send_message_id(data)
    return ""
