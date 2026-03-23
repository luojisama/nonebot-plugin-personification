from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List

from .tool_registry import ToolRegistry
from ..skills.tool_caller import AnthropicToolCaller, GeminiToolCaller, OpenAICodexToolCaller, ToolCaller


MAX_STEPS = 5

_TOOL_GROUPS: dict[str, set[str]] = {
    "always": {"datetime", "get_user_persona"},
    "search": {
        "web_search",
        "get_daily_news",
        "get_trending",
        "get_joke",
        "get_history_today",
        "get_epic_games",
        "get_gold_price",
        "get_baike_entry",
        "get_exchange_rate",
    },
    "weather": {"weather"},
    "image": {"analyze_image", "select_sticker", "understand_sticker"},
    "task": {"create_user_task", "cancel_user_task"},
    "social": {"get_group_list", "send_friend_request"},
}

_SEARCH_KEYWORDS = frozenset([
    "搜索", "查一下", "查查", "新闻", "热搜", "今天发生", "最新", "最近",
    "刚刚", "发布", "版本", "更新", "是真的吗", "真假", "百科", "汇率",
    "gold", "黄金", "搜", "找找",
])
_WEATHER_KEYWORDS = frozenset([
    "天气", "气温", "下雨", "温度", "穿什么", "带伞", "热不热", "冷不冷",
    "weather",
])
_IMAGE_KEYWORDS = frozenset([
    "图片", "图", "表情", "表情包", "翻译", "识别", "这是谁", "什么角色",
    "出自", "哪部", "analyze", "sticker",
])
_TASK_KEYWORDS = frozenset([
    "提醒", "定时", "每天", "每周", "每月", "任务", "闹钟", "到时候告诉我",
    "reminder", "task",
])
_SOCIAL_KEYWORDS = frozenset([
    "群号",
    "在哪些群",
    "哪个群",
    "加我好友",
    "加好友",
    "申请好友",
    "你在哪群",
    "告诉我群号",
])


@dataclass
class AgentResult:
    text: str
    pending_actions: List[dict]
    direct_output: bool = False


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
        line = section.splitlines()[0].strip()
        if line:
            return line
    return raw


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


def _requires_fresh_lookup(text: str) -> bool:
    query = (text or "").strip().lower()
    if not query:
        return False
    keywords = [
        "最新",
        "最近",
        "刚刚",
        "今天",
        "发布",
        "发布时间",
        "版本",
        "更新",
        "chatgpt",
        "gpt",
        "新闻",
        "热搜",
    ]
    if any(key in query for key in keywords):
        return True
    return bool(re.search(r"(gpt\s*[- ]?\d+(\.\d+)*)", query))


def _requires_image_translation(text: str, has_images: bool) -> bool:
    query = (text or "").strip().lower()
    if not query or not has_images:
        return False
    keywords = [
        "翻译", "译一下", "译成", "汉化", "日文", "日语", "英文", "英语",
        "台词", "对白", "字幕", "原文", "这页", "图里字", "图中文字",
        "translate", "translation", "ocr",
    ]
    return any(key in query for key in keywords)


def _extract_target_language(text: str) -> str:
    """从用户输入推断翻译目标语言，默认中文。"""
    q = (text or "").strip().lower()
    if any(k in q for k in ("英文", "英语", "english")):
        return "英文"
    if any(k in q for k in ("日文", "日语", "japanese")):
        return "日文"
    if any(k in q for k in ("韩文", "韩语", "korean")):
        return "韩文"
    if any(k in q for k in ("繁体", "繁中", "traditional chinese")):
        return "繁体中文"
    return "中文"


def _requires_image_analysis(text: str, has_images: bool) -> bool:
    query = (text or "").strip().lower()
    if not query or not has_images:
        return False
    keywords = [
        "角色",
        "人物",
        "是谁",
        "出自",
        "哪部",
        "什么作品",
        "哪个番",
        "哪部动漫",
        "哪部动画",
        "叫什么",
    ]
    return any(key in query for key in keywords)


def _requires_image_refresh(text: str, has_images: bool) -> bool:
    query = (text or "").strip().lower()
    if not query or not has_images:
        return False
    keywords = [
        "刷新",
        "重新识别",
        "重新翻译",
        "重新分析",
        "重识别",
        "重翻",
        "再识别",
        "再翻译",
        "refresh",
        "retry",
        "recheck",
    ]
    return any(key in query for key in keywords)


def _select_tool_schemas(
    registry: ToolRegistry,
    query: str,
    messages: List[dict],
    has_images: bool,
    requires_fresh_lookup: bool,
) -> list[dict]:
    """根据本轮消息特征，动态选取需要暴露给模型的工具 schema。"""
    q = (query or "").lower()
    active_groups: set[str] = {"always"}

    if requires_fresh_lookup or any(kw in q for kw in _SEARCH_KEYWORDS):
        active_groups.add("search")
    if any(kw in q for kw in _WEATHER_KEYWORDS):
        active_groups.add("weather")
    if has_images or any(kw in q for kw in _IMAGE_KEYWORDS):
        active_groups.add("image")
    if any(kw in q for kw in _TASK_KEYWORDS):
        active_groups.add("task")
    if any(kw in query for kw in _SOCIAL_KEYWORDS):
        active_groups.add("social")
    if any(
        "send_friend_request" in str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == "system"
    ):
        active_groups.add("social")

    allowed_names: set[str] = set()
    for group in active_groups:
        allowed_names |= _TOOL_GROUPS.get(group, set())

    all_schemas = registry.openai_schemas()
    filtered = [s for s in all_schemas if s.get("function", {}).get("name") in allowed_names]
    return filtered if filtered else all_schemas


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
        getattr(plugin_config, "personification_builtin_search", True)
        and isinstance(tool_caller, (GeminiToolCaller, AnthropicToolCaller, OpenAICodexToolCaller))
    )
    pending_actions: List[dict] = []
    user_text = _extract_latest_user_text(messages)
    user_query_text = _extract_focus_query_text(user_text)
    user_images = list(current_image_urls or [])
    if not user_images:
        user_images = _extract_latest_user_images(messages)
    requires_fresh_lookup = _requires_fresh_lookup(user_query_text)
    requires_image_translation = _requires_image_translation(user_query_text, bool(user_images))
    requires_image_analysis = _requires_image_analysis(user_query_text, bool(user_images))
    requires_image_refresh = _requires_image_refresh(user_query_text, bool(user_images))
    has_tool_call = False
    has_image_tool_call = False
    if requires_image_translation:
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前问题与用户刚发送的图片相关，且用户明确要求翻译图片中的文字。"
                    "回答前应优先调用 analyze_image，使用 task=translate 直接输出原文与译文对照。"
                    "禁止让用户手动把图里的字再发一遍。"
                ),
            }
        )
    if requires_image_analysis:
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前问题与用户刚发送的图片相关，且涉及人物/作品识别。"
                    "回答前应优先调用 analyze_image，并开启 web_lookup 获取候选作品线索。"
                ),
            }
        )
    if requires_image_refresh:
        messages.append(
            {
                "role": "system",
                "content": (
                    "如果用户要求重新识别、重新翻译或刷新当前图片，调用 analyze_image 时必须传 refresh=true，"
                    "忽略已有缓存并重新分析。"
                ),
            }
        )
    if requires_fresh_lookup:
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前用户问题具有时效性或版本依赖，回答前应先调用工具核实。"
                    "优先考虑 web_search；涉及新闻榜单可用 get_daily_news/get_trending。"
                ),
            }
        )

    for _step in range(max_steps):
        active_schemas = _select_tool_schemas(
            registry,
            user_query_text,
            messages,
            has_images=bool(user_images),
            requires_fresh_lookup=requires_fresh_lookup,
        )
        response = await tool_caller.chat_with_tools(
            messages,
            active_schemas,
            use_builtin_search,
        )
        logger.info(
            f"[agent] step={_step + 1} finish_reason={response.finish_reason} "
            f"tool_calls={len(response.tool_calls)}"
        )
        if response.finish_reason == "stop":
            if requires_image_translation and not has_image_tool_call:
                fallback_tool = registry.get("analyze_image")
                if fallback_tool is not None:
                    fallback_id = f"fallback-image-translate-{_step + 1}"
                    fallback_args = {
                        "query": user_query_text,
                        "task": "translate",
                        "detail": "detailed",
                        "target_language": _extract_target_language(user_query_text),
                        "refresh": requires_image_refresh,
                    }
                    if user_images:
                        fallback_args["image_urls"] = user_images
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": fallback_id,
                                    "type": "function",
                                    "function": {
                                        "name": "analyze_image",
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
                        logger.warning(f"[agent] fallback image translate error: {e}")
                    messages.append(
                        tool_caller.build_tool_result_message(
                            fallback_id,
                            "analyze_image",
                            fallback_result,
                        )
                    )
                    has_tool_call = True
                    has_image_tool_call = True
                    requires_image_translation = False
                    logger.info("[agent] fallback tool_call name=analyze_image(task=translate)")
                    return AgentResult(
                        text=str(fallback_result or ""),
                        pending_actions=pending_actions,
                        direct_output=True,
                    )
                messages.append(
                    {
                        "role": "system",
                        "content": "该问题需要先调用 analyze_image（task=translate）识别并翻译图片文字，再回答。",
                    }
                )
                requires_image_translation = False
                continue
            if requires_image_analysis and not has_image_tool_call:
                fallback_tool = registry.get("analyze_image")
                if fallback_tool is not None:
                    fallback_id = f"fallback-analyze-image-{_step + 1}"
                    fallback_args = {
                        "query": user_query_text,
                        "task": "identify",
                        "focus": "person",
                        "detail": "detailed",
                        "web_lookup": True,
                        "refresh": requires_image_refresh,
                    }
                    if user_images:
                        fallback_args["image_urls"] = user_images
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": fallback_id,
                                    "type": "function",
                                    "function": {
                                        "name": "analyze_image",
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
                        logger.warning(f"[agent] fallback analyze_image error: {e}")
                    messages.append(
                        tool_caller.build_tool_result_message(
                            fallback_id,
                            "analyze_image",
                            fallback_result,
                        )
                    )
                    has_tool_call = True
                    has_image_tool_call = True
                    requires_image_analysis = False
                    logger.info("[agent] fallback tool_call name=analyze_image")
                    continue
                messages.append(
                    {
                        "role": "system",
                        "content": "该问题需要先调用 analyze_image 获取图像识别与候选作品线索，再回答。",
                    }
                )
                requires_image_analysis = False
                continue
            if requires_fresh_lookup and not has_tool_call:
                fallback_tool = registry.get("web_search")
                if fallback_tool is not None and user_query_text:
                    fallback_id = f"fallback-web-search-{_step + 1}"
                    fallback_args = {"query": user_query_text}
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": fallback_id,
                                    "type": "function",
                                    "function": {
                                        "name": "web_search",
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
                        logger.warning(f"[agent] fallback web_search error: {e}")
                    messages.append(
                        tool_caller.build_tool_result_message(
                            fallback_id,
                            "web_search",
                            fallback_result,
                        )
                    )
                    has_tool_call = True
                    requires_fresh_lookup = False
                    logger.info("[agent] fallback tool_call name=web_search")
                    continue
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "当前问题涉及实时或版本事实，请先调用至少一个工具获取依据后再回答。"
                            "优先使用 web_search、get_daily_news 或 get_trending。"
                        ),
                    }
                )
                requires_fresh_lookup = False
                continue
            return AgentResult(
                text=response.content,
                pending_actions=pending_actions,
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
            if tool_call.name == "analyze_image":
                has_image_tool_call = True
            logger.info(f"[agent] tool_call name={tool_call.name}")
            tool = registry.get(tool_call.name)
            if tool is None:
                result = f"工具 {tool_call.name} 不存在"
            else:
                try:
                    tool_args = dict(tool_call.arguments or {})
                    if tool_call.name == "analyze_image" and user_images and not tool_args.get("image_urls"):
                        tool_args["image_urls"] = user_images
                    if tool_call.name == "analyze_image" and requires_image_refresh and "refresh" not in tool_args:
                        tool_args["refresh"] = True
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

            if tool_call.name == "analyze_image" and (
                str(tool_args.get("task", "") or "").strip().lower() == "translate"
                or requires_image_translation
            ):
                return AgentResult(
                    text=str(result or ""),
                    pending_actions=pending_actions,
                    direct_output=True,
                )

            messages.append(
                tool_caller.build_tool_result_message(
                    tool_call.id,
                    tool_call.name,
                    result,
                )
            )

    logger.warning("[agent] MAX_STEPS reached")
    return AgentResult(
        text="[NO_REPLY]",
        pending_actions=pending_actions,
    )
