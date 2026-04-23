from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

pipeline_context = load_personification_module("nonebot_plugin_personification.handlers.reply_pipeline.pipeline_context")


class _FakeCaller:
    def __init__(self, content: str = "", *, should_fail: bool = False, vision_unavailable: bool = False) -> None:
        self._content = content
        self._should_fail = should_fail
        self._vision_unavailable = vision_unavailable
        self.calls = 0

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        del messages, tools, use_builtin_search
        self.calls += 1
        if self._should_fail:
            raise RuntimeError("boom")
        return SimpleNamespace(
            content=self._content,
            vision_unavailable=self._vision_unavailable,
        )


def _build_runtime(
    lite_caller: object | None = None,
    *,
    agent_caller: object | None = None,
    api_type: str = "openai",
    model: str = "gpt-4o-mini",
    lite_model: str = "gpt-4o-mini",
) -> object:
    logger = SimpleNamespace(debug=lambda *_args, **_kwargs: None)
    plugin_config = SimpleNamespace(
        personification_api_type=api_type,
        personification_model=model,
        personification_lite_model=lite_model,
    )
    providers = [{"api_type": api_type, "model": model}]
    return SimpleNamespace(
        lite_tool_caller=lite_caller,
        agent_tool_caller=agent_caller,
        logger=logger,
        plugin_config=plugin_config,
        get_configured_api_providers=lambda: providers,
    )


def test_classify_incoming_image_mface_short_circuit() -> None:
    pipeline_context.clear_image_classify_cache()
    runtime = _build_runtime()

    result = asyncio.run(
        pipeline_context.classify_incoming_image(
            runtime=runtime,
            image_url="data:image/png;base64,aaa",
            source_kind="mface",
            width=512,
            height=512,
        )
    )

    assert result.kind == "sticker"
    assert result.source == "rule"


def test_classify_incoming_image_missing_size_short_circuit() -> None:
    pipeline_context.clear_image_classify_cache()
    runtime = _build_runtime()

    result = asyncio.run(
        pipeline_context.classify_incoming_image(
            runtime=runtime,
            image_url="data:image/png;base64,aaa",
            source_kind="image",
            width=0,
            height=0,
        )
    )

    assert result.kind == "sticker"
    assert result.reason == "missing_size_short_circuit"


def test_classify_incoming_image_gif_short_circuit() -> None:
    pipeline_context.clear_image_classify_cache()
    runtime = _build_runtime()

    result = asyncio.run(
        pipeline_context.classify_incoming_image(
            runtime=runtime,
            image_url="http://example.com/x.gif",
            source_kind="image",
            width=512,
            height=512,
        )
    )

    assert result.kind == "sticker"
    assert result.reason == "gif_short_circuit"


def test_classify_incoming_image_uses_llm_and_hits_file_cache() -> None:
    pipeline_context.clear_image_classify_cache()
    lite_caller = _FakeCaller("photo")
    runtime = _build_runtime(lite_caller)

    first = asyncio.run(
        pipeline_context.classify_incoming_image(
            runtime=runtime,
            image_url="data:image/png;base64,aaa",
            source_kind="image",
            width=1920,
            height=1080,
            file_id="abc",
        )
    )
    second = asyncio.run(
        pipeline_context.classify_incoming_image(
            runtime=runtime,
            image_url="data:image/png;base64,bbb",
            source_kind="image",
            width=1920,
            height=1080,
            file_id="abc",
        )
    )

    assert first.kind == "photo"
    assert first.source == "lite_tool_caller"
    assert second.kind == "photo"
    assert second.source == "cache"
    assert lite_caller.calls == 1


def test_classify_incoming_image_falls_back_to_conservative_sticker_on_llm_failure() -> None:
    pipeline_context.clear_image_classify_cache()
    runtime = _build_runtime(_FakeCaller(should_fail=True))

    result = asyncio.run(
        pipeline_context.classify_incoming_image(
            runtime=runtime,
            image_url="data:image/png;base64,aaa",
            source_kind="image",
            width=1920,
            height=1080,
            file_id="fail",
        )
    )

    assert result.kind == "sticker"
    assert result.source == "fallback"


def test_classify_incoming_image_uses_size_fallback_when_no_vision_route() -> None:
    pipeline_context.clear_image_classify_cache()
    runtime = _build_runtime(
        _FakeCaller("photo"),
        api_type="openai_codex",
        model="gpt-5.4-mini",
        lite_model="gpt-5.4-mini",
    )

    result = asyncio.run(
        pipeline_context.classify_incoming_image(
            runtime=runtime,
            image_url="data:image/png;base64,aaa",
            source_kind="image",
            width=1920,
            height=1080,
            file_id="size-only",
        )
    )

    assert result.kind == "photo"
    assert result.source == "size_fallback"

