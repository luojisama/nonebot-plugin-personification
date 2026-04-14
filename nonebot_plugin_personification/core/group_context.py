from __future__ import annotations

from typing import Any

from .context_policy import stringify_history_content


def render_group_context_structured(messages: list[dict[str, Any]], trigger_msg_id: str = "") -> str:
    if not messages:
        return ""

    by_message_id: dict[str, dict[str, Any]] = {}
    by_user_id: dict[str, str] = {}
    for msg in messages:
        message_id = str(msg.get("message_id", "") or "").strip()
        if message_id:
            by_message_id[message_id] = msg
        user_id = str(msg.get("user_id", "") or "").strip()
        speaker = str(
            msg.get("nickname")
            or msg.get("speaker")
            or msg.get("user_name")
            or msg.get("role")
            or "未知"
        ).strip()
        if user_id and speaker:
            by_user_id[user_id] = speaker

    def _resolve_user_name(user_id: str) -> str:
        return by_user_id.get(user_id, user_id or "未知")

    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = stringify_history_content(msg.get("content", ""))[:120]
        if not content:
            continue
        nickname = str(
            msg.get("nickname")
            or msg.get("speaker")
            or msg.get("user_name")
            or msg.get("role")
            or "未知"
        )
        user_id = str(msg.get("user_id", "") or "")
        message_id = str(msg.get("message_id", "") or "")
        label = "当前消息" if trigger_msg_id and message_id == trigger_msg_id else "群聊"
        relation_parts: list[str] = []

        reply_to_msg_id = str(msg.get("reply_to_msg_id", "") or "")
        if reply_to_msg_id:
            referenced = by_message_id.get(reply_to_msg_id)
            if referenced:
                reply_name = str(
                    referenced.get("nickname")
                    or referenced.get("speaker")
                    or referenced.get("user_id")
                    or "未知"
                )
            else:
                reply_name = _resolve_user_name(str(msg.get("reply_to_user_id", "") or ""))
            relation_parts.append(f"回复{reply_name}")
            if label != "当前消息":
                label = "被引用" if trigger_msg_id and reply_to_msg_id == trigger_msg_id else label
        elif msg.get("reply_to_user_id"):
            relation_parts.append(f"回复{_resolve_user_name(str(msg.get('reply_to_user_id', '') or ''))}")

        raw_mentions = msg.get("mentioned_ids", [])
        mentioned_ids = raw_mentions if isinstance(raw_mentions, list) else []
        if mentioned_ids:
            mention_names = [f"@{_resolve_user_name(str(uid or ''))}" for uid in mentioned_ids[:4] if str(uid or "")]
            if mention_names:
                relation_parts.append("提及" + " ".join(mention_names))

        if msg.get("is_at_bot"):
            relation_parts.append("@Bot=是")

        scene = str(msg.get("scene", "") or "").strip()
        scene_map = {
            "private": "私聊",
            "direct": "对你说",
            "observe": "群聊旁观",
            "reply": "机器人回复",
        }
        if scene in scene_map:
            relation_parts.append(f"场景={scene_map[scene]}")

        relation = "|".join(relation_parts) if relation_parts else "普通发言"
        lines.append(f"[{label}][{nickname}|uid={user_id}|{relation}] {content}")
    return "\n".join(lines)
