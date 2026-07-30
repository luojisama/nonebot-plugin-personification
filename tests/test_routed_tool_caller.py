from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

ai_routes = load_personification_module("plugin.personification.core.ai_routes")
tool_impl = load_personification_module("plugin.personification.skills.skillpacks.tool_caller.scripts.impl")


class _FakeCaller:
    def __init__(self, name: str, responses: list[object]) -> None:
        self.name = name
        self._responses = list(responses)
        self.messages_seen: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        self.messages_seen.append(list(messages or []))
        del messages, tools, use_builtin_search
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def build_tool_result_message(self, tool_call_id: str, tool_name: str, result: str) -> dict[str, str]:
        return {
            "caller": self.name,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "result": result,
        }


def test_routed_tool_caller_falls_back_after_invalid_primary_response() -> None:
    empty = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw={},
    )
    valid = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="最终结果",
        tool_calls=[],
        raw={},
    )
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[_FakeCaller("primary", [empty])],
        fallback_caller=_FakeCaller("fallback", [valid]),
        logger=None,
    )

    response = asyncio.run(routed.chat_with_tools([], [], False))

    assert response.content == "最终结果"


def test_routed_tool_caller_routes_tool_result_back_to_originating_caller() -> None:
    response = tool_impl.ToolCallerResponse(
        finish_reason="tool_calls",
        content="",
        tool_calls=[SimpleNamespace(id="call-1")],
        raw={},
    )
    primary = _FakeCaller("primary", [response])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=None,
        logger=None,
    )

    asyncio.run(routed.chat_with_tools([], [], False))
    tool_result = routed.build_tool_result_message("call-1", "web_search", "done")

    assert tool_result["caller"] == "primary"
    assert tool_result["tool_name"] == "web_search"


def test_routed_tool_caller_pins_synthetic_tool_result_to_same_caller() -> None:
    empty = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw={},
    )
    first_valid = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="需要补查",
        tool_calls=[],
        raw={},
    )
    final_valid = tool_impl.ToolCallerResponse(
        finish_reason="stop",
        content="最终结果",
        tool_calls=[],
        raw={},
    )
    primary = _FakeCaller("primary", [empty, AssertionError("primary should not see fallback-shaped tool result")])
    fallback = _FakeCaller("fallback", [first_valid, final_valid])
    routed = ai_routes.RoutedToolCaller(
        primary_callers=[primary],
        fallback_caller=fallback,
        logger=None,
    )

    response = asyncio.run(routed.chat_with_tools([], [], False))
    assert response.content == "需要补查"

    tool_result = routed.build_tool_result_message("fallback-wiki_lookup-1", "wiki_lookup", "查到了")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "fallback-wiki_lookup-1",
                    "type": "function",
                    "function": {"name": "wiki_lookup", "arguments": "{}"},
                }
            ],
        },
        tool_result,
    ]
    response = asyncio.run(routed.chat_with_tools(messages, [], False))

    assert tool_result["caller"] == "fallback"
    assert "_personification_routed_caller" in tool_result
    assert all("_personification_routed_caller" not in msg for msg in fallback.messages_seen[-1])
    assert response.content == "最终结果"
