from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

safety_filter = load_personification_module("plugin.personification.core.safety_filter")


def _resp(finish_reason="stop", content="", raw=None):
    return SimpleNamespace(finish_reason=finish_reason, content=content, raw=raw)


# ──────────────────────── detect_api_block ────────────────────────

def test_detect_openai_content_filter() -> None:
    raw = {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]}
    assert safety_filter.detect_api_block(_resp(raw=raw)).startswith("openai:")


def test_detect_gemini_block_reason() -> None:
    raw = {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
    assert "SAFETY" in safety_filter.detect_api_block(_resp(raw=raw))


def test_detect_gemini_candidate_finish_reason() -> None:
    raw = {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]}
    assert safety_filter.detect_api_block(_resp(raw=raw)).startswith("gemini:")


def test_detect_anthropic_refusal() -> None:
    raw = {"stop_reason": "refusal", "content": []}
    assert safety_filter.detect_api_block(_resp(raw=raw)).startswith("anthropic:")


def test_detect_finish_reason_on_response_itself() -> None:
    assert safety_filter.detect_api_block(_resp(finish_reason="content_filter"))


def test_normal_response_not_flagged() -> None:
    assert safety_filter.detect_api_block(_resp(finish_reason="stop", content="正常回复")) == ""
    # Gemini 正常响应：blockReason 为 UNSPECIFIED，candidates 正常 STOP
    raw = {"promptFeedback": {"blockReason": "BLOCK_REASON_UNSPECIFIED"},
           "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hi"}]}}]}
    assert safety_filter.detect_api_block(_resp(content="hi", raw=raw)) == ""


def test_none_and_garbage_safe() -> None:
    assert safety_filter.detect_api_block(None) == ""
    assert safety_filter.detect_api_block(_resp(raw=object())) == ""


# ──────────────────────── sanitize_or_retry 集成 ────────────────────────

def test_sanitize_retries_on_api_block_then_raises() -> None:
    blocked = _resp(content="", raw={"candidates": [{"finishReason": "SAFETY"}]})
    calls = {"n": 0}

    async def _call():
        calls["n"] += 1
        return blocked

    async def run():
        try:
            await safety_filter.sanitize_or_retry(call=_call, retry_call=_call, purpose="t")
            return None
        except safety_filter.SafetyRefusalError as e:
            return e

    err = asyncio.run(run())
    assert err is not None
    assert err.source == "api_block"
    assert "SAFETY" in err.reason
    assert calls["n"] == 2  # 首次 + 重试各一次


def test_sanitize_recovers_when_retry_succeeds() -> None:
    blocked = _resp(content="", raw={"candidates": [{"finishReason": "SAFETY"}]})
    good = _resp(content="正常画像内容")
    seq = [blocked, good]

    async def _call():
        return seq.pop(0)

    async def run():
        return await safety_filter.sanitize_or_retry(call=_call, retry_call=_call, purpose="t")

    out = asyncio.run(run())
    assert out is good


def test_sanitize_passes_through_normal() -> None:
    good = _resp(content="正常内容", finish_reason="stop")

    async def _call():
        return good

    out = asyncio.run(safety_filter.sanitize_or_retry(call=_call, purpose="t"))
    assert out is good
