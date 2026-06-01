"""OneBot 实例信息 TTL + LRU 缓存。

为 WebUI 与其他模块提供 `bot.get_stranger_info` / `bot.get_group_info` 的
带 TTL 与 LRU 限容的内存缓存。失败一律降级为空串，不向上抛异常。

设计要点：
- 进程内单例 dict，重启失效（昵称变动不频繁，TTL 30 分钟）
- LRU 上限 500 条，超出从最旧的开始淘汰
- 缓存与远程查询都用 asyncio.Lock 串行化，避免并发请求重复打 OneBot
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

_DEFAULT_TTL_SECONDS = 1800  # 30 分钟
_MAX_ENTRIES = 500

_user_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_group_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_user_lock = asyncio.Lock()
_group_lock = asyncio.Lock()


def _evict_if_needed(cache: "OrderedDict[str, tuple[float, str]]") -> None:
    while len(cache) > _MAX_ENTRIES:
        cache.popitem(last=False)


def _get_cached(cache: "OrderedDict[str, tuple[float, str]]", key: str) -> str | None:
    item = cache.get(key)
    if item is None:
        return None
    expires_at, value = item
    if time.time() >= expires_at:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return value


def _set_cached(
    cache: "OrderedDict[str, tuple[float, str]]",
    key: str,
    value: str,
    ttl: int,
) -> None:
    cache[key] = (time.time() + ttl, value)
    cache.move_to_end(key)
    _evict_if_needed(cache)


async def get_user_nickname(
    bot: Any,
    user_id: str | int,
    *,
    ttl: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """按 user_id 取 QQ 昵称，命中缓存则不调 bot；任何失败返回空串。"""
    key = str(user_id).strip()
    if not key:
        return ""
    async with _user_lock:
        cached = _get_cached(_user_cache, key)
        if cached is not None:
            return cached
    if bot is None:
        return ""
    nickname = ""
    try:
        info = await bot.get_stranger_info(user_id=int(key))
        if isinstance(info, dict):
            nickname = str(info.get("nickname", "") or "")
    except Exception:
        nickname = ""
    async with _user_lock:
        _set_cached(_user_cache, key, nickname, ttl)
    return nickname


async def get_group_name(
    bot: Any,
    group_id: str | int,
    *,
    ttl: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """按 group_id 取群名，命中缓存则不调 bot；任何失败返回空串。"""
    key = str(group_id).strip()
    if not key:
        return ""
    async with _group_lock:
        cached = _get_cached(_group_cache, key)
        if cached is not None:
            return cached
    if bot is None:
        return ""
    group_name = ""
    try:
        info = await bot.get_group_info(group_id=int(key))
        if isinstance(info, dict):
            group_name = str(info.get("group_name", "") or "")
    except Exception:
        group_name = ""
    async with _group_lock:
        _set_cached(_group_cache, key, group_name, ttl)
    return group_name


def _clear_caches_for_testing() -> None:
    """仅供测试使用，清空两层缓存。"""
    _user_cache.clear()
    _group_cache.clear()


__all__ = ["get_user_nickname", "get_group_name"]
