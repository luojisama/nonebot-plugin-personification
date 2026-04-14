from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any


class PluginKnowledgeStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "plugin_knowledge"
        self.local_dir = self.root / "local"
        self.store_dir = self.root / "store"
        self.runtime_dir = self.root / "runtime"
        self.root.mkdir(parents=True, exist_ok=True)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    @property
    def index_path(self) -> Path:
        return self.root / "_index.json"

    @property
    def build_state_path(self) -> Path:
        return self.root / "_build_state.json"

    def _read_json_nolock(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
        return data if isinstance(data, type(default)) else default

    def _write_json_nolock(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read_json(self, path: Path, default: Any) -> Any:
        with self._sync_lock:
            return self._read_json_nolock(path, default)

    def _write_json(self, path: Path, data: Any) -> None:
        with self._sync_lock:
            self._write_json_nolock(path, data)

    def load_index_sync(self) -> dict:
        with self._sync_lock:
            data = self._read_json_nolock(self.index_path, {"plugins": {}})
        if not isinstance(data.get("plugins"), dict):
            data["plugins"] = {}
        return data

    async def load_index(self) -> dict:
        async with self._async_lock:
            return await asyncio.to_thread(self.load_index_sync)

    def save_index_sync(self, index: dict) -> None:
        with self._sync_lock:
            self._write_json_nolock(self.index_path, index)

    async def save_index(self, index: dict) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self.save_index_sync, index)

    def load_build_state_sync(self) -> dict:
        with self._sync_lock:
            data = self._read_json_nolock(self.build_state_path, {"plugins": {}})
        if not isinstance(data.get("plugins"), dict):
            data["plugins"] = {}
        return data

    async def load_build_state(self) -> dict:
        async with self._async_lock:
            return await asyncio.to_thread(self.load_build_state_sync)

    def save_build_state_sync(self, state: dict) -> None:
        with self._sync_lock:
            self._write_json_nolock(self.build_state_path, state)

    async def save_build_state(self, state: dict) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self.save_build_state_sync, state)

    def _category_dir(self, category: str) -> Path:
        return self.store_dir if category == "store" else self.local_dir

    async def save_plugin_entry(self, plugin_name: str, category: str, entry: dict) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self._save_plugin_entry_sync, plugin_name, category, entry)

    def _save_plugin_entry_sync(self, plugin_name: str, category: str, entry: dict) -> None:
        with self._sync_lock:
            category_dir = self._category_dir(category)
            rel_file = f"{category}/{plugin_name}.json"
            target = category_dir / f"{plugin_name}.json"
            self._write_json_nolock(target, entry)
            index = self._read_json_nolock(self.index_path, {"plugins": {}})
            plugins = index.setdefault("plugins", {})
            current = plugins.get(plugin_name, {}) if isinstance(plugins.get(plugin_name), dict) else {}
            merged_keywords: list[str] = []
            for item in list(entry.get("keywords") or []):
                text = str(item or "").strip()
                if text and text not in merged_keywords:
                    merged_keywords.append(text)
            features = entry.get("features", {})
            if isinstance(features, dict):
                for feature in features.values():
                    if not isinstance(feature, dict):
                        continue
                    for item in [feature.get("title", ""), feature.get("summary", "")]:
                        text = str(item or "").strip()
                        if text and text not in merged_keywords:
                            merged_keywords.append(text)
                    for item in list(feature.get("keywords") or []):
                        text = str(item or "").strip()
                        if text and text not in merged_keywords:
                            merged_keywords.append(text)
                    for item in list(feature.get("config_items") or []):
                        text = str(item or "").strip()
                        if text and text not in merged_keywords:
                            merged_keywords.append(text)
            plugins[plugin_name] = {
                **current,
                "plugin_name": plugin_name,
                "category": category,
                "file": rel_file,
                "display_name": str(entry.get("display_name", "") or ""),
                "summary": str(entry.get("summary", "") or ""),
                "keywords": merged_keywords,
                "updated_at": str(entry.get("updated_at", "") or ""),
            }
            self._write_json_nolock(self.index_path, index)

    def load_plugin_entry_sync(self, plugin_name: str) -> dict | None:
        with self._sync_lock:
            index = self._read_json_nolock(self.index_path, {"plugins": {}})
            plugins = index.get("plugins", {})
            meta = plugins.get(plugin_name) if isinstance(plugins, dict) else None
            if not isinstance(meta, dict):
                return None
            file_rel = str(meta.get("file", "") or "").strip()
            if not file_rel:
                return None
            path = self.root / file_rel
            data = self._read_json_nolock(path, None)
            return data if isinstance(data, dict) else None

    async def load_plugin_entry(self, plugin_name: str) -> dict | None:
        async with self._async_lock:
            return await asyncio.to_thread(self.load_plugin_entry_sync, plugin_name)

    async def save_runtime_snapshot(self, plugin_name: str, snapshot: dict) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self._save_runtime_snapshot_sync, plugin_name, snapshot)

    def _save_runtime_snapshot_sync(self, plugin_name: str, snapshot: dict) -> None:
        with self._sync_lock:
            target = self.runtime_dir / f"{plugin_name}.json"
            self._write_json_nolock(target, snapshot)
            index = self._read_json_nolock(self.index_path, {"plugins": {}})
            plugins = index.setdefault("plugins", {})
            current = plugins.get(plugin_name, {}) if isinstance(plugins.get(plugin_name), dict) else {}
            current["has_runtime_data"] = True
            current["runtime_file"] = f"runtime/{plugin_name}.json"
            plugins[plugin_name] = current
            self._write_json_nolock(self.index_path, index)

    def load_runtime_snapshot_sync(self, plugin_name: str) -> dict | None:
        with self._sync_lock:
            path = self.runtime_dir / f"{plugin_name}.json"
            data = self._read_json_nolock(path, None)
            return data if isinstance(data, dict) else None

    async def load_runtime_snapshot(self, plugin_name: str) -> dict | None:
        async with self._async_lock:
            return await asyncio.to_thread(self.load_runtime_snapshot_sync, plugin_name)

    def search_plugins(self, query: str, top_k: int = 5) -> list[str]:
        text = str(query or "").strip().lower()
        if not text:
            return []
        index = self.load_index_sync()
        plugins = index.get("plugins", {})
        if not isinstance(plugins, dict):
            return []
        tokens = self._to_search_tokens(text)
        scored: list[tuple[int, str]] = []
        for plugin_name, meta in plugins.items():
            if not isinstance(meta, dict):
                continue
            haystack = " ".join(
                [
                    str(plugin_name or ""),
                    str(meta.get("display_name", "") or ""),
                    str(meta.get("summary", "") or ""),
                    " ".join(str(item or "") for item in (meta.get("keywords") or [])),
                ]
            ).lower()
            score = 0
            if text == str(plugin_name).lower():
                score += 30
            elif text in haystack:
                score += 10
            for token in tokens:
                if len(token) >= 2 and token in haystack:
                    score += 3 if len(token) >= 4 else 1
            if score > 0:
                scored.append((score, str(plugin_name)))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [name for _score, name in scored[:top_k]]

    @staticmethod
    def _to_search_tokens(text: str) -> list[str]:
        normalized = text.lower().replace("_", " ").replace("-", " ")
        words = [token for token in normalized.split() if token]
        compact = "".join(ch for ch in normalized if not ch.isspace())
        bigrams = [compact[i : i + 2] for i in range(max(0, len(compact) - 1))]
        tokens: list[str] = []
        for token in words + bigrams:
            if token and token not in tokens:
                tokens.append(token)
        return tokens or [text]
