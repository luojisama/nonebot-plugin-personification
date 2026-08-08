from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

import httpx

from plugin.personification.agent.tool_registry import AgentTool


BASE_URL_DEFAULT = "https://60s.viki.moe"
LOCAL_BASE_URL_DEFAULT = "http://127.0.0.1:4399"

PLATFORM_MAP = {
    "微博": "weibo",
    "知乎": "zhihu",
    "抖音": "douyin",
    "B站": "bili",
    "哔哩哔哩": "bili",
    "百度": "baidu/hot",
    "百度热搜": "baidu/hot",
    "头条": "toutiao",
    "今日头条": "toutiao",
    "小红书": "rednote",
    "RedNote": "rednote",
}

TECH_NEWS_SOURCE_MAP = {
    "IT之家": ("it-news", "IT之家"),
    "IT": ("it-news", "IT之家"),
    "Hacker News": ("hacker-news/top", "Hacker News"),
    "HackerNews": ("hacker-news/top", "Hacker News"),
    "HN": ("hacker-news/top", "Hacker News"),
}


DAILY_NEWS_DESCRIPTION = """获取今天的60秒新闻早报，包含当天重要新闻摘要和每日一句。
适合场景：用户问"今天有什么新闻"、"今天发生了什么"、"给我说说今天的大事"。
挑1-3条最有意思的新闻分享，加上自己的感受，不要逐条列举，不要说"根据新闻API"。"""

TRENDING_DESCRIPTION = """获取各平台实时热搜榜单。
支持平台：微博、知乎、抖音、B站（哔哩哔哩）、百度、头条、小红书。
适合场景：用户问"微博在讨论什么"、"知乎热搜是啥"、"B站最近流行什么"。
返回 top10，可以加入自己对某个话题的看法。"""

AI_NEWS_DESCRIPTION = """获取 AI 领域今日资讯。
适合场景：用户问"AI 最近有什么新闻"、"今天 AI 圈有什么动静"、"给我来点 AI 资讯"。
当天尚未发布时自动返回最近一期，并在标题中保留实际日期。
挑 1-3 条重点自然概括，不要机械逐条播报，不要说"根据 API"。"""

TECH_NEWS_DESCRIPTION = """获取科技资讯，支持 IT之家和 Hacker News。
适合场景：用户问最近科技圈、数码行业、开发者社区有什么新闻。
source 可选 IT之家或 Hacker News；没有指定时使用 IT之家。"""

JOKE_DESCRIPTION = """获取一条随机搞笑段子。
适合场景：用户让你讲笑话、群聊气氛需要活跃时。
不要主动频繁调用，每次对话最多使用一次。"""

HISTORY_TODAY_DESCRIPTION = """获取历史上的今天发生的重要事件。
适合场景：用户问"今天历史上发生了什么"、聊到某个历史事件时想了解背景。
返回1-2条最有意思的历史事件，自然引出。"""

EPIC_GAMES_DESCRIPTION = """获取 Epic Games 本周免费领取的游戏信息。
适合场景：用户聊到游戏、问"Epic 有啥免费游戏"。"""

GOLD_PRICE_DESCRIPTION = """获取最新黄金价格信息。
适合场景：用户问"黄金多少钱一克"、"今天金价多少"、"最近金价怎么样"。"""

BAIKE_DESCRIPTION = """查询百度百科词条摘要。
适合场景：用户问"某个概念是什么"、"给我查下某人的百科"、"帮我科普一下"。"""

EXCHANGE_RATE_DESCRIPTION = """查询货币汇率。
适合场景：用户问"美元兑人民币多少"、"日元汇率"、"换汇比例是多少"。"""


async def _request_json(base_url: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
    endpoint = f"{base_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(endpoint, params=params)
        resp.raise_for_status()
        return resp.json()


async def _fetch_v2_data(
    remote_base_url: str,
    path: str,
    *,
    local_base_url: str | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    bases: list[str] = []
    for base in (local_base_url, remote_base_url):
        normalized = str(base or "").strip().rstrip("/")
        if normalized and normalized not in bases:
            bases.append(normalized)

    if not bases:
        raise ValueError("no available 60s api base url")

    errors: list[str] = []
    for base in bases:
        try:
            payload = await _request_json(base, path, params=params)
            if not isinstance(payload, dict):
                raise ValueError("invalid v2 payload")
            if payload.get("code") != 200:
                raise ValueError(f"v2 code not success: {payload.get('code')}")
            return payload.get("data", {})
        except Exception as e:
            errors.append(f"{base}: {e}")

    raise ValueError("; ".join(errors))


def _to_mmdd(iso_text: str) -> str:
    raw = (iso_text or "").strip()
    if not raw:
        return "未知时间"
    try:
        normalized = raw.replace("/", "-").replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return f"{dt.month:02d}月{dt.day:02d}日"
    except Exception:
        return raw


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("list", "items", "rates", "metals", "games"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _extract_items(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            nested = data.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _extract_text(data: Any, *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _canonical_failure(error: str) -> str:
    return json.dumps(
        {"ok": False, "error": error},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _valid_ai_news_items(data: Any) -> list[dict[str, Any]]:
    items = _extract_items(data, "news", "items", "list")
    return [item for item in items if isinstance(item, dict) and _extract_text(item, "title", "name")]


def _latest_ai_news_data(data: Any) -> dict[str, Any]:
    items = _valid_ai_news_items(data)
    dated_items = [item for item in items if _extract_text(item, "date")]
    if dated_items:
        latest_date = max(_extract_text(item, "date") for item in dated_items)
        return {
            "date": latest_date,
            "news": [item for item in dated_items if _extract_text(item, "date") == latest_date],
        }
    date = _extract_text(data, "date")
    return {"date": date if date and date != "all" else "最近一期", "news": items}


async def _fetch_latest_ai_news(
    remote_base_url: str,
    *,
    local_base_url: str | None = None,
) -> dict[str, Any]:
    primary_error: Exception | None = None
    primary_data: Any = {}
    try:
        primary_data = await _fetch_v2_data(
            remote_base_url,
            "/v2/ai-news",
            local_base_url=local_base_url,
        )
        if _valid_ai_news_items(primary_data):
            return _latest_ai_news_data(primary_data)
    except Exception as exc:
        primary_error = exc

    try:
        history_data = await _fetch_v2_data(
            remote_base_url,
            "/v2/ai-news",
            local_base_url=local_base_url,
            params={"all": 1},
        )
    except Exception as history_error:
        if primary_error is not None:
            raise primary_error from history_error
        raise

    latest = _latest_ai_news_data(history_data)
    if _valid_ai_news_items(latest):
        return latest
    if primary_error is not None:
        raise primary_error
    return _latest_ai_news_data(primary_data)


def _format_ai_news(data: Any) -> str:
    date = _extract_text(data, "date") or "今日"
    lines = [f"【AI 资讯 {date}】"]
    for idx, item in enumerate(_valid_ai_news_items(data)[:8], 1):
        title = _extract_text(item, "title", "name")
        detail = _extract_text(item, "summary", "description", "detail", "content")
        source = _extract_text(item, "source")
        line = f"{idx}. {title}"
        if source:
            line += f" [{source}]"
        lines.append(line)
        if detail:
            lines.append(detail[:80])
    return "\n".join(lines) if len(lines) > 1 else _canonical_failure("no_results")


def _format_epic_end_date(game: dict[str, Any]) -> str:
    return _to_mmdd(
        _extract_text(
            game,
            "free_end",
            "end",
            "freeEnd",
        )
    )


def _format_epic_game_line(game: dict[str, Any]) -> list[str]:
    title = _extract_text(game, "title", "name")
    if not title:
        return []
    is_free_now = bool(game.get("is_free_now"))
    end_date = _format_epic_end_date(game)
    status = "免费至" if is_free_now else "预计免费至"
    lines = [f"《{title}》 {status} {end_date}"]
    desc = _extract_text(game, "description", "summary", "content")
    if desc:
        lines.append(desc[:50])
    return lines


def _iter_gold_rows(data: Any) -> Iterable[dict[str, Any]]:
    rows = _as_list(data)
    if rows:
        for row in rows:
            if isinstance(row, dict):
                yield row
        return

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("name", key)
                yield row


def _format_gold_row(row: dict[str, Any]) -> str:
    name = str(
        row.get("name")
        or row.get("brand")
        or row.get("title")
        or row.get("shop")
        or row.get("type")
        or "黄金"
    ).strip()
    price = row.get("price")
    if price is None:
        price = row.get("value")
    if price is None:
        price = row.get("now")
    unit = str(row.get("unit") or "元/克").strip()
    suffix = f"{price}{unit}" if price not in (None, "") else "暂无报价"
    return f"{name}: {suffix}"


def _coerce_rate_lookup(data: Any) -> tuple[str, dict[str, float]]:
    base_code = "CNY"
    rates: dict[str, float] = {}

    if isinstance(data, dict):
        base_code = str(data.get("base_code") or data.get("base") or "CNY").upper()

        if isinstance(data.get("rates"), dict):
            for code, rate in data["rates"].items():
                try:
                    rates[str(code).upper()] = float(rate)
                except Exception:
                    continue

        for row in _as_list(data):
            if not isinstance(row, dict):
                continue
            code = str(row.get("currency") or row.get("code") or row.get("symbol") or "").upper().strip()
            if not code:
                continue
            rate = row.get("rate")
            if rate is None:
                rate = row.get("value")
            try:
                rates[code] = float(rate)
            except Exception:
                continue

    return base_code, rates


def _format_rate(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _build_exchange_lines(
    data: Any,
    *,
    base_currency: str = "",
    quote_currency: str = "",
) -> list[str]:
    reference_code, rates = _coerce_rate_lookup(data)
    if not rates:
        return ["汇率数据暂不可用。"]

    requested_base = (base_currency or reference_code).strip().upper() or reference_code
    requested_quote = (quote_currency or "").strip().upper()

    if requested_base == reference_code:
        base_to_reference = 1.0
    elif requested_base in rates and rates[requested_base] > 0:
        base_to_reference = 1 / rates[requested_base]
    else:
        return [f"未找到基准货币 {requested_base} 的汇率。"]

    def _convert(target: str) -> float | None:
        if target == reference_code:
            return base_to_reference
        if target not in rates:
            return None
        return base_to_reference * rates[target]

    if requested_quote:
        rate_value = _convert(requested_quote)
        if rate_value is None:
            return [f"未找到目标货币 {requested_quote} 的汇率。"]
        return [f"1 {requested_base} ≈ {_format_rate(rate_value)} {requested_quote}"]

    major_codes = ["USD", "EUR", "JPY", "HKD", "GBP"]
    lines = [f"【汇率参考 基准 {requested_base}】"]
    for code in major_codes:
        if code == requested_base:
            continue
        rate_value = _convert(code)
        if rate_value is None:
            continue
        lines.append(f"1 {requested_base} ≈ {_format_rate(rate_value)} {code}")
    return lines if len(lines) > 1 else ["暂无可展示的主流汇率。"]


def build_daily_news_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler() -> str:
        try:
            data = await _fetch_v2_data(
                remote_base_url,
                "/v2/60s",
                local_base_url=local_base_url,
            )
            date = str(data.get("date", "今日"))
            news_items = data.get("news", [])
            tip = str(data.get("tip", "")).strip()

            lines = [f"【今日早报 {date}】"]
            if isinstance(news_items, list):
                for item in news_items[:15]:
                    text = str(item or "").strip()
                    if text:
                        lines.append(f"{len(lines)}. {text}")
            if tip:
                lines.append(f"💬 每日一句：{tip}")
            return "\n".join(lines) if len(lines) > 1 else _canonical_failure("no_results")
        except Exception:
            logger.warning("[news] get_daily_news 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_daily_news",
        description=DAILY_NEWS_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )


def build_ai_news_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler() -> str:
        try:
            data = await _fetch_latest_ai_news(
                remote_base_url,
                local_base_url=local_base_url,
            )
            return _format_ai_news(data)
        except Exception:
            logger.warning("[news] get_ai_news 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_ai_news",
        description=AI_NEWS_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )


def _resolve_tech_news_source(source: str) -> tuple[str, str] | None:
    requested = str(source or "IT之家").strip() or "IT之家"
    requested_key = requested.casefold()
    for alias, target in TECH_NEWS_SOURCE_MAP.items():
        if alias.casefold() == requested_key:
            return target
    return None


def _format_tech_news(data: Any, source_name: str) -> str:
    items = _extract_items(data, "news", "items", "list")
    lines = [f"【{source_name} 科技资讯】"]
    item_number = 0
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        title = _extract_text(item, "title", "name")
        if not title:
            continue
        item_number += 1
        score = item.get("score")
        score_suffix = (
            f" [{score} points]"
            if source_name == "Hacker News" and score not in (None, "")
            else ""
        )
        lines.append(f"{item_number}. {title}{score_suffix}")
        detail = _extract_text(item, "description", "summary", "detail")
        if detail:
            lines.append(detail[:100])
    return "\n".join(lines) if item_number else _canonical_failure("no_results")


def build_tech_news_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler(source: str = "IT之家") -> str:
        resolved = _resolve_tech_news_source(source)
        if resolved is None:
            return _canonical_failure("invalid_source")
        endpoint, source_name = resolved
        try:
            data = await _fetch_v2_data(
                remote_base_url,
                f"/v2/{endpoint}",
                local_base_url=local_base_url,
            )
            return _format_tech_news(data, source_name)
        except Exception:
            logger.warning("[news] get_tech_news 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_tech_news",
        description=TECH_NEWS_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["IT之家", "Hacker News"],
                    "description": "资讯来源；未指定时默认 IT之家",
                },
            },
            "required": [],
        },
        handler=_handler,
    )


def _format_trending(data: Any, platform_name: str) -> str:
    items = _extract_items(data, "list", "items")
    lines = [f"【{platform_name} 热搜 Top10】"]
    item_number = 0
    for item in items[:10]:
        if isinstance(item, dict):
            title = _extract_text(item, "title", "name", "word", "hotword", "keyword")
        else:
            title = str(item or "").strip()
        if title:
            item_number += 1
            lines.append(f"{item_number}. {title}")
    return "\n".join(lines) if item_number else _canonical_failure("no_results")


def build_trending_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler(platform: str) -> str:
        platform_name = str(platform or "").strip()
        mapped = PLATFORM_MAP.get(platform_name)
        if not mapped:
            return _canonical_failure("invalid_platform")

        try:
            data = await _fetch_v2_data(
                remote_base_url,
                f"/v2/{mapped}",
                local_base_url=local_base_url,
            )
            return _format_trending(data, platform_name)
        except Exception:
            logger.warning("[news] get_trending 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_trending",
        description=TRENDING_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": ["微博", "知乎", "抖音", "B站", "百度", "头条", "小红书"],
                    "description": "热榜平台",
                },
            },
            "required": ["platform"],
        },
        handler=_handler,
    )


def build_joke_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler() -> str:
        try:
            data = await _fetch_v2_data(
                remote_base_url,
                "/v2/duanzi",
                local_base_url=local_base_url,
            )
            content = _extract_text(data, "duanzi", "content", "text")
            return content or _canonical_failure("no_results")
        except Exception:
            logger.warning("[news] get_joke 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_joke",
        description=JOKE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )


def build_history_today_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler() -> str:
        try:
            data = await _fetch_v2_data(
                remote_base_url,
                "/v2/today-in-history",
                local_base_url=local_base_url,
            )
            items = _extract_items(data, "items", "list")
            lines = ["【历史上的今天】"]
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                year = str(item.get("year", "")).strip()
                title = str(item.get("title", "")).strip()
                if year and title:
                    lines.append(f"{year}年：{title}")
            return "\n".join(lines) if len(lines) > 1 else _canonical_failure("no_results")
        except Exception:
            logger.warning("[news] get_history_today 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_history_today",
        description=HISTORY_TODAY_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )


def build_epic_games_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler() -> str:
        try:
            data = await _fetch_v2_data(
                remote_base_url,
                "/v2/epic",
                local_base_url=local_base_url,
            )
            games = _extract_items(data, "list", "games", "items")
            lines = ["【Epic 本周免费游戏】"]
            for game in games[:4]:
                if not isinstance(game, dict):
                    continue
                lines.extend(_format_epic_game_line(game))
            return "\n".join(lines) if len(lines) > 1 else _canonical_failure("no_results")
        except Exception:
            logger.warning("[news] get_epic_games 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_epic_games",
        description=EPIC_GAMES_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )


def build_gold_price_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler() -> str:
        try:
            data = await _fetch_v2_data(
                remote_base_url,
                "/v2/gold-price",
                local_base_url=local_base_url,
            )
            lines = ["【黄金价格】"]
            for row in list(_iter_gold_rows(data))[:5]:
                lines.append(_format_gold_row(row))
            return "\n".join(lines) if len(lines) > 1 else _canonical_failure("no_results")
        except Exception:
            logger.warning("[news] get_gold_price 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_gold_price",
        description=GOLD_PRICE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )


def build_baike_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler(word: str) -> str:
        keyword = str(word or "").strip()
        if not keyword:
            return _canonical_failure("invalid_word")

        try:
            data = await _fetch_v2_data(
                remote_base_url,
                "/v2/baike",
                local_base_url=local_base_url,
                params={"word": keyword},
            )
            title = str(data.get("title") or keyword).strip()
            summary = str(
                data.get("content")
                or data.get("description")
                or data.get("summary")
                or ""
            ).strip()
            url = str(data.get("url") or data.get("link") or "").strip()

            lines = [f"【百度百科 {title}】"]
            if summary:
                lines.append(summary)
            if url:
                lines.append(url)
            return "\n".join(lines) if len(lines) > 1 else _canonical_failure("no_results")
        except Exception:
            logger.warning("[news] get_baike_entry 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_baike_entry",
        description=BAIKE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "word": {"type": "string", "description": "要查询的百科词条关键词"},
            },
            "required": ["word"],
        },
        handler=_handler,
    )


def build_exchange_rate_tool(
    remote_base_url: str,
    logger: Any,
    local_base_url: str | None = None,
) -> AgentTool:
    async def _handler(base_currency: str = "", quote_currency: str = "") -> str:
        try:
            data = await _fetch_v2_data(
                remote_base_url,
                "/v2/exchange-rate",
                local_base_url=local_base_url,
            )
            _reference_code, rates = _coerce_rate_lookup(data)
            if not rates:
                return _canonical_failure("no_results")
            lines = _build_exchange_lines(
                data,
                base_currency=base_currency,
                quote_currency=quote_currency,
            )
            return "\n".join(lines)
        except Exception:
            logger.warning("[news] get_exchange_rate 失败")
            return _canonical_failure("fetch_failed")

    return AgentTool(
        name="get_exchange_rate",
        description=EXCHANGE_RATE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "base_currency": {"type": "string", "description": "基准货币代码，如 CNY、USD、JPY"},
                "quote_currency": {"type": "string", "description": "目标货币代码，如 USD、CNY、EUR"},
            },
            "required": [],
        },
        handler=_handler,
    )
