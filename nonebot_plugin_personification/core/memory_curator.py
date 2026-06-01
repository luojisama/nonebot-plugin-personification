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

    def schedule_turn_capture(
        self,
        *,
        user_utterance: str,
        bot_response: str,
        user_id: str = "",
        group_id: str = "",
        evidence_refs: list[str] | None = None,
        vision_summary: str = "",
        semantic_frame: Any = None,
        scope: str = "",
    ) -> None:
        if not self.memory_store.palace_enabled():
            return
        user_text = normalize_text(user_utterance)[:500]
        bot_text = normalize_text(bot_response)[:500]
        if not user_text and not bot_text:
            return
        task = asyncio.create_task(
            self.capture_turn(
                user_utterance=user_text,
                bot_response=bot_text,
                user_id=user_id,
                group_id=group_id,
                evidence_refs=list(evidence_refs or []),
                vision_summary=vision_summary,
                semantic_frame=semantic_frame,
                scope=scope,
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
        permission_type = _classify_permission_type(
            memory_type="episodic",
            group_id=group_id,
            semantic_frame=None,
            tone_risk=tone_risk,
            irony_risk=irony_risk,
            summary=text,
        )
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
                "permission_type": permission_type,
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

    async def capture_turn(
        self,
        *,
        user_utterance: str,
        bot_response: str,
        user_id: str = "",
        group_id: str = "",
        evidence_refs: list[str] | None = None,
        vision_summary: str = "",
        semantic_frame: Any = None,
        scope: str = "",
    ) -> None:
        user_text = normalize_text(user_utterance)[:500]
        bot_text = normalize_text(bot_response)[:500]
        if not user_text and not bot_text:
            return
        if user_text and bot_text:
            summary = f"用户问/说：{user_text}；bot 回：{bot_text}"
        elif user_text:
            summary = f"用户问/说：{user_text}"
        else:
            summary = f"bot 回：{bot_text}"
        summary = normalize_text(summary)[:800]
        combined = f"{user_text} {bot_text}".strip()
        topic_tags: list[str] = []
        for token in tokenize(combined):
            normalized = normalize_text(token)
            if normalized and len(normalized) >= 2 and normalized not in topic_tags:
                topic_tags.append(normalized)
            if len(topic_tags) >= 8:
                break
        entity_tags = extract_entities(combined, topic_tags, [])[:10]
        frame_payload = {
            key: str(getattr(semantic_frame, key, "") or "").strip()
            for key in (
                "chat_intent",
                "plugin_question_intent",
                "ambiguity_level",
                "domain_focus",
                "output_mode",
                "session_goal",
            )
            if str(getattr(semantic_frame, key, "") or "").strip()
        }
        memory_scope = str(scope or "").strip()
        if not memory_scope:
            memory_scope = f"group:{group_id}" if group_id else (f"user:{user_id}" if user_id else "both")
        permission_type = _classify_permission_type(
            memory_type="episodic_turn",
            group_id=group_id,
            semantic_frame=semantic_frame,
            tone_risk=0.08,
            irony_risk=0.1,
            summary=summary,
        )
        memory_id = await asyncio.to_thread(
            self.memory_store.write_memory_item,
            {
                "memory_type": "episodic_turn",
                "palace_zone": "recent_episode",
                "summary": summary,
                "user_utterance": user_text,
                "bot_response": bot_text,
                "evidence_refs": list(evidence_refs or [])[:8],
                "vision_summary": normalize_text(vision_summary)[:240],
                "semantic_frame": frame_payload,
                "scope": memory_scope,
                "source_kind": "conversation_turn",
                "source_refs": list(evidence_refs or [])[:8],
                "user_id": str(user_id or ""),
                "group_id": str(group_id or ""),
                "thread_id": "",
                "topic_tags": topic_tags,
                "entity_tags": entity_tags,
                "snippets": [user_text[:120], bot_text[:120]],
                "time_created": time.time(),
                "last_accessed_at": 0,
                "access_count": 0,
                "confidence": 0.62,
                "salience": min(0.9, 0.52 + min(len(entity_tags), 4) * 0.05),
                "stability": 0.4,
                "emotional_weight": 0.0,
                "privacy_level": "default",
                "permission_type": permission_type,
                "expires_at": 0.0,
                "supports_recall": True,
                "supports_autofill": False,
                "revision": 1,
                "tone_risk": 0.08,
                "irony_risk": 0.1,
                "group_scope": "isolated" if group_id else "shared",
                "cross_group_allowed": False,
                "time_sensitivity": "normal",
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


def _classify_permission_type(
    *,
    memory_type: str,
    group_id: str = "",
    semantic_frame: Any = None,
    tone_risk: float = 0.0,
    irony_risk: float = 0.0,
    summary: str = "",
) -> str:
    normalized_type = str(memory_type or "").lower()
    if normalized_type in {"group_meme", "concept_anchor"}:
        return "group_meme"
    if normalized_type == "conflict_memory":
        return "conflict_memory"
    domain_focus = str(getattr(semantic_frame, "domain_focus", "") or "").lower()
    requires_care = bool(getattr(semantic_frame, "requires_emotional_care", False))
    if requires_care or domain_focus == "emotion":
        return "private_fact"
    if tone_risk >= 0.6 or irony_risk >= 0.6:
        return "conflict_memory"
    if not str(group_id or "").strip():
        return "private_fact"
    return "public_preference"
