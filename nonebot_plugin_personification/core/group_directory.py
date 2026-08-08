from __future__ import annotations

import time
from typing import Any, Iterable

from .data_store import get_data_store


_NAMESPACE = "group_directory"
# WebUI 列表首先依赖 get_group_list 的实时 membership 结果。配置、白名单、
# 画像与历史目录仍会进入 union，但不应在每次只读列表加载时同步探测一批
# 已不在实时群列表中的候选；NapCat 对不存在群的 get_group_info 可能逐次等待
# 数秒，旧默认 20 会把多个页面的首载放大到约 45 秒。需要主动核验的调用方
# 仍可显式传入正数 probe_limit。
_DEFAULT_PROBE_LIMIT = 0


def normalize_group_list(data: Any) -> list[dict[str, Any]]:
    """Normalize common OneBot group-list response wrappers."""
    while isinstance(data, dict):
        wrapped = next(
            (data.get(key) for key in ("data", "groups", "group_list") if isinstance(data.get(key), (dict, list))),
            None,
        )
        if wrapped is None:
            return []
        data = wrapped
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict) and str(item.get("group_id", "") or "").strip()]


def _bot_id(bot: Any, fallback: Any = "") -> str:
    return str(getattr(bot, "self_id", "") or fallback or "unknown").strip()


def _runtime_bots(runtime: Any) -> list[tuple[str, Any]]:
    found: dict[int, tuple[str, Any]] = {}
    for holder in (getattr(runtime, "runtime_bundle", None), runtime):
        getter = getattr(holder, "get_bots", None)
        if not callable(getter):
            continue
        try:
            bots = getter() or {}
        except Exception:
            continue
        values = bots.items() if isinstance(bots, dict) else enumerate(bots)
        for fallback, bot in values:
            if bot is not None:
                found[id(bot)] = (_bot_id(bot, fallback), bot)
    return list(found.values())


async def _call(bot: Any, api: str, **kwargs: Any) -> Any:
    method = getattr(bot, api, None)
    if callable(method):
        return await method(**kwargs)
    call_api = getattr(bot, "call_api", None)
    if callable(call_api):
        return await call_api(api, **kwargs)
    raise AttributeError(api)


def _load_directory() -> dict[str, dict[str, Any]]:
    try:
        data = get_data_store().load_sync(_NAMESPACE)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def record_observed_group(
    bot_self_id: str | int,
    group_id: str | int,
    *,
    source: str,
    group_name: str = "",
    observed_at: float | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an observed group; event entry points can call this later."""
    bid = str(bot_self_id or "unknown").strip()
    gid = str(group_id or "").strip()
    if not gid:
        return {}
    now = float(observed_at or time.time())
    result: dict[str, Any] = {}

    def mutate(current: Any) -> dict[str, Any]:
        nonlocal result
        directory = current if isinstance(current, dict) else {}
        key = f"{bid}:{gid}"
        old = directory.get(key) if isinstance(directory.get(key), dict) else {}
        provenance = old.get("provenance") if isinstance(old.get("provenance"), dict) else {}
        provenance[str(source or "observed")] = now
        result = {
            **old,
            "bot_self_id": bid,
            "group_id": gid,
            "group_name": str(group_name or old.get("group_name", "") or ""),
            "provenance": provenance,
            "first_seen_at": float(old.get("first_seen_at", now) or now),
            "last_seen_at": now,
            "freshness": {"observed_at": now, "source": str(source or "observed")},
        }
        if details:
            result["details"] = dict(details)
        directory[key] = result
        return directory

    try:
        get_data_store().mutate_sync(_NAMESPACE, mutate)
    except Exception:
        mutate(_load_directory())
    return dict(result)


def _known_sources(runtime: Any) -> dict[str, set[str]]:
    from ..utils import load_group_configs, load_whitelist

    sources: dict[str, set[str]] = {}

    def add(values: Iterable[Any], source: str) -> None:
        for value in values:
            gid = str(value or "").strip()
            if gid:
                sources.setdefault(gid, set()).add(source)

    add(getattr(runtime.plugin_config, "personification_whitelist", []) or [], "config_whitelist")
    add(load_whitelist(), "dynamic_whitelist")
    configs = load_group_configs()
    add(configs.keys() if isinstance(configs, dict) else [], "group_config")
    bundle = getattr(runtime, "runtime_bundle", None)
    profile_service = getattr(bundle, "profile_service", None)
    if profile_service is not None:
        try:
            add(profile_service.list_groups(), "profile_memory")
        except Exception:
            pass
    for entry in _load_directory().values():
        if isinstance(entry, dict):
            add([entry.get("group_id")], "directory")
    return sources


async def discover_group_union(runtime: Any, *, probe_limit: int = _DEFAULT_PROBE_LIMIT) -> list[dict[str, Any]]:
    sources = _known_sources(runtime)
    rows: dict[str, dict[str, Any]] = {}
    bots = _runtime_bots(runtime)
    listed_by_bot: dict[str, set[str]] = {}

    for bot_self_id, bot in bots:
        try:
            listed = normalize_group_list(await _call(bot, "get_group_list"))
        except Exception:
            listed = []
        listed_by_bot[bot_self_id] = set()
        for raw in listed:
            gid = str(raw.get("group_id", "") or "").strip()
            listed_by_bot[bot_self_id].add(gid)
            sources.setdefault(gid, set()).add("onebot_group_list")
            entry = rows.setdefault(gid, {"group_id": gid})
            name = str(raw.get("group_name", "") or raw.get("groupName", "") or "").strip()
            if name:
                entry["group_name"] = name
            entry.update({key: raw[key] for key in ("member_count", "max_member_count") if key in raw})
            entry.setdefault("bot_self_ids", []).append(bot_self_id)
            record_observed_group(bot_self_id, gid, source="onebot_group_list", group_name=name, details=raw)

    candidates = sorted(sources)
    # Whitelists, group config, and profile memory are union-level discovery
    # sources. They do not prove that every connected bot belongs to the group.
    remaining = max(0, int(probe_limit))
    for bot_self_id, bot in bots:
        for gid in candidates:
            if remaining <= 0:
                break
            if gid in listed_by_bot.get(bot_self_id, set()) or not gid.isdigit():
                continue
            remaining -= 1
            try:
                info = await _call(bot, "get_group_info", group_id=int(gid), no_cache=True)
            except Exception:
                continue
            if not isinstance(info, dict) or not str(info.get("group_id", gid) or "").strip():
                continue
            name = str(info.get("group_name", "") or "").strip()
            sources.setdefault(gid, set()).add("onebot_group_info_probe")
            entry = rows.setdefault(gid, {"group_id": gid})
            if name:
                entry["group_name"] = name
            entry.setdefault("bot_self_ids", []).append(bot_self_id)
            record_observed_group(bot_self_id, gid, source="onebot_group_info_probe", group_name=name, details=info)

    persisted = _load_directory()
    for gid in candidates:
        entry = rows.setdefault(gid, {"group_id": gid})
        scoped = [value for value in persisted.values() if isinstance(value, dict) and str(value.get("group_id", "")) == gid]
        if not entry.get("group_name"):
            entry["group_name"] = next((str(item.get("group_name", "")) for item in scoped if item.get("group_name")), "")
        entry["sources"] = sorted(sources.get(gid, set()))
        entry["provenance"] = {str(item.get("bot_self_id", "")): item.get("provenance", {}) for item in scoped}
        entry["memberships"] = [
            {
                "bot_id": str(item.get("bot_self_id", "")),
                "group_id": gid,
                "provenance": dict(item.get("provenance", {})),
                "last_seen_at": float(item.get("last_seen_at", 0) or 0),
            }
            for item in scoped
            if str(item.get("bot_self_id", "")) not in {"", "unknown"}
        ]
        entry["freshness"] = max((float(item.get("last_seen_at", 0) or 0) for item in scoped), default=0.0)
        entry["bot_self_ids"] = sorted({item["bot_id"] for item in entry["memberships"]})
    return sorted(rows.values(), key=lambda item: str(item.get("group_id", "")))


__all__ = ["discover_group_union", "normalize_group_list", "record_observed_group"]
