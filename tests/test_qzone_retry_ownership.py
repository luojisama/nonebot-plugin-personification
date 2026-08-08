from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from plugin.personification.core import ai_routes, llm_context, provider_router
from plugin.personification.skills.skillpacks.tool_caller.scripts import impl as caller_impl


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None

    def error(self, *_args, **_kwargs) -> None:
        return None


class _HttpError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code, headers={})


def _provider(name: str) -> dict:
    return {
        "name": name,
        "api_type": "openai",
        "model": "test-model",
        "max_retries": 5,
    }


def _response(content: str = "ok", *, finish_reason: str = "stop") -> caller_impl.ToolCallerResponse:
    return caller_impl.ToolCallerResponse(finish_reason, content, [], {})


def test_qzone_policy_tries_each_provider_once_and_keeps_provider_fallback(monkeypatch) -> None:  # noqa: ANN001
    calls = {"primary": 0, "fallback": 0}

    async def _call(provider, *_args, **_kwargs):  # noqa: ANN001, ANN202
        calls[provider["name"]] += 1
        if provider["name"] == "primary":
            raise _HttpError(503)
        return _response()

    async def _unexpected_sleep(_delay: float) -> None:
        raise AssertionError("provider_router must not own QZone retry delays")

    monkeypatch.setattr(provider_router, "_call_provider_once", _call)
    monkeypatch.setattr(provider_router.asyncio, "sleep", _unexpected_sleep)
    provider_router.PROVIDER_FAILURE_STATE.clear()
    token = llm_context.set_llm_context(
        purpose="qzone_generation",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        response, errors, attempts, _ = asyncio.run(provider_router._try_provider_chain(
            [_provider("primary"), _provider("fallback")],
            messages=[],
            plugin_config=SimpleNamespace(),
            logger=_Logger(),
        ))
    finally:
        llm_context.reset_llm_context(token)

    assert response is not None and response.content == "ok"
    assert calls == {"primary": 1, "fallback": 1}
    assert len(errors) == 1
    assert len(attempts) == 1


class _FakeCaller:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat_with_tools(self, *_args, **_kwargs):  # noqa: ANN201
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def build_tool_result_message(self, *_args, **_kwargs) -> dict:
        return {}


def test_qzone_routed_caller_skips_same_caller_reframe_but_keeps_fallback() -> None:
    primary = _FakeCaller([
        _response("", finish_reason="content_filter"),
        AssertionError("same caller safety retry must be disabled"),
    ])
    fallback = _FakeCaller([_response("fallback result")])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=fallback,
        logger=_Logger(),
    )
    token = llm_context.set_llm_context(
        purpose="qzone_generation",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        response = asyncio.run(routed.chat_with_tools([], [], False))
    finally:
        llm_context.reset_llm_context(token)

    assert response.content == "fallback result"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_qzone_routed_caller_propagates_cancelled_error() -> None:
    primary = _FakeCaller([asyncio.CancelledError()])
    fallback = _FakeCaller([_response("must not run")])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=fallback,
        logger=_Logger(),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(routed.chat_with_tools([], [], False))
    assert fallback.calls == 0


def test_qzone_policy_disables_openai_sdk_retries_without_changing_normal_calls(monkeypatch) -> None:  # noqa: ANN001
    client_kwargs: list[dict] = []

    async def _create(**_kwargs):  # noqa: ANN202
        message = SimpleNamespace(content="ok", tool_calls=[], annotations=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    def _async_openai(**kwargs):  # noqa: ANN001, ANN202
        client_kwargs.append(dict(kwargs))
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
            responses=SimpleNamespace(create=_create),
        )

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _async_openai
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    async def _http_client(*_args, **_kwargs):  # noqa: ANN202
        return object()

    monkeypatch.setattr(caller_impl, "_get_pooled_http_client", _http_client)
    caller = caller_impl.OpenAIToolCaller(
        api_key="secret",
        base_url="https://example.test/v1",
        model="test-model",
    )
    asyncio.run(caller.chat_with_tools([{"role": "user", "content": "hi"}], [], False))

    token = llm_context.set_llm_context(
        purpose="qzone_generation",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        asyncio.run(caller.chat_with_tools([{"role": "user", "content": "hi"}], [], False))
    finally:
        llm_context.reset_llm_context(token)

    assert "max_retries" not in client_kwargs[0]
    assert client_kwargs[1]["max_retries"] == 0


def test_qzone_policy_disables_anthropic_sdk_retries_without_changing_normal_calls(monkeypatch) -> None:  # noqa: ANN001
    client_kwargs: list[dict] = []

    async def _create(**_kwargs):  # noqa: ANN202
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=None,
        )

    def _async_anthropic(**kwargs):  # noqa: ANN001, ANN202
        client_kwargs.append(dict(kwargs))
        return SimpleNamespace(messages=SimpleNamespace(create=_create))

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.AsyncAnthropic = _async_anthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    caller = caller_impl.AnthropicToolCaller(
        api_key="secret",
        base_url="https://example.test",
        model="test-model",
    )
    asyncio.run(caller.chat_with_tools([{"role": "user", "content": "hi"}], [], False))

    token = llm_context.set_llm_context(
        purpose="qzone_semantic_review",
        retry_policy=llm_context.LLM_RETRY_POLICY_SINGLE_ATTEMPT,
    )
    try:
        asyncio.run(caller.chat_with_tools([{"role": "user", "content": "hi"}], [], False))
    finally:
        llm_context.reset_llm_context(token)

    assert "max_retries" not in client_kwargs[0]
    assert client_kwargs[1]["max_retries"] == 0
