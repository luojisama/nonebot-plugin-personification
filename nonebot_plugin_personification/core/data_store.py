from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Optional

from .db import connect_sync, init_db_sync
from .migration import migrate_all
from .paths import get_data_dir as _get_data_dir


_ROOT_KEY = "__root__"

class DataStore:
    """
    基于 SQLite kv_store 的命名空间存储。

    对外仍保持 load/save/mutate/update 接口，以兼容原有调用方。
    每个 namespace 仍然表现为“一整个 JSON 文档”，只是在底层存进 SQLite。
    """

    def __init__(self, plugin_config: Any = None, logger: Any = None) -> None:
        self._base = Path(_get_data_dir(plugin_config))
        self._logger = logger
        self._async_locks: dict[str, asyncio.Lock] = {}

    def _alock(self, name: str) -> asyncio.Lock:
        if name not in self._async_locks:
            self._async_locks[name] = asyncio.Lock()
        return self._async_locks[name]

    def _read(self, name: str) -> Any:
        with connect_sync() as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE namespace=? AND key=?",
                (name, _ROOT_KEY),
            ).fetchone()
        if not row:
            return {}
        try:
            raw = row["value"] if hasattr(row, "__getitem__") else row[0]
            return json.loads(raw)
        except Exception:
            return {}

    def _write(self, name: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        with connect_sync() as conn:
            conn.execute(
                """
                INSERT INTO kv_store(namespace, key, value, updated_at)
                VALUES (?, ?, ?, unixepoch('now'))
                ON CONFLICT(namespace, key)
                DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (name, _ROOT_KEY, payload),
            )
            conn.commit()

    def load_sync(self, name: str) -> Any:
        return self._read(name)

    def save_sync(self, name: str, data: Any) -> None:
        self._write(name, data)

    def mutate_sync(self, name: str, mutator: Callable[[Any], Any]) -> Any:
        current = self._read(name)
        updated = mutator(current)
        if updated is None:
            updated = current
        self._write(name, updated)
        return updated

    def update_sync(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        def _mutate(current: Any) -> dict[str, Any]:
            data = current if isinstance(current, dict) else {}
            data.update(patch)
            return data

        updated = self.mutate_sync(name, _mutate)
        return updated if isinstance(updated, dict) else {}

    async def load(self, name: str) -> Any:
        async with self._alock(name):
            return await asyncio.to_thread(self.load_sync, name)

    async def save(self, name: str, data: Any) -> None:
        async with self._alock(name):
            await asyncio.to_thread(self.save_sync, name, data)

    async def update(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self._alock(name):
            return await asyncio.to_thread(self.update_sync, name, patch)


_store: Optional[DataStore] = None


def init_data_store(plugin_config: Any, logger: Any = None) -> DataStore:
    global _store
    data_dir = _get_data_dir(plugin_config)
    init_db_sync(data_dir)
    migrate_all(data_dir, logger=logger or _SilentLogger())
    _store = DataStore(plugin_config, logger=logger)
    return _store


def get_data_store() -> DataStore:
    if _store is None:
        raise RuntimeError("DataStore not initialized. Call init_data_store() first.")
    return _store


class _SilentLogger:
    def warning(self, _msg: str) -> None:
        return None
