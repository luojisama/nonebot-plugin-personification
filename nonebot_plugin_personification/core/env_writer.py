from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key

from .config_manager import (
    _restrict_sensitive_file_permissions,
    _write_payload_atomic,
    get_env_config_path,
)
from .runtime_config import (
    _iter_env_file_candidates,
    get_runtime_config_path,
    read_env_file_value,
)


def _file_has_key(path: Path, field_name: str) -> bool:
    target = str(field_name or "").strip().lower()
    if not target:
        return False
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip().lower()
            if key == target:
                return True
    except Exception:
        return False
    return False


def _resolve_dotenv_target(field_name: str = "") -> Path | None:
    """选择写入目标 .env 文件。

    优先选已包含该字段的文件（就地更新，避免在另一个文件里产生重复副本，
    导致 read_env_file_value 读到旧值），否则退回第一个存在的候选文件。
    """
    candidates = list(_iter_env_file_candidates())
    if field_name:
        for path in candidates:
            if _file_has_key(path, field_name):
                return path
    for path in candidates:
        return path
    return None


def _serialize_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def write_dotenv(field_name: str, value: Any, *, backup: bool = True, target: Path | None = None) -> Path | None:
    """写入 .env.prod / .env，保留注释顺序；写前备份。
    若新值与文件中现有值完全一致，则跳过备份与写入，避免产生无意义的 .bak 副本。
    返回实际写入（或确认未变）的文件路径，无可写文件时返回 None。
    """
    path = target or _resolve_dotenv_target(field_name)
    if path is None:
        return None
    new_value_str = _serialize_value(value)
    if path.exists():
        try:
            existing = dotenv_values(str(path))
        except Exception:
            existing = None
        if isinstance(existing, dict) and existing.get(field_name) == new_value_str:
            return path
    if backup and path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            shutil.copy2(path, backup_path)
            _restrict_sensitive_file_permissions(backup_path)
        except Exception:
            pass
    set_key(str(path), field_name, new_value_str, quote_mode="auto")
    _restrict_sensitive_file_permissions(path)
    return path


def write_env_json(field_name: str, value: Any, plugin_config: Any) -> Path:
    """写入插件 env.json 覆盖层。"""
    path = get_env_config_path(plugin_config)
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                payload = data
        except Exception:
            payload = {}
    payload[field_name] = value
    _write_payload_atomic(path, payload)
    return path


def write_both(field_name: str, value: Any, plugin_config: Any) -> dict[str, Any]:
    """持久化 WebUI 配置。

    保留历史函数名供调用方兼容，但实际只写插件自己的 env.json。
    .env.prod / .env 只作为首次启用时的导入来源，不再由 WebUI 改写，
    避免产生 .bak 文件并避免重启后旧 env 值把配置拉回去。
    """
    result: dict[str, Any] = {
        "field_name": field_name,
        "value": value,
        "dotenv_path": None,
        "env_json_path": None,
        "errors": [],
    }
    # env.json：权威运行时层，始终写
    try:
        json_path = write_env_json(field_name, value, plugin_config)
        result["env_json_path"] = str(json_path)
    except Exception as exc:
        result["errors"].append(f"env.json write failed: {exc}")
    return result


def resolve_value_sources(field_name: str, plugin_config: Any) -> dict[str, Any]:
    """返回字段在 .env / env.json / runtime_config / 默认值 各层的快照。

    供 WebUI 显示来源快照并辅助管理员决策。插件字段优先级：
    env.json > 首次导入用 .env > runtime_config > 默认值。
    """
    sources: dict[str, Any] = {
        "field_name": field_name,
        "env_file": None,
        "env_json": None,
        "runtime_config": None,
        "default": None,
        "current": getattr(plugin_config, field_name, None),
        "active_source": "default",
    }
    env_json_path = get_env_config_path(plugin_config)
    if env_json_path.exists():
        try:
            data = json.loads(env_json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and field_name in data:
                sources["env_json"] = data[field_name]
                sources["active_source"] = "env_json"
        except Exception:
            pass
    env_val = read_env_file_value(field_name)
    if env_val:
        sources["env_file"] = env_val
        if sources["active_source"] == "default":
            sources["active_source"] = "env_file"
    rt_path = get_runtime_config_path(plugin_config)
    if rt_path.exists():
        try:
            data = json.loads(rt_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and field_name in data:
                sources["runtime_config"] = data[field_name]
                if sources["active_source"] == "default":
                    sources["active_source"] = "runtime_config"
        except Exception:
            pass
    sources["default"] = getattr(type(plugin_config), field_name, None)
    return sources


__all__ = [
    "write_dotenv",
    "write_env_json",
    "write_both",
    "resolve_value_sources",
]
