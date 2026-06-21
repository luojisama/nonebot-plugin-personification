import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .config_registry import get_config_entries
from .paths import get_data_dir

RUNTIME_CONFIG_PATH = Path("data/personification/runtime_config.json")
_RUNTIME_INFO_ATTR = "_personification_runtime_load_info"


def _get_plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _dedupe_dirs(dirs: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        key = str(directory.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(directory)
    return unique


def _env_files_from_dirs(dirs: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    seen_paths: set[str] = set()
    for directory in _dedupe_dirs(dirs):
        for name in (".env.prod", ".env"):
            path = directory / name
            key = str(path.resolve())
            if path.exists() and key not in seen_paths:
                paths.append(path)
                seen_paths.add(key)
    return paths


def get_runtime_config_path(plugin_config: Any) -> Path:
    return Path(get_data_dir(plugin_config)) / "runtime_config.json"


def _iter_env_file_candidates() -> list[Path]:
    cwd = Path.cwd()
    cwd_chain = [cwd] + list(cwd.parents)[:5]
    plugin_root = _get_plugin_root()
    plugin_chain = [plugin_root] + list(plugin_root.parents)[:5]
    plugin_container = plugin_root.parent if plugin_root.parent.name.lower() in {"plugin", "plugins"} else None

    def is_plugin_local_dir(directory: Path) -> bool:
        if _path_is_relative_to(directory, plugin_root):
            return True
        return plugin_container is not None and directory.resolve() == plugin_container.resolve()

    cwd_resolved = cwd.resolve()
    cwd_is_plugin_entry = cwd_resolved == plugin_root.resolve() or (
        plugin_container is not None and cwd_resolved == plugin_container.resolve()
    )
    project_dirs = [directory for directory in plugin_chain if not is_plugin_local_dir(directory)]
    cwd_dirs = []
    if not cwd_is_plugin_entry:
        cwd_dirs.append(cwd)
    cwd_dirs.extend(directory for directory in cwd_chain[1:] if not is_plugin_local_dir(directory))
    preferred_dirs = project_dirs + cwd_dirs if cwd_is_plugin_entry else cwd_dirs + project_dirs
    preferred = _env_files_from_dirs(preferred_dirs)
    if preferred:
        return preferred

    # 兼容旧版本误写在插件目录里的 .env 文件；只在找不到工程级 env 时读取。
    fallback_dirs = [directory for directory in cwd_chain + plugin_chain if is_plugin_local_dir(directory)]
    return _env_files_from_dirs(fallback_dirs)


def _parse_env_assignment(lines: list[str], start: int) -> tuple[str, str, int] | None:
    raw_line = lines[start]
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = raw_value.strip()
    if value and value[0] in {"'", '"'}:
        quote = value[0]
        body = value[1:]
        if body.endswith(quote) and not body.endswith("\\" + quote):
            return key, body[:-1], start
        collected = [body]
        index = start + 1
        while index < len(lines):
            part = lines[index]
            if part.rstrip().endswith(quote) and not part.rstrip().endswith("\\" + quote):
                collected.append(part.rstrip()[:-1])
                return key, "\n".join(collected), index
            collected.append(part)
            index += 1
        return key, "\n".join(collected), index - 1
    return key, value, start


def read_env_file_value(key: str) -> str:
    target = str(key or "").strip().lower()
    if not target:
        return ""
    for path in _iter_env_file_candidates():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        index = 0
        while index < len(lines):
            parsed = _parse_env_assignment(lines, index)
            if parsed is None:
                index += 1
                continue
            parsed_key, value, end_index = parsed
            if parsed_key.strip().lower() == target:
                return value.strip()
            index = max(index + 1, end_index + 1)
    return ""


def _collect_env_file_keys() -> set[str]:
    """直接读取 .env.prod / .env 文件，提取显式设置的 personification_ 字段名。

    不依赖 pydantic __pydantic_fields_set__（NoneBot2 通过文件加载时该 set 可能
    不包含文件来源字段），改为直接扫描 env 文件，确保 .env.prod 里的字段拥有比
    runtime_config.json 更高优先级。

    搜索范围：cwd 及其向上 5 层目录 + 插件文件位置向上 5 层目录。这样无论 bot
    通过 systemd / cd / docker 等何种方式启动，cwd 不在 bot 根目录时也能找到
    .env.prod。
    """
    found: set[str] = set()
    for path in _iter_env_file_candidates():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        index = 0
        while index < len(lines):
            parsed = _parse_env_assignment(lines, index)
            if parsed is None:
                index += 1
                continue
            key_field, _value, end_index = parsed
            key_field = key_field.strip().lower()
            if key_field.startswith("personification_"):
                found.add(key_field)
            index = max(index + 1, end_index + 1)
    return found


def _collect_explicit_env_fields(plugin_config: Any) -> set[str]:
    explicit: set[str] = set()
    # 1. pydantic __pydantic_fields_set__（对 env 变量可靠，对 .env 文件不一定）
    raw_fields = getattr(plugin_config, "__pydantic_fields_set__", None)
    if isinstance(raw_fields, Iterable):
        explicit.update(
            str(field or "").strip()
            for field in raw_fields
            if str(field or "").strip().startswith("personification_")
        )
    # 2. 操作系统环境变量
    for key in os.environ.keys():
        lowered = str(key or "").strip().lower()
        if lowered.startswith("personification_"):
            explicit.add(lowered)
    # 3. 直接读取 .env.prod / .env 文件（确保 .env.prod 里的字段不被 runtime 覆盖）
    explicit.update(_collect_env_file_keys())
    return explicit


def _collect_env_json_fields(plugin_config: Any) -> set[str]:
    """Collect fields explicitly persisted by ConfigManager/env_writer in env.json.

    Startup loads env.json before runtime_config.json. Treat fields present in
    env.json as higher-priority runtime overrides so stale managed_globals in
    runtime_config.json cannot revert WebUI changes after a restart.
    """
    path = Path(get_data_dir(plugin_config)) / "env.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()
    return {
        str(key or "").strip()
        for key in payload.keys()
        if str(key or "").strip().startswith("personification_")
    }


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
    env_json_fields = _collect_env_json_fields(plugin_config)
    protected_runtime_fields = explicit_env_fields | env_json_fields
    info = {
        "path": str(path),
        "explicit_env_fields": sorted(explicit_env_fields),
        "env_json_fields": sorted(env_json_fields),
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_global_enabled",
        value=data.get("global_enabled", True),
        runtime_key="global_enabled",
        explicit_env_fields=protected_runtime_fields,
        info=info,
    )
    _apply_runtime_value(
        plugin_config,
        field_name="personification_tts_global_enabled",
        value=data.get("tts_global_enabled", True),
        runtime_key="tts_global_enabled",
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
        explicit_env_fields=protected_runtime_fields,
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
                explicit_env_fields=protected_runtime_fields,
                info=info,
            )
    info["loaded"] = True
    _set_runtime_load_info(plugin_config, info)
    if info["skipped_runtime_keys"]:
        logger.info(
            "personification: runtime_config respected env/env.json overrides; skipped keys="
            + ", ".join(sorted(info["skipped_runtime_keys"]))
        )
    if info["errors"]:
        for error in info["errors"]:
            logger.error(f"加载运行时配置字段失败 {error}")
