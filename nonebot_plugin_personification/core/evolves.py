from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .embedding_index import cosine_similarity, embed_text, normalize_text, tokenize
from .memory_store import MemoryStore
from .search_ranker import safe_float


_ALLOWED_RELATIONS = {"replaces", "enriches", "confirms", "challenges", "none"}
_RELATION_ALIASES = {
    "replace": "replaces",
    "replaces": "replaces",
    "替代": "replaces",
    "更新": "replaces",
    "覆盖": "replaces",
    "enrich": "enriches",
    "enriches": "enriches",
    "supplements": "enriches",
    "补充": "enriches",
    "扩展": "enriches",
    "confirm": "confirms",
    "confirms": "confirms",
    "确认": "confirms",
    "佐证": "confirms",
    "challenge": "challenges",
    "challenges": "challenges",
    "contradicts": "challenges",
    "质疑": "challenges",
    "冲突": "challenges",
    "none": "none",
    "unrelated": "none",
    "无关": "none",
}
_LLM_CANDIDATE_LIMIT = 8


@dataclass(frozen=True)
class EvolvesDecision:
    relation_type: str
    confidence: float
    reason: str


class EvolvesEngine:
    def __init__(
        self,
        memory_store: MemoryStore,
        logger: Any = None,
        call_ai_api: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.logger = logger
        self.call_ai_api = call_ai_api

    def set_call_ai_api(self, call_ai_api: Callable[..., Awaitable[Any]] | None) -> None:
        self.call_ai_api = call_ai_api

    def analyze_new_memory(self, memory_id: str, *, group_id: str = "") -> list[EvolvesDecision]:
        _ = memory_id, group_id
        return []

    def process_memory(self, memory_id: str, *, group_id: str = "") -> list[dict[str, Any]]:
        """Synchronous fallback path used by legacy callers.

        This path deliberately avoids keyword semantic decisions. It only links
        memories when similarity/entity overlap is strong enough to infer a
        conservative confirm/enrich relation without another LLM call.
        """
        current = self.memory_store.get_memory_item(memory_id)
        if not isinstance(current, dict):
            return []
        candidates = self._select_relation_candidates(
            current,
            group_id=group_id or str(current.get("group_id", "") or ""),
            limit=_LLM_CANDIDATE_LIMIT,
        )
        pairs = self._decide_relations_fallback(current, candidates)
        return self._apply_decision_pairs(current, pairs)

    async def process_memory_async(self, memory_id: str, *, group_id: str = "") -> list[dict[str, Any]]:
        current = self.memory_store.get_memory_item(memory_id)
        if not isinstance(current, dict):
            return []
        candidates = self._select_relation_candidates(
            current,
            group_id=group_id or str(current.get("group_id", "") or ""),
            limit=_LLM_CANDIDATE_LIMIT,
        )
        if not candidates:
            return []

        pairs: list[tuple[dict[str, Any], EvolvesDecision]] = []
        llm_succeeded = False
        if self.call_ai_api is not None:
            pairs, llm_succeeded = await self._decide_relations_with_llm(current, candidates)

        if not llm_succeeded:
            pairs = self._decide_relations_fallback(current, candidates)
        return self._apply_decision_pairs(current, pairs)

    def _select_relation_candidates(
        self,
        current: dict[str, Any],
        *,
        group_id: str = "",
        limit: int = _LLM_CANDIDATE_LIMIT,
    ) -> list[dict[str, Any]]:
        current_id = str(current.get("memory_id", "") or "")
        candidates = self.memory_store.list_related_memory_candidates(
            memory_id=current_id,
            group_id=group_id or str(current.get("group_id", "") or ""),
            limit=max(limit * 2, limit),
        )
        scored: list[tuple[float, dict[str, Any]]] = []
        for candidate in candidates:
            if str(candidate.get("memory_id", "") or "") == current_id:
                continue
            score = self._candidate_score(current, candidate)
            if score <= 0:
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _score, item in scored[: max(1, int(limit or _LLM_CANDIDATE_LIMIT))]]

    def _candidate_score(
        self,
        current: dict[str, Any],
        candidate: dict[str, Any],
    ) -> float:
        current_text = normalize_text(current.get("summary", ""))
        candidate_text = normalize_text(candidate.get("summary", ""))
        if not current_text or not candidate_text:
            return 0.0

        current_tokens = set(tokenize(current_text))
        candidate_tokens = set(tokenize(candidate_text))
        similarity = cosine_similarity(embed_text(current_text), embed_text(candidate_text))
        token_overlap = len(current_tokens & candidate_tokens)
        current_entities = {normalize_text(value) for value in current.get("entity_tags", []) if normalize_text(value)}
        candidate_entities = {
            normalize_text(value) for value in candidate.get("entity_tags", []) if normalize_text(value)
        }
        current_topics = {normalize_text(value) for value in current.get("topic_tags", []) if normalize_text(value)}
        candidate_topics = {normalize_text(value) for value in candidate.get("topic_tags", []) if normalize_text(value)}
        entity_overlap = len(current_entities & candidate_entities)
        topic_overlap = len(current_topics & candidate_topics)
        if token_overlap <= 0 and entity_overlap <= 0 and topic_overlap <= 0 and similarity < 0.42:
            return 0.0
        group_bonus = 0.06 if str(current.get("group_id", "") or "") == str(candidate.get("group_id", "") or "") else 0.0
        return round(
            similarity
            + min(token_overlap, 4) * 0.03
            + min(entity_overlap, 3) * 0.18
            + min(topic_overlap, 3) * 0.12
            + group_bonus,
            6,
        )

    def _decide_relations_fallback(
        self,
        current: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], EvolvesDecision]]:
        pairs: list[tuple[dict[str, Any], EvolvesDecision]] = []
        current_text = normalize_text(current.get("summary", ""))
        if not current_text:
            return pairs
        for candidate in candidates:
            candidate_text = normalize_text(candidate.get("summary", ""))
            if not candidate_text:
                continue
            similarity = cosine_similarity(embed_text(current_text), embed_text(candidate_text))
            token_overlap = len(set(tokenize(current_text)) & set(tokenize(candidate_text)))
            current_entities = {
                normalize_text(value) for value in current.get("entity_tags", []) if normalize_text(value)
            }
            candidate_entities = {
                normalize_text(value) for value in candidate.get("entity_tags", []) if normalize_text(value)
            }
            current_topics = {normalize_text(value) for value in current.get("topic_tags", []) if normalize_text(value)}
            candidate_topics = {
                normalize_text(value) for value in candidate.get("topic_tags", []) if normalize_text(value)
            }
            semantic_overlap = len(current_entities & candidate_entities) + len(current_topics & candidate_topics)
            if similarity >= 0.78:
                if abs(len(current_text) - len(candidate_text)) >= 18:
                    decision = EvolvesDecision("enriches", min(0.82, 0.48 + similarity * 0.28), "高相似且信息量不同")
                else:
                    decision = EvolvesDecision("confirms", min(0.84, 0.46 + similarity * 0.3), "高相似重复证据")
                pairs.append((candidate, decision))
                continue
            if (similarity >= 0.5 and token_overlap >= 2) or (semantic_overlap >= 2 and similarity >= 0.32):
                pairs.append(
                    (
                        candidate,
                        EvolvesDecision("enriches", min(0.78, 0.42 + similarity * 0.28), "语义相近且存在补充信息"),
                    )
                )
        return pairs

    async def _decide_relations_with_llm(
        self,
        current: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> tuple[list[tuple[dict[str, Any], EvolvesDecision]], bool]:
        if self.call_ai_api is None:
            return [], False
        candidate_by_id = {str(item.get("memory_id", "") or ""): item for item in candidates}
        messages = [
            {
                "role": "system",
                "content": (
                    "你是记忆演化关系判别器。只判断 current 与 candidates 的关系，不生成聊天回复。\n"
                    "关系定义：replaces=当前记忆替代旧记忆；enriches=当前记忆补充旧记忆；"
                    "confirms=当前记忆确认旧记忆；challenges=当前记忆质疑或冲突旧记忆；none=无关。\n"
                    "不要靠单个关键词下结论，要根据语义、实体、时间和事实关系判断。\n"
                    "只输出 JSON：{\"relations\":[{\"target_memory_id\":\"...\",\"relation_type\":\"replaces|enriches|confirms|challenges|none\",\"confidence\":0.0,\"reason\":\"短原因\"}]}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current": self._render_memory_for_llm(current),
                        "candidates": [self._render_memory_for_llm(item) for item in candidates],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            raw = await self._call_evolves_llm(messages)
            payload = _extract_json_payload(_response_text(raw))
        except Exception as exc:
            self._log_debug(f"[evolves] llm decision failed: {exc}")
            return [], False
        relation_items = _coerce_relation_items(payload)
        pairs: list[tuple[dict[str, Any], EvolvesDecision]] = []
        for item in relation_items:
            target_id = str(item.get("target_memory_id", item.get("memory_id", "")) or "").strip()
            candidate = candidate_by_id.get(target_id)
            if candidate is None:
                continue
            relation = _normalize_relation_type(item.get("relation_type"))
            if relation == "none":
                continue
            confidence = _clamp_float(item.get("confidence"), 0.0, 1.0, default=0.0)
            if confidence < 0.45:
                continue
            reason = str(item.get("reason", "") or "LLM 判定").strip()[:120] or "LLM 判定"
            pairs.append((candidate, EvolvesDecision(relation, confidence, reason)))
        return pairs, True

    async def _call_evolves_llm(self, messages: list[dict[str, Any]]) -> Any:
        assert self.call_ai_api is not None
        try:
            return await self.call_ai_api(messages, temperature=0.0)
        except TypeError:
            signature = None
            try:
                signature = inspect.signature(self.call_ai_api)
            except Exception:
                pass
            if signature is not None and "temperature" not in signature.parameters:
                return await self.call_ai_api(messages)
            raise

    def _render_memory_for_llm(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "memory_id": str(payload.get("memory_id", "") or ""),
            "memory_type": str(payload.get("memory_type", "") or ""),
            "summary": str(payload.get("summary", "") or "")[:500],
            "entity_tags": [str(item) for item in list(payload.get("entity_tags") or [])[:8]],
            "topic_tags": [str(item) for item in list(payload.get("topic_tags") or [])[:8]],
            "group_id": str(payload.get("group_id", "") or ""),
            "time_created": safe_float(payload.get("time_created", 0), 0),
            "confidence": safe_float(payload.get("confidence", 0), 0),
        }

    def _apply_decision_pairs(
        self,
        current: dict[str, Any],
        pairs: list[tuple[dict[str, Any], EvolvesDecision]],
    ) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for candidate, decision in pairs:
            target_id = str(candidate.get("memory_id", "") or "")
            key = (target_id, decision.relation_type)
            if not target_id or key in seen:
                continue
            seen.add(key)
            self._apply_relation(current, candidate, decision)
            decisions.append(
                {
                    "relation_type": decision.relation_type,
                    "source_memory_id": str(current.get("memory_id", "") or ""),
                    "target_memory_id": target_id,
                    "confidence": round(decision.confidence, 4),
                    "reason": decision.reason,
                }
            )
        return decisions

    def _apply_relation(
        self,
        current: dict[str, Any],
        candidate: dict[str, Any],
        decision: EvolvesDecision,
    ) -> None:
        current_id = str(current.get("memory_id", "") or "")
        candidate_id = str(candidate.get("memory_id", "") or "")
        if not current_id or not candidate_id:
            return
        relation = decision.relation_type
        self.memory_store.link_memories(
            source_memory_id=current_id,
            target_ref=candidate_id,
            relation_type=relation,
            weight=decision.confidence,
        )
        self.memory_store.link_memories(
            source_memory_id=candidate_id,
            target_ref=current_id,
            relation_type=f"linked_{relation}",
            weight=max(0.3, decision.confidence - 0.1),
        )
        if relation == "replaces":
            updated = dict(candidate)
            updated["superseded_by"] = current_id
            updated["revision"] = int(updated.get("revision", 1) or 1) + 1
            self.memory_store.write_memory_item(updated)
            return
        if relation == "enriches":
            merged_topics = list(candidate.get("topic_tags") or [])
            for item in current.get("topic_tags") or []:
                value = normalize_text(item)
                if value and value not in merged_topics:
                    merged_topics.append(value)
            enriched = dict(current)
            enriched["topic_tags"] = merged_topics[:12]
            enriched["revision"] = int(enriched.get("revision", 1) or 1) + 1
            self.memory_store.write_memory_item(enriched)
            return
        if relation == "confirms":
            self.memory_store.report_memory_feedback(memory_id=candidate_id, outcome="verified")
            return
        if relation == "challenges":
            current_payload = dict(current)
            candidate_payload = dict(candidate)
            current_conflicts = list(current_payload.get("conflict_refs") or [])
            candidate_conflicts = list(candidate_payload.get("conflict_refs") or [])
            if candidate_id not in current_conflicts:
                current_conflicts.append(candidate_id)
            if current_id not in candidate_conflicts:
                candidate_conflicts.append(current_id)
            current_payload["conflict_refs"] = current_conflicts[:12]
            candidate_payload["conflict_refs"] = candidate_conflicts[:12]
            current_payload["revision"] = int(current_payload.get("revision", 1) or 1) + 1
            candidate_payload["revision"] = int(candidate_payload.get("revision", 1) or 1) + 1
            self.memory_store.write_memory_item(current_payload)
            self.memory_store.write_memory_item(candidate_payload)

    def _log_debug(self, message: str) -> None:
        log = getattr(self.logger, "debug", None)
        if callable(log):
            log(message)


def _normalize_relation_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _RELATION_ALIASES.get(text, text if text in _ALLOWED_RELATIONS else "none")


def _response_text(value: Any) -> str:
    if value is None:
        return ""
    content = getattr(value, "content", None)
    if content is not None:
        return str(content or "")
    return str(value or "")


def _extract_json_payload(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start_candidates = [idx for idx in (raw.find("{"), raw.find("[")) if idx >= 0]
    if not start_candidates:
        return {}
    start = min(start_candidates)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if end <= start:
        return {}
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}


def _coerce_relation_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_items = payload.get("relations", [])
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []
    return [dict(item) for item in raw_items if isinstance(item, dict)]


def _clamp_float(value: Any, low: float, high: float, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))
