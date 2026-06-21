from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


chat_intent = load_personification_module("plugin.personification.core.chat_intent")
pipeline_context = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.pipeline_context"
)


def test_scenario_field_parsed_from_llm_response() -> None:
    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            return SimpleNamespace(
                content=(
                    '{"chat_intent":"banter","ambiguity_level":"low",'
                    '"recommend_silence":false,"domain_focus":"social",'
                    '"conversation_scenario":"sarcasm_irony","confidence":0.7}'
                )
            )

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "你可真行啊",
            is_group=True,
            tool_caller=_Caller(),
        )
    )

    assert frame.conversation_scenario == "sarcasm_irony"


def test_unknown_scenario_defaults_to_normal() -> None:
    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            return SimpleNamespace(
                content=(
                    '{"chat_intent":"banter","ambiguity_level":"low",'
                    '"recommend_silence":false,"domain_focus":"social",'
                    '"conversation_scenario":"unknown_value","confidence":0.7}'
                )
            )

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "随便说点什么",
            is_group=True,
            tool_caller=_Caller(),
        )
    )

    assert frame.conversation_scenario == "normal"


def test_scenario_instruction_for_argument() -> None:
    text = pipeline_context.build_scenario_instruction("argument")

    assert "不要站队" in text


def test_scenario_instruction_for_sarcasm() -> None:
    text = pipeline_context.build_scenario_instruction("sarcasm_irony")

    assert "反讽" in text


def test_scenario_instruction_normal_is_empty() -> None:
    assert pipeline_context.build_scenario_instruction("normal") == ""
    assert pipeline_context.build_scenario_instruction("") == ""
