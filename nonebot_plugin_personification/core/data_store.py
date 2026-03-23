from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional


class DataStore:
    """
    线程安全 + 协程安全的 JSON 存储。

    - 每个 store_name 对应一个 JSON 文件
    - 同步读写使用 threading.Lock 串行化
    - 异步读写额外使用 asyncio.Lock 保证协程级顺序
    - 异步路径内部仍会复用同步锁，避免 sync/async 混用时互相覆盖
    - 写入使用 tmp -> replace，降低文件损坏风险
    """

    def __init__(self, plugin_config: Any = None) -> None:
        self._base = Path(_get_data_dir(plugin_config))
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._sync_locks: dict[str, threading.Lock] = {}

    def _path(self, name: str) -> Path:
        return self._base / f"{name}.json"

    def _alock(self, name: str) -> asyncio.Lock:
        if name not in self._async_locks:
            self._async_locks[name] = asyncio.Lock()
        return self._async_locks[name]

    def _slock(self, name: str) -> threading.Lock:
        if name not in self._sync_locks:
            self._sync_locks[name] = threading.Lock()
        return self._sync_locks[name]

    def _read(self, name: str) -> Any:
        path = self._path(name)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, name: str, data: Any) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load_sync(self, name: str) -> Any:
        with self._slock(name):
            return self._read(name)

    def save_sync(self, name: str, data: Any) -> None:
        with self._slock(name):
            self._write(name, data)

    def mutate_sync(self, name: str, mutator: Callable[[Any], Any]) -> Any:
        with self._slock(name):
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


def _get_data_dir(plugin_config: Any | None = None) -> Path:
    configured = ""
    if plugin_config is not None:
        configured = str(getattr(plugin_config, "personification_data_dir", "") or "").strip()
    if configured:
        return Path(configured)

    try:
        from nonebot_plugin_localstore import get_data_dir

        try:
            return Path(get_data_dir("personification"))
        except TypeError:
            return Path(get_data_dir()) / "personification"
    except Exception:
        return Path("data") / "personification"


def init_data_store(plugin_config: Any) -> DataStore:
    global _store
    _store = DataStore(plugin_config)
    return _store


def get_data_store() -> DataStore:
    if _store is None:
        raise RuntimeError("DataStore not initialized. Call init_data_store() first.")
    return _store
