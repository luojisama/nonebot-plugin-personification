from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

tool_loop = load_personification_module("plugin.personification.agent.runtime.tool_loop")


class _Logger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message: str) -> None:
        self.infos.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class _ToolCaller:
    def build_tool_result_message(self, tool_call_id: str, tool_name: str, result: str) -> dict[str, str]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        }


def test_append_assistant_tool_calls_message_uses_empty_string_content() -> None:
    messages: list[dict] = []
    response = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                name="search_web",
                arguments={"query": "天气"},
            )
        ],
    )

    tool_loop.append_assistant_tool_calls_message(messages=messages, response=response)

    assert messages == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "arguments": '{"query": "天气"}',
                    },
                }
            ],
        }
    ]


def test_append_single_tool_call_exchange_adds_assistant_and_tool_result() -> None:
    messages: list[dict] = []

    tool_loop.append_single_tool_call_exchange(
        messages=messages,
        tool_caller=_ToolCaller(),
        call_id="fallback-search-1",
        tool_name="search_web",
        tool_args={"query": "AI 新闻"},
        result="查到一条",
    )

    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "search_web"
    assert messages[1] == {
        "role": "tool",
        "tool_call_id": "fallback-search-1",
        "name": "search_web",
        "content": "查到一条",
    }


def test_response_aware_builders_keep_parallel_results_in_one_turn() -> None:
    messages: list[dict] = []
    calls = [
        SimpleNamespace(id="call-1", name="first", arguments={}),
        SimpleNamespace(id="call-2", name="second", arguments={}),
    ]
    response = SimpleNamespace(content="", tool_calls=calls)

    class _TurnCaller(_ToolCaller):
        def build_assistant_tool_calls_message(self, _response):  # noqa: ANN001, ANN201
            return {"role": "model", "parts": [{"functionCall": {"name": "first"}}]}

        def build_tool_result_messages(self, _response, results):  # noqa: ANN001, ANN201
            return [
                {
                    "role": "user",
                    "parts": [
                        {"functionResponse": {"name": call.name, "response": {"result": result}}}
                        for call, result in results
                    ],
                }
            ]

    caller = _TurnCaller()
    tool_loop.append_assistant_tool_calls_message(
        messages=messages,
        response=response,
        tool_caller=caller,
    )
    tool_loop.append_tool_result_messages(
        messages=messages,
        tool_caller=caller,
        response=response,
        results=[(calls[0], "one"), (calls[1], "two")],
    )

    assert len(messages) == 2
    assert messages[0]["role"] == "model"
    assert [part["functionResponse"]["name"] for part in messages[1]["parts"]] == [
        "first",
        "second",
    ]


def test_observe_model_step_records_trace_and_warns_empty_stop() -> None:
    traces: list[dict] = []
    logger = _Logger()
    response = SimpleNamespace(
        finish_reason="stop",
        content="",
        tool_calls=[],
        usage={},
        raw={"status": "completed", "model": "fake", "output": [], "usage": {"output_tokens": 0}},
    )

    content_len = tool_loop.observe_model_step(
        response=response,
        tool_caller=SimpleNamespace(),
        logger=logger,
        step=2,
        selected_names=["search_web"],
        runtime_chat_intent="lookup",
        model_elapsed_ms=123,
        record_trace=lambda **kwargs: traces.append(kwargs),
    )

    assert content_len == 0
    assert traces[0]["key"] == "agent_model_step"
    assert traces[0]["status"] == "warn"
    assert "tool_calls=0" in traces[0]["detail"]
    assert logger.warnings
    assert "provider returned empty stop response" in logger.warnings[0]
