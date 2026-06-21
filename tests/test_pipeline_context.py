from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

pipeline_context = load_personification_module("plugin.personification.handlers.reply_pipeline.pipeline_context")


def test_should_use_agent_for_reply_rejects_high_ambiguity_lookup_without_direct_mention() -> None:
    decision = pipeline_context.should_use_agent_for_reply(
        plugin_config=SimpleNamespace(
            personification_agent_enabled=True,
            personification_web_search_always=False,
        ),
        tool_registry=object(),
        agent_tool_caller=object(),
        message_intent="lookup",
        ambiguity_level="high",
        is_direct_mention=False,
        has_image_input=False,
    )

    assert decision is False


def test_should_use_agent_for_reply_keeps_direct_mention_lookup_enabled() -> None:
    decision = pipeline_context.should_use_agent_for_reply(
        plugin_config=SimpleNamespace(
            personification_agent_enabled=True,
            personification_web_search_always=False,
        ),
        tool_registry=object(),
        agent_tool_caller=object(),
        message_intent="lookup",
        ambiguity_level="high",
        is_direct_mention=True,
        has_image_input=False,
    )

    assert decision is True


def test_confidence_style_instruction_medium() -> None:
    text = pipeline_context.build_confidence_style_instruction(0.7, is_group=True)

    assert "我理解是" in text
    assert "不要把不确定的推断说死" in text


def test_confidence_style_instruction_low_group() -> None:
    text = pipeline_context.build_confidence_style_instruction(0.35, is_group=True)

    assert "[NO_REPLY]" in text
