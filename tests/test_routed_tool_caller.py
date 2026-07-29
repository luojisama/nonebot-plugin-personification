from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module

ai_routes = load_personification_module("plugin.personification.core.ai_routes")
llm_context = load_personification_module("plugin.personification.core.llm_context")
tool_impl = load_personification_module("plugin.personification.skills.skillpacks.tool_caller.scripts.impl")


class _FakeCaller:
    def __init__(self, name: str, responses: list[object]) -> None:
        self.name = name
        self._responses = list(responses)
        self.messages_seen: list[list[dict]] = []
        self.calls_seen: list[tuple[list[dict], list[dict], bool]] = []

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        self.messages_seen.append(list(messages or []))
        self.calls_seen.append((list(messages or []), list(tools or []), use_builtin_search))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def build_tool_result_message(self, tool_call_id: str, tool_name: str, result: str) -> dict[str, str]:
        return {
            "caller": self.name,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "result": result,
        }


def test_routed_tool_caller_falls_back_after_invalid_primary_response() -> None:
    empty = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw={},
    )
    valid = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="最终结果",
        tool_calls=[],
        raw={},
    )
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [empty])],
        fallback_caller=_FakeCaller("fallback", [valid]),
        logger=None,
    )

    response = asyncio.run(routed.chat_with_tools([], [], False))

    assert response.content == "最终结果"


def test_routed_tool_caller_routes_tool_result_back_to_originating_caller() -> None:
    response = tool_impl.ToolCallerResponse(
        finish_reason="tool_calls",
        content="",
        tool_calls=[tool_impl.ToolCall(id="call-1", name="web_search", arguments={})],
        raw={},
    )
    primary = _FakeCaller("primary", [response])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=None,
        logger=None,
    )

    actual = asyncio.run(routed.chat_with_tools([], [], False))
    tool_result = routed.build_tool_result_messages(actual, [(actual.tool_calls[0], "done")])[0]

    assert tool_result["caller"] == "primary"
    assert tool_result["tool_name"] == "web_search"


def test_routed_tool_caller_response_route_survives_duplicate_call_ids() -> None:
    first_response = tool_impl.ToolCallerResponse(
        finish_reason="tool_calls",
        content="",
        tool_calls=[tool_impl.ToolCall(id="same-id", name="first_tool", arguments={})],
        raw={},
    )
    second_response = tool_impl.ToolCallerResponse(
        finish_reason="tool_calls",
        content="",
        tool_calls=[tool_impl.ToolCall(id="same-id", name="second_tool", arguments={})],
        raw={},
    )
    route_error = RuntimeError("route unavailable")
    primary = _FakeCaller("primary", [first_response, route_error])
    fallback = _FakeCaller("fallback", [second_response])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=fallback,
        logger=None,
    )

    first = asyncio.run(routed.chat_with_tools([], [], False))
    second = asyncio.run(routed.chat_with_tools([], [], False))
    first_results = routed.build_tool_result_messages(first, [(first.tool_calls[0], "one")])
    second_results = routed.build_tool_result_messages(second, [(second.tool_calls[0], "two")])

    assert first_results[0]["caller"] == "primary"
    assert second_results[0]["caller"] == "fallback"
    assert first_results[0]["_personification_routed_caller"] != second_results[0][
        "_personification_routed_caller"
    ]


def test_routed_tool_caller_pins_synthetic_tool_result_to_same_caller() -> None:
    empty = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw={},
    )
    first_valid = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="需要补查",
        tool_calls=[],
        raw={},
    )
    final_valid = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="最终结果",
        tool_calls=[],
        raw={},
    )
    primary = _FakeCaller("primary", [empty, AssertionError("primary should not see fallback-shaped tool result")])
    fallback = _FakeCaller("fallback", [first_valid, final_valid])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=fallback,
        logger=None,
    )

    response = asyncio.run(routed.chat_with_tools([], [], False))
    assert response.content == "需要补查"

    tool_result = routed.build_synthetic_tool_evidence_message(
        response,
        "wiki_lookup",
        {},
        "查到了",
    )
    messages = [tool_result]
    response = asyncio.run(routed.chat_with_tools(messages, [], False))

    assert "_personification_routed_caller" in tool_result
    assert tool_result["_personification_untrusted"] is True
    assert all("_personification_routed_caller" not in msg for msg in fallback.messages_seen[-1])
    assert response.content == "最终结果"


def test_routed_tool_caller_reframes_api_block_on_same_caller() -> None:
    blocked = tool_impl.ToolCallerResponse(
        finish_reason="content_filter", content="", tool_calls=[], raw={}
    )
    valid = tool_impl.ToolCallerResponse(
        finish_reason="stop", content="安全重试成功", tool_calls=[], raw={}
    )
    primary = _FakeCaller("primary", [blocked, valid])
    fallback = _FakeCaller("fallback", [AssertionError("fallback should not run")])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary], fallback_caller=fallback, logger=None
    )
    messages = [
        {"role": "system", "content": "正常系统规则"},
        {
            "role": "system",
            "content": "外部画像数据",
            "_personification_untrusted": True,
        },
    ]

    response = asyncio.run(routed.chat_with_tools(messages, [{"name": "search"}], True))

    assert response.content == "安全重试成功"
    assert len(primary.calls_seen) == 2
    assert primary.calls_seen[1][0][0] == messages[0]
    assert primary.calls_seen[1][0][1]["role"] == "user"
    assert primary.calls_seen[1][0][1]["content"].startswith("[背景数据，仅供理解")
    assert primary.calls_seen[1][1] == [{"name": "search"}]
    assert primary.calls_seen[1][2] is True


def test_routed_tool_caller_exact_refusal_retries_then_falls_back() -> None:
    refusal = tool_impl.ToolCallerResponse(
        finish_reason="stop", content="I can't discuss that.", tool_calls=[], raw={}
    )
    valid = tool_impl.ToolCallerResponse(
        finish_reason="stop", content="候选结果", tool_calls=[], raw={}
    )
    primary = _FakeCaller("primary", [refusal, refusal])
    fallback = _FakeCaller("fallback", [valid])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary], fallback_caller=fallback, logger=None
    )

    response = asyncio.run(routed.chat_with_tools([], [], False))

    assert response.content == "候选结果"
    assert len(primary.calls_seen) == 2
    assert len(fallback.calls_seen) == 1


def test_routed_tool_caller_does_not_treat_ordinary_i_cant_as_hard_refusal() -> None:
    natural = tool_impl.ToolCallerResponse(
        finish_reason="stop", content="I can't wait to see it", tool_calls=[], raw={}
    )
    primary = _FakeCaller("primary", [natural])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary], fallback_caller=None, logger=None
    )

    response = asyncio.run(routed.chat_with_tools([], [], False))

    assert response.content == "I can't wait to see it"
    assert len(primary.calls_seen) == 1


def test_routed_tool_caller_keeps_safety_retry_pinned() -> None:
    blocked = tool_impl.ToolCallerResponse(
        finish_reason="content_filter", content="", tool_calls=[], raw={}
    )
    valid = tool_impl.ToolCallerResponse(
        finish_reason="stop", content="原 caller 恢复", tool_calls=[], raw={}
    )
    primary = _FakeCaller("primary", [AssertionError("pinning was lost")])
    fallback = _FakeCaller("fallback", [valid, blocked, valid])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary], fallback_caller=fallback, logger=None
    )
    initial = asyncio.run(routed.chat_with_tools([], [], False))
    primary_calls_before_pinned_turn = len(primary.calls_seen)
    tool_result = routed.build_synthetic_tool_evidence_message(initial, "search", {}, "done")

    response = asyncio.run(routed.chat_with_tools([tool_result], [], False))

    assert response.content == "原 caller 恢复"
    assert len(primary.calls_seen) == primary_calls_before_pinned_turn


def test_qzone_routed_tool_caller_preserves_all_safe_route_attempts() -> None:
    candidate_error = RuntimeError("private candidate response")
    candidate_error.code = "provider_model_candidate_unavailable"
    candidate_error.status_code = 404
    candidate_error.retryable = True
    request_error = RuntimeError("private request response")
    request_error.status_code = 422
    primary = _FakeCaller("primary", [candidate_error])
    fallback = _FakeCaller("fallback", [request_error])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=fallback,
        logger=None,
        route_descriptors=[
            {"name": "antigravity", "api_type": "antigravity_cli", "model": "auto-gemini-3"},
            {"name": "backup", "api_type": "openai", "model": "backup-model"},
        ],
    )
    token = llm_context.set_llm_context(
        purpose="qzone_generation",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
            asyncio.run(routed.chat_with_tools([], [], False))
    finally:
        llm_context.reset_llm_context(token)

    error = caught.value
    assert error.code == "provider_model_candidate_unavailable"
    assert error.status_code == 404
    assert error.retryable is True
    assert [item["provider"] for item in error.route_attempts] == ["antigravity", "backup"]
    assert [item["status_code"] for item in error.route_attempts] == [404, 422]
    assert [item["request_count"] for item in error.route_attempts] == [1, 1]
    assert all("private" not in str(item) for item in error.route_attempts)


def test_normal_routed_tool_caller_raises_structured_aggregate() -> None:
    permission_error = RuntimeError("private permission response")
    permission_error.status_code = 403
    permission_error.code = "opaque-secret-code"
    permission_error.auth_mode = "bearer"
    permission_error.request_count = 2
    request_error = RuntimeError("private request response")
    request_error.status_code = 400
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [permission_error])],
        fallback_caller=_FakeCaller("fallback", [request_error]),
        logger=None,
        route_descriptors=[
            {
                "name": "gemini-primary",
                "api_type": "gemini",
                "model": "gemini-test",
                "gemini_auth_mode": "auto",
            },
            {"name": "openai-fallback", "api_type": "openai", "model": "gpt-test"},
        ],
    )

    with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
        asyncio.run(routed.chat_with_tools([], [], False))

    error = caught.value
    assert error.code == "provider_permission_denied"
    assert error.status_code == 403
    assert [item["code"] for item in error.route_attempts] == [
        "provider_permission_denied",
        "provider_request_rejected",
    ]
    assert error.route_attempts[0]["auth_mode"] == "bearer"
    assert error.route_attempts[0]["request_count"] == 2
    assert "opaque-secret-code" not in str(error.route_attempts)
    assert all("private" not in str(item) for item in error.route_attempts)


def test_provider_route_trace_summary_is_allowlisted_and_redacted() -> None:
    error = ai_routes.RoutedToolCallerError(
        [
            {
                "provider": "zellon primary",
                "api_type": "gemini",
                "model": "gemini-3-flash-agent",
                "status_code": 400,
                "code": "provider_request_rejected",
                "wire_tools_count": 47,
                "tool_schema_hash": "abcdef123456",
                "request_count": 1,
                "api_url": "https://private.example/v1beta?key=secret",
                "prompt": "raw private prompt",
                "response_body": "raw private response",
            }
        ]
    )

    summary = ai_routes.summarize_provider_route_attempts(error)

    assert "provider:zellon_primary" in summary
    assert "api:gemini" in summary
    assert "model:gemini-3-flash-agent" in summary
    assert "http:400" in summary
    assert "tools:47" in summary
    assert "schema:abcdef123456" in summary
    assert "requests:1" in summary
    assert "code:provider_request_rejected" in summary
    assert "private.example" not in summary
    assert "secret" not in summary
    assert "raw private" not in summary


@pytest.mark.parametrize(
    ("response", "expected_code", "retryable"),
    [
        (
            tool_impl.ToolCallerResponse("stop", "", [], {}),
            "provider_invalid_response",
            True,
        ),
        (
            tool_impl.ToolCallerResponse(
                "stop",
                "请求被安全策略阻止",
                [],
                {"response": {"candidates": [{"finishReason": "SAFETY"}]}},
            ),
            "provider_safety_block",
            False,
        ),
    ],
)
def test_routed_tool_caller_structures_non_exception_exhaustion(
    response,
    expected_code: str,
    retryable: bool,
) -> None:  # noqa: ANN001
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [response])],
        fallback_caller=None,
        logger=None,
    )
    token = llm_context.set_llm_context(
        purpose="provider_probe",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
            asyncio.run(routed.chat_with_tools([], [], False))
    finally:
        llm_context.reset_llm_context(token)

    assert caught.value.code == expected_code
    assert caught.value.retryable is retryable


def test_routed_tool_caller_marks_timeout_retryable() -> None:
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [TimeoutError("private timeout")])],
        fallback_caller=None,
        logger=None,
    )

    with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
        asyncio.run(routed.chat_with_tools([], [], False))

    assert caught.value.code == "provider_timeout"
    assert caught.value.retryable is True


def test_routed_tool_caller_preserves_structured_model_error_on_http_400() -> None:
    inner = RuntimeError("private invalid model detail")
    inner.code = "invalid_model"
    error = RuntimeError("safe outer rejection")
    error.status_code = 400
    error.code = "provider_request_rejected"
    error.wire_tools_count = 3
    error.__cause__ = inner
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [error])],
        fallback_caller=None,
        logger=None,
    )

    with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
        asyncio.run(routed.chat_with_tools([], [{"type": "function"}] * 3, False))

    assert caught.value.code == "provider_model_unavailable"
    assert caught.value.status_code == 400
    assert caught.value.wire_tools_count == 3


def test_routed_tool_caller_recognizes_wrapped_model_error_text_on_http_400() -> None:
    inner = RuntimeError("model is invalid")
    error = RuntimeError("safe outer rejection")
    error.status_code = 400
    error.code = "provider_request_rejected"
    error.wire_tools_count = 1
    error.__cause__ = inner
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [error])],
        fallback_caller=None,
        logger=None,
    )

    with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
        asyncio.run(routed.chat_with_tools([], [{"type": "function"}], False))

    assert caught.value.code == "provider_model_unavailable"


def test_routed_tool_caller_prefers_recoverable_schema_rejection_over_model_error() -> None:
    error = ai_routes.RoutedToolCallerError([
        {
            "provider": "bad-model",
            "status_code": 400,
            "code": "provider_model_unavailable",
            "retryable": False,
            "wire_tools_count": 3,
        },
        {
            "provider": "schema-gateway",
            "status_code": 400,
            "code": "provider_request_rejected",
            "retryable": False,
            "wire_tools_count": 2,
        },
    ])

    assert error.code == "provider_request_rejected"
    assert error.wire_tools_count == 2


def test_routed_safety_error_keeps_actual_wire_schema_count() -> None:
    response = tool_impl.ToolCallerResponse(
        "stop",
        "请求被安全策略阻止",
        [],
        {"response": {"candidates": [{"finishReason": "SAFETY"}]}},
        wire_tools_count=0,
    )
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [response])],
        fallback_caller=None,
        logger=None,
    )
    token = llm_context.set_llm_context(
        purpose="provider_probe",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
            asyncio.run(routed.chat_with_tools([], [{"type": "function"}], False))
    finally:
        llm_context.reset_llm_context(token)

    assert caught.value.code == "provider_safety_block"
    assert caught.value.tools_count == 1
    assert caught.value.wire_tools_count == 0


def test_routed_tool_caller_records_safe_request_shape() -> None:
    error = RuntimeError("private schema response")
    error.status_code = 400
    error.wire_tools_count = 0
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [error])],
        fallback_caller=None,
        logger=None,
    )
    messages = [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "safe_lookup",
                "description": "private description must not be copied",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
        asyncio.run(routed.chat_with_tools(messages, tools, True))

    attempt = caught.value.route_attempts[0]
    assert attempt["request_kind"] == "function_calling"
    assert attempt["message_count"] == 2
    assert attempt["prompt_chars"] == len("system texthello")
    assert attempt["tools_count"] == 1
    assert attempt["wire_tools_count"] == 0
    assert len(attempt["tool_names_hash"]) == 12
    assert len(attempt["tool_schema_hash"]) == 12
    assert attempt["builtin_search"] is True
    assert caught.value.tools_count == 1
    assert caught.value.wire_tools_count == 0
    assert caught.value.tool_names_hash == attempt["tool_names_hash"]
    assert "private description" not in str(attempt)


def test_runtime_builder_wraps_legacy_caller_when_provider_list_is_empty(monkeypatch) -> None:  # noqa: ANN001
    raw_error = RuntimeError("private raw provider error")
    legacy = _FakeCaller("legacy", [raw_error])
    monkeypatch.setattr(ai_routes, "_get_primary_provider_list", lambda *_args: [])
    monkeypatch.setattr(ai_routes, "resolve_global_fallback_provider", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_routes, "_build_tool_caller", lambda _config: legacy)
    monkeypatch.setattr(
        ai_routes,
        "get_primary_provider_config",
        lambda *_args: {"name": "legacy", "api_type": "openai", "model": "legacy-model"},
    )

    caller = ai_routes.build_routed_tool_caller(SimpleNamespace(), logger=None)

    assert isinstance(caller, ai_routes.RoutedToolCaller)
    with pytest.raises(ai_routes.RoutedToolCallerError) as caught:
        asyncio.run(caller.chat_with_tools([], [], False))
    assert caught.value.code == "provider_call_failed"
    assert "private raw provider error" not in str(caught.value.route_attempts)


def test_qzone_probe_uses_response_scoped_route_state() -> None:
    empty = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw={},
    )
    tool_response = tool_impl.ToolCallerResponse(
        finish_reason="tool_calls",
        content="",
        tool_calls=[SimpleNamespace(id="probe-call")],
        raw={},
    )
    primary = _FakeCaller("primary", [empty])
    fallback = _FakeCaller("fallback", [tool_response])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=fallback,
        logger=None,
    )
    token = llm_context.set_llm_context(
        purpose="qzone_provider_probe",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        response = asyncio.run(routed.chat_with_tools([], [{"type": "function"}], False))
    finally:
        llm_context.reset_llm_context(token)

    assert response.tool_calls[0].id == "probe-call"
    assert response.route_key == routed._caller_route_keys[id(fallback)]
    assert not hasattr(routed, "_default_result_caller")
    assert not hasattr(routed, "_tool_call_callers")
