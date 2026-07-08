from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from ._loader import load_personification_module


sticker_library = load_personification_module("plugin.personification.core.sticker_library")
sticker_labeler_impl = load_personification_module(
    "plugin.personification.skills.skillpacks.sticker_labeler.scripts.impl"
)
web_grounding = load_personification_module("plugin.personification.core.web_grounding")


def _logger() -> SimpleNamespace:
    return SimpleNamespace(
        debug=lambda *_a, **_k: None,
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
    )


def _runtime(**overrides: Any) -> SimpleNamespace:
    cfg = SimpleNamespace(
        personification_sticker_collect_meme_policy="accept",
        personification_sticker_labeler_research_enabled=True,
        personification_sticker_labeler_research_max_queries=2,
        personification_sticker_labeler_research_timeout=5.0,
    )
    for key, value in overrides.pop("config_overrides", {}).items():
        setattr(cfg, key, value)
    return SimpleNamespace(
        plugin_config=cfg,
        lite_call_ai_api=overrides.pop("lite_call_ai_api", None),
        call_ai_api=overrides.pop("call_ai_api", None),
        get_current_time=overrides.pop("get_current_time", lambda: datetime(2026, 6, 30, 12, 0)),
        logger=_logger(),
        **overrides,
    )


def _vision_json(**overrides: Any) -> str:
    payload = {
        "summary": "初稿概括",
        "description": "二次元角色做出得意动作，图中文字清楚",
        "ocr_text": "你也想起舞吗",
        "use_hint": "适合接梗或轻微挑衅时使用",
        "avoid_hint": "严肃安慰或道歉场景不适合",
        "mood_tags": ["得意", "搞笑"],
        "scene_tags": ["接梗", "吐槽"],
        "proactive_send": True,
        "should_collect": True,
        "collect_confidence": 0.82,
        "collect_reason": "梗明确",
        "is_sticker": True,
        "style": "anime",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_research_disabled_keeps_single_vision_pass(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    async def _fake_vision(**kwargs):  # noqa: ANN001
        calls.append(kwargs["prompt"])
        return _vision_json(summary="只跑初稿"), "vision_initial"

    monkeypatch.setattr(sticker_library, "analyze_images_with_route_or_fallback", _fake_vision)

    result = asyncio.run(
        sticker_library.analyze_sticker_image(
            runtime=_runtime(
                config_overrides={"personification_sticker_labeler_research_enabled": False}
            ),
            image_refs=["data:image/png;base64,aaa"],
        )
    )

    assert len(calls) == 1
    assert result.summary == "只跑初稿"
    assert result.vision_route == "vision_initial"


def test_research_enabled_searches_and_refines_label(monkeypatch) -> None:  # noqa: ANN001
    vision_prompts: list[str] = []
    search_calls: list[dict[str, Any]] = []

    async def _fake_vision(**kwargs):  # noqa: ANN001
        vision_prompts.append(kwargs["prompt"])
        if len(vision_prompts) == 1:
            return _vision_json(summary="初稿待查"), "vision_initial"
        return _vision_json(summary="梗图复核", description="结合检索后确认是台词梗图"), "vision_refined"

    async def _fake_lite_call(messages, **kwargs):  # noqa: ANN001
        assert kwargs["use_builtin_search"] is False
        assert "你也想起舞吗" in messages[-1]["content"]
        return json.dumps({"queries": ["你也想起舞吗 表情包 出处"], "reason": "有台词"}, ensure_ascii=False)

    async def _fake_search(query: str, *, context_hint: str, get_now, logger):  # noqa: ANN001
        search_calls.append({"query": query, "context_hint": context_hint, "now": get_now()})
        return "搜索摘要：这句常见于网络梗图语境，用于挑衅或接梗。"

    monkeypatch.setattr(sticker_library, "analyze_images_with_route_or_fallback", _fake_vision)
    monkeypatch.setattr(web_grounding, "do_web_search", _fake_search)

    result = asyncio.run(
        sticker_library.analyze_sticker_image(
            runtime=_runtime(lite_call_ai_api=_fake_lite_call),
            image_refs=["data:image/png;base64,aaa"],
        )
    )

    assert [call["query"] for call in search_calls] == ["你也想起舞吗 表情包 出处"]
    assert "表情包打标" in search_calls[0]["context_hint"]
    assert search_calls[0]["now"].year == 2026
    assert len(vision_prompts) == 2
    assert "【第一轮视觉初稿】" in vision_prompts[1]
    assert "【联网检索摘要】" in vision_prompts[1]
    assert "搜索摘要" in vision_prompts[1]
    assert result.summary == "梗图复核"
    assert result.vision_route == "vision_refined+web_research"


def test_research_search_failure_falls_back_to_initial_label(monkeypatch) -> None:  # noqa: ANN001
    vision_calls = 0

    async def _fake_vision(**_kwargs):  # noqa: ANN001
        nonlocal vision_calls
        vision_calls += 1
        if vision_calls > 1:
            raise AssertionError("search failure should not trigger refine vision pass")
        return _vision_json(summary="保留初稿"), "vision_initial"

    async def _fake_lite_call(_messages, **_kwargs):  # noqa: ANN001
        return json.dumps({"queries": ["不存在的表情包线索"], "reason": "需要查"}, ensure_ascii=False)

    async def _failing_search(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("network down")

    monkeypatch.setattr(sticker_library, "analyze_images_with_route_or_fallback", _fake_vision)
    monkeypatch.setattr(web_grounding, "do_web_search", _failing_search)

    result = asyncio.run(
        sticker_library.analyze_sticker_image(
            runtime=_runtime(lite_call_ai_api=_fake_lite_call),
            image_refs=["data:image/png;base64,aaa"],
        )
    )

    assert vision_calls == 1
    assert result.summary == "保留初稿"
    assert result.vision_route == "vision_initial"


def test_planner_empty_query_list_is_respected() -> None:
    async def _empty_lite_call(_messages, **_kwargs):  # noqa: ANN001
        return json.dumps({"queries": [], "reason": "普通表情不用查"}, ensure_ascii=False)

    result = sticker_library.StickerVisionResult(
        summary="有 OCR 但模型认为不用查",
        description="普通表情",
        ocr_text="早上好",
        use_hint="打招呼时使用",
        avoid_hint="严肃场景不用",
        mood_tags=["开心"],
        scene_tags=["打招呼"],
        proactive_send=True,
        should_collect=True,
        collect_reason="常用",
        is_sticker=True,
        style="meme",
        vision_route="vision_initial",
        collect_confidence=0.8,
    )

    queries = asyncio.run(
        sticker_library._plan_sticker_research_queries(  # noqa: SLF001
            runtime=_runtime(lite_call_ai_api=_empty_lite_call),
            result=result,
            limit=2,
        )
    )

    assert queries == []


def test_parse_research_queries_sanitizes_duplicates_and_limit() -> None:
    raw = {
        "queries": [
            "",
            "  角色   台词  ",
            "角色 台词",
            "x" * 81,
            "另一个 梗",
            "第三个",
        ]
    }

    assert sticker_library._parse_sticker_research_queries(raw, limit=0) == []  # noqa: SLF001
    assert sticker_library._parse_sticker_research_queries(raw, limit=2) == [  # noqa: SLF001
        "角色 台词",
        "另一个 梗",
    ]


def test_sticker_labeler_includes_gif_and_uses_contact_sheet(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    gif_understanding = load_personification_module("plugin.personification.core.gif_understanding")
    gif_file = tmp_path / "motion.gif"
    gif_file.write_bytes(b"GIF89a-demo")
    labeler = sticker_labeler_impl.StickerLabeler(tmp_path, logger=_logger())
    assert gif_file in labeler.list_sticker_files()

    monkeypatch.setattr(
        gif_understanding,
        "build_gif_contact_sheet_data_url",
        lambda payload, config: {
            "data_url": "data:image/jpeg;base64,abc",
            "frame_count": 12,
            "sampled_frames": 4,
            "duration_ms": 900,
        },
    )
    captured: dict[str, Any] = {}

    async def _fake_analyze(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return sticker_library.StickerVisionResult(
            summary="挥手拒绝",
            description="角色连续挥手，文字和动作表达拒绝",
            ocr_text="不行",
            use_hint="拒绝请求时发",
            avoid_hint="严肃道歉时不适合",
            mood_tags=["拒绝"],
            scene_tags=["拒绝请求"],
            proactive_send=False,
            should_collect=True,
            collect_reason="动图明确",
            is_sticker=True,
            style="anime",
            vision_route="route_direct",
            collect_confidence=0.9,
        )

    monkeypatch.setattr(sticker_labeler_impl, "analyze_sticker_image", _fake_analyze)
    runtime = SimpleNamespace(plugin_config=SimpleNamespace(), vision_caller=None)
    result = asyncio.run(labeler.label_file(gif_file, runtime))

    assert captured["image_refs"] == ["data:image/jpeg;base64,abc"]
    assert "GIF/动态表情" in captured["prompt"]
    assert "总帧数约 12" in captured["prompt"]
    assert result["description"] == "角色连续挥手，文字和动作表达拒绝"
