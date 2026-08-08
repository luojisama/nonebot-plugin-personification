from __future__ import annotations

from typing import Any


_NON_PERSONA_SOURCE_KINDS = frozenset({
    "bot",
    "plugin",
    "plugin_command",
    "system",
})


def _field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def source_kind_of(record: Any) -> str:
    return str(_field(record, "source_kind", "") or "").strip().lower()


def is_personification_reply_record(record: Any, bot_self_id: str = "") -> bool:
    """Return whether a persisted/quoted record is a real personification reply.

    New records have authoritative ``source_kind`` provenance.  Account-level
    fallbacks are intentionally limited to legacy rows where that field is
    absent, because other plugins can send through the same QQ account.
    """

    if record is None:
        return False
    source_kind = source_kind_of(record)
    if source_kind:
        return source_kind == "bot_reply"
    if str(_field(record, "role", "") or "").strip().lower() == "assistant":
        return True
    if bool(_field(record, "is_bot", False)):
        return True
    bot_id = str(bot_self_id or "").strip()
    user_id = str(_field(record, "user_id", "") or "").strip()
    return bool(bot_id and user_id == bot_id)


def is_external_plugin_record(record: Any) -> bool:
    return source_kind_of(record) == "plugin"


def is_human_chat_record(record: Any, bot_self_id: str = "") -> bool:
    if record is None:
        return False
    source_kind = source_kind_of(record)
    if source_kind in _NON_PERSONA_SOURCE_KINDS or source_kind == "bot_reply":
        return False
    if bool(_field(record, "is_bot", False)):
        return False
    bot_id = str(bot_self_id or "").strip()
    user_id = str(_field(record, "user_id", "") or "").strip()
    return bool(user_id and (not bot_id or user_id != bot_id))


__all__ = [
    "is_external_plugin_record",
    "is_human_chat_record",
    "is_personification_reply_record",
    "source_kind_of",
]
