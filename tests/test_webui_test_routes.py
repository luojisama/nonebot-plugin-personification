from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ._loader import load_personification_module

# 复用 smoke 的 fixture + 登录
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401

test_routes = load_personification_module("plugin.personification.webui.routes.test_routes")
tool_registry_module = load_personification_module("plugin.personification.agent.tool_registry")


class _FakeResp:
    def __init__(self, content="ok", finish_reason="stop", raw=None, wire_tools_count=None):
        self.content = content
        self.finish_reason = finish_reason
        self.raw = raw
        self.tool_calls = []
        self.usage = {}
        self.model_used = "m"
        self.vision_unavailable = False
        self.wire_tools_count = wire_tools_count


class _FakeCaller:
    def __init__(self, content="hello"):
        self._content = content

    async def chat_with_tools(self, messages, tools, use_builtin_search):
        return _FakeResp(content=self._content)


class _RaisingCaller:
    def __init__(self, exc: BaseException):
        self._exc = exc

    async def chat_with_tools(self, messages, tools, use_builtin_search):
        raise self._exc


class _QzoneProbeCaller:
    def __init__(self):
        self.messages = []
        self.tools = []
        self.context = {}
        self.use_builtin_search = None

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        llm_context = load_personification_module("plugin.personification.core.llm_context")
        self.messages = list(messages)
        self.tools = list(tools)
        self.context = llm_context.current_llm_context()
        self.use_builtin_search = use_builtin_search
        return _FakeResp(
            content='{"action":"skip","reason":"probe_ok"}',
            wire_tools_count=len(tools),
        )


def _set_routed_caller(runtime_context, caller, registry=None) -> None:  # noqa: ANN001
    current = runtime_context.app_module.get_runtime_context()
    runtime_context.app_module.set_runtime_context(
        plugin_config=runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=current.get_bots,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(agent_tool_caller=caller, tool_registry=registry),
    )


async def _noop_tool(**_kwargs):  # noqa: ANN001
    return "ok"


def _tool_registry(*names: str):  # noqa: ANN201
    registry = tool_registry_module.ToolRegistry()
    for name in names:
        registry.register(
            tool_registry_module.AgentTool(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                handler=_noop_tool,
                metadata={"source_kind": "builtin"},
            )
        )
    return registry


def _patch_ai_routes(monkeypatch, providers, caller_content="hi"):
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda pc, lg: providers)
    monkeypatch.setattr(
        ai_routes, "build_single_provider_caller",
        lambda pc, prov, **kw: _FakeCaller(content=f"{prov.get('name')}:{caller_content}"),
    )


def test_chat_all_probes_every_provider(_runtime_context, monkeypatch) -> None:
    providers = [
        {"name": "main", "api_type": "openai", "model": "gpt-4o", "priority": 1},
        {"name": "backup", "api_type": "anthropic", "model": "claude", "priority": 2},
    ]
    _patch_ai_routes(monkeypatch, providers)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post("/personification/api/test/chat-all", json={"prompt": "hi"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    names = {r["name"] for r in body["results"]}
    assert names == {"main", "backup"}
    for r in body["results"]:
        assert r["ok"] is True
        assert r["duration_ms"] >= 0
        assert r["content"].endswith(":hi")
        assert r["diagnostic"]["code"] == "provider_test_complete"
    assert body["diagnostic"]["code"] == "provider_test_all_complete"


def test_chat_all_flags_blocked_provider(_runtime_context, monkeypatch) -> None:
    providers = [{"name": "g", "api_type": "gemini", "model": "gemini-2", "priority": 1}]
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda pc, lg: providers)

    class _BlockedCaller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            return _FakeResp(content="", raw={"candidates": [{"finishReason": "SAFETY"}]})

    monkeypatch.setattr(ai_routes, "build_single_provider_caller", lambda pc, prov, **kw: _BlockedCaller())
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post("/personification/api/test/chat-all", json={"prompt": "hi"})
    assert res.status_code == 200
    r = res.json()["results"][0]
    assert r["ok"] is False
    assert "SAFETY" in r["blocked_reason"]
    assert r["diagnostic"]["code"] == "provider_test_blocked"
    assert r["diagnostic"]["retryable"] is False


def test_chat_single_preserves_response_fields_and_adds_diagnostic(_runtime_context) -> None:
    _set_routed_caller(_runtime_context, _FakeCaller(content="single hello"))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post("/personification/api/test/chat", json={"prompt": "hi"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["content"] == "single hello"
    assert body["finish_reason"] == "stop"
    assert body["duration_ms"] >= 0
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert body["model_used"] == "m"
    assert body["diagnostic"]["code"] == "provider_test_complete"


def test_chat_single_qzone_profile_uses_production_compatible_shape(_runtime_context) -> None:
    caller = _QzoneProbeCaller()
    _set_routed_caller(
        _runtime_context,
        caller,
        _tool_registry("web_search", "weather", "send_qq_face"),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "检查空间草稿调用", "profile": "qzone"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["profile"] == "qzone"
    assert body["tools_count"] == 2
    assert body["tool_profile"] == "qzone_read_only"
    assert len(body["tool_names_hash"]) == 12
    assert len(body["tool_schema_hash"]) == 12
    assert body["wire_tools_count"] == 2
    assert body["probe_degraded"] is False
    assert body["diagnostic"]["code"] == "provider_test_qzone_compatible"
    details = {item["label"]: item["value"] for item in body["diagnostic"]["details"]}
    assert details["Tool profile"] == "qzone_read_only"
    assert details["Tool names hash"] == body["tool_names_hash"]
    assert details["Schema hash"] == body["tool_schema_hash"]
    assert body["runtime"]["build_id"]
    assert caller.context["purpose"] == "qzone_provider_probe"
    assert caller.context["retry_policy"] == "single_attempt"
    assert {tool["function"]["name"] for tool in caller.tools} == {"web_search", "weather"}
    assert caller.use_builtin_search is False
    assert "无副作用" in caller.messages[0]["content"]
    llm_context = load_personification_module("plugin.personification.core.llm_context")
    assert llm_context.current_llm_context() == {}


def test_chat_single_qzone_profile_uses_real_tool_free_shape_when_empty(_runtime_context) -> None:
    caller = _QzoneProbeCaller()
    _set_routed_caller(_runtime_context, caller)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "检查空间草稿调用", "profile": "qzone"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tools_count"] == 0
    assert body["wire_tools_count"] == 0
    assert body["tool_profile"] == "qzone_read_only"
    assert body["probe_degraded"] is True
    assert body["diagnostic"]["code"] == "provider_test_qzone_tool_free"
    degraded_details = {item["label"]: item["value"] for item in body["diagnostic"]["details"]}
    assert degraded_details["Tool profile"] == "qzone_read_only"
    assert caller.tools == []


def test_chat_single_qzone_tool_free_profile_rejects_unexpected_tool_call(_runtime_context) -> None:
    class _UnexpectedToolCaller(_QzoneProbeCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            response = await super().chat_with_tools(messages, tools, use_builtin_search)
            response.content = ""
            response.tool_calls = [SimpleNamespace(id="unexpected", name="web_search")]
            return response

    caller = _UnexpectedToolCaller()
    _set_routed_caller(_runtime_context, caller)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "检查空间草稿调用", "profile": "qzone"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["diagnostic"]["code"] == "provider_test_qzone_unexpected_tool_call"
    assert body["ok"] is False


def test_chat_single_qzone_profile_rejects_empty_wire_schema(_runtime_context) -> None:
    class _DroppedSchemaCaller(_QzoneProbeCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            response = await super().chat_with_tools(messages, tools, use_builtin_search)
            response.wire_tools_count = 0
            return response

    caller = _DroppedSchemaCaller()
    _set_routed_caller(_runtime_context, caller, _tool_registry("web_search"))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "检查空间草稿调用", "profile": "qzone"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["wire_tools_count"] == 0
    assert body["diagnostic"]["code"] == "provider_test_qzone_schema_empty"
    assert body["ok"] is False


def test_chat_single_qzone_profile_rejects_partial_wire_schema(_runtime_context) -> None:
    class _PartialSchemaCaller(_QzoneProbeCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            response = await super().chat_with_tools(messages, tools, use_builtin_search)
            response.wire_tools_count = 1
            return response

    caller = _PartialSchemaCaller()
    _set_routed_caller(_runtime_context, caller, _tool_registry("web_search", "weather"))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "检查空间草稿调用", "profile": "qzone"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["wire_tools_count"] == 1
    assert body["diagnostic"]["code"] == "provider_test_qzone_schema_partial"
    assert body["ok"] is False


def test_chat_single_qzone_profile_rejects_non_json_output(_runtime_context) -> None:
    _set_routed_caller(_runtime_context, _FakeCaller(content="ordinary text"))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "检查空间草稿调用", "profile": "qzone"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["diagnostic"]["code"] == "provider_test_qzone_output_invalid"


def test_chat_single_qzone_failure_exposes_safe_route_attempts(_runtime_context) -> None:
    exc = RuntimeError("https://private.example/?token=secret raw body")
    exc.code = "provider_model_candidate_unavailable"
    exc.status_code = 404
    exc.retryable = True
    exc.route_attempts = (
        {
            "provider": "antigravity",
            "api_type": "antigravity_cli",
            "model": "gemini-3.5-flash-low",
            "status_code": 404,
            "code": "provider_model_candidate_unavailable",
            "auth_mode": "bearer",
            "request_count": 2,
            "request_kind": "function_calling",
            "tools_count": 7,
            "tool_names_hash": "abc123def456",
            "builtin_search": False,
        },
    )
    _set_routed_caller(_runtime_context, _RaisingCaller(exc))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "检查空间草稿调用", "profile": "qzone"},
    )

    assert res.status_code == 502
    report = res.json()["detail"]
    assert report["code"] == "provider_test_model_unavailable"
    assert report["retryable"] is True
    details = {item["label"]: item["value"] for item in report["details"]}
    assert details["Provider route 1"].endswith(
        "HTTP 404 · provider_model_candidate_unavailable"
    )
    assert "auth=bearer" in details["Provider route 1"]
    assert "requests=2" in details["Provider route 1"]
    assert "kind=function_calling · tools=7 · schema=abc123def456 · builtin=false" in details["Provider route 1"]
    assert "private.example" not in res.text
    assert "raw body" not in res.text
    llm_context = load_personification_module("plugin.personification.core.llm_context")
    assert llm_context.current_llm_context() == {}


def test_provider_failure_prefers_aggregate_route_selection_over_last_cause() -> None:
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    candidate = ai_routes.RoutedToolCallerError([
        {
            "provider": "antigravity",
            "status_code": 404,
            "code": "provider_model_candidate_unavailable",
            "retryable": True,
        },
        {
            "provider": "backup",
            "status_code": 0,
            "code": "provider_call_failed",
            "retryable": True,
        },
    ])
    candidate.__cause__ = asyncio.TimeoutError("private timeout")
    request_rejected = ai_routes.RoutedToolCallerError([
        {
            "provider": "primary",
            "status_code": 422,
            "code": "provider_request_rejected",
            "retryable": False,
        },
        {
            "provider": "backup",
            "status_code": 0,
            "code": "provider_call_failed",
            "retryable": True,
        },
    ])
    request_rejected.__cause__ = ConnectionError("private network error")

    _, candidate_report = test_routes._provider_failure_report(candidate)
    _, request_report = test_routes._provider_failure_report(request_rejected)

    assert candidate_report["code"] == "provider_test_model_unavailable"
    assert request_report["code"] == "provider_test_request_rejected"

    mixed = ai_routes.RoutedToolCallerError([
        {
            "provider": "bad-model",
            "status_code": 400,
            "code": "provider_model_unavailable",
            "retryable": False,
        },
        {
            "provider": "schema-gateway",
            "status_code": 400,
            "code": "provider_request_rejected",
            "retryable": False,
        },
    ])
    mixed.__cause__ = RuntimeError("model is invalid")
    _, mixed_report = test_routes._provider_failure_report(mixed)
    assert mixed_report["code"] == "provider_test_request_rejected"


def test_provider_failure_classifies_structured_safety_block() -> None:
    blocked = RuntimeError("safe provider policy block")
    blocked.code = "provider_safety_block"

    status, report = test_routes._provider_failure_report(blocked)

    assert status == 502
    assert report["code"] == "provider_test_blocked"
    assert report["phase"] == "provider_policy"
    assert report["retryable"] is False


def test_provider_failure_recognizes_wrapped_model_error_text() -> None:
    inner = RuntimeError("model is invalid")
    outer = RuntimeError("safe outer rejection")
    outer.status_code = 400
    outer.__cause__ = inner

    status, report = test_routes._provider_failure_report(outer)

    assert status == 502
    assert report["code"] == "provider_test_model_unavailable"
    assert report["retryable"] is False


def test_chat_single_internal_failure_is_structured_and_redacted(_runtime_context) -> None:
    internal_error = RuntimeError("https://private.example/chat?api_key=top-secret raw body")
    internal_error.code = "opaque-secret-code"
    internal_error.route_attempts = ({"provider": "route", "code": "opaque-secret-code"},)
    _set_routed_caller(
        _runtime_context,
        _RaisingCaller(internal_error),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post("/personification/api/test/chat", json={"prompt": "hi"})

    assert res.status_code == 500, res.text
    report = res.json()["detail"]
    assert report["code"] == "provider_test_internal_error"
    assert report["trace_id"]
    serialized = json.dumps(report)
    assert "private.example" not in serialized
    assert "top-secret" not in serialized
    assert "opaque-secret-code" not in serialized
    assert "raw body" not in serialized


def _provider_test_exception(kind: str) -> BaseException:
    request = httpx.Request("POST", "https://private.example/chat?api_key=top-secret")
    if kind == "auth":
        response = httpx.Response(401, request=request, text="token=top-secret raw body")
        return httpx.HTTPStatusError("Authorization: Bearer top-secret", request=request, response=response)
    if kind == "permission":
        response = httpx.Response(403, request=request, text="token=top-secret raw body")
        return httpx.HTTPStatusError("Forbidden token=top-secret", request=request, response=response)
    if kind == "timeout":
        return asyncio.TimeoutError("token=top-secret raw body")
    if kind == "network":
        return httpx.ConnectError("https://private.example token=top-secret", request=request)
    if kind == "parse":
        return json.JSONDecodeError("token=top-secret", "raw body top-secret", 0)
    return RuntimeError("https://private.example/?token=top-secret raw body")


@pytest.mark.parametrize(
    ("kind", "expected_code", "retryable"),
    [
        ("auth", "provider_test_auth_failed", False),
        ("permission", "provider_test_permission_denied", False),
        ("timeout", "provider_test_timeout", True),
        ("network", "provider_test_network_failed", True),
        ("parse", "provider_test_parse_failed", False),
        ("internal", "provider_test_internal_error", True),
    ],
)
def test_chat_all_provider_failures_are_classified_without_raw_output(
    _runtime_context,
    monkeypatch,
    kind: str,
    expected_code: str,
    retryable: bool,
) -> None:
    provider = {
        "name": "main",
        "api_type": "openai",
        "model": "gpt-test",
        "priority": 1,
        "api_key": "provider-secret",
    }
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda pc, lg: [provider])
    monkeypatch.setattr(
        ai_routes,
        "build_single_provider_caller",
        lambda pc, prov, **kw: _RaisingCaller(_provider_test_exception(kind)),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post("/personification/api/test/chat-all", json={"prompt": "hi"})

    assert res.status_code == 200, res.text
    body = res.json()
    item = body["results"][0]
    assert item["ok"] is False
    assert item["diagnostic"]["code"] == expected_code
    assert item["diagnostic"]["retryable"] is retryable
    assert body["diagnostic"]["code"] == "provider_test_all_failed"
    serialized = json.dumps(body)
    assert "private.example" not in serialized
    assert "top-secret" not in serialized
    assert "raw body" not in serialized
    assert "provider-secret" not in serialized


def test_chat_all_reports_empty_model_output(_runtime_context, monkeypatch) -> None:
    providers = [{"name": "empty", "api_type": "openai", "model": "gpt-test", "priority": 1}]
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda pc, lg: providers)
    monkeypatch.setattr(ai_routes, "build_single_provider_caller", lambda pc, prov, **kw: _FakeCaller(content=""))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.post("/personification/api/test/chat-all", json={"prompt": "hi"})

    assert res.status_code == 200, res.text
    item = res.json()["results"][0]
    assert item["ok"] is False
    assert item["content"] == ""
    assert item["diagnostic"]["code"] == "provider_test_model_empty"


def test_chat_all_requires_prompt(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post("/personification/api/test/chat-all", json={"prompt": ""})
    assert res.status_code == 400


def test_config_provider_models_probes_openai_compatible(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"data": [{"id": "gpt-test"}, {"id": "gpt-test-mini"}]}

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def get(self, url, headers=None, params=None):  # noqa: ANN001, ANN201
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _Resp()

    monkeypatch.setattr(config_routes.httpx, "AsyncClient", _Client)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/config/provider-models",
        json={
            "provider": {
                "api_type": "openai",
                "api_url": "https://example.test/v1",
                "api_key": "sk-test",
                "model": "gpt-current-alias",
            }
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert captured["url"] == "https://example.test/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert [m["id"] for m in body["models"]] == ["gpt-current-alias", "gpt-test", "gpt-test-mini"]


def test_config_provider_models_probes_gemini_openai_compatible(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"data": [{"id": "gemini-2.5-flash"}, {"id": "gemini-2.5-pro"}]}

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def get(self, url, headers=None, params=None):  # noqa: ANN001, ANN201
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _Resp()

    monkeypatch.setattr(config_routes.httpx, "AsyncClient", _Client)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/config/provider-models",
        json={
            "provider": {
                "api_type": "gemini",
                "api_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key": "gk-test",
            }
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert captured["headers"]["Authorization"] == "Bearer gk-test"
    assert captured["params"] == {}
    assert [m["id"] for m in body["models"]] == ["gemini-2.5-flash", "gemini-2.5-pro"]


def test_config_provider_models_probes_native_gemini_with_google_header(
    _runtime_context, monkeypatch  # noqa: ANN001
) -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {
                "models": [
                    {
                        "name": "models/gemini-test",
                        "displayName": "Gemini Test",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            }

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def get(self, url, headers=None, params=None):  # noqa: ANN001, ANN201
            captured.update(url=url, headers=headers or {}, params=params or {})
            return _Resp()

    monkeypatch.setattr(config_routes.httpx, "AsyncClient", _Client)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/config/provider-models",
        json={
            "provider": {
                "api_type": "gemini",
                "api_url": "https://gemini-gateway.example",
                "api_key": "gemini-secret",
                "gemini_auth_mode": "auto",
            }
        },
    )

    assert res.status_code == 200, res.text
    assert captured["url"] == "https://gemini-gateway.example/v1beta/models"
    assert captured["headers"] == {"x-goog-api-key": "gemini-secret"}
    assert captured["params"] == {}
    assert captured["client_kwargs"]["follow_redirects"] is False


def test_config_provider_models_cli_routes_return_selectable_candidates(_runtime_context) -> None:  # noqa: ANN001
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    cases = {
        "gemini_cli": "gemini-2.5-flash",
        "antigravity_cli": "gemini-3.5-flash-low",
        "claude_code": "claude-opus-4-7",
        "openai_codex": "gpt-5.3-codex",
    }
    for api_type, expected in cases.items():
        res = client.post(
            "/personification/api/config/provider-models",
            json={"provider": {"api_type": api_type, "model": ""}},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["source"] == "local_cache"
        assert expected in {item["id"] for item in body["models"]}


def test_config_provider_models_endpoint_normalizes_version_paths() -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")

    anthropic_url, anthropic_headers, _, anthropic_parser = config_routes._models_endpoint(  # noqa: SLF001
        {"api_type": "anthropic", "api_url": "https://api.anthropic.com/v1", "api_key": "ak-test"}
    )
    assert anthropic_url == "https://api.anthropic.com/v1/models"
    assert anthropic_headers["x-api-key"] == "ak-test"
    assert anthropic_headers["anthropic-version"]
    assert anthropic_parser == "anthropic"

    gemini_url, _, gemini_params, gemini_parser = config_routes._models_endpoint(  # noqa: SLF001
        {
            "api_type": "gemini",
            "api_url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent",
            "api_key": "gk-test",
        }
    )
    assert gemini_url == "https://generativelanguage.googleapis.com/v1beta/models"
    assert gemini_params == {}
    assert gemini_parser == "gemini"

    gemini_openai_url, gemini_openai_headers, gemini_openai_params, gemini_openai_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {
                "api_type": "gemini",
                "api_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "api_key": "gk-openai",
            }
        )
    )
    assert gemini_openai_url == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert gemini_openai_headers["Authorization"] == "Bearer gk-openai"
    assert gemini_openai_params == {}
    assert gemini_openai_parser == "gemini_openai"

    openai_gemini_url, openai_gemini_headers, openai_gemini_params, openai_gemini_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {
                "api_type": "openai",
                "api_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "gk-openai",
            }
        )
    )
    assert openai_gemini_url == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert openai_gemini_headers["Authorization"] == "Bearer gk-openai"
    assert openai_gemini_params == {}
    assert openai_gemini_parser == "openai"

    zellon_openai_url, zellon_openai_headers, zellon_openai_params, zellon_openai_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {"api_type": "openai", "api_url": "https://anti.zellon.me", "api_key": "sk-zellon"}
        )
    )
    assert zellon_openai_url == "https://anti.zellon.me/v1/models"
    assert zellon_openai_headers["Authorization"] == "Bearer sk-zellon"
    assert zellon_openai_params == {}
    assert zellon_openai_parser == "openai"

    zellon_gemini_url, zellon_gemini_headers, zellon_gemini_params, zellon_gemini_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {"api_type": "gemini", "api_url": "https://anti.zellon.me", "api_key": "sk-zellon"}
        )
    )
    assert zellon_gemini_url == "https://anti.zellon.me/v1beta/models"
    assert zellon_gemini_headers == {}
    assert zellon_gemini_params == {}
    assert zellon_gemini_parser == "gemini"


def test_persona_prompt_inline_system_prompt(_runtime_context) -> None:
    _runtime_context.plugin_config.personification_system_prompt = "你是一个活泼的群友" * 10
    _runtime_context.plugin_config.personification_prompt_path = ""
    _runtime_context.plugin_config.personification_system_path = ""
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/test/persona-prompt")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "活泼的群友" in body["content"]
    assert body["exists"] is True
    assert body["is_file"] is False
    assert body["diagnostic"]["code"] == "persona_prompt_inline_loaded"


def test_persona_prompt_reads_specified_path(_runtime_context, tmp_path) -> None:
    f = tmp_path / "persona.txt"
    f.write_text("自定义人设内容", encoding="utf-8")
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/test/persona-prompt", params={"path": str(f)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_file"] is True
    assert body["content"] == "自定义人设内容"
    assert body["diagnostic"]["code"] == "persona_prompt_file_loaded"


def test_persona_prompt_missing_path_reports_not_exists(_runtime_context, tmp_path) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/test/persona-prompt", params={"path": str(tmp_path / "nope.txt")})
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is False
    assert body["diagnostic"]["code"] == "persona_prompt_path_not_found"
    assert body["diagnostic"]["ok"] is False


def test_persona_prompt_read_failure_is_structured_and_redacted(_runtime_context, monkeypatch) -> None:
    prompt_loader = load_personification_module("plugin.personification.core.prompt_loader")

    class _UnreadablePrompt:
        def is_file(self):
            return True

        def stat(self):
            return SimpleNamespace(st_size=32)

        def read_text(self, **_kwargs):
            raise PermissionError("C:/private/persona.txt?token=top-secret")

    monkeypatch.setattr(prompt_loader, "_resolve_candidate_path", lambda _path: _UnreadablePrompt())
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    res = client.get("/personification/api/test/persona-prompt", params={"path": "persona.txt"})

    assert res.status_code == 500, res.text
    report = res.json()["detail"]
    assert report["code"] == "persona_prompt_read_failed"
    assert report["trace_id"]
    serialized = json.dumps(report)
    assert "private/persona" not in serialized
    assert "top-secret" not in serialized


def test_test_frontend_persists_and_renders_operation_diagnostics() -> None:
    source = (Path(__file__).resolve().parents[1] / "webui" / "static" / "app-tools.js").read_text(encoding="utf-8")

    assert "persistTestOperationResult(state.testResult)" in source
    assert "persistTestOperationResult(state.testAllResult)" in source
    assert "sessionStorage.setItem(_TEST_OPERATION_RESULT_STORAGE_KEY" in source
    assert "testDiagnosticSnapshot" in source
    assert "renderOperationDiagnostic(result.diagnostic)" in source
    assert "renderOperationDiagnostic(p.diagnostic)" in source
    assert 'operationDiagnosticFromError(e, "路由模型测试未完成")' in source
    assert 'operationDiagnosticFromError(e, "全部 Provider 测试未完成")' in source
    assert 'operationDiagnosticFromError(e, "人设 prompt 读取未完成")' in source
    assert "不保存模型正文" in source


def test_persona_template_builder_uses_main_model(_runtime_context, monkeypatch) -> None:
    persona_template_routes = load_personification_module("plugin.personification.webui.routes.persona_template_routes")

    async def _fake_sources(*, runtime, work_title, character_name, search_aliases=None):
        return [
            {
                "kind": "wiki",
                "query": f"{work_title} {character_name}",
                "source": "萌娘百科",
                "title": character_name,
                "url": "https://example.test/character",
                "summary": "角色资料摘要",
                "confidence": 0.9,
            }
        ]

    monkeypatch.setattr(persona_template_routes, "_gather_persona_sources", _fake_sources)
    async def _fake_avatar_search(**_kwargs):
        return []

    monkeypatch.setattr(persona_template_routes, "_search_avatar_image_sources", _fake_avatar_search)

    class _MainCaller:
        def __init__(self):
            self.calls = []
            self.contexts = []

        async def __call__(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            llm_context = load_personification_module("plugin.personification.core.llm_context")
            self.contexts.append(dict(llm_context.current_llm_context()))
            user_text = str(messages[-1]["content"])
            if "生成插件内可直接使用的人设 YAML" in user_text:
                return """
name: 测试角色
tts:
  voice: default_zh
  style: 平静 自然
  user_hint: 用自然语气朗读。
status: |
  心情: "平静"
  状态: "测试中"
  记忆: ""
  动作: "看群消息"
nick_name:
  - 测试角色
ack_phrases:
  - 我看看
initial_message: "我是测试角色"
mute_keyword:
  - 闭嘴
input: |
  # 当前时间
  {time}
  # 触发原因
  {trigger_reason}
  {schedule_instruction}
  # 对话历史
  {history_new}
  # 当前消息
  {history_last}
  # 当前状态
  {status}
  <output>
  <message>消息正文</message>
  </output>
system: |
  你是测试角色，不是 AI 助手。
  ## 资料冲突与缺口
  - 无
""".strip()
            if "输出 JSON" in user_text:
                return '{"facts":["事实 S1"],"conflicts":[],"unknowns":[]}'
            return "ok"

    caller = _MainCaller()
    old_get_bots = _runtime_context.app_module.get_runtime_context().get_bots
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=old_get_bots,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(call_ai_api=caller),
    )

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/persona-template/build",
        json={"work_title": "测试作品", "character_name": "测试角色"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["model_role"] == "configured_main"
    assert body["sources"][0]["source"] == "萌娘百科"
    assert len(body["subagents"]) == 3
    assert body["template_valid"] is True
    assert "system:" in body["template"]
    assert "input" in body["template_keys"]
    assert body["history_record"]["work_title"] == "测试作品"
    assert Path(body["export_path"]).is_file()
    history = client.get("/personification/api/persona-template/history?limit=5")
    assert history.status_code == 200, history.text
    assert history.json()["records"][0]["record_id"] == body["history_record"]["record_id"]
    detail = client.get(
        "/personification/api/persona-template/history/" + body["history_record"]["record_id"]
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["result"]["template"] == body["template"]
    apply_path = Path(_runtime_context.plugin_config.personification_data_dir) / "active_persona.yaml"
    _runtime_context.plugin_config.personification_prompt_path = str(apply_path)
    applied = client.post(
        "/personification/api/persona-template/apply",
        json={"record_id": body["history_record"]["record_id"]},
    )
    assert applied.status_code == 200, applied.text
    assert Path(applied.json()["path"]).is_file()
    assert "system:" in apply_path.read_text(encoding="utf-8")
    edited_template = body["template"].replace('initial_message: "我是测试角色"', 'initial_message: "编辑后的人设"')
    edited = client.put(
        "/personification/api/persona-template/history/" + body["history_record"]["record_id"],
        json={"template": edited_template},
    )
    assert edited.status_code == 200, edited.text
    assert "编辑后的人设" in edited.json()["result"]["template"]
    invalid_edit = client.put(
        "/personification/api/persona-template/history/" + body["history_record"]["record_id"],
        json={"template": "name: [invalid"},
    )
    assert invalid_edit.status_code == 400
    deleted = client.delete(
        "/personification/api/persona-template/history/" + body["history_record"]["record_id"]
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get(
        "/personification/api/persona-template/history/" + body["history_record"]["record_id"]
    ).status_code == 404
    assert len(caller.calls) == 6
    assert caller.calls[0]["kwargs"].get("use_builtin_search") is False
    assert all(call["kwargs"].get("use_builtin_search") is True for call in caller.calls[1:4])
    purposes = [ctx.get("purpose") for ctx in caller.contexts]
    assert purposes == [
        "persona_template_alias_planning",
        "persona_template_research",
        "persona_template_research",
        "persona_template_research",
        "persona_template_signature_candidates",
        "persona_template_synthesis",
    ]


def test_persona_template_builder_supports_custom_description(_runtime_context) -> None:
    class _MainCaller:
        def __init__(self):
            self.calls = []
            self.contexts = []

        async def __call__(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            llm_context = load_personification_module("plugin.personification.core.llm_context")
            self.contexts.append(dict(llm_context.current_llm_context()))
            user_text = str(messages[-1]["content"])
            if "原创人设描述" in user_text or "用户描述资料" in user_text:
                return """
name: 星野露
tts:
  voice: default_zh
  style: 平静 自然
  user_hint: 用自然语气朗读。
status: |
  心情: "平静"
  状态: "观察群聊"
  记忆: ""
  动作: "看群消息"
nick_name:
  - 星野露
ack_phrases:
  - 我看看
initial_message: "我是星野露"
mute_keyword:
  - 闭嘴
input: |
  # 当前时间
  {time}
  # 触发原因
  {trigger_reason}
  {schedule_instruction}
  # 对话历史
  {history_new}
  # 当前消息
  {history_last}
  # 当前状态
  {status}
  <output>
  <message>消息正文</message>
  </output>
system: |
  你是星野露，不是 AI 助手。
  ## 角色身份与不可替换锚点
  - 自定义人设。
  ## 资料冲突与缺口
  - 无
""".strip()
            return '{"facts":["用户描述事实"],"conflicts":[],"unknowns":[]}'

    caller = _MainCaller()
    old_get_bots = _runtime_context.app_module.get_runtime_context().get_bots
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=old_get_bots,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(call_ai_api=caller),
    )

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/persona-template/build",
        json={
            "mode": "custom",
            "persona_name": "星野露",
            "gender": "女",
            "personality": "温柔但会吐槽",
            "traits": "喜欢在群里用外号称呼熟人",
            "hobbies": "观星、游戏",
            "description": "一个夜猫子原创角色，说话轻，熟了之后会自然插话。",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "custom"
    assert body["work_title"] == "自定义人设"
    assert body["character_name"] == "星野露"
    assert body["sources"][0]["kind"] == "custom_description"
    assert body["template_valid"] is True
    assert len(body["subagents"]) == 3
    assert all(call["kwargs"].get("use_builtin_search") is False for call in caller.calls)
    purposes = [ctx.get("purpose") for ctx in caller.contexts]
    assert purposes == [
        "persona_template_custom_research",
        "persona_template_custom_research",
        "persona_template_custom_research",
        "persona_template_custom_synthesis",
    ]


def test_persona_template_builder_has_no_sample_specific_branches() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    source = module.Path(module.__file__).resolve()
    text = source.read_text(encoding="utf-8")
    forbidden_literals = [
        "绪山真寻",
        "西木野真姬",
        "早濑优香",
        "ONIMAI 官方",
        "LLWiki",
        "KivoWiki",
        "onimai.jp/character/mahiro",
    ]
    for literal in forbidden_literals:
        assert literal not in text


def test_persona_template_search_queries_use_generic_aliases() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    aliases = module._normalize_search_aliases(
        {
            "work_aliases": ["Example Work"],
            "character_aliases": ["Example Hero"],
            "queries": ["Example Work Example Hero character profile"],
        },
        work_title="测试作品",
        character_name="测试太郎",
    )
    queries = module._persona_search_queries("测试作品", "测试太郎", aliases)
    site_queries = module._persona_site_search_queries("测试作品", "测试太郎", aliases)

    assert "测试 太郎" in aliases["character_aliases"]
    assert any("Example Work" in query and "Example Hero" in query for query in queries)
    assert any("测试 太郎" in query for query in queries)
    assert any(query.startswith("site:") and "Example Hero" in query for query in site_queries)


def test_avatar_search_uses_planned_aliases_without_real_network() -> None:
    module = load_personification_module("plugin.personification.webui.routes.persona_template_routes")
    calls = []

    async def fake_searcher(query, **_kwargs):
        calls.append(query)
        return json.dumps({"results": [{
            "source": "mock",
            "title": query,
            "image_url": f"https://images.example.test/{len(calls)}.png",
            "page_url": "https://example.test/character",
        }]})

    aliases = {
        "work_aliases": ["测试作品", "Test Work"],
        "character_aliases": ["测试太郎", "Test Taro"],
    }
    results = asyncio.run(module._search_avatar_image_sources(
        work_title="测试作品",
        character_name="测试太郎",
        logger=None,
        search_aliases=aliases,
        searcher=fake_searcher,
    ))

    assert results
    assert any("Test Work" in query and "Test Taro" in query for query in calls)
    assert all(item["query"] in calls for item in results)


def test_avatar_search_rejects_web_fallback_rows_without_image_urls() -> None:
    module = load_personification_module("plugin.personification.webui.routes.persona_template_routes")
    diagnostics = {}

    async def fake_searcher(_query, **_kwargs):
        return json.dumps({
            "source_type": "web",
            "results": [{"title": "角色页面", "url": "https://example.test/character"}],
        })

    results = asyncio.run(module._search_avatar_image_sources(
        work_title="测试作品",
        character_name="测试角色",
        logger=None,
        searcher=fake_searcher,
        diagnostics=diagnostics,
    ))

    assert results == []
    assert diagnostics["direct_image_count"] == 0
    assert diagnostics["web_fallback_row_count"] > 0


def test_avatar_search_writes_aggregate_warning_without_urls() -> None:
    module = load_personification_module("plugin.personification.webui.routes.persona_template_routes")
    diagnostics = {}
    messages = []
    logger = SimpleNamespace(info=messages.append, warning=messages.append)

    async def fake_searcher(_query, **_kwargs):
        return json.dumps({
            "source_type": "web",
            "results": [],
            "image_search_diagnostics": {
                "http_status": 200,
                "content_type": "text/html",
                "response_bytes": 1200,
                "raw_item_count": 0,
                "direct_image_count": 0,
                "error_type": "",
            },
        })

    results = asyncio.run(module._search_avatar_image_sources(
        work_title="测试作品",
        character_name="测试角色",
        logger=logger,
        searcher=fake_searcher,
        diagnostics=diagnostics,
    ))

    assert results == []
    assert diagnostics["empty_parse_count"] == diagnostics["query_count"]
    assert diagnostics["http_status_counts"] == {"200": diagnostics["query_count"]}
    assert diagnostics["content_type_counts"] == {"text/html": diagnostics["query_count"]}
    assert diagnostics["response_bytes"] > 0
    assert len(messages) == 1
    assert "空解析=" in messages[0]
    assert "example.test" not in messages[0]


def test_persona_template_source_relevance_rejects_weak_character_mentions() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    aliases = module._normalize_search_aliases(
        None,
        work_title="测试作品",
        character_name="测试太郎",
    )

    assert module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试太郎 - 萌娘百科",
        summary="测试太郎是《测试作品》的登场角色。",
        search_aliases=aliases,
    )
    assert module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试作品人物列表",
        summary="这里介绍测试作品角色，包括测试太郎。",
        search_aliases=aliases,
    )
    assert not module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试声优",
        summary="曾在测试作品中为测试太郎配音。",
        search_aliases=aliases,
    )
    assert not module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试太郎的母亲",
        summary="测试太郎的母亲是《测试作品》的登场角色。",
        search_aliases=aliases,
    )


def test_persona_template_validation_reports_quality_warnings() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    template = """
name: 测试角色
tts:
  voice: default_zh
status: |
  心情: "平静"
nick_name:
  - 测试角色
ack_phrases:
  - 我看看
initial_message: "我是测试角色"
mute_keyword:
  - 闭嘴
input: |
  {time}
  {trigger_reason}
  {schedule_instruction}
  {history_new}
  {history_last}
  {status}
  <output>
  <message>消息正文</message>
  </output>
system: |
  你是测试角色。作为助手回复用户问题。
  ## 资料冲突与缺口
  - 待确认
""".strip()

    validation = module._validate_template_yaml(template)

    assert validation["valid"] is True
    joined = "\n".join(validation["warnings"])
    assert "system 偏短" in joined
    assert "助手/客服式身份" in joined
    assert "ack_phrases" in joined


def test_persona_profile_apply_blocks_cross_record_and_returns_partial(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    history_mod = load_personification_module("plugin.personification.core.persona_template_history")
    route_mod = load_personification_module("plugin.personification.webui.routes.persona_template_routes")
    revision_a = "a" * 32
    revision_b = "b" * 32
    avatar_id = "1" * 32
    signature_id = "2" * 32
    unverified_avatar_id = "3" * 32
    data_dir = Path(_runtime_context.plugin_config.personification_data_dir)
    image_dir = data_dir / "persona_avatar_candidates" / revision_a
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / f"{avatar_id}.jpg").write_bytes(b"sanitized-image")
    (image_dir / f"{unverified_avatar_id}.jpg").write_bytes(b"safe-but-unverified")
    monkeypatch.setattr(route_mod, "candidate_file", lambda candidate, **_kwargs: image_dir / f"{candidate['candidate_id']}.jpg")
    record_a = history_mod.record_persona_template_result(
        {
            "work_title": "作品 A",
            "character_name": "角色 A",
            "revision": revision_a,
            "avatar_candidates": [{
                "candidate_id": avatar_id,
                "revision": revision_a,
                "suffix": ".jpg",
                "mime": "image/jpeg",
                "safety_status": "pass",
                "vision_status": "verified",
            }, {
                "candidate_id": unverified_avatar_id,
                "revision": revision_a,
                "suffix": ".jpg",
                "mime": "image/jpeg",
                "safety_status": "pass",
                "vision_status": "unavailable",
            }],
            "signature_candidates": [],
        }
    )
    history_mod.record_persona_template_result(
        {
            "work_title": "作品 B",
            "character_name": "角色 B",
            "revision": revision_b,
            "avatar_candidates": [],
            "signature_candidates": [{
                "candidate_id": signature_id,
                "revision": revision_b,
                "text": "另一个记录的签名",
                "safety_status": "pass",
            }],
        }
    )

    class _Bot:
        def __init__(self):
            self.calls = []

        async def call_api(self, api, **kwargs):
            self.calls.append((api, kwargs))
            if api == "send_private_msg":
                _runtime_context.sent.append(kwargs)

        async def send_private_msg(self, **kwargs):
            _runtime_context.sent.append(kwargs)

    bot = _Bot()
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=lambda: {"bot": bot},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(call_ai_api=lambda *_a, **_k: None),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    bot.calls.clear()
    rejected = client.post(
        "/personification/api/persona-template/profile-apply",
        json={
            "bot_id": "bot",
            "record_id": record_a["record_id"],
            "revision": revision_a,
            "avatar_candidate_id": unverified_avatar_id,
            "confirm_avatar": True,
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "failed"
    assert rejected.json()["code"] == "persona_profile_assets_failed"
    assert rejected.json()["steps"][0]["status"] == "error"
    assert bot.calls == []
    response = client.post(
        "/personification/api/persona-template/profile-apply",
        json={
            "bot_id": "bot",
            "record_id": record_a["record_id"],
            "revision": revision_a,
            "avatar_candidate_id": avatar_id,
            "signature_candidate_id": signature_id,
            "confirm_avatar": True,
            "confirm_signature": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "partial", body
    assert body["results"]["avatar"]["status"] == "applied"
    assert body["results"]["signature"]["status"] == "failed"
    assert body["code"] == "persona_profile_assets_partial"
    assert body["partial"] is True
    assert body["outcome_unknown"] is False
    assert [item["status"] for item in body["steps"]] == ["ok", "error"]
    assert "error" not in body["results"]["signature"]
    assert [call[0] for call in bot.calls] == ["set_qq_avatar"]
    restored = history_mod.get_persona_template_record(record_a["record_id"])
    assert restored["result"]["avatar_candidates"][0]["candidate_id"] == avatar_id
    assert restored["profile_apply_audit"][-1]["status"] == "partial"
