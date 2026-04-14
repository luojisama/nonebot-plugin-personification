import json
from pathlib import Path
from typing import Any

from ..plugin_data import get_plugin_data_dir
from .config_registry import get_config_entries

RUNTIME_CONFIG_PATH = get_plugin_data_dir() / "runtime_config.json"


def get_runtime_config_path(plugin_config: Any) -> Path:
    _ = plugin_config
    return get_plugin_data_dir() / "runtime_config.json"


def save_plugin_runtime_config(plugin_config: Any, logger: Any, path: Path = RUNTIME_CONFIG_PATH) -> None:
    """保存运行时配置（联网、作息全局开关、主动消息开关、全局开关、语音开关）。"""
    data = {
        "web_search": plugin_config.personification_web_search,
        "web_search_always": getattr(plugin_config, "personification_web_search_always", False),
        "builtin_search": getattr(plugin_config, "personification_builtin_search", True),
        "model_builtin_search_enabled": getattr(
            plugin_config,
            "personification_model_builtin_search_enabled",
            getattr(plugin_config, "personification_builtin_search", True),
        ),
        "tool_web_search_enabled": getattr(
            plugin_config,
            "personification_tool_web_search_enabled",
            getattr(plugin_config, "personification_web_search", True),
        ),
        "tool_web_search_mode": getattr(
            plugin_config,
            "personification_tool_web_search_mode",
            "enabled",
        ),
        "schedule_global": plugin_config.personification_schedule_global,
        "proactive_enabled": plugin_config.personification_proactive_enabled,
        "group_idle_enabled": getattr(plugin_config, "personification_group_idle_enabled", False),
        "global_enabled": getattr(plugin_config, "personification_global_enabled", True),
        "tts_global_enabled": getattr(plugin_config, "personification_tts_global_enabled", True),
        "skill_sources": getattr(plugin_config, "personification_skill_sources", None),
        "skill_remote_enabled": getattr(plugin_config, "personification_skill_remote_enabled", False),
        "skill_allow_unsafe_external": getattr(plugin_config, "personification_skill_allow_unsafe_external", False),
        "skill_require_admin_review": getattr(plugin_config, "personification_skill_require_admin_review", True),
        "managed_globals": {
            entry.key: getattr(plugin_config, entry.field_name, entry.default)
            for entry in get_config_entries("global")
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"保存运行时配置失败: {e}")


def load_plugin_runtime_config(plugin_config: Any, logger: Any, path: Path = RUNTIME_CONFIG_PATH) -> None:
    """加载运行时配置并回填到插件配置对象。"""
    if not path.exists():
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"加载运行时配置失败: {e}")
        return

    plugin_config.personification_web_search = data.get(
        "web_search",
        True,
    )
    plugin_config.personification_web_search_always = data.get(
        "web_search_always",
        getattr(plugin_config, "personification_web_search_always", False),
    )
    plugin_config.personification_builtin_search = data.get(
        "builtin_search",
        getattr(plugin_config, "personification_builtin_search", True),
    )
    plugin_config.personification_model_builtin_search_enabled = data.get(
        "model_builtin_search_enabled",
        getattr(
            plugin_config,
            "personification_model_builtin_search_enabled",
            getattr(plugin_config, "personification_builtin_search", True),
        ),
    )
    plugin_config.personification_tool_web_search_enabled = data.get(
        "tool_web_search_enabled",
        getattr(
            plugin_config,
            "personification_tool_web_search_enabled",
            getattr(plugin_config, "personification_web_search", True),
        ),
    )
    plugin_config.personification_tool_web_search_mode = data.get(
        "tool_web_search_mode",
        getattr(plugin_config, "personification_tool_web_search_mode", "enabled"),
    )
    plugin_config.personification_schedule_global = data.get(
        "schedule_global",
        plugin_config.personification_schedule_global,
    )
    plugin_config.personification_proactive_enabled = data.get(
        "proactive_enabled",
        plugin_config.personification_proactive_enabled,
    )
    plugin_config.personification_group_idle_enabled = data.get(
        "group_idle_enabled",
        getattr(plugin_config, "personification_group_idle_enabled", False),
    )
    plugin_config.personification_global_enabled = data.get(
        "global_enabled",
        True,
    )
    plugin_config.personification_tts_global_enabled = data.get(
        "tts_global_enabled",
        True,
    )
    plugin_config.personification_skill_sources = data.get(
        "skill_sources",
        getattr(plugin_config, "personification_skill_sources", None),
    )
    plugin_config.personification_skill_remote_enabled = data.get(
        "skill_remote_enabled",
        getattr(plugin_config, "personification_skill_remote_enabled", False),
    )
    plugin_config.personification_skill_allow_unsafe_external = data.get(
        "skill_allow_unsafe_external",
        getattr(plugin_config, "personification_skill_allow_unsafe_external", False),
    )
    plugin_config.personification_skill_require_admin_review = data.get(
        "skill_require_admin_review",
        getattr(plugin_config, "personification_skill_require_admin_review", True),
    )
    managed_globals = data.get("managed_globals", {})
    if isinstance(managed_globals, dict):
        for entry in get_config_entries("global"):
            if entry.key not in managed_globals:
                continue
            try:
                setattr(plugin_config, entry.field_name, managed_globals[entry.key])
            except Exception as e:
                logger.error(f"加载运行时配置字段失败 {entry.key}: {e}")
