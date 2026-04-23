from __future__ import annotations

import asyncio
import json
from datetime import datetime

from ._loader import load_personification_module

runner = load_personification_module("nonebot_plugin_personification.agent.runtime.runner")
metrics = load_personification_module("nonebot_plugin_personification.core.metrics")
tool_registry = load_personification_module("nonebot_plugin_personification.agent.tool_registry")


class _FakeLogger:
    def __init__(self) -> None:
        self.warning_messages: list[str] = []

    def info(self, *_args, **_kwargs) -> None:
        return

    def debug(self, *_args, **_kwargs) -> None:
        return

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


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

