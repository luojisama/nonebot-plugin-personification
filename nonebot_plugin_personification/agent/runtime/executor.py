from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ..query_rewriter import ContextualQueryRewrite
from ..tool_registry import ToolRegistry
from ...core.error_utils import log_exception
from ...core.metrics import record_counter, record_timing
from ...core.time_ctx import get_configured_now
from .fallbacks import _tool_result_indicates_empty
from .intent import _clean_user_query_text
from .tool_args import (
    _query_variants_for_tool,
    _rewrite_tool_args,
    _sanitize_tool_args_for_schema,
)


_IMAGE_GENERATION_TOOL_NAME = "generate_image"
_RETRYABLE_LOOKUP_TOOLS = frozenset(
    {"parallel_research", "web_search", "search_web", "wiki_lookup", "resolve_acg_entity", "collect_resources", "search_images"}
)
_TIME_SENSITIVE_SEARCH_TOOLS = frozenset({"web_search", "search_web"})
_TIME_SENSITIVE_RE = re.compile("最新|近期|现在|今年|今天|当前|latest|recent|now", re.IGNORECASE)


def _maybe_inject_date_to_query(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in _TIME_SENSITIVE_SEARCH_TOOLS:
        return args
    query = str(args.get("query", "") or "").strip()
    if not query or not _TIME_SENSITIVE_RE.search(query):
        return args
    try:
        now = get_configured_now()
        date_str = now.strftime("%Y年") + str(now.month) + "月" + str(now.day) + "日"
    except Exception:
        return args
    if date_str in query:
        return args
    return {**args, "query": f"{query} ({date_str})"}


async def _invoke_tool_handler(
    *,
    tool_name: str,
    tool: Any,
    tool_args: dict[str, Any],
) -> str:
    if tool.local:
        return await tool.handler(**tool_args)
    from ..mcp.bridge import McpBridge

    return await McpBridge().call_remote(tool_name, tool_args)


def _remaining_time_budget_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, float(deadline) - time.monotonic())


def _tool_timeout_result(tool_name: str) -> str:
    normalized_tool_name = str(tool_name or "").strip()
    if normalized_tool_name == _IMAGE_GENERATION_TOOL_NAME:
        return "图片生成失败：生成超时，请稍后重试"
    return "工具调用失败：超时"


async def _execute_tool_with_retries(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_args: dict[str, Any],
    rewritten_query: ContextualQueryRewrite | None,
    user_images: list[str],
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
    logger: Any,
    budget_deadline: float | None = None,
) -> tuple[dict[str, Any], str]:
    tool = registry.get(tool_name)
    if tool is None:
        record_counter("agent.tool_fail_total", tool=tool_name, reason="missing")
        return dict(tool_args or {}), f"工具 {tool_name} 不存在"

    tool_args = _maybe_inject_date_to_query(tool_name, dict(tool_args or {}))
    query_variants = _query_variants_for_tool(
        tool_name=tool_name,
        tool_args=tool_args,
        rewritten_query=rewritten_query,
    )
    if not query_variants:
        query_variants = [_clean_user_query_text(tool_args.get("query", ""))]
    last_args = dict(tool_args or {})
    last_result = ""
    for index, query in enumerate(query_variants or [""]):
        attempt_args = dict(tool_args or {})
        if query:
            attempt_args["query"] = query
        attempt_args = _rewrite_tool_args(
            registry=registry,
            tool_name=tool_name,
            tool_args=attempt_args,
            rewritten_query=rewritten_query,
            user_images=user_images,
            previous_tool_name=previous_tool_name,
            previous_tool_result_text=previous_tool_result_text,
        )
        attempt_args = _sanitize_tool_args_for_schema(
            registry=registry,
            tool_name=tool_name,
            tool_args=attempt_args,
        )
        last_args = attempt_args
        remaining_timeout = _remaining_time_budget_seconds(budget_deadline)
        if remaining_timeout is not None and remaining_timeout <= 0.0:
            record_counter("agent.tool_fail_total", tool=tool_name, reason="timeout")
            record_timing("agent.tool_exec_ms", 0, tool=tool_name, status="timeout")
            logger.warning(f"[agent] tool {tool_name} skipped because time budget was exhausted")
            last_result = _tool_timeout_result(tool_name)
            break
        started_at = time.monotonic()
        try:
            invoke_coro = _invoke_tool_handler(
                tool_name=tool_name,
                tool=tool,
                tool_args=attempt_args,
            )
            if remaining_timeout is None:
                last_result = await invoke_coro
            else:
                last_result = await asyncio.wait_for(invoke_coro, timeout=remaining_timeout)
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            record_counter("agent.tool_fail_total", tool=tool_name, reason="timeout")
            record_timing("agent.tool_exec_ms", elapsed_ms, tool=tool_name, status="timeout")
            last_result = _tool_timeout_result(tool_name)
            logger.warning(f"[agent] tool {tool_name} timed out after {elapsed_ms}ms")
            break
        except Exception as e:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            record_counter("agent.tool_fail_total", tool=tool_name, reason="exception")
            record_timing("agent.tool_exec_ms", elapsed_ms, tool=tool_name, status="fail")
            last_result = f"工具调用失败：{e}"
            log_exception(
                logger,
                f"[agent] tool {tool_name} error after {elapsed_ms}ms",
                e,
            )
            break
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        record_counter("agent.tool_ok_total", tool=tool_name)
        record_timing("agent.tool_exec_ms", elapsed_ms, tool=tool_name, status="ok")
        logger.info(
            f"[agent] tool_exec name={tool_name} attempt={index + 1}/{len(query_variants or [''])} "
            f"elapsed_ms={elapsed_ms} result_len={len(str(last_result or ''))}"
        )
        if index > 0:
            logger.info(f"[agent] retry {tool_name} with candidate={attempt_args.get('query', '')}")
        if tool_name not in _RETRYABLE_LOOKUP_TOOLS or not _tool_result_indicates_empty(last_result):
            break
    return last_args, last_result


__all__ = [
    "_execute_tool_with_retries",
    "_invoke_tool_handler",
    "_maybe_inject_date_to_query",
    "_remaining_time_budget_seconds",
    "_tool_timeout_result",
]
