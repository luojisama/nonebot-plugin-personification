from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config_registry import get_config_entries
from .paths import get_data_dir


_ENV_CONFIG_INFO_ATTR = "_personification_env_config_info"
_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}
_SYNC_LOCKS: dict[str, threading.RLock] = {}


def get_env_config_path(plugin_config: Any) -> Path:
    return Path(get_data_dir(plugin_config)) / "env.json"


def _managed_field_names() -> list[str]:
    return [entry.field_name for entry in get_config_entries("global")]


def _collect_explicit_env_fields(plugin_config: Any) -> set[str]:
    """收集 .env / OS 环境变量 / pydantic 显式声明的插件字段。

    这些字段只用于首次导入 env.json。导入后 env.json 是 WebUI 管理的权威层，
    不再被 .env.prod 里的旧值永久压制。
    """
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
    # 兜底：直接扫描 .env / .env.prod 文件，确保被 env 文件显式声明的字段
    # 永远不会被 env.json 持久化层覆盖。复用 runtime_config 的同名函数避免重复。
    try:
        from .runtime_config import _collect_env_file_keys
        explicit.update(_collect_env_file_keys())
    except Exception:
        pass
    return explicit


def _set_env_config_info(plugin_config: Any, info: dict[str, Any]) -> None:
    try:
        plugin_config.__dict__[_ENV_CONFIG_INFO_ATTR] = dict(info)
    except Exception:
        try:
            object.__setattr__(plugin_config, _ENV_CONFIG_INFO_ATTR, dict(info))
        except Exception:
            return


def get_env_config_load_info(plugin_config: Any) -> dict[str, Any]:
    info = getattr(plugin_config, _ENV_CONFIG_INFO_ATTR, None)
    return dict(info) if isinstance(info, dict) else {}


def _path_key(path: Path) -> str:
    return str(path.resolve())


def _get_async_lock(path: Path) -> asyncio.Lock:
    key = _path_key(path)
    lock = _ASYNC_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ASYNC_LOCKS[key] = lock
    return lock


def _get_sync_lock(path: Path) -> threading.RLock:
    key = _path_key(path)
    lock = _SYNC_LOCKS.get(key)
    if lock is None:
        lock = threading.RLock()
        _SYNC_LOCKS[key] = lock
    return lock


def _new_load_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "applied_fields": [],
        "imported_fields": [],
        "skipped_fields": [],
        "errors": [],
        "loaded": False,
    }


def _restrict_sensitive_file_permissions(path: Path) -> None:
    """Best-effort: keep config files containing tokens/cookies owner-readable only."""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _write_payload_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        _restrict_sensitive_file_permissions(Path(tmp_path))
        os.replace(tmp_path, path)
        _restrict_sensitive_file_permissions(path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ConfigManager:
    def __init__(self, *, plugin_config: Any, logger: Any) -> None:
        self.plugin_config = plugin_config
        self.logger = logger
        self.path = get_env_config_path(plugin_config)
        self._async_lock = _get_async_lock(self.path)
        self._sync_lock = _get_sync_lock(self.path)

    def save(self) -> None:
        with self._sync_lock:
            self._save_unlocked()

    async def reload(self) -> None:
        async with self._async_lock:
            with self._sync_lock:
                self._load_unlocked()

    async def update(self, updates: Mapping[str, Any]) -> None:
        async with self._async_lock:
            with self._sync_lock:
                payload = self._managed_payload()
                for field_name, value in dict(updates or {}).items():
                    if field_name in payload:
                        payload[field_name] = value
                _write_payload_atomic(self.path, payload)
                for field_name, value in payload.items():
                    try:
                        setattr(self.plugin_config, field_name, value)
                    except Exception as exc:
                        if self.logger is not None:
                            self.logger.warning(
                                f"personification: update env config apply failed field={field_name}: {exc}"
                            )

    def load(self) -> None:
        with self._sync_lock:
            self._load_unlocked()

    def _managed_payload(self) -> dict[str, Any]:
        return {
            field_name: getattr(self.plugin_config, field_name, None)
            for field_name in _managed_field_names()
        }

    def _save_unlocked(self) -> None:
        payload = self._managed_payload()
        try:
            _write_payload_atomic(self.path, payload)
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning(f"personification: save env config failed path={self.path}: {exc}")
            return

    def _load_unlocked(self) -> None:
        info = _new_load_info(self.path)
        had_file = self.path.exists()
        payload: dict[str, Any] = {}
        if not had_file:
            payload = {}
        else:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception as exc:
                info["errors"].append(str(exc))
                _set_env_config_info(self.plugin_config, info)
                if self.logger is not None:
                    self.logger.warning(f"personification: load env config failed path={self.path}: {exc}")
                return
            if isinstance(loaded, dict):
                payload = loaded
            else:
                payload = {}

        managed_fields = set(_managed_field_names())
        imported_fields: list[str] = []
        for field_name in sorted(_collect_explicit_env_fields(self.plugin_config)):
            if field_name not in managed_fields or field_name in payload:
                continue
            if not hasattr(self.plugin_config, field_name):
                continue
            payload[field_name] = getattr(self.plugin_config, field_name, None)
            imported_fields.append(field_name)

        if imported_fields:
            try:
                _write_payload_atomic(self.path, payload)
            except Exception as exc:
                info["errors"].append(f"bootstrap import save failed: {exc}")
                if self.logger is not None:
                    self.logger.warning(
                        f"personification: import env fields to env.json failed path={self.path}: {exc}"
                    )

        if not had_file and not imported_fields:
            _set_env_config_info(self.plugin_config, info)
            return

        for field_name in _managed_field_names():
            if field_name not in payload:
                continue
            try:
                setattr(self.plugin_config, field_name, payload[field_name])
            except Exception as exc:
                info["errors"].append(f"{field_name}: {exc}")
                continue
            info["applied_fields"].append(field_name)
        info["imported_fields"] = imported_fields
        info["loaded"] = True
        _set_env_config_info(self.plugin_config, info)
        if imported_fields and self.logger is not None:
            self.logger.info(
                "personification: imported initial .env fields into env.json; fields="
                + ", ".join(imported_fields)
            )
        if had_file and self.logger is not None:
            env_shadowed = sorted(set(info["applied_fields"]) & _collect_explicit_env_fields(self.plugin_config))
            if env_shadowed:
                self.logger.info(
                    "personification: env.json overrides legacy .env bootstrap fields; fields="
                    + ", ".join(env_shadowed)
                )


def save_managed_env_config(plugin_config: Any, logger: Any) -> None:
    ConfigManager(plugin_config=plugin_config, logger=logger).save()


def load_managed_env_config(plugin_config: Any, logger: Any) -> None:
    ConfigManager(plugin_config=plugin_config, logger=logger).load()


__all__ = [
    "ConfigManager",
    "get_env_config_load_info",
    "get_env_config_path",
    "load_managed_env_config",
    "save_managed_env_config",
]
