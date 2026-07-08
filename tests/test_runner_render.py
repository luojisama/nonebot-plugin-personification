from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

from ._loader import load_personification_module

runner = load_personification_module("plugin.personification.agent.runtime.runner")
metrics = load_personification_module("plugin.personification.core.metrics")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")
tool_impl = load_personification_module("plugin.personification.skills.skillpacks.tool_caller.scripts.impl")


class _FakeLogger:
    def __init__(self) -> None:
        self.warning_messages: list[str] = []

    def info(self, *_args, **_kwargs) -> None:
        return

    def debug(self, *_args, **_kwargs) -> None:
        return

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


class _FakeToolCaller:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "use_builtin_search": use_builtin_search,
            }
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def build_tool_result_message(self, tool_call_id: str, tool_name: str, result: str) -> dict[str, str]:
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "result": result,
        }


def _register_query_tool(handler):  # noqa: ANN001
    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="search_web",
            description="",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": [],
            },
            handler=handler,
        )
    )
    return registry


def test_render_tool_result_for_user_returns_no_result_signal_for_empty_text() -> None:
    payload = json.loads(runner._render_tool_result_for_user("web_search", "", "  最新新闻 "))
    assert payload == {"status": "no_result", "query": "最新新闻"}


def test_render_tool_result_for_user_returns_no_result_signal_for_empty_results() -> None:
    payload = json.loads(
        runner._render_tool_result_for_user(
            "web_search",
            json.dumps({"query": "天气", "results": []}, ensure_ascii=False),
            "天气",
        )
    )
    assert payload == {"status": "no_result", "query": "天气"}


def test_maybe_inject_date_to_query_handles_time_sensitive_queries(monkeypatch) -> None:
    monkeypatch.setattr(runner, "get_configured_now", lambda: datetime(2026, 4, 22, 9, 0, 0))

    injected = runner._maybe_inject_date_to_query("web_search", {"query": "最新天气"})
    untouched = runner._maybe_inject_date_to_query("web_search", {"query": "天气怎么样"})

    assert injected["query"] == "最新天气 (2026年4月22日)"
    assert untouched["query"] == "天气怎么样"


def test_execute_tool_with_retries_records_success_metrics() -> None:
    async def _handler(**kwargs):  # noqa: ANN001
        assert kwargs["query"] == "天气"
        return "ok"

    logger = _FakeLogger()
    metrics.reset_metrics()
    try:
        tool_args, result = asyncio.run(
            runner._execute_tool_with_retries(
                registry=_register_query_tool(_handler),
                tool_name="search_web",
                tool_args={"query": "天气"},
                rewritten_query=None,
                user_images=[],
                logger=logger,
            )
        )
        snapshot = metrics.snapshot_metrics()
        counter_map = {item["name"]: item["value"] for item in snapshot["counters"]}
        timing_names = {item["name"] for item in snapshot["timings"]}

        assert tool_args["query"] == "天气"
        assert result == "ok"
        assert counter_map["agent.tool_ok_total{tool=search_web}"] == 1
        assert "agent.tool_exec_ms{status=ok,tool=search_web}" in timing_names
    finally:
        metrics.reset_metrics()


def test_execute_tool_with_retries_records_failure_metrics() -> None:
    async def _handler(**_kwargs):  # noqa: ANN001
        raise RuntimeError("boom")

    logger = _FakeLogger()
    metrics.reset_metrics()
    try:
        _tool_args, result = asyncio.run(
            runner._execute_tool_with_retries(
                registry=_register_query_tool(_handler),
                tool_name="search_web",
                tool_args={"query": "天气"},
                rewritten_query=None,
                user_images=[],
                logger=logger,
            )
        )
        snapshot = metrics.snapshot_metrics()
        counter_map = {item["name"]: item["value"] for item in snapshot["counters"]}
        timing_names = {item["name"] for item in snapshot["timings"]}

        assert result == "工具调用失败：boom"
        assert counter_map["agent.tool_fail_total{reason=exception,tool=search_web}"] == 1
        assert "agent.tool_exec_ms{status=fail,tool=search_web}" in timing_names
        assert logger.warning_messages
    finally:
        metrics.reset_metrics()


def test_execute_tool_with_retries_records_timeout_metrics() -> None:
    async def _handler(**_kwargs):  # noqa: ANN001
        await asyncio.sleep(0.1)
        return "too late"

    logger = _FakeLogger()
    metrics.reset_metrics()
    try:
        _tool_args, result = asyncio.run(
            runner._execute_tool_with_retries(
                registry=_register_query_tool(_handler),
                tool_name="search_web",
                tool_args={"query": "天气"},
                rewritten_query=None,
                user_images=[],
                logger=logger,
                budget_deadline=time.monotonic() + 0.03,
            )
        )
        snapshot = metrics.snapshot_metrics()
        counter_map = {item["name"]: item["value"] for item in snapshot["counters"]}
        timing_names = {item["name"] for item in snapshot["timings"]}

        assert result == "工具调用失败：超时"
        assert counter_map["agent.tool_fail_total{reason=timeout,tool=search_web}"] == 1
        assert "agent.tool_exec_ms{status=timeout,tool=search_web}" in timing_names
        assert logger.warning_messages
    finally:
        metrics.reset_metrics()


def test_wrap_tool_result_in_persona_rewrites_search_style_output() -> None:
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="这事儿现在一般还是找靠谱代购群问得快。",
                tool_calls=[],
                raw={},
            )
        ]
    )

    wrapped = asyncio.run(
        runner._wrap_tool_result_in_persona(
            tool_caller=caller,
            rendered_tool_result="先给你整理 2 条「代购」参考：\n1. xxx\nhttps://example.com/abc",
            user_query_text="代购在哪里找啊",
            persona_system="你是群里的活人，说话自然点。" * 30,
        )
    )

    assert wrapped == "这事儿现在一般还是找靠谱代购群问得快。"
    assert len(caller.calls) == 1
    assert caller.calls[0]["tools"] == []


def test_run_agent_returns_immediately_for_banter_stop_without_tools(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        runner,
        "_infer_intent_decision_with_context",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=SimpleNamespace(
            chat_intent="banter",
            plugin_question_intent="capability",
            ambiguity_level="low",
        )),
    )

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="这种一般还是看你们那边群里路子。",
                tool_calls=[],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[
                {"role": "system", "content": "你是群里的正常成员。" * 80},
                {"role": "user", "content": "代购在哪里找啊"},
            ],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=1,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
        )
    )

    assert result.text == "这种一般还是看你们那边群里路子。"
    assert result.direct_output is False
    assert result.bypass_length_limits is False
    assert len(caller.calls) == 1


def test_run_agent_final_quality_rewrites_observer_reply(monkeypatch) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []

    def _capture_stage(**kwargs):  # noqa: ANN001
        stages.append(dict(kwargs))

    monkeypatch.setattr(runner, "_record_reply_trace_stage", _capture_stage)

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="我先看看情况，等会再说",
                tool_calls=[],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="那先别绕远，卡在哪个点了",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[
                {"role": "system", "content": "你是群里的正常成员。" * 80},
                {"role": "user", "content": "这题写不动了"},
            ],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=1,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
    )

    quality_stage = next(stage for stage in stages if stage["key"] == "agent_reply_quality")
    assert result.text == "那先别绕远，卡在哪个点了"
    assert result.quality_checks[-1]["action"] == "rewritten"
    assert len(caller.calls) == 2
    assert caller.calls[1]["tools"] == []
    assert "action=rewritten" in quality_stage["detail"]


def test_run_agent_final_quality_silences_unfixed_ooc_reply() -> None:
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="根据搜索结果，我先看看情况，等会再说",
                tool_calls=[],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="我先看看情况，等会再说",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "明天会下雨吗"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=1,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="",
                ambiguity_level="medium",
            ),
        )
    )

    assert result.text == "[SILENCE]"
    assert result.quality_checks[-1]["action"] == "silenced"
    assert len(caller.calls) == 2


def test_run_agent_retries_lookup_when_banter_draft_asks_group_about_unknown_term(monkeypatch) -> None:  # noqa: ANN001
    handled: list[dict[str, object]] = []

    async def _handler(**kwargs):  # noqa: ANN001
        handled.append(dict(kwargs))
        assert kwargs["query"] == "猎鹰阿努比斯藏东西了"
        return "检索摘要：疑似游戏/梗相关实体，语境里是在说藏东西。"

    async def _fallback_lookup(**kwargs):  # noqa: ANN001
        assert kwargs["chat_intent"] == "banter"
        assert kwargs["user_query_text"] == "猎鹰阿努比斯藏东西了"
        assert kwargs["draft_answer_text"] == "猎鹰阿努比斯？这是哪个游戏的梗啊"
        return "search_web", {"query": "猎鹰阿努比斯藏东西了"}

    monkeypatch.setattr(runner, "_select_semantic_fallback_tool", _fallback_lookup)

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="猎鹰阿努比斯？这是哪个游戏的梗啊",
                tool_calls=[],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="RETRY_SEARCH",
                tool_calls=[],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="它还真会藏东西啊。",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[
                {"role": "system", "content": "你是群里的正常成员。" * 80},
                {"role": "user", "content": "猎鹰阿努比斯藏东西了"},
            ],
            registry=_register_query_tool(_handler),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=3,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="capability",
                ambiguity_level="high",
            ),
        )
    )

    assert result.text == "它还真会藏东西啊。"
    assert handled == [{"query": "猎鹰阿努比斯藏东西了"}]
    assert len(caller.calls) == 3
    assert caller.calls[1]["tools"] == []
    assert "候选回复" in caller.calls[1]["messages"][1]["content"]


def test_run_agent_reviews_high_ambiguity_banter_even_without_question_mark(monkeypatch) -> None:  # noqa: ANN001
    handled: list[dict[str, object]] = []

    async def _handler(**kwargs):  # noqa: ANN001
        handled.append(dict(kwargs))
        return "检索摘要：这是某个新梗的上下文。"

    async def _fallback_lookup(**kwargs):  # noqa: ANN001
        assert kwargs["chat_intent"] == "banter"
        assert kwargs["draft_answer_text"] == "这肯定是在说老版本活动"
        return "search_web", {"query": "高歧义新梗"}

    monkeypatch.setattr(runner, "_select_semantic_fallback_tool", _fallback_lookup)

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="这肯定是在说老版本活动",
                tool_calls=[],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="RETRY_SEARCH",
                tool_calls=[],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="原来是这个梗啊。",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[
                {"role": "system", "content": "你是群里的正常成员。" * 80},
                {"role": "user", "content": "高歧义新梗"},
            ],
            registry=_register_query_tool(_handler),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=3,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="capability",
                ambiguity_level="high",
            ),
        )
    )

    assert result.text == "原来是这个梗啊。"
    assert handled == [{"query": "高歧义新梗"}]
    assert caller.calls[1]["tools"] == []


def test_run_agent_uses_precomputed_intent_without_reinferring(monkeypatch) -> None:  # noqa: ANN001
    async def _should_not_run(*_args, **_kwargs):  # noqa: ANN001
        raise AssertionError("intent inference should be skipped")

    monkeypatch.setattr(runner, "_infer_intent_decision_with_context", _should_not_run)

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="直接复用外层意图了。",
                tool_calls=[],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我看看这个"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=1,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
        )
    )

    assert result.text == "直接复用外层意图了。"
    assert len(caller.calls) == 1


def test_run_agent_records_budget_profile_without_enforcing_it(monkeypatch) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []

    def _capture_stage(**kwargs):  # noqa: ANN001
        stages.append(dict(kwargs))

    monkeypatch.setattr(runner, "_record_reply_trace_stage", _capture_stage)

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="直接接一句就行。",
                tool_calls=[],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "这局又寄了"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=10,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
            turn_plan=SimpleNamespace(
                reply_action="reply",
                speech_act="participate",
                research_need="none",
                output_mode="chat_short",
                tool_intent=["none"],
            ),
            time_budget_seconds=150,
        )
    )

    budget_stage = next(stage for stage in stages if stage["key"] == "agent_budget")
    assert "budget=light_chat" in budget_stage["detail"]
    assert "suggested_steps=2" in budget_stage["detail"]
    assert "actual_steps=10" in budget_stage["detail"]
    assert result.text == "直接接一句就行。"
    assert len(caller.calls) == 1


def test_run_agent_returns_no_reply_when_time_budget_is_exhausted() -> None:
    caller = _FakeToolCaller([])

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我查一下"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
            time_budget_seconds=0.0,
        )
    )

    assert result.text == "[NO_REPLY]"
    assert caller.calls == []


def test_run_agent_uses_tool_metadata_contract_for_queued_action_silence() -> None:
    async def _handler(**_kwargs):  # noqa: ANN001
        return json.dumps({"ok": True, "queued": True, "kind": "custom_action"}, ensure_ascii=False)

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="send_custom_expression",
            description="send custom expression",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_handler,
            metadata={
                "intent_tags": ["expression"],
                "evidence_kind": "action",
                "side_effect": "send_message",
                "final_behavior": "silence_on_success",
            },
        )
    )
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-custom",
                        name="send_custom_expression",
                        arguments={},
                    )
                ],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "发个表情"}],
            registry=registry,
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="expression",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
    )

    assert result.text == "[SILENCE]"
    assert len(caller.calls) == 1


def test_run_agent_sends_ack_when_first_tool_call_appears(monkeypatch) -> None:  # noqa: ANN001
    ack_calls: list[str] = []

    async def _handler(**kwargs):  # noqa: ANN001
        assert kwargs["query"] == "热搜"
        return "工具结果"

    async def _ack_sender(text: str) -> None:
        ack_calls.append(text)

    monkeypatch.setattr(
        runner,
        "_select_semantic_fallback_tool",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
    )

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-1",
                        name="search_web",
                        arguments={"query": "热搜"},
                    )
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="查到了。",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我看看热搜"}],
            registry=_register_query_tool(_handler),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
                logger=_FakeLogger(),
                precomputed_intent=SimpleNamespace(
                    chat_intent="banter",
                    plugin_question_intent="capability",
                    ambiguity_level="low",
                ),
            ack_sender=_ack_sender,
        )
    )

    assert result.text == "查到了。"
    assert ack_calls == [""]


def test_run_agent_appends_evidence_guidance_after_tool_result(monkeypatch) -> None:  # noqa: ANN001
    handled: list[dict[str, object]] = []

    async def _handler(**kwargs):  # noqa: ANN001
        handled.append(dict(kwargs))
        assert "热搜" in kwargs["query"]
        return "热搜结果：A"

    monkeypatch.setattr(runner, "_evidence_synthesizer_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        runner,
        "_select_semantic_fallback_tool",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
    )
    synth_calls: list[dict[str, object]] = []

    async def _fake_synthesize_evidence(**kwargs):  # noqa: ANN001
        synth_calls.append(dict(kwargs))
        return runner.EvidenceSynthesis(
            selected_memory_ids=["m1"],
            memory_inject_style="factual",
            tool_evidence_digest="A 是当前可用结果",
            uncertainty_notes=[],
            needs_more_research=False,
            research_followup_query="",
        )

    monkeypatch.setattr(runner, "synthesize_evidence_with_llm", _fake_synthesize_evidence)

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-1",
                        name="search_web",
                        arguments={"query": "热搜"},
                    )
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="A 现在比较热。",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我看看热搜"}],
            registry=_register_query_tool(_handler),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=3,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
                personification_evidence_synthesizer_enabled=True,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
            candidate_memories=[{"memory_id": "m1", "summary": "以前聊过 A"}],
        )
    )

    assert result.text == "A 现在比较热。"
    assert len(caller.calls) == 2
    assert handled == [{"query": "帮我看看热搜"}]
    assert synth_calls
    assert synth_calls[0]["candidate_memories"] == [{"memory_id": "m1", "summary": "以前聊过 A"}]
    assert synth_calls[0]["tool_results"][0]["tool_name"] == "search_web"
    final_messages = caller.calls[1]["messages"]
    assert any(
        "证据综合器给出的当前可用证据" in str(message.get("content", ""))
        and "A 是当前可用结果" in str(message.get("content", ""))
        for message in final_messages
        if isinstance(message, dict)
    )


def test_run_agent_returns_generated_image_marker_without_rewrite(monkeypatch) -> None:  # noqa: ANN001
    async def _handler(**kwargs):  # noqa: ANN001
        assert kwargs["prompt"] == "Kobe Bryant iced tea poster"
        return "[IMAGE_B64]QUJD[/IMAGE_B64]"

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="generate_image",
            description="generate image",
            parameters={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
            handler=_handler,
        )
    )
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-image",
                        name="generate_image",
                        arguments={"prompt": "Kobe Bryant iced tea poster"},
                    )
                ],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我生成一张海报"}],
            registry=registry,
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="image_generation",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
    )

    assert result.text == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert result.bypass_length_limits is True
    assert len(caller.calls) == 1


def test_run_agent_returns_image_generation_failure_without_rewrite(monkeypatch) -> None:  # noqa: ANN001
    async def _handler(**kwargs):  # noqa: ANN001
        assert kwargs["prompt"] == "Kobe Bryant iced tea poster"
        return (
            "图片生成失败：empty image response "
            "(status=failed raw_keys=id,error output_items=1 output_types=image_generation_call)"
        )

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="generate_image",
            description="generate image",
            parameters={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
            handler=_handler,
        )
    )
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-image",
                        name="generate_image",
                        arguments={"prompt": "Kobe Bryant iced tea poster"},
                    )
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="好了，海报给你。",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我生成一张海报"}],
            registry=registry,
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="image_generation",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
    )

    assert result.text == "图片生成失败：图片服务没有返回图片数据"
    assert result.bypass_length_limits is False
    assert len(caller.calls) == 1


def test_run_agent_returns_image_generation_timeout_without_rewrite(monkeypatch) -> None:  # noqa: ANN001
    async def _handler(**kwargs):  # noqa: ANN001
        assert kwargs["prompt"] == "Kobe Bryant iced tea poster"
        await asyncio.sleep(0.2)
        return "[IMAGE_B64]QUJD[/IMAGE_B64]"

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="generate_image",
            description="generate image",
            parameters={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
            handler=_handler,
        )
    )
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-image",
                        name="generate_image",
                        arguments={"prompt": "Kobe Bryant iced tea poster"},
                    )
                ],
                raw={},
            )
        ]
    )

    started_at = time.monotonic()
    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我生成一张海报"}],
            registry=registry,
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="image_generation",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
            time_budget_seconds=0.05,
        )
    )
    elapsed = time.monotonic() - started_at

    assert result.text == "图片生成失败：生成超时，请稍后重试"
    assert result.bypass_length_limits is False
    assert len(caller.calls) == 1
    assert elapsed < 0.18


def test_run_agent_starts_background_image_generation_for_send_capable_executor(monkeypatch) -> None:  # noqa: ANN001
    sent_texts: list[str] = []
    sent_images: list[str] = []

    async def _handler(**kwargs):  # noqa: ANN001
        assert kwargs["prompt"] == "Kobe Bryant iced tea poster, energetic commercial key visual"
        assert kwargs["size"] == "1024x1536"
        await asyncio.sleep(0)
        return "[IMAGE_B64]QUJD[/IMAGE_B64]"

    class _Executor:
        async def execute(self, *_args, **_kwargs):  # noqa: ANN001
            return ""

        async def send_text(self, text: str) -> None:
            sent_texts.append(text)

        async def send_image_b64(self, image_b64: str) -> None:
            sent_images.append(image_b64)

    async def _run() -> object:
        registry = tool_registry.ToolRegistry()
        registry.register(
            tool_registry.AgentTool(
                name="generate_image",
                description="generate image",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "size": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
                handler=_handler,
            )
        )
        caller = _FakeToolCaller(
            [
                tool_impl.ToolCallerResponse(
                    finish_reason="stop",
                    content="我先按这个感觉画一版",
                    tool_calls=[],
                    raw={},
                ),
                tool_impl.ToolCallerResponse(
                    finish_reason="tool_calls",
                    content="",
                    tool_calls=[
                        tool_impl.ToolCall(
                            id="call-image",
                            name="generate_image",
                            arguments={
                                "prompt": "Kobe Bryant iced tea poster, energetic commercial key visual",
                                "size": "1024x1536",
                            },
                        )
                    ],
                    raw={},
                ),
            ]
        )
        result = await runner.run_agent(
            messages=[{"role": "user", "content": "帮我生成一张海报"}],
            registry=registry,
            tool_caller=caller,
            executor=_Executor(),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
                personification_image_gen_background_enabled=True,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="image_generation",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
        await asyncio.sleep(0.01)
        return result, caller

    result, caller = asyncio.run(_run())

    assert result.text == "我先按这个感觉画一版"
    assert sent_texts == []
    assert sent_images == ["QUJD"]
    assert len(caller.calls) == 2


def test_background_image_generation_can_use_reference_search_before_generate(monkeypatch) -> None:  # noqa: ANN001
    sent_images: list[str] = []
    search_calls: list[dict[str, object]] = []

    async def _search_handler(**kwargs):  # noqa: ANN001
        search_calls.append(dict(kwargs))
        return "reference result: red iced tea bottle, basketball advertising poster"

    async def _image_handler(**kwargs):  # noqa: ANN001
        assert "reference result" in kwargs["prompt"]
        assert kwargs["size"] == "1024x1536"
        return "[IMAGE_B64]QUJD[/IMAGE_B64]"

    class _Executor:
        async def execute(self, *_args, **_kwargs):  # noqa: ANN001
            return ""

        async def send_text(self, _text: str) -> None:
            return

        async def send_image_b64(self, image_b64: str) -> None:
            sent_images.append(image_b64)

    async def _run() -> object:
        registry = tool_registry.ToolRegistry()
        registry.register(
            tool_registry.AgentTool(
                name="search_images",
                description="search images",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                handler=_search_handler,
            )
        )
        registry.register(
            tool_registry.AgentTool(
                name="generate_image",
                description="generate image",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "size": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
                handler=_image_handler,
            )
        )
        caller = _FakeToolCaller(
            [
                tool_impl.ToolCallerResponse(
                    finish_reason="stop",
                    content="我先找点参考再画",
                    tool_calls=[],
                    raw={},
                ),
                tool_impl.ToolCallerResponse(
                    finish_reason="tool_calls",
                    content="",
                    tool_calls=[
                        tool_impl.ToolCall(
                            id="call-search",
                            name="search_images",
                            arguments={"query": "iced tea basketball poster reference", "limit": 3},
                        )
                    ],
                    raw={},
                ),
                tool_impl.ToolCallerResponse(
                    finish_reason="tool_calls",
                    content="",
                    tool_calls=[
                        tool_impl.ToolCall(
                            id="call-image",
                            name="generate_image",
                            arguments={
                                "prompt": (
                                    "Use reference result: red iced tea bottle, "
                                    "basketball advertising poster. Build a dramatic vertical poster."
                                ),
                                "size": "1024x1536",
                            },
                        )
                    ],
                    raw={},
                ),
            ]
        )
        result = await runner.run_agent(
            messages=[{"role": "user", "content": "帮我生成一张篮球冰红茶广告海报"}],
            registry=registry,
            tool_caller=caller,
            executor=_Executor(),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
                personification_image_gen_background_enabled=True,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="image_generation",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
        await asyncio.sleep(0.01)
        return result, caller

    result, caller = asyncio.run(_run())

    assert result.text == "我先找点参考再画"
    assert search_calls == [{"query": "iced tea basketball poster reference", "limit": 3}]
    assert sent_images == ["QUJD"]
    assert len(caller.calls) == 3


def test_codex_image_generation_summary_includes_error_detail() -> None:
    summary = tool_impl.OpenAICodexToolCaller._summarize_image_generation_response(
        {
            "status": "failed",
            "error": {"code": "policy_violation", "message": "request blocked"},
            "output": [
                {
                    "type": "image_generation_call",
                    "error": {"message": "image generation failed"},
                }
            ],
        }
    )

    assert "status=failed" in summary
    assert "output_types=image_generation_call" in summary
    assert "error=policy_violation: request blocked" in summary
    assert "image_generation_call_error=image generation failed" in summary


def test_codex_image_generation_request_forces_hosted_tool() -> None:
    image_b64 = "QUJD" * 44

    class _CaptureCodexCaller(tool_impl.OpenAICodexToolCaller):
        def __init__(self) -> None:
            super().__init__(model="gpt-5.3-codex")
            self.payloads: list[dict[str, object]] = []

        async def _get_access_token(self):  # noqa: ANN001
            return "token", None

        async def _request_codex_response(self, payload, **_kwargs):  # noqa: ANN001
            self.payloads.append(payload)
            return {"output": [{"type": "image_generation_call", "result": image_b64}]}

    caller = _CaptureCodexCaller()

    result = asyncio.run(caller.generate_image("poster", size="1792x1024"))

    assert result == {"b64_json": image_b64}
    assert len(caller.payloads) == 1
    payload = caller.payloads[0]
    assert payload["model"] == "gpt-5.3-codex"
    assert payload["tool_choice"] == {"type": "image_generation"}
    assert payload["tools"] == [
        {
            "type": "image_generation",
            "model": "gpt-image-2",
            "size": "1536x1024",
            "quality": "auto",
            "action": "generate",
        }
    ]


def test_codex_image_generation_size_alias_updates_tool_size_and_prompt_hint() -> None:
    image_b64 = "QUJD" * 44

    class _CaptureCodexCaller(tool_impl.OpenAICodexToolCaller):
        def __init__(self) -> None:
            super().__init__(model="gpt-5.3-codex")
            self.payloads: list[dict[str, object]] = []

        async def _get_access_token(self):  # noqa: ANN001
            return "token", None

        async def _request_codex_response(self, payload, **_kwargs):  # noqa: ANN001
            self.payloads.append(payload)
            return {"output": [{"type": "image_generation_call", "result": image_b64}]}

    caller = _CaptureCodexCaller()

    result = asyncio.run(caller.generate_image("poster", size="竖版"))

    assert result == {"b64_json": image_b64}
    payload = caller.payloads[0]
    assert payload["tools"][0]["size"] == "1024x1536"
    user_text = payload["input"][0]["content"][0]["text"]
    assert "Requested aspect/size: portrait (竖版)." in user_text


def test_codex_image_generation_passes_reference_images_as_input_image() -> None:
    image_b64 = "QUJD" * 44

    class _CaptureCodexCaller(tool_impl.OpenAICodexToolCaller):
        def __init__(self) -> None:
            super().__init__(model="gpt-5.3-codex")
            self.payloads: list[dict[str, object]] = []

        async def _get_access_token(self):  # noqa: ANN001
            return "token", None

        async def _request_codex_response(self, payload, **_kwargs):  # noqa: ANN001
            self.payloads.append(payload)
            return {"output": [{"type": "image_generation_call", "result": image_b64}]}

    caller = _CaptureCodexCaller()

    result = asyncio.run(
        caller.generate_image(
            "按参考图画海报",
            size="1024x1024",
            images=["https://example.com/ref.png"],
            reference_mode="input_image",
        )
    )

    assert result == {"b64_json": image_b64}
    content = caller.payloads[0]["input"][0]["content"]
    assert any(part.get("type") == "input_image" and part.get("image_url") == "https://example.com/ref.png" for part in content)


def test_codex_image_generation_auto_reference_falls_back_to_text_prompt() -> None:
    image_b64 = "QUJD" * 44

    class _CaptureCodexCaller(tool_impl.OpenAICodexToolCaller):
        def __init__(self) -> None:
            super().__init__(model="gpt-5.3-codex")
            self.payloads: list[dict[str, object]] = []

        async def _get_access_token(self):  # noqa: ANN001
            return "token", None

        async def _request_codex_response(self, payload, **_kwargs):  # noqa: ANN001
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                raise RuntimeError("image input not supported")
            return {"output": [{"type": "image_generation_call", "result": image_b64}]}

    caller = _CaptureCodexCaller()

    result = asyncio.run(
        caller.generate_image(
            "按参考图画海报",
            images=["https://example.com/ref.png"],
            reference_mode="auto",
        )
    )

    assert result["b64_json"] == image_b64
    assert "reference images were rejected" in result["warning"]
    fallback_content = caller.payloads[1]["input"][0]["content"][0]["text"]
    assert "Reference image mode: unavailable" in fallback_content
