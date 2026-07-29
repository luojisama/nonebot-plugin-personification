from __future__ import annotations

import asyncio

from plugin.personification.skills.skillpacks.tool_caller.scripts import impl


def _response(provider_history):  # noqa: ANN001, ANN201
    return impl.ToolCallerResponse(
        finish_reason="tool_calls",
        content="",
        tool_calls=[impl.ToolCall(id="call-1", name="lookup", arguments={})],
        raw={},
        provider_history=provider_history,
    )


def test_anthropic_preserves_signed_thinking_blocks_before_tool_result() -> None:
    native_blocks = [
        {"type": "thinking", "thinking": "opaque", "signature": "signed-thinking"},
        {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
    ]
    caller = impl.AnthropicToolCaller(api_key="key", base_url="", model="claude-test")
    response = _response(native_blocks)
    messages = [
        caller.build_assistant_tool_calls_message(response),
        caller.build_tool_result_message("call-1", "lookup", "result"),
    ]

    _system, converted = impl._convert_messages_to_anthropic(messages)

    assert converted[0] == {"role": "assistant", "content": native_blocks}
    assert converted[1]["content"][0]["tool_use_id"] == "call-1"


def test_openai_responses_preserves_reasoning_and_function_items() -> None:
    native_items = [
        {"type": "reasoning", "id": "reasoning-1", "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": "{}",
        },
    ]
    caller = impl.OpenAIToolCaller(api_key="key", base_url="", model="gpt-5")
    response = _response(native_items)
    messages = [
        caller.build_assistant_tool_calls_message(response),
        caller.build_tool_result_message("call-1", "lookup", "result"),
    ]

    _instructions, input_items = impl._openai_responses_input(messages)

    assert input_items[:2] == native_items
    assert input_items[2] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "result",
    }


def test_codex_stateless_request_preserves_encrypted_reasoning_for_continuation() -> None:
    native_items = [
        {"type": "reasoning", "id": "reasoning-1", "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": "{}",
        },
    ]
    captured: list[dict] = []
    responses = [
        {"output": native_items},
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "final"}],
                }
            ]
        },
    ]

    class _Caller(impl.OpenAICodexToolCaller):
        async def _request_codex_response(self, payload, **_kwargs):  # noqa: ANN001, ANN202
            captured.append(payload)
            return responses.pop(0)

    caller = _Caller(model="gpt-5-codex")
    messages = [{"role": "user", "content": "lookup"}]
    first = asyncio.run(caller.chat_with_tools(messages, [], False))
    messages.append(caller.build_assistant_tool_calls_message(first))
    messages.append(caller.build_tool_result_message("call-1", "lookup", "result"))
    second = asyncio.run(caller.chat_with_tools(messages, [], False))

    assert second.content == "final"
    assert captured[0]["store"] is False
    assert captured[0]["include"] == ["reasoning.encrypted_content"]
    assert captured[1]["input"][1:3] == native_items
    assert captured[1]["input"][3] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "result",
    }
