from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from .reply_text_policy import normalize_visible_reply_text
from .visible_output import guard_visible_text


EVIDENCE_ENVELOPE_TYPE = "personification_evidence_envelope"
_REVIEW_FIELDS = {
    "action",
    "text",
    "claims_ok",
    "inferences_ok",
    "mechanism_hidden",
}


def _bounded_lines(value: Any, *, limit: int, chars: int) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = []
    result: list[str] = []
    for item in items[:limit]:
        text = " ".join(str(item or "").split())[:chars]
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class EvidenceEnvelope:
    allowed_claims: tuple[str, ...]
    forbidden_inferences: tuple[str, ...]
    confidence: float
    natural_fallback: str
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": EVIDENCE_ENVELOPE_TYPE,
            "available": self.available,
            "allowed_claims": list(self.allowed_claims),
            "forbidden_inferences": list(self.forbidden_inferences),
            "confidence": self.confidence,
            "natural_fallback": self.natural_fallback,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_value(cls, value: Any) -> "EvidenceEnvelope | None":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                return None
        if not isinstance(value, Mapping) or str(value.get("type", "")) != EVIDENCE_ENVELOPE_TYPE:
            return None
        allowed = _bounded_lines(value.get("allowed_claims"), limit=8, chars=320)
        forbidden = _bounded_lines(value.get("forbidden_inferences"), limit=8, chars=320)
        raw_confidence = value.get("confidence", 0.0)
        if isinstance(raw_confidence, bool):
            confidence = 0.0
        else:
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError, OverflowError):
                confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        fallback = normalize_visible_reply_text(str(value.get("natural_fallback", "") or ""))
        fallback = guard_visible_text(
            fallback,
            surface="evidence_natural_fallback",
            allow_direct_media=False,
            allow_control=False,
        )
        if not fallback:
            return None
        return cls(
            allowed_claims=allowed,
            forbidden_inferences=forbidden,
            confidence=max(0.0, min(1.0, confidence)),
            natural_fallback=fallback,
            available=bool(value.get("available", True)),
        )


@dataclass(frozen=True, slots=True)
class EvidenceRenderOutcome:
    text: str
    action: str
    rewrite_used: bool = False


async def render_constrained_evidence(
    envelope: EvidenceEnvelope,
    *,
    tool_caller: Any,
    persona_system: str = "",
    timeout: float = 8.0,
) -> EvidenceRenderOutcome:
    """Render one evidence envelope, then independently review at most one rewrite."""

    fallback = envelope.natural_fallback
    if tool_caller is None:
        return EvidenceRenderOutcome(fallback, "fallback_no_caller")
    evidence_json = json.dumps(
        {
            "allowed_claims": list(envelope.allowed_claims),
            "forbidden_inferences": list(envelope.forbidden_inferences),
            "confidence": envelope.confidence,
            "natural_fallback": fallback,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    candidate_messages: list[dict[str, Any]] = []
    if str(persona_system or "").strip():
        candidate_messages.append(
            {"role": "system", "content": str(persona_system or "").strip()}
        )
    candidate_messages.extend(
        [
            {
                "role": "system",
                "content": (
                    "把内部 evidence envelope 渲染成一句自然群聊口吻。"
                    "只能使用 allowed_claims，不得触碰 forbidden_inferences，"
                    "不得提及系统、工具、接口、摘要、分析流程或内容策略。"
                    "画面中的人物或角色不等于使用头像的用户。"
                    "只输出纯文本短句，不要 Markdown；没有更自然的说法就原样输出 natural_fallback。"
                ),
            },
            {"role": "user", "content": evidence_json},
        ]
    )
    try:
        candidate_response = await asyncio.wait_for(
            tool_caller.chat_with_tools(candidate_messages, [], False),
            timeout=max(1.0, float(timeout)),
        )
        candidate = normalize_visible_reply_text(
            str(getattr(candidate_response, "content", "") or "")
        )
        candidate = guard_visible_text(
            candidate,
            surface="constrained_evidence_candidate",
            allow_direct_media=False,
            allow_control=False,
        )
        if not candidate:
            return EvidenceRenderOutcome(fallback, "fallback_render_failed")

        review_messages = [
            {
                "role": "system",
                "content": (
                    "独立审阅候选句是否严格遵守 evidence envelope。"
                    "只输出一个 JSON object，字段必须且只能是："
                    '{"action":"accept|rewrite|fallback","text":"",'
                    '"claims_ok":true,"inferences_ok":true,"mechanism_hidden":true}。'
                    "accept 时 text 留空；只有候选可通过一次改写修复时使用 rewrite 并给出完整短句；"
                    "无法安全修复时使用 fallback。"
                ),
            },
            {
                "role": "user",
                "content": f"evidence={evidence_json}\ncandidate={candidate[:600]}",
            },
        ]
        review_response = await asyncio.wait_for(
            tool_caller.chat_with_tools(review_messages, [], False),
            timeout=max(1.0, float(timeout)),
        )
        review = json.loads(str(getattr(review_response, "content", "") or "").strip())
        if not isinstance(review, dict) or set(review) != _REVIEW_FIELDS:
            return EvidenceRenderOutcome(fallback, "fallback_review_invalid")
        if not all(
            review.get(field) is True
            for field in ("claims_ok", "inferences_ok", "mechanism_hidden")
        ):
            return EvidenceRenderOutcome(fallback, "fallback_review_rejected")
        action = str(review.get("action", "") or "").strip().lower()
        if action == "accept":
            return EvidenceRenderOutcome(candidate, "accepted")
        if action != "rewrite":
            return EvidenceRenderOutcome(fallback, "fallback_review_requested")
        rewritten = normalize_visible_reply_text(str(review.get("text", "") or ""))
        rewritten = guard_visible_text(
            rewritten,
            surface="constrained_evidence_rewrite",
            allow_direct_media=False,
            allow_control=False,
        )
        if not rewritten:
            return EvidenceRenderOutcome(fallback, "fallback_rewrite_invalid")
        return EvidenceRenderOutcome(rewritten, "rewritten", rewrite_used=True)
    except Exception:
        return EvidenceRenderOutcome(fallback, "fallback_exception")


__all__ = [
    "EVIDENCE_ENVELOPE_TYPE",
    "EvidenceEnvelope",
    "EvidenceRenderOutcome",
    "render_constrained_evidence",
]
