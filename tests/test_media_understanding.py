from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


media_understanding = load_personification_module("plugin.personification.core.media_understanding")


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
