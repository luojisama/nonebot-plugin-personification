from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, List

from ..query_rewriter import ContextualQueryRewrite, QueryRewriteContext, contextual_query_rewriter
from ...core.error_utils import log_exception
from ...core.metrics import record_counter, record_timing
from ...core.time_ctx import get_configured_now
from ..tool_registry import ToolRegistry
from ...core.message_parts import extract_text_from_parts
from ...core.web_grounding import merge_grounding_topic
from ...skills.skillpacks.tool_caller.scripts.impl import (
    AnthropicToolCaller,
    GeminiToolCaller,
    OpenAICodexToolCaller,
    ToolCaller,
)
from .constants import DEFAULT_AGENT_MAX_STEPS, MAX_EMPTY_LOOKUP_RECOVERY_ROUNDS
from .intent import (
    _clean_user_query_text,
    _compact_lookup_query,
    _derive_query_rewrite_context,
    _extract_focus_query_text,
    _extract_group_topic_hint,
    _extract_latest_user_images,
    _extract_latest_user_text,
    _extract_quoted_message_text,
    _infer_chat_intent,
    _infer_intent_decision_with_context,
    _messages_indicate_private_scene,
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
    _requires_forced_lookup,
    _schema_tool_name,
    _select_tool_schemas,
    _semantic_tool_guidance,
)
from .fallbacks import (
    _cancel_task_safely,
    _inject_background_tool_result,
    _parse_json_tool_result,
    _run_background_vision_fallback,
    _select_semantic_fallback_tool,
    _tool_result_indicates_empty,
)


_SHORT_CONFIRMATION_HINTS = frozenset([
    "好", "好的", "好啊", "好呀", "行", "行啊", "可以", "可以的",
    "嗯", "嗯嗯", "要", "要的", "来", "来吧", "安排", "整理吧",
    "发我", "发吧", "给我", "给我吧",
])
_DEFERRED_LOOKUP_STRONG_PATTERNS = (
    re.compile(r"(我|这边).{0,8}(去|先|再)?(查|搜|找|看)(一下|下|看)?"),
    re.compile(r"(稍等|等我|你等下|先别急).{0,8}(查|搜|找|看)"),
    re.compile(r"(我|这边).{0,12}(换个关键词|继续搜|继续找|再搜|再找)"),
)
_DEFERRED_LOOKUP_WEAK_HINTS = (
    "查一下", "查下", "搜一下", "搜下", "找一下", "找下", "看一下", "看下",
    "继续找", "继续搜", "换个关键词", "再搜", "再找", "稍等", "等我", "我去查", "我去找", "我去搜",
)
_LOOKUP_FINAL_REPLY_HINTS = (
        "先给你", "先整理", "我整理了", "结论是", "建议你", "可以直接", "答案是",
    "你可以", "建议改搜", "补充更具体", "更具体一点",
)
_QUERY_REWRITE_TOOL_NAMES = frozenset(
    {
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
    {"web_search", "search_web", "wiki_lookup", "resolve_acg_entity", "collect_resources", "search_images"}
)
_TIME_SENSITIVE_SEARCH_TOOLS = frozenset({"web_search", "search_web"})
_TIME_SENSITIVE_RE = re.compile("\u6700\u65b0|\u8fd1\u671f|\u73b0\u5728|\u4eca\u5e74|\u4eca\u5929|\u5f53\u524d|latest|recent|now", re.IGNORECASE)


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
_PLAIN_EMPTY_TOOL_RESULT_MARKERS = (
    "未找到足够可靠的wiki条目",
    "没有找到足够可靠的wiki条目",
    "未找到可靠wiki条目",
    "未找到相关wiki条目",
    "未找到可靠结果",
    "没有找到可靠结果",
    "no_results",
)
_FORCED_LOOKUP_PATTERNS = (
    re.compile(r"(最新|现在|当前|今天|刚刚|最近|多少钱|多少|天气|汇率|股价|票房|热搜|新闻|价格)"),
)
_LOOKUP_QUERY_LEADING_RE = re.compile(
    r"^(?:我问的是|我想问的是|我想问下|我想问一下|我想知道|我问下|我问一下|"
    r"请问|想问下|想问一下|帮我查下|帮我查一下|帮我搜下|帮我搜一下|查一下|搜一下)\s*"
)
_LOOKUP_QUERY_TRAILING_RE = re.compile(
    r"(?:到底)?(?:算|指)?(?:是)?(?:什么(?:东西|意思|玩意儿?|来着)?|啥(?:意思|东西)?|"
    r"指什么|是什么(?:东西|意思|玩意儿?)?|怎么回事)\s*[?？!！.。]*$"
)


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


async def _classify_deferred_lookup_reply(
    *,
    tool_caller: ToolCaller,
    user_query_text: str,
    assistant_reply_text: str,
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
) -> bool:
    reply = str(assistant_reply_text or "").strip()
    if not reply:
        return False

    response = await tool_caller.chat_with_tools(
        [
            {
                "role": "system",
                "content": (
                    "你是回复状态分类器。"
                    "判断 assistant 当前草稿到底是在直接给最终答案，还是只是在承诺继续搜索/继续查找。"
                    "如果草稿本质上是在说还要继续搜、继续换关键词、继续找资料，而没有真正完成答复，只输出 RETRY_SEARCH。"
                    "如果草稿已经是可直接发给用户的最终答复，或者明确结束搜索并给出结论/建议，只输出 FINAL_ANSWER。"
                    "禁止输出解释、标点、JSON、其他文本。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户需求：{str(user_query_text or '').strip() or '[EMPTY]'}\n"
                    f"assistant 草稿：{reply}\n"
                    f"上一轮工具：{str(previous_tool_name or '').strip() or '[NONE]'}\n"
                    f"上一轮工具结果摘要：{str(previous_tool_result_text or '').strip()[:600] or '[NONE]'}"
                ),
            },
        ],
        [],
        False,
    )
    decision = str(response.content or "").strip().upper()
    return decision == "RETRY_SEARCH"


def _looks_like_deferred_lookup_reply(text: str) -> bool:
    reply = str(text or "").strip()
    if not reply:
        return False
    if any(hint in reply for hint in _LOOKUP_FINAL_REPLY_HINTS):
        return False
    return any(pattern.search(reply) for pattern in _DEFERRED_LOOKUP_STRONG_PATTERNS)


def _should_classify_deferred_lookup_reply(
    *,
    assistant_reply_text: str,
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
) -> bool:
    if not str(assistant_reply_text or "").strip():
        return False
    return bool(str(previous_tool_name or "").strip())


def _looks_like_short_confirmation(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?：:~～]+", "", str(text or "").strip().lower())
    if not normalized:
        return False
    if normalized in _SHORT_CONFIRMATION_HINTS:
        return True
    return len(normalized) <= 4 and normalized in {
        "好", "行", "嗯", "要", "来", "可", "ok", "yes",
    }


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
        started_at = time.monotonic()
        try:
            last_result = await _invoke_tool_handler(
                tool_name=tool_name,
                tool=tool,
                tool_args=attempt_args,
            )
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


def _render_tool_result_for_user(tool_name: str, result_text: str, query: str) -> str:
    raw = str(result_text or "").strip()
    if not raw:
        return json.dumps({"status": "no_result", "query": _clean_user_query_text(query)}, ensure_ascii=False)

    if tool_name == "collect_resources":
        return raw

    payload = _parse_json_tool_result(raw)
    if not isinstance(payload, dict):
        return raw

    results = payload.get("results", [])
    if not isinstance(results, list) or not results:
        subject = _clean_user_query_text(query) or str(payload.get("query", "") or "这个主题").strip()
        return json.dumps({"status": "no_result", "query": subject}, ensure_ascii=False)

    lines: list[str] = []
    subject = _clean_user_query_text(query) or str(payload.get("query", "") or "这个主题").strip()
    lines.append(f"先给你整理 {min(len(results), 3)} 条「{subject}」参考：")
    for index, item in enumerate(results[:3], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or item.get("full_name", "") or item.get("url", "")).strip()
        snippet = str(item.get("snippet", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        source = str(item.get("source", "") or "").strip()
        line = f"{index}. {title}"
        if source:
            line += f" [{source}]"
        lines.append(line)
        if snippet:
            lines.append(snippet[:120])
        if url:
            lines.append(url)
    return "\n".join(lines).strip()


def _tool_signature(tool_name: str, tool_args: dict[str, Any]) -> str:
    return (
        f"{str(tool_name or '').strip()}:"
        f"{json.dumps(tool_args or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    )


async def run_agent(
    messages: List[dict],
    registry: ToolRegistry,
    tool_caller: ToolCaller,
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
) -> AgentResult:
    use_builtin_search = (
        bool(
            getattr(
                plugin_config,
                "personification_model_builtin_search_enabled",
                getattr(plugin_config, "personification_builtin_search", True),
            )
        )
        and isinstance(tool_caller, (GeminiToolCaller, AnthropicToolCaller, OpenAICodexToolCaller))
    )
    pending_actions: List[dict] = []
    last_tool_name = ""
    last_tool_result_text = ""
    last_fallback_signature = ""
    empty_lookup_tools: set[str] = set()
    semantic_fallback_attempted = False
    empty_lookup_recovery_rounds = 0
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
    force_lookup = _requires_forced_lookup(preliminary_query_text or user_text)
    runtime_chat_intent = "lookup" if chat_intent == "banter" and force_lookup else chat_intent
    effective_max_steps = _normalize_agent_max_steps(
        max_steps if max_steps is not None else getattr(plugin_config, "personification_agent_max_steps", DEFAULT_AGENT_MAX_STEPS)
    )
    rewrite_context = _derive_query_rewrite_context(
        messages,
        current_images=user_images,
        provided=query_rewrite_context,
    )
    if runtime_chat_intent == "banter":
        rewritten_query = ContextualQueryRewrite(
            primary_query=preliminary_query_text,
            query_candidates=[preliminary_query_text] if preliminary_query_text else [],
            context_clues=[],
            need_image_understanding=bool(user_images),
            recommended_tools=[],
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
    vision_fallback_task: asyncio.Task | None = None
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
        vision_fallback_task = None

    for _step in range(effective_max_steps):
        active_schemas = _select_tool_schemas(
            registry,
            has_images=bool(user_images),
            chat_intent=runtime_chat_intent,
            plugin_question_intent=plugin_query_intent,
            force_lookup=force_lookup,
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
        promised_lookup = False
        if response.finish_reason == "stop" and not response.tool_calls and content_len > 0:
            if runtime_chat_intent != "banter" and _looks_like_deferred_lookup_reply(response.content):
                promised_lookup = True
            elif runtime_chat_intent != "banter" and _should_classify_deferred_lookup_reply(
                assistant_reply_text=response.content,
                previous_tool_name=last_tool_name,
                previous_tool_result_text=last_tool_result_text,
            ):
                promised_lookup = await _classify_deferred_lookup_reply(
                    tool_caller=tool_caller,
                    user_query_text=user_query_text,
                    assistant_reply_text=response.content,
                    previous_tool_name=last_tool_name,
                    previous_tool_result_text=last_tool_result_text,
                )
        if response.finish_reason == "stop":
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
                    or promised_lookup
                    or content_len == 0
                    or response.vision_unavailable
                )
            )
            if should_run_fallback_lookup:
                semantic_fallback_attempted = True
                fallback_lookup = await _select_semantic_fallback_tool(
                    tool_caller=tool_caller,
                    registry=registry,
                    user_query_text=user_query_text,
                    rewritten_query=rewritten_query,
                    draft_answer_text=response.content,
                    context_hint=context_hint,
                    has_images=bool(user_images),
                    chat_intent=runtime_chat_intent,
                    plugin_question_intent=plugin_query_intent,
                    force_lookup=force_lookup,
                    user_images=user_images,
                    previous_tool_name=last_tool_name,
                    previous_tool_result_text=last_tool_result_text,
                )
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
                        semantic_fallback_attempted = False
                        logger.info(f"[agent] fallback tool_call name={fallback_name}")
                        continue
                    logger.info(f"[agent] semantic fallback selected unavailable tool: {fallback_name}")
            if (
                (content_len == 0 or promised_lookup)
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
            if promised_lookup and not has_tool_call:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "不要口头承诺你会去查。"
                            "如果不需要工具，就直接基于现有上下文回答；"
                            "如果需要工具，就直接调用。"
                        ),
                    }
                )
                logger.info("[agent] deferred lookup reply without tool call, forcing direct answer rewrite")
                continue
            if promised_lookup and has_tool_call and previous_tool_empty:
                if empty_lookup_recovery_rounds < MAX_EMPTY_LOOKUP_RECOVERY_ROUNDS:
                    empty_lookup_recovery_rounds += 1
                    semantic_fallback_attempted = False
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                f"上一轮工具没有命中有效结果。当前是第 {empty_lookup_recovery_rounds}/{MAX_EMPTY_LOOKUP_RECOVERY_ROUNDS} 次重试。"
                                "不要只说你还要继续找。"
                                "如果还能换查询策略或改用别的工具，就直接调用；"
                                "否则直接明确说明暂时没找到，并给出 2 到 3 个更好的搜索方向。"
                            ),
                        }
                    )
                    logger.info(
                        f"[agent] empty lookup recovery round={empty_lookup_recovery_rounds}, reopening agent loop"
                    )
                    continue
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "你刚才已经连续多次调用工具，但结果为空或无命中，已达到重试上限。"
                            "现在不要继续口头承诺，也不要再发起新搜索。"
                            "必须直接向用户说明暂时没找到可靠结果，并给出更好的搜索方向或让用户补充更具体目标。"
                        ),
                    }
                )
                logger.info("[agent] empty lookup retries exhausted, forcing final direct answer")
                continue
            if promised_lookup and has_tool_call and last_tool_result_text:
                logger.info("[agent] deferred lookup reply after tool result, returning last tool result directly")
                return AgentResult(
                    text=_render_tool_result_for_user(last_tool_name, last_tool_result_text, user_query_text),
                    pending_actions=pending_actions,
                    direct_output=True,
                    bypass_length_limits=True,
                )
            if promised_lookup and has_tool_call:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "你已经拿到了工具结果。"
                            "现在必须基于现有结果直接回答，给出简短摘要；"
                            "如果是攻略、教程、资料请求，再附上 2 到 4 条参考链接。"
                            "禁止继续说“我去找”“我给你找”“等我查”。"
                        ),
                    }
                )
                logger.info("[agent] deferred lookup reply after tool result, forcing final answer")
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
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
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
            if tool is None:
                result = f"工具 {tool_call.name} 不存在"
            else:
                tool_args, result = await _execute_tool_with_retries(
                    registry=registry,
                    tool_name=tool_call.name,
                    tool_args=dict(tool_call.arguments or {}),
                    rewritten_query=rewritten_query,
                    user_images=user_images,
                    previous_tool_name=last_tool_name,
                    previous_tool_result_text=last_tool_result_text,
                    logger=logger,
                )
            logger.info(
                f"[agent] tool_result name={tool_call.name} "
                f"preview={str(result).replace(chr(10), ' ')[:220]}"
            )
            last_tool_name = str(tool_call.name or "").strip()
            if str(result or "").strip():
                last_tool_result_text = str(result).strip()
            if last_tool_name in _RETRYABLE_LOOKUP_TOOLS:
                if _tool_result_indicates_empty(result):
                    empty_lookup_tools.add(last_tool_name)
                else:
                    empty_lookup_tools.discard(last_tool_name)
            semantic_fallback_attempted = False

            messages.append(
                tool_caller.build_tool_result_message(
                    tool_call.id,
                    tool_call.name,
                    result,
                )
            )

    logger.warning("[agent] MAX_STEPS reached")
    if last_tool_result_text:
        logger.warning("[agent] using last tool result as fallback final answer")
        return AgentResult(
            text=_render_tool_result_for_user(last_tool_name, last_tool_result_text, user_query_text),
            pending_actions=pending_actions,
            direct_output=True,
            bypass_length_limits=True,
        )
    return AgentResult(
        text="[NO_REPLY]",
        pending_actions=pending_actions,
    )
