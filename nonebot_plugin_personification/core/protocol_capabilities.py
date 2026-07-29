"""Backward-compatible helpers backed by the typed per-Bot adapter."""

from __future__ import annotations

from typing import Any

from .protocol_adapter import get_protocol_adapter, reset_protocol_adapters


def reset_capability_cache() -> None:
    reset_protocol_adapters()


async def detect_flavor(bot: Any, logger: Any = None) -> str:
    identity = await get_protocol_adapter(bot, logger=logger).identity()
    return identity.implementation


async def emoji_react(
    bot: Any,
    plugin_config: Any,
    *,
    message_id: Any,
    face_id: int,
    group_id: str = "",
    logger: Any = None,
) -> bool:
    try:
        normalized_message_id = int(message_id)
        normalized_face_id = int(face_id)
    except (TypeError, ValueError):
        return False
    result = await get_protocol_adapter(bot, plugin_config, logger).emoji_react(
        message_id=normalized_message_id,
        face_id=normalized_face_id,
        group_id=str(group_id or ""),
    )
    return result.ok


async def poke(
    bot: Any,
    plugin_config: Any,
    *,
    user_id: Any,
    group_id: str = "",
    logger: Any = None,
) -> bool:
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    result = await get_protocol_adapter(bot, plugin_config, logger).poke(
        user_id=normalized_user_id,
        group_id=str(group_id or ""),
    )
    return result.ok


async def set_typing(
    bot: Any,
    plugin_config: Any,
    *,
    user_id: Any,
    logger: Any = None,
) -> bool:
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    result = await get_protocol_adapter(bot, plugin_config, logger).set_typing(
        user_id=normalized_user_id
    )
    return result.ok


__all__ = [
    "detect_flavor",
    "emoji_react",
    "poke",
    "reset_capability_cache",
    "set_typing",
]
