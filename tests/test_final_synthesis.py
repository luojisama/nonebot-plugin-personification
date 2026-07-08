from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module

final_synthesis = load_personification_module("plugin.personification.agent.runtime.final_synthesis")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")


async def _noop_handler(**_kwargs):  # noqa: ANN001
    return "ok"


class _WrapCaller:
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


def _register(registry, name: str, metadata: dict | None = None) -> None:  # noqa: ANN001
    registry.register(
        tool_registry.AgentTool(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_noop_handler,
            metadata=metadata or {},
        )
    )


def test_direct_tool_result_agent_result_silences_queued_send() -> None:
    registry = tool_registry.ToolRegistry()
    _register(
        registry,
        "send_custom_expression",
        {
            "side_effect": "send_message",
            "final_behavior": "silence_on_success",
            "intent_tags": ["expression"],
        },
    )

    result = final_synthesis.direct_tool_result_agent_result(
        registry=registry,
        tool_name="send_custom_expression",
        result_text=json.dumps({"ok": True, "queued": True}, ensure_ascii=False),
        pending_actions=[{"type": "send", "params": {}}],
    )

    assert result is not None
    assert result.text == "[SILENCE]"
    assert result.pending_actions == [{"type": "send", "params": {}}]
    assert result.bypass_length_limits is False


def test_synthesize_max_steps_result_returns_no_reply_without_tool_result() -> None:
    result = asyncio.run(
        final_synthesis.synthesize_max_steps_result(
            registry=tool_registry.ToolRegistry(),
            tool_name="",
            result_text="",
            user_query_text="帮我查一下",
            messages=[],
            pending_actions=[],
            tool_caller=_WrapCaller("不会被调用"),
        )
    )

    assert result.text == "[NO_REPLY]"


def test_synthesize_max_steps_result_wraps_last_tool_result_with_persona() -> None:
    caller = _WrapCaller("这事我看下来，先按官网说法走。")
    payload = {
        "query": "某插件配置",
        "results": [
            {"title": "官方文档", "snippet": "配置项说明", "url": "https://example.com/docs"},
        ],
    }

    result = asyncio.run(
        final_synthesis.synthesize_max_steps_result(
            registry=tool_registry.ToolRegistry(),
            tool_name="search_web",
            result_text=json.dumps(payload, ensure_ascii=False),
            user_query_text="某插件配置怎么开",
            messages=[{"role": "system", "content": "你是群友。" * 120}],
            pending_actions=[],
            tool_caller=caller,
        )
    )

    assert result.text == "这事我看下来，先按官网说法走。"
    assert result.direct_output is False
    assert result.bypass_length_limits is False
    assert len(caller.calls) == 1
    sent_messages = caller.calls[0]["messages"]
    assert sent_messages[0]["role"] == "system"
    assert "你是群友" in sent_messages[0]["content"]
