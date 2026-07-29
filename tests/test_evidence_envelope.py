from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


evidence = load_personification_module("plugin.personification.core.evidence_envelope")


class _Caller:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        assert tools == []
        assert use_builtin_search is False
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.responses.pop(0))


def _envelope():  # noqa: ANN202
    return evidence.EvidenceEnvelope(
        allowed_claims=("头像里像是个金发红眼、比着剪刀手的动漫角色。",),
        forbidden_inferences=(
            "不得把头像里的角色说成用户本人。",
            "不得提及系统、工具、接口或内部分析。",
        ),
        confidence=0.9,
        natural_fallback="看得到，头像里像是个金发红眼、比着剪刀手的动漫角色。",
    )


def test_evidence_envelope_roundtrip_is_bounded_and_internal_only() -> None:
    envelope = _envelope()
    parsed = evidence.EvidenceEnvelope.from_value(envelope.to_json())

    assert parsed == envelope
    payload = json.loads(envelope.to_json())
    assert payload["type"] == "personification_evidence_envelope"
    assert set(payload) == {
        "type",
        "available",
        "allowed_claims",
        "forbidden_inferences",
        "confidence",
        "natural_fallback",
    }
    assert "url" not in envelope.to_json().lower()
    assert evidence.EvidenceEnvelope.from_value({"type": "other"}) is None


def test_constrained_evidence_render_accepts_reviewed_persona_candidate() -> None:
    caller = _Caller(
        [
            "看得到，金发红眼还比着剪刀手，挺显眼的。",
            json.dumps(
                {
                    "action": "accept",
                    "text": "",
                    "claims_ok": True,
                    "inferences_ok": True,
                    "mechanism_hidden": True,
                },
                ensure_ascii=False,
            ),
        ]
    )

    outcome = asyncio.run(
        evidence.render_constrained_evidence(
            _envelope(),
            tool_caller=caller,
            persona_system="你是说话简短的群友。",
        )
    )

    assert outcome.text == "看得到，金发红眼还比着剪刀手，挺显眼的。"
    assert outcome.action == "accepted"
    assert outcome.rewrite_used is False
    assert len(caller.calls) == 2
    assert caller.calls[0][0]["content"] == "你是说话简短的群友。"


def test_constrained_evidence_review_failure_returns_natural_fallback() -> None:
    caller = _Caller(
        [
            "工具显示你本人就是个金发女生。",
            json.dumps(
                {
                    "action": "fallback",
                    "text": "",
                    "claims_ok": False,
                    "inferences_ok": False,
                    "mechanism_hidden": False,
                },
                ensure_ascii=False,
            ),
        ]
    )

    outcome = asyncio.run(
        evidence.render_constrained_evidence(_envelope(), tool_caller=caller)
    )

    assert outcome.text == _envelope().natural_fallback
    assert outcome.action == "fallback_review_rejected"
    assert "工具" not in outcome.text
    assert "你本人" not in outcome.text


@pytest.mark.parametrize(
    "bad_candidate",
    [
        "系统发的安全摘要说这是动漫角色。",
        "工具显示这是金发红眼角色。",
        "安全分析结果是个动漫角色。",
        "我只能通过接口看到这是动漫头像。",
    ],
)
def test_bad_internal_mechanism_corpus_falls_back(bad_candidate: str) -> None:
    caller = _Caller(
        [
            bad_candidate,
            json.dumps(
                {
                    "action": "fallback",
                    "text": "",
                    "claims_ok": True,
                    "inferences_ok": True,
                    "mechanism_hidden": False,
                },
                ensure_ascii=False,
            ),
        ]
    )

    outcome = asyncio.run(
        evidence.render_constrained_evidence(_envelope(), tool_caller=caller)
    )

    assert outcome.text == _envelope().natural_fallback
    assert all(
        marker not in outcome.text
        for marker in ("系统发", "工具显示", "安全分析", "通过接口")
    )
