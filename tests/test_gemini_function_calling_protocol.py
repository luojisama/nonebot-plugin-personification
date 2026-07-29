from __future__ import annotations

import asyncio
import json as json_module

from plugin.personification.skills.skillpacks.tool_caller.scripts import impl


def _tool_schema(name: str = "get_ai_news") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "lookup",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_gemini_round_trip_preserves_native_content_signature_and_call_id(monkeypatch) -> None:  # noqa: ANN001
    captured: list[dict] = []
    native_content = {
        "role": "model",
        "parts": [
            {"thought": True, "text": "opaque thought", "thoughtSignature": "turn-signature"},
            {
                "functionCall": {"id": "provider-call-1", "name": "get_ai_news", "args": {}},
                "thoughtSignature": "function-signature==",
            },
        ],
    }
    responses = [
        {"candidates": [{"content": native_content}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "final"}]}}]},
    ]

    class _Response:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, _url, headers=None, params=None, json=None):  # noqa: ANN001, ANN201
            del headers, params
            captured.append(json or {})
            return _Response(responses.pop(0))

    monkeypatch.setattr(impl.httpx, "AsyncClient", _Client)
    caller = impl.GeminiToolCaller(
        api_key="secret",
        base_url="https://anti.zellon.me/v1beta",
        model="gemini-3-flash-agent",
    )
    messages = [{"role": "user", "content": "最近有什么 AI 新闻"}]
    first = asyncio.run(caller.chat_with_tools(messages, [_tool_schema()], False))

    assert first.tool_calls[0].id == "provider-call-1"
    assert first.tool_calls[0].provider_call_id == "provider-call-1"
    messages.append(caller.build_assistant_tool_calls_message(first))
    messages.extend(caller.build_tool_result_messages(first, [(first.tool_calls[0], "no results")]))
    second = asyncio.run(caller.chat_with_tools(messages, [_tool_schema()], False))

    assert second.content == "final"
    second_payload = captured[1]
    assert second_payload["contents"][1] == native_content
    assert second_payload["contents"][2] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "id": "provider-call-1",
                    "name": "get_ai_news",
                    "response": {"result": "no results"},
                }
            }
        ],
    }
    assert second_payload["tools"][0].get("functionDeclarations")
    assert "function_declarations" not in second_payload["tools"][0]


def test_gemini_runtime_id_is_unique_and_not_sent_as_provider_id() -> None:
    calls = impl._extract_gemini_tool_calls(
        [
            {"functionCall": {"name": "first", "args": {}}},
            {"functionCall": {"name": "second", "args": {}}},
        ]
    )
    caller = impl.GeminiToolCaller(api_key="secret", base_url="", model="gemini-test")
    messages = caller.build_tool_result_messages(
        impl.ToolCallerResponse("tool_calls", "", calls, {}),
        [(calls[0], "one"), (calls[1], "two")],
    )

    assert calls[0].id != calls[1].id
    assert all(call.provider_call_id == "" for call in calls)
    assert len(messages) == 1
    parts = messages[0]["parts"]
    assert [part["functionResponse"]["name"] for part in parts] == ["first", "second"]
    assert all("id" not in part["functionResponse"] for part in parts)


def test_gemini_converter_uses_canonical_part_names() -> None:
    _system, contents = impl._convert_messages_to_gemini(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "id": "call-1",
                            "name": "lookup",
                            "response": {"result": "ok"},
                        }
                    }
                ],
            },
        ]
    )

    assert "functionCall" in contents[0]["parts"][1]
    assert "functionResponse" in contents[1]["parts"][0]


def test_gemini_cli_continuation_stays_on_originating_concrete_model(monkeypatch) -> None:  # noqa: ANN001
    caller = impl.GeminiCliToolCaller(
        model="auto-gemini-3",
        project="project",
        thinking_mode="none",
    )
    model_a, model_b = impl._gemini_cli_model_candidates("auto-gemini-3")[:2]
    requested_models: list[str] = []
    model_b_calls = 0

    class _Client:
        async def post(self, url, *, json, headers):  # noqa: ANN001, ANN202
            nonlocal model_b_calls
            del headers
            model = json["model"]
            requested_models.append(model)
            request = impl.httpx.Request("POST", url)
            if model == model_a:
                return impl.httpx.Response(404, request=request, json={"error": "not found"})
            model_b_calls += 1
            if model_b_calls == 1:
                content = {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"id": "call-1", "name": "get_ai_news", "args": {}},
                            "thoughtSignature": "opaque-signature",
                        }
                    ],
                }
            else:
                content = {"role": "model", "parts": [{"text": "final"}]}
            return impl.httpx.Response(
                200,
                request=request,
                json={"response": {"candidates": [{"content": content}]}},
            )

    async def _get_access_token(*, force_refresh=False):  # noqa: ANN001
        del force_refresh
        return "token", None

    async def _resolve_project(_token, _auth_file=None):  # noqa: ANN001
        return "project"

    async def _get_client(*_args, **_kwargs):  # noqa: ANN001, ANN202
        return _Client()

    monkeypatch.setattr(caller, "_get_access_token", _get_access_token)
    monkeypatch.setattr(caller, "_resolve_project", _resolve_project)
    monkeypatch.setattr(impl, "_get_pooled_http_client", _get_client)

    messages = [{"role": "user", "content": "news"}]
    first = asyncio.run(caller.chat_with_tools(messages, [_tool_schema()], False))
    assert first.model_used == model_b
    messages.append(caller.build_assistant_tool_calls_message(first))
    messages.extend(caller.build_tool_result_messages(first, [(first.tool_calls[0], "no results")]))
    second = asyncio.run(caller.chat_with_tools(messages, [_tool_schema()], False))

    assert second.content == "final"
    assert requested_models == [model_a, model_b, model_b]


def test_antigravity_continuation_ignores_shared_preferred_model(monkeypatch) -> None:  # noqa: ANN001
    caller = impl.AntigravityCliToolCaller(
        model="auto-gemini-3",
        project="project",
        thinking_mode="none",
    )
    model_a, model_b = impl._antigravity_cli_model_candidates("auto-gemini-3")[:2]
    requested_models: list[str] = []
    model_b_calls = 0

    class _Client:
        async def post(self, url, *, json, headers):  # noqa: ANN001, ANN202
            nonlocal model_b_calls
            del headers
            model = json["model"]
            requested_models.append(model)
            request = impl.httpx.Request("POST", url)
            if model == model_a:
                return impl.httpx.Response(404, request=request, text="not found")
            model_b_calls += 1
            if model_b_calls == 1:
                content = {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"id": "call-1", "name": "get_ai_news", "args": {}},
                            "thoughtSignature": "opaque-signature",
                        }
                    ],
                }
            else:
                content = {"role": "model", "parts": [{"text": "final"}]}
            payload = {"response": {"candidates": [{"content": content}]}}
            return impl.httpx.Response(
                200,
                request=request,
                text=f"data: {json_module.dumps(payload)}\n\n",
            )

    async def _get_access_token(*, force_refresh=False):  # noqa: ANN001
        del force_refresh
        return "token", None

    async def _resolve_project(_token, _auth_file=None):  # noqa: ANN001
        return "project"

    async def _get_client(*_args, **_kwargs):  # noqa: ANN001, ANN202
        return _Client()

    monkeypatch.setattr(caller, "_get_access_token", _get_access_token)
    monkeypatch.setattr(caller, "_resolve_project", _resolve_project)
    monkeypatch.setattr(impl, "_get_pooled_http_client", _get_client)

    messages = [{"role": "user", "content": "news"}]
    first = asyncio.run(caller.chat_with_tools(messages, [_tool_schema()], False))
    assert first.model_used == model_b
    messages.append(caller.build_assistant_tool_calls_message(first))
    messages.extend(caller.build_tool_result_messages(first, [(first.tool_calls[0], "no results")]))
    caller._preferred_concrete_model = model_a
    second = asyncio.run(caller.chat_with_tools(messages, [_tool_schema()], False))

    assert second.content == "final"
    assert requested_models == [model_a, model_b, model_b]
