from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..query_rewriter import ContextualQueryRewrite
from ..tool_registry import ToolRegistry
from ...skills.skillpacks.tool_caller.scripts.impl import ToolCaller
from .tool_selection import _select_tool_schemas


_PLAIN_EMPTY_TOOL_RESULT_MARKERS = (
    "未找到足够可靠的wiki条目",
    "没有找到足够可靠的wiki条目",
    "未找到可靠wiki条目",
    "未找到相关wiki条目",
    "未找到可靠结果",
    "没有找到可靠结果",
    "no_results",
)


async def cancel_task_safely(task: asyncio.Task | None, logger: Any, label: str = "task") -> None:
    if task is None:
        return
    if task.done():
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"[agent] {label} cleanup failed: {exc}")
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        logger.warning(f"[agent] {label} cleanup timed out after 2.0s")
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning(f"[agent] {label} cleanup failed: {exc}")


async def select_semantic_fallback_tool(
    *,
    tool_caller: ToolCaller,
    registry: ToolRegistry,
    user_query_text: str,
    rewritten_query: ContextualQueryRewrite | None,
    draft_answer_text: str,
    context_hint: str = "",
    has_images: bool = False,
    chat_intent: str = "",
    plugin_question_intent: str = "",
    user_images: list[str] | None = None,
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
) -> tuple[str, dict] | None:
    query = str(user_query_text or "").strip()
    semantic_schemas = _select_tool_schemas(
        registry,
        has_images=has_images,
        chat_intent=chat_intent,
        plugin_question_intent=plugin_question_intent,
    )
    if not query or not semantic_schemas:
        return None
    intent = rewritten_query or ContextualQueryRewrite(
        primary_query=query,
        query_candidates=[query],
        context_clues=[],
        need_image_understanding=False,
        recommended_tools=[],
        search_plan=[],
    )

    planner_messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是工具路由器。"
                "你的唯一职责是审查当前草稿回答是否还需要工具补充。"
                "如果不需要任何工具，必须只输出 NO_TOOL。"
                "如果需要工具，必须直接发起一个且仅一个工具调用。"
                "不要输出解释、分析、寒暄或口头承诺。"
                "根据语义、当前草稿内容和工具描述做决定，不要按固定关键词机械路由。"
                "优先选择能直接满足用户需求的工具；如果答案已经足够，就返回 NO_TOOL。"
                "优先考虑：事实风险、歧义程度、图片是否存在、是否需要回忆过去。"
                "高歧义 ACG 实体优先考虑 resolve_acg_entity；有图片线索时优先考虑 vision_analyze。"
                "如果用户像是在问梗、黑话、谐音、空耳、外号、别称、缩写，优先考虑 resolve_acg_entity 或 web_search 做校验。"
                "如果用户在问 bot 某个插件的实现方式、配置读取、命令匹配或发送逻辑，优先考虑 search_plugin_source。"
                "普通事实查证、实时信息、定义解释优先考虑 web_search；"
                "search_web 更适合找入口页、资料页和链接列表，不是默认事实核查首选。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户当前需求：{query}"
                + (
                    f"\n重写后的主检索词：{intent.primary_query}"
                    if str(intent.primary_query or "").strip() else ""
                )
                + (
                    f"\n候选检索词：{', '.join(intent.query_candidates[:4])}"
                    if intent.query_candidates else ""
                )
                + (
                    f"\n上下文线索：{'; '.join(intent.context_clues[:4])}"
                    if intent.context_clues else ""
                )
                + (
                    f"\n建议优先工具：{', '.join(intent.recommended_tools[:4])}"
                    if intent.recommended_tools else ""
                )
                + (
                    f"\n检索计划：{'；'.join(intent.search_plan[:3])}"
                    if intent.search_plan else ""
                )
                + (f"\n上下文提示：{context_hint}" if str(context_hint or "").strip() else "")
                + f"\n当前草稿回答：{str(draft_answer_text or '').strip() or '[EMPTY]'}"
                + f"\n当前消息是否包含图片：{'是' if has_images else '否'}"
                + (
                    f"\n上一轮工具：{previous_tool_name}"
                    if str(previous_tool_name or "").strip() else ""
                )
                + (
                    f"\n上一轮工具结果摘要：{previous_tool_result_text[:600]}"
                    if str(previous_tool_result_text or "").strip() else ""
                )
            ),
        },
    ]
    response = await tool_caller.chat_with_tools(planner_messages, semantic_schemas, False)
    if response.tool_calls:
        tool_call = response.tool_calls[0]
        return str(tool_call.name or "").strip(), dict(tool_call.arguments or {})
    if str(response.content or "").strip().upper() == "NO_TOOL":
        return None
    return None


async def run_background_vision_fallback(
    *,
    registry: ToolRegistry,
    query: str,
    images: list[str],
) -> tuple[str, dict[str, Any], str] | None:
    tool = registry.get("vision_analyze")
    if tool is None or not images:
        return None
    try:
        if not tool.enabled():
            return None
    except Exception:
        return None
    args = {"query": query, "images": list(images)}
    result = await tool.handler(**args)
    return "vision_analyze", args, str(result or "")


async def inject_background_tool_result(
    *,
    messages: list[dict],
    tool_caller: ToolCaller,
    tool_name: str,
    tool_args: dict[str, Any],
    result: str,
    step: int,
) -> None:
    fallback_id = f"background-{tool_name}-{step}"
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": fallback_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }
            ],
        }
    )
    messages.append(tool_caller.build_tool_result_message(fallback_id, tool_name, result))


def parse_json_tool_result(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def tool_result_indicates_empty(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    payload = parse_json_tool_result(raw)
    if not isinstance(payload, dict):
        normalized = re.sub(r"\s+", "", raw).lower()
        return any(marker in normalized for marker in _PLAIN_EMPTY_TOOL_RESULT_MARKERS)
    results = payload.get("results", [])
    if isinstance(results, list) and results:
        return False
    top_candidates = payload.get("top_candidates", [])
    if isinstance(top_candidates, list) and top_candidates:
        return False
    if payload.get("error") == "no_results":
        return True
    if "top_candidates" in payload:
        return not bool(top_candidates)
    return not bool(payload.get("ok"))


_cancel_task_safely = cancel_task_safely
_select_semantic_fallback_tool = select_semantic_fallback_tool
_run_background_vision_fallback = run_background_vision_fallback
_inject_background_tool_result = inject_background_tool_result
_parse_json_tool_result = parse_json_tool_result
_tool_result_indicates_empty = tool_result_indicates_empty

__all__ = [
    "_cancel_task_safely",
    "_inject_background_tool_result",
    "_parse_json_tool_result",
    "_run_background_vision_fallback",
    "_select_semantic_fallback_tool",
    "_tool_result_indicates_empty",
    "cancel_task_safely",
    "inject_background_tool_result",
    "parse_json_tool_result",
    "run_background_vision_fallback",
    "select_semantic_fallback_tool",
    "tool_result_indicates_empty",
]
