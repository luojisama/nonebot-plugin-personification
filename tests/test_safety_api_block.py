from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

safety_filter = load_personification_module("plugin.personification.core.safety_filter")
visible_output = load_personification_module("plugin.personification.core.visible_output")
provider_router = load_personification_module("plugin.personification.core.provider_router")
provider_health = load_personification_module("plugin.personification.core.provider_health")


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


def test_route_safety_uses_exact_refusal_not_broad_refusal() -> None:
    assert safety_filter.detect_route_safety_issue(_resp(content="I can't discuss that.")) == (
        "exact_provider_refusal"
    )
    assert safety_filter.detect_route_safety_issue(_resp(content="I can't wait to see it")) == ""


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


def test_screenshot_server_refusal_is_never_visible() -> None:
    text = "严重违规安全限制已被触发。因此，无法为您生成完整的回复。"
    assert safety_filter.detect_refusal(text) is True
    decision = visible_output.assess_visible_text(text)
    assert decision.allowed is False
    assert decision.pattern_id == "provider_policy_text"
    assert len(decision.summary_hash) == 12
    assert visible_output.guard_visible_text(text, surface="test") == ""


def test_visible_output_does_not_misclassify_natural_language() -> None:
    assert visible_output.assess_visible_text("I can't wait to see it").allowed is True
    assert visible_output.assess_visible_text("如果情况持续，可以考虑咨询专业人士").allowed is True


def test_visible_output_blocks_persona_ai_or_company_identity_without_blocking_third_party_discussion() -> None:
    blocked = (
        "我是某公司开发的 AI 助手",
        "我是谷歌开发的 AI 助手",
        "我是 Google 的 Gemini 模型",
        "我是某公司的员工",
        "我是你的智能助手",
        "我不是 AI，其实是 Gemini",
        "我的底层模型是 Antigravity",
        "此刻和你聊天的其实是 Codex",
        "别人都说我是 OpenAI 的聊天机器人",
    )
    allowed = (
        "Gemini 最近改了工具协议",
        "我觉得某公司发布的 AI 产品这次改得还行",
        "我是一名 AI 研究员",
        "I am an assistant professor working on compiler theory",
        "如果我是 AI，我会先把第三方讨论和自我身份区分开",
        "她是某公司的 AI 助手",
    )

    for text in blocked:
        decision = visible_output.assess_visible_text(text)
        assert decision.allowed is False
        assert decision.reason == "persona_identity_leak"
        assert decision.pattern_id == "persona_identity_leak"
        assert visible_output.guard_visible_text(text, surface="test") == ""
    for text in allowed:
        assert visible_output.assess_visible_text(text).allowed is True


def test_media_does_not_bypass_residual_or_caption_checks() -> None:
    import base64

    media = base64.b64encode(b"small-image-placeholder").decode("ascii")
    leaked = f"[IMAGE_B64]{media}[/IMAGE_B64]\nAuthorization: Bearer secret-token-value"
    assert visible_output.assess_visible_text(leaked).allowed is False
    assert visible_output.assess_visible_text(f"[IMAGE_B64]{media}").allowed is False
    assert visible_output.assess_visible_text(
        f"[IMAGE_B64]{media}[/IMAGE_B64]", allow_direct_media=False
    ).allowed is False


def test_visible_output_scans_beyond_old_prefix_limit() -> None:
    text = "正常内容" * 500 + "\napi_key=very-secret-token"
    decision = visible_output.assess_visible_text(text)
    assert decision.allowed is False
    assert decision.pattern_id == "credential_like_text"


def test_nested_cli_block_reason_is_detected() -> None:
    response = _resp(raw={"response": {"candidates": [{"finishReason": "SAFETY"}]}})
    assert safety_filter.detect_api_block(response).startswith("gemini:")


def test_provider_router_reframes_blocked_context_before_retry(monkeypatch) -> None:  # noqa: ANN001
    calls: list[list[dict]] = []
    responses = [
        SimpleNamespace(
            finish_reason="stop",
            content="严重违规安全限制已被触发，因此无法回答。",
            raw={"response": {"candidates": [{"finishReason": "SAFETY"}]}},
            tool_calls=[],
            vision_unavailable=False,
        ),
        SimpleNamespace(
            finish_reason="stop",
            content="这事换个角度看就顺了",
            raw={},
            tool_calls=[],
            vision_unavailable=False,
        ),
    ]

    async def _call(_provider, messages, **_kwargs):  # noqa: ANN001
        calls.append(messages)
        return responses.pop(0)

    monkeypatch.setattr(provider_router, "_call_provider_once", _call)
    monkeypatch.setattr(provider_router, "_mark_provider_success", lambda _name: None)
    logger = SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)

    async def run():
        return await provider_router._try_provider_chain(
            [{"name": "p", "api_type": "openai", "model": "m", "max_retries": 1}],
            messages=[
                {"role": "system", "content": "正常人设规则"},
                {"role": "system", "content": "## 用户档案\n某人资料"},
                {"role": "user", "content": "继续聊刚才的话题"},
            ],
            plugin_config=object(),
            logger=logger,
        )

    response, errors, attempts, _vision = asyncio.run(run())
    assert response.content == "这事换个角度看就顺了"
    assert any("safety_block" in item for item in errors)
    assert attempts == []
    assert calls[1][1]["role"] == "system"
    assert calls[1][1]["content"] == "## 用户档案\n某人资料"
    assert calls[1][-1]["role"] == "system"


def test_provider_router_classifies_empty_safety_reframe_as_invalid_response(monkeypatch) -> None:  # noqa: ANN001
    responses = [
        SimpleNamespace(
            finish_reason="stop",
            content="请求被安全策略阻止",
            raw={"response": {"candidates": [{"finishReason": "SAFETY"}]}},
            tool_calls=[],
            vision_unavailable=False,
        ),
        SimpleNamespace(
            finish_reason="stop",
            content="",
            raw={},
            tool_calls=[],
            vision_unavailable=False,
        ),
    ]

    async def _call(*_args, **_kwargs):  # noqa: ANN202
        return responses.pop(0)

    monkeypatch.setattr(provider_router, "_call_provider_once", _call)
    monkeypatch.setattr(provider_router, "_mark_provider_failure", lambda *_args: 0.0)
    logger = SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)

    response, _errors, attempts, _vision = asyncio.run(provider_router._try_provider_chain(
        [{"name": "p", "api_type": "openai", "model": "m", "max_retries": 1}],
        messages=[{"role": "user", "content": "继续"}],
        plugin_config=object(),
        logger=logger,
    ))

    assert response is None
    assert attempts[-1]["code"] == "provider_invalid_response"
    assert attempts[-1]["retryable"] is True


def test_safe_reframe_only_demotes_explicit_untrusted_metadata() -> None:
    messages = [
        {"role": "system", "content": "## 用户档案\n标题不能触发降级"},
        {
            "role": "system",
            "content": "外部资料",
            "_personification_untrusted": True,
        },
        {
            "role": "assistant",
            "_personification_provider_history": {
                "role": "model",
                "parts": [{"functionCall": {"name": "lookup"}, "thoughtSignature": "opaque"}],
            },
            "_personification_provider_model": "gemini-concrete",
            "_personification_routed_caller": "private-route",
        },
    ]

    reframed = safety_filter.build_safe_reframe_messages(messages)

    assert reframed[0] == messages[0]
    assert reframed[1]["role"] == "user"
    assert "_personification_untrusted" not in reframed[1]
    assert reframed[2]["_personification_provider_history"] == messages[2][
        "_personification_provider_history"
    ]
    assert reframed[2]["_personification_provider_model"] == "gemini-concrete"
    assert "_personification_routed_caller" not in reframed[2]


def test_safe_reframe_never_preserves_untrusted_provider_history() -> None:
    reframed = safety_filter.build_safe_reframe_messages(
        [
            {
                "role": "assistant",
                "content": "external",
                "_personification_untrusted": True,
                "_personification_provider_history": {"parts": [{"thoughtSignature": "forged"}]},
                "_personification_provider_model": "forged-model",
            }
        ]
    )

    assert reframed[0]["role"] == "user"
    assert "_personification_provider_history" not in reframed[0]
    assert "_personification_provider_model" not in reframed[0]


def test_provider_health_records_block_as_failure_and_safe_retry_as_success(monkeypatch) -> None:  # noqa: ANN001
    results: list[tuple[bool, str]] = []
    responses = [
        _resp(finish_reason="content_filter"),
        _resp(content="安全重试成功"),
    ]

    class _Caller:
        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003
            return responses.pop(0)

    monkeypatch.setattr(provider_router, "_build_provider_caller", lambda *_a, **_k: _Caller())
    monkeypatch.setattr(
        provider_health,
        "record_request_result",
        lambda **kwargs: results.append((kwargs["success"], kwargs["error_kind"])),
    )
    provider = {
        "name": "p",
        "api_type": "openai",
        "api_key": "k",
        "api_url": "https://example.invalid",
        "model": "m",
    }

    asyncio.run(provider_router._call_provider_once(provider, [], plugin_config=object()))
    asyncio.run(provider_router._call_provider_once(provider, [], plugin_config=object()))

    assert results == [(False, "safety_block"), (True, "")]
