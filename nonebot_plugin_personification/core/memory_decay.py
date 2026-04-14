from __future__ import annotations

import json
import time
from typing import Any

from .memory_store import MemoryStore
from .search_ranker import now_ts, safe_float


class MemoryDecayScheduler:
    def __init__(self, memory_store: MemoryStore, logger: Any = None) -> None:
        self.memory_store = memory_store
        self.logger = logger

    def run_once(self) -> int:
        if not self.memory_store.palace_enabled():
            return 0
        db_path = self.memory_store.memory_palace_dir / "memory_palace.db"
        now = time.time()
        import sqlite3

        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT memory_id, payload, expires_at
                FROM memory_items
                """
            ).fetchall()
            purged = 0
            for row in rows:
                payload = self.memory_store._get_memory_payload(str(row["memory_id"] or ""), conn=conn)
                if not payload:
                    continue
                expires_at = safe_float(row["expires_at"], 0)
                if expires_at and expires_at <= now:
                    conn.execute("DELETE FROM memory_items WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    conn.execute("DELETE FROM memory_entities WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    conn.execute("DELETE FROM memory_relations WHERE source_memory_id=?", (str(row["memory_id"] or ""),))
                    try:
                        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    except Exception:
                        pass
                    purged += 1
                    continue
                age_days = max(0.0, (now_ts() - safe_float(payload.get("time_created", 0), 0)) / 86400.0)
                reinforcement = min(float(int(payload.get("reinforcement_count", 0) or 0)), 8.0)
                access_count = min(float(int(payload.get("access_count", 0) or 0)), 12.0)
                decay_factor = max(0.01, 0.025 - reinforcement * 0.0015 - access_count * 0.0008)
                if str(payload.get("time_sensitivity", "normal") or "normal") in {"high", "strong", "hot"}:
                    decay_factor += 0.012
                if str(payload.get("superseded_by", "") or ""):
                    decay_factor += 0.02
                payload["salience"] = max(0.0, safe_float(payload.get("salience", 0.4), 0.4) - min(age_days * decay_factor, 0.12))
                payload["stability"] = max(0.0, safe_float(payload.get("stability", 0.4), 0.4) - min(age_days * decay_factor * 0.7, 0.08))
                payload["confidence"] = max(0.0, safe_float(payload.get("confidence", 0.5), 0.5) - (0.05 if str(payload.get("superseded_by", "")) else 0.0))
                payload["revision"] = int(payload.get("revision", 1) or 1) + 1
                if payload["salience"] < 0.08 and payload["stability"] < 0.08 and reinforcement <= 0:
                    conn.execute("DELETE FROM memory_items WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    conn.execute("DELETE FROM memory_entities WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    conn.execute("DELETE FROM memory_relations WHERE source_memory_id=?", (str(row["memory_id"] or ""),))
                    try:
                        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (str(row["memory_id"] or ""),))
                    except Exception:
                        pass
                    purged += 1
                    continue
                conn.execute(
                    """
                    UPDATE memory_items
                    SET payload=?, salience=?, stability=?, confidence=?, revision=?, updated_at=?
                    WHERE memory_id=?
                    """,
                    (
                        # If future decay/reconsolidation starts rewriting summary/entity fields,
                        # memory_fts / memory_entities / memory_embeddings must be rebuilt too.
                        json.dumps(payload, ensure_ascii=False),
                        float(payload["salience"]),
                        float(payload["stability"]),
                        float(payload["confidence"]),
                        int(payload["revision"]),
                        now_ts(),
                        str(row["memory_id"] or ""),
                    ),
                )
            conn.commit()
            return purged
        finally:
            conn.close()
