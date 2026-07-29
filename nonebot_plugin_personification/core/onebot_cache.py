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
from typing import Any, Iterable

from .user_profile_meta import build_user_profile_meta

_DEFAULT_TTL_SECONDS = 1800  # 30 分钟
_FAILURE_TTL_SECONDS = 15
_GROUP_MEMBER_SUCCESS_TTL_SECONDS = 5 * 60
_MAX_ENTRIES = 500

_user_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_user_profile_cache: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()
_group_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_group_member_cache: "OrderedDict[str, tuple[float, tuple[bool, dict[str, Any]]]]" = OrderedDict()
_user_lock = asyncio.Lock()
_group_lock = asyncio.Lock()
_group_member_lock = asyncio.Lock()
_user_profile_inflight: dict[str, asyncio.Task[tuple[dict[str, Any], bool]]] = {}
_group_inflight: dict[str, asyncio.Task[tuple[str, bool]]] = {}
_group_member_inflight: dict[str, asyncio.Task[tuple[dict[str, Any], bool]]] = {}


def _scoped_key(bot: Any, value: str) -> str:
    return f"{str(getattr(bot, 'self_id', '') or 'unknown')}:{value}"


def _group_member_scoped_key(bot: Any, group_id: str, user_id: str) -> str:
    return _scoped_key(bot, f"{group_id}:{user_id}")


def _evict_if_needed(cache: "OrderedDict[str, tuple[float, Any]]") -> None:
    while len(cache) > _MAX_ENTRIES:
        cache.popitem(last=False)


def _get_cached(cache: "OrderedDict[str, tuple[float, Any]]", key: str) -> Any | None:
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
    cache: "OrderedDict[str, tuple[float, Any]]",
    key: str,
    value: Any,
    ttl: int,
) -> None:
    cache[key] = (time.time() + ttl, value)
    cache.move_to_end(key)
    _evict_if_needed(cache)


async def _call_onebot_api(bot: Any, api: str, **kwargs: Any) -> Any:
    """兼容适配器方法和通用 call_api 两种调用形态。"""
    method = getattr(bot, api, None)
    last_exc: Exception | None = None
    if callable(method):
        try:
            return await method(**kwargs)
        except Exception as exc:
            last_exc = exc
    call_api = getattr(bot, "call_api", None)
    if callable(call_api):
        try:
            return await call_api(api, **kwargs)
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise AttributeError(api)


def _extract_group_names(data: Any) -> dict[str, str]:
    if isinstance(data, dict):
        for key in ("groups", "data", "group_list"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return {}
    names: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id", "") or "").strip()
        group_name = str(item.get("group_name", "") or item.get("groupName", "") or "").strip()
        if group_id and group_name:
            names[group_id] = group_name
    return names


def _numeric_onebot_id(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _refresh_group_names_from_list(bot: Any, *, ttl: int) -> dict[str, str]:
    if bot is None:
        return {}
    try:
        names = _extract_group_names(await _call_onebot_api(bot, "get_group_list"))
    except Exception:
        names = {}
    if not names:
        return {}
    async with _group_lock:
        for group_id, group_name in names.items():
            _set_cached(_group_cache, _scoped_key(bot, group_id), group_name, ttl)
    return names


async def get_user_nickname(
    bot: Any,
    user_id: str | int,
    *,
    ttl: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """按 user_id 取 QQ 昵称，命中缓存则不调 bot；任何失败返回空串。"""
    user_key = str(user_id).strip()
    if not user_key:
        return ""
    key = _scoped_key(bot, user_key)
    async with _user_lock:
        cached = _get_cached(_user_cache, key)
        if cached is not None:
            return cached
    profile = await get_user_profile(bot, user_key, ttl=ttl)
    nickname = str(profile.get("nickname", "") or "")
    async with _user_lock:
        _set_cached(_user_cache, key, nickname, ttl)
    return nickname


async def get_user_profile(
    bot: Any,
    user_id: str | int,
    *,
    ttl: int = _DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Return normalized QQ profile fields for prompt/WebUI use.

    Standard OneBot only guarantees nickname/sex/age; adapters may add qid,
    signature, levels, or other account fields. Missing/failed fields degrade to
    deterministic avatar/homepage URLs and do not raise.
    """
    user_key = str(user_id).strip()
    if not user_key:
        return {}
    key = _scoped_key(bot, user_key)
    async with _user_lock:
        cached = _get_cached(_user_profile_cache, key)
        if cached is not None:
            return dict(cached)
    async def fetch() -> tuple[dict[str, Any], bool]:
        raw: dict[str, Any] = {}
        success = bot is None or _numeric_onebot_id(user_key) is None
        numeric_id = _numeric_onebot_id(user_key)
        if bot is not None and numeric_id is not None:
            try:
                info = await _call_onebot_api(bot, "get_stranger_info", user_id=numeric_id)
                if isinstance(info, dict):
                    raw, success = dict(info), True
            except Exception:
                pass
        return build_user_profile_meta(user_key, stranger_info=raw, source="onebot_cache"), success

    async with _user_lock:
        task = _user_profile_inflight.get(key)
        if task is None:
            task = asyncio.create_task(fetch())
            _user_profile_inflight[key] = task
    try:
        profile, success = await task
    finally:
        async with _user_lock:
            if _user_profile_inflight.get(key) is task:
                _user_profile_inflight.pop(key, None)
    async with _user_lock:
        _set_cached(_user_profile_cache, key, dict(profile), ttl if success else min(ttl, _FAILURE_TTL_SECONDS))
    return dict(profile)


async def get_group_name(
    bot: Any,
    group_id: str | int,
    *,
    ttl: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """按 group_id 取群名，命中缓存则不调 bot；任何失败返回空串。"""
    group_key = str(group_id).strip()
    if not group_key:
        return ""
    key = _scoped_key(bot, group_key)
    async with _group_lock:
        cached = _get_cached(_group_cache, key)
        if cached is not None:
            return cached
    if bot is None:
        return ""
    async def fetch() -> tuple[str, bool]:
        try:
            info = await _call_onebot_api(bot, "get_group_info", group_id=int(group_key))
            if isinstance(info, dict):
                return str(info.get("group_name", "") or ""), True
        except Exception:
            pass
        return "", False

    async with _group_lock:
        task = _group_inflight.get(key)
        if task is None:
            task = asyncio.create_task(fetch())
            _group_inflight[key] = task
    try:
        group_name, success = await task
    finally:
        async with _group_lock:
            if _group_inflight.get(key) is task:
                _group_inflight.pop(key, None)
    async with _group_lock:
        _set_cached(_group_cache, key, group_name, ttl if success else min(ttl, _FAILURE_TTL_SECONDS))
    return group_name


async def get_group_name_map(
    bot: Any,
    group_ids: Iterable[str | int] | None = None,
    *,
    ttl: int = _DEFAULT_TTL_SECONDS,
) -> dict[str, str]:
    """批量解析群名，优先使用 get_group_list，再按需回退 get_group_info。

    传入 group_ids 时只保证返回这些 id 的键；缓存中的空串表示历史查询失败，
    批量路径会主动用群列表刷新一次，避免短期失败把 WebUI 固定成无名群。
    """
    keys: list[str] = []
    seen: set[str] = set()
    if group_ids is not None:
        for raw in group_ids:
            key = str(raw).strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)

    result: dict[str, str] = {}
    needs_refresh: list[str] = []
    async with _group_lock:
        for key in keys:
            cached = _get_cached(_group_cache, _scoped_key(bot, key))
            if cached:
                result[key] = cached
            else:
                needs_refresh.append(key)

    if bot is None:
        return {key: result.get(key, "") for key in keys} if keys else {}

    if needs_refresh or not keys:
        listed = await _refresh_group_names_from_list(bot, ttl=ttl)
        for key in needs_refresh:
            if listed.get(key):
                result[key] = listed[key]
        if not keys:
            result.update(listed)

    if keys:
        for key in keys:
            if key in result:
                continue
            result[key] = await get_group_name(bot, key, ttl=ttl)
    return result


async def get_group_member_info(
    bot: Any,
    group_id: str | int,
    user_id: str | int,
) -> dict[str, Any] | None:
    """Return current bot/group scoped membership proof, or ``None``.

    Successful proofs live for five minutes. Failures live for fifteen seconds
    and concurrent misses share one adapter/call_api request.
    """
    group_key = str(group_id or "").strip()
    user_key = str(user_id or "").strip()
    numeric_group = _numeric_onebot_id(group_key)
    numeric_user = _numeric_onebot_id(user_key)
    if bot is None or numeric_group is None or numeric_user is None:
        return None
    key = _group_member_scoped_key(bot, group_key, user_key)
    async with _group_member_lock:
        cached = _get_cached(_group_member_cache, key)
        if cached is not None:
            success, info = cached
            return dict(info) if success else None

    async def fetch() -> tuple[dict[str, Any], bool]:
        try:
            raw = await _call_onebot_api(
                bot,
                "get_group_member_info",
                group_id=numeric_group,
                user_id=numeric_user,
            )
        except Exception:
            return {}, False
        if not isinstance(raw, dict) or not raw:
            return {}, False
        returned_group = str(raw.get("group_id", "") or "").strip()
        returned_user = str(raw.get("user_id", "") or "").strip()
        if returned_group and returned_group != group_key:
            return {}, False
        if returned_user and returned_user != user_key:
            return {}, False
        return dict(raw), True

    async with _group_member_lock:
        task = _group_member_inflight.get(key)
        if task is None:
            task = asyncio.create_task(fetch())
            _group_member_inflight[key] = task
    try:
        info, success = await asyncio.shield(task)
    finally:
        async with _group_member_lock:
            if _group_member_inflight.get(key) is task:
                _group_member_inflight.pop(key, None)
    async with _group_member_lock:
        _set_cached(
            _group_member_cache,
            key,
            (success, dict(info)),
            _GROUP_MEMBER_SUCCESS_TTL_SECONDS if success else _FAILURE_TTL_SECONDS,
        )
    return dict(info) if success else None


def _clear_caches_for_testing() -> None:
    """仅供测试使用，清空两层缓存。"""
    _user_cache.clear()
    _user_profile_cache.clear()
    _group_cache.clear()
    _group_member_cache.clear()
    _user_profile_inflight.clear()
    _group_inflight.clear()
    _group_member_inflight.clear()


__all__ = [
    "get_group_member_info",
    "get_group_name",
    "get_group_name_map",
    "get_user_nickname",
    "get_user_profile",
]
