import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import httpx


WEB_SEARCH_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网获取最新信息、新闻、知识等内容。当需要查找实时信息或不确定某个事实时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，应简洁明确，支持中英文",
                }
            },
            "required": ["query"],
        },
    },
}

WEB_SEARCH_TOOL_GEMINI = {
    "name": "web_search",
    "description": "Search the web for recent facts, news, or uncertain information.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Search query.",
            }
        },
        "required": ["query"],
    },
}

WEB_SEARCH_TOOL_ANTHROPIC = {
    "name": "web_search",
    "description": "Search the web for recent facts, news, or uncertain information.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
            }
        },
        "required": ["query"],
    },
}

WEB_SEARCH_TOOL_ANTHROPIC_NATIVE = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}


_TAVILY_API_KEY_CACHE: Optional[str] = None


def _get_tavily_api_key() -> str:
    global _TAVILY_API_KEY_CACHE
    if _TAVILY_API_KEY_CACHE is not None:
        return _TAVILY_API_KEY_CACHE

    key = os.getenv("TAVILY_API_KEY", "").strip()
    if key:
        _TAVILY_API_KEY_CACHE = key
        return key

    config_paths = [
        Path("data/cmd_config.json"),
        Path("cmd_config.json"),
    ]
    for path in config_paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            raw = data.get("provider_settings", {}).get("websearch_tavily_key", "")
            if isinstance(raw, list):
                key = str(raw[0]).strip() if raw else ""
            else:
                key = str(raw).strip()
            if key:
                _TAVILY_API_KEY_CACHE = key
                return key
        except Exception:
            continue

    _TAVILY_API_KEY_CACHE = ""
    return ""


def infer_grounding_intent(text: str) -> str:
    text = (text or "").lower()
    news_keys = ["新闻", "热搜", "瓜", "辟谣", "最新", "进展", "事件", "发布会", "通报"]
    knowledge_keys = ["什么是", "科普", "原理", "百科", "概念", "定义", "为什么", "怎么回事"]
    rec_keys = ["推荐", "安利", "值得", "好看", "好听", "好用", "买什么", "看什么", "玩什么"]

    if any(k in text for k in news_keys):
        return "news"
    if any(k in text for k in rec_keys):
        return "recommend"
    if any(k in text for k in knowledge_keys):
        return "knowledge"
    return "generic"


def extract_grounding_topic(text: str) -> str:
    if not text:
        return ""
    s = re.sub(r"\[[^\]]+\]", " ", text)
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 120:
        s = s[:120]
    return s


async def fetch_tavily_context(
    keyword: str,
    *,
    intent: str = "generic",
    get_now: Callable[[], Any],
    logger: Any,
) -> str:
    api_key = _get_tavily_api_key()
    if not api_key:
        return ""

    if intent == "news":
        now_date = get_now().strftime("%Y-%m-%d")
        query = f"{keyword} {now_date} 最新进展 来龙去脉 事实核查"
    elif intent == "knowledge":
        query = f"{keyword} 是什么 原理 科普 关键事实"
    elif intent == "recommend":
        query = f"{keyword} 评价 亮点 口碑 简介"
    else:
        query = keyword

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "include_answer": True,
        "max_results": 3,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post("https://api.tavily.com/search", json=payload)
            if resp.status_code != 200:
                return ""
            data = resp.json()
            answer = str(data.get("answer", "")).strip()
            if answer:
                return answer
            results = data.get("results", [])
            snippets: List[str] = []
            for item in results[:3]:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content", "")).strip()
                if content:
                    snippets.append(re.sub(r"\s+", " ", content)[:180])
            return " | ".join(snippets)
    except Exception as e:
        logger.warning(f"拟人插件：Tavily 搜索失败: {e}")
        return ""


async def fetch_baike_summary(keyword: str, *, logger: Any) -> str:
    if not keyword:
        return ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=headers) as client:
            url = f"https://baike.baidu.com/search/word?word={quote(keyword)}"
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            html_text = resp.text

            m = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
                html_text,
                flags=re.IGNORECASE,
            )
            if m:
                desc = re.sub(r"\s+", " ", m.group(1)).strip()
                return desc[:220]

            m = re.search(
                r'<div[^>]*class="[^"]*lemma-summary[^"]*"[^>]*>(.*?)</div>',
                html_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if m:
                text = re.sub(r"<[^>]+>", "", m.group(1))
                text = re.sub(r"\s+", " ", text).strip()
                return text[:220]
    except Exception as e:
        logger.warning(f"拟人插件：百度百科抓取失败: {e}")
    return ""


async def build_grounding_context(
    user_text: str,
    *,
    web_search_enabled: bool,
    get_now: Callable[[], Any],
    logger: Any,
) -> str:
    if not web_search_enabled:
        return ""

    topic = extract_grounding_topic(user_text)
    if not topic:
        return ""

    intent = infer_grounding_intent(topic)
    if intent in {"knowledge", "recommend"}:
        baike_task = asyncio.create_task(fetch_baike_summary(topic, logger=logger))
        tavily_task = asyncio.create_task(fetch_tavily_context(topic, intent=intent, get_now=get_now, logger=logger))
        baike_text, tavily_text = await asyncio.gather(baike_task, tavily_task)
        if not baike_text and not tavily_text:
            return ""
        return (
            "【联网防幻觉校验】\n"
            f"- 话题: {topic}\n"
            f"- 百度百科: {baike_text or '无命中'}\n"
            f"- Tavily: {tavily_text or '无命中'}\n"
            "回答时必须优先使用以上事实，不允许脱离资料自由脑补。"
        )

    if intent == "news":
        tavily_text = await fetch_tavily_context(topic, intent="news", get_now=get_now, logger=logger)
        if not tavily_text:
            return ""
        return (
            "【新闻联网校验】\n"
            f"- 事件: {topic}\n"
            f"- Tavily 最新进展与背景: {tavily_text}\n"
            "请基于进展与始末回答，不要只看标题做推断。"
        )

    return ""


def should_avoid_interrupting(
    group_id: str,
    *,
    is_random_chat: bool,
    get_recent_group_msgs: Callable[[str, int], list[dict]],
    now_ts: Optional[int] = None,
    hot_chat_min_pass_rate: float = 0.2,
) -> bool:
    """
    判断当前是否应避免插话（KY 保护）。

    原逻辑：热聊（20min内16条+4人）时完全拦截。
    新逻辑：热聊时按热度给通过概率，越热概率越低，但保留最低通过率。
    - 非随机聊天（被 @ 或直接对话）：永远不拦截。
    - 冷场：永远不拦截。
    - 热聊：拦截概率 = 1 - max(hot_chat_min_pass_rate, 1 - active_ratio)
    """
    if not is_random_chat:
        return False
    recent = get_recent_group_msgs(group_id, limit=60)
    if not recent:
        return False

    now = int(time.time()) if now_ts is None else int(now_ts)
    window = 20 * 60
    active = [
        msg
        for msg in recent
        if isinstance(msg, dict)
        and not msg.get("is_bot", False)
        and now - int(msg.get("time", 0) or 0) <= window
    ]
    if len(active) < 16:
        return False

    speakers = {str(m.get("nickname", "")).strip() for m in active if m.get("nickname")}
    if len(speakers) < 4:
        return False

    active_ratio = min(1.0, (len(active) - 16) / 44.0)
    pass_rate = max(float(hot_chat_min_pass_rate), 1.0 - active_ratio * 0.8)

    import random

    return random.random() > pass_rate


async def do_web_search(
    query: str,
    *,
    get_now: Callable[[], Any],
    logger: Any,
) -> str:
    """执行联网检索，优先 Tavily/百科，失败再回退 DuckDuckGo。"""
    try:
        topic = extract_grounding_topic(query)
        intent = infer_grounding_intent(topic)

        if intent in {"knowledge", "recommend"}:
            baike_task = asyncio.create_task(fetch_baike_summary(topic, logger=logger))
            tavily_task = asyncio.create_task(fetch_tavily_context(topic, intent=intent, get_now=get_now, logger=logger))
            baike_text, tavily_text = await asyncio.gather(baike_task, tavily_task)
            blocks: List[str] = []
            if baike_text:
                blocks.append(f"百科: {baike_text}")
            if tavily_text:
                blocks.append(f"Tavily: {tavily_text}")
            if blocks:
                return "\n".join(blocks)

        if intent == "news":
            tavily_text = await fetch_tavily_context(topic, intent="news", get_now=get_now, logger=logger)
            if tavily_text:
                return f"Tavily 新闻结果: {tavily_text}"

        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            search_query = topic or query
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": search_query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers={"User-Agent": "Mozilla/5.0 (compatible; PersonificationBot/1.0)"},
            )
            if resp.status_code == 414 and len(search_query) > 48:
                compact_query = search_query[:48]
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": compact_query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; PersonificationBot/1.0)"},
                )
            data: Dict[str, Any] = {}
            try:
                data = resp.json()
            except Exception:
                logger.warning(
                    "拟人插件：DuckDuckGo Instant API 返回非 JSON，"
                    f"status={resp.status_code} content-type={resp.headers.get('content-type', '')}"
                )
            results: List[str] = []
            if data.get("AbstractText"):
                results.append(f"摘要: {data['AbstractText']}")
            for topic_item in data.get("RelatedTopics", [])[:5]:
                if isinstance(topic_item, dict) and topic_item.get("Text"):
                    results.append(f"- {topic_item['Text']}")
            if results:
                return "\n".join(results)

            html_resp = await client.get(
                "https://duckduckgo.com/html/",
                params={"q": query},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            html_text = html_resp.text
            raw_blocks = re.findall(
                r'<div[^>]*class="[^"]*result__body[^"]*"[^>]*>(.*?)</div>\s*</div>',
                html_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            parsed: List[str] = []
            for block in raw_blocks[:8]:
                title_match = re.search(
                    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>',
                    block,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                snippet_match = re.search(
                    r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|<div[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>',
                    block,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                title = ""
                snippet = ""
                if title_match:
                    title = re.sub(r"<[^>]+>", "", title_match.group(1) or "")
                if snippet_match:
                    snippet = re.sub(r"<[^>]+>", "", (snippet_match.group(1) or snippet_match.group(2) or ""))
                title = re.sub(r"\s+", " ", title).strip()
                snippet = re.sub(r"\s+", " ", snippet).strip()
                if title and snippet:
                    parsed.append(f"- {title}: {snippet}")
                elif title:
                    parsed.append(f"- {title}")
            if parsed:
                return "\n".join(parsed[:5])
        return ""
    except Exception as e:
        logger.warning(f"拟人插件：联网搜索失败: {e}")
        return ""
