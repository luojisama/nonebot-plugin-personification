from __future__ import annotations

import asyncio
import time
from typing import Any

from .embedding_index import normalize_text, tokenize
from .entity_index import extract_entities
from .memory_store import MemoryStore


class MemoryCurator:
    def __init__(
        self,
        memory_store: MemoryStore,
        logger: Any = None,
        background_intelligence: Any = None,
    ) -> None:
        self.memory_store = memory_store
        self.logger = logger
        self.background_intelligence = background_intelligence
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule_capture(
        self,
        *,
        summary: str,
        user_id: str = "",
        group_id: str = "",
        topic_tags: list[str] | None = None,
        entity_tags: list[str] | None = None,
    ) -> None:
        if not self.memory_store.palace_enabled():
            return
        text = str(summary or "").strip()
        if not text:
            return
        task = asyncio.create_task(
            self.capture(
                summary=text,
                user_id=user_id,
                group_id=group_id,
                topic_tags=topic_tags or [],
                entity_tags=entity_tags or [],
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def capture(
        self,
        *,
        summary: str,
        user_id: str = "",
        group_id: str = "",
        topic_tags: list[str] | None = None,
        entity_tags: list[str] | None = None,
    ) -> None:
        text = normalize_text(summary)[:600]
        if not text or len(text) < 8:
            return
        lowered = text.lower()
        derived_topics = [token for token in tokenize(text) if len(token) >= 2][:8]
        merged_topics: list[str] = []
        for value in list(topic_tags or []) + derived_topics:
            normalized = normalize_text(value)
            if normalized and normalized not in merged_topics:
                merged_topics.append(normalized)
        merged_entities = extract_entities(text, merged_topics, list(entity_tags or []))[:10]
        irony_risk = 0.35 if any(token in lowered for token in ("反话", "玩梗", "整活", "狗头", "/s")) else 0.08
        tone_risk = 0.3 if any(token in lowered for token in ("也许", "好像", "可能", "猜", "大概")) else 0.08
        is_time_sensitive = any(token in lowered for token in ("今天", "刚刚", "最近", "版本", "活动", "联动", "更新"))
        stability = 0.28 if is_time_sensitive else 0.42
        confidence = 0.48 if irony_risk > 0.3 else 0.6
        salience = 0.5 + min(len(merged_entities), 4) * 0.05
        expires_at = 0.0
        if is_time_sensitive:
            expires_at = time.time() + 86400 * 7
        memory_id = await asyncio.to_thread(
            self.memory_store.write_memory_item,
            {
                "memory_type": "episodic",
                "palace_zone": "recent_episode",
                "summary": text,
                "source_kind": "conversation",
                "source_refs": [],
                "user_id": str(user_id or ""),
                "group_id": str(group_id or ""),
                "thread_id": "",
                "topic_tags": merged_topics,
                "entity_tags": merged_entities,
                "snippets": [text[:120]],
                "time_created": time.time(),
                "last_accessed_at": 0,
                "access_count": 0,
                "confidence": confidence,
                "salience": min(0.9, salience),
                "stability": stability,
                "emotional_weight": 0.0,
                "privacy_level": "default",
                "expires_at": expires_at,
                "supports_recall": True,
                "supports_autofill": False,
                "revision": 1,
                "tone_risk": tone_risk,
                "irony_risk": irony_risk,
                "group_scope": "isolated" if group_id else "shared",
                "cross_group_allowed": False,
                "time_sensitivity": "high" if is_time_sensitive else "normal",
                "conflict_refs": [],
                "superseded_by": "",
                "reinforcement_count": 1,
            },
        )
        if self.background_intelligence is not None:
            self.background_intelligence.schedule_new_memory(
                memory_id=str(memory_id or ""),
                group_id=str(group_id or ""),
            )
