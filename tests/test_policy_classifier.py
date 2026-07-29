from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module


policy_classifier = load_personification_module("plugin.personification.core.policy_classifier")


def _response(
    *,
    verdict: str,
    category: str = "harassment",
    intent: str = "targeted_attack",
    severity: str = "high",
    confidence: float = 0.95,
    reason_code: str = "classified",
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "category": category,
            "intent": intent,
            "severity": severity,
            "confidence": confidence,
            "reason_code": reason_code,
        }
    )


class _Caller:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def chat_with_tools(self, messages, _tools, _builtin_search):  # noqa: ANN001, ANN201
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response)


def _event():  # noqa: ANN202
    return policy_classifier.PolicyEventInput(
        user_id="10001",
        event_id="event-1",
        surface="qq_group",
        text="待判断文本",
        is_direct=False,
        context="引用内容不是用户立场",
    )


def test_boundary_topic_does_not_require_confirmation() -> None:
    caller = _Caller(
        [
            _response(
                verdict="boundary_topic",
                category="political_sensitive",
                intent="neutral_mention",
                severity="low",
                confidence=0.91,
                reason_code="ordinary_political_mention",
            )
        ]
    )

    result = asyncio.run(policy_classifier.PolicyClassifier(caller).classify(_event()))

    assert result.verdict == "boundary_topic"
    assert result.confirmed is True
    assert len(caller.calls) == 1
    assert "待判断文本" in caller.calls[0][1]["content"]
    assert "待判断文本" not in caller.calls[0][0]["content"]


def test_violation_requires_independent_confirmation() -> None:
    caller = _Caller(
        [
            _response(verdict="confirmed_violation", reason_code="primary_abuse"),
            _response(verdict="confirmed_violation", reason_code="independent_abuse"),
        ]
    )

    result = asyncio.run(policy_classifier.PolicyClassifier(caller).classify(_event()))

    assert result.verdict == "confirmed_violation"
    assert result.confirmed is True
    assert result.confidence == 0.95
    assert result.reason_code == "primary_abuse"
    assert len(caller.calls) == 2
    assert "不要参考前一个模型的理由" in caller.calls[1][0]["content"]
    assert "primary_abuse" not in json.dumps(caller.calls[1], ensure_ascii=False)


def test_critical_requires_both_classifiers_to_agree() -> None:
    caller = _Caller(
        [
            _response(
                verdict="critical_violation",
                category="threat",
                intent="credible_threat",
                severity="critical",
                reason_code="credible_threat",
            ),
            _response(
                verdict="critical_violation",
                category="threat",
                intent="credible_threat",
                severity="critical",
                confidence=0.9,
                reason_code="confirmed_threat",
            ),
        ]
    )

    result = asyncio.run(policy_classifier.PolicyClassifier(caller).classify(_event()))

    assert result.verdict == "critical_violation"
    assert result.severity == "critical"
    assert result.confidence == 0.9
    assert result.confirmed is True


def test_single_critical_vote_is_downgraded_to_confirmed_noncritical() -> None:
    caller = _Caller(
        [
            _response(
                verdict="critical_violation",
                category="threat",
                intent="credible_threat",
                severity="critical",
                reason_code="primary_threat",
            ),
            _response(
                verdict="confirmed_violation",
                category="threat",
                intent="targeted_attack",
                severity="high",
                reason_code="noncritical_confirmation",
            ),
        ]
    )

    result = asyncio.run(policy_classifier.PolicyClassifier(caller).classify(_event()))

    assert result.verdict == "confirmed_violation"
    assert result.severity == "high"
    assert result.confirmed is True


def test_rejected_or_unavailable_confirmation_fails_closed_to_boundary() -> None:
    rejected = _Caller(
        [
            _response(verdict="confirmed_violation"),
            _response(
                verdict="boundary_topic",
                category="other",
                intent="uncertain",
                severity="low",
                confidence=0.7,
            ),
        ]
    )
    unavailable = _Caller([_response(verdict="confirmed_violation"), RuntimeError("offline")])

    rejected_result = asyncio.run(
        policy_classifier.PolicyClassifier(rejected).classify(_event())
    )
    unavailable_result = asyncio.run(
        policy_classifier.PolicyClassifier(unavailable).classify(_event())
    )

    assert rejected_result.verdict == "boundary_topic"
    assert rejected_result.confirmed is False
    assert rejected_result.reason_code == "violation_confirmation_failed"
    assert unavailable_result.verdict == "boundary_topic"
    assert unavailable_result.confirmed is False
    assert unavailable_result.confidence == 0.0


def test_invalid_primary_response_is_quarantined_without_guessing() -> None:
    caller = _Caller(["not-json"])

    result = asyncio.run(policy_classifier.PolicyClassifier(caller).classify(_event()))

    assert result.verdict == "boundary_topic"
    assert result.intent == "uncertain"
    assert result.confirmed is False
    assert result.reason_code == "classifier_unavailable"
    assert len(caller.calls) == 1
