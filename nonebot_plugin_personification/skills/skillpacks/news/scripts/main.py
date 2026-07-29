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
        for item in items[:8]:
            text = str(item or "").strip()
            if text:
                lines.append(f"{len(lines)}. {text}")
    if tip:
        lines.append(f"💬 每日一句：{tip}")
    return "\n".join(lines) if len(lines) > 1 else impl._canonical_failure("no_results")


async def _ai_news() -> str:
    data = await impl._fetch_latest_ai_news(
        impl.BASE_URL_DEFAULT,
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    return impl._format_ai_news(data)


async def _tech_news(source: str = "IT之家") -> str:
    resolved = impl._resolve_tech_news_source(source)
    if resolved is None:
        return impl._canonical_failure("invalid_source")
    endpoint, source_name = resolved
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        f"/v2/{endpoint}",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    return impl._format_tech_news(data, source_name)


async def _trending(platform: str) -> str:
    platform = str(platform or "").strip()
    mapped = impl.PLATFORM_MAP.get(platform)
    if not mapped:
        return impl._canonical_failure("invalid_platform")
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        f"/v2/{mapped}",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    return impl._format_trending(data, platform)


async def _joke() -> str:
    data = await impl._fetch_v2_data(impl.BASE_URL_DEFAULT, "/v2/duanzi", local_base_url=impl.LOCAL_BASE_URL_DEFAULT)
    return impl._extract_text(data, "duanzi", "content", "text") or impl._canonical_failure("no_results")


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
    return "\n".join(lines) if len(lines) > 1 else impl._canonical_failure("no_results")


async def _epic() -> str:
    data = await impl._fetch_v2_data(impl.BASE_URL_DEFAULT, "/v2/epic", local_base_url=impl.LOCAL_BASE_URL_DEFAULT)
    games = impl._extract_items(data, "list", "games", "items")
    lines = ["【Epic 本周免费游戏】"]
    for game in games[:4]:
        if not isinstance(game, dict):
            continue
        lines.extend(impl._format_epic_game_line(game))
    return "\n".join(lines) if len(lines) > 1 else impl._canonical_failure("no_results")


async def _gold() -> str:
    data = await impl._fetch_v2_data(impl.BASE_URL_DEFAULT, "/v2/gold-price", local_base_url=impl.LOCAL_BASE_URL_DEFAULT)
    lines = ["【黄金价格】"]
    for row in list(impl._iter_gold_rows(data))[:5]:
        lines.append(impl._format_gold_row(row))
    return "\n".join(lines) if len(lines) > 1 else impl._canonical_failure("no_results")


async def _baike(word: str) -> str:
    word = str(word or "").strip()
    if not word:
        return impl._canonical_failure("invalid_word")
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
    return "\n".join(lines) if len(lines) > 1 else impl._canonical_failure("no_results")


async def _exchange(base_currency: str = "", quote_currency: str = "") -> str:
    data = await impl._fetch_v2_data(
        impl.BASE_URL_DEFAULT,
        "/v2/exchange-rate",
        local_base_url=impl.LOCAL_BASE_URL_DEFAULT,
    )
    _reference_code, rates = impl._coerce_rate_lookup(data)
    if not rates:
        return impl._canonical_failure("no_results")
    lines = impl._build_exchange_lines(data, base_currency=base_currency, quote_currency=quote_currency)
    return "\n".join(lines)


async def run(
    topic: str = "daily",
    platform: str = "微博",
    source: str = "IT之家",
    keyword: str = "",
    base_currency: str = "",
    quote_currency: str = "",
) -> str:
    topic_key = str(topic or "daily").strip().lower()
    handlers: dict[str, Callable[[], object]] = {
        "daily": _daily_news,
        "news": _daily_news,
        "ai_news": _ai_news,
        "ai-news": _ai_news,
        "ai": _ai_news,
        "tech": lambda: _tech_news(source),
        "tech_news": lambda: _tech_news(source),
        "tech-news": lambda: _tech_news(source),
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
        return "topic 可选: daily, ai_news, tech_news, trending, joke, history, epic, gold, baike, exchange"
    try:
        return str(await target())
    except Exception:
        return impl._canonical_failure("fetch_failed")


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
        impl.build_tech_news_tool(base, logger, local_base),
        impl.build_trending_tool(base, logger, local_base),
        impl.build_joke_tool(base, logger, local_base),
        impl.build_history_today_tool(base, logger, local_base),
        impl.build_epic_games_tool(base, logger, local_base),
        impl.build_gold_price_tool(base, logger, local_base),
        impl.build_baike_tool(base, logger, local_base),
        impl.build_exchange_rate_tool(base, logger, local_base),
    ]
