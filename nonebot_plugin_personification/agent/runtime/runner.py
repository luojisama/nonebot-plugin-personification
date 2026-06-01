from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List

from ..query_rewriter import ContextualQueryRewrite, QueryRewriteContext, contextual_query_rewriter
from ...core.error_utils import log_exception
from ...core.metrics import record_counter, record_timing
from ...core.time_ctx import get_configured_now
from ..tool_registry import ToolRegistry
from ...core.message_parts import extract_text_from_parts
from ...core.web_grounding import merge_grounding_topic
from .constants import (
    DEFAULT_AGENT_MAX_STEPS,
)
from .executor import _execute_tool_with_retries
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
from .tool_args import (
    _query_variants_for_tool,
    _rewrite_tool_args,
    _sanitize_tool_args_for_schema,
    _schema_allowed_parameters,
    _tool_allows_parameter,
)
from .tool_selection import (
    _normalize_agent_max_steps,
    _schema_tool_name,
    _select_tool_schemas,
    _semantic_tool_guidance,
)
from .wrappers import (
    _IMAGE_B64_TOOL_RESULT_RE,
    _extract_persona_system_prompt,
    _format_image_generation_failure,
    _is_direct_media_tool_result,
    _render_tool_result_for_user,
    _wrap_tool_result_in_persona,
)
from .fallbacks import (
    _cancel_task_safely,
    _inject_background_tool_result,
    _parse_json_tool_result,
    _run_background_vision_fallback,
    _select_semantic_fallback_tool,
    _tool_result_indicates_empty,
)


_QUERY_REWRITE_TOOL_NAMES = frozenset(
    {
        "parallel_research",
        "web_search",
        "search_web",
        "wiki_lookup",
        "resolve_acg_entity",
        "vision_analyze",
        "analyze_image",
        "collect_resources",
        "search_images",
    }
)
_RETRYABLE_LOOKUP_TOOLS = frozenset(
    {"parallel_research", "web_search", "search_web", "wiki_lookup", "resolve_acg_entity", "collect_resources", "search_images"}
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
_PLUGIN_KNOWLEDGE_TOOL_NAMES = frozenset(
    {"search_plugin_knowledge", "search_plugin_source", "list_plugins", "list_plugin_features", "get_feature_detail"}
)
_PLUGIN_LATEST_EXTRA_TOOL_NAMES = frozenset({"web_search", "search_official_site", "search_github_repos"})
_NETWORK_TOOL_NAMES = frozenset(
    {
        "parallel_research",
        "web_search",
        "search_web",
        "multi_search_engine",
        "collect_resources",
        "search_images",
        "search_official_site",
        "search_github_repos",
        "wiki_lookup",
        "get_baike_entry",
        "get_daily_news",
        "get_ai_news",
        "get_trending",
        "get_history_today",
        "get_epic_games",
        "get_gold_price",
        "get_exchange_rate",
        "weather",
    }
)
_BANTER_BLOCKED_TOOL_NAMES = frozenset(
    set(_NETWORK_TOOL_NAMES)
    | set(_PLUGIN_KNOWLEDGE_TOOL_NAMES)
    | {"vision_analyze", "analyze_image", "resolve_acg_entity"}
)
_BUILTIN_SEARCH_CALLER_NAMES = frozenset({
    "GeminiToolCaller",
    "AnthropicToolCaller",
    "OpenAICodexToolCaller",
    # 三个 CLI 协议 caller 内部也已注入对应原生 search 工具（impl.py:3203/3741/4005）
    "GeminiCliToolCaller",
    "AntigravityCliToolCaller",
    "ClaudeCodeToolCaller",
})


@dataclass
class AgentResult:
    text: str
    pending_actions: List[dict]
    direct_output: bool = False
    bypass_length_limits: bool = False


def _summarize_tool_response_raw(raw: Any) -> str:
    if not isinstance(raw, dict):
        if raw is None:
            return "raw=none"
        return f"raw_type={type(raw).__name__}"

    output = raw.get("output", [])
    if isinstance(output, list):
        output_items = len(output)
        output_types = ",".join(
            str(item.get("type", "?"))
            for item in output[:3]
            if isinstance(item, dict)
        ) or "none"
    else:
        output_items = "n/a"
        output_types = "n/a"
    usage = raw.get("usage", {})
    output_tokens = usage.get("output_tokens", "?") if isinstance(usage, dict) else "?"
    status = raw.get("status", "?")
    model = raw.get("model", "?")
    return (
        f"status={status} model={model} output_items={output_items} "
        f"output_types={output_types} output_tokens={output_tokens}"
    )


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




def _direct_tool_result_agent_result(
    *,
    tool_name: str,
    result_text: str,
    pending_actions: List[dict],
) -> "AgentResult | None":
    text = str(result_text or "").strip()
    normalized_tool_name = str(tool_name or "").strip()
    if _is_direct_media_tool_result(normalized_tool_name, text):
        return AgentResult(
            text=text,
            pending_actions=pending_actions,
            direct_output=False,
            bypass_length_limits=True,
        )
    if normalized_tool_name == _IMAGE_GENERATION_TOOL_NAME:
        return AgentResult(
            text=_format_image_generation_failure(text),
            pending_actions=pending_actions,
            direct_output=False,
            bypass_length_limits=False,
        )
    return None


def _tool_signature(tool_name: str, tool_args: dict[str, Any]) -> str:
    return (
        f"{str(tool_name or '').strip()}:"
        f"{json.dumps(tool_args or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    )


def _caller_supports_builtin_search(tool_caller: Any) -> bool:
    return tool_caller.__class__.__name__ in _BUILTIN_SEARCH_CALLER_NAMES


async def _safe_ack(
    ack_sender: Callable[[str], Awaitable[None]],
    text: str,
    logger: Any,
) -> None:
    try:
        await ack_sender(text)
    except Exception as exc:
        logger.debug(f"[agent] ack send failed: {exc}")


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
    last_tool_name = ""
    last_tool_result_text = ""
    last_fallback_signature = ""
    empty_lookup_tools: set[str] = set()
    semantic_fallback_attempted = False
    tool_result_records: list[dict[str, Any]] = []
    evidence_synthesis_rounds = 0
    last_evidence_tool_count = 0
    max_evidence_synthesis_rounds = 2
    pending_evidence_followup_query = ""
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
    else:
        intent_decision = await _infer_intent_decision_with_context(
            preliminary_query_text or user_text,
            messages,
            tool_caller=tool_caller,
            repeat_clusters=repeat_clusters,
            relationship_hint=relationship_hint,
            recent_bot_replies=recent_bot_replies,
        )
    chat_intent = intent_decision.chat_intent
    plugin_query_intent = intent_decision.plugin_question_intent if chat_intent == "plugin_question" else ""
    runtime_chat_intent = chat_intent
    evidence_turn_plan = _plan_for_evidence(turn_plan, intent_decision, has_images=bool(user_images))
    effective_max_steps = _normalize_agent_max_steps(
        max_steps if max_steps is not None else getattr(plugin_config, "personification_agent_max_steps", DEFAULT_AGENT_MAX_STEPS)
    )
    rewrite_context = _derive_query_rewrite_context(
        messages,
        current_images=user_images,
        provided=query_rewrite_context,
    )
    if runtime_chat_intent in {"banter", "image_generation"}:
        rewritten_query = ContextualQueryRewrite(
            primary_query=preliminary_query_text,
            query_candidates=[preliminary_query_text] if preliminary_query_text else [],
            context_clues=[],
            need_image_understanding=bool(user_images),
            recommended_tools=["generate_image"] if runtime_chat_intent == "image_generation" else [],
            search_plan=[],
        )
    else:
        rewritten_query = await contextual_query_rewriter(
            tool_caller=tool_caller,
            history_new=rewrite_context.history_new,
            history_last=rewrite_context.history_last,
            trigger_reason=rewrite_context.trigger_reason,
            images=rewrite_context.images,
            quoted_message=rewrite_context.quoted_message,
            topic_hint=context_hint,
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
    has_tool_call = False
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
            return AgentResult(
                text=status_reply or "[NO_REPLY]",
                pending_actions=pending_actions,
                direct_output=False,
                bypass_length_limits=False,
            )
    messages.append(
        {
            "role": "system",
            "content": _semantic_tool_guidance(),
        }
    )
    messages.append(
        {
            "role": "system",
            "content": (
                "最终对用户的回复必须自然、像群聊里的活人接话。"
                "不要暴露工具、检索、看图、回忆这些中间步骤。"
                "遇到不确定或有歧义时，优先查证或承认不确定，不要硬猜。"
            ),
        }
    )
    messages.append(
        {
            "role": "system",
            "content": (
                "群聊里通常多个话题并行：A 群友讨论地震、B 群友讨论自己的近况、C 群友在闲扯，"
                "时间相近不代表语义相关。\n"
                "硬性规则：\n"
                "1. 你回复的是上下文中标记为「当前消息」的那一条；其它发言只是背景，不要把它们的内容拿来回答当前问题。\n"
                "2. 不要把不同人说的关键词（地名、人名、状态）跨话题拼接。"
                "比如 A 在说地震位置是「广西柳州」，同时 B 在说「我家在浙江」，"
                "当 C 问「这次地震严重吗」时，你只能基于 A 的位置信息回答，绝不能说「浙江有震感」。\n"
                "3. 引用某人状态前先问自己：这个状态是不是当前消息的语境？如果不是，就不要写进去。\n"
                "4. 拿不准时宁可简短、含糊或承认不知道，也不要把无关上下文糊上去。"
            ),
        }
    )
    if runtime_chat_intent == "banter":
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前更像接梗、吐槽、复读或顺嘴接话场景。"
                    "优先短句自然接话，不要进入解释、定义、考据或检索腔。"
                ),
            }
        )
    elif runtime_chat_intent == "image_generation":
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前用户是在要求生成图片。必须调用 generate_image 工具，"
                    "不要只回复提示词、描述或制作步骤。"
                ),
            }
        )
    elif runtime_chat_intent == "plugin_question":
        plugin_hint = (
            "当前更像在问插件能力、命令、实现或配置。"
            "如果需要工具，优先使用本地插件知识和源码工具，不要先联网。"
            "优先考虑：search_plugin_source、search_plugin_knowledge、list_plugin_features、get_feature_detail、list_plugins。"
        )
        if plugin_query_intent == "latest":
            plugin_hint += (
                "如果对方明确问官网、仓库、最新文档或版本，再考虑 web_search、search_official_site、search_github_repos。"
            )
        messages.append(
            {
                "role": "system",
                "content": plugin_hint,
            }
        )
    if intent_decision.ambiguity_level == "high":
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前这句里有高歧义名词/对象，容易误解。"
                    "如果上下文和工具证据仍不足，请优先承认不确定；群聊里若没人明确在 cue 你，也可以输出 [NO_REPLY]。"
                ),
            }
        )
    if rewritten_query.primary_query:
        messages.append(
            {
                "role": "system",
                "content": (
                    f"当前检索意图主查询：{rewritten_query.primary_query}\n"
                    + (
                        f"候选查询：{'；'.join(rewritten_query.query_candidates[:4])}\n"
                        if rewritten_query.query_candidates else ""
                    )
                    + (
                        f"上下文线索：{'；'.join(rewritten_query.context_clues[:4])}\n"
                        if rewritten_query.context_clues else ""
                    )
                    + (
                        f"检索计划：{'；'.join(rewritten_query.search_plan[:3])}\n"
                        if rewritten_query.search_plan else ""
                    )
                    + "如果需要调用 web_search/wiki_lookup/resolve_acg_entity/vision_analyze，优先使用这些检索词，"
                    + "不要直接拿用户最后一句口语补充当 query。"
                    + "工具优先级由你结合这份计划和当前证据自主判断。"
                ),
            }
    )
    if user_images:
        if direct_image_input:
            image_prompt = (
                "如果当前消息包含图片输入，请直接结合图片和文字理解用户意图。"
                "如果你只看到图片占位或视觉摘要，不要声称自己直接看到了原图。"
                "必要时可以调用视觉分析工具进一步分析图片。"
            )
        else:
            image_prompt = (
                "当前轮包含图片相关上下文，但你不一定直接收到了原图。"
                "如果你看到的是图片占位或视觉摘要，请把它当作摘要，不要声称自己直接看到了原图。"
                "必要时可以调用视觉分析工具进一步分析图片。"
            )
        messages.append(
            {
                "role": "system",
                "content": image_prompt,
            }
        )

    async def _append_evidence_guidance_if_needed(*, draft_answer_text: str = "") -> EvidenceSynthesis | None:
        nonlocal evidence_synthesis_rounds, last_evidence_tool_count, semantic_fallback_attempted, pending_evidence_followup_query
        if not _evidence_synthesizer_enabled(plugin_config):
            return None
        if evidence_synthesis_rounds >= max_evidence_synthesis_rounds:
            return None
        if not tool_result_records or len(tool_result_records) <= last_evidence_tool_count:
            return None
        started_at = time.monotonic()
        evidence = await synthesize_evidence_with_llm(
            tool_caller=tool_caller,
            turn_plan=evidence_turn_plan,
            candidate_memories=list(candidate_memories or [])[:12],
            tool_results=tool_result_records[:8],
            draft_answer_text=draft_answer_text,
            url_summaries=list(url_summaries or [])[:5],
            group_context=context_hint,
            quote_chain=list(quote_chain or [])[:8],
            cross_verify_enabled=bool(getattr(plugin_config, "personification_cross_verify_enabled", False)),
        )
        evidence_synthesis_rounds += 1
        last_evidence_tool_count = len(tool_result_records)
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
            semantic_fallback_attempted = False
            pending_evidence_followup_query = str(evidence.research_followup_query or "").strip()
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
                f"forcing answer from last_tool_result={bool(last_tool_result_text)}"
            )
            if last_tool_result_text:
                direct_result = _direct_tool_result_agent_result(
                    tool_name=last_tool_name,
                    result_text=last_tool_result_text,
                    pending_actions=pending_actions,
                )
                if direct_result is not None:
                    return direct_result
                rendered_tool_result = _render_tool_result_for_user(
                    last_tool_name,
                    last_tool_result_text,
                    user_query_text,
                )
                return AgentResult(
                    text=await _wrap_tool_result_in_persona(
                        tool_caller=tool_caller,
                        rendered_tool_result=rendered_tool_result,
                        user_query_text=user_query_text,
                        persona_system=_extract_persona_system_prompt(messages),
                        turn_plan=turn_plan,
                    ),
                    pending_actions=pending_actions,
                    direct_output=False,
                    bypass_length_limits=False,
                )
            return AgentResult(
                text="[NO_REPLY]",
                pending_actions=pending_actions,
            )
        await _append_evidence_guidance_if_needed()
        active_schemas = _select_tool_schemas(
            registry,
            has_images=bool(user_images),
            chat_intent=runtime_chat_intent,
            plugin_question_intent=plugin_query_intent,
        )
        selected_names = [
            _schema_tool_name(schema)
            for schema in active_schemas
            if _schema_tool_name(schema)
        ]
        logger.debug(f"[agent] exposed {len(active_schemas)} tools to model")
        logger.info(f"[agent] selected tools: {', '.join(selected_names) if selected_names else 'none'}")
        model_started_at = time.monotonic()
        response = await tool_caller.chat_with_tools(
            messages,
            active_schemas,
            use_builtin_search,
        )
        model_elapsed_ms = int((time.monotonic() - model_started_at) * 1000)
        try:
            usage = getattr(response, "usage", None) or {}
            if isinstance(usage, dict) and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
                from ...core import llm_context as _llm_ctx
                from ...core import token_ledger as _ledger

                ctx = _llm_ctx.current_llm_context()
                # 从 tool_caller 类名推导 provider，比从 model 名推导更准确
                # （特别针对 Codex 使用 chatgpt OAuth、model 字段含 "gpt" 易误判的情况）
                caller_cls = type(tool_caller).__name__.lower()
                if "codex" in caller_cls:
                    provider_label = "codex"
                elif "anthropic" in caller_cls or "claudecode" in caller_cls or "claude" in caller_cls:
                    provider_label = "anthropic"
                elif "geminicli" in caller_cls:
                    provider_label = "gemini"
                elif "gemini" in caller_cls:
                    provider_label = "gemini"
                elif "openai" in caller_cls:
                    provider_label = "openai"
                else:
                    provider_label = ""  # 让 token_ledger 从 model 名自行推导
                _ledger.record_llm_call(
                    model=str(getattr(response, "model_used", "") or ""),
                    prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                    group_id=str(ctx.get("group_id", "") or ""),
                    user_id=str(ctx.get("user_id", "") or ""),
                    purpose=str(ctx.get("purpose", "") or "agent"),
                    provider=provider_label,
                )
        except Exception:
            pass
        content_len = len(str(response.content or "").strip())
        logger.info(
            f"[agent] step={_step + 1} finish_reason={response.finish_reason} "
            f"tool_calls={len(response.tool_calls)} content_len={content_len} "
            f"model_elapsed_ms={model_elapsed_ms}"
        )
        if response.finish_reason == "stop" and not response.tool_calls and content_len == 0:
            logger.warning(
                "[agent] provider returned empty stop response "
                + _summarize_tool_response_raw(response.raw)
            )
        if response.finish_reason == "stop" and not response.tool_calls and content_len > 0:
            if _evidence_synthesizer_enabled(plugin_config) and has_tool_call:
                await _append_evidence_guidance_if_needed(draft_answer_text=str(response.content or ""))
        if response.finish_reason == "stop":
            if runtime_chat_intent == "banter" and not response.tool_calls and content_len > 0:
                return AgentResult(
                    text=str(response.content or "").strip(),
                    pending_actions=pending_actions,
                    bypass_length_limits=False,
                )
            if (
                response.vision_unavailable
                and bool(
                    getattr(
                        plugin_config,
                        "personification_fallback_enabled",
                        getattr(plugin_config, "personification_vision_fallback_enabled", True),
                    )
                )
                and registry.get("vision_analyze") is not None
            ):
                try:
                    background = await _run_background_vision_fallback(
                        registry=registry,
                        query=user_query_text or user_text or "请分析图片",
                        images=user_images,
                    )
                except Exception as e:
                    logger.warning(f"[agent] vision fallback failed: {e}")
                    background = None
                if background is not None:
                    bg_name, bg_args, bg_result = background
                    await _inject_background_tool_result(
                        messages=messages,
                        tool_caller=tool_caller,
                        tool_name=bg_name,
                        tool_args=bg_args,
                        result=bg_result,
                        step=_step + 1,
                    )
                    has_tool_call = True
                    last_tool_name = bg_name
                    last_tool_result_text = bg_result
                    logger.info("[agent] injected background vision fallback result")
                    continue
            fallback_lookup = None
            previous_tool_empty = _tool_result_indicates_empty(last_tool_result_text)
            should_run_fallback_lookup = (
                runtime_chat_intent != "banter"
                and not semantic_fallback_attempted
                and bool(user_query_text)
                and (
                    not has_tool_call
                    or bool(pending_evidence_followup_query)
                    or content_len == 0
                    or response.vision_unavailable
                )
            )
            if should_run_fallback_lookup:
                semantic_fallback_attempted = True
                fallback_query_text = pending_evidence_followup_query or user_query_text
                fallback_lookup = await _select_semantic_fallback_tool(
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
                    previous_tool_name=last_tool_name,
                    previous_tool_result_text=last_tool_result_text,
                )
                if fallback_lookup is None and pending_evidence_followup_query:
                    pending_evidence_followup_query = ""
            if fallback_lookup is not None:
                fallback_name, fallback_args = fallback_lookup
                if fallback_name in empty_lookup_tools:
                    logger.info(f"[agent] semantic fallback skipped previously empty tool: {fallback_name}")
                    fallback_lookup = None
                elif fallback_name == last_tool_name and previous_tool_empty:
                    logger.info(f"[agent] semantic fallback skipped immediate empty tool repeat: {fallback_name}")
                    fallback_lookup = None
            if fallback_lookup is not None:
                fallback_name, fallback_args = fallback_lookup
                fallback_signature = _tool_signature(fallback_name, fallback_args)
                if fallback_signature == last_fallback_signature:
                    logger.info("[agent] semantic fallback repeated same tool signature; skipping")
                    fallback_lookup = None
                else:
                    last_fallback_signature = fallback_signature
                    fallback_tool = registry.get(fallback_name)
                    if fallback_tool is not None:
                        fallback_args, fallback_result = await _execute_tool_with_retries(
                            registry=registry,
                            tool_name=fallback_name,
                            tool_args=fallback_args,
                            rewritten_query=rewritten_query,
                            user_images=user_images,
                            previous_tool_name=last_tool_name,
                            previous_tool_result_text=last_tool_result_text,
                            logger=logger,
                            budget_deadline=budget_deadline,
                        )
                        fallback_id = f"fallback-{fallback_name}-{_step + 1}"
                        messages.append(
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": fallback_id,
                                        "type": "function",
                                        "function": {
                                            "name": fallback_name,
                                            "arguments": json.dumps(fallback_args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        )
                        messages.append(
                            tool_caller.build_tool_result_message(
                                fallback_id,
                                fallback_name,
                                fallback_result,
                            )
                        )
                        last_tool_name = str(fallback_name or "").strip()
                        if str(fallback_result or "").strip():
                            last_tool_result_text = str(fallback_result).strip()
                        has_tool_call = True
                        pending_evidence_followup_query = ""
                        tool_result_records.append(
                            _build_tool_result_record(
                                tool_name=fallback_name,
                                tool_args=fallback_args,
                                result=fallback_result,
                            )
                        )
                        semantic_fallback_attempted = False
                        logger.info(f"[agent] fallback tool_call name={fallback_name}")
                        await _append_evidence_guidance_if_needed()
                        continue
                    logger.info(f"[agent] semantic fallback selected unavailable tool: {fallback_name}")
            if (
                content_len == 0
                and bool(
                    getattr(
                        plugin_config,
                        "personification_fallback_enabled",
                        getattr(plugin_config, "personification_vision_fallback_enabled", True),
                    )
                )
                and registry.get("vision_analyze") is not None
                and user_images
            ):
                try:
                    background = await _run_background_vision_fallback(
                        registry=registry,
                        query=user_query_text or user_text or "请分析图片",
                        images=user_images,
                    )
                except Exception as e:
                    logger.warning(f"[agent] deferred vision fallback failed: {e}")
                    background = None
                if background is not None:
                    bg_name, bg_args, bg_result = background
                    await _inject_background_tool_result(
                        messages=messages,
                        tool_caller=tool_caller,
                        tool_name=bg_name,
                        tool_args=bg_args,
                        result=bg_result,
                        step=_step + 1,
                    )
                    has_tool_call = True
                    last_tool_name = bg_name
                    last_tool_result_text = bg_result
                    logger.info("[agent] awaited background vision fallback result")
                    continue
            if content_len == 0:
                return AgentResult(
                    text="[NO_REPLY]",
                    pending_actions=pending_actions,
                )
            return AgentResult(
                text=response.content,
                pending_actions=pending_actions,
                bypass_length_limits=has_tool_call,
            )

        if response.tool_calls:
            if not has_tool_call and not ack_sent and ack_sender is not None:
                ack_sent = True
                await _safe_ack(ack_sender, "", logger)
            messages.append(
                {
                    "role": "assistant",
                    # 必须用空字符串而非 None：部分严格 provider（Rust 反序列化）
                    # 会把 null 当作"缺失字段"直接 400 拒绝。
                    "content": response.content if response.content else "",
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                            },
                        }
                        for tool_call in response.tool_calls
                    ],
                }
            )

        for tool_call in response.tool_calls:
            has_tool_call = True
            logger.info(f"[agent] tool_call name={tool_call.name}")
            tool = registry.get(tool_call.name)
            tool_args = dict(tool_call.arguments or {})
            if tool is None:
                result = f"工具 {tool_call.name} 不存在"
            else:
                tool_args, result = await _execute_tool_with_retries(
                    registry=registry,
                    tool_name=tool_call.name,
                    tool_args=tool_args,
                    rewritten_query=rewritten_query,
                    user_images=user_images,
                    previous_tool_name=last_tool_name,
                    previous_tool_result_text=last_tool_result_text,
                    logger=logger,
                    budget_deadline=budget_deadline,
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
            last_tool_name = str(tool_call.name or "").strip()
            if str(result or "").strip():
                last_tool_result_text = str(result).strip()
            tool_result_records.append(
                _build_tool_result_record(
                    tool_name=last_tool_name,
                    tool_args=tool_args,
                    result=result,
                )
            )
            if last_tool_name in _RETRYABLE_LOOKUP_TOOLS:
                if _tool_result_indicates_empty(result):
                    empty_lookup_tools.add(last_tool_name)
                else:
                    empty_lookup_tools.discard(last_tool_name)
            semantic_fallback_attempted = False
            direct_result = _direct_tool_result_agent_result(
                tool_name=last_tool_name,
                result_text=result,
                pending_actions=pending_actions,
            )
            if direct_result is not None:
                return direct_result

            messages.append(
                tool_caller.build_tool_result_message(
                    tool_call.id,
                    tool_call.name,
                    result,
                )
            )
            await _append_evidence_guidance_if_needed()

    logger.warning("[agent] MAX_STEPS reached")
    if last_tool_result_text:
        logger.warning("[agent] using last tool result as fallback final answer")
        direct_result = _direct_tool_result_agent_result(
            tool_name=last_tool_name,
            result_text=last_tool_result_text,
            pending_actions=pending_actions,
        )
        if direct_result is not None:
            return direct_result
        rendered_tool_result = _render_tool_result_for_user(
            last_tool_name,
            last_tool_result_text,
            user_query_text,
        )
        return AgentResult(
            text=await _wrap_tool_result_in_persona(
                tool_caller=tool_caller,
                rendered_tool_result=rendered_tool_result,
                user_query_text=user_query_text,
                persona_system=_extract_persona_system_prompt(messages),
                turn_plan=turn_plan,
            ),
            pending_actions=pending_actions,
            direct_output=False,
            bypass_length_limits=False,
        )
    return AgentResult(
        text="[NO_REPLY]",
        pending_actions=pending_actions,
    )
