import json
import os
from pathlib import Path
from typing import Any, Iterable

from .config_registry import get_config_entries
from .paths import get_data_dir

RUNTIME_CONFIG_PATH = Path("data/personification/runtime_config.json")
_RUNTIME_INFO_ATTR = "_personification_runtime_load_info"


def get_runtime_config_path(plugin_config: Any) -> Path:
    return Path(get_data_dir(plugin_config)) / "runtime_config.json"


def _collect_explicit_env_fields(plugin_config: Any) -> set[str]:
    explicit: set[str] = set()
    raw_fields = getattr(plugin_config, "__pydantic_fields_set__", None)
    if isinstance(raw_fields, Iterable):
        explicit.update(
            str(field or "").strip()
            for field in raw_fields
            if str(field or "").strip().startswith("personification_")
        )
    for key in os.environ.keys():
        lowered = str(key or "").strip().lower()
        if lowered.startswith("personification_"):
            explicit.add(lowered)
    return explicit


def _set_runtime_load_info(plugin_config: Any, info: dict[str, Any]) -> None:
    try:
        plugin_config.__dict__[_RUNTIME_INFO_ATTR] = info
    except Exception:
        try:
            object.__setattr__(plugin_config, _RUNTIME_INFO_ATTR, info)
        except Exception:
            pass


def get_runtime_load_info(plugin_config: Any) -> dict[str, Any]:
    info = getattr(plugin_config, _RUNTIME_INFO_ATTR, None)
    return dict(info) if isinstance(info, dict) else {}


def _apply_runtime_value(
    plugin_config: Any,
    *,
    field_name: str,
    value: Any,
    runtime_key: str,
    explicit_env_fields: set[str],
    info: dict[str, Any],
) -> None:
    if field_name in explicit_env_fields:
        info["skipped_runtime_keys"].append(runtime_key)
        return
    try:
        setattr(plugin_config, field_name, value)
    except Exception as e:
        info["errors"].append(f"{runtime_key}: {e}")
        return
    info["applied_runtime_keys"].append(runtime_key)


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
    explicit_env_fields = _collect_explicit_env_fields(plugin_config)
    info = {
        "path": str(path),
        "explicit_env_fields": sorted(explicit_env_fields),
        "applied_runtime_keys": [],
        "skipped_runtime_keys": [],
        "errors": [],
        "loaded": False,
    }
    if not path.exists():
        _set_runtime_load_info(plugin_config, info)
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"加载运行时配置失败: {e}")
        info["errors"].append(str(e))
        _set_runtime_load_info(plugin_config, info)
        return

    _apply_runtime_value(
        plugin_config,
        field_name="personification_web_search",
        value=data.get("web_search", True),
        runtime_key="web_search",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_web_search_always",
        value=data.get(
            "web_search_always",
            getattr(plugin_config, "personification_web_search_always", False),
        ),
        runtime_key="web_search_always",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_builtin_search",
        value=data.get(
            "builtin_search",
            getattr(plugin_config, "personification_builtin_search", True),
        ),
        runtime_key="builtin_search",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_model_builtin_search_enabled",
        value=data.get(
            "model_builtin_search_enabled",
            getattr(
                plugin_config,
                "personification_model_builtin_search_enabled",
                getattr(plugin_config, "personification_builtin_search", True),
            ),
        ),
        runtime_key="model_builtin_search_enabled",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_tool_web_search_enabled",
        value=data.get(
            "tool_web_search_enabled",
            getattr(
                plugin_config,
                "personification_tool_web_search_enabled",
                getattr(plugin_config, "personification_web_search", True),
            ),
        ),
        runtime_key="tool_web_search_enabled",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_tool_web_search_mode",
        value=data.get(
            "tool_web_search_mode",
            getattr(plugin_config, "personification_tool_web_search_mode", "enabled"),
        ),
        runtime_key="tool_web_search_mode",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_schedule_global",
        value=data.get(
            "schedule_global",
            plugin_config.personification_schedule_global,
        ),
        runtime_key="schedule_global",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_proactive_enabled",
        value=data.get(
            "proactive_enabled",
            plugin_config.personification_proactive_enabled,
        ),
        runtime_key="proactive_enabled",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_group_idle_enabled",
        value=data.get(
            "group_idle_enabled",
            getattr(plugin_config, "personification_group_idle_enabled", False),
        ),
        runtime_key="group_idle_enabled",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_global_enabled",
        value=data.get("global_enabled", True),
        runtime_key="global_enabled",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_tts_global_enabled",
        value=data.get("tts_global_enabled", True),
        runtime_key="tts_global_enabled",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_skill_sources",
        value=data.get(
            "skill_sources",
            getattr(plugin_config, "personification_skill_sources", None),
        ),
        runtime_key="skill_sources",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_skill_remote_enabled",
        value=data.get(
            "skill_remote_enabled",
            getattr(plugin_config, "personification_skill_remote_enabled", False),
        ),
        runtime_key="skill_remote_enabled",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_skill_allow_unsafe_external",
        value=data.get(
            "skill_allow_unsafe_external",
            getattr(plugin_config, "personification_skill_allow_unsafe_external", False),
        ),
        runtime_key="skill_allow_unsafe_external",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_skill_require_admin_review",
        value=data.get(
            "skill_require_admin_review",
            getattr(plugin_config, "personification_skill_require_admin_review", True),
        ),
        runtime_key="skill_require_admin_review",
        explicit_env_fields=explicit_env_fields,
        info=info,
    )
    managed_globals = data.get("managed_globals", {})
    if isinstance(managed_globals, dict):
        for entry in get_config_entries("global"):
            if entry.key not in managed_globals:
                continue
            _apply_runtime_value(
                plugin_config,
                field_name=entry.field_name,
                value=managed_globals[entry.key],
                runtime_key=entry.key,
                explicit_env_fields=explicit_env_fields,
                info=info,
            )
    info["loaded"] = True
    _set_runtime_load_info(plugin_config, info)
    if info["skipped_runtime_keys"]:
        logger.info(
            "personification: runtime_config respected explicit env overrides; skipped keys="
            + ", ".join(sorted(info["skipped_runtime_keys"]))
        )
    if info["errors"]:
        for error in info["errors"]:
            logger.error(f"加载运行时配置字段失败 {error}")
