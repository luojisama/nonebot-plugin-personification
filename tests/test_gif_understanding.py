from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any

import pytest

from ._loader import load_personification_module


config_mod = load_personification_module("plugin.personification.config")
gif_understanding = load_personification_module("plugin.personification.core.gif_understanding")
pipeline_sticker = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.pipeline_sticker"
)


class _FakeSegment:
    def __init__(self, data: dict[str, Any]) -> None:
        self.type = "image"
        self.data = data


class _StubHttpClient:
    pass


def _logger() -> SimpleNamespace:
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
    )


def _runtime(**config_overrides: Any) -> SimpleNamespace:
    defaults = {
        "personification_gif_understanding_enabled": True,
        "personification_gif_understanding_timeout": 5.0,
        "personification_gif_max_bytes": 8 * 1024 * 1024,
        "personification_gif_max_decode_frames": 180,
        "personification_gif_sample_frames": 4,
        "personification_gif_contact_sheet_long_edge": 900,
        "personification_gif_max_per_turn": 1,
        "personification_gif_summary_cache_enabled": False,
        "personification_thinking_mode": "none",
    }
    defaults.update(config_overrides)
    return SimpleNamespace(
        plugin_config=SimpleNamespace(**defaults),
        vision_caller=None,
        logger=_logger(),
        get_configured_api_providers=lambda: [],
    )


def _gif_bytes(frame_count: int = 6) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    frames = []
    colors = ["red", "green", "blue", "yellow", "purple", "orange", "white", "black"]
    for index in range(frame_count):
        image = Image.new("RGB", (64, 48), colors[index % len(colors)])
        frames.append(image)
    out = io.BytesIO()
    frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
    )
    return out.getvalue()


def test_gif_config_defaults_are_off_and_bounded() -> None:
    cfg = config_mod.Config()

    assert cfg.personification_gif_understanding_enabled is False
    assert cfg.personification_gif_understanding_timeout == 12.0
    assert gif_understanding.get_gif_understanding_timeout(cfg) <= 35.0
    assert cfg.personification_gif_max_per_turn == 1
    assert cfg.personification_gif_summary_cache_enabled is True


def test_summarize_gif_disabled_returns_without_visual_call(monkeypatch) -> None:
    called = False

    async def _fake_analyze(**_kwargs):  # noqa: ANN003
        nonlocal called
        called = True
        return "不应调用", "route_direct"

    monkeypatch.setattr(gif_understanding, "analyze_images_with_route_or_fallback", _fake_analyze)

    result = asyncio.run(
        gif_understanding.summarize_gif_bytes(
            runtime=_runtime(personification_gif_understanding_enabled=False),
            payload=b"GIF89a",
        )
    )

    assert result.route == "disabled"
    assert result.summary == ""
    assert called is False


def test_summarize_gif_builds_single_contact_sheet_for_visual_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_analyze(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return "角色先挥手，然后露出开心表情。", "route_direct"

    monkeypatch.setattr(gif_understanding, "analyze_images_with_route_or_fallback", _fake_analyze)

    result = asyncio.run(
        gif_understanding.summarize_gif_bytes(
            runtime=_runtime(),
            payload=_gif_bytes(frame_count=6),
            source_hint="image",
            summary_hint="动画表情",
        )
    )

    assert result.summary == "角色先挥手，然后露出开心表情。"
    assert result.route == "route_direct"
    assert result.frame_count == 6
    assert result.sampled_frames == 4
    assert len(captured["image_refs"]) == 1
    assert captured["image_refs"][0].startswith("data:image/jpeg;base64,")
    assert "不是多张无关图片" in captured["prompt"]


def test_frame_sampling_keeps_timeline_with_stride_limit() -> None:
    indices = gif_understanding._select_frame_indices(50, 8)

    assert indices[0] == 0
    assert indices[-1] == 49
    assert len(indices) <= 8
    assert indices == sorted(indices)


def test_summarize_gif_timeout_is_bounded(monkeypatch) -> None:
    async def _slow_inner(**_kwargs):  # noqa: ANN003
        await asyncio.sleep(2.0)
        return gif_understanding.GifSummaryResult(summary="late", route="route_direct")

    monkeypatch.setattr(gif_understanding, "_summarize_gif_bytes_inner", _slow_inner)

    result = asyncio.run(
        gif_understanding.summarize_gif_bytes(
            runtime=_runtime(personification_gif_understanding_timeout=1.0),
            payload=b"GIF89a",
        )
    )

    assert result.route == "timeout"
    assert result.duration_ms < 1500


def test_pipeline_gif_default_off_preserves_no_reply() -> None:
    message_text_ref: list[str] = []
    image_urls: list[str] = []
    sticker_candidates_ref: list[Any] = []
    stop_reply_ref = [False]

    asyncio.run(
        pipeline_sticker.extract_images_from_segment(
            _FakeSegment({"url": "https://example.com/a.gif", "file": "a.gif"}),
            runtime=_runtime(personification_gif_understanding_enabled=False),
            http_client=_StubHttpClient(),
            message_text_ref=message_text_ref,
            image_urls=image_urls,
            sticker_candidates_ref=sticker_candidates_ref,
            logger=_logger(),
            stop_reply_ref=stop_reply_ref,
        )
    )

    assert stop_reply_ref[0] is True
    assert message_text_ref == []
    assert image_urls == []


def test_pipeline_gif_enabled_injects_summary_without_stopping(monkeypatch) -> None:
    async def _fake_download(**_kwargs):  # noqa: ANN003
        return "image/gif", b"GIF89a", True

    async def _fake_summarize(**_kwargs):  # noqa: ANN003
        return gif_understanding.GifSummaryResult(
            summary="角色点头后举起牌子表示赞同。",
            route="route_direct",
            frame_count=6,
            sampled_frames=4,
            duration_ms=120,
        )

    monkeypatch.setattr(pipeline_sticker, "download_safe_image_bytes", _fake_download)
    monkeypatch.setattr(pipeline_sticker, "summarize_gif_bytes", _fake_summarize)

    message_text_ref: list[str] = []
    image_urls: list[str] = []
    sticker_candidates_ref: list[Any] = []
    stop_reply_ref = [False]

    asyncio.run(
        pipeline_sticker.extract_images_from_segment(
            _FakeSegment({"url": "https://example.com/a.gif", "file": "a.gif"}),
            runtime=_runtime(),
            http_client=_StubHttpClient(),
            message_text_ref=message_text_ref,
            image_urls=image_urls,
            sticker_candidates_ref=sticker_candidates_ref,
            logger=_logger(),
            stop_reply_ref=stop_reply_ref,
            gif_understanding_counter_ref=[0],
        )
    )

    assert stop_reply_ref[0] is False
    assert image_urls == []
    assert message_text_ref == ["[动态表情语义（系统注入，仅供理解，不可复述）：角色点头后举起牌子表示赞同。]"]
