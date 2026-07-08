from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ...core.metrics import record_timing
from .evidence import build_tool_result_record
from .executor import _execute_tool_with_retries
from .fallbacks import (
    _inject_background_tool_result,
    _run_background_vision_fallback,
    _tool_result_indicates_empty,
)
from .final_synthesis import AgentResult
from .loop_utils import _RETRYABLE_LOOKUP_TOOLS, tool_signature
from .tool_loop import append_single_tool_call_exchange
from .tool_selection import _schema_tool_name


@dataclass
class StopFlowState:
    has_tool_call: bool = False
    last_tool_name: str = ""
    last_tool_result_text: str = ""
    last_fallback_signature: str = ""
    semantic_fallback_attempted: bool = False
    pending_evidence_followup_query: str = ""
    empty_lookup_tools: set[str] = field(default_factory=set)
    tool_result_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class StopFlowDecision:
    action: str
    result: AgentResult | None = None

    @classmethod
    def continue_loop(cls) -> "StopFlowDecision":
        return cls(action="continue")

    @classmethod
    def return_result(cls, result: AgentResult) -> "StopFlowDecision":
        return cls(action="return", result=result)


def _has_lookup_schema(schemas: list[dict]) -> bool:
    return any(_schema_tool_name(schema) in _RETRYABLE_LOOKUP_TOOLS for schema in list(schemas or []))


def _should_review_banter_lookup_draft(*, ambiguity_level: str, draft_answer_text: str) -> bool:
    # 只用结构性信号控制是否追加一次模型审查，避免把具体话题词写进代码语义。
    if str(ambiguity_level or "").strip() == "high":
        return True
    draft = str(draft_answer_text or "").strip()
    return "?" in draft or "？" in draft


def _vision_fallback_enabled(plugin_config: Any) -> bool:
    return bool(
        getattr(
            plugin_config,
            "personification_fallback_enabled",
            getattr(plugin_config, "personification_vision_fallback_enabled", True),
        )
    )


async def _try_inject_vision_fallback(
    *,
    state: StopFlowState,
    messages: list[dict],
    tool_caller: Any,
    registry: Any,
    plugin_config: Any,
    logger: Any,
    query: str,
    user_images: list[str],
    step: int,
    warning_message: str,
    success_message: str,
) -> bool:
    if (
        not _vision_fallback_enabled(plugin_config)
        or registry.get("vision_analyze") is None
        or not user_images
    ):
        return False
    try:
        background = await _run_background_vision_fallback(
            registry=registry,
            query=query,
            images=user_images,
        )
    except Exception as exc:
        logger.warning(f"{warning_message}: {exc}")
        background = None
    if background is None:
        return False
    bg_name, bg_args, bg_result = background
    await _inject_background_tool_result(
        messages=messages,
        tool_caller=tool_caller,
        tool_name=bg_name,
        tool_args=bg_args,
        result=bg_result,
        step=step,
    )
    state.has_tool_call = True
    state.last_tool_name = bg_name
    state.last_tool_result_text = bg_result
    logger.info(success_message)
    return True


async def _classify_banter_lookup_retry(
    *,
    state: StopFlowState,
    response: Any,
    content_len: int,
    runtime_chat_intent: str,
    intent_decision: Any,
    active_schemas: list[dict],
    user_query_text: str,
    tool_caller: Any,
    logger: Any,
    record_trace: Callable[..., None],
    classify_deferred_lookup_reply: Callable[..., Awaitable[bool]],
) -> bool:
    if runtime_chat_intent != "banter" or response.tool_calls or content_len <= 0:
        return False
    if (
        not state.has_tool_call
        and not state.semantic_fallback_attempted
        and bool(user_query_text)
        and _has_lookup_schema(active_schemas)
        and _should_review_banter_lookup_draft(
            ambiguity_level=str(getattr(intent_decision, "ambiguity_level", "") or ""),
            draft_answer_text=str(response.content or ""),
        )
    ):
        lookup_review_started_at = time.monotonic()
        retry = await classify_deferred_lookup_reply(
            tool_caller=tool_caller,
            user_query_text=user_query_text,
            assistant_reply_text=str(response.content or ""),
            previous_tool_name=state.last_tool_name,
            previous_tool_result_text=state.last_tool_result_text,
        )
        lookup_review_elapsed_ms = int((time.monotonic() - lookup_review_started_at) * 1000)
        record_timing(
            "agent.banter_lookup_review_ms",
            lookup_review_elapsed_ms,
            retry=bool(retry),
        )
        record_trace(
            key="agent_banter_lookup_review",
            label="Banter 查证裁判",
            status="warn" if retry else "ok",
            detail=f"retry={bool(retry)} elapsed_ms={lookup_review_elapsed_ms}",
            hint="若此阶段经常较慢，配置 lite_model 并关闭严格主模型模式",
        )
        if retry:
            logger.info("[agent] banter draft requested lookup retry")
        return bool(retry)
    return False


async def _select_stop_fallback_lookup(
    *,
    state: StopFlowState,
    response: Any,
    content_len: int,
    runtime_chat_intent: str,
    banter_requires_lookup_retry: bool,
    user_query_text: str,
    rewritten_query: Any,
    context_hint: str,
    user_images: list[str],
    plugin_query_intent: str,
    tool_caller: Any,
    registry: Any,
    record_trace: Callable[..., None],
    logger: Any,
    select_semantic_fallback_tool: Callable[..., Awaitable[tuple[str, dict] | None]],
) -> tuple[str, dict] | None:
    previous_tool_empty = _tool_result_indicates_empty(state.last_tool_result_text)
    non_banter_fallback_needed = (
        runtime_chat_intent != "banter"
        and (
            not state.has_tool_call
            or bool(state.pending_evidence_followup_query)
            or content_len == 0
            or response.vision_unavailable
        )
    )
    should_run_fallback_lookup = (
        not state.semantic_fallback_attempted
        and bool(user_query_text)
        and (non_banter_fallback_needed or banter_requires_lookup_retry)
    )
    fallback_lookup = None
    if should_run_fallback_lookup:
        state.semantic_fallback_attempted = True
        fallback_query_text = state.pending_evidence_followup_query or user_query_text
        fallback_planner_started_at = time.monotonic()
        fallback_lookup = await select_semantic_fallback_tool(
            tool_caller=tool_caller,
            registry=registry,
            user_query_text=fallback_query_text,
            rewritten_query=rewritten_query,
            draft_answer_text=response.content,
            context_hint=context_hint,
            has_images=bool(user_images),
            chat_intent=runtime_chat_intent,
            plugin_question_intent=plugin_query_intent,
            user_images=user_images,
            previous_tool_name=state.last_tool_name,
            previous_tool_result_text=state.last_tool_result_text,
        )
        fallback_planner_elapsed_ms = int((time.monotonic() - fallback_planner_started_at) * 1000)
        record_timing(
            "agent.semantic_fallback_planner_ms",
            fallback_planner_elapsed_ms,
            selected=bool(fallback_lookup),
            intent=runtime_chat_intent or "unknown",
        )
        record_trace(
            key="agent_semantic_fallback",
            label="语义 fallback 选工具",
            status="ok" if fallback_lookup else "warn",
            detail=(
                f"selected={fallback_lookup[0] if fallback_lookup else '-'} "
                f"intent={runtime_chat_intent or '-'} elapsed_ms={fallback_planner_elapsed_ms}"
            ),
        )
        if fallback_lookup is None and state.pending_evidence_followup_query:
            state.pending_evidence_followup_query = ""
    if fallback_lookup is None:
        return None
    fallback_name, _fallback_args = fallback_lookup
    if fallback_name in state.empty_lookup_tools:
        logger.info(f"[agent] semantic fallback skipped previously empty tool: {fallback_name}")
        return None
    if fallback_name == state.last_tool_name and previous_tool_empty:
        logger.info(f"[agent] semantic fallback skipped immediate empty tool repeat: {fallback_name}")
        return None
    fallback_signature = tool_signature(fallback_name, fallback_lookup[1])
    if fallback_signature == state.last_fallback_signature:
        logger.info("[agent] semantic fallback repeated same tool signature; skipping")
        return None
    state.last_fallback_signature = fallback_signature
    return fallback_lookup


async def _run_stop_fallback_tool(
    *,
    state: StopFlowState,
    fallback_name: str,
    fallback_args: dict,
    step: int,
    registry: Any,
    rewritten_query: Any,
    user_images: list[str],
    logger: Any,
    budget_deadline: float | None,
    messages: list[dict],
    tool_caller: Any,
    record_trace: Callable[..., None],
    append_evidence_guidance: Callable[..., Awaitable[Any]],
) -> bool:
    fallback_tool = registry.get(fallback_name)
    if fallback_tool is None:
        logger.info(f"[agent] semantic fallback selected unavailable tool: {fallback_name}")
        return False
    fallback_tool_started_at = time.monotonic()
    fallback_args, fallback_result = await _execute_tool_with_retries(
        registry=registry,
        tool_name=fallback_name,
        tool_args=fallback_args,
        rewritten_query=rewritten_query,
        user_images=user_images,
        previous_tool_name=state.last_tool_name,
        previous_tool_result_text=state.last_tool_result_text,
        logger=logger,
        budget_deadline=budget_deadline,
    )
    record_trace(
        key="agent_fallback_tool",
        label="fallback 工具执行",
        status="ok" if str(fallback_result or "").strip() else "warn",
        detail=f"tool={fallback_name} elapsed_ms={int((time.monotonic() - fallback_tool_started_at) * 1000)}",
    )
    fallback_id = f"fallback-{fallback_name}-{step}"
    append_single_tool_call_exchange(
        messages=messages,
        tool_caller=tool_caller,
        call_id=fallback_id,
        tool_name=fallback_name,
        tool_args=fallback_args,
        result=fallback_result,
    )
    state.last_tool_name = str(fallback_name or "").strip()
    if str(fallback_result or "").strip():
        state.last_tool_result_text = str(fallback_result).strip()
    state.has_tool_call = True
    state.pending_evidence_followup_query = ""
    state.tool_result_records.append(
        build_tool_result_record(
            tool_name=fallback_name,
            tool_args=fallback_args,
            result=fallback_result,
        )
    )
    state.semantic_fallback_attempted = False
    logger.info(f"[agent] fallback tool_call name={fallback_name}")
    await append_evidence_guidance()
    return True


async def handle_model_stop(
    *,
    state: StopFlowState,
    response: Any,
    content_len: int,
    active_schemas: list[dict],
    runtime_chat_intent: str,
    intent_decision: Any,
    registry: Any,
    tool_caller: Any,
    logger: Any,
    messages: list[dict],
    pending_actions: list[dict],
    plugin_config: Any,
    user_query_text: str,
    user_text: str,
    user_images: list[str],
    rewritten_query: Any,
    context_hint: str,
    plugin_query_intent: str,
    budget_deadline: float | None,
    step: int,
    record_trace: Callable[..., None],
    append_evidence_guidance: Callable[..., Awaitable[Any]],
    classify_deferred_lookup_reply: Callable[..., Awaitable[bool]],
    select_semantic_fallback_tool: Callable[..., Awaitable[tuple[str, dict] | None]],
) -> StopFlowDecision:
    banter_requires_lookup_retry = await _classify_banter_lookup_retry(
        state=state,
        response=response,
        content_len=content_len,
        runtime_chat_intent=runtime_chat_intent,
        intent_decision=intent_decision,
        active_schemas=active_schemas,
        user_query_text=user_query_text,
        tool_caller=tool_caller,
        logger=logger,
        record_trace=record_trace,
        classify_deferred_lookup_reply=classify_deferred_lookup_reply,
    )
    if runtime_chat_intent == "banter" and not response.tool_calls and content_len > 0 and not banter_requires_lookup_retry:
        record_trace(
            key="agent_finish",
            label="Agent 收尾",
            status="ok",
            detail=f"reason=banter_stop content_len={content_len}",
        )
        return StopFlowDecision.return_result(
            AgentResult(
                text=str(response.content or "").strip(),
                pending_actions=pending_actions,
                bypass_length_limits=False,
            )
        )
    if response.vision_unavailable:
        injected = await _try_inject_vision_fallback(
            state=state,
            messages=messages,
            tool_caller=tool_caller,
            registry=registry,
            plugin_config=plugin_config,
            logger=logger,
            query=user_query_text or user_text or "请分析图片",
            user_images=user_images,
            step=step,
            warning_message="[agent] vision fallback failed",
            success_message="[agent] injected background vision fallback result",
        )
        if injected:
            return StopFlowDecision.continue_loop()
    fallback_lookup = await _select_stop_fallback_lookup(
        state=state,
        response=response,
        content_len=content_len,
        runtime_chat_intent=runtime_chat_intent,
        banter_requires_lookup_retry=banter_requires_lookup_retry,
        user_query_text=user_query_text,
        rewritten_query=rewritten_query,
        context_hint=context_hint,
        user_images=user_images,
        plugin_query_intent=plugin_query_intent,
        tool_caller=tool_caller,
        registry=registry,
        record_trace=record_trace,
        logger=logger,
        select_semantic_fallback_tool=select_semantic_fallback_tool,
    )
    if fallback_lookup is not None:
        fallback_name, fallback_args = fallback_lookup
        ran_tool = await _run_stop_fallback_tool(
            state=state,
            fallback_name=fallback_name,
            fallback_args=fallback_args,
            step=step,
            registry=registry,
            rewritten_query=rewritten_query,
            user_images=user_images,
            logger=logger,
            budget_deadline=budget_deadline,
            messages=messages,
            tool_caller=tool_caller,
            record_trace=record_trace,
            append_evidence_guidance=append_evidence_guidance,
        )
        if ran_tool:
            return StopFlowDecision.continue_loop()
    if banter_requires_lookup_retry:
        record_trace(
            key="agent_finish",
            label="Agent 收尾",
            status="warn",
            detail="reason=banter_lookup_retry_failed text=[NO_REPLY]",
        )
        return StopFlowDecision.return_result(
            AgentResult(
                text="[NO_REPLY]",
                pending_actions=pending_actions,
                bypass_length_limits=False,
            )
        )
    if content_len == 0:
        injected = await _try_inject_vision_fallback(
            state=state,
            messages=messages,
            tool_caller=tool_caller,
            registry=registry,
            plugin_config=plugin_config,
            logger=logger,
            query=user_query_text or user_text or "请分析图片",
            user_images=user_images,
            step=step,
            warning_message="[agent] deferred vision fallback failed",
            success_message="[agent] awaited background vision fallback result",
        )
        if injected:
            return StopFlowDecision.continue_loop()
    if content_len == 0:
        record_trace(
            key="agent_finish",
            label="Agent 收尾",
            status="warn",
            detail="reason=empty_stop text=[NO_REPLY]",
        )
        return StopFlowDecision.return_result(
            AgentResult(
                text="[NO_REPLY]",
                pending_actions=pending_actions,
            )
        )
    record_trace(
        key="agent_finish",
        label="Agent 收尾",
        status="ok",
        detail=f"reason=model_stop content_len={content_len} has_tool_call={bool(state.has_tool_call)}",
    )
    return StopFlowDecision.return_result(
        AgentResult(
            text=response.content,
            pending_actions=pending_actions,
            bypass_length_limits=state.has_tool_call,
        )
    )


__all__ = [
    "StopFlowDecision",
    "StopFlowState",
    "_has_lookup_schema",
    "_should_review_banter_lookup_draft",
    "handle_model_stop",
]
