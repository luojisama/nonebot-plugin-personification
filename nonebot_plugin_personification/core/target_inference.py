from __future__ import annotations

from typing import Any, Iterable

from .message_relations import (
    extract_mentioned_ids,
    extract_reply_message_id,
    extract_reply_sender_id,
)
from .message_provenance import (
    is_external_plugin_record,
    is_human_chat_record,
    is_personification_reply_record,
    source_kind_of,
)


TARGET_BOT = "TARGET_BOT"
TARGET_UNCLEAR = "TARGET_UNCLEAR"
TARGET_OTHERS = "TARGET_OTHERS"
TARGET_EXTERNAL_PLUGIN = "TARGET_EXTERNAL_PLUGIN"


class MessageTargetDecision(str):
    """String-compatible target result with auditable structural evidence."""

    def __new__(
        cls,
        target: str,
        *,
        reason: str = "unclear",
        anchor_message_id: str = "",
        participants: Iterable[str] = (),
        source_kind: str = "",
    ) -> "MessageTargetDecision":
        value = str(target or TARGET_UNCLEAR)
        obj = str.__new__(cls, value)
        obj.target = value
        obj.reason = str(reason or "unclear")
        obj.anchor_message_id = str(anchor_message_id or "")
        obj.participants = tuple(
            dict.fromkeys(str(item or "").strip() for item in participants if str(item or "").strip())
        )
        obj.source_kind = str(source_kind or "").strip().lower()
        return obj

    def trace_fields(self) -> dict[str, Any]:
        return {
            "message_target": self.target,
            "message_target_reason": self.reason,
            "message_target_anchor_id": self.anchor_message_id,
            "message_target_participants": list(self.participants),
            "message_target_source_kind": self.source_kind,
        }


def _decision(target: str, **kwargs: Any) -> MessageTargetDecision:
    return MessageTargetDecision(target, **kwargs)


def normalize_message_target_for_review(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if text == TARGET_BOT or lowered == "bot":
        return "bot"
    if text == TARGET_OTHERS or lowered in {"others", "someone_else"}:
        return "others"
    if text == TARGET_EXTERNAL_PLUGIN or lowered == "external_plugin":
        return "external_plugin"
    if lowered == "broadcast":
        return "broadcast"
    return "uncertain"


def normalize_message_target_for_plan(value: Any) -> str:
    review_target = normalize_message_target_for_review(value)
    if review_target == "others":
        return "someone_else"
    return review_target


def _message_by_id(messages: list[dict], message_id: str) -> dict[str, Any] | None:
    wanted = str(message_id or "").strip()
    if not wanted:
        return None
    for message in reversed(list(messages or [])):
        if isinstance(message, dict) and str(message.get("message_id", "") or "").strip() == wanted:
            return message
    return None


def _relation_targets(message: dict[str, Any], *, bot_self_id: str) -> tuple[str, ...]:
    targets: list[str] = []
    reply_to_user_id = str(message.get("reply_to_user_id", "") or "").strip()
    if reply_to_user_id:
        targets.append(reply_to_user_id)
    raw_mentions = message.get("mentioned_ids", [])
    if isinstance(raw_mentions, list):
        targets.extend(str(item or "").strip() for item in raw_mentions)
    return tuple(
        dict.fromkeys(
            target
            for target in targets
            if target and target != str(bot_self_id or "").strip()
        )
    )


def _event_timestamp(event: Any, messages: list[dict]) -> float:
    try:
        event_time = float(getattr(event, "time", 0) or 0)
    except (TypeError, ValueError):
        event_time = 0.0
    if event_time > 0:
        return event_time
    for message in reversed(messages):
        try:
            value = float(message.get("time", message.get("timestamp", 0)) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _carried_human_dialogue(
    event: Any,
    *,
    bot_self_id: str,
    recent_group_msgs: list[dict],
    window: int = 8,
    max_age_seconds: float = 120.0,
) -> MessageTargetDecision | None:
    current_user_id = str(getattr(event, "user_id", "") or "").strip()
    if not current_user_id:
        return None
    current_message_id = str(getattr(event, "message_id", "") or "").strip()
    records = [
        message
        for message in list(recent_group_msgs or [])
        if isinstance(message, dict)
        and (not current_message_id or str(message.get("message_id", "") or "").strip() != current_message_id)
    ][-max(1, int(window)):]
    now_ts = _event_timestamp(event, records)
    if now_ts > 0:
        filtered: list[dict] = []
        for message in records:
            try:
                timestamp = float(message.get("time", message.get("timestamp", 0)) or 0)
            except (TypeError, ValueError):
                timestamp = 0.0
            if timestamp <= 0 or 0 <= now_ts - timestamp <= max_age_seconds:
                filtered.append(message)
        records = filtered

    for anchor_index in range(len(records) - 1, -1, -1):
        anchor = records[anchor_index]
        if not is_human_chat_record(anchor, bot_self_id):
            continue
        anchor_user_id = str(anchor.get("user_id", "") or "").strip()
        targets = _relation_targets(anchor, bot_self_id=bot_self_id)
        if len(targets) != 1 or not anchor_user_id:
            continue
        target_user_id = targets[0]
        if target_user_id == anchor_user_id or current_user_id not in {anchor_user_id, target_user_id}:
            continue
        pair = {anchor_user_id, target_user_id}
        valid = True
        for message in records[anchor_index + 1:]:
            if not is_human_chat_record(message, bot_self_id):
                valid = False
                break
            sender_id = str(message.get("user_id", "") or "").strip()
            if sender_id not in pair:
                valid = False
                break
            relation_targets = _relation_targets(message, bot_self_id=bot_self_id)
            if any(target not in pair for target in relation_targets):
                valid = False
                break
        if valid:
            return _decision(
                TARGET_OTHERS,
                reason="carried_human_dialogue",
                anchor_message_id=str(anchor.get("message_id", "") or ""),
                participants=(anchor_user_id, target_user_id),
                source_kind=source_kind_of(anchor),
            )
    return None


def infer_message_target(
    event: Any,
    *,
    bot_self_id: str,
    recent_group_msgs: list[dict],
    window: int = 5,
) -> MessageTargetDecision:
    bot_self_id = str(bot_self_id or "").strip()
    if not bot_self_id:
        return _decision(TARGET_UNCLEAR)

    try:
        mentioned_ids, is_at_bot = extract_mentioned_ids(
            getattr(event, "message", []) or [],
            bot_self_id=bot_self_id,
        )
        if is_at_bot:
            return _decision(
                TARGET_BOT,
                reason="explicit_persona_mention",
                participants=(str(getattr(event, "user_id", "") or ""), bot_self_id),
            )
        if any(mentioned_id != bot_self_id for mentioned_id in mentioned_ids):
            return _decision(
                TARGET_OTHERS,
                reason="explicit_human_mention",
                participants=(str(getattr(event, "user_id", "") or ""), *mentioned_ids),
            )
    except Exception:
        pass

    reply = getattr(event, "reply", None)
    reply_sender_id = extract_reply_sender_id(reply)
    reply_to_msg_id = extract_reply_message_id(event)
    if reply_to_msg_id:
        replied_record = _message_by_id(list(recent_group_msgs or [])[-max(1, int(window)):], reply_to_msg_id)
        if replied_record is not None:
            replied_user_id = str(replied_record.get("user_id", "") or "").strip()
            if is_personification_reply_record(replied_record, bot_self_id):
                return _decision(
                    TARGET_BOT,
                    reason="reply_to_personification",
                    anchor_message_id=reply_to_msg_id,
                    participants=(str(getattr(event, "user_id", "") or ""), replied_user_id),
                    source_kind=source_kind_of(replied_record),
                )
            if is_external_plugin_record(replied_record):
                return _decision(
                    TARGET_EXTERNAL_PLUGIN,
                    reason="reply_to_external_plugin",
                    anchor_message_id=reply_to_msg_id,
                    participants=(str(getattr(event, "user_id", "") or ""), replied_user_id),
                    source_kind=source_kind_of(replied_record),
                )
            return _decision(
                TARGET_OTHERS,
                reason="reply_to_human",
                anchor_message_id=reply_to_msg_id,
                participants=(str(getattr(event, "user_id", "") or ""), replied_user_id),
                source_kind=source_kind_of(replied_record),
            )
        if is_external_plugin_record(reply):
            return _decision(
                TARGET_EXTERNAL_PLUGIN,
                reason="reply_to_external_plugin",
                anchor_message_id=reply_to_msg_id,
                source_kind=source_kind_of(reply),
            )
        if reply_sender_id and reply_sender_id == bot_self_id:
            return _decision(
                TARGET_BOT,
                reason="legacy_reply_to_bot_account",
                anchor_message_id=reply_to_msg_id,
                participants=(str(getattr(event, "user_id", "") or ""), reply_sender_id),
            )
        return _decision(
            TARGET_OTHERS,
            reason="reply_to_human",
            anchor_message_id=reply_to_msg_id,
            participants=(str(getattr(event, "user_id", "") or ""), reply_sender_id),
        )

    carried = _carried_human_dialogue(
        event,
        bot_self_id=bot_self_id,
        recent_group_msgs=recent_group_msgs,
    )
    if carried is not None:
        return carried
    return _decision(TARGET_UNCLEAR)


__all__ = [
    "MessageTargetDecision",
    "TARGET_BOT",
    "TARGET_EXTERNAL_PLUGIN",
    "TARGET_OTHERS",
    "TARGET_UNCLEAR",
    "infer_message_target",
    "normalize_message_target_for_plan",
    "normalize_message_target_for_review",
]
