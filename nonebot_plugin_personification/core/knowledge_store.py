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
        self.source_dir = self.root / "source"
        self.root.mkdir(parents=True, exist_ok=True)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.RLock()

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
        if default is None:
            return data
        return data if isinstance(data, type(default)) else default

    def _write_json_nolock(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _unlink_nolock(path: Path) -> bool:
        try:
            if path.exists():
                path.unlink()
                return True
        except Exception:
            return False
        return False

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

    @staticmethod
    def _collect_source_terms(snapshot: dict) -> list[str]:
        terms: list[str] = []

        def _append(value: Any) -> None:
            text = str(value or "").strip()
            if not text or text in terms:
                return
            terms.append(text)

        files = snapshot.get("files", [])
        if isinstance(files, list):
            for item in files:
                if not isinstance(item, dict):
                    continue
                _append(item.get("path"))
                for symbol in list(item.get("symbols") or [])[:24]:
                    _append(symbol)

        chunks = snapshot.get("chunks", [])
        if isinstance(chunks, list):
            for item in chunks[:48]:
                if not isinstance(item, dict):
                    continue
                _append(item.get("file"))
                for symbol in list(item.get("symbols") or [])[:12]:
                    _append(symbol)
        return terms

    async def save_plugin_entry(self, plugin_name: str, category: str, entry: dict) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self._save_plugin_entry_sync, plugin_name, category, entry)

    def _save_plugin_entry_sync(self, plugin_name: str, category: str, entry: dict) -> None:
        with self._sync_lock:
            category_dir = self._category_dir(category)
            rel_file = f"{category}/{plugin_name}.json"
            target = category_dir / f"{plugin_name}.json"
            self._write_json_nolock(target, entry)
            def _mutate(index: dict) -> dict:
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
                        for item in list(feature.get("files") or []):
                            text = str(item or "").strip()
                            if text and text not in merged_keywords:
                                merged_keywords.append(text)
                        for item in list(feature.get("symbols") or []):
                            text = str(item or "").strip()
                            if text and text not in merged_keywords:
                                merged_keywords.append(text)
                for item in [entry.get("architecture_summary", "")]:
                    text = str(item or "").strip()
                    if text and text not in merged_keywords:
                        merged_keywords.append(text)
                for collection_name in ("entrypoints", "implementation_map", "data_access"):
                    for item in list(entry.get(collection_name) or []):
                        if not isinstance(item, dict):
                            continue
                        for value in item.values():
                            if isinstance(value, list):
                                for part in value:
                                    text = str(part or "").strip()
                                    if text and text not in merged_keywords:
                                        merged_keywords.append(text)
                            else:
                                text = str(value or "").strip()
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
                return index

            self.update_index_sync(_mutate)

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
            def _mutate(index: dict) -> dict:
                plugins = index.setdefault("plugins", {})
                current = plugins.get(plugin_name, {}) if isinstance(plugins.get(plugin_name), dict) else {}
                current["has_runtime_data"] = True
                current["runtime_file"] = f"runtime/{plugin_name}.json"
                plugins[plugin_name] = current
                return index

            self.update_index_sync(_mutate)

    def load_runtime_snapshot_sync(self, plugin_name: str) -> dict | None:
        with self._sync_lock:
            path = self.runtime_dir / f"{plugin_name}.json"
            data = self._read_json_nolock(path, None)
            return data if isinstance(data, dict) else None

    async def load_runtime_snapshot(self, plugin_name: str) -> dict | None:
        async with self._async_lock:
            return await asyncio.to_thread(self.load_runtime_snapshot_sync, plugin_name)

    async def save_source_snapshot(self, plugin_name: str, snapshot: dict) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self._save_source_snapshot_sync, plugin_name, snapshot)

    def _save_source_snapshot_sync(self, plugin_name: str, snapshot: dict) -> None:
        with self._sync_lock:
            target = self.source_dir / f"{plugin_name}.json"
            self._write_json_nolock(target, snapshot)

            def _mutate(index: dict) -> dict:
                plugins = index.setdefault("plugins", {})
                current = plugins.get(plugin_name, {}) if isinstance(plugins.get(plugin_name), dict) else {}
                files = snapshot.get("files", [])
                chunks = snapshot.get("chunks", [])
                current["has_source_data"] = True
                current["source_file"] = f"source/{plugin_name}.json"
                current["source_file_count"] = len(files) if isinstance(files, list) else 0
                current["source_chunk_count"] = len(chunks) if isinstance(chunks, list) else 0
                current["source_terms"] = self._collect_source_terms(snapshot)
                plugins[plugin_name] = {
                    "plugin_name": plugin_name,
                    **current,
                }
                return index

            self.update_index_sync(_mutate)

    def load_source_snapshot_sync(self, plugin_name: str) -> dict | None:
        with self._sync_lock:
            path = self.source_dir / f"{plugin_name}.json"
            data = self._read_json_nolock(path, None)
            return data if isinstance(data, dict) else None

    async def load_source_snapshot(self, plugin_name: str) -> dict | None:
        async with self._async_lock:
            return await asyncio.to_thread(self.load_source_snapshot_sync, plugin_name)

    def find_plugin_candidates_sync(self, query: str, top_k: int = 8) -> list[str]:
        normalized = str(query or "").strip().lower()
        if not normalized:
            return []
        index = self.load_index_sync()
        plugins = index.get("plugins", {})
        if not isinstance(plugins, dict):
            return []

        exact_matches: list[str] = []
        for plugin_name, meta in plugins.items():
            if not isinstance(meta, dict):
                continue
            display_name = str(meta.get("display_name", "") or "").strip().lower()
            plugin_key = str(plugin_name or "").strip().lower()
            if normalized in {plugin_key, display_name}:
                exact_matches.append(str(plugin_name))
        if exact_matches:
            return sorted(set(exact_matches))

        return self.search_plugins(query, top_k=max(1, int(top_k or 8)))

    async def find_plugin_candidates(self, query: str, top_k: int = 8) -> list[str]:
        async with self._async_lock:
            return await asyncio.to_thread(self.find_plugin_candidates_sync, query, top_k)

    def delete_plugin_knowledge_sync(self, plugin_name: str) -> dict[str, Any]:
        normalized = str(plugin_name or "").strip()
        if not normalized:
            return {"deleted": False, "plugin_name": "", "removed_files": 0}

        with self._sync_lock:
            removed_files = 0
            index = self._read_json_nolock(self.index_path, {"plugins": {}})
            plugins = index.get("plugins", {})
            meta = plugins.pop(normalized, None) if isinstance(plugins, dict) else None

            candidate_paths = [
                self.local_dir / f"{normalized}.json",
                self.store_dir / f"{normalized}.json",
                self.runtime_dir / f"{normalized}.json",
                self.source_dir / f"{normalized}.json",
            ]
            if isinstance(meta, dict):
                file_rel = str(meta.get("file", "") or "").strip()
                runtime_file = str(meta.get("runtime_file", "") or "").strip()
                source_file = str(meta.get("source_file", "") or "").strip()
                for rel_path in (file_rel, runtime_file, source_file):
                    if rel_path:
                        candidate_paths.append(self.root / rel_path)

            for path in candidate_paths:
                if self._unlink_nolock(path):
                    removed_files += 1

            build_state = self._read_json_nolock(self.build_state_path, {"plugins": {}})
            state_plugins = build_state.get("plugins", {})
            if isinstance(state_plugins, dict):
                state_plugins.pop(normalized, None)
                build_state["plugins"] = state_plugins
            else:
                build_state = {"plugins": {}}

            self._write_json_nolock(self.index_path, index if isinstance(index, dict) else {"plugins": {}})
            self._write_json_nolock(self.build_state_path, build_state)
            return {
                "deleted": bool(meta) or removed_files > 0,
                "plugin_name": normalized,
                "removed_files": removed_files,
            }

    async def delete_plugin_knowledge(self, plugin_name: str) -> dict[str, Any]:
        async with self._async_lock:
            return await asyncio.to_thread(self.delete_plugin_knowledge_sync, plugin_name)

    def clear_all_plugin_knowledge_sync(self) -> dict[str, Any]:
        with self._sync_lock:
            removed_files = 0
            for directory in (self.local_dir, self.store_dir, self.runtime_dir, self.source_dir):
                for path in directory.glob("*.json"):
                    if self._unlink_nolock(path):
                        removed_files += 1
            self._write_json_nolock(self.index_path, {"plugins": {}})
            self._write_json_nolock(self.build_state_path, {"plugins": {}})
            return {"cleared": True, "removed_files": removed_files}

    async def clear_all_plugin_knowledge(self) -> dict[str, Any]:
        async with self._async_lock:
            return await asyncio.to_thread(self.clear_all_plugin_knowledge_sync)

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
                    " ".join(str(item or "") for item in (meta.get("source_terms") or [])),
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

    def update_index_sync(self, mutator: Any) -> dict:
        with self._sync_lock:
            index = self._read_json_nolock(self.index_path, {"plugins": {}})
            if not isinstance(index, dict):
                index = {"plugins": {}}
            if not isinstance(index.get("plugins"), dict):
                index["plugins"] = {}
            result = mutator(index)
            updated = result if isinstance(result, dict) else index
            self._write_json_nolock(self.index_path, updated)
            return updated
