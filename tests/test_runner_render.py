from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module

runner = load_personification_module("plugin.personification.agent.runtime.runner")
final_synthesis = load_personification_module("plugin.personification.agent.runtime.final_synthesis")
reply_quality = load_personification_module("plugin.personification.agent.runtime.reply_quality")
fallbacks = load_personification_module("plugin.personification.agent.runtime.fallbacks")
image_generation = load_personification_module("plugin.personification.agent.runtime.image_generation")
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


def test_structured_memory_outcome_distinguishes_usable_and_empty() -> None:
    usable = json.dumps({"query": "上次", "memories": [{"summary": "聊过这件事"}]}, ensure_ascii=False)
    empty = json.dumps({"query": "上次", "memories": []}, ensure_ascii=False)

    assert fallbacks.tool_result_outcome(usable) == "usable_evidence"
    assert fallbacks.tool_result_outcome(empty) == "empty_evidence"


def test_structured_visual_and_avatar_outcomes() -> None:
    visual = json.dumps(
        {"scene_summary": "一张游戏截图", "visual_evidence": [{"index": 1}]},
        ensure_ascii=False,
    )
    avatar = json.dumps(
        {"ok": True, "available": True, "analysis": {"style": "插画"}, "insight": {}},
        ensure_ascii=False,
    )
    unavailable_avatar = json.dumps(
        {"ok": True, "available": False, "analysis": {}, "insight": {}},
        ensure_ascii=False,
    )

    assert fallbacks.tool_result_outcome(visual) == "usable_evidence"
    assert fallbacks.tool_result_outcome(avatar) == "usable_evidence"
    assert fallbacks.tool_result_outcome(unavailable_avatar) == "empty_evidence"
    assert runner._render_tool_result_for_user("vision_analyze", visual, "看图") == visual
    assert runner._render_tool_result_for_user("inspect_current_user_avatar", avatar, "头像") == avatar


def test_render_tool_result_preserves_structured_top_candidates() -> None:
    raw = json.dumps(
        {
            "top_candidates": [{"name": "候选角色", "confidence": 0.9}],
            "recommended_interpretation": "候选角色",
        },
        ensure_ascii=False,
    )

    assert runner._render_tool_result_for_user("resolve_acg_entity", raw, "这个角色") == raw


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


def test_execute_tool_with_retries_skips_failed_signatures_and_uses_new_query() -> None:
    calls: list[str] = []

    async def _handler(**kwargs):  # noqa: ANN001
        query = kwargs["query"]
        calls.append(query)
        if query == "新问题":
            return json.dumps({"results": []})
        return json.dumps({"results": [{"title": "找到结果"}]})

    unavailable = {fallbacks.tool_signature("search_web", {"query": "旧问题"})}
    tool_args, result = asyncio.run(
        runner._execute_tool_with_retries(
            registry=_register_query_tool(_handler),
            tool_name="search_web",
            tool_args={"query": "新问题"},
            rewritten_query=SimpleNamespace(
                primary_query="旧问题",
                query_candidates=["旧问题", "第三个问题"],
            ),
            user_images=[],
            unavailable_tool_signatures=unavailable,
            logger=_FakeLogger(),
        )
    )

    assert calls == ["新问题", "第三个问题"]
    assert tool_args == {"query": "第三个问题"}
    assert json.loads(result)["results"]
    assert fallbacks.tool_signature("search_web", {"query": "新问题"}) in unavailable


def test_execute_tool_with_retries_skips_zero_arg_signature_without_invoking() -> None:
    calls = 0

    async def _handler() -> str:
        nonlocal calls
        calls += 1
        return "不应执行"

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="get_ai_news",
            description="",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_handler,
        )
    )
    signature = fallbacks.tool_signature("get_ai_news", {})

    tool_args, result = asyncio.run(
        runner._execute_tool_with_retries(
            registry=registry,
            tool_name="get_ai_news",
            tool_args={},
            rewritten_query=None,
            user_images=[],
            unavailable_tool_signatures={signature},
            logger=_FakeLogger(),
        )
    )

    assert calls == 0
    assert tool_args == {}
    assert json.loads(result) == {"error": "no_results", "ok": False}


def test_run_agent_max_steps_uses_earlier_usable_result_after_empty_result() -> None:
    async def _search_handler(**_kwargs):  # noqa: ANN001
        return "真实检索结果"

    async def _empty_news_handler() -> str:
        return ""

    registry = _register_query_tool(_search_handler)
    registry.register(
        tool_registry.AgentTool(
            name="get_ai_news",
            description="",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_empty_news_handler,
        )
    )
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(id="call-search", name="search_web", arguments={"query": "问题"}),
                    tool_impl.ToolCall(id="call-news", name="get_ai_news", arguments={}),
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="按查到的情况看，确实是这样。",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "问题"}],
            registry=registry,
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

    assert result.text == "按查到的情况看，确实是这样。"
    assert len(caller.calls) == 2
    assert "真实检索结果" in str(caller.calls[1]["messages"])


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


def test_wrap_tool_result_in_persona_propagates_provider_failure() -> None:
    error = RuntimeError("private provider response")
    error.code = "provider_call_failed"
    caller = _FakeToolCaller([error])

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(runner._wrap_tool_result_in_persona(
            tool_caller=caller,
            rendered_tool_result='{"raw":"tool dump"}',
            user_query_text="查询",
        ))

    assert caught.value is error


def test_avatar_pair_constrained_review_rejects_real_relation_rewrite() -> None:
    envelope = {
        "type": "personification_evidence_envelope",
        "available": True,
        "allowed_claims": ["两张头像在构图、元素或风格上呈现视觉配套。"],
        "forbidden_inferences": ["不能据此判断两位用户现实中是情侣、朋友、认识或同一人。"],
        "confidence": 0.9,
        "natural_fallback": "两张头像在构图、元素或风格上呈现视觉配套。",
    }
    caller = _FakeToolCaller(
        [
            SimpleNamespace(content="他们现实里就是情侣"),
            SimpleNamespace(
                content=json.dumps(
                    {
                        "action": "fallback",
                        "text": "",
                        "claims_ok": False,
                        "inferences_ok": False,
                        "mechanism_hidden": True,
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )
    result = asyncio.run(
        reply_quality.finalize_agent_reply_quality(
            final_synthesis.AgentResult(
                text=envelope["natural_fallback"],
                pending_actions=[],
                quality_context="constrained_persona_output",
                evidence_envelope=envelope,
            ),
            tool_caller=caller,
            messages=[{"role": "system", "content": "你是普通群友。"}],
        )
    )
    assert "呈现视觉配套" in result.text
    assert "现实里就是情侣" not in result.text
    assert len(caller.calls) == 2


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


def test_run_agent_passes_required_interaction_and_current_text_to_quality_gate(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    async def _capture_quality(result, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return result

    monkeypatch.setattr(runner, "finalize_agent_reply_quality", _capture_quality)
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="候选回复",
                tool_calls=[],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[
                {"role": "system", "content": "你是群里的普通成员。"},
                {"role": "user", "content": "@bot 这个称呼是什么意思"},
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
                ambiguity_level="high",
            ),
            is_group=True,
            reply_required=True,
        )
    )

    assert result.text == "候选回复"
    assert captured["reply_required"] is True
    assert captured["current_user_text"] == "@bot 这个称呼是什么意思"
    assert captured["is_group"] is True


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
    assert result.failure_code == "agent_time_budget_exhausted"
    assert caller.calls == []


def test_run_agent_marks_quality_timeout_as_operational_failure(monkeypatch) -> None:  # noqa: ANN001
    async def _timeout(*_args, **_kwargs):  # noqa: ANN001
        raise asyncio.TimeoutError

    monkeypatch.setattr(runner, "finalize_agent_reply_quality", _timeout)
    caller = _FakeToolCaller([
        tool_impl.ToolCallerResponse(
            finish_reason="stop",
            content="正常生成的候选回复",
            tool_calls=[],
            raw={},
        )
    ])

    result = asyncio.run(runner.run_agent(
        messages=[{"role": "user", "content": "在吗"}],
        registry=tool_registry.ToolRegistry(),
        tool_caller=caller,
        executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
        plugin_config=SimpleNamespace(
            personification_agent_max_steps=2,
            personification_model_builtin_search_enabled=False,
            personification_builtin_search=False,
        ),
        logger=_FakeLogger(),
        precomputed_intent=SimpleNamespace(
            chat_intent="banter",
            plugin_question_intent="capability",
            ambiguity_level="low",
        ),
    ))

    assert result.text == "[NO_REPLY]"
    assert result.failure_code == "agent_quality_timeout"


def test_run_agent_skips_query_rewrite_for_direct_native_image_answer(monkeypatch) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "_record_reply_trace_stage", lambda **kwargs: stages.append(kwargs))
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="图里的文字是：测试通过。",
                tool_calls=[],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "你翻译一下"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
            ),
            logger=_FakeLogger(),
            current_image_urls=["data:image/png;base64,AA=="],
            direct_image_input=True,
            precomputed_intent=SimpleNamespace(
                chat_intent="explanation",
                plugin_question_intent="capability",
                ambiguity_level="high",
            ),
            turn_plan=SimpleNamespace(
                reply_action="reply",
                speech_act="answer",
                research_need="none",
                output_mode="chat_answer",
                tool_intent=["vision"],
            ),
            time_budget_seconds=30,
            reply_required=True,
        )
    )

    rewrite_stage = next(stage for stage in stages if stage["key"] == "agent_query_rewrite")
    assert result.text == "图里的文字是：测试通过。"
    assert len(caller.calls) == 1
    assert "reason=direct_native_image" in str(rewrite_stage["detail"])


def test_run_agent_query_rewrite_timeout_falls_back_and_continues(monkeypatch) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "_QUERY_REWRITE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runner, "_record_reply_trace_stage", lambda **kwargs: stages.append(kwargs))

    class _SlowRewriteCaller(_FakeToolCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            self.calls.append({"messages": messages, "tools": tools, "use_builtin_search": use_builtin_search})
            if len(self.calls) == 1:
                await asyncio.sleep(10)
            return self._responses.pop(0)

    caller = _SlowRewriteCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="先按图里原文逐句翻译。",
                tool_calls=[],
                raw={},
            )
        ]
    )
    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我解释一下"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="explanation",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
            turn_plan=SimpleNamespace(
                reply_action="reply",
                speech_act="answer",
                research_need="none",
                output_mode="chat_answer",
                tool_intent=["none"],
            ),
            time_budget_seconds=30,
        )
    )

    rewrite_stage = next(stage for stage in stages if stage["key"] == "agent_query_rewrite")
    assert result.text == "先按图里原文逐句翻译。"
    assert len(caller.calls) == 2
    assert rewrite_stage["status"] == "warn"
    assert "timeout=true" in str(rewrite_stage["detail"])


def test_run_agent_query_rewrite_consumes_agent_deadline(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(runner, "_QUERY_REWRITE_TIMEOUT_SECONDS", 1.0)

    class _AlwaysSlowCaller(_FakeToolCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            self.calls.append({"messages": messages, "tools": tools, "use_builtin_search": use_builtin_search})
            await asyncio.sleep(10)

    caller = _AlwaysSlowCaller([])
    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "帮我解释一下"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="explanation",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
            time_budget_seconds=0.01,
        )
    )

    assert result.text == "[NO_REPLY]"
    assert result.failure_code == "agent_time_budget_exhausted"
    assert len(caller.calls) == 1


def test_run_agent_invalid_query_rewrite_is_traced_as_structural_fallback(monkeypatch) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "_record_reply_trace_stage", lambda **kwargs: stages.append(kwargs))
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(finish_reason="stop", content="not-json", tool_calls=[], raw={}),
            tool_impl.ToolCallerResponse(finish_reason="stop", content="继续回答", tool_calls=[], raw={}),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "解释一下"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="explanation",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
            time_budget_seconds=30,
        )
    )

    rewrite_stage = next(stage for stage in stages if stage["key"] == "agent_query_rewrite")
    assert result.text == "继续回答"
    assert rewrite_stage["status"] == "warn"
    assert "fallback=invalid_payload" in str(rewrite_stage["detail"])


def test_run_agent_main_model_call_is_bounded_by_agent_deadline(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(runner, "_record_reply_trace_stage", lambda **_kwargs: None)

    class _SlowMainCaller(_FakeToolCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            self.calls.append({"messages": messages, "tools": tools, "use_builtin_search": use_builtin_search})
            await asyncio.sleep(10)

    caller = _SlowMainCaller([])
    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "你翻译一下"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=2,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
            ),
            logger=_FakeLogger(),
            current_image_urls=["data:image/png;base64,AA=="],
            direct_image_input=True,
            precomputed_intent=SimpleNamespace(
                chat_intent="explanation",
                plugin_question_intent="capability",
                ambiguity_level="low",
            ),
            turn_plan=SimpleNamespace(
                reply_action="reply",
                speech_act="answer",
                research_need="none",
                output_mode="chat_answer",
                tool_intent=["vision"],
            ),
            time_budget_seconds=0.01,
        )
    )

    assert result.text == "[NO_REPLY]"
    assert result.failure_code == "agent_model_timeout"
    assert len(caller.calls) == 1


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
    assert result.suppress_reply_recovery is False
    assert len(caller.calls) == 1


def test_structured_agent_stop_returns_raw_without_semantic_fallback() -> None:
    raw = '  {"action":"send","reason":"provider marker stays internal"}\n'
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content=raw,
                tool_calls=[],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "输出 planner JSON"}],
            registry=tool_registry.ToolRegistry(),
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=3,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=True,
                personification_vision_fallback_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
            structured_output=True,
        )
    )

    assert result.text == raw
    assert len(caller.calls) == 1


def test_structured_agent_max_steps_never_persona_synthesizes_tool_result() -> None:
    async def _handler(**_kwargs):  # noqa: ANN003
        return '{"results":[{"title":"evidence"}]}'

    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-structured",
                        name="search_web",
                        arguments={"query": "topic"},
                    )
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-structured-3",
                        name="search_web",
                        arguments={"query": "topic final"},
                    )
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-structured-2",
                        name="search_web",
                        arguments={"query": "topic follow-up"},
                    )
                ],
                raw={},
            ),
        ]
    )
    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "输出 planner JSON"}],
            registry=_register_query_tool(_handler),
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
            max_steps=1,
            structured_output=True,
            finalize_quality=False,
        )
    )

    assert result.text == "[NO_REPLY]"
    assert result.failure_code == "agent_structured_max_steps_exhausted"
    assert len(caller.calls) == 3


def test_run_agent_routes_avatar_capability_to_current_user_tool() -> None:
    calls: list[str] = []

    async def _avatar_handler(**_kwargs):  # noqa: ANN001
        calls.append("inspect_current_user_avatar")
        return json.dumps(
            {"ok": True, "available": True, "analysis": {"status": "success"}, "insight": {"neutral_summary": "蓝发动画角色"}},
            ensure_ascii=False,
        )

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="inspect_current_user_avatar",
            description="只读确认当前发送者安全头像摘要是否可用",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_avatar_handler,
            metadata={
                "intent_tags": ["runtime_capability", "current_user"],
                "source_kind": "first_party_runtime",
                "risk_level": "low",
                "side_effect": "none",
            },
        )
    )
    registry.register(
        tool_registry.AgentTool(
            name="list_plugins",
            description="列出插件",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda **_kwargs: asyncio.sleep(0, result="[]"),
        )
    )
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(id="call-avatar", name="inspect_current_user_avatar", arguments={})
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="能看到你现在这张头像的安全摘要，是蓝发动画角色。",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "你能看见我头像吗"}],
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
                chat_intent="plugin_question",
                plugin_question_intent="runtime_capability",
                ambiguity_level="low",
            ),
        )
    )

    first_tool_names = {
        schema["function"]["name"]
        for schema in caller.calls[0]["tools"]
    }
    assert first_tool_names == {"inspect_current_user_avatar"}
    assert len(caller.calls) == 2
    assert calls == ["inspect_current_user_avatar"]
    assert "蓝发动画角色" in result.text


def test_run_agent_uses_response_aware_turn_builders() -> None:
    async def _handler(**_kwargs):  # noqa: ANN001
        return "tool evidence"

    class _TurnAwareCaller(_FakeToolCaller):
        def __init__(self, responses):  # noqa: ANN001
            super().__init__(responses)
            self.assistant_builds = 0
            self.result_builds = 0

        def build_assistant_tool_calls_message(self, _response):  # noqa: ANN001, ANN201
            self.assistant_builds += 1
            return {"role": "assistant", "content": "native tool turn"}

        def build_tool_result_messages(self, _response, results):  # noqa: ANN001, ANN201
            self.result_builds += 1
            return [
                {
                    "role": "user",
                    "content": f"native result turn: {results[0][1]}",
                }
            ]

    registry = _register_query_tool(_handler)
    caller = _TurnAwareCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-search",
                        name="search_web",
                        arguments={"query": "AI 新闻"},
                    )
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="自然回复",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "聊聊 AI 新闻"}],
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
                chat_intent="banter",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
    )

    assert result.text == "自然回复"
    assert caller.assistant_builds == 1
    assert caller.result_builds == 1
    assert len(caller.calls) == 2
    second_messages = caller.calls[1]["messages"]
    assert second_messages[-2]["content"] == "native tool turn"
    assert second_messages[-1]["content"] == "native result turn: tool evidence"


def test_run_agent_avatar_pair_result_uses_constrained_persona_quality() -> None:
    async def _handler(**_kwargs):  # noqa: ANN001
        return json.dumps(
            {
                "type": "personification_evidence_envelope",
                "available": True,
                "allowed_claims": ["两张头像在构图、元素或风格上呈现视觉配套。"],
                "forbidden_inferences": ["不能据此判断两位用户现实中是情侣、朋友、认识或同一人。"],
                "confidence": 0.9,
                "natural_fallback": "两张头像在构图、元素或风格上呈现视觉配套。",
            },
            ensure_ascii=False,
        )

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="inspect_group_user_avatar_pair",
            description="compare avatars",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_handler,
            metadata={"side_effect": "none", "final_behavior": "constrained_persona_output"},
        )
    )
    caller = _FakeToolCaller(
        [
            tool_impl.ToolCallerResponse(
                finish_reason="tool_calls",
                content="",
                tool_calls=[
                    tool_impl.ToolCall(
                        id="call-avatar-pair",
                        name="inspect_group_user_avatar_pair",
                        arguments={},
                    )
                ],
                raw={},
            ),
            SimpleNamespace(content="这俩头像看着像一套画风。"),
            SimpleNamespace(
                content=json.dumps(
                    {
                        "action": "accept",
                        "text": "",
                        "claims_ok": True,
                        "inferences_ok": True,
                        "mechanism_hidden": True,
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "比较这两个人的头像"}],
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
                chat_intent="banter",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
        )
    )

    assert result.direct_output is False
    assert result.bypass_length_limits is False
    assert result.text == "这俩头像看着像一套画风。"
    assert "现实里就是情侣" not in result.text
    assert len(caller.calls) == 3


def test_run_agent_sends_ack_when_first_tool_call_appears(monkeypatch) -> None:  # noqa: ANN001
    ack_calls: list[str] = []

    async def _handler(**kwargs):  # noqa: ANN001
        assert "热搜" in kwargs["query"]
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


def test_run_agent_suppresses_ack_when_any_first_tool_call_requests_it() -> None:
    ack_calls: list[str] = []
    handled: list[str] = []

    async def _search_handler(**_kwargs):  # noqa: ANN001
        handled.append("search_web")
        return "工具结果"

    async def _recall_handler(**_kwargs):  # noqa: ANN001
        handled.append("recall_latest_own_output")
        return json.dumps({"ok": True, "queued": True}, ensure_ascii=False)

    async def _ack_sender(text: str) -> None:
        ack_calls.append(text)

    registry = _register_query_tool(_search_handler)
    registry.register(
        tool_registry.AgentTool(
            name="recall_latest_own_output",
            description="recall latest own output",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_recall_handler,
            metadata={
                "intent_tags": ["conversation_action"],
                "side_effect": "message_recall",
                "final_behavior": "silence_on_success",
                "ack_behavior": "suppress",
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
                        id="call-search",
                        name="search_web",
                        arguments={"query": "刚才的消息"},
                    ),
                    tool_impl.ToolCall(
                        id="call-recall",
                        name="recall_latest_own_output",
                        arguments={},
                    ),
                ],
                raw={},
            )
        ]
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "处理上一条输出"}],
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
                chat_intent="banter",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
            ack_sender=_ack_sender,
        )
    )

    assert result.text == "[SILENCE]"
    assert result.suppress_reply_recovery is True
    assert ack_calls == []
    assert handled == ["search_web", "recall_latest_own_output"]


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


def test_run_agent_stops_equivalent_searches_after_social_coverage_is_satisfied() -> None:
    handled: list[str] = []

    async def _social_handler(**_kwargs):  # noqa: ANN001
        handled.append("social_content_search")
        return json.dumps(
            {
                "schema_version": 1,
                "partial": False,
                "aggregation": {
                    "requested_limit": 10,
                    "returned_count": 2,
                    "source_group_count": 2,
                    "selected_platforms": ["xiaoheihe", "bilibili"],
                    "successful_platforms": ["xiaoheihe", "bilibili"],
                    "covered_platforms": ["xiaoheihe", "bilibili"],
                    "coverage_status": "complete",
                    "satisfies_request": True,
                },
                "items": [
                    {
                        "platform": "xiaoheihe",
                        "content_id": "179364001",
                        "source_group_id": "source_1",
                        "title": "花来出处",
                        "canonical_url": "https://xiaoheihe.cn/app/bbs/link/179364001",
                    },
                    {
                        "platform": "bilibili",
                        "content_id": "BV1abc",
                        "source_group_id": "source_2",
                        "title": "花来讨论",
                        "canonical_url": "https://www.bilibili.com/video/BV1abc/",
                    },
                ],
            },
            ensure_ascii=False,
        )

    async def _web_handler(**_kwargs):  # noqa: ANN001
        raise AssertionError("generic web search must not run after satisfied social evidence")

    registry = tool_registry.ToolRegistry()
    for name, handler in (
        ("social_content_search", _social_handler),
        ("web_search", _web_handler),
    ):
        registry.register(
            tool_registry.AgentTool(
                name=name,
                description="lookup",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                handler=handler,
                metadata={
                    "category": "retrieval",
                    "intent_tags": ["lookup", "game_slang"],
                    "evidence_kind": "social" if name == "social_content_search" else "web",
                    "side_effect": "none",
                    "retryable": True,
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
                        id="call-social",
                        name="social_content_search",
                        arguments={"query": "花来"},
                    )
                ],
                raw={},
            ),
            tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="花来是玩家社区里的调侃说法。",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    turn_plan = SimpleNamespace(
        tool_intent=["lookup_web"],
        research_need="high",
        session_goal="解释这个说法",
    )

    result = asyncio.run(
        runner.run_agent(
            messages=[{"role": "user", "content": "花来是什么意思"}],
            registry=registry,
            tool_caller=caller,
            executor=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
            plugin_config=SimpleNamespace(
                personification_agent_max_steps=3,
                personification_model_builtin_search_enabled=False,
                personification_builtin_search=False,
                personification_fallback_enabled=False,
                personification_vision_fallback_enabled=False,
                personification_evidence_synthesizer_enabled=False,
            ),
            logger=_FakeLogger(),
            precomputed_intent=SimpleNamespace(
                chat_intent="banter",
                plugin_question_intent="",
                ambiguity_level="low",
            ),
            turn_plan=turn_plan,
        )
    )

    assert handled == ["social_content_search"]
    second_tools = {
        schema["function"]["name"] for schema in caller.calls[1]["tools"]
    }
    assert "social_content_search" not in second_tools
    assert "web_search" not in second_tools
    assert "社交证据已满足" in turn_plan.session_goal
    assert "https://xiaoheihe.cn/app/bbs/link/179364001" in result.text
    assert result.evidence_delivery_status == "recovered"


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

    assert result.text == "[NO_REPLY]"
    assert result.failure_code == "agent_image_generation_failed"
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

    assert result.text == "[NO_REPLY]"
    assert result.failure_code == "agent_image_generation_failed"
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
    assert result.suppress_reply_recovery is True
    assert sent_texts == []
    assert sent_images == ["QUJD"]
    assert len(caller.calls) == 2


def test_background_image_no_reply_status_suppresses_required_recovery(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(runner, "_start_background_image_generation", lambda **_kwargs: True)
    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="generate_image",
            description="generate image",
            parameters={"type": "object", "properties": {}},
            handler=lambda **_kwargs: None,
        )
    )
    caller = _FakeToolCaller([
        tool_impl.ToolCallerResponse(
            finish_reason="stop",
            content="[NO_REPLY]",
            tool_calls=[],
            raw={},
        )
    ])
    executor = SimpleNamespace(
        send_text=lambda _text: asyncio.sleep(0),
        send_image_b64=lambda _data: asyncio.sleep(0),
        execute=lambda *_args, **_kwargs: None,
    )

    result = asyncio.run(runner.run_agent(
        messages=[{"role": "user", "content": "画一张图"}],
        registry=registry,
        tool_caller=caller,
        executor=executor,
        plugin_config=SimpleNamespace(
            personification_agent_max_steps=2,
            personification_model_builtin_search_enabled=False,
            personification_builtin_search=False,
            personification_image_gen_background_enabled=True,
        ),
        logger=_FakeLogger(),
        precomputed_intent=SimpleNamespace(
            chat_intent="image_generation",
            plugin_question_intent="",
            ambiguity_level="low",
        ),
        reply_required=True,
    ))

    assert result.text == "[NO_REPLY]"
    assert result.suppress_reply_recovery is True


def test_background_image_provider_failure_stays_silent_and_redacted() -> None:
    sent_texts: list[str] = []

    class _Executor:
        async def send_text(self, text: str) -> None:
            sent_texts.append(text)

        async def send_image_b64(self, _image_b64: str) -> None:
            raise AssertionError("image must not be sent")

    class _FailingCaller:
        async def chat_with_tools(self, *_args, **_kwargs):  # noqa: ANN001
            error = RuntimeError("https://private.example/?api_key=top-secret")
            error.code = "provider_call_failed"
            raise error

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="generate_image",
            description="generate image",
            parameters={"type": "object", "properties": {}},
            handler=lambda **_kwargs: None,
        )
    )
    logger = _FakeLogger()

    asyncio.run(image_generation._run_background_image_generation(
        registry=registry,
        executor=_Executor(),
        tool_caller=_FailingCaller(),
        messages=[],
        user_request="画一张图",
        rewritten_query=None,
        user_images=[],
        use_builtin_search=False,
        logger=logger,
    ))

    assert sent_texts == []
    assert logger.warning_messages
    assert "provider_failure" in logger.warning_messages[-1]
    assert "top-secret" not in logger.warning_messages[-1]
    assert "private.example" not in logger.warning_messages[-1]


def test_background_research_failure_does_not_reinject_raw_exception() -> None:
    async def _failing_search(**_kwargs):  # noqa: ANN001
        raise RuntimeError("https://private.example/?api_key=top-secret")

    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="search_web",
            description="search",
            parameters={"type": "object", "properties": {}},
            handler=_failing_search,
        )
    )
    registry.register(
        tool_registry.AgentTool(
            name="generate_image",
            description="generate image",
            parameters={"type": "object", "properties": {}},
            handler=lambda **_kwargs: None,
        )
    )
    caller = _FakeToolCaller([
        tool_impl.ToolCallerResponse(
            finish_reason="tool_calls",
            content="",
            tool_calls=[tool_impl.ToolCall(id="search", name="search_web", arguments={})],
            raw={},
        ),
        tool_impl.ToolCallerResponse(
            finish_reason="stop",
            content="这次先不继续画。",
            tool_calls=[],
            raw={},
        ),
    ])
    logger = _FakeLogger()
    sent_texts: list[str] = []
    executor = SimpleNamespace(
        send_text=lambda text: asyncio.sleep(0, result=sent_texts.append(text)),
        send_image_b64=lambda _data: asyncio.sleep(0),
    )

    asyncio.run(image_generation._run_background_image_generation(
        registry=registry,
        executor=executor,
        tool_caller=caller,
        messages=[],
        user_request="画一张图",
        rewritten_query=None,
        user_images=[],
        use_builtin_search=False,
        logger=logger,
    ))

    assert sent_texts == ["这次先不继续画。"]
    assert len(caller.calls) == 2
    rendered_messages = str(caller.calls[1]["messages"])
    assert "top-secret" not in rendered_messages
    assert "private.example" not in rendered_messages
    assert any("type=RuntimeError" in message for message in logger.warning_messages)


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
