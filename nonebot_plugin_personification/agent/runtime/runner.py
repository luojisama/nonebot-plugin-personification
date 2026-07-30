from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable, List

from ..query_rewriter import ContextualQueryRewrite, QueryRewriteContext, contextual_query_rewriter
from ...core.metrics import record_counter, record_timing
from ...core.time_ctx import get_configured_now
from ..tool_registry import ToolRegistry
from ...core.message_parts import extract_text_from_parts
from ...core.web_grounding import merge_grounding_topic
from .constants import (
    DEFAULT_AGENT_MAX_STEPS,
)
from .executor import (
    _execute_tool_with_retries,
    _invoke_tool_handler,
    _remaining_time_budget_seconds,
    _tool_timeout_result,
)
from .evidence import (
    EvidenceSynthesis,
    build_tool_result_record as _build_tool_result_record,
    evidence_synthesizer_enabled as _evidence_synthesizer_enabled,
    plan_for_evidence as _plan_for_evidence,
    render_evidence_guidance as _evidence_guidance,
    synthesize_evidence_with_llm,
)
from .image_generation import (
    _IMAGE_GENERATION_TOOL_NAME,
    _can_start_background_image_generation,
    _extract_image_b64_tool_result,
    _generate_image_generation_status_reply,
    _start_background_image_generation,
)
from .intent import (
    _clean_user_query_text,
    _derive_query_rewrite_context,
    _extract_focus_query_text,
    _extract_group_topic_hint,
    _extract_latest_user_images,
    _extract_latest_user_text,
    _infer_intent_decision_with_context,
    _recover_followup_query_from_context,
    _render_message_text,
)
from .loop_utils import (
    _RETRYABLE_LOOKUP_TOOLS,
    caller_supports_builtin_search as _caller_supports_builtin_search,
    record_reply_trace_stage as _record_reply_trace_stage,
    safe_ack as _safe_ack,
    tool_result_trace_status as _tool_result_trace_status,
)
from .prompting import append_agent_system_prompts
from .reply_quality import finalize_agent_reply_quality
from .stop_flow import (
    StopFlowState,
    _has_lookup_schema,
    _should_review_banter_lookup_draft,
    handle_model_stop,
)
from .tool_loop import (
    append_assistant_tool_calls_message,
    append_tool_result_message,
    observe_model_step,
    selected_tool_names,
    trace_tool_call,
    trace_tool_result,
)
from .tool_args import (
    _query_variants_for_tool,
    _rewrite_tool_args,
    _sanitize_tool_args_for_schema,
    _schema_allowed_parameters,
    _tool_allows_parameter,
)
from .budgeting import apply_agent_budget_profile, derive_agent_budget_profile, render_agent_budget_trace_detail
from .final_synthesis import AgentResult, direct_tool_result_agent_result, synthesize_max_steps_result
from .tool_selection import (
    _normalize_agent_max_steps,
    _schema_tool_name,
    _select_tool_schemas,
)
from .tool_contracts import recommended_tools_for_chat_intent
from .wrappers import (
    _IMAGE_B64_TOOL_RESULT_RE,
    _render_tool_result_for_user,
    _wrap_tool_result_in_persona,
)
from .fallbacks import (
    _cancel_task_safely,
    _parse_json_tool_result,
    _select_semantic_fallback_tool,
    _tool_result_indicates_empty,
)

_TIME_SENSITIVE_SEARCH_TOOLS = frozenset({"web_search", "search_web"})
_TIME_SENSITIVE_RE = re.compile("\u6700\u65b0|\u8fd1\u671f|\u73b0\u5728|\u4eca\u5e74|\u4eca\u5929|\u5f53\u524d|latest|recent|now", re.IGNORECASE)


async def _spawn_active_learning(
    *,
    tool_caller: Any,
    memory_store: Any,
    uncertainty_notes: list[str],
    group_id: str,
    research_followup_query: str,
    plugin_config: Any,
) -> None:
    try:
        from ...core.active_learning import run_active_learning
        await run_active_learning(
            tool_caller=tool_caller,
            memory_store=memory_store,
            uncertainty_notes=uncertainty_notes,
            group_id=group_id,
            research_followup_query=research_followup_query,
            plugin_config=plugin_config,
        )
    except Exception:
        pass


def _maybe_inject_date_to_query(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in _TIME_SENSITIVE_SEARCH_TOOLS:
        return args
    query = str(args.get("query", "") or "").strip()
    if not query or not _TIME_SENSITIVE_RE.search(query):
        return args
    try:
        now = get_configured_now()
        date_str = now.strftime("%Y\u5e74") + str(now.month) + "\u6708" + str(now.day) + "\u65e5"
    except Exception:
        return args
    if date_str in query:
        return args
    return {**args, "query": f"{query} ({date_str})"}


async def _classify_deferred_lookup_reply(
    *,
    tool_caller: Any,
    user_query_text: str,
    assistant_reply_text: str,
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
    timeout: float = 8.0,
) -> bool:
    """Ask the model whether a draft should trigger one more lookup.

    This is a compatibility helper for older tests and callers. It keeps the
    decision model-led: code only parses the model's explicit enum answer.
    """

    if tool_caller is None:
        return False
    messages = [
        {
            "role": "system",
            "content": (
                "判断候选回复是否应该立刻补一次查证。"
                "如果候选回复只是在承诺稍后去查、承认自己没懂但没有查、"
                "反问用户/群友某个专有名词/梗/外号/游戏动漫卡牌术语是什么，"
                "或明显在没有证据时凭印象猜，应严格输出 RETRY_SEARCH。"
                "如果上一轮工具已经查过且结果为空，或候选回复已经能自然作为最终回复，严格输出 FINAL_ANSWER。"
                "只输出这两个枚举之一。"
                "按语义判断，不要按固定关键词机械判断。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户问题：{str(user_query_text or '').strip()[:300]}\n"
                f"候选回复：{str(assistant_reply_text or '').strip()[:300]}\n"
                f"上一个工具：{str(previous_tool_name or '').strip()[:80] or '[无]'}\n"
                f"上一个工具结果：{str(previous_tool_result_text or '').strip()[:500] or '[无]'}"
            ),
        },
    ]
    try:
        response = await asyncio.wait_for(
            tool_caller.chat_with_tools(messages, [], False),
            timeout=timeout,
        )
    except Exception:
        return False
    verdict = str(getattr(response, "content", "") or "").strip().upper()
    return verdict == "RETRY_SEARCH"


async def run_agent(
    messages: List[dict],
    registry: ToolRegistry,
    tool_caller: Any,
    executor: Any,
    plugin_config: Any,
    logger: Any,
    max_steps: int | None = None,
    current_image_urls: List[str] | None = None,
    direct_image_input: bool = False,
    query_rewrite_context: QueryRewriteContext | None = None,
    repeat_clusters: list[dict[str, Any]] | None = None,
    relationship_hint: str = "",
    recent_bot_replies: list[str] | None = None,
    precomputed_intent: Any = None,
    turn_plan: Any = None,
    candidate_memories: list[dict[str, Any]] | None = None,
    url_summaries: list[str] | None = None,
    quote_chain: list[dict[str, Any]] | None = None,
    time_budget_seconds: float | None = None,
    ack_sender: Callable[[str], Awaitable[None]] | None = None,
) -> AgentResult:
    use_builtin_search = (
        bool(
            getattr(
                plugin_config,
                "personification_model_builtin_search_enabled",
                getattr(plugin_config, "personification_builtin_search", True),
            )
        )
        and _caller_supports_builtin_search(tool_caller)
    )
    pending_actions: List[dict] = []
    bind_actions = getattr(executor, "bind_pending_actions", None)
    if callable(bind_actions):
        bind_actions(pending_actions)
    stop_state = StopFlowState()

    async def _finalize_result(result: AgentResult, *, reason: str) -> AgentResult:
        return await finalize_agent_reply_quality(
            result,
            tool_caller=tool_caller,
            messages=messages,
            turn_plan=turn_plan,
            record_trace=_record_reply_trace_stage,
            logger=logger,
            reason=reason,
        )

    evidence_synthesis_rounds = 0
    last_evidence_tool_count = 0
    max_evidence_synthesis_rounds = 2
    user_text = _extract_latest_user_text(messages)
    focus_query_text = _clean_user_query_text(_extract_focus_query_text(user_text))
    contextual_query_text = _recover_followup_query_from_context(user_text, focus_query_text)
    context_hint = _extract_group_topic_hint(messages)
    user_images = list(current_image_urls or [])
    if not user_images:
        user_images = _extract_latest_user_images(messages)
    preliminary_query_text = _clean_user_query_text(
        contextual_query_text
        or focus_query_text
        or user_text
    )
    if precomputed_intent is not None:
        intent_decision = precomputed_intent
        logger.debug("[agent] using precomputed intent_decision, skipping LLM inference")
        _record_reply_trace_stage(
            key="agent_intent",
            label="Agent 意图判别",
            status="info",
            detail=(
                f"precomputed=true intent={getattr(intent_decision, 'chat_intent', '')} "
                f"ambiguity={getattr(intent_decision, 'ambiguity_level', '')}"
            ),
        )
    else:
        intent_started_at = time.monotonic()
        intent_decision = await _infer_intent_decision_with_context(
            preliminary_query_text or user_text,
            messages,
            tool_caller=tool_caller,
            repeat_clusters=repeat_clusters,
            relationship_hint=relationship_hint,
            recent_bot_replies=recent_bot_replies,
        )
        intent_elapsed_ms = int((time.monotonic() - intent_started_at) * 1000)
        record_timing("agent.intent_ms", intent_elapsed_ms)
        _record_reply_trace_stage(
            key="agent_intent",
            label="Agent 意图判别",
            status="ok",
            detail=(
                f"intent={getattr(intent_decision, 'chat_intent', '')} "
                f"ambiguity={getattr(intent_decision, 'ambiguity_level', '')} "
                f"elapsed_ms={intent_elapsed_ms}"
            ),
        )
    chat_intent = intent_decision.chat_intent
    plugin_query_intent = intent_decision.plugin_question_intent if chat_intent == "plugin_question" else ""
    runtime_chat_intent = chat_intent
    evidence_turn_plan = _plan_for_evidence(turn_plan, intent_decision, has_images=bool(user_images))
    effective_max_steps = _normalize_agent_max_steps(
        max_steps if max_steps is not None else getattr(plugin_config, "personification_agent_max_steps", DEFAULT_AGENT_MAX_STEPS)
    )
    budget_profile = derive_agent_budget_profile(
        turn_plan=turn_plan,
        intent_decision=intent_decision,
        actual_max_steps=effective_max_steps,
        actual_time_budget_seconds=time_budget_seconds,
    )
    budget_mode = getattr(plugin_config, "personification_agent_budget_mode", "shadow")
    effective_max_steps, time_budget_seconds, budget_profile, budget_applied = apply_agent_budget_profile(
        budget_profile,
        mode=budget_mode,
        actual_max_steps=effective_max_steps,
        actual_time_budget_seconds=time_budget_seconds,
    )
    record_counter(
        "agent.budget_profile_total",
        mode=budget_profile.mode,
        source=budget_profile.source,
    )
    if budget_profile.suggested_time_budget_seconds is not None:
        record_timing(
            "agent.budget_suggested_ms",
            budget_profile.suggested_time_budget_seconds * 1000.0,
            mode=budget_profile.mode,
        )
    _record_reply_trace_stage(
        key="agent_budget",
        label="Agent 预算模式",
        status="info",
        detail=render_agent_budget_trace_detail(
            budget_profile,
            actual_max_steps=effective_max_steps,
            actual_time_budget_seconds=time_budget_seconds,
        ),
        hint=(
            "当前已按 adaptive 模式接管本轮 Agent 步数和剩余秒数"
            if budget_applied
            else "当前仅 shadow 观测，不直接改变生产超时或工具步数"
        ),
    )
    _record_reply_trace_stage(
        key="agent_start",
        label="Agent 开始",
        status="info",
        detail=(
            f"max_steps={effective_max_steps} builtin_search={bool(use_builtin_search)} "
            f"images={len(user_images)} caller={type(tool_caller).__name__}"
        ),
    )
    rewrite_context = _derive_query_rewrite_context(
        messages,
        current_images=user_images,
        provided=query_rewrite_context,
    )
    if runtime_chat_intent in {"banter", "image_generation", "expression"}:
        rewritten_query = ContextualQueryRewrite(
            primary_query=preliminary_query_text,
            query_candidates=[preliminary_query_text] if preliminary_query_text else [],
            context_clues=[],
            need_image_understanding=bool(user_images),
            recommended_tools=recommended_tools_for_chat_intent(registry, runtime_chat_intent),
            search_plan=[],
        )
        _record_reply_trace_stage(
            key="agent_query_rewrite",
            label="Agent 查询改写",
            status="info",
            detail=f"skipped=true intent={runtime_chat_intent or '-'}",
        )
    else:
        rewrite_started_at = time.monotonic()
        rewritten_query = await contextual_query_rewriter(
            tool_caller=tool_caller,
            history_new=rewrite_context.history_new,
            history_last=rewrite_context.history_last,
            trigger_reason=rewrite_context.trigger_reason,
            images=rewrite_context.images,
            quoted_message=rewrite_context.quoted_message,
            topic_hint=context_hint,
        )
        rewrite_elapsed_ms = int((time.monotonic() - rewrite_started_at) * 1000)
        record_timing("agent.query_rewrite_ms", rewrite_elapsed_ms, intent=runtime_chat_intent or "unknown")
        _record_reply_trace_stage(
            key="agent_query_rewrite",
            label="Agent 查询改写",
            status="ok",
            detail=f"intent={runtime_chat_intent or '-'} elapsed_ms={rewrite_elapsed_ms}",
        )
    effective_query_text = (
        rewritten_query.primary_query
        or contextual_query_text
        or focus_query_text
        or rewrite_context.history_last
    )
    user_query_text = _clean_user_query_text(
        merge_grounding_topic(
            effective_query_text,
            context_hint,
        )
    )
    ack_sent = False
    budget_deadline = (
        time.monotonic() + max(0.0, float(time_budget_seconds or 0.0))
        if time_budget_seconds is not None
        else None
    )
    background_image_request = user_query_text or preliminary_query_text or user_text
    if (
        runtime_chat_intent == "image_generation"
        and bool(getattr(plugin_config, "personification_image_gen_background_enabled", True))
        and _can_start_background_image_generation(
            registry=registry,
            executor=executor,
            user_request=background_image_request,
        )
    ):
        status_reply = await _generate_image_generation_status_reply(
            tool_caller=tool_caller,
            messages=messages,
            user_request=background_image_request,
            logger=logger,
        )
        if _start_background_image_generation(
            registry=registry,
            executor=executor,
            tool_caller=tool_caller,
            messages=messages,
            user_request=background_image_request,
            rewritten_query=rewritten_query,
            user_images=user_images,
            use_builtin_search=use_builtin_search,
            logger=logger,
        ):
            return await _finalize_result(
                AgentResult(
                    text=status_reply or "[NO_REPLY]",
                    pending_actions=pending_actions,
                    direct_output=False,
                    bypass_length_limits=False,
                ),
                reason="background_image_generation",
            )
    append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent=runtime_chat_intent,
        plugin_query_intent=plugin_query_intent,
        intent_decision=intent_decision,
        rewritten_query=rewritten_query,
        turn_plan=turn_plan,
        user_images=user_images,
        direct_image_input=direct_image_input,
    )

    async def _append_evidence_guidance_if_needed(*, draft_answer_text: str = "") -> EvidenceSynthesis | None:
        nonlocal evidence_synthesis_rounds, last_evidence_tool_count
        if not _evidence_synthesizer_enabled(plugin_config):
            return None
        if evidence_synthesis_rounds >= max_evidence_synthesis_rounds:
            return None
        if not stop_state.tool_result_records or len(stop_state.tool_result_records) <= last_evidence_tool_count:
            return None
        started_at = time.monotonic()
        evidence = await synthesize_evidence_with_llm(
            tool_caller=tool_caller,
            turn_plan=evidence_turn_plan,
            candidate_memories=list(candidate_memories or [])[:12],
            tool_results=stop_state.tool_result_records[:8],
            draft_answer_text=draft_answer_text,
            url_summaries=list(url_summaries or [])[:5],
            group_context=context_hint,
            quote_chain=list(quote_chain or [])[:8],
            cross_verify_enabled=bool(getattr(plugin_config, "personification_cross_verify_enabled", False)),
        )
        evidence_synthesis_rounds += 1
        last_evidence_tool_count = len(stop_state.tool_result_records)
        record_counter(
            "evidence_synthesizer.synthesis_total",
            needs_more_research=bool(evidence.needs_more_research),
            memory_style=evidence.memory_inject_style,
        )
        record_timing(
            "evidence_synthesizer.synthesis_ms",
            (time.monotonic() - started_at) * 1000.0,
        )
        messages.append({"role": "system", "content": _evidence_guidance(evidence)})
        if evidence.needs_more_research:
            stop_state.semantic_fallback_attempted = False
            stop_state.pending_evidence_followup_query = str(evidence.research_followup_query or "").strip()
        logger.info(
            "[agent] evidence synthesis "
            f"round={evidence_synthesis_rounds} selected_memories={len(evidence.selected_memory_ids)} "
            f"needs_more_research={evidence.needs_more_research}"
        )
        if evidence.uncertainty_notes and bool(getattr(plugin_config, "personification_active_learning_enabled", False)):
            try:
                group_scope = str(context_hint or "").replace("group:", "").split(",")[0].strip() or "unknown"
                _learning_query = str(evidence.research_followup_query or evidence.uncertainty_notes[0]).strip()
                asyncio.create_task(
                    _spawn_active_learning(
                        tool_caller=tool_caller,
                        memory_store=getattr(executor, "memory_store", None),
                        uncertainty_notes=evidence.uncertainty_notes,
                        group_id=group_scope,
                        research_followup_query=_learning_query,
                        plugin_config=plugin_config,
                    )
                )
            except Exception:
                pass
        return evidence

    for _step in range(effective_max_steps):
        if budget_deadline is not None and time.monotonic() >= budget_deadline:
            logger.warning(
                f"[agent] time budget exhausted at step={_step + 1}, "
                f"forcing answer from last_tool_result={bool(stop_state.last_tool_result_text)}"
            )
            if stop_state.last_tool_result_text:
                return await _finalize_result(
                    await synthesize_max_steps_result(
                        registry=registry,
                        tool_name=stop_state.last_tool_name,
                        result_text=stop_state.last_tool_result_text,
                        user_query_text=user_query_text,
                        messages=messages,
                        pending_actions=pending_actions,
                        tool_caller=tool_caller,
                        turn_plan=turn_plan,
                    ),
                    reason="time_budget_last_tool",
                )
            return await _finalize_result(
                AgentResult(
                    text="[NO_REPLY]",
                    pending_actions=pending_actions,
                ),
                reason="time_budget_empty",
            )
        await _append_evidence_guidance_if_needed()
        active_schemas = _select_tool_schemas(
            registry,
            has_images=bool(user_images),
            chat_intent=runtime_chat_intent,
            plugin_question_intent=plugin_query_intent,
        )
        selected_names = selected_tool_names(active_schemas, _schema_tool_name)
        logger.debug(f"[agent] exposed {len(active_schemas)} tools to model")
        logger.info(f"[agent] selected tools: {', '.join(selected_names) if selected_names else 'none'}")
        model_started_at = time.monotonic()
        response = await tool_caller.chat_with_tools(
            messages,
            active_schemas,
            use_builtin_search,
        )
        model_elapsed_ms = int((time.monotonic() - model_started_at) * 1000)
        content_len = observe_model_step(
            response=response,
            tool_caller=tool_caller,
            logger=logger,
            step=_step + 1,
            selected_names=selected_names,
            runtime_chat_intent=runtime_chat_intent,
            model_elapsed_ms=model_elapsed_ms,
            record_trace=_record_reply_trace_stage,
        )
        if response.finish_reason == "stop" and not response.tool_calls and content_len > 0:
            if _evidence_synthesizer_enabled(plugin_config) and stop_state.has_tool_call:
                await _append_evidence_guidance_if_needed(draft_answer_text=str(response.content or ""))
        if response.finish_reason == "stop":
            stop_decision = await handle_model_stop(
                state=stop_state,
                response=response,
                content_len=content_len,
                active_schemas=active_schemas,
                runtime_chat_intent=runtime_chat_intent,
                intent_decision=intent_decision,
                registry=registry,
                tool_caller=tool_caller,
                logger=logger,
                messages=messages,
                pending_actions=pending_actions,
                plugin_config=plugin_config,
                user_query_text=user_query_text,
                user_text=user_text,
                user_images=user_images,
                rewritten_query=rewritten_query,
                context_hint=context_hint,
                plugin_query_intent=plugin_query_intent,
                budget_deadline=budget_deadline,
                step=_step + 1,
                record_trace=_record_reply_trace_stage,
                append_evidence_guidance=_append_evidence_guidance_if_needed,
                classify_deferred_lookup_reply=_classify_deferred_lookup_reply,
                select_semantic_fallback_tool=_select_semantic_fallback_tool,
            )
            if stop_decision.action == "continue":
                continue
            if stop_decision.result is not None:
                return await _finalize_result(stop_decision.result, reason="model_stop")

        if response.tool_calls:
            if not stop_state.has_tool_call and not ack_sent and ack_sender is not None:
                ack_sent = True
                await _safe_ack(ack_sender, "", logger)
            append_assistant_tool_calls_message(messages=messages, response=response)

        for tool_call in response.tool_calls:
            stop_state.has_tool_call = True
            logger.info(f"[agent] tool_call name={tool_call.name}")
            tool = registry.get(tool_call.name)
            tool_args = trace_tool_call(
                tool_call=tool_call,
                step=_step + 1,
                record_trace=_record_reply_trace_stage,
            )
            tool_started_at = time.monotonic()
            if tool is None:
                result = f"工具 {tool_call.name} 不存在"
            else:
                tool_args, result = await _execute_tool_with_retries(
                    registry=registry,
                    tool_name=tool_call.name,
                    tool_args=tool_args,
                    rewritten_query=rewritten_query,
                    user_images=user_images,
                    previous_tool_name=stop_state.last_tool_name,
                    previous_tool_result_text=stop_state.last_tool_result_text,
                    logger=logger,
                    budget_deadline=budget_deadline,
                )
            tool_elapsed_ms = int((time.monotonic() - tool_started_at) * 1000)
            trace_tool_result(
                tool_name=str(tool_call.name or "").strip(),
                result=result,
                step=_step + 1,
                elapsed_ms=tool_elapsed_ms,
                record_trace=_record_reply_trace_stage,
                status_for_result=_tool_result_trace_status,
            )
            tool_result_preview_limit = (
                1000
                if str(tool_call.name or "").strip() == _IMAGE_GENERATION_TOOL_NAME
                else 220
            )
            logger.info(
                f"[agent] tool_result name={tool_call.name} "
                f"preview={str(result).replace(chr(10), ' ')[:tool_result_preview_limit]}"
            )
            stop_state.last_tool_name = str(tool_call.name or "").strip()
            if str(result or "").strip():
                stop_state.last_tool_result_text = str(result).strip()
            stop_state.tool_result_records.append(
                _build_tool_result_record(
                    tool_name=stop_state.last_tool_name,
                    tool_args=tool_args,
                    result=result,
                )
            )
            if stop_state.last_tool_name in _RETRYABLE_LOOKUP_TOOLS:
                if _tool_result_indicates_empty(result):
                    stop_state.empty_lookup_tools.add(stop_state.last_tool_name)
                else:
                    stop_state.empty_lookup_tools.discard(stop_state.last_tool_name)
            stop_state.semantic_fallback_attempted = False
            direct_result = direct_tool_result_agent_result(
                registry=registry,
                tool_name=stop_state.last_tool_name,
                result_text=result,
                pending_actions=pending_actions,
            )
            if direct_result is not None:
                _record_reply_trace_stage(
                    key="agent_finish",
                    label="Agent 收尾",
                    status="ok",
                    detail=f"reason=direct_tool_result tool={stop_state.last_tool_name}",
                )
                return await _finalize_result(direct_result, reason="direct_tool_result")

            append_tool_result_message(
                messages=messages,
                tool_caller=tool_caller,
                tool_call=tool_call,
                result=result,
            )
            await _append_evidence_guidance_if_needed()

    logger.warning("[agent] MAX_STEPS reached")
    _record_reply_trace_stage(
        key="agent_max_steps",
        label="Agent 步数上限",
        status="warn",
        detail=f"max_steps={effective_max_steps} last_tool={stop_state.last_tool_name or '-'}",
    )
    if stop_state.last_tool_result_text:
        logger.warning("[agent] using last tool result as fallback final answer")
        return await _finalize_result(
            await synthesize_max_steps_result(
                registry=registry,
                tool_name=stop_state.last_tool_name,
                result_text=stop_state.last_tool_result_text,
                user_query_text=user_query_text,
                messages=messages,
                pending_actions=pending_actions,
                tool_caller=tool_caller,
                turn_plan=turn_plan,
            ),
            reason="max_steps_last_tool",
        )
    return await _finalize_result(
        AgentResult(
            text="[NO_REPLY]",
            pending_actions=pending_actions,
        ),
        reason="max_steps_empty",
    )
