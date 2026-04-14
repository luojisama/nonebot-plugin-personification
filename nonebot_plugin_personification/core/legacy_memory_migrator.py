from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..agent.inner_state import get_personification_data_dir
from .db import get_db_path
from .memory_store import MemoryStore, _connect, _json_loads


_MEMORY_JSON_TARGETS = {
    "chat_history.json": "chat_history",
    "chat_history.json.bak": "chat_history",
    "session_histories.json": "session_histories",
    "session_histories.json.bak": "session_histories",
    "user_personas.json": "user_personas",
    "user_personas.json.bak": "user_personas",
    "group_context.json": "group_context",
    "group_context.json.bak": "group_context",
}


class LegacyMemoryMigrator:
    _LEGACY_SQLITE_MIGRATION_KEY = "legacy_sqlite_to_dual_memory_v3"

    def __init__(self, memory_store: MemoryStore, logger: Any = None) -> None:
        self.memory_store = memory_store
        self.logger = logger

    def migrate_once(self) -> None:
        self._migrate_legacy_sqlite()
        for path in self._discover_json_files():
            try:
                self._migrate_json_file(path)
            except Exception as exc:
                self._record_file_state(
                    source_path=path,
                    source_count=0,
                    success_count=0,
                    skipped_count=0,
                    error_count=1,
                    target_group="",
                    target_db="",
                    recycle_status=f"error:{exc.__class__.__name__}",
                )

    def _migrate_legacy_sqlite(self) -> None:
        legacy_path = get_db_path()
        if not legacy_path.exists():
            return
        migration_db = self.memory_store.memory_palace_dir / "migration_state.db"
        with sqlite3.connect(migration_db, check_same_thread=False) as state_conn:
            row = state_conn.execute(
                "SELECT status FROM migration_entries WHERE migration_key=?",
                (self._LEGACY_SQLITE_MIGRATION_KEY,),
            ).fetchone()
            if row and str(row[0] or "") == "done":
                return

        legacy_conn = sqlite3.connect(legacy_path, check_same_thread=False)
        legacy_conn.row_factory = sqlite3.Row
        migrated = 0
        target_groups: set[str] = set()
        target_dbs: set[str] = set()
        try:
            tables = self._discover_legacy_tables(legacy_conn)
            if "user_personas" in tables:
                count, groups, dbs = self._migrate_legacy_sqlite_user_personas(legacy_conn)
                migrated += count
                target_groups.update(groups)
                target_dbs.update(dbs)
            migrated_group_ids: set[str] = set()
            if "group_messages" in tables:
                count, groups, dbs = self._migrate_legacy_sqlite_group_messages(legacy_conn)
                migrated += count
                migrated_group_ids.update(groups)
                target_groups.update(groups)
                target_dbs.update(dbs)
            if "session_messages" in tables:
                count, groups, dbs = self._migrate_legacy_sqlite_session_messages(
                    legacy_conn,
                    skip_group_ids=migrated_group_ids,
                )
                migrated += count
                target_groups.update(groups)
                target_dbs.update(dbs)
            if "kv_store" in tables:
                count, groups, dbs = self._migrate_legacy_sqlite_kv_namespaces(legacy_conn)
                migrated += count
                target_groups.update(groups)
                target_dbs.update(dbs)
        finally:
            legacy_conn.close()

        with sqlite3.connect(migration_db, check_same_thread=False) as state_conn:
            state_conn.execute(
                """
                INSERT INTO migration_entries(migration_key, status, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(migration_key) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (self._LEGACY_SQLITE_MIGRATION_KEY, "done", time.time()),
            )
            state_conn.commit()
        self._record_file_state(
            source_path=legacy_path,
            source_count=migrated,
            success_count=migrated,
            skipped_count=0,
            error_count=0,
            target_group=",".join(sorted(target_groups))[:200],
            target_db=",".join(sorted(target_dbs))[:200],
            recycle_status="kept",
        )

    def _discover_legacy_tables(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {
            str(row["name"] or "").strip()
            for row in rows
            if str(row["name"] or "").strip()
        }

    def _extract_created_at(self, payload: Any) -> float | None:
        if isinstance(payload, dict):
            for key in ("created_at", "timestamp", "time", "updated_at"):
                raw = payload.get(key)
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
        return None

    def _group_has_chat_history(self, group_id: str) -> bool:
        group_id = str(group_id or "").strip()
        if not group_id:
            return False
        group_dir = self.memory_store.ensure_group_space(group_id)
        with _connect(group_dir / "chat_history.db") as conn:
            row = conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
        return row is not None

    def _migrate_legacy_sqlite_user_personas(
        self,
        conn: sqlite3.Connection,
    ) -> tuple[int, set[str], set[str]]:
        try:
            rows = conn.execute(
                "SELECT user_id, persona, updated_at FROM user_personas ORDER BY updated_at DESC"
            ).fetchall()
        except Exception:
            rows = []
        migrated = 0
        for row in rows:
            self.memory_store.upsert_core_profile(
                user_id=str(row["user_id"] or ""),
                profile_text=str(row["persona"] or ""),
                profile_json={"migrated_from": "user_personas"},
                source="legacy_memory_migrator",
            )
            migrated += 1
        return migrated, {"shared"}, {"core_user_profiles.db"}

    def _migrate_legacy_sqlite_group_messages(
        self,
        conn: sqlite3.Connection,
    ) -> tuple[int, set[str], set[str]]:
        try:
            rows = conn.execute(
                """
                SELECT group_id, user_id, nickname, content, is_bot, reply_to_msg_id, reply_to_user_id,
                       mentioned_ids, is_at_bot, message_id, source_kind, timestamp
                FROM group_messages
                ORDER BY timestamp ASC, id ASC
                """
            ).fetchall()
        except Exception:
            rows = []
        migrated = 0
        groups: set[str] = set()
        skipped_existing_groups: set[str] = set()
        allowed_groups: set[str] = set()
        for row in rows:
            group_id = str(row["group_id"] or "").strip()
            text = str(row["content"] or "").strip()
            if not group_id or not text:
                continue
            if group_id in skipped_existing_groups:
                continue
            if group_id not in allowed_groups:
                if self._group_has_chat_history(group_id):
                    skipped_existing_groups.add(group_id)
                    continue
                allowed_groups.add(group_id)
            groups.add(group_id)
            metadata = {
                "user_id": str(row["user_id"] or ""),
                "nickname": str(row["nickname"] or ""),
                "is_bot": bool(row["is_bot"]),
                "reply_to_msg_id": row["reply_to_msg_id"],
                "reply_to_user_id": row["reply_to_user_id"],
                "mentioned_ids": _json_loads(row["mentioned_ids"], []),
                "is_at_bot": bool(row["is_at_bot"]),
                "message_id": str(row["message_id"] or ""),
                "source_kind": str(row["source_kind"] or ""),
            }
            role = "assistant" if bool(row["is_bot"]) else (str(row["nickname"] or row["user_id"] or "user"))
            self.memory_store.append_group_message(
                group_id=group_id,
                role=role,
                content={"text": text},
                metadata=metadata,
                created_at=self._extract_created_at({"timestamp": row["timestamp"]}),
            )
            migrated += 1
        return migrated, groups, {"chat_history.db"}

    def _migrate_legacy_sqlite_session_messages(
        self,
        conn: sqlite3.Connection,
        *,
        skip_group_ids: set[str],
    ) -> tuple[int, set[str], set[str]]:
        try:
            rows = conn.execute(
                """
                SELECT session_id, role, content, timestamp, metadata
                FROM session_messages
                ORDER BY timestamp ASC, id ASC
                """
            ).fetchall()
        except Exception:
            rows = []
        migrated = 0
        groups: set[str] = set()
        skipped_existing_groups: set[str] = set()
        allowed_groups: set[str] = set()
        for row in rows:
            session_id = str(row["session_id"] or "").strip()
            if not session_id.startswith("group_"):
                continue
            group_id = session_id.removeprefix("group_").strip()
            if not group_id or group_id in skip_group_ids or group_id in skipped_existing_groups:
                continue
            if group_id not in allowed_groups:
                if self._group_has_chat_history(group_id):
                    skipped_existing_groups.add(group_id)
                    continue
                allowed_groups.add(group_id)
            content = _json_loads(row["content"], row["content"])
            if isinstance(content, dict):
                text = str(content.get("text", "") or content.get("content", "")).strip()
            else:
                text = str(content or "").strip()
            if not text:
                continue
            metadata = _json_loads(row["metadata"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            self.memory_store.append_group_message(
                group_id=group_id,
                role=str(row["role"] or "user"),
                content={"text": text},
                metadata=metadata,
                created_at=self._extract_created_at({"timestamp": row["timestamp"]}),
            )
            groups.add(group_id)
            migrated += 1
        return migrated, groups, {"chat_history.db"}

    def _migrate_legacy_sqlite_kv_namespaces(
        self,
        conn: sqlite3.Connection,
    ) -> tuple[int, set[str], set[str]]:
        try:
            rows = conn.execute(
                "SELECT namespace, key, value FROM kv_store WHERE key='__root__'"
            ).fetchall()
        except Exception:
            rows = []
        migrated = 0
        groups: set[str] = set()
        targets: set[str] = set()
        for row in rows:
            namespace = str(row["namespace"] or "").strip()
            payload = _json_loads(row["value"], {})
            if namespace not in {"chat_history", "group_context"} or not isinstance(payload, dict):
                continue
            count, _, target_group, target_db = self._migrate_group_context_json(payload)
            migrated += count
            groups.update({group for group in str(target_group or "").split(",") if group})
            if target_db:
                targets.add(target_db)
        return migrated, groups, targets

    def _discover_json_files(self) -> list[Path]:
        roots = {
            get_personification_data_dir(self.memory_store.plugin_config),
            self.memory_store.root_dir,
            self.memory_store.root_dir.parent,
        }
        candidates: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if self.memory_store.recycle_bin_dir in path.parents:
                    continue
                name = path.name.lower()
                if name in _MEMORY_JSON_TARGETS:
                    candidates.append(path)
        unique: dict[str, Path] = {}
        for path in candidates:
            unique[str(path.resolve())] = path
        return list(unique.values())

    def _migrate_json_file(self, path: Path) -> None:
        path = path.resolve()
        if self._file_already_done(path):
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._record_file_state(
                source_path=path,
                source_count=0,
                success_count=0,
                skipped_count=0,
                error_count=1,
                target_group="",
                target_db="",
                recycle_status="invalid_json",
            )
            return

        source_count = self._estimate_record_count(payload)
        if source_count <= 0:
            recycled = self._recycle_file(path)
            self._record_file_state(
                source_path=path,
                source_count=0,
                success_count=0,
                skipped_count=0,
                error_count=0,
                target_group="",
                target_db="",
                recycle_status="recycled" if recycled else "kept",
            )
            return

        name = path.name.lower()
        migration_type = _MEMORY_JSON_TARGETS.get(name, "")
        success = 0
        skipped = 0
        target_group = ""
        target_db = ""

        if migration_type == "chat_history":
            success, skipped, target_group, target_db = self._migrate_chat_history_json(payload)
        elif migration_type == "group_context":
            success, skipped, target_group, target_db = self._migrate_group_context_json(payload)
        elif migration_type == "user_personas":
            success, skipped, target_group, target_db = self._migrate_user_personas_json(payload)
        elif migration_type == "session_histories":
            success, skipped, target_group, target_db = self._migrate_session_histories_json(payload)

        recycled = False
        if success + skipped >= source_count:
            recycled = self._recycle_file(path)
        self._record_file_state(
            source_path=path,
            source_count=source_count,
            success_count=success,
            skipped_count=skipped,
            error_count=max(0, source_count - success - skipped),
            target_group=target_group,
            target_db=target_db,
            recycle_status="recycled" if recycled else "kept",
        )

    def _migrate_chat_history_json(self, payload: Any) -> tuple[int, int, str, str]:
        if not isinstance(payload, dict):
            return 0, 0, "", ""
        success = 0
        skipped = 0
        groups: set[str] = set()
        for group_id, group_data in payload.items():
            gid = str(group_id or "").strip()
            if not gid or not isinstance(group_data, dict):
                skipped += 1
                continue
            groups.add(gid)
            style = str(group_data.get("style", "") or "").strip()
            if style:
                self.memory_store.save_group_context(group_id=gid, key="style", value=style)
                success += 1
            messages = group_data.get("messages", [])
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        skipped += 1
                        continue
                    text = str(message.get("content", "") or "").strip()
                    if not text:
                        skipped += 1
                        continue
                    metadata = {k: v for k, v in message.items() if k != "content"}
                    self.memory_store.append_group_message(
                        group_id=gid,
                        role=str(message.get("role", message.get("nickname", "user")) or "user"),
                        content={"text": text},
                        metadata=metadata,
                        created_at=self._extract_created_at(message),
                    )
                    success += 1
        return success, skipped, ",".join(sorted(groups))[:200], "chat_history.db"

    def _migrate_group_context_json(self, payload: Any) -> tuple[int, int, str, str]:
        if not isinstance(payload, dict):
            return 0, 0, "", ""
        success = 0
        skipped = 0
        groups: set[str] = set()
        for group_id, value in payload.items():
            gid = str(group_id or "").strip()
            if not gid:
                skipped += 1
                continue
            groups.add(gid)
            if isinstance(value, dict):
                for key, entry in value.items():
                    self.memory_store.save_group_context(group_id=gid, key=str(key or ""), value=entry)
                    success += 1
            else:
                self.memory_store.save_group_context(group_id=gid, key="summary", value=value)
                success += 1
        return success, skipped, ",".join(sorted(groups))[:200], "group_context.db"

    def _migrate_user_personas_json(self, payload: Any) -> tuple[int, int, str, str]:
        if not isinstance(payload, dict):
            return 0, 0, "", ""
        success = 0
        skipped = 0
        for user_id, entry in payload.items():
            uid = str(user_id or "").strip()
            if not uid:
                skipped += 1
                continue
            if isinstance(entry, dict):
                text = str(entry.get("data", "") or entry.get("persona", "")).strip()
                raw_time = entry.get("time", entry.get("updated_at", 0))
                profile_json = {"migrated_from": "user_personas.json", "legacy_time": raw_time}
            else:
                text = str(entry or "").strip()
                profile_json = {"migrated_from": "user_personas.json"}
            if not text:
                skipped += 1
                continue
            self.memory_store.upsert_core_profile(
                user_id=uid,
                profile_text=text,
                profile_json=profile_json,
                source="legacy_json",
            )
            success += 1
        return success, skipped, "shared", "core_user_profiles.db"

    def _migrate_session_histories_json(self, payload: Any) -> tuple[int, int, str, str]:
        if not isinstance(payload, dict):
            return 0, 0, "", ""
        success = 0
        skipped = 0
        groups: set[str] = set()
        for session_id, entries in payload.items():
            sid = str(session_id or "").strip()
            if not sid or not isinstance(entries, list):
                skipped += 1
                continue
            if sid.startswith("group_"):
                group_id = sid.removeprefix("group_")
            else:
                group_id = sid
            if not group_id:
                skipped += 1
                continue
            groups.add(group_id)
            for entry in entries:
                if not isinstance(entry, dict):
                    skipped += 1
                    continue
                text = str(entry.get("content", "") or "").strip()
                if not text:
                    skipped += 1
                    continue
                self.memory_store.append_group_message(
                    group_id=group_id,
                    role=str(entry.get("role", "user") or "user"),
                    content={"text": text},
                    metadata={k: v for k, v in entry.items() if k != "content"},
                    created_at=self._extract_created_at(entry),
                )
                success += 1
        return success, skipped, ",".join(sorted(groups))[:200], "chat_history.db"

    def _estimate_record_count(self, payload: Any) -> int:
        if isinstance(payload, dict):
            count = 0
            for value in payload.values():
                if isinstance(value, list):
                    count += len(value)
                elif isinstance(value, dict):
                    if "messages" in value and isinstance(value["messages"], list):
                        count += len(value["messages"])
                    else:
                        count += max(1, len(value))
                else:
                    count += 1
            return count
        if isinstance(payload, list):
            return len(payload)
        return 0

    def _file_already_done(self, path: Path) -> bool:
        with _connect(self.memory_store.memory_palace_dir / "migration_state.db") as conn:
            row = conn.execute(
                """
                SELECT source_count, success_count, skipped_count, error_count, recycle_status
                FROM migration_files
                WHERE source_path=?
                """,
                (str(path),),
            ).fetchone()

        if not row:
            return False

        status = str(row["recycle_status"] or "")
        source_count = int(row["source_count"] or 0)
        success_count = int(row["success_count"] or 0)
        skipped_count = int(row["skipped_count"] or 0)
        error_count = int(row["error_count"] or 0)

        if status == "recycled":
            return True

        if source_count > 0 and error_count == 0 and success_count + skipped_count >= source_count:
            return True

        return False

    def _recycle_file(self, path: Path) -> bool:
        try:
            target = self.memory_store.recycle_bin_dir / path.name
            if target.exists():
                target = self.memory_store.recycle_bin_dir / f"{int(time.time())}_{path.name}"
            shutil.move(str(path), str(target))
            return True
        except Exception:
            return False

    def _record_file_state(
        self,
        *,
        source_path: Path,
        source_count: int,
        success_count: int,
        skipped_count: int,
        error_count: int,
        target_group: str,
        target_db: str,
        recycle_status: str,
    ) -> None:
        with _connect(self.memory_store.memory_palace_dir / "migration_state.db") as conn:
            conn.execute(
                """
                INSERT INTO migration_files(source_path, source_count, success_count, skipped_count, error_count,
                                            target_group, target_db, recycle_status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_count=excluded.source_count,
                    success_count=excluded.success_count,
                    skipped_count=excluded.skipped_count,
                    error_count=excluded.error_count,
                    target_group=excluded.target_group,
                    target_db=excluded.target_db,
                    recycle_status=excluded.recycle_status,
                    updated_at=excluded.updated_at
                """,
                (
                    str(source_path),
                    int(source_count or 0),
                    int(success_count or 0),
                    int(skipped_count or 0),
                    int(error_count or 0),
                    str(target_group or ""),
                    str(target_db or ""),
                    str(recycle_status or ""),
                    time.time(),
                ),
            )
            conn.commit()
