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
                # P1：长期记忆保护——以下类型 / 标记的记忆不再硬删，避免"长期记忆丢失"。
                # 满足任一条件即视为"半永久"：
                # - memory_type 属于摘要 / 知识 / 画像类（session/daily summary、group_knowledge 等）
                # - reinforcement_count > 0（被多次提及/确认）
                # - access_count > 2（多次被检索）
                # - palace_zone 标记为 semantic 或 background
                memory_type = str(payload.get("memory_type", "") or "").strip().lower()
                palace_zone = str(payload.get("palace_zone", "") or "").strip().lower()
                tier = str(payload.get("tier", "") or "").strip().lower()
                semi_permanent_types = {
                    "session_summary",
                    "daily_summary",
                    "group_knowledge",
                    "group_meme",
                    "concept_anchor",
                    "user_persona",
                    "persona_knowledge",
                    "group_relation",
                    "core_profile",
                    "fact",
                    "semantic",
                }
                is_semi_permanent = (
                    memory_type in semi_permanent_types
                    or reinforcement > 0
                    or access_count > 2
                    or palace_zone in {"semantic", "background"}
                    or tier in {"semantic", "background"}
                )
                if is_semi_permanent:
                    # 给底线值兜底：salience/stability 不会被衰减到 0；保留少量召回价值
                    payload["salience"] = max(0.1, payload["salience"])
                    payload["stability"] = max(0.1, payload["stability"])
                if payload["salience"] < 0.08 and payload["stability"] < 0.08 and reinforcement <= 0 and not is_semi_permanent:
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
                # P4：根据 reinforcement / 时长 重新判定 tier。
                # 不会降级 semantic/background（这两个由摘要逻辑显式置位）。
                try:
                    from .memory_tier import (
                        TIER_BACKGROUND,
                        TIER_EPISODIC,
                        TIER_SEMANTIC,
                        TIER_WORKING,
                        should_promote,
                    )

                    current_tier = str(payload.get("tier", "") or "").strip().lower()
                    if current_tier not in {TIER_SEMANTIC, TIER_BACKGROUND}:
                        promoted = should_promote(payload)
                        if promoted:
                            payload["tier"] = promoted
                        elif current_tier == TIER_WORKING and age_days >= 1.0:
                            payload["tier"] = TIER_EPISODIC
                        elif not current_tier:
                            payload["tier"] = TIER_EPISODIC if age_days >= 1.0 else TIER_WORKING
                except Exception:
                    pass
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
