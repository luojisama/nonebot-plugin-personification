"""协议端扩展能力探测与降级封装。

OneBot v11 标准之外的能力（贴表情、戳一戳、输入状态）各协议端 API 不同：

- NapCat / LLOneBot：``set_msg_emoji_like`` / ``group_poke`` / ``friend_poke`` /
  ``send_poke`` / ``set_input_status``
- Lagrange.OneBot：``set_group_reaction`` / ``group_poke`` / ``friend_poke``
- go-cqhttp：以上均不支持

封装原则：
- never-raise，全部返回 bool；
- 首次调用失败按 (self_id, api) 缓存 unsupported，之后直接跳过，不重复试错；
- ``personification_protocol_extensions`` 配置为 none 时全部禁用，
  为具体档位名时跳过自动识别。
"""

from __future__ import annotations

import time
from typing import Any

_KNOWN_FLAVORS = ("napcat", "lagrange", "llonebot", "gocq")

_flavor_cache: dict[str, str] = {}
_unsupported: dict[str, float] = {}


def reset_capability_cache() -> None:
    """测试用：清空探测缓存。"""
    _flavor_cache.clear()
    _unsupported.clear()


def _config_mode(plugin_config: Any) -> str:
    raw = getattr(plugin_config, "personification_protocol_extensions", "auto")
    return str(raw or "auto").strip().lower()


def _self_id(bot: Any) -> str:
    return str(getattr(bot, "self_id", "") or "")


async def detect_flavor(bot: Any, logger: Any = None) -> str:
    """通过标准 API get_version_info 的 app_name 识别协议端实现。"""
    self_id = _self_id(bot)
    cached = _flavor_cache.get(self_id)
    if cached:
        return cached
    flavor = "unknown"
    try:
        info = await bot.call_api("get_version_info")
        app = str((info or {}).get("app_name", "") or "").lower()
        if "napcat" in app:
            flavor = "napcat"
        elif "lagrange" in app:
            flavor = "lagrange"
        elif "llonebot" in app:
            flavor = "llonebot"
        elif "go-cqhttp" in app or "gocq" in app:
            flavor = "gocq"
    except Exception as exc:
        if logger is not None:
            logger.debug(f"[protocol_caps] get_version_info 失败，按 unknown 处理: {exc}")
    _flavor_cache[self_id] = flavor
    if logger is not None:
        logger.info(f"[protocol_caps] 协议端识别结果: {flavor or 'unknown'}")
    return flavor


async def _resolve_flavor(bot: Any, plugin_config: Any, logger: Any = None) -> str:
    mode = _config_mode(plugin_config)
    if mode in _KNOWN_FLAVORS:
        return mode
    return await detect_flavor(bot, logger)


def _is_unsupported(bot: Any, api: str) -> bool:
    return f"{_self_id(bot)}:{api}" in _unsupported


def _mark_unsupported(bot: Any, api: str) -> None:
    _unsupported[f"{_self_id(bot)}:{api}"] = time.time()


async def _try_api(bot: Any, api: str, logger: Any = None, **kwargs: Any) -> bool:
    if _is_unsupported(bot, api):
        return False
    try:
        await bot.call_api(api, **kwargs)
        return True
    except Exception as exc:
        _mark_unsupported(bot, api)
        if logger is not None:
            logger.debug(f"[protocol_caps] {api} 调用失败，标记为不支持: {exc}")
        return False


async def emoji_react(
    bot: Any,
    plugin_config: Any,
    *,
    message_id: Any,
    face_id: int,
    group_id: str = "",
    logger: Any = None,
) -> bool:
    """给指定消息贴表情；返回是否成功。"""
    if _config_mode(plugin_config) == "none":
        return False
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    flavor = await _resolve_flavor(bot, plugin_config, logger)
    if flavor == "gocq":
        return False
    if flavor == "lagrange":
        if not group_id:
            return False
        return await _try_api(
            bot,
            "set_group_reaction",
            logger=logger,
            group_id=int(group_id),
            message_id=mid,
            code=str(int(face_id)),
            is_add=True,
        )
    # napcat / llonebot / unknown
    return await _try_api(
        bot,
        "set_msg_emoji_like",
        logger=logger,
        message_id=mid,
        emoji_id=int(face_id),
        set=True,
    )


async def poke(
    bot: Any,
    plugin_config: Any,
    *,
    user_id: Any,
    group_id: str = "",
    logger: Any = None,
) -> bool:
    """戳一戳；群聊走 group_poke→send_poke，私聊走 friend_poke→send_poke。"""
    if _config_mode(plugin_config) == "none":
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    flavor = await _resolve_flavor(bot, plugin_config, logger)
    if flavor == "gocq":
        return False
    if group_id:
        gid = int(group_id)
        if await _try_api(bot, "group_poke", logger=logger, group_id=gid, user_id=uid):
            return True
        return await _try_api(bot, "send_poke", logger=logger, group_id=gid, user_id=uid)
    if await _try_api(bot, "friend_poke", logger=logger, user_id=uid):
        return True
    return await _try_api(bot, "send_poke", logger=logger, user_id=uid)


async def set_typing(
    bot: Any,
    plugin_config: Any,
    *,
    user_id: Any,
    logger: Any = None,
) -> bool:
    """私聊"正在输入"状态；目前仅 NapCat 系支持（event_type=1 正在输入）。"""
    if _config_mode(plugin_config) == "none":
        return False
    flavor = await _resolve_flavor(bot, plugin_config, logger)
    if flavor not in {"napcat", "llonebot", "unknown"}:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return await _try_api(bot, "set_input_status", logger=logger, user_id=uid, event_type=1)


__all__ = [
    "detect_flavor",
    "emoji_react",
    "poke",
    "set_typing",
    "reset_capability_cache",
]
