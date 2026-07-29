from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


media_understanding = load_personification_module("plugin.personification.core.media_understanding")
vision_caller = load_personification_module(
    "plugin.personification.skills.skillpacks.vision_caller.scripts.impl"
)


def test_analyze_images_tries_primary_routes_before_fallback(monkeypatch) -> None:
    calls: list[str] = []

    class _FakeCaller:
        def __init__(self, model: str) -> None:
            self.model = model

        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            del messages, tools, use_builtin_search
            calls.append(self.model)
            if self.model == "text-only":
                return SimpleNamespace(content="", vision_unavailable=True)
            return SimpleNamespace(content="primary vision result", vision_unavailable=False)

    class _Fallback:
        async def describe(self, prompt: str, image_url: str) -> str:
            del prompt, image_url
            raise AssertionError("fallback should wait until primary routes are exhausted")

    def _fake_build_tool_caller(config):  # noqa: ANN001
        return _FakeCaller(str(getattr(config, "personification_model", "") or ""))

    monkeypatch.setattr(media_understanding, "build_tool_caller", _fake_build_tool_caller)
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_thinking_mode="none"),
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        get_configured_api_providers=lambda: [
            {
                "name": "primary_text",
                "api_type": "openai",
                "api_url": "https://primary.example/v1",
                "api_key": "primary-key-1",
                "model": "text-only",
            },
            {
                "name": "primary_vision",
                "api_type": "openai",
                "api_url": "https://primary.example/v1",
                "api_key": "primary-key-2",
                "model": "vision-ok",
            },
        ],
    )

    result, route = asyncio.run(
        media_understanding.analyze_images_with_route_or_fallback(
            runtime=runtime,
            prompt="describe",
            image_refs=["data:image/png;base64,AA=="],
            fallback_vision_caller=_Fallback(),
        )
    )

    assert result == "primary vision result"
    assert route == "route_direct"
    assert calls == ["text-only", "vision-ok"]


def test_joint_only_analysis_sends_both_images_in_one_primary_request(monkeypatch) -> None:  # noqa: ANN001
    requests: list[list[dict]] = []

    class _FakeCaller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            del tools, use_builtin_search
            requests.append(messages)
            return SimpleNamespace(content="joint result", vision_unavailable=False)

    monkeypatch.setattr(media_understanding, "build_tool_caller", lambda _config: _FakeCaller())
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_thinking_mode="none", personification_model_overrides={}),
        get_configured_api_providers=lambda: [
            {"name": "vision", "api_type": "openai", "api_key": "key", "model": "vision-model"}
        ],
    )
    refs = ["data:image/png;base64,AA==", "data:image/png;base64,AQ=="]
    result, route = asyncio.run(
        media_understanding.analyze_images_with_primary_route_joint_only(
            runtime=runtime,
            prompt="compare",
            image_refs=refs,
        )
    )
    assert (result, route) == ("joint result", "route_direct")
    assert len(requests) == 1
    content = requests[0][0]["content"]
    image_parts = [item for item in content if item.get("type") == "image_url"]
    assert [item["image_url"]["url"] for item in image_parts] == refs


def test_joint_only_primary_failure_never_uses_single_image_fallback(monkeypatch) -> None:  # noqa: ANN001
    class _FakeCaller:
        async def chat_with_tools(self, *_args, **_kwargs):  # noqa: ANN001
            return SimpleNamespace(content="", vision_unavailable=True)

    class _ForbiddenFallback:
        async def describe(self, *_args, **_kwargs):  # noqa: ANN001
            raise AssertionError("joint-only API must not use per-image fallback")

    monkeypatch.setattr(media_understanding, "build_tool_caller", lambda _config: _FakeCaller())
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_thinking_mode="none", personification_model_overrides={}),
        vision_caller=_ForbiddenFallback(),
        get_configured_api_providers=lambda: [
            {"name": "vision", "api_type": "openai", "api_key": "key", "model": "vision-model"}
        ],
    )
    result, route = asyncio.run(
        media_understanding.analyze_images_with_primary_route_joint_only(
            runtime=runtime,
            prompt="compare",
            image_refs=["data:image/png;base64,AA==", "data:image/png;base64,AQ=="],
        )
    )
    assert result == ""
    assert route == "joint_vision_unavailable"


def test_gemini_media_uses_only_google_api_key_header(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, url, headers=None, params=None, json=None):  # noqa: ANN001, ANN201
            captured.update(url=url, headers=headers or {}, params=params or {}, json=json or {})
            return _Resp()

    monkeypatch.setattr(media_understanding.httpx, "AsyncClient", _Client)
    result = asyncio.run(media_understanding._call_gemini_media(
        api_key="media-secret",
        base_url="https://gemini-media.example",
        model="gemini-test",
        prompt="describe",
    ))

    assert result == "ok"
    assert captured["headers"]["x-goog-api-key"] == "media-secret"
    assert "Authorization" not in captured["headers"]
    assert captured["params"] == {}


def test_gemini_vision_uses_only_google_api_key_header(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"candidates": [{"content": {"parts": [{"text": "vision ok"}]}}]}

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, url, headers=None, params=None, json=None):  # noqa: ANN001, ANN201
            captured.update(url=url, headers=headers or {}, params=params or {}, json=json or {})
            return _Resp()

    monkeypatch.setattr(vision_caller.httpx, "AsyncClient", _Client)
    caller = vision_caller.GeminiVisionCaller(
        api_key="vision-secret",
        base_url="https://gemini-vision.example",
        model="gemini-test",
    )
    result = asyncio.run(caller.describe("describe", "data:image/png;base64,AA=="))

    assert result == "vision ok"
    assert captured["headers"]["x-goog-api-key"] == "vision-secret"
    assert "Authorization" not in captured["headers"]
    assert captured["params"] == {}


def test_gemini_vision_batches_function_responses_and_preserves_signature(monkeypatch) -> None:  # noqa: ANN001
    captured: list[dict] = []
    native_parts = [
        {
            "functionCall": {"id": "call-1", "name": "first", "args": {}},
            "thoughtSignature": "opaque-signature",
        },
        {"functionCall": {"id": "call-2", "name": "second", "args": {}}},
    ]
    responses = [
        {"candidates": [{"content": {"role": "model", "parts": native_parts}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}]},
    ]
    caller = vision_caller.GeminiVisionCaller(
        api_key="vision-secret",
        base_url="https://gemini-vision.example",
        model="gemini-3-flash-agent",
    )

    async def _generate(payload):  # noqa: ANN001, ANN202
        captured.append(payload)
        return responses.pop(0)

    async def _handler(name, _args):  # noqa: ANN001, ANN202
        return f"result-{name}"

    monkeypatch.setattr(caller, "_generate_content", _generate)
    tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("first", "second")
    ]

    result = asyncio.run(
        caller.describe_with_tools(
            "inspect",
            "data:image/png;base64,AA==",
            tools,
            tool_handler=_handler,
        )
    )

    assert result == "done"
    assert captured[1]["contents"][-2]["parts"] == native_parts
    assert captured[1]["contents"][-1] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "id": "call-1",
                    "name": "first",
                    "response": {"result": "result-first"},
                }
            },
            {
                "functionResponse": {
                    "id": "call-2",
                    "name": "second",
                    "response": {"result": "result-second"},
                }
            },
        ],
    }


def test_vision_builder_passes_gemini_auth_mode_without_breaking_anthropic() -> None:
    gemini = vision_caller.build_vision_caller(SimpleNamespace(
        personification_api_type="gemini",
        personification_api_key="gemini-secret",
        personification_api_url="https://gemini.example",
        personification_model="gemini-test",
        personification_gemini_auth_mode="bearer",
        personification_vision_fallback_enabled=False,
    ))
    anthropic = vision_caller.build_vision_caller(SimpleNamespace(
        personification_api_type="anthropic",
        personification_api_key="anthropic-secret",
        personification_api_url="https://anthropic.example",
        personification_model="claude-test",
        personification_gemini_auth_mode="bearer",
        personification_vision_fallback_enabled=False,
    ))

    assert isinstance(gemini, vision_caller.GeminiVisionCaller)
    assert gemini.auth_mode == "bearer"
    assert isinstance(anthropic, vision_caller.AnthropicVisionCaller)


def test_media_provider_proxy_exposes_gemini_auth_mode() -> None:
    proxy = media_understanding._ProviderConfigProxy(
        SimpleNamespace(personification_gemini_auth_mode="auto"),
        {"gemini_auth_mode": "bearer"},
    )

    assert proxy.personification_gemini_auth_mode == "bearer"
