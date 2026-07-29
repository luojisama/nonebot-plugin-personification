from __future__ import annotations

import json
import hashlib
import sqlite3
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .embedding_index import EMBED_MODEL_VERSION, cosine_similarity, embed_text, normalize_text, tokenize
from .entity_index import extract_entities
from .memory_defaults import MAX_MEMORY_RECALL_TOP_K
from .search_ranker import (
    build_time_hint,
    now_ts,
    query_looks_ambiguous,
    query_looks_latest,
    rank_memory_payload,
    safe_float,
    time_hint_score,
)


MAX_RECALL_LIMIT = MAX_MEMORY_RECALL_TOP_K
MAX_SEARCH_STATS_ROWS = 20000
SEARCH_STATS_RETENTION_DAYS = 30
SEARCH_STATS_PRUNE_INTERVAL_SECONDS = 3600
VECTOR_CHUNK_MODEL_VERSION = EMBED_MODEL_VERSION
VECTOR_CHUNK_TEXT_LIMIT = 900

_MAINTENANCE_LOCKS: dict[str, threading.RLock] = {}
_MAINTENANCE_LOCKS_GUARD = threading.Lock()
_PROFILE_GENERATIONS: dict[str, int] = {}


class LocalProfileRevisionConflict(ValueError):
    code = "stale_revision"

    def __init__(self, *, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"{self.code}: expected revision {expected_revision}, got {current_revision}"
        )


class ProfileGenerationConflict(ValueError):
    code = "profile_generation_changed"

    def __init__(self, *, expected_generation: int, current_generation: int) -> None:
        self.expected_generation = expected_generation
        self.current_generation = current_generation
        super().__init__(
            f"{self.code}: expected generation {expected_generation}, got {current_generation}"
        )


def _shared_maintenance_lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _MAINTENANCE_LOCKS_GUARD:
        return _MAINTENANCE_LOCKS.setdefault(key, threading.RLock())


def _plugin_data_dir(plugin_config: Any | None = None) -> Path:
    configured = ""
    if plugin_config is not None:
        configured = str(getattr(plugin_config, "personification_data_dir", "") or "").strip()
    if configured:
        return Path(configured)
    try:
        import nonebot_plugin_localstore as store

        return Path(store.get_plugin_data_dir())
    except Exception:
        return Path("data") / "personification"


def resolve_memory_root(plugin_config: Any | None = None) -> Path:
    return _plugin_data_dir(plugin_config)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 共享连接可能被事件循环与线程池并发使用，写锁竞争时等待而不是立刻抛
    # "database is locked"。
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _json_loads(text: Any, default: Any) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except Exception:
        return default
    return value


def _pack_float16_vector(values: Any) -> bytes:
    if not isinstance(values, list):
        return b""
    floats: list[float] = []
    for value in values:
        try:
            floats.append(float(value))
        except (TypeError, ValueError):
            floats.append(0.0)
    if not floats:
        return b""
    try:
        return struct.pack(f"<{len(floats)}e", *floats)
    except Exception:
        return b""


@dataclass
class MemorySearchCandidate:
    memory_id: str
    payload: dict[str, Any]
    base_score: float
    match_reasons: list[str]
    source: str


def _fuse_recall_candidates(
    groups: list[list[MemorySearchCandidate]],
    *,
    limit: int,
    rrf_k: int = 60,
) -> list[MemorySearchCandidate]:
    fused: dict[str, MemorySearchCandidate] = {}
    source_names: dict[str, list[str]] = {}
    for group in groups:
        ranked = sorted(list(group or []), key=lambda item: item.base_score, reverse=True)
        for rank, candidate in enumerate(ranked, start=1):
            if not candidate.memory_id:
                continue
            bonus = 1.0 / float(max(1, rrf_k) + rank)
            existing = fused.get(candidate.memory_id)
            if existing is None:
                fused[candidate.memory_id] = MemorySearchCandidate(
                    memory_id=candidate.memory_id,
                    payload=dict(candidate.payload),
                    base_score=float(candidate.base_score) + bonus,
                    match_reasons=list(candidate.match_reasons or []),
                    source=str(candidate.source or "").strip(),
                )
                source_names[candidate.memory_id] = [str(candidate.source or "").strip()] if candidate.source else []
                continue
            existing.base_score = max(existing.base_score, float(candidate.base_score)) + bonus
            for reason in list(candidate.match_reasons or []):
                if reason and reason not in existing.match_reasons:
                    existing.match_reasons.append(reason)
            source = str(candidate.source or "").strip()
            if source and source not in source_names.setdefault(candidate.memory_id, []):
                source_names[candidate.memory_id].append(source)
    for memory_id, candidate in fused.items():
        sources = source_names.get(memory_id, [])
        if len(sources) > 1:
            candidate.source = "+".join(sources)
            reason = "RRF混合召回:" + ",".join(sources[:4])
            if reason not in candidate.match_reasons:
                candidate.match_reasons.append(reason)
    return sorted(fused.values(), key=lambda item: item.base_score, reverse=True)[: max(1, int(limit or 1))]


class MemoryStore:
    def __init__(self, plugin_config: Any, logger: Any = None) -> None:
        self.plugin_config = plugin_config
        self.logger = logger
        self.root_dir = resolve_memory_root(plugin_config)
        self.grouped_memory_dir = self.root_dir / "grouped_memory"
        self.groups_dir = self.grouped_memory_dir / "groups"
        self.shared_dir = self.grouped_memory_dir / "shared"
        self.memory_palace_dir = self.root_dir / "memory_palace"
        self.recycle_bin_dir = self.root_dir / "_legacy_recycle_bin"
        self.maintenance_lock = _shared_maintenance_lock(self.root_dir)
        self._profile_generation_key = str(self.root_dir.resolve())
        with self.maintenance_lock:
            _PROFILE_GENERATIONS.setdefault(self._profile_generation_key, 0)
        self._fts_available = False
        self._last_search_stats_prune_ts = 0.0

    def initialize(self) -> None:
        self._relocate_old_memory_dirs()
        self.groups_dir.mkdir(parents=True, exist_ok=True)
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.memory_palace_dir.mkdir(parents=True, exist_ok=True)
        self.recycle_bin_dir.mkdir(parents=True, exist_ok=True)
        self._init_shared_db()
        self._init_palace_db()

    def _relocate_old_memory_dirs(self) -> None:
        current_root = self.root_dir
        if current_root.name != "personification":
            return

        old_root = current_root.parent
        for name in ("grouped_memory", "memory_palace", "_legacy_recycle_bin"):
            old_path = old_root / name
            new_path = current_root / name

            if not old_path.exists():
                continue

            try:
                if old_path.resolve() == new_path.resolve():
                    continue
            except Exception:
                pass

            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)

                if not new_path.exists():
                    old_path.rename(new_path)
                    continue

                if not old_path.is_dir():
                    continue

                for src in old_path.rglob("*"):
                    if src.is_dir():
                        continue

                    rel = src.relative_to(old_path)
                    dst = new_path / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)

                    if not dst.exists():
                        src.rename(dst)
            except Exception as exc:
                warning = getattr(self.logger, "warning", None)
                if callable(warning):
                    warning(f"[memory] failed to relocate old memory dir {old_path} -> {new_path}: {exc}")

    def memory_enabled(self) -> bool:
        return bool(getattr(self.plugin_config, "personification_memory_enabled", True))

    def palace_enabled(self) -> bool:
        return self.memory_enabled() and bool(
            getattr(self.plugin_config, "personification_memory_palace_enabled", False)
        )

    def ensure_group_space(self, group_id: str) -> Path:
        safe_group_id = str(group_id or "").strip() or "unknown"
        group_dir = self.groups_dir / safe_group_id
        group_dir.mkdir(parents=True, exist_ok=True)
        self._init_group_chat_db(group_dir / "chat_history.db")
        self._init_group_context_db(group_dir / "group_context.db")
        self._init_local_profile_db(group_dir / "local_user_profiles.db")
        return group_dir

    def append_group_message(
        self,
        *,
        group_id: str,
        role: str,
        content: Any,
        metadata: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> None:
        group_dir = self.ensure_group_space(group_id)
        payload = json.dumps(content, ensure_ascii=False)
        meta = json.dumps(metadata or {}, ensure_ascii=False)
        with _connect(group_dir / "chat_history.db") as conn:
            conn.execute(
                """
                INSERT INTO messages(role, content, metadata, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(role or ""), payload, meta, float(created_at or time.time())),
            )
            conn.commit()

    def save_group_context(self, *, group_id: str, key: str, value: Any) -> None:
        group_dir = self.ensure_group_space(group_id)
        with _connect(group_dir / "group_context.db") as conn:
            conn.execute(
                """
                INSERT INTO context_entries(context_key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(context_key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(key or ""), json.dumps(value, ensure_ascii=False), time.time()),
            )
            conn.commit()

    def upsert_local_profile(
        self,
        *,
        group_id: str,
        user_id: str,
        profile_text: str,
        profile_json: dict[str, Any] | None = None,
    ) -> None:
        with self.maintenance_lock:
            self._upsert_local_profile_unlocked(
                group_id=group_id,
                user_id=user_id,
                profile_text=profile_text,
                profile_json=profile_json,
            )

    def _upsert_local_profile_unlocked(
        self,
        *,
        group_id: str,
        user_id: str,
        profile_text: str,
        profile_json: dict[str, Any] | None = None,
    ) -> None:
        group_dir = self.ensure_group_space(group_id)
        payload = json.dumps(profile_json or {}, ensure_ascii=False)
        with _connect(group_dir / "local_user_profiles.db") as conn:
            conn.execute(
                """
                INSERT INTO profiles(user_id, profile_text, profile_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    profile_text=excluded.profile_text,
                    profile_json=excluded.profile_json,
                    updated_at=excluded.updated_at
                """,
                (str(user_id or ""), str(profile_text or ""), payload, time.time()),
            )
            conn.commit()

    def atomic_patch_local_profile(
        self,
        *,
        group_id: str,
        user_id: str,
        patcher: Callable[[dict[str, Any]], dict[str, Any]],
        profile_text: str | None = None,
        expected_revision: int | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Atomically patch one group-local profile and advance its revision."""
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid:
            raise ValueError("group_id is required")
        if not uid:
            raise ValueError("user_id is required")

        with self.maintenance_lock:
            self._check_profile_generation_unlocked(expected_generation)
            group_dir = self.ensure_group_space(gid)
            with _connect(group_dir / "local_user_profiles.db") as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT profile_text, profile_json FROM profiles WHERE user_id=?",
                    (uid,),
                ).fetchone()
                current_json = _json_loads(row["profile_json"] if row else "{}", {})
                if not isinstance(current_json, dict):
                    current_json = {}
                try:
                    current_revision = max(0, int(current_json.get("revision", 0) or 0))
                except (TypeError, ValueError):
                    current_revision = 0
                if expected_revision is not None:
                    try:
                        normalized_expected_revision = int(expected_revision)
                    except (TypeError, ValueError):
                        conn.rollback()
                        raise ValueError("expected_revision must be an integer") from None
                    if normalized_expected_revision != current_revision:
                        conn.rollback()
                        raise LocalProfileRevisionConflict(
                            expected_revision=normalized_expected_revision,
                            current_revision=current_revision,
                        )

                patch_input = dict(current_json)
                patch_input["revision"] = current_revision
                patched_json = patcher(patch_input)
                if not isinstance(patched_json, dict):
                    conn.rollback()
                    raise TypeError("profile patcher must return a dict")
                patched_json = dict(patched_json)
                patched_json["revision"] = current_revision + 1
                next_text = (
                    str(profile_text)
                    if profile_text is not None
                    else str(row["profile_text"] or "") if row else ""
                )
                updated_at = time.time()
                conn.execute(
                    """
                    INSERT INTO profiles(user_id, profile_text, profile_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        profile_text=excluded.profile_text,
                        profile_json=excluded.profile_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        uid,
                        next_text,
                        json.dumps(patched_json, ensure_ascii=False),
                        updated_at,
                    ),
                )
                conn.commit()
        return {
            "profile_text": next_text,
            "profile_json": patched_json,
            "updated_at": updated_at,
        }

    def get_local_profile(self, *, group_id: str, user_id: str) -> dict[str, Any] | None:
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return None
        path = self.groups_dir / gid / "local_user_profiles.db"
        if not path.is_file():
            return None
        with _connect(path) as conn:
            row = conn.execute(
                "SELECT profile_text, profile_json, updated_at FROM profiles WHERE user_id=?",
                (uid,),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["profile_json"] or "{}")
        except Exception:
            payload = {}
        return {
            "profile_text": str(row["profile_text"] or ""),
            "profile_json": payload if isinstance(payload, dict) else {},
            "updated_at": float(row["updated_at"] or 0),
        }

    def delete_local_profile(self, *, group_id: str, user_id: str) -> bool:
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid:
            raise ValueError("group_id is required")
        if not uid:
            raise ValueError("user_id is required")
        with self.maintenance_lock:
            self._advance_profile_generation_unlocked()
            path = self.groups_dir / gid / "local_user_profiles.db"
            if not path.is_file():
                return False
            with _connect(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
                conn.commit()
        return cursor.rowcount > 0

    def upsert_core_profile(
        self,
        *,
        user_id: str,
        profile_text: str,
        profile_json: dict[str, Any] | None = None,
        source: str = "persona_service",
    ) -> None:
        with self.maintenance_lock:
            payload = json.dumps(profile_json or {}, ensure_ascii=False)
            with _connect(self.shared_dir / "core_user_profiles.db") as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO profiles(user_id, profile_text, profile_json, source, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        profile_text=excluded.profile_text,
                        profile_json=excluded.profile_json,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    (str(user_id or ""), str(profile_text or ""), payload, str(source or ""), time.time()),
                )
                conn.commit()

    def atomic_patch_core_profile(
        self,
        *,
        user_id: str,
        patcher: Callable[[dict[str, Any]], dict[str, Any]],
        profile_text: str | None = None,
        source: str = "",
        source_if_missing: str = "profile_patch",
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Patch one core profile without overwriting concurrent JSON fields."""
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")
        with self.maintenance_lock:
            self._check_profile_generation_unlocked(expected_generation)
            with _connect(self.shared_dir / "core_user_profiles.db") as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT profile_text, profile_json, source FROM profiles WHERE user_id=?",
                    (uid,),
                ).fetchone()
                current_json = _json_loads(row["profile_json"] if row else "{}", {})
                if not isinstance(current_json, dict):
                    current_json = {}
                patched_json = patcher(dict(current_json))
                if not isinstance(patched_json, dict):
                    conn.rollback()
                    raise TypeError("profile patcher must return a dict")
                next_text = str(profile_text) if profile_text is not None else str(row["profile_text"] or "") if row else ""
                existing_source = str(row["source"] or "") if row else ""
                next_source = (
                    str(source or "").strip()
                    or existing_source
                    or str(source_if_missing or "profile_patch")
                )
                updated_at = time.time()
                conn.execute(
                    """
                    INSERT INTO profiles(user_id, profile_text, profile_json, source, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        profile_text=excluded.profile_text,
                        profile_json=excluded.profile_json,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    (
                        uid,
                        next_text,
                        json.dumps(patched_json, ensure_ascii=False),
                        next_source,
                        updated_at,
                    ),
                )
                conn.commit()
        return {
            "profile_text": next_text,
            "profile_json": patched_json,
            "source": next_source,
            "updated_at": updated_at,
        }

    def get_core_profile(self, user_id: str) -> dict[str, Any] | None:
        with _connect(self.shared_dir / "core_user_profiles.db") as conn:
            row = conn.execute(
                "SELECT profile_text, profile_json, source, updated_at FROM profiles WHERE user_id=?",
                (str(user_id or ""),),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["profile_json"] or "{}")
        except Exception:
            payload = {}
        return {
            "profile_text": str(row["profile_text"] or ""),
            "profile_json": payload if isinstance(payload, dict) else {},
            "source": str(row["source"] or ""),
            "updated_at": float(row["updated_at"] or 0),
        }

    def delete_core_profile(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")
        path = self.shared_dir / "core_user_profiles.db"
        with self.maintenance_lock:
            self._advance_profile_generation_unlocked()
            if not path.is_file():
                return False
            with _connect(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
                conn.commit()
        return cursor.rowcount > 0

    def list_groups(self) -> list[str]:
        """枚举 groups_dir 下所有已建立空间的 group_id。"""
        if not self.groups_dir.exists():
            return []
        out: list[str] = []
        for entry in self.groups_dir.iterdir():
            if entry.is_dir():
                out.append(entry.name)
        out.sort()
        return out

    def list_core_profiles(self) -> list[dict[str, Any]]:
        path = self.shared_dir / "core_user_profiles.db"
        if not path.exists():
            return []
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT user_id, profile_text, profile_json, source, updated_at "
                "FROM profiles ORDER BY updated_at DESC"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["profile_json"] or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "user_id": str(row["user_id"] or ""),
                    "profile_text": str(row["profile_text"] or ""),
                    "profile_json": payload if isinstance(payload, dict) else {},
                    "source": str(row["source"] or ""),
                    "updated_at": float(row["updated_at"] or 0),
                }
            )
        return out

    def list_local_profiles(self, group_id: str) -> list[dict[str, Any]]:
        safe_group_id = str(group_id or "").strip()
        if not safe_group_id:
            return []
        group_dir = self.groups_dir / safe_group_id
        path = group_dir / "local_user_profiles.db"
        if not path.exists():
            return []
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT user_id, profile_text, profile_json, updated_at "
                "FROM profiles ORDER BY updated_at DESC"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["profile_json"] or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "user_id": str(row["user_id"] or ""),
                    "profile_text": str(row["profile_text"] or ""),
                    "profile_json": payload if isinstance(payload, dict) else {},
                    "updated_at": float(row["updated_at"] or 0),
                }
            )
        return out

    def clear_all_profiles(self) -> dict[str, int]:
        counts = {"core_profiles": 0, "local_profiles": 0}
        with self.maintenance_lock:
            self._advance_profile_generation_unlocked()
            core_path = self.shared_dir / "core_user_profiles.db"
            if core_path.is_file():
                with _connect(core_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute("DELETE FROM profiles")
                    counts["core_profiles"] = max(0, cursor.rowcount)
                    conn.commit()

            for group_id in self.list_groups():
                local_path = self.groups_dir / group_id / "local_user_profiles.db"
                if not local_path.is_file():
                    continue
                with _connect(local_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute("DELETE FROM profiles")
                    counts["local_profiles"] += max(0, cursor.rowcount)
                    conn.commit()
        return counts

    def purge_user_profile_data(self, user_id: str) -> dict[str, int]:
        """Irreversibly remove one user's profiles and memory-palace items.

        The shared maintenance lock and generation fence ensure an in-flight scoped
        profile write cannot resurrect data after this operation completes.
        """

        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")
        counts = {
            "core_profiles": 0,
            "local_profiles": 0,
            "memory_items": 0,
        }
        with self.maintenance_lock:
            self._advance_profile_generation_unlocked()
            core_path = self.shared_dir / "core_user_profiles.db"
            if core_path.is_file():
                with _connect(core_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
                    counts["core_profiles"] = max(0, int(cursor.rowcount or 0))
                    conn.commit()

            for group_id in self.list_groups():
                local_path = self.groups_dir / group_id / "local_user_profiles.db"
                if not local_path.is_file():
                    continue
                with _connect(local_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
                    counts["local_profiles"] += max(0, int(cursor.rowcount or 0))
                    conn.commit()

            palace_path = self.memory_palace_dir / "memory_palace.db"
            if palace_path.is_file():
                with _connect(palace_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    rows = conn.execute(
                        "SELECT memory_id FROM memory_items WHERE user_id=?",
                        (uid,),
                    ).fetchall()
                    memory_ids = [str(row["memory_id"] or "") for row in rows]
                    for memory_id in memory_ids:
                        conn.execute(
                            "DELETE FROM memory_relations WHERE source_memory_id=? OR target_ref=?",
                            (memory_id, memory_id),
                        )
                        conn.execute("DELETE FROM memory_entities WHERE memory_id=?", (memory_id,))
                        conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
                        conn.execute("DELETE FROM memory_vector_chunks WHERE memory_id=?", (memory_id,))
                        if self._fts_available:
                            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                    cursor = conn.execute("DELETE FROM memory_items WHERE user_id=?", (uid,))
                    counts["memory_items"] = max(0, int(cursor.rowcount or 0))
                    conn.commit()
        return counts

    def get_profile_generation(self) -> int:
        with self.maintenance_lock:
            return _PROFILE_GENERATIONS.get(self._profile_generation_key, 0)

    def advance_profile_generation(self) -> int:
        with self.maintenance_lock:
            return self._advance_profile_generation_unlocked()

    def _advance_profile_generation_unlocked(self) -> int:
        next_generation = _PROFILE_GENERATIONS.get(self._profile_generation_key, 0) + 1
        _PROFILE_GENERATIONS[self._profile_generation_key] = next_generation
        return next_generation

    def _check_profile_generation_unlocked(self, expected_generation: int | None) -> None:
        if expected_generation is None:
            return
        current_generation = _PROFILE_GENERATIONS.get(self._profile_generation_key, 0)
        if int(expected_generation) != current_generation:
            raise ProfileGenerationConflict(
                expected_generation=int(expected_generation),
                current_generation=current_generation,
            )

    def write_memory_item(self, item: dict[str, Any]) -> str:
        with self.maintenance_lock:
            return self._write_memory_item_unlocked(item)

    def _write_memory_item_unlocked(self, item: dict[str, Any]) -> str:
        payload = self._normalize_memory_item(item)
        memory_id = str(payload["memory_id"])
        searchable_text = self._build_searchable_text(payload)
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            # Use an IMMEDIATE transaction so revision read + write stays serialized
            # across concurrent writers and behaves like SQLite-flavored CAS.
            conn.execute("BEGIN IMMEDIATE")
            current = self._get_memory_payload(memory_id, conn=conn)
            if current is not None:
                prev_revision = int(current.get("revision", 0) or 0)
                next_revision = int(payload.get("revision", prev_revision) or prev_revision)
                if next_revision < prev_revision:
                    conn.rollback()
                    return memory_id
                payload["revision"] = max(prev_revision + 1, next_revision)
            created_at = float(payload.get("time_created", time.time()) or time.time())
            updated_at = time.time()
            before_changes = conn.total_changes
            conn.execute(
                """
                INSERT INTO memory_items(memory_id, memory_type, palace_zone, summary, aliases, topic_tags, entity_tags, snippets,
                                         user_id, group_id, thread_id, payload, created_at, updated_at, salience,
                                         stability, confidence, supports_recall, supports_autofill, expires_at,
                                         revision, tone_risk, irony_risk, group_scope, cross_group_allowed,
                                         time_sensitivity, superseded_by, reinforcement_count, last_accessed_at, access_count,
                                         permission_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    memory_type=excluded.memory_type,
                    palace_zone=excluded.palace_zone,
                    summary=excluded.summary,
                    aliases=excluded.aliases,
                    topic_tags=excluded.topic_tags,
                    entity_tags=excluded.entity_tags,
                    snippets=excluded.snippets,
                    user_id=excluded.user_id,
                    group_id=excluded.group_id,
                    thread_id=excluded.thread_id,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at,
                    salience=excluded.salience,
                    stability=excluded.stability,
                    confidence=excluded.confidence,
                    supports_recall=excluded.supports_recall,
                    supports_autofill=excluded.supports_autofill,
                    expires_at=excluded.expires_at,
                    revision=excluded.revision,
                    tone_risk=excluded.tone_risk,
                    irony_risk=excluded.irony_risk,
                    group_scope=excluded.group_scope,
                    cross_group_allowed=excluded.cross_group_allowed,
                    time_sensitivity=excluded.time_sensitivity,
                    superseded_by=excluded.superseded_by,
                    reinforcement_count=excluded.reinforcement_count,
                    last_accessed_at=excluded.last_accessed_at,
                    access_count=excluded.access_count,
                    permission_type=excluded.permission_type
                WHERE excluded.revision >= memory_items.revision
                """,
                (
                    memory_id,
                    payload["memory_type"],
                    payload["palace_zone"],
                    payload["summary"],
                    json.dumps(payload.get("aliases", []), ensure_ascii=False),
                    json.dumps(payload.get("topic_tags", []), ensure_ascii=False),
                    json.dumps(payload.get("entity_tags", []), ensure_ascii=False),
                    json.dumps(payload.get("snippets", []), ensure_ascii=False),
                    payload["user_id"],
                    payload["group_id"],
                    payload["thread_id"],
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                    updated_at,
                    float(payload.get("salience", 0) or 0),
                    float(payload.get("stability", 0) or 0),
                    float(payload.get("confidence", 0) or 0),
                    1 if bool(payload.get("supports_recall", True)) else 0,
                    1 if bool(payload.get("supports_autofill", False)) else 0,
                    float(payload.get("expires_at", 0) or 0),
                    int(payload.get("revision", 1) or 1),
                    float(payload.get("tone_risk", 0) or 0),
                    float(payload.get("irony_risk", 0) or 0),
                    str(payload.get("group_scope", "shared") or "shared"),
                    1 if bool(payload.get("cross_group_allowed", False)) else 0,
                    str(payload.get("time_sensitivity", "normal") or "normal"),
                    str(payload.get("superseded_by", "") or ""),
                    int(payload.get("reinforcement_count", 0) or 0),
                    float(payload.get("last_accessed_at", 0) or 0),
                    int(payload.get("access_count", 0) or 0),
                    str(payload.get("permission_type", "public_preference") or "public_preference"),
                ),
            )
            if conn.total_changes == before_changes:
                conn.rollback()
                return memory_id
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
            conn.execute(
                """
                INSERT INTO memory_embeddings(memory_id, model_version, embedding, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, EMBED_MODEL_VERSION, json.dumps(payload.get("_embedding", []), ensure_ascii=False), updated_at),
            )
            conn.execute(
                "UPDATE memory_items SET embedding=?, embedding_model=? WHERE memory_id=?",
                (_pack_float16_vector(payload.get("_embedding", [])), EMBED_MODEL_VERSION, memory_id),
            )
            self._write_vector_chunks(conn, payload=payload, updated_at=updated_at)
            conn.execute("DELETE FROM memory_entities WHERE memory_id=?", (memory_id,))
            for entity in payload.get("_entities", []):
                conn.execute(
                    """
                    INSERT INTO memory_entities(entity, memory_id, entity_type, weight, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (entity, memory_id, "tag", 1.0, updated_at),
                )
            conn.execute("DELETE FROM memory_relations WHERE source_memory_id=?", (memory_id,))
            self._write_relations(conn, payload)
            if self._fts_available:
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                conn.execute(
                    """
                    INSERT INTO memory_fts(memory_id, summary, aliases, topic_tags, entity_tags, snippets)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        self._fts_projection(payload["summary"]),
                        self._fts_projection(payload.get("aliases", [])),
                        self._fts_projection(payload.get("topic_tags", [])),
                        self._fts_projection(payload.get("entity_tags", [])),
                        self._fts_projection(payload.get("snippets", [])),
                    ),
                )
            if searchable_text:
                conn.execute(
                    """
                    INSERT INTO memory_clusters(cluster_id, cluster_type, label, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cluster_id) DO UPDATE SET
                        label=excluded.label,
                        payload=excluded.payload,
                        updated_at=excluded.updated_at
                    """,
                    (
                        f"topic:{payload['group_id'] or 'global'}",
                        "topic",
                        (payload.get("topic_tags") or ["recent"])[0],
                        json.dumps({"last_memory_id": memory_id}, ensure_ascii=False),
                        updated_at,
                    ),
                )
            conn.commit()
        return memory_id

    def recall_memories(
        self,
        *,
        query: str,
        scope: str = "auto",
        user_id: str = "",
        group_id: str = "",
        limit: int = 0,
        mode: str = "auto",
        context_type: str = "auto",
    ) -> list[dict[str, Any]]:
        configured_limit = int(
            getattr(self.plugin_config, "personification_memory_recall_top_k", MAX_RECALL_LIMIT) or MAX_RECALL_LIMIT
        )
        limit = max(1, min(int(limit or configured_limit), MAX_RECALL_LIMIT))
        scan_limit = self._memory_search_scan_limit(limit=limit)
        normalized_query = normalize_text(query)
        if self.palace_enabled() and group_id and self.needs_bootstrap(group_id):
            self.bootstrap_group_memories(group_id)
        normalized_context_type = self._normalize_context_type(context_type, group_id=group_id)

        candidate_map: dict[str, MemorySearchCandidate] = {}
        for candidate in self._search_fast(
            query=normalized_query,
            scope=scope,
            user_id=user_id,
            group_id=group_id,
            limit=max(limit * 3, 12),
            scan_limit=scan_limit,
        ):
            existing = candidate_map.get(candidate.memory_id)
            if existing is None or candidate.base_score > existing.base_score:
                candidate_map[candidate.memory_id] = candidate
            elif existing is not None:
                existing.base_score = max(existing.base_score, candidate.base_score)
                for reason in candidate.match_reasons:
                    if reason not in existing.match_reasons:
                        existing.match_reasons.append(reason)

        ranked = sorted(candidate_map.values(), key=lambda item: item.base_score, reverse=True)
        deep_mode = mode == "deep" or (
            mode == "auto"
            and (
                len(ranked) <= 2
                or (ranked and ranked[0].base_score < 0.62)
                or query_looks_ambiguous(normalized_query)
            )
        )
        if deep_mode and ranked:
            # 当前 deep 模式仍是最小可用版：基于关系/实体/时间的启发式深度重排，
            # 不是完整的 LLM 深度 recall 策略器。
            ranked = self._rerank_deep(
                query=normalized_query,
                ranked=ranked[: max(limit * 2, 6)],
                group_id=group_id,
            )

        if not ranked:
            fallback = self._recall_item_fallback(
                group_id=group_id,
                user_id=user_id,
                scope=scope,
                query=normalized_query,
                limit=limit,
                scan_limit=scan_limit,
            )
            if len(fallback) < limit:
                fallback.extend(
                    self._recall_grouped_fallback(
                        group_id=group_id,
                        query=normalized_query,
                        limit=limit - len(fallback),
                    )
                )
            fallback = self._filter_recalled_by_permission(fallback, context_type=normalized_context_type)
            self._record_search_stats(
                query=normalized_query,
                scope=scope,
                mode="fallback",
                requested_group_id=group_id,
                requested_user_id=user_id,
                hit_count=len(fallback),
                used_memory_ids=[],
                status="fallback",
            )
            return fallback

        results: list[dict[str, Any]] = []
        used_ids: list[str] = []
        for candidate in ranked:
            payload = dict(candidate.payload)
            filtered_payload = self._permission_filtered_payload(payload, context_type=normalized_context_type)
            if filtered_payload is None:
                continue
            payload.pop("_query", None)
            payload.pop("_embedding", None)
            payload.pop("_entities", None)
            payload = {**payload, **filtered_payload}
            payload["why_relevant"] = "；".join(candidate.match_reasons[:4])
            payload["score"] = round(candidate.base_score, 4)
            payload["time_hint"] = build_time_hint(safe_float(payload.get("time_created", 0), 0))
            payload["search_source"] = candidate.source
            results.append(payload)
            used_ids.append(candidate.memory_id)
            self._mark_memory_accessed(candidate.memory_id)
            if len(results) >= limit:
                break

        self._record_search_stats(
            query=normalized_query,
            scope=scope,
            mode="deep" if deep_mode else "fast",
            requested_group_id=group_id,
            requested_user_id=user_id,
            hit_count=len(results),
            used_memory_ids=used_ids,
            status="ok",
        )
        return results

    def _memory_search_scan_limit(self, *, limit: int) -> int:
        configured = safe_float(
            getattr(self.plugin_config, "personification_memory_search_scan_limit", 800),
            800,
        )
        floor = max(int(limit or 1) * 8, 80)
        return max(floor, min(int(configured or 800), 5000))

    def _recall_item_fallback(
        self,
        *,
        group_id: str,
        user_id: str,
        scope: str,
        query: str,
        limit: int,
        scan_limit: int,
    ) -> list[dict[str, Any]]:
        query_tokens = [token for token in tokenize(query) if token]
        if not query_tokens:
            return []
        normalized_query = normalize_text(query).lower()
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM memory_items
                WHERE supports_recall=1
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(limit * 8, min(int(scan_limit or 0), 5000), 48),),
            ).fetchall()

        scored: list[tuple[float, dict[str, Any]]] = []
        requested_group_id = str(group_id or "")
        requested_user_id = str(user_id or "")
        for row in rows:
            payload = _json_loads(row["payload"], {})
            if not isinstance(payload, dict) or not self._scope_matches(payload, scope):
                continue
            payload_group_id = str(payload.get("group_id") or "")
            payload_user_id = str(payload.get("user_id") or "")
            cross_group_allowed = bool(payload.get("cross_group_allowed", False))
            if requested_group_id and payload_group_id and payload_group_id != requested_group_id and not cross_group_allowed:
                continue
            if requested_user_id and payload_user_id and payload_user_id != requested_user_id:
                continue

            searchable = normalize_text(self._build_searchable_text(payload)).lower()
            if not searchable:
                continue
            searchable_tokens = set(tokenize(searchable))
            token_overlap = len(set(query_tokens) & searchable_tokens)
            substring_hits = sum(1 for token in query_tokens if token and token in searchable)
            if token_overlap <= 0 and substring_hits <= 0 and normalized_query not in searchable:
                continue
            base_score = 0.24 + min(token_overlap, 5) * 0.08 + min(substring_hits, 5) * 0.05
            payload = dict(payload)
            payload["why_relevant"] = "记忆文本兜底命中"
            payload["score"] = round(
                rank_memory_payload(
                    payload,
                    query=query,
                    base_score=base_score,
                    requested_group_id=requested_group_id,
                    requested_user_id=requested_user_id,
                ),
                4,
            )
            payload["time_hint"] = build_time_hint(safe_float(payload.get("time_created", 0), 0))
            payload["search_source"] = "item_fallback"
            payload.pop("_embedding", None)
            payload.pop("_entities", None)
            scored.append((float(payload["score"]), payload))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def needs_bootstrap(self, group_id: str) -> bool:
        with _connect(self.memory_palace_dir / "bootstrap_state.db") as conn:
            row = conn.execute(
                "SELECT 1 FROM bootstrap_groups WHERE group_id=?",
                (str(group_id or ""),),
            ).fetchone()
        return row is None

    def mark_bootstrapped(self, group_id: str, summary: str = "") -> None:
        with _connect(self.memory_palace_dir / "bootstrap_state.db") as conn:
            conn.execute(
                """
                INSERT INTO bootstrap_groups(group_id, summary, bootstrapped_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    summary=excluded.summary,
                    bootstrapped_at=excluded.bootstrapped_at
                """,
                (str(group_id or ""), str(summary or ""), time.time()),
            )
            conn.commit()

    def bootstrap_group_memories(self, group_id: str) -> None:
        group_id = str(group_id or "").strip()
        if not group_id or not self.palace_enabled() or not self.needs_bootstrap(group_id):
            return
        group_dir = self.ensure_group_space(group_id)
        summaries: list[str] = []
        with _connect(group_dir / "chat_history.db") as conn:
            rows = conn.execute(
                """
                SELECT content, metadata, created_at
                FROM messages
                ORDER BY id DESC
                LIMIT 24
                """
            ).fetchall()
        for row in reversed(rows):
            content = _json_loads(row["content"], row["content"])
            metadata = _json_loads(row["metadata"], {})
            text = ""
            if isinstance(content, list):
                parts = [str(part.get("text", "")).strip() for part in content if isinstance(part, dict)]
                text = " ".join(part for part in parts if part)
            elif isinstance(content, dict):
                text = str(content.get("text", "") or content.get("content", "")).strip()
            else:
                text = str(content or "").strip()
            if not text:
                continue
            speaker = str(metadata.get("user_id", "") or "")
            if speaker:
                summaries.append(f"{speaker}: {text[:120]}")
            else:
                summaries.append(text[:120])
        if summaries:
            self.write_memory_item(
                {
                    "memory_id": f"bootstrap:{group_id}",
                    "memory_type": "group",
                    "palace_zone": "group",
                    "summary": " | ".join(summaries[:8]),
                    "source_kind": "bootstrap",
                    "source_refs": [f"group:{group_id}"],
                    "group_id": group_id,
                    "topic_tags": ["bootstrap", "recent"],
                    "entity_tags": [],
                    "supports_recall": True,
                    "supports_autofill": False,
                    "salience": 0.6,
                    "stability": 0.45,
                    "confidence": 0.65,
                    "time_sensitivity": "normal",
                    "group_scope": "isolated",
                    "cross_group_allowed": False,
                    "revision": 1,
                }
            )
        self.mark_bootstrapped(group_id, "group recent context")

    def report_memory_feedback(
        self,
        *,
        memory_id: str,
        outcome: str,
        superseded_by: str = "",
    ) -> None:
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            return
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            payload = self._get_memory_payload(memory_id, conn=conn)
            if not payload:
                return
            if outcome == "used":
                payload["access_count"] = int(payload.get("access_count", 0) or 0) + 1
                payload["last_accessed_at"] = now_ts()
                payload["reinforcement_count"] = int(payload.get("reinforcement_count", 0) or 0) + 1
            elif outcome == "verified":
                payload["confidence"] = min(1.0, safe_float(payload.get("confidence", 0.5), 0.5) + 0.08)
                payload["stability"] = min(1.0, safe_float(payload.get("stability", 0.4), 0.4) + 0.06)
                payload["reinforcement_count"] = int(payload.get("reinforcement_count", 0) or 0) + 1
            elif outcome == "wrong":
                payload["confidence"] = max(0.0, safe_float(payload.get("confidence", 0.5), 0.5) - 0.25)
                payload["stability"] = max(0.0, safe_float(payload.get("stability", 0.4), 0.4) - 0.18)
            if superseded_by:
                payload["superseded_by"] = str(superseded_by)
            payload["revision"] = int(payload.get("revision", 1) or 1) + 1
        self.write_memory_item(payload)

    def get_memory_item(self, memory_id: str) -> dict[str, Any] | None:
        return self._get_memory_payload(str(memory_id or ""))

    def get_vector_index_status(self) -> dict[str, Any]:
        db_path = self.memory_palace_dir / "memory_palace.db"
        status: dict[str, Any] = {
            "enabled": self._vector_index_enabled(),
            "backend": str(getattr(self.plugin_config, "personification_memory_vector_backend", "sqlite_exact") or "sqlite_exact"),
            "model_version": VECTOR_CHUNK_MODEL_VERSION,
            "memory_count": 0,
            "chunk_count": 0,
            "stale_count": 0,
            "available": db_path.exists(),
        }
        if not db_path.exists():
            return status
        with _connect(db_path) as conn:
            memory_row = conn.execute("SELECT COUNT(1) AS cnt FROM memory_items WHERE supports_recall=1").fetchone()
            chunk_row = conn.execute("SELECT COUNT(1) AS cnt FROM memory_vector_chunks").fetchone()
            stale_row = conn.execute(
                """
                SELECT COUNT(1) AS cnt
                FROM memory_items i
                WHERE i.supports_recall=1
                  AND NOT EXISTS (
                    SELECT 1 FROM memory_vector_chunks c
                    WHERE c.memory_id=i.memory_id AND c.model_version=?
                  )
                """,
                (VECTOR_CHUNK_MODEL_VERSION,),
            ).fetchone()
        status["memory_count"] = int(memory_row["cnt"] if memory_row else 0)
        status["chunk_count"] = int(chunk_row["cnt"] if chunk_row else 0)
        status["stale_count"] = int(stale_row["cnt"] if stale_row else 0)
        return status

    def rebuild_vector_index(self, *, limit: int = 0) -> dict[str, Any]:
        if not self.palace_enabled():
            return {"rebuilt": 0, "status": "disabled"}
        max_rows = int(limit or 0)
        sql = """
            SELECT payload
            FROM memory_items
            WHERE supports_recall=1
            ORDER BY updated_at DESC
        """
        params: tuple[Any, ...] = ()
        if max_rows > 0:
            sql += " LIMIT ?"
            params = (max_rows,)
        rebuilt = 0
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                payload = _json_loads(row["payload"], {})
                if not isinstance(payload, dict):
                    continue
                self._write_vector_chunks(conn, payload=payload, updated_at=now_ts())
                rebuilt += 1
            conn.commit()
        return {"rebuilt": rebuilt, "status": "ok", "index": self.get_vector_index_status()}

    def mark_memories_summarized(self, memory_ids: list[str], *, summarized_by: str = "") -> int:
        updated = 0
        for memory_id in list(memory_ids or [])[:80]:
            payload = self.get_memory_item(str(memory_id or ""))
            if not isinstance(payload, dict):
                continue
            if str(payload.get("source_kind", "") or "") == "crystal":
                continue
            payload["summary_status"] = "summarized"
            payload["summarized_at"] = now_ts()
            if summarized_by:
                refs = list(payload.get("summary_refs") or [])
                if summarized_by not in refs:
                    refs.append(str(summarized_by))
                payload["summary_refs"] = refs[-8:]
                payload["summarized_by"] = str(summarized_by)
            if str(payload.get("tier", "") or "").strip().lower() not in {"semantic", "background"}:
                payload["tier"] = "background"
            if str(payload.get("memory_type", "") or "") in {"episodic", "episodic_turn"}:
                payload["palace_zone"] = "background"
            payload["salience"] = max(0.12, min(safe_float(payload.get("salience", 0.4), 0.4), 0.42) * 0.72)
            payload["supports_recall"] = bool(payload.get("supports_recall", True))
            try:
                self.write_memory_item(payload)
                updated += 1
            except Exception:
                continue
        return updated

    def list_recent_memories(
        self,
        *,
        group_id: str = "",
        user_id: str = "",
        palace_zone: str = "",
        limit: int = 20,
        min_confidence: float = 0.0,
        source_kind: str = "",
        memory_type: str = "",
    ) -> list[dict[str, Any]]:
        if not self.palace_enabled():
            return []
        clauses = ["1=1"]
        params: list[Any] = []
        if group_id:
            clauses.append("(group_id=? OR group_id='')")
            params.append(str(group_id))
        if user_id:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        if palace_zone:
            clauses.append("palace_zone = ?")
            params.append(str(palace_zone))
        if source_kind:
            clauses.append("json_extract(payload, '$.source_kind') = ?")
            params.append(str(source_kind))
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(str(memory_type))
        clauses.append("confidence >= ?")
        params.append(float(min_confidence))
        params.append(max(1, int(limit or 20)))
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            rows = conn.execute(
                f"""
                SELECT payload
                FROM memory_items
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = _json_loads(row["payload"], {})
            if isinstance(payload, dict):
                items.append(payload)
        return items

    def list_related_memory_candidates(
        self,
        *,
        memory_id: str,
        group_id: str = "",
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        base = self.get_memory_item(memory_id)
        if not isinstance(base, dict):
            return []
        target_entities = {normalize_text(value) for value in base.get("entity_tags", []) if normalize_text(value)}
        target_topics = {normalize_text(value) for value in base.get("topic_tags", []) if normalize_text(value)}
        recent = self.list_recent_memories(group_id=group_id or str(base.get("group_id", "") or ""), limit=max(limit * 3, 24))
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in recent:
            candidate_id = str(item.get("memory_id", "") or "")
            if not candidate_id or candidate_id == str(memory_id):
                continue
            candidate_entities = {normalize_text(value) for value in item.get("entity_tags", []) if normalize_text(value)}
            candidate_topics = {normalize_text(value) for value in item.get("topic_tags", []) if normalize_text(value)}
            score = 0.0
            score += len(target_entities & candidate_entities) * 1.2
            score += len(target_topics & candidate_topics) * 0.8
            if str(item.get("group_id", "") or "") == str(base.get("group_id", "") or ""):
                score += 0.4
            if score <= 0:
                continue
            scored.append((score, item))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in scored[: max(1, int(limit or 12))]]

    def link_memories(
        self,
        *,
        source_memory_id: str,
        target_ref: str,
        relation_type: str,
        weight: float = 1.0,
    ) -> None:
        source_id = str(source_memory_id or "").strip()
        target = str(target_ref or "").strip()
        relation = str(relation_type or "").strip()
        if not source_id or not target or not relation:
            return
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            existing = conn.execute(
                """
                SELECT 1
                FROM memory_relations
                WHERE source_memory_id=? AND target_ref=? AND relation_type=?
                LIMIT 1
                """,
                (source_id, target, relation),
            ).fetchone()
            if existing is not None:
                return
            conn.execute(
                """
                INSERT INTO memory_relations(source_memory_id, target_ref, relation_type, weight, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, target, relation, float(weight), now_ts()),
            )
            conn.commit()

    def get_relations(
        self,
        *,
        source_memory_id: str,
        relation_type: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        source_id = str(source_memory_id or "").strip()
        if not source_id:
            return []
        clauses = ["source_memory_id=?"]
        params: list[Any] = [source_id]
        if relation_type:
            clauses.append("relation_type=?")
            params.append(str(relation_type))
        params.append(max(1, int(limit or 20)))
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            rows = conn.execute(
                f"""
                SELECT source_memory_id, target_ref, relation_type, weight, updated_at
                FROM memory_relations
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [
            {
                "source_memory_id": str(row["source_memory_id"] or ""),
                "target_ref": str(row["target_ref"] or ""),
                "relation_type": str(row["relation_type"] or ""),
                "weight": float(row["weight"] or 0),
                "updated_at": float(row["updated_at"] or 0),
            }
            for row in rows
        ]

    def prune_search_stats_now(self) -> int:
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            before_row = conn.execute("SELECT COUNT(1) AS cnt FROM memory_search_stats").fetchone()
            before = int(before_row["cnt"] if before_row else 0)
            self._prune_search_stats(conn, current_ts=now_ts())
            after_row = conn.execute("SELECT COUNT(1) AS cnt FROM memory_search_stats").fetchone()
            after = int(after_row["cnt"] if after_row else 0)
            conn.commit()
        return max(0, before - after)

    def get_memory_stats(self) -> dict[str, Any]:
        grouped_groups = 0
        if self.groups_dir.exists():
            grouped_groups = sum(1 for item in self.groups_dir.iterdir() if item.is_dir())
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            palace_row = conn.execute("SELECT COUNT(1) AS cnt FROM memory_items").fetchone()
            crystal_row = conn.execute(
                """
                SELECT COUNT(1) AS cnt
                FROM memory_items
                WHERE json_extract(payload, '$.source_kind')='crystal'
                """
            ).fetchone()
            search_row = conn.execute("SELECT COUNT(1) AS cnt FROM memory_search_stats").fetchone()
        with _connect(self.memory_palace_dir / "bootstrap_state.db") as conn:
            bootstrap_row = conn.execute("SELECT COUNT(1) AS cnt FROM bootstrap_groups").fetchone()
            recent_bootstraps = conn.execute(
                """
                SELECT group_id, summary, bootstrapped_at
                FROM bootstrap_groups
                ORDER BY bootstrapped_at DESC
                LIMIT 5
                """
            ).fetchall()
        return {
            "group_count": grouped_groups,
            "palace_count": int(palace_row["cnt"] if palace_row else 0),
            "crystal_count": int(crystal_row["cnt"] if crystal_row else 0),
            "search_stats_count": int(search_row["cnt"] if search_row else 0),
            "bootstrap_count": int(bootstrap_row["cnt"] if bootstrap_row else 0),
            "recent_bootstraps": [
                {
                    "group_id": str(row["group_id"] or ""),
                    "summary": str(row["summary"] or ""),
                    "bootstrapped_at": float(row["bootstrapped_at"] or 0),
                }
                for row in recent_bootstraps
            ],
            "grouped_memory_dir": str(self.grouped_memory_dir),
            "memory_palace_dir": str(self.memory_palace_dir),
        }

    def get_recall_stats(self, *, limit: int = 10) -> dict[str, Any]:
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            recent = conn.execute(
                """
                SELECT query, scope, mode, requested_group_id, requested_user_id, hit_count, used_memory_ids, status, created_at
                FROM memory_search_stats
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit or 10)),),
            ).fetchall()
            totals = conn.execute(
                """
                SELECT COUNT(1) AS total,
                       SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_count,
                       SUM(CASE WHEN status='fallback' THEN 1 ELSE 0 END) AS fallback_count
                FROM memory_search_stats
                """
            ).fetchone()
        return {
            "total": int(totals["total"] if totals and totals["total"] is not None else 0),
            "ok_count": int(totals["ok_count"] if totals and totals["ok_count"] is not None else 0),
            "fallback_count": int(
                totals["fallback_count"] if totals and totals["fallback_count"] is not None else 0
            ),
            "recent": [
                {
                    "query": str(row["query"] or ""),
                    "scope": str(row["scope"] or ""),
                    "mode": str(row["mode"] or ""),
                    "requested_group_id": str(row["requested_group_id"] or ""),
                    "requested_user_id": str(row["requested_user_id"] or ""),
                    "hit_count": int(row["hit_count"] or 0),
                    "used_memory_ids": _json_loads(row["used_memory_ids"], []),
                    "status": str(row["status"] or ""),
                    "created_at": float(row["created_at"] or 0),
                }
                for row in recent
            ],
        }

    def get_migration_status(self, *, limit: int = 10) -> dict[str, Any]:
        state_path = self.memory_palace_dir / "migration_state.db"
        with _connect(state_path) as conn:
            entries = conn.execute(
                """
                SELECT migration_key, status, updated_at
                FROM migration_entries
                ORDER BY updated_at DESC, migration_key ASC
                """
            ).fetchall()
            recent_files = conn.execute(
                """
                SELECT source_path, source_count, success_count, skipped_count, error_count,
                       target_group, target_db, recycle_status, updated_at
                FROM migration_files
                ORDER BY updated_at DESC, source_path ASC
                LIMIT ?
                """,
                (max(1, int(limit or 10)),),
            ).fetchall()
        return {
            "entries": [
                {
                    "migration_key": str(row["migration_key"] or ""),
                    "status": str(row["status"] or ""),
                    "updated_at": float(row["updated_at"] or 0),
                }
                for row in entries
            ],
            "recent_files": [
                {
                    "source_path": str(row["source_path"] or ""),
                    "source_count": int(row["source_count"] or 0),
                    "success_count": int(row["success_count"] or 0),
                    "skipped_count": int(row["skipped_count"] or 0),
                    "error_count": int(row["error_count"] or 0),
                    "target_group": str(row["target_group"] or ""),
                    "target_db": str(row["target_db"] or ""),
                    "recycle_status": str(row["recycle_status"] or ""),
                    "updated_at": float(row["updated_at"] or 0),
                }
                for row in recent_files
            ],
        }

    def _normalize_memory_item(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        payload["memory_id"] = str(payload.get("memory_id") or uuid.uuid4().hex)
        payload["memory_type"] = str(payload.get("memory_type") or "episodic")
        payload["palace_zone"] = str(payload.get("palace_zone") or payload["memory_type"])
        payload["summary"] = normalize_text(payload.get("summary", ""))[:800]
        payload["aliases"] = [normalize_text(value) for value in list(payload.get("aliases") or []) if normalize_text(value)]
        payload["topic_tags"] = [normalize_text(value) for value in list(payload.get("topic_tags") or []) if normalize_text(value)]
        payload["entity_tags"] = [normalize_text(value) for value in list(payload.get("entity_tags") or []) if normalize_text(value)]
        payload["snippets"] = [normalize_text(value)[:120] for value in list(payload.get("snippets") or []) if normalize_text(value)]
        payload["source_kind"] = str(payload.get("source_kind") or "conversation")
        payload["source_refs"] = list(payload.get("source_refs") or [])
        payload["user_id"] = str(payload.get("user_id") or "")
        payload["group_id"] = str(payload.get("group_id") or "")
        payload["thread_id"] = str(payload.get("thread_id") or "")
        payload["time_created"] = safe_float(payload.get("time_created", now_ts()), now_ts())
        payload["last_accessed_at"] = safe_float(payload.get("last_accessed_at", 0), 0)
        payload["access_count"] = int(payload.get("access_count", 0) or 0)
        payload["confidence"] = min(1.0, max(0.0, safe_float(payload.get("confidence", 0.5), 0.5)))
        payload["salience"] = min(1.0, max(0.0, safe_float(payload.get("salience", 0.4), 0.4)))
        payload["stability"] = min(1.0, max(0.0, safe_float(payload.get("stability", 0.35), 0.35)))
        payload["emotional_weight"] = safe_float(payload.get("emotional_weight", 0), 0)
        payload["privacy_level"] = str(payload.get("privacy_level") or "default")
        payload["permission_type"] = self._normalize_permission_type(payload.get("permission_type"), payload=payload)
        payload["expires_at"] = safe_float(payload.get("expires_at", 0), 0)
        payload["supports_recall"] = bool(payload.get("supports_recall", True))
        payload["supports_autofill"] = bool(payload.get("supports_autofill", False))
        payload["revision"] = int(payload.get("revision", 1) or 1)
        payload["tone_risk"] = min(1.0, max(0.0, safe_float(payload.get("tone_risk", 0), 0)))
        payload["irony_risk"] = min(1.0, max(0.0, safe_float(payload.get("irony_risk", 0), 0)))
        payload["group_scope"] = str(payload.get("group_scope") or ("isolated" if payload["group_id"] else "shared"))
        payload["cross_group_allowed"] = bool(payload.get("cross_group_allowed", False))
        payload["time_sensitivity"] = str(payload.get("time_sensitivity") or "normal")
        payload["conflict_refs"] = list(payload.get("conflict_refs") or [])
        payload["superseded_by"] = str(payload.get("superseded_by") or "")
        payload["reinforcement_count"] = int(payload.get("reinforcement_count", 0) or 0)
        # P4：初次写入或迁移时分配 tier；调用方显式设置的优先保留。
        try:
            from .memory_tier import assign_tier_on_write

            assign_tier_on_write(payload)
        except Exception:
            payload.setdefault("tier", "episodic")
        searchable = " ".join(
            [
                payload["summary"],
                " ".join(payload["aliases"]),
                " ".join(payload["topic_tags"]),
                " ".join(payload["entity_tags"]),
                " ".join(payload["snippets"]),
            ]
        )
        payload["_embedding"] = embed_text(searchable)
        payload["_entities"] = extract_entities(payload["summary"], payload["topic_tags"], payload["entity_tags"])
        return payload

    def _normalize_permission_type(self, value: Any, *, payload: dict[str, Any]) -> str:
        normalized = str(value or "").strip().lower()
        allowed = {
            "public_preference",
            "private_fact",
            "group_meme",
            "conflict_memory",
            "sensitive_memory",
        }
        if normalized in allowed:
            return normalized
        memory_type = str(payload.get("memory_type", "") or "").strip().lower()
        source_kind = str(payload.get("source_kind", "") or "").strip().lower()
        privacy_level = str(payload.get("privacy_level", "") or "").strip().lower()
        if privacy_level in {"private", "sensitive"}:
            return "private_fact" if privacy_level == "private" else "sensitive_memory"
        if memory_type in {"group_meme", "concept_anchor"}:
            return "group_meme"
        if memory_type in {"conflict_memory"} or source_kind in {"conflict", "avoidance"}:
            return "conflict_memory"
        if not payload.get("group_id") and payload.get("user_id"):
            return "private_fact"
        return "public_preference"

    def _normalize_context_type(self, context_type: str, *, group_id: str = "") -> str:
        normalized = str(context_type or "").strip().lower()
        if normalized in {"group", "private"}:
            return normalized
        return "group" if str(group_id or "").strip() else "private"

    def _permission_filtered_payload(
        self,
        payload: dict[str, Any],
        *,
        context_type: str,
    ) -> dict[str, Any] | None:
        permission = self._normalize_permission_type(payload.get("permission_type"), payload=payload)
        if context_type != "group":
            payload["permission_type"] = permission
            return payload
        if permission in {"private_fact", "sensitive_memory"}:
            return None
        if permission == "conflict_memory":
            summary = str(payload.get("summary", "") or "").strip()
            return {
                **payload,
                "summary": "此话题可能敏感，回复要中性克制，不要复述具体细节。",
                "raw_summary_redacted": True,
                "permission_type": permission,
                "permission_note": "conflict_memory redacted for group context",
                "snippets": [],
                "aliases": [],
                "entity_tags": [],
                "topic_tags": [],
            }
        payload["permission_type"] = permission
        return payload

    def _filter_recalled_by_permission(
        self,
        items: list[dict[str, Any]],
        *,
        context_type: str,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            payload = self._permission_filtered_payload(dict(item), context_type=context_type)
            if payload is not None:
                filtered.append(payload)
        return filtered

    def _get_memory_payload(
        self,
        memory_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if conn is None:
            with _connect(self.memory_palace_dir / "memory_palace.db") as own_conn:
                return self._get_memory_payload(memory_id, conn=own_conn)
        row = conn.execute("SELECT payload FROM memory_items WHERE memory_id=?", (str(memory_id or ""),)).fetchone()
        if not row:
            return None
        payload = _json_loads(row["payload"], {})
        return payload if isinstance(payload, dict) else None

    def _write_relations(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        memory_id = str(payload.get("memory_id") or "")
        for entity in payload.get("_entities", [])[:8]:
            conn.execute(
                """
                INSERT INTO memory_relations(source_memory_id, target_ref, relation_type, weight, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, str(entity), "same_entity", 1.0, now_ts()),
            )
        for conflict in list(payload.get("conflict_refs") or [])[:6]:
            conn.execute(
                """
                INSERT INTO memory_relations(source_memory_id, target_ref, relation_type, weight, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, str(conflict), "related_to", 0.6, now_ts()),
            )
        superseded_by = str(payload.get("superseded_by") or "")
        if superseded_by:
            conn.execute(
                """
                INSERT INTO memory_relations(source_memory_id, target_ref, relation_type, weight, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, superseded_by, "evolves", 0.9, now_ts()),
            )

    def _candidate_visible_for_request(
        self,
        payload: dict[str, Any],
        *,
        group_id: str,
        user_id: str,
    ) -> bool:
        requested_group_id = str(group_id or "")
        requested_user_id = str(user_id or "")
        payload_group_id = str(payload.get("group_id") or "")
        payload_user_id = str(payload.get("user_id") or "")
        cross_group_allowed = bool(payload.get("cross_group_allowed", False))
        if requested_group_id and payload_group_id and payload_group_id != requested_group_id and not cross_group_allowed:
            return False
        if requested_user_id and payload_user_id and payload_user_id != requested_user_id:
            return False
        return True

    def _candidate_from_payload(
        self,
        payload: dict[str, Any],
        *,
        base_score: float,
        reason: str,
        source: str,
        requested_group_id: str,
        requested_user_id: str,
    ) -> MemorySearchCandidate:
        score = rank_memory_payload(
            payload,
            query=payload.get("_query", ""),
            base_score=base_score,
            requested_group_id=requested_group_id,
            requested_user_id=requested_user_id,
        )
        return MemorySearchCandidate(
            memory_id=str(payload.get("memory_id") or ""),
            payload=payload,
            base_score=score,
            match_reasons=[reason],
            source=source,
        )

    def _build_searchable_text(self, payload: dict[str, Any]) -> str:
        return " ".join(
            [
                str(payload.get("summary", "") or ""),
                " ".join(str(value) for value in list(payload.get("aliases") or []) if str(value).strip()),
                " ".join(str(value) for value in list(payload.get("topic_tags") or []) if str(value).strip()),
                " ".join(str(value) for value in list(payload.get("entity_tags") or []) if str(value).strip()),
                " ".join(str(value) for value in list(payload.get("snippets") or []) if str(value).strip()),
            ]
        ).strip()

    def _embedding_for_payload(
        self,
        *,
        conn: sqlite3.Connection,
        payload: dict[str, Any],
        row: sqlite3.Row,
    ) -> list[float]:
        raw_embedding = _json_loads(row["embedding"], [])
        model_version = str(row["model_version"] or "")
        if isinstance(raw_embedding, list) and model_version == EMBED_MODEL_VERSION:
            return raw_embedding

        refreshed = embed_text(self._build_searchable_text(payload))
        memory_id = str(payload.get("memory_id") or "")
        if memory_id:
            conn.execute(
                """
                INSERT INTO memory_embeddings(memory_id, model_version, embedding, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    model_version=excluded.model_version,
                    embedding=excluded.embedding,
                    updated_at=excluded.updated_at
                """,
                (
                    memory_id,
                    EMBED_MODEL_VERSION,
                    json.dumps(refreshed, ensure_ascii=False),
                    now_ts(),
                ),
            )
            conn.execute(
                "UPDATE memory_items SET embedding=?, embedding_model=? WHERE memory_id=?",
                (_pack_float16_vector(refreshed), EMBED_MODEL_VERSION, memory_id),
            )
        return refreshed

    def _fts_projection(self, value: Any) -> str:
        if isinstance(value, list):
            source = " ".join(str(item or "").strip() for item in value if str(item or "").strip())
        else:
            source = str(value or "").strip()
        if not source:
            return ""
        tokens = tokenize(source)
        if not tokens:
            return normalize_text(source)
        return " ".join(tokens)

    def _scope_matches(self, payload: dict[str, Any], scope: str) -> bool:
        normalized_scope = str(scope or "auto").strip().lower()
        if normalized_scope in {"", "auto"}:
            return True
        memory_type = str(payload.get("memory_type", "") or "").lower()
        palace_zone = str(payload.get("palace_zone", "") or "").lower()
        if normalized_scope == "recent_episode":
            return memory_type == "episodic" or palace_zone in {"recent_episode", "working"}
        if normalized_scope == "person":
            return memory_type == "social" or palace_zone == "person"
        if normalized_scope == "group":
            return memory_type == "group" or palace_zone == "group"
        if normalized_scope == "topic":
            return memory_type == "semantic" or palace_zone == "topic"
        if normalized_scope == "self":
            return memory_type == "self_model" or palace_zone == "self"
        if normalized_scope == "future":
            return memory_type == "prospective" or palace_zone == "future"
        return True

    def _search_fast(
        self,
        *,
        query: str,
        scope: str,
        user_id: str,
        group_id: str,
        limit: int,
        scan_limit: int,
    ) -> list[MemorySearchCandidate]:
        groups = [
            self._search_by_vector_chunks(
                query=query,
                group_id=group_id,
                user_id=user_id,
                scope=scope,
                limit=max(limit, 20),
                scan_limit=scan_limit,
            ),
            self._search_by_fts(
                query=query,
                group_id=group_id,
                user_id=user_id,
                scope=scope,
                limit=max(limit, 20),
                scan_limit=scan_limit,
            ),
            self._search_by_embedding(
                query=query,
                group_id=group_id,
                user_id=user_id,
                scope=scope,
                limit=max(limit, 20),
                scan_limit=scan_limit,
            ),
            self._search_by_entity(
                query=query,
                group_id=group_id,
                user_id=user_id,
                scope=scope,
                limit=max(limit, 10),
                scan_limit=scan_limit,
            ),
            self._search_by_time(
                query=query,
                group_id=group_id,
                user_id=user_id,
                scope=scope,
                limit=limit,
                scan_limit=scan_limit,
            ),
        ]
        return _fuse_recall_candidates(groups, limit=limit)

    def _rerank_deep(
        self,
        *,
        query: str,
        ranked: list[MemorySearchCandidate],
        group_id: str = "",
    ) -> list[MemorySearchCandidate]:
        query_tokens = set(tokenize(query))
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            for candidate in ranked:
                relation_hits = conn.execute(
                    """
                    SELECT COUNT(1)
                    FROM memory_relations
                    WHERE source_memory_id=? AND relation_type IN ('same_entity', 'evolves')
                    """,
                    (candidate.memory_id,),
                ).fetchone()
                candidate.base_score += min(int(relation_hits[0] if relation_hits else 0), 3) * 0.04
                entity_tokens = {normalize_text(value).lower() for value in candidate.payload.get("entity_tags", [])}
                if query_tokens and entity_tokens:
                    overlap = len(query_tokens & entity_tokens)
                    candidate.base_score += min(overlap, 3) * 0.05
                candidate.base_score += time_hint_score(
                    query,
                    safe_float(candidate.payload.get("time_created", 0), 0),
                    safe_float(candidate.payload.get("expires_at", 0), 0),
                    str(candidate.payload.get("time_sensitivity", "normal") or "normal"),
                )
                candidate.base_score = round(candidate.base_score, 6)
                if "deep_rerank" not in candidate.match_reasons:
                    candidate.match_reasons.append("deep_rerank")
        return sorted(ranked, key=lambda item: item.base_score, reverse=True)

    def _vector_index_enabled(self) -> bool:
        if not bool(getattr(self.plugin_config, "personification_memory_rag_enabled", True)):
            return False
        backend = str(getattr(self.plugin_config, "personification_memory_vector_backend", "sqlite_exact") or "")
        return backend.strip().lower() in {"", "sqlite", "sqlite_exact", "local"}

    def _vector_content_hash(self, text: str) -> str:
        return hashlib.blake2b(
            normalize_text(text).encode("utf-8"),
            digest_size=16,
            person=b"pers-rag-v1",
        ).hexdigest()

    def _build_vector_chunks(self, payload: dict[str, Any]) -> list[tuple[str, str]]:
        memory_id = str(payload.get("memory_id") or "").strip()
        if not memory_id:
            return []
        raw_chunks = [
            self._build_searchable_text(payload),
            *[str(value or "") for value in list(payload.get("snippets") or [])],
        ]
        seen: set[str] = set()
        chunks: list[tuple[str, str]] = []
        for index, raw_text in enumerate(raw_chunks):
            text = normalize_text(raw_text)[:VECTOR_CHUNK_TEXT_LIMIT]
            if not text:
                continue
            digest = self._vector_content_hash(text)
            if digest in seen:
                continue
            seen.add(digest)
            chunks.append((f"{memory_id}:{index}:{digest[:8]}", text))
            if len(chunks) >= 4:
                break
        return chunks

    def _write_vector_chunks(
        self,
        conn: sqlite3.Connection,
        *,
        payload: dict[str, Any],
        updated_at: float,
    ) -> None:
        memory_id = str(payload.get("memory_id") or "").strip()
        if not memory_id:
            return
        conn.execute("DELETE FROM memory_vector_chunks WHERE memory_id=?", (memory_id,))
        if not self._vector_index_enabled() or not bool(payload.get("supports_recall", True)):
            return
        for chunk_id, text in self._build_vector_chunks(payload):
            embedding = embed_text(text)
            conn.execute(
                """
                INSERT INTO memory_vector_chunks(
                    chunk_id, memory_id, chunk_text, content_hash, model_version,
                    embedding, embedding_dim, group_id, user_id, permission_type,
                    group_scope, cross_group_allowed, salience, confidence, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    memory_id,
                    text,
                    self._vector_content_hash(text),
                    VECTOR_CHUNK_MODEL_VERSION,
                    json.dumps(embedding, ensure_ascii=False),
                    len(embedding),
                    str(payload.get("group_id") or ""),
                    str(payload.get("user_id") or ""),
                    str(payload.get("permission_type", "public_preference") or "public_preference"),
                    str(payload.get("group_scope", "shared") or "shared"),
                    1 if bool(payload.get("cross_group_allowed", False)) else 0,
                    float(payload.get("salience", 0) or 0),
                    float(payload.get("confidence", 0) or 0),
                    float(updated_at or now_ts()),
                ),
            )

    def _search_by_vector_chunks(
        self,
        *,
        query: str,
        group_id: str,
        user_id: str,
        scope: str,
        limit: int,
        scan_limit: int,
    ) -> list[MemorySearchCandidate]:
        if not query or not self._vector_index_enabled():
            return []
        query_embedding = embed_text(query)
        if not query_embedding:
            return []
        configured_candidates = safe_float(
            getattr(self.plugin_config, "personification_memory_rag_candidate_limit", 80),
            80,
        )
        scan = max(limit * 4, min(int(configured_candidates or 80), max(int(scan_limit or 800), 80)))
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            rows = conn.execute(
                """
                SELECT c.memory_id, c.chunk_text, c.embedding, c.model_version, i.payload
                FROM memory_vector_chunks c
                JOIN memory_items i ON i.memory_id = c.memory_id
                WHERE i.supports_recall=1
                  AND c.model_version=?
                ORDER BY c.salience DESC, c.updated_at DESC
                LIMIT ?
                """,
                (VECTOR_CHUNK_MODEL_VERSION, min(scan, 5000)),
            ).fetchall()
        best_by_memory: dict[str, MemorySearchCandidate] = {}
        for row in rows:
            payload = _json_loads(row["payload"], {})
            if not isinstance(payload, dict):
                continue
            if not self._scope_matches(payload, scope):
                continue
            if not self._candidate_visible_for_request(payload, group_id=group_id, user_id=user_id):
                continue
            embedding = _json_loads(row["embedding"], [])
            if not isinstance(embedding, list):
                continue
            similarity = cosine_similarity(query_embedding, embedding)
            if similarity <= 0.08:
                continue
            payload["_query"] = query
            candidate = self._candidate_from_payload(
                payload,
                base_score=0.2 + similarity * 0.76,
                reason="RAG向量块",
                source="vector",
                requested_group_id=group_id,
                requested_user_id=user_id,
            )
            chunk_text = str(row["chunk_text"] or "").strip()
            if chunk_text:
                candidate.match_reasons.append("chunk:" + chunk_text[:80])
            existing = best_by_memory.get(candidate.memory_id)
            if existing is None or candidate.base_score > existing.base_score:
                best_by_memory[candidate.memory_id] = candidate
        return sorted(best_by_memory.values(), key=lambda item: item.base_score, reverse=True)[: max(1, int(limit or 1))]

    def _search_by_fts(
        self,
        *,
        query: str,
        group_id: str,
        user_id: str,
        scope: str,
        limit: int,
        scan_limit: int,
    ) -> list[MemorySearchCandidate]:
        if not query:
            return []
        query_tokens = [token for token in tokenize(query) if token][:6]
        rows: list[sqlite3.Row] = []
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            if self._fts_available and query_tokens:
                try:
                    match_query = " OR ".join(query_tokens)
                    rows = conn.execute(
                        """
                        SELECT i.payload
                        FROM memory_fts f
                        JOIN memory_items i ON i.memory_id = f.memory_id
                        WHERE i.supports_recall=1
                          AND memory_fts MATCH ?
                        ORDER BY i.updated_at DESC
                        LIMIT ?
                        """,
                        (match_query, min(max(limit * 4, 40), max(int(scan_limit or 80), 80))),
                    ).fetchall()
                except Exception:
                    rows = []
            if not rows:
                rows = conn.execute(
                    """
                    SELECT payload
                    FROM memory_items
                    WHERE supports_recall=1
                      AND summary LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (f"%{query[:32]}%", min(max(limit * 4, 40), max(int(scan_limit or 80), 80))),
                ).fetchall()
        results: list[MemorySearchCandidate] = []
        query_token_set = set(tokenize(query))
        for row in rows:
            payload = _json_loads(row["payload"], {})
            if not isinstance(payload, dict):
                continue
            if not self._scope_matches(payload, scope):
                continue
            if not self._candidate_visible_for_request(payload, group_id=group_id, user_id=user_id):
                continue
            searchable = " ".join(
                [
                    payload.get("summary", ""),
                    " ".join(payload.get("aliases", [])),
                    " ".join(payload.get("topic_tags", [])),
                    " ".join(payload.get("entity_tags", [])),
                    " ".join(payload.get("snippets", [])),
                ]
            )
            tokens = set(tokenize(searchable))
            overlap = len(query_token_set & tokens)
            payload["_query"] = query
            results.append(
                self._candidate_from_payload(
                    payload,
                    base_score=0.34 + min(overlap, 6) * 0.08,
                    reason="全文命中",
                    source="fts",
                    requested_group_id=group_id,
                    requested_user_id=user_id,
                )
            )
        return results

    def _search_by_entity(
        self,
        *,
        query: str,
        group_id: str,
        user_id: str,
        scope: str,
        limit: int,
        scan_limit: int,
    ) -> list[MemorySearchCandidate]:
        entities = extract_entities(query, [], [])
        if not entities:
            return []
        placeholders = ",".join("?" for _ in entities)
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT i.payload
                FROM memory_entities e
                JOIN memory_items i ON i.memory_id = e.memory_id
                WHERE i.supports_recall=1
                  AND e.entity IN ({placeholders})
                ORDER BY i.updated_at DESC
                LIMIT ?
                """,
                (*entities, min(max(limit * 4, 40), max(int(scan_limit or 80), 80))),
            ).fetchall()
        results: list[MemorySearchCandidate] = []
        entity_set = {normalize_text(entity).lower() for entity in entities}
        for row in rows:
            payload = _json_loads(row["payload"], {})
            if not isinstance(payload, dict):
                continue
            if not self._scope_matches(payload, scope):
                continue
            if not self._candidate_visible_for_request(payload, group_id=group_id, user_id=user_id):
                continue
            payload_entities = {normalize_text(value).lower() for value in payload.get("entity_tags", [])}
            payload_entities |= {normalize_text(value).lower() for value in payload.get("aliases", [])}
            overlap = len(entity_set & payload_entities)
            payload["_query"] = query
            results.append(
                self._candidate_from_payload(
                    payload,
                    base_score=0.4 + min(overlap, 4) * 0.11,
                    reason="实体命中",
                    source="entity",
                    requested_group_id=group_id,
                    requested_user_id=user_id,
                )
            )
        return results

    def _search_by_embedding(
        self,
        *,
        query: str,
        group_id: str,
        user_id: str,
        scope: str,
        limit: int,
        scan_limit: int,
    ) -> list[MemorySearchCandidate]:
        query_embedding = embed_text(query)
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            row_map: dict[str, sqlite3.Row] = {}
            scan = max(int(scan_limit or 800), max(limit * 8, 80))
            queries = [
                (
                    """
                    SELECT i.memory_id, i.payload, e.embedding, e.model_version
                    FROM memory_embeddings e
                    JOIN memory_items i ON i.memory_id = e.memory_id
                    WHERE i.supports_recall=1
                    ORDER BY i.updated_at DESC
                    LIMIT ?
                    """,
                    (scan,),
                ),
                (
                    """
                    SELECT i.memory_id, i.payload, e.embedding, e.model_version
                    FROM memory_embeddings e
                    JOIN memory_items i ON i.memory_id = e.memory_id
                    WHERE i.supports_recall=1
                      AND (
                        i.memory_type IN ('semantic', 'persona_knowledge', 'group_knowledge', 'core_profile', 'fact')
                        OR i.palace_zone IN ('topic', 'profile', 'person', 'group', 'self')
                      )
                    ORDER BY i.salience DESC, i.updated_at DESC
                    LIMIT ?
                    """,
                    (max(limit * 10, scan // 2),),
                ),
                (
                    """
                    SELECT i.memory_id, i.payload, e.embedding, e.model_version
                    FROM memory_embeddings e
                    JOIN memory_items i ON i.memory_id = e.memory_id
                    WHERE i.supports_recall=1
                    ORDER BY i.salience DESC, i.updated_at DESC
                    LIMIT ?
                    """,
                    (max(limit * 10, scan // 3),),
                ),
            ]
            for sql, params in queries:
                for row in conn.execute(sql, params).fetchall():
                    memory_id = str(row["memory_id"] or "")
                    if memory_id and memory_id not in row_map:
                        row_map[memory_id] = row
            rows = list(row_map.values())
            results: list[MemorySearchCandidate] = []
            refreshed_any = False
            for row in rows:
                payload = _json_loads(row["payload"], {})
                if not isinstance(payload, dict):
                    continue
                if not self._scope_matches(payload, scope):
                    continue
                if not self._candidate_visible_for_request(payload, group_id=group_id, user_id=user_id):
                    continue
                embedding = self._embedding_for_payload(conn=conn, payload=payload, row=row)
                if str(row["model_version"] or "") != EMBED_MODEL_VERSION:
                    refreshed_any = True
                similarity = cosine_similarity(query_embedding, embedding)
                if similarity <= 0.08:
                    continue
                payload["_query"] = query
                results.append(
                    self._candidate_from_payload(
                        payload,
                        base_score=0.18 + similarity * 0.72,
                        reason="语义相近",
                        source="embedding",
                        requested_group_id=group_id,
                        requested_user_id=user_id,
                    )
                )
            if refreshed_any:
                conn.commit()
        return results

    def _search_by_time(
        self,
        *,
        query: str,
        group_id: str,
        user_id: str,
        scope: str,
        limit: int,
        scan_limit: int,
    ) -> list[MemorySearchCandidate]:
        if not query:
            return []
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM memory_items
                WHERE supports_recall=1
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (min(max(limit * 8, 48), max(int(scan_limit or 80), 80)),),
            ).fetchall()
        results: list[MemorySearchCandidate] = []
        for row in rows:
            payload = _json_loads(row["payload"], {})
            if not isinstance(payload, dict):
                continue
            if not self._scope_matches(payload, scope):
                continue
            if not self._candidate_visible_for_request(payload, group_id=group_id, user_id=user_id):
                continue
            delta = time_hint_score(
                query,
                safe_float(payload.get("time_created", 0), 0),
                safe_float(payload.get("expires_at", 0), 0),
                str(payload.get("time_sensitivity", "normal") or "normal"),
            )
            if delta <= 0:
                continue
            payload["_query"] = query
            results.append(
                self._candidate_from_payload(
                    payload,
                    base_score=0.22 + delta,
                    reason="时间线索",
                    source="time",
                    requested_group_id=group_id,
                    requested_user_id=user_id,
                )
            )
        return results

    def _mark_memory_accessed(self, memory_id: str) -> None:
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            conn.execute(
                """
                UPDATE memory_items
                SET last_accessed_at=?, access_count=COALESCE(access_count, 0) + 1
                WHERE memory_id=?
                """,
                (now_ts(), str(memory_id or "")),
            )
            conn.commit()

    def _record_search_stats(
        self,
        *,
        query: str,
        scope: str,
        mode: str,
        requested_group_id: str,
        requested_user_id: str,
        hit_count: int,
        used_memory_ids: list[str],
        status: str,
    ) -> None:
        current_ts = now_ts()
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            conn.execute(
                """
                INSERT INTO memory_search_stats(query, scope, mode, requested_group_id, requested_user_id,
                                                hit_count, used_memory_ids, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(query or ""),
                    str(scope or "auto"),
                    str(mode or "fast"),
                    str(requested_group_id or ""),
                    str(requested_user_id or ""),
                    int(hit_count or 0),
                    json.dumps(list(used_memory_ids or []), ensure_ascii=False),
                    str(status or "ok"),
                    current_ts,
                ),
            )
            if current_ts - self._last_search_stats_prune_ts >= SEARCH_STATS_PRUNE_INTERVAL_SECONDS:
                self._prune_search_stats(conn, current_ts=current_ts)
                self._last_search_stats_prune_ts = current_ts
            conn.commit()

    def _prune_search_stats(self, conn: sqlite3.Connection, *, current_ts: float) -> None:
        min_created_at = current_ts - SEARCH_STATS_RETENTION_DAYS * 86400
        conn.execute(
            "DELETE FROM memory_search_stats WHERE created_at < ?",
            (float(min_created_at),),
        )
        row = conn.execute("SELECT COUNT(1) AS cnt FROM memory_search_stats").fetchone()
        total = int(row["cnt"] if row else 0)
        overflow = max(0, total - MAX_SEARCH_STATS_ROWS)
        if overflow <= 0:
            return
        conn.execute(
            """
            DELETE FROM memory_search_stats
            WHERE id IN (
                SELECT id
                FROM memory_search_stats
                ORDER BY created_at ASC, id ASC
                LIMIT ?
            )
            """,
            (overflow,),
        )

    def _recall_grouped_fallback(self, *, group_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        group_id = str(group_id or "").strip()
        if not group_id:
            return []
        group_dir = self.ensure_group_space(group_id)
        query_tokens = set(tokenize(query))
        rows: list[sqlite3.Row] = []
        with _connect(group_dir / "chat_history.db") as conn:
            rows = conn.execute(
                """
                SELECT content, metadata, created_at
                FROM messages
                ORDER BY id DESC
                LIMIT 48
                """
            ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            content = _json_loads(row["content"], row["content"])
            text = ""
            if isinstance(content, list):
                text = " ".join(
                    str(part.get("text", "")).strip()
                    for part in content
                    if isinstance(part, dict) and str(part.get("text", "")).strip()
                )
            elif isinstance(content, dict):
                text = str(content.get("text", "") or content.get("content", "")).strip()
            else:
                text = str(content or "").strip()
            if not text:
                continue
            tokens = set(tokenize(text))
            overlap = len(tokens & query_tokens)
            if query_tokens and overlap <= 0:
                continue
            created_at = safe_float(row["created_at"], 0)
            score = 0.22 + min(overlap, 5) * 0.08 + max(0.0, 0.18 - ((now_ts() - created_at) / 86400.0) * 0.02)
            scored.append(
                (
                    score,
                    {
                        "memory_id": f"grouped:{group_id}:{int(created_at)}",
                        "memory_type": "episodic",
                        "palace_zone": "recent_episode",
                        "summary": text[:180],
                        "group_id": group_id,
                        "why_relevant": "群聊近期上下文命中",
                        "time_hint": build_time_hint(created_at),
                        "score": round(score, 4),
                        "search_source": "grouped_fallback",
                        "supports_recall": True,
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def _init_shared_db(self) -> None:
        with _connect(self.shared_dir / "core_user_profiles.db") as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles(
                    user_id TEXT PRIMARY KEY,
                    profile_text TEXT NOT NULL DEFAULT '',
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def _init_group_chat_db(self, path: Path) -> None:
        with _connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _init_group_context_db(self, path: Path) -> None:
        with _connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_entries(
                    context_key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _init_local_profile_db(self, path: Path) -> None:
        with _connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles(
                    user_id TEXT PRIMARY KEY,
                    profile_text TEXT NOT NULL DEFAULT '',
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def _ensure_table_columns(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        columns: dict[str, str],
    ) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {ddl}")

    def _init_palace_db(self) -> None:
        with _connect(self.memory_palace_dir / "memory_palace.db") as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items(
                    memory_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,
                    palace_zone TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    topic_tags TEXT NOT NULL DEFAULT '[]',
                    entity_tags TEXT NOT NULL DEFAULT '[]',
                    snippets TEXT NOT NULL DEFAULT '[]',
                    user_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    thread_id TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    salience REAL NOT NULL DEFAULT 0,
                    stability REAL NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    supports_recall INTEGER NOT NULL DEFAULT 1,
                    supports_autofill INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    tone_risk REAL NOT NULL DEFAULT 0,
                    irony_risk REAL NOT NULL DEFAULT 0,
                    group_scope TEXT NOT NULL DEFAULT 'shared',
                    cross_group_allowed INTEGER NOT NULL DEFAULT 0,
                    time_sensitivity TEXT NOT NULL DEFAULT 'normal',
                    superseded_by TEXT NOT NULL DEFAULT '',
                    reinforcement_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at REAL NOT NULL DEFAULT 0,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    permission_type TEXT NOT NULL DEFAULT 'public_preference',
                    embedding BLOB,
                    embedding_model TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "memory_items",
                {
                    "aliases": "TEXT NOT NULL DEFAULT '[]'",
                    "snippets": "TEXT NOT NULL DEFAULT '[]'",
                    "thread_id": "TEXT NOT NULL DEFAULT ''",
                    "revision": "INTEGER NOT NULL DEFAULT 1",
                    "tone_risk": "REAL NOT NULL DEFAULT 0",
                    "irony_risk": "REAL NOT NULL DEFAULT 0",
                    "group_scope": "TEXT NOT NULL DEFAULT 'shared'",
                    "cross_group_allowed": "INTEGER NOT NULL DEFAULT 0",
                    "time_sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                    "superseded_by": "TEXT NOT NULL DEFAULT ''",
                    "reinforcement_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_accessed_at": "REAL NOT NULL DEFAULT 0",
                    "access_count": "INTEGER NOT NULL DEFAULT 0",
                    "permission_type": "TEXT NOT NULL DEFAULT 'public_preference'",
                    "embedding": "BLOB",
                    "embedding_model": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED,
                    summary,
                    aliases,
                    topic_tags,
                    entity_tags,
                    snippets
                )
                """
            )
            self._fts_available = True
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embeddings(
                    memory_id TEXT PRIMARY KEY,
                    model_version TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_vector_chunks(
                    chunk_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    chunk_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    embedding_dim INTEGER NOT NULL DEFAULT 0,
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    permission_type TEXT NOT NULL DEFAULT 'public_preference',
                    group_scope TEXT NOT NULL DEFAULT 'shared',
                    cross_group_allowed INTEGER NOT NULL DEFAULT 0,
                    salience REAL NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entities(
                    entity TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT 'tag',
                    weight REAL NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(entity, memory_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_relations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_memory_id TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_clusters(
                    cluster_id TEXT PRIMARY KEY,
                    cluster_type TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_search_stats(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'auto',
                    mode TEXT NOT NULL DEFAULT 'fast',
                    requested_group_id TEXT NOT NULL DEFAULT '',
                    requested_user_id TEXT NOT NULL DEFAULT '',
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    used_memory_ids TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'ok',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_items_lookup ON memory_items(group_id, user_id, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_items_recall ON memory_items(supports_recall, group_id, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_vector_lookup ON memory_vector_chunks(model_version, group_id, user_id, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_vector_memory ON memory_vector_chunks(memory_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_palace_zone ON memory_items(palace_zone)"
            )
            # WU5：webui 群详情 list_recent_memories 按 (group_id, memory_type, updated_at) 过滤；
            # 加复合索引避免在 memory_items 全表扫描
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_items_group_type ON memory_items(group_id, memory_type, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_items_salience ON memory_items(group_id, salience)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_entities_lookup ON memory_entities(entity, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_relations_lookup ON memory_relations(source_memory_id, relation_type)"
            )
            conn.commit()
        with _connect(self.memory_palace_dir / "bootstrap_state.db") as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bootstrap_groups(
                    group_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    bootstrapped_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        with _connect(self.memory_palace_dir / "migration_state.db") as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migration_entries(
                    migration_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migration_files(
                    source_path TEXT PRIMARY KEY,
                    source_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    target_group TEXT NOT NULL DEFAULT '',
                    target_db TEXT NOT NULL DEFAULT '',
                    recycle_status TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()


_MEMORY_STORE: MemoryStore | None = None


def init_memory_store(plugin_config: Any, logger: Any = None) -> MemoryStore:
    global _MEMORY_STORE
    store = MemoryStore(plugin_config, logger=logger)
    store.initialize()
    _MEMORY_STORE = store
    return store


def get_memory_store() -> MemoryStore:
    if _MEMORY_STORE is None:
        raise RuntimeError("MemoryStore not initialized")
    return _MEMORY_STORE
