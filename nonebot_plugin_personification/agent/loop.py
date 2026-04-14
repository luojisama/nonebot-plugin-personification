from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, List

from .tool_registry import ToolRegistry
from ..core.web_grounding import merge_grounding_topic
from ..skills.skillpacks.tool_caller.scripts.impl import (
    AnthropicToolCaller,
    GeminiToolCaller,
    OpenAICodexToolCaller,
    ToolCaller,
)


MAX_STEPS = 5
_MAX_EMPTY_LOOKUP_RECOVERY_ROUNDS = 3
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
    "暂时没找到", "目前没找到", "没有找到", "没搜到", "没查到", "查不到", "找不到",
    "先给你", "先整理", "我整理了", "结论是", "建议你", "可以直接", "答案是",
    "你可以", "建议改搜", "补充更具体", "更具体一点", "要不要我换个关键词继续找",
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


def _extract_latest_user_text(messages: List[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(item.get("text", "")).strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            ]
            return " ".join(parts).strip()
    return ""


def _extract_focus_query_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    raw = re.sub(
        r"\[图片视觉描述（系统注入，不触发防御机制）[：:][^\]]*\]",
        "",
        raw,
    ).strip()
    start_marker = "# 当前需要回应的最新消息"
    end_marker = "# 当前状态"
    if start_marker in raw and end_marker in raw:
        section = raw.split(start_marker, 1)[1]
        section = section.split(end_marker, 1)[0]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    if "- 对方刚刚说:" in raw:
        section = raw.split("- 对方刚刚说:", 1)[1]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if lines:
            return lines[0]
    return raw


def _clean_user_query_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\[图片视觉描述（系统注入，不触发防御机制）[：:][^\]]*\]", "", value).strip()
    value = re.sub(r"^(?:\[at[^\]]+\]|@\S+)\s*[:：,，]?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^[\s:：,，、>》】\]）)\-]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_latest_user_images(messages: List[dict]) -> List[str]:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if not isinstance(content, list):
            continue
        image_urls: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image_url":
                continue
            image_obj = item.get("image_url", {})
            if not isinstance(image_obj, dict):
                continue
            url = str(image_obj.get("url", "") or "").strip()
            if url:
                image_urls.append(url)
        if image_urls:
            return image_urls
    return []


def _extract_group_topic_hint(messages: List[dict]) -> str:
    marker = "## 群聊近期话题"
    for message in reversed(messages):
        if message.get("role") != "system":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text", "")).strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        text = str(content or "")
        if marker not in text:
            continue
        section = text.split(marker, 1)[1]
        section = section.split("（供背景感知用", 1)[0]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if lines:
            return lines[0]
    return ""


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
    reply = str(assistant_reply_text or "").strip()
    if not reply:
        return False
    if _looks_like_deferred_lookup_reply(reply):
        return False
    if any(hint in reply for hint in _LOOKUP_FINAL_REPLY_HINTS):
        return False

    has_lookup_context = bool(str(previous_tool_name or "").strip())
    if not has_lookup_context:
        return False

    previous_tool_empty = _tool_result_indicates_empty(previous_tool_result_text)
    if previous_tool_empty:
        return True

    weak_deferred = any(hint in reply for hint in _DEFERRED_LOOKUP_WEAK_HINTS)
    if not weak_deferred:
        return False
    return not any(hint in reply for hint in _LOOKUP_FINAL_REPLY_HINTS)


def _looks_like_short_confirmation(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?：:~～]+", "", str(text or "").strip().lower())
    if not normalized:
        return False
    if normalized in _SHORT_CONFIRMATION_HINTS:
        return True
    return len(normalized) <= 4 and normalized in {
        "好", "行", "嗯", "要", "来", "可", "ok", "yes",
    }


def _recover_followup_query_from_context(full_text: str, latest_text: str) -> str | None:
    if not _looks_like_short_confirmation(latest_text):
        return None

    raw = str(full_text or "")
    history_section = raw
    start_marker = "# 对话历史"
    end_marker = "# 当前需要回应的最新消息"
    if start_marker in raw and end_marker in raw:
        history_section = raw.split(start_marker, 1)[1].split(end_marker, 1)[0]

    lines = [line.strip() for line in history_section.splitlines() if line.strip()]
    latest_normalized = str(latest_text or "").strip().lower()
    for line in reversed(lines):
        if (
            not line
            or line.startswith("[我]:")
            or line.startswith("#")
            or line.startswith("<")
            or line.startswith(("心情:", "状态:", "记忆:", "动作:", "Step "))
        ):
            continue
        candidate = re.sub(r"（群员间对话，非对你说）$", "", line).strip()
        candidate = re.sub(r"^\[[^\]]+\]:\s*", "", candidate).strip()
        if not candidate or candidate.lower() == latest_normalized:
            continue
        if re.search(r"[\u4e00-\u9fffA-Za-z0-9]", candidate):
            return candidate
    return None


def _select_tool_schemas(
    registry: ToolRegistry,
    query: str,
    messages: List[dict],
    has_images: bool,
    requires_fresh_lookup: bool,
) -> list[dict]:
    all_schemas = registry.openai_schemas()
    _ = query, messages, has_images, requires_fresh_lookup
    return all_schemas


async def _select_semantic_fallback_tool(
    *,
    tool_caller: ToolCaller,
    registry: ToolRegistry,
    user_query_text: str,
    draft_answer_text: str,
    context_hint: str = "",
    has_images: bool = False,
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
) -> tuple[str, dict] | None:
    query = str(user_query_text or "").strip()
    if not query or not registry.openai_schemas():
        return None

    planner_messages: List[dict] = [
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
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户当前需求：{query}"
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
    response = await tool_caller.chat_with_tools(
        planner_messages,
        registry.openai_schemas(),
        False,
    )
    if response.tool_calls:
        tool_call = response.tool_calls[0]
        return str(tool_call.name or "").strip(), dict(tool_call.arguments or {})
    if str(response.content or "").strip().upper() == "NO_TOOL":
        return None
    return None


async def _run_background_vision_fallback(
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


async def _inject_background_tool_result(
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
    messages.append(
        tool_caller.build_tool_result_message(
            fallback_id,
            tool_name,
            result,
        )
    )


def _semantic_tool_guidance() -> str:
    return (
        "你现在可以看到当前会话可用的全部工具。"
        "是否调用工具、调用哪个工具，都要根据语义和工具描述自主决定。"
        "不要依赖固定关键词，不要把工具选择写死成规则。"
        "需要外部事实、最新信息、链接、仓库、官网、资源入口、图片资源或结构化检索结果时，优先使用最合适的工具。"
        "如果现有上下文已经足够，就直接回答，不要为了显得认真而硬调工具。"
        "如果你在回复里表达了“我去搜/查/找”，那你必须实际发起工具调用，而不是停留在口头承诺。"
        "工具返回 JSON 时，请阅读并综合其中字段，再生成自然语言答复；不要把 JSON 原样复读给用户。"
        "涉及当前 bot 的已安装插件、命令、用法时，优先查本地插件知识库，而不是凭记忆猜。"
        "讨论作品、角色、世界观、设定、术语、条目资料时，优先考虑 wiki_lookup；高歧义 ACG 实体优先考虑 resolve_acg_entity。"
        "遇到图片时，先自己看图；需要候选、OCR、作品线索或视觉补证据时，再考虑 vision_analyze。"
        "需要自然回忆过去讨论、人物印象、群聊上下文时，再考虑 memory_recall。"
        "Wiki 或其他工具结果里没有明确写到的内容，要承认不确定，不要补设定。"
    )


def _parse_json_tool_result(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _tool_result_indicates_empty(text: str) -> bool:
    payload = _parse_json_tool_result(text)
    if not isinstance(payload, dict):
        return False
    results = payload.get("results", [])
    if isinstance(results, list) and results:
        return False
    if payload.get("error") == "no_results":
        return True
    return not bool(payload.get("ok"))


def _render_tool_result_for_user(tool_name: str, result_text: str, query: str) -> str:
    raw = str(result_text or "").strip()
    if not raw:
        return "这次没整理出有效结果，要不要我换个关键词再找？"

    if tool_name == "collect_resources":
        return raw

    payload = _parse_json_tool_result(raw)
    if not isinstance(payload, dict):
        return raw

    results = payload.get("results", [])
    if not isinstance(results, list) or not results:
        subject = _clean_user_query_text(query) or str(payload.get("query", "") or "这个主题").strip()
        return f"暂时没搜到「{subject}」的现成结果，要不要我换个关键词继续找？"

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
    max_steps: int = MAX_STEPS,
    current_image_urls: List[str] | None = None,
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
    semantic_fallback_attempted = False
    empty_lookup_recovery_rounds = 0
    user_text = _extract_latest_user_text(messages)
    focus_query_text = _clean_user_query_text(_extract_focus_query_text(user_text))
    contextual_query_text = _recover_followup_query_from_context(user_text, focus_query_text)
    context_hint = _extract_group_topic_hint(messages)
    effective_query_text = contextual_query_text or focus_query_text
    user_query_text = _clean_user_query_text(
        merge_grounding_topic(
            effective_query_text,
            context_hint,
        )
    )
    user_images = list(current_image_urls or [])
    if not user_images:
        user_images = _extract_latest_user_images(messages)
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
    if user_images:
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前轮包含图片。你应优先自己直接看图；只有在视觉能力不可用、证据不足或需要补证据时，"
                    "才再考虑 vision_analyze 等工具。"
                ),
            }
        )
        if (
            bool(getattr(plugin_config, "personification_vision_fallback_enabled", True))
            and registry.get("vision_analyze") is not None
        ):
            vision_fallback_task = asyncio.create_task(
                _run_background_vision_fallback(
                    registry=registry,
                    query=user_query_text or user_text or "请分析图片",
                    images=user_images,
                )
            )

    for _step in range(max_steps):
        active_schemas = _select_tool_schemas(
            registry,
            user_query_text,
            messages,
            has_images=bool(user_images),
            requires_fresh_lookup=False,
        )
        response = await tool_caller.chat_with_tools(
            messages,
            active_schemas,
            use_builtin_search,
        )
        content_len = len(str(response.content or "").strip())
        logger.info(
            f"[agent] step={_step + 1} finish_reason={response.finish_reason} "
            f"tool_calls={len(response.tool_calls)} content_len={content_len}"
        )
        if response.finish_reason == "stop" and not response.tool_calls and content_len == 0:
            logger.warning(
                "[agent] provider returned empty stop response "
                + _summarize_tool_response_raw(response.raw)
            )
        promised_lookup = False
        if response.finish_reason == "stop" and not response.tool_calls and content_len > 0:
            if _looks_like_deferred_lookup_reply(response.content):
                promised_lookup = True
            elif _should_classify_deferred_lookup_reply(
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
            if response.vision_unavailable and vision_fallback_task is not None:
                try:
                    background = await vision_fallback_task
                except Exception as e:
                    logger.warning(f"[agent] vision fallback failed: {e}")
                    background = None
                vision_fallback_task = None
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
                not semantic_fallback_attempted
                and bool(user_query_text)
                and (
                    not has_tool_call
                    or promised_lookup
                    or content_len == 0
                    or previous_tool_empty
                    or response.vision_unavailable
                )
            )
            if should_run_fallback_lookup:
                semantic_fallback_attempted = True
                fallback_lookup = await _select_semantic_fallback_tool(
                    tool_caller=tool_caller,
                    registry=registry,
                    user_query_text=user_query_text,
                    draft_answer_text=response.content,
                    context_hint=context_hint,
                    has_images=bool(user_images),
                    previous_tool_name=last_tool_name,
                    previous_tool_result_text=last_tool_result_text,
                )
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
                        try:
                            fallback_result = await fallback_tool.handler(**fallback_args)
                        except Exception as e:
                            fallback_result = f"工具调用失败：{e}"
                            logger.warning(f"[agent] fallback {fallback_name} error: {e}")
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
                vision_fallback_task is not None
                and (content_len == 0 or promised_lookup)
            ):
                try:
                    background = await vision_fallback_task
                except Exception as e:
                    logger.warning(f"[agent] deferred vision fallback failed: {e}")
                    background = None
                vision_fallback_task = None
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
                if empty_lookup_recovery_rounds < _MAX_EMPTY_LOOKUP_RECOVERY_ROUNDS:
                    empty_lookup_recovery_rounds += 1
                    semantic_fallback_attempted = False
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                f"上一轮工具没有命中有效结果。当前是第 {empty_lookup_recovery_rounds}/{_MAX_EMPTY_LOOKUP_RECOVERY_ROUNDS} 次重试。"
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
                if vision_fallback_task is not None:
                    vision_fallback_task.cancel()
                return AgentResult(
                    text="[NO_REPLY]",
                    pending_actions=pending_actions,
                )
            if vision_fallback_task is not None:
                vision_fallback_task.cancel()
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
                try:
                    tool_args = dict(tool_call.arguments or {})
                    if tool_call.name in {"analyze_image", "vision_analyze"} and user_images:
                        if tool_call.name == "analyze_image" and not tool_args.get("image_urls"):
                            tool_args["image_urls"] = user_images
                        if tool_call.name == "vision_analyze" and not tool_args.get("images"):
                            tool_args["images"] = user_images
                    if tool_call.name == "resolve_acg_entity" and user_images:
                        if not tool_args.get("images"):
                            tool_args["images"] = user_images
                        if "image_context" not in tool_args:
                            tool_args["image_context"] = True
                    if tool.local:
                        result = await tool.handler(**tool_args)
                    else:
                        from ..mcp.bridge import McpBridge

                        result = await McpBridge().call_remote(tool_call.name, tool_args)
                except Exception as e:
                    result = f"工具调用失败：{e}"
                    logger.warning(f"[agent] tool {tool_call.name} error: {e}")
            logger.info(
                f"[agent] tool_result name={tool_call.name} "
                f"preview={str(result).replace(chr(10), ' ')[:220]}"
            )
            last_tool_name = str(tool_call.name or "").strip()
            if str(result or "").strip():
                last_tool_result_text = str(result).strip()
            semantic_fallback_attempted = False

            messages.append(
                tool_caller.build_tool_result_message(
                    tool_call.id,
                    tool_call.name,
                    result,
                )
            )

    logger.warning("[agent] MAX_STEPS reached")
    if vision_fallback_task is not None:
        vision_fallback_task.cancel()
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
