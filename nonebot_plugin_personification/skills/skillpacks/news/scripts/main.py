from __future__ import annotations

from typing import Callable

from plugin.personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def _daily_news() -> str:
    data = await impl._fetch_v2_data(impl.BASE_URL_DEFAULT, "/v2/60s", local_base_url=impl.LOCAL_BASE_URL_DEFAULT)
    date = str(data.get("date", "今日"))
    items = data.get("news", [])
    tip = str(data.get("tip", "")).strip()
    lines = [f"【今日早报 {date}】"]
    if isinstance(items, list):
        for idx, item in enumerate(items[:8], 1):
            lines.append(f"{idx}. {str(item)}")
    if tip:
        lines.append(f"💬 每日一句：{tip}")
    return "\n".join(lines)


async def _ai_news() -> str:
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        "/v2/ai-news",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    date = str(data.get("date", "今日")).strip() or "今日"
    items = impl._extract_items(data, "news", "items", "list")
    lines = [f"【AI 资讯 {date}】"]
    for idx, item in enumerate(items[:8], 1):
        if not isinstance(item, dict):
            continue
        title = impl._extract_text(item, "title", "name")
        detail = impl._extract_text(item, "summary", "description", "detail", "content")
        source = impl._extract_text(item, "source")
        if not title:
            continue
        line = f"{idx}. {title}"
        if source:
            line += f" [{source}]"
        lines.append(line)
        if detail:
            lines.append(detail[:80])
    return "\n".join(lines) if len(lines) > 1 else "AI 资讯暂时获取失败，稍后再试。"


async def _trending(platform: str) -> str:
    platform = str(platform or "").strip()
    mapped = impl.PLATFORM_MAP.get(platform)
    if not mapped:
        return "不支持该平台，可选：微博、知乎、抖音、B站"
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        f"/v2/{mapped}",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    items = impl._extract_items(data, "list", "items")
    lines = [f"【{platform} 热搜 Top10】"]
    for idx, item in enumerate(items[:10], 1):
        if isinstance(item, dict):
            title = impl._extract_text(item, "title", "name", "word", "hotword", "keyword")
        else:
            title = str(item or "").strip()
        if title:
            lines.append(f"{idx}. {title}")
    return "\n".join(lines)


async def _joke() -> str:
    data = await impl._fetch_v2_data(impl.BASE_URL_DEFAULT, "/v2/duanzi", local_base_url=impl.LOCAL_BASE_URL_DEFAULT)
    return impl._extract_text(data, "duanzi", "content", "text") or "段子暂时获取失败，等会再讲一个。"


async def _history() -> str:
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        "/v2/today-in-history",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    items = impl._extract_items(data, "items", "list")
    lines = ["【历史上的今天】"]
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        year = str(item.get("year", "")).strip()
        title = str(item.get("title", "")).strip()
        if year and title:
            lines.append(f"{year}年：{title}")
    return "\n".join(lines)


async def _epic() -> str:
    data = await impl._fetch_v2_data(impl.BASE_URL_DEFAULT, "/v2/epic", local_base_url=impl.LOCAL_BASE_URL_DEFAULT)
    games = impl._extract_items(data, "list", "games", "items")
    lines = ["【Epic 本周免费游戏】"]
    for game in games[:4]:
        if not isinstance(game, dict):
            continue
        lines.extend(impl._format_epic_game_line(game))
    return "\n".join(lines) if len(lines) > 1 else "Epic 本周暂无免费游戏信息。"


async def _gold() -> str:
    data = await impl._fetch_v2_data(impl.BASE_URL_DEFAULT, "/v2/gold-price", local_base_url=impl.LOCAL_BASE_URL_DEFAULT)
    lines = ["【黄金价格】"]
    for row in list(impl._iter_gold_rows(data))[:5]:
        lines.append(impl._format_gold_row(row))
    return "\n".join(lines) if len(lines) > 1 else "黄金价格暂时获取失败，稍后再试。"


async def _baike(word: str) -> str:
    word = str(word or "").strip()
    if not word:
        return "请告诉我想查的百科词条。"
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        "/v2/baike",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
        params={"word": word},
    )
    title = str(data.get("title") or word).strip()
    summary = str(data.get("content") or data.get("description") or data.get("summary") or "").strip()
    url = str(data.get("url") or data.get("link") or "").strip()
    lines = [f"【百度百科 {title}】"]
    if summary:
        lines.append(summary)
    if url:
        lines.append(url)
    return "\n".join(lines) if len(lines) > 1 else f"没有查到“{word}”的百科摘要。"


async def _exchange(base_currency: str = "", quote_currency: str = "") -> str:
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        "/v2/exchange-rate",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    lines = impl._build_exchange_lines(data, base_currency=base_currency, quote_currency=quote_currency)
    return "\n".join(lines)


async def run(topic: str = "daily", platform: str = "微博", keyword: str = "", base_currency: str = "", quote_currency: str = "") -> str:
    topic_key = str(topic or "daily").strip().lower()
    handlers: dict[str, Callable[[], object]] = {
        "daily": _daily_news,
        "news": _daily_news,
        "ai_news": _ai_news,
        "ai-news": _ai_news,
        "ai": _ai_news,
        "trending": lambda: _trending(platform),
        "joke": _joke,
        "history": _history,
        "epic": _epic,
        "gold": _gold,
        "baike": lambda: _baike(keyword),
        "exchange": lambda: _exchange(base_currency=base_currency, quote_currency=quote_currency),
    }
    target = handlers.get(topic_key)
    if target is None:
        return "topic 可选: daily, trending, joke, history, epic, gold, baike, exchange"
    try:
        return str(await target())
    except Exception as e:
        return f"新闻能力调用失败: {e}"


def build_tools(runtime: SkillRuntime):
    if not getattr(runtime.plugin_config, "personification_60s_enabled", True):
        return []
    base = str(
        getattr(runtime.plugin_config, "personification_60s_api_base", "https://60s.viki.moe") or ""
    ).strip().rstrip("/") or "https://60s.viki.moe"
    local_base = str(
        getattr(runtime.plugin_config, "personification_60s_local_api_base", "http://127.0.0.1:4399") or ""
    ).strip().rstrip("/") or "http://127.0.0.1:4399"
    logger = runtime.logger
    return [
        impl.build_daily_news_tool(base, logger, local_base),
        impl.build_ai_news_tool(base, logger, local_base),
        impl.build_trending_tool(base, logger, local_base),
        impl.build_joke_tool(base, logger, local_base),
        impl.build_history_today_tool(base, logger, local_base),
        impl.build_epic_games_tool(base, logger, local_base),
        impl.build_gold_price_tool(base, logger, local_base),
        impl.build_baike_tool(base, logger, local_base),
        impl.build_exchange_rate_tool(base, logger, local_base),
    ]
