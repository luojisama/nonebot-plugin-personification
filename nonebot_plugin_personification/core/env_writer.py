from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key

from .config_manager import _write_payload_atomic, get_env_config_path
from .runtime_config import (
    _iter_env_file_candidates,
    get_runtime_config_path,
    read_env_file_value,
)


def _resolve_dotenv_target() -> Path | None:
    for path in _iter_env_file_candidates():
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
    path = target or _resolve_dotenv_target()
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
        except Exception:
            pass
    set_key(str(path), field_name, new_value_str, quote_mode="auto")
    return path


def write_env_json(field_name: str, value: Any, plugin_config: Any) -> Path:
    """写入 env.json 覆盖层。
    与 ConfigManager._save_unlocked 不同，这里不剔除 .env 中已存在的字段，
    因为本函数与 write_dotenv 配对，用于让 env.json 与 .env 同步该 key。
    """
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
    """双写 .env.prod + env.json，返回结构化结果（含错误信息，不抛异常）。"""
    result: dict[str, Any] = {
        "field_name": field_name,
        "value": value,
        "dotenv_path": None,
        "env_json_path": None,
        "errors": [],
    }
    try:
        dotenv_path = write_dotenv(field_name, value)
        if dotenv_path is not None:
            result["dotenv_path"] = str(dotenv_path)
        else:
            result["errors"].append("no writable .env / .env.prod found")
    except PermissionError as exc:
        result["errors"].append(f".env write blocked: {exc}")
    except Exception as exc:
        result["errors"].append(f".env write failed: {exc}")
    try:
        json_path = write_env_json(field_name, value, plugin_config)
        result["env_json_path"] = str(json_path)
    except Exception as exc:
        result["errors"].append(f"env.json write failed: {exc}")
    return result


def resolve_value_sources(field_name: str, plugin_config: Any) -> dict[str, Any]:
    """返回字段在 .env / env.json / runtime_config / 默认值 各层的快照。

    供 WebUI 显示 "当前生效来源" 并辅助管理员决策。优先级与 ConfigManager 一致：
    .env > env.json > runtime_config > 默认值。
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
    env_val = read_env_file_value(field_name)
    if env_val:
        sources["env_file"] = env_val
        sources["active_source"] = "env_file"
    env_json_path = get_env_config_path(plugin_config)
    if env_json_path.exists():
        try:
            data = json.loads(env_json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and field_name in data:
                sources["env_json"] = data[field_name]
                if sources["active_source"] == "default":
                    sources["active_source"] = "env_json"
        except Exception:
            pass
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
