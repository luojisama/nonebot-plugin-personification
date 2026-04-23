from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from nonebot import get_plugin_config, logger

from .config import Config
from .core.config_registry import (
    describe_choices,
    get_config_entries,
    get_entry_label,
    read_config_value,
)
from .core.runtime_config import (
    get_runtime_config_path,
    load_plugin_runtime_config,
    save_plugin_runtime_config,
)
from .core.session_store import chat_histories
from .utils import (
    add_group_to_whitelist,
    get_group_config as load_single_group_config,
    get_recent_group_msgs,
    load_group_configs,
    load_whitelist,
    remove_group_from_whitelist,
    set_group_enabled,
    set_group_prompt,
    set_group_schedule_enabled,
    set_group_sticker_enabled,
    set_group_tts_enabled,
)

WEB_CONSOLE_API_VERSION = 1

_BOOL_TRUE = {"1", "true", "yes", "on", "开", "开启", "启用"}
_BOOL_FALSE = {"0", "false", "no", "off", "关", "关闭", "禁用"}

_RUNTIME_GLOBAL_ENTRIES = (
    {
        "key": "global_enabled",
        "field_name": "personification_global_enabled",
        "display_name": "全局拟人回复",
        "value_type": "bool",
        "default": True,
        "description": "控制插件整体回复能力的总开关。",
    },
    {
        "key": "tts_global_enabled",
        "field_name": "personification_tts_global_enabled",
        "display_name": "全局语音回复",
        "value_type": "bool",
        "default": True,
        "description": "控制语音回复能力是否允许在任意群生效。",
    },
    {
        "key": "web_search",
        "field_name": "personification_web_search",
        "display_name": "兼容联网总开关",
        "value_type": "bool",
        "default": True,
        "description": "兼容旧配置的联网总开关。",
    },
    {
        "key": "schedule_global",
        "field_name": "personification_schedule_global",
        "display_name": "全局作息模拟",
        "value_type": "bool",
        "default": False,
        "description": "是否允许作息模拟在全局运行。",
    },
    {
        "key": "proactive_enabled",
        "field_name": "personification_proactive_enabled",
        "display_name": "主动私聊",
        "value_type": "bool",
        "default": False,
        "description": "是否允许主动私聊发起话题。",
    },
    {
        "key": "group_idle_enabled",
        "field_name": "personification_group_idle_enabled",
        "display_name": "群空闲主动发话",
        "value_type": "bool",
        "default": False,
        "description": "是否允许群聊长时间安静时主动发话。",
    },
    {
        "key": "skill_remote_enabled",
        "field_name": "personification_skill_remote_enabled",
        "display_name": "远程技能加载",
        "value_type": "bool",
        "default": False,
        "description": "允许使用远程 skill 源。",
    },
    {
        "key": "skill_require_admin_review",
        "field_name": "personification_skill_require_admin_review",
        "display_name": "远程技能管理员审核",
        "value_type": "bool",
        "default": True,
        "description": "远程技能是否必须管理员审核后才能启用。",
    },
    {
        "key": "skill_allow_unsafe_external",
        "field_name": "personification_skill_allow_unsafe_external",
        "display_name": "允许不安全外部技能",
        "value_type": "bool",
        "default": False,
        "description": "是否放宽对不安全外部 skill 的限制。",
    },
)

_GROUP_EXTRA_ENTRIES = (
    {
        "key": "whitelisted",
        "field_name": "whitelisted",
        "display_name": "群白名单",
        "value_type": "bool",
        "default": False,
        "description": "控制当前群是否在拟人化白名单内。",
    },
    {
        "key": "custom_prompt",
        "field_name": "custom_prompt",
        "display_name": "自定义 Prompt",
        "value_type": "str",
        "default": "",
        "description": "为单个群追加额外人设提示词。",
    },
)


def _get_identity() -> Dict[str, str]:
    module_name = str(__package__ or __name__.split(".")[0])
    if module_name == "nonebot_plugin_personification":
        return {
            "variant": "online",
            "module_name": module_name,
            "display_name": "nonebot-plugin-shiro-personification",
        }
    return {
        "variant": "local",
        "module_name": module_name,
        "display_name": "personification",
    }


def _get_live_plugin_config() -> Any:
    return get_plugin_config(Config)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    raise ValueError("布尔值仅支持 true/false、on/off、开/关、1/0")


def _normalize_value(value: Any, value_type: str, *, choices: Optional[List[str]] = None) -> Any:
    if value_type == "bool":
        return _parse_bool(value)
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "list":
        if isinstance(value, list):
            return value
        text = str(value or "").strip()
        if not text:
            return []
        return [item.strip() for item in text.split(",") if item.strip()]

    normalized = "" if value is None else str(value)
    if choices:
        lowered = normalized.strip().lower()
        matched = next((choice for choice in choices if str(choice).lower() == lowered), None)
        if matched is None:
            raise ValueError(f"可选值: {', '.join(choices)}")
        return matched
    return normalized


def _clone_plugin_config(plugin_config: Any) -> Any:
    if plugin_config is None:
        return None
    if hasattr(plugin_config, "model_copy"):
        return plugin_config.model_copy(deep=True)
    if hasattr(plugin_config, "copy"):
        return plugin_config.copy(deep=True)
    try:
        if hasattr(plugin_config, "model_dump"):
            return type(plugin_config)(**plugin_config.model_dump())
        if hasattr(plugin_config, "dict"):
            return type(plugin_config)(**plugin_config.dict())
    except Exception:
        pass
    return plugin_config


def _runtime_config_path(plugin_config: Any) -> Optional[Path]:
    try:
        runtime_path = get_runtime_config_path(plugin_config)
    except Exception as e:
        logger.error(f"[web_console_api] 读取运行时配置路径失败: {e}")
        return None
    return runtime_path if isinstance(runtime_path, Path) else Path(runtime_path)


def _load_effective_plugin_config() -> Any:
    live_config = _get_live_plugin_config()
    runtime_config = _clone_plugin_config(live_config)
    if runtime_config is None:
        return None

    runtime_path = _runtime_config_path(runtime_config)
    try:
        if runtime_path is not None:
            load_plugin_runtime_config(runtime_config, logger, path=runtime_path)
        else:
            load_plugin_runtime_config(runtime_config, logger)
    except TypeError:
        load_plugin_runtime_config(runtime_config, logger)
    except Exception as e:
        logger.error(f"[web_console_api] 加载运行时配置失败: {e}")
    return runtime_config


def _serialize_entry(entry: Any, value: Any) -> Dict[str, Any]:
    if isinstance(entry, dict):
        choices = list(entry.get("choices", []) or [])
        value_type = str(entry.get("value_type") or "str")
        if value_type == "bool":
            choices_text = "开 / 关"
        elif choices:
            choices_text = ", ".join(choices)
        else:
            choices_text = "自由文本"
        return {
            "key": str(entry["key"]),
            "field_name": str(entry.get("field_name") or entry["key"]),
            "label": str(entry.get("display_name") or entry["key"]),
            "description": str(entry.get("description") or ""),
            "value_type": value_type,
            "default": entry.get("default"),
            "value": value,
            "choices": choices,
            "choices_text": choices_text,
            "scope": "group" if entry["key"] in {"whitelisted", "custom_prompt"} else "global",
            "category": "config",
            "admin_only": True,
            "hot_reloadable": True,
            "risk_note": "",
        }

    return {
        "key": str(entry.key),
        "field_name": str(entry.field_name),
        "label": str(get_entry_label(entry) or entry.key),
        "description": str(getattr(entry, "description", "") or ""),
        "value_type": str(getattr(entry, "value_type", "str") or "str"),
        "default": getattr(entry, "default", None),
        "value": value,
        "choices": list(getattr(entry, "choices", ()) or ()),
        "choices_text": describe_choices(entry),
        "scope": str(getattr(entry, "scope", "global") or "global"),
        "category": str(getattr(entry, "category", "config") or "config"),
        "admin_only": bool(getattr(entry, "admin_only", True)),
        "hot_reloadable": bool(getattr(entry, "hot_reloadable", True)),
        "risk_note": str(getattr(entry, "risk_note", "") or ""),
    }


def _load_global_state() -> Dict[str, Any]:
    effective_config = _load_effective_plugin_config()
    if effective_config is None:
        return {}

    state: Dict[str, Any] = {}
    for entry in _RUNTIME_GLOBAL_ENTRIES:
        state[entry["key"]] = getattr(effective_config, entry["field_name"], entry["default"])
    for entry in get_config_entries("global"):
        state[entry.key] = read_config_value(entry, plugin_config=effective_config)
    return state


def _build_global_entries() -> List[Dict[str, Any]]:
    effective_config = _load_effective_plugin_config()
    if effective_config is None:
        return []

    entries: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for entry in _RUNTIME_GLOBAL_ENTRIES:
        entries.append(
            _serialize_entry(
                entry,
                getattr(effective_config, entry["field_name"], entry["default"]),
            )
        )
        seen.add(str(entry["key"]))
    for entry in get_config_entries("global"):
        if entry.key in seen:
            continue
        entries.append(
            _serialize_entry(
                entry,
                read_config_value(entry, plugin_config=effective_config),
            )
        )
    return entries


def _build_group_entries(group_id: str) -> List[Dict[str, Any]]:
    normalized_group_id = str(group_id).replace("group_", "", 1)
    group_config = load_single_group_config(normalized_group_id)
    if not isinstance(group_config, dict):
        group_config = {}

    plugin_config = _get_live_plugin_config()
    config_whitelist = list(getattr(plugin_config, "personification_whitelist", []) or [])
    runtime_whitelist = load_whitelist()
    if not isinstance(runtime_whitelist, list):
        runtime_whitelist = []

    entries: List[Dict[str, Any]] = []
    for entry in get_config_entries("group"):
        entries.append(
            _serialize_entry(
                entry,
                read_config_value(
                    entry,
                    plugin_config=plugin_config,
                    group_config=group_config,
                ),
            )
        )
    for entry in _GROUP_EXTRA_ENTRIES:
        if entry["key"] == "whitelisted":
            value = normalized_group_id in config_whitelist or normalized_group_id in runtime_whitelist
        else:
            value = group_config.get(entry["field_name"], entry["default"])
        entries.append(_serialize_entry(entry, value))
    return entries


def _write_runtime_config(runtime_config: Any) -> None:
    runtime_path = _runtime_config_path(runtime_config)
    try:
        if runtime_path is not None:
            save_plugin_runtime_config(runtime_config, logger, path=runtime_path)
        else:
            save_plugin_runtime_config(runtime_config, logger)
    except TypeError:
        save_plugin_runtime_config(runtime_config, logger)


def _collect_group_ids(group_configs: Dict[str, dict]) -> List[str]:
    group_ids: Set[str] = set()
    for raw_group_id in group_configs.keys():
        group_ids.add(str(raw_group_id).replace("group_", "", 1))
    for session_id in chat_histories.keys():
        if isinstance(session_id, str) and session_id.startswith("group_"):
            group_ids.add(session_id.replace("group_", "", 1))
    runtime_whitelist = load_whitelist()
    if isinstance(runtime_whitelist, list):
        for group_id in runtime_whitelist:
            group_ids.add(str(group_id).replace("group_", "", 1))
    config_whitelist = list(getattr(_get_live_plugin_config(), "personification_whitelist", []) or [])
    for group_id in config_whitelist:
        group_ids.add(str(group_id).replace("group_", "", 1))
    return sorted(group_ids, key=lambda item: int(item) if str(item).isdigit() else str(item))


def get_provider_info() -> Dict[str, Any]:
    info = _get_identity()
    runtime_path = _runtime_config_path(_get_live_plugin_config())
    return {
        "api_version": WEB_CONSOLE_API_VERSION,
        **info,
        "runtime_config_path": str(runtime_path) if runtime_path is not None else None,
    }


def get_status() -> Dict[str, Any]:
    provider = get_provider_info()
    global_state = _load_global_state()
    group_configs = load_group_configs()
    if not isinstance(group_configs, dict):
        group_configs = {}

    plugin_config = _get_live_plugin_config()
    config_whitelist = list(getattr(plugin_config, "personification_whitelist", []) or [])
    runtime_whitelist = load_whitelist()
    if not isinstance(runtime_whitelist, list):
        runtime_whitelist = []

    groups: Dict[str, Dict[str, Any]] = {}
    for group_id in _collect_group_ids(group_configs):
        group_key = f"group_{group_id}"
        try:
            group_config = load_single_group_config(group_id)
            if not isinstance(group_config, dict):
                group_config = {}
            whitelisted = group_id in config_whitelist or group_id in runtime_whitelist
            enabled = bool(group_config.get("enabled")) if "enabled" in group_config else whitelisted
            session_history = chat_histories.get(group_key, [])
            recent_messages = get_recent_group_msgs(group_id)
            groups[group_key] = {
                "enabled": enabled,
                "whitelisted": whitelisted,
                "sticker_enabled": bool(group_config.get("sticker_enabled", True)),
                "schedule_enabled": bool(group_config.get("schedule_enabled", False)),
                "tts_enabled": bool(group_config.get("tts_enabled", True)),
                "proactive_enabled": bool(global_state.get("proactive_enabled", False)),
                "custom_prompt": group_config.get("custom_prompt") or "",
                "session_history_len": len(session_history) if isinstance(session_history, list) else 0,
                "recent_messages_count": len(recent_messages) if isinstance(recent_messages, list) else 0,
            }
        except Exception as e:
            logger.error(f"[web_console_api] 读取拟人群状态失败 group={group_id}: {e}")
            groups[group_key] = {
                "enabled": False,
                "whitelisted": False,
                "sticker_enabled": False,
                "schedule_enabled": False,
                "tts_enabled": False,
                "proactive_enabled": bool(global_state.get("proactive_enabled", False)),
                "custom_prompt": "",
                "session_history_len": 0,
                "recent_messages_count": 0,
                "error": str(e),
            }

    return {
        "available": True,
        "provider": provider,
        "groups": groups,
        "global": global_state,
    }


def get_global_config_entries() -> Dict[str, Any]:
    return {
        "available": True,
        "provider": get_provider_info(),
        "entries": _build_global_entries(),
    }


def update_global_config(data: dict[str, Any] | None) -> Dict[str, Any]:
    runtime_config = _load_effective_plugin_config()
    if runtime_config is None:
        return {"available": False, "error": "无法读取拟人插件运行时配置"}

    runtime_entry_map = {entry["key"]: entry for entry in _RUNTIME_GLOBAL_ENTRIES}
    registry_entry_map = {entry.key: entry for entry in get_config_entries("global")}

    try:
        for key, raw_value in (data or {}).items():
            if key in runtime_entry_map:
                entry = runtime_entry_map[key]
                normalized_value = _normalize_value(
                    raw_value,
                    str(entry.get("value_type") or "str"),
                    choices=list(entry.get("choices", []) or []),
                )
                setattr(runtime_config, entry["field_name"], normalized_value)
                continue
            if key in registry_entry_map:
                entry = registry_entry_map[key]
                normalized_value = (
                    entry.normalize_value(raw_value)
                    if hasattr(entry, "normalize_value")
                    else _normalize_value(
                        raw_value,
                        str(getattr(entry, "value_type", "str") or "str"),
                        choices=list(getattr(entry, "choices", ()) or ()),
                    )
                )
                setattr(runtime_config, entry.field_name, normalized_value)
                continue
            raise ValueError(f"不支持的拟人全局配置项: {key}")

        _write_runtime_config(runtime_config)
        return get_global_config_entries()
    except Exception as e:
        logger.error(f"[web_console_api] 更新拟人全局配置失败: {e}")
        return {"available": False, "error": str(e)}


def get_group_config(group_id: str) -> Dict[str, Any]:
    normalized_group_id = str(group_id).replace("group_", "", 1)
    return {
        "available": True,
        "provider": get_provider_info(),
        "group_id": f"group_{normalized_group_id}",
        "entries": _build_group_entries(normalized_group_id),
    }


def update_group_config(group_id: str, data: dict[str, Any] | None) -> Dict[str, Any]:
    normalized_group_id = str(group_id).replace("group_", "", 1)

    try:
        for key, raw_value in (data or {}).items():
            if key == "enabled":
                set_group_enabled(normalized_group_id, _parse_bool(raw_value))
                continue
            if key == "sticker_enabled":
                set_group_sticker_enabled(normalized_group_id, _parse_bool(raw_value))
                continue
            if key == "schedule_enabled":
                set_group_schedule_enabled(normalized_group_id, _parse_bool(raw_value))
                continue
            if key == "tts_enabled":
                set_group_tts_enabled(normalized_group_id, _parse_bool(raw_value))
                continue
            if key == "custom_prompt":
                custom_prompt = None if raw_value in [None, ""] else str(raw_value)
                set_group_prompt(normalized_group_id, custom_prompt)
                continue
            if key == "whitelisted":
                whitelist_enabled = _parse_bool(raw_value)
                if whitelist_enabled:
                    add_group_to_whitelist(normalized_group_id)
                else:
                    remove_group_from_whitelist(normalized_group_id)
                continue
            raise ValueError(f"不支持的拟人群配置项: {key}")

        return {
            "available": True,
            "provider": get_provider_info(),
            "group_id": f"group_{normalized_group_id}",
            "config": load_single_group_config(normalized_group_id),
            "entries": _build_group_entries(normalized_group_id),
        }
    except Exception as e:
        logger.error(f"[web_console_api] 更新拟人群配置失败 group={normalized_group_id}: {e}")
        return {"available": False, "error": str(e)}


def get_stats() -> Dict[str, Any]:
    return {
        "available": True,
        "provider": get_provider_info(),
        "groups": {},
        "note": "调用统计由 Web 控制台侧维护。",
    }


__all__ = [
    "WEB_CONSOLE_API_VERSION",
    "get_provider_info",
    "get_status",
    "get_global_config_entries",
    "update_global_config",
    "get_group_config",
    "update_group_config",
    "get_stats",
]
