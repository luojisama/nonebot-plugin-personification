from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .embedding_index import cosine_similarity, embed_text, normalize_text, tokenize
from .memory_store import MemoryStore
from .search_ranker import safe_float


_REPLACE_HINTS = ("改成", "改为", "现在", "已经", "不再", "换成", "更新为")
_CHALLENGE_HINTS = ("不是", "并非", "不对", "不准确", "存疑", "质疑", "错误", "假的")
_CONFIRM_HINTS = ("确实", "确认", "证实", "没错", "是真的", "同样", "也说")


@dataclass(frozen=True)
class EvolvesDecision:
    relation_type: str
    confidence: float
    reason: str


class EvolvesEngine:
    def __init__(self, memory_store: MemoryStore, logger: Any = None) -> None:
        self.memory_store = memory_store
        self.logger = logger

    def analyze_new_memory(self, memory_id: str, *, group_id: str = "") -> list[EvolvesDecision]:
        _ = memory_id, group_id
        return []

    def process_memory(self, memory_id: str, *, group_id: str = "") -> list[dict[str, Any]]:
        current = self.memory_store.get_memory_item(memory_id)
        if not isinstance(current, dict):
            return []
        candidates = self.memory_store.list_related_memory_candidates(
            memory_id=memory_id,
            group_id=group_id or str(current.get("group_id", "") or ""),
            limit=10,
        )
        decisions: list[dict[str, Any]] = []
        for candidate in candidates:
            decision = self._decide_relation(current, candidate)
            if decision is None:
                continue
            self._apply_relation(current, candidate, decision)
            decisions.append(
                {
                    "relation_type": decision.relation_type,
                    "source_memory_id": str(current.get("memory_id", "") or ""),
                    "target_memory_id": str(candidate.get("memory_id", "") or ""),
                    "confidence": round(decision.confidence, 4),
                    "reason": decision.reason,
                }
            )
        return decisions

    def _decide_relation(
        self,
        current: dict[str, Any],
        candidate: dict[str, Any],
    ) -> EvolvesDecision | None:
        current_text = normalize_text(current.get("summary", ""))
        candidate_text = normalize_text(candidate.get("summary", ""))
        if not current_text or not candidate_text:
            return None

        current_tokens = set(tokenize(current_text))
        candidate_tokens = set(tokenize(candidate_text))
        if not current_tokens or not candidate_tokens:
            return None

        overlap = len(current_tokens & candidate_tokens)
        if overlap <= 0:
            return None

        similarity = cosine_similarity(embed_text(current_text), embed_text(candidate_text))
        current_lower = current_text.lower()
        candidate_lower = candidate_text.lower()
        current_created = safe_float(current.get("time_created", 0), 0)
        candidate_created = safe_float(candidate.get("time_created", 0), 0)
        newer_bias = 0.08 if current_created >= candidate_created else 0.0

        if any(token in current_lower for token in _CHALLENGE_HINTS):
            return EvolvesDecision("challenges", min(0.92, 0.55 + similarity * 0.3), "检测到显式质疑表达")
        if any(token in current_lower for token in _REPLACE_HINTS) and similarity >= 0.2:
            return EvolvesDecision("replaces", min(0.95, 0.6 + similarity * 0.25 + newer_bias), "检测到更新/替代表达")
        if any(token in current_lower for token in _CONFIRM_HINTS) and similarity >= 0.24:
            return EvolvesDecision("confirms", min(0.9, 0.52 + similarity * 0.3), "检测到确认表达")
        if similarity >= 0.75:
            if abs(len(current_text) - len(candidate_text)) >= 16:
                return EvolvesDecision("enriches", min(0.86, 0.5 + similarity * 0.3), "高相似但信息量不同")
            return EvolvesDecision("confirms", min(0.86, 0.48 + similarity * 0.32), "高相似重复证据")
        if similarity >= 0.45 and overlap >= 2:
            return EvolvesDecision("enriches", min(0.82, 0.44 + similarity * 0.32), "主题重叠且出现补充信息")
        return None

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
