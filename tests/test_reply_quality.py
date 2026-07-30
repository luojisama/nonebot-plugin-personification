from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


reply_quality = load_personification_module("plugin.personification.agent.runtime.reply_quality")
final_synthesis = load_personification_module("plugin.personification.agent.runtime.final_synthesis")


class _RewriteCaller:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "use_builtin_search": use_builtin_search,
            }
        )
        return SimpleNamespace(content=self.content)


def _agent_result(text: str, *, direct_output: bool = False) -> object:
    return final_synthesis.AgentResult(
        text=text,
        pending_actions=[],
        direct_output=direct_output,
        bypass_length_limits=False,
    )


def test_finalize_agent_reply_quality_normalizes_markdown_without_llm() -> None:
    traces: list[dict[str, object]] = []

    result = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("**广州** 接下来雨不少"),
            tool_caller=None,
            messages=[],
            record_trace=lambda **kwargs: traces.append(kwargs),
            reason="unit",
        )
    )

    assert result.text == "广州 接下来雨不少"
    assert result.quality_checks[-1]["action"] == "normalized"
    assert "markdown_or_trace" in result.quality_checks[-1]["flags"]
    assert traces[-1]["key"] == "agent_reply_quality"
    assert "action=normalized" in traces[-1]["detail"]


def test_finalize_agent_reply_quality_does_not_rewrite_local_normalization() -> None:
    caller = _RewriteCaller("[NO_REPLY]")

    result = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("SILENCE: 这个事得看具体情况"),
            tool_caller=caller,
            messages=[],
            reason="unit",
        )
    )

    assert result.text == "这个事得看具体情况"
    assert caller.calls == []
    assert result.quality_checks[-1]["action"] == "normalized"


def test_finalize_agent_reply_quality_rewrites_observer_posture_once() -> None:
    caller = _RewriteCaller("那先别绕远，就看当前这个点")
    traces: list[dict[str, object]] = []

    result = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("我先看看情况，等会再说"),
            tool_caller=caller,
            messages=[{"role": "system", "content": "你是群友。"}],
            record_trace=lambda **kwargs: traces.append(kwargs),
            reason="unit",
        )
    )

    assert result.text == "那先别绕远，就看当前这个点"
    assert len(caller.calls) == 1
    assert caller.calls[0]["tools"] == []
    assert result.quality_checks[-1]["action"] == "rewritten"
    assert result.quality_checks[-1]["revision_attempted"] is True
    assert "formulaic_tic" in result.quality_checks[-1]["flags"]
    assert "action=rewritten" in traces[-1]["detail"]


def test_finalize_agent_reply_quality_rewrites_group_visible_question() -> None:
    caller = _RewriteCaller("地点没拿准，我别乱猜天气。")

    result = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("你那边是哪儿啊，我别乱猜天气。"),
            tool_caller=caller,
            messages=[{"role": "system", "content": "你是群友。"}],
            reason="unit",
        )
    )

    assert result.text == "地点没拿准，我别乱猜天气。"
    assert len(caller.calls) == 1
    assert "group_visible_question" in result.quality_checks[-1]["flags"]
    assert result.quality_checks[-1]["action"] == "rewritten"


def test_finalize_agent_reply_quality_silences_group_question_rewrite_if_still_question() -> None:
    caller = _RewriteCaller("你那边是哪儿啊")

    result = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("你那边是哪儿啊，我别乱猜天气。"),
            tool_caller=caller,
            messages=[{"role": "system", "content": "你是群友。"}],
            reason="unit",
        )
    )

    assert result.text == "[SILENCE]"
    assert result.quality_checks[-1]["action"] == "silenced"


def test_finalize_agent_reply_quality_silences_when_revision_still_ooc() -> None:
    caller = _RewriteCaller("我先看看情况，等会再说")

    result = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("根据搜索结果，我先看看情况，等会再说"),
            tool_caller=caller,
            messages=[],
            reason="unit",
        )
    )

    assert result.text == "[SILENCE]"
    assert result.quality_checks[-1]["action"] == "silenced"
    assert result.quality_checks[-1]["revision_attempted"] is True


def test_finalize_agent_reply_quality_skips_direct_and_control_outputs() -> None:
    direct = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("https://example.com/file.txt", direct_output=True),
            tool_caller=_RewriteCaller("不该调用"),
            messages=[],
            reason="unit",
        )
    )
    control = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            _agent_result("[NO_REPLY]"),
            tool_caller=_RewriteCaller("不该调用"),
            messages=[],
            reason="unit",
        )
    )

    assert direct.text == "https://example.com/file.txt"
    assert direct.quality_checks[-1]["action"] == "skipped"
    assert control.text == "[NO_REPLY]"
    assert control.quality_checks[-1]["action"] == "skipped"
