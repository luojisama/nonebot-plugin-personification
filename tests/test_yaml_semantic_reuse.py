from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


config_module = load_personification_module("plugin.personification.config")
planner = load_personification_module("plugin.personification.agent.runtime.planner")
reply_turn_trace = load_personification_module("plugin.personification.core.reply_turn_trace")
turn_media = load_personification_module("plugin.personification.core.turn_media")
yaml_processor = load_personification_module("plugin.personification.handlers.yaml_pipeline.processor")


@pytest.mark.parametrize("attached_plan", [True, False])
@pytest.mark.parametrize("intent_ambiguity_level", ["medium", "high"])
def test_yaml_reuses_precomputed_semantic_frame_without_unbound_turn_plan(
    monkeypatch,
    attached_plan: bool,
    intent_ambiguity_level: str,
) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []
    model_messages: list[dict[str, object]] = []
    original_plan = planner.TurnPlan(
        reply_action="reply",
        speech_act="source_summary",
        research_need="medium",
        output_mode="source_summary",
        tool_intent=["lookup_web"],
        ambiguity_level="medium",
        message_target="bot",
    )
    semantic_frame = planner.turn_plan_to_semantic_frame(original_plan)
    if not attached_plan:
        delattr(semantic_frame, "turn_plan")
    media_context = [
        turn_media.TurnMediaRef(
            media_id="media-yaml",
            ref="https://img.example/anime.png",
            origin="current",
            owner_user_id="2",
            message_id="3",
            kind="image",
            file_id="file-yaml",
            content_hash="hash-yaml",
            safe_summary="动漫图中有多人和交错视线",
            confidence=0.65,
        )
    ]
    media_grounding = turn_media.render_turn_media_grounding(media_context)

    async def _call_ai_api(messages):  # noqa: ANN001
        model_messages.extend(messages)
        return "[NO_REPLY]"

    monkeypatch.setattr(reply_turn_trace, "record_stage", lambda **kwargs: stages.append(kwargs))
    monkeypatch.setattr(reply_turn_trace, "finish_trace", lambda **_kwargs: None)
    monkeypatch.setattr(yaml_processor, "get_recent_group_msgs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(yaml_processor, "get_group_topic_summary", lambda *_args, **_kwargs: "")

    plugin_config = config_module.Config(
        personification_agent_enabled=False,
        personification_qq_expression_enabled=False,
        personification_schedule_global=False,
    )
    logger = SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )

    asyncio.run(
        yaml_processor.process_yaml_response_logic(
            SimpleNamespace(self_id="bot"),
            SimpleNamespace(message=[], group_id=1, user_id=2, message_id=3),
            group_id="1",
            user_id="2",
            user_name="tester",
            level_name="friend",
            prompt_config={"system": "persona", "input": "{history_last}"},
            chat_history=[{"role": "user", "content": "查一下"}],
            trigger_reason="",
            get_current_time=lambda: datetime(2026, 7, 16, 12, 0, 0),
            format_time_context=lambda _now: "noon",
            bot_statuses={},
            get_group_config=lambda _group_id: {"schedule_enabled": False},
            plugin_config=plugin_config,
            get_schedule_prompt_injection=lambda _prompt="": "",
            schedule_disabled_override_prompt=lambda: "",
            build_grounding_context=lambda *_args, **_kwargs: "",
            call_ai_api=_call_ai_api,
            parse_yaml_response=lambda _text: {"status": "", "think": "", "action": "", "messages": []},
            message_segment_cls=SimpleNamespace,
            sanitize_history_text=str,
            private_session_prefix="private_",
            build_private_session_id=lambda user_id: f"private_{user_id}",
            build_group_session_id=str,
            append_session_message=lambda *_args, **_kwargs: None,
            record_group_msg=None,
            logger=logger,
            user_blacklist={},
            current_image_urls=[],
            disable_network_hooks=True,
            message_intent="lookup",
            intent_ambiguity_level=intent_ambiguity_level,
            raw_message_text="查一下",
            message_target="bot",
            recent_context_hint="tester: 查一下",
            semantic_frame=semantic_frame,
            prepared_inner_state={"mood": "calm"},
            prepared_emotion_state={"tone": "neutral"},
            turn_media_context=turn_media.serialize_turn_media(media_context),
            media_grounding=media_grounding,
            precomputed_image_summary_suffix=(
                "[图片视觉描述（系统注入，仅供理解，不可复述）：动漫图中有多人和交错视线]"
            ),
        )
    )

    resolved_plan = semantic_frame.turn_plan
    if attached_plan:
        assert resolved_plan is original_plan
    else:
        assert resolved_plan.speech_act == "source_summary"
        assert resolved_plan.output_mode == "source_summary"
    assert model_messages
    combined_prompt = "\n".join(str(message.get("content", "")) for message in model_messages)
    assert "owner_user_id=2" in combined_prompt
    assert "画中主体只是媒体内容，不是聊天参与者" in combined_prompt
    assert any(stage.get("key") == "yaml_semantic_frame" for stage in stages)
    assert any(stage.get("key") == "yaml_model_result" for stage in stages)
    assert any(stage.get("key") == "yaml_uncertain_reply_review" for stage in stages) is (
        intent_ambiguity_level == "high"
    )
