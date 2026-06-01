from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

import httpx

# 复用现有基础设施，避免重复造轮子：
# - resource_collector：免 key 多搜索引擎抓取 + 工具函数（_strip_tags / search_web）
# - wiki_search：维基百科 / 萌娘百科 / Fandom 条目查询
# - core.web_fetch：带 SSRF 防护/代理/正文抽取的网页抓取（抓官方公告页）
# 采用绝对导入（与 parallel_research 一致的可靠写法）；builtin skillpack 经合成包名加载，
# 3 级相对导入不可靠，故用 plugin.personification.* 绝对路径。
from plugin.personification.skills.skillpacks.resource_collector.scripts import impl as rc_impl
from plugin.personification.skills.skillpacks.wiki_search.scripts import impl as wiki_impl
from plugin.personification.core.web_fetch import fetch_web_page


_STEAM_STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
_STEAM_NEWS_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 各 aspect 拼进社区搜索的关键词。
_ASPECT_SEARCH_KEYWORDS = {
    "update": "更新公告 新版本 版本更新 patch notes",
    "guide": "攻略 图文攻略 流程 通关",
    "story": "剧情 设定 世界观 结局解析",
    "tips": "技巧 进阶技巧 小技巧 注意事项 tips",
}

_ASPECT_LABEL = {
    "update": "更新/补丁公告",
    "guide": "攻略",
    "story": "剧情/设定",
    "tips": "技巧",
}

# 攻略/技巧定向搜索时优先关注的社区站点（用于排序加权，不做硬过滤）。
_DEFAULT_COMMUNITY_SITES = [
    # 综合站
    "gamersky.com",
    "3dmgame.com",
    "17173.com",
    "ign.com",
    "fextralife.com",
    "gamefaqs.gamespot.com",
    "reddit.com",
    "bilibili.com",
    "tieba.baidu.com",
    # 手游 / 厂商社区（腾讯/网易/米哈游等非 Steam 平台覆盖）
    "taptap.cn",
    "taptap.com",
    "bbs.mihoyo.com",
    "miyoushe.com",
    "nga.cn",
    "ngabbs.com",
    # 主机社区（PlayStation / Switch / Xbox）
    "a9vg.com",
    "psnine.com",
]

# 头部游戏官方公告源精选映射：预设 10 主流网游 + 10 主流单机（近期、不限平台，
# 含 Switch/PS 独占等 Steam 覆盖不到的）。names 为别名（匹配时大小写/空格/标点不敏感，
# 支持子串包含）；urls 为官方新闻/公告页。
# 注意：部分官网是 JS 渲染的 SPA，正文可能抽取不全——此时仍会把官方链接作为权威来源给出。
# URL 可能随官网改版失效，需要时更新此表即可。
_OFFICIAL_GAME_SOURCES: list[dict[str, Any]] = [
    # ===== 10 主流网游 / 服务型游戏 =====
    {"names": ["原神", "genshin", "genshin impact"], "label": "原神官网",
     "urls": ["https://ys.mihoyo.com/main/news/index/2"]},
    {"names": ["崩坏：星穹铁道", "崩坏星穹铁道", "星穹铁道", "星铁", "honkai star rail", "hsr", "star rail"],
     "label": "星穹铁道官网", "urls": ["https://sr.mihoyo.com/news"]},
    {"names": ["绝区零", "zenless zone zero", "zzz"], "label": "绝区零官网",
     "urls": ["https://zzz.mihoyo.com/main/news"]},
    {"names": ["鸣潮", "wuthering waves"], "label": "鸣潮官网",
     "urls": ["https://mc.kurogames.com/"]},
    {"names": ["王者荣耀", "王者", "honor of kings"], "label": "王者荣耀官网",
     "urls": ["https://pvp.qq.com/"]},
    {"names": ["三角洲行动", "三角洲", "delta force"], "label": "三角洲行动官网",
     "urls": ["https://df.qq.com/"]},
    {"names": ["英雄联盟", "lol", "league of legends", "撸啊撸"], "label": "英雄联盟官方",
     "urls": ["https://www.leagueoflegends.com/zh-cn/news/tags/patch-notes/"]},
    {"names": ["无畏契约", "valorant", "瓦罗兰特"], "label": "无畏契约官方",
     "urls": ["https://playvalorant.com/zh-cn/news/"]},
    {"names": ["永劫无间", "naraka", "naraka bladepoint"], "label": "永劫无间官网",
     "urls": ["https://www.yjwujian.com/"]},
    {"names": ["守望先锋", "守望先锋2", "overwatch", "overwatch 2", "ow2"], "label": "守望先锋官方",
     "urls": ["https://overwatch.blizzard.com/zh-cn/news/"]},
    # ===== 10 主流单机 / 买断制（近期） =====
    {"names": ["黑神话：悟空", "黑神话悟空", "黑神话", "black myth wukong", "black myth: wukong", "wukong"],
     "label": "黑神话悟空官网", "urls": ["https://www.heishenhua.com/"]},
    {"names": ["怪物猎人荒野", "怪物猎人：荒野", "怪猎荒野", "monster hunter wilds", "mh wilds"],
     "label": "怪物猎人荒野官网", "urls": ["https://www.monsterhunter.com/wilds/"]},
    {"names": ["艾尔登法环", "老头环", "elden ring", "黄金树幽影", "shadow of the erdtree"],
     "label": "艾尔登法环官网", "urls": ["https://www.eldenring.jp/"]},
    {"names": ["塞尔达传说：王国之泪", "王国之泪", "塞尔达王国之泪", "tears of the kingdom", "totk"],
     "label": "塞尔达·王国之泪官网", "urls": ["https://www.zelda.com/tears-of-the-kingdom/"]},
    {"names": ["最终幻想7重生", "最终幻想7：重生", "最终幻想7 重生", "ff7重生", "ff7 rebirth",
               "final fantasy vii rebirth"], "label": "FF7 重生官网",
     "urls": ["https://ffvii.square-enix-games.com/"]},
    {"names": ["赛博朋克2077", "赛博朋克 2077", "2077", "cyberpunk 2077", "cyberpunk2077"],
     "label": "赛博朋克2077官网", "urls": ["https://www.cyberpunk.net/"]},
    {"names": ["博德之门3", "博德之门 3", "bg3", "baldur's gate 3", "baldurs gate 3"],
     "label": "博德之门3官网", "urls": ["https://baldursgate3.game/"]},
    {"names": ["刺客信条：影", "刺客信条影", "刺客信条 影", "assassin's creed shadows", "ac shadows"],
     "label": "刺客信条·影官网",
     "urls": ["https://www.ubisoft.com/zh-cn/game/assassins-creed/shadows"]},
    {"names": ["战神：诸神黄昏", "战神诸神黄昏", "战神5", "god of war ragnarok", "god of war ragnarök",
               "gow ragnarok"], "label": "战神·诸神黄昏官网",
     "urls": ["https://godofwar.playstation.com/"]},
    {"names": ["双影奇境", "split fiction"], "label": "双影奇境官网",
     "urls": ["https://www.ea.com/games/split-fiction"]},
]


def _resolve_logger(logger: Any) -> Any:
    return rc_impl._get_logger(logger)


def _normalize_aspect(aspect: str) -> str:
    value = str(aspect or "").strip().lower()
    if value in _ASPECT_SEARCH_KEYWORDS:
        return value
    # 一些常见同义词兜底
    alias = {
        "patch": "update",
        "patchnotes": "update",
        "news": "update",
        "walkthrough": "guide",
        "guides": "guide",
        "plot": "story",
        "lore": "story",
        "tip": "tips",
        "trick": "tips",
        "tricks": "tips",
    }
    return alias.get(value, "guide")


def _community_sites(plugin_config: Any) -> list[str]:
    raw = getattr(plugin_config, "personification_game_info_community_sites", None)
    sites: list[str] = []
    if isinstance(raw, (list, tuple)):
        sites = [str(s).strip() for s in raw if str(s).strip()]
    elif isinstance(raw, str) and raw.strip():
        sites = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
    return sites or list(_DEFAULT_COMMUNITY_SITES)


def _timeout(plugin_config: Any) -> float:
    try:
        return float(getattr(plugin_config, "personification_game_info_timeout", 15.0) or 15.0)
    except (TypeError, ValueError):
        return 15.0


async def _resolve_steam_appid(
    name: str,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
    timeout: float,
) -> tuple[Optional[int], str]:
    """用 Steam store 搜索把游戏名解析为 appid（免 key）。找不到返回 (None, "")。"""
    term = str(name or "").strip()
    if not term:
        return None, ""
    try:
        resp = await http_client.get(
            _STEAM_STORE_SEARCH,
            params={"term": term, "cc": "us", "l": "schinese"},
            headers=_HEADERS,
            timeout=timeout,
        )
        data = resp.json()
    except Exception as exc:
        logger.debug(f"[game_info] steam storesearch failed: {exc}")
        return None, ""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return None, ""
    first = items[0]
    if not isinstance(first, dict):
        return None, ""
    appid = first.get("id")
    try:
        appid_int = int(appid)
    except (TypeError, ValueError):
        return None, ""
    return appid_int, str(first.get("name", term) or term)


async def _fetch_steam_news(
    appid: int,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
    timeout: float,
    count: int = 5,
) -> list[dict[str, Any]]:
    try:
        resp = await http_client.get(
            _STEAM_NEWS_API,
            params={"appid": appid, "count": count, "maxlength": 400, "l": "schinese"},
            headers=_HEADERS,
            timeout=timeout,
        )
        data = resp.json()
    except Exception as exc:
        logger.debug(f"[game_info] steam news failed appid={appid}: {exc}")
        return []
    appnews = data.get("appnews") if isinstance(data, dict) else None
    items = appnews.get("newsitems") if isinstance(appnews, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _format_steam_updates(game: str, matched_name: str, news: list[dict[str, Any]]) -> str:
    lines = [f"【{game} 的更新/公告 · Steam 官方】(匹配条目：{matched_name})"]
    for idx, item in enumerate(news[:5], 1):
        title = str(item.get("title", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        contents = rc_impl._strip_tags(str(item.get("contents", "") or "")).strip()
        feed = str(item.get("feedlabel", "") or "").strip()
        if not title:
            continue
        head = f"{idx}. {title}"
        if feed:
            head += f"（{feed}）"
        lines.append(head)
        if contents:
            lines.append(f"   {contents[:160]}")
        if url:
            lines.append(f"   链接：{url}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _community_search(
    query: str,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
    community_sites: list[str],
    limit: int = 6,
) -> list[dict[str, Any]]:
    """复用 resource_collector 的免 key 搜索，按社区站点做软性加权排序。"""
    try:
        raw = await rc_impl.search_web(query, limit, http_client=http_client, logger=logger)
        payload = json.loads(raw)
    except Exception as exc:
        logger.debug(f"[game_info] community search failed: {exc}")
        return []
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(results, list):
        return []

    def _site_rank(item: dict[str, Any]) -> int:
        url = str(item.get("url", "") or "").lower()
        for i, site in enumerate(community_sites):
            if site in url:
                return i
        return len(community_sites) + 1

    results = sorted(
        [r for r in results if isinstance(r, dict)],
        key=_site_rank,
    )
    return results[:limit]


def _format_community(label: str, game: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return ""
    lines = [f"【{game} 的{label} · 社区搜索】"]
    for idx, item in enumerate(results[:6], 1):
        title = str(item.get("title", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        snippet = str(item.get("snippet", "") or "").strip()
        source = str(item.get("source", "") or "").strip()
        if not title and not url:
            continue
        head = f"{idx}. {title or url}"
        if source:
            head += f" [{source}]"
        lines.append(head)
        if snippet:
            lines.append(f"   {snippet[:160]}")
        if url:
            lines.append(f"   链接：{url}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _format_story(
    game: str,
    query: str,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> str:
    """剧情/设定优先走 wiki_lookup（Fandom/萌娘/维基）。"""
    wiki_query = f"{game} {query}".strip() if query else f"{game} 剧情 设定"
    try:
        raw = await wiki_impl.wiki_lookup(wiki_query, http_client=http_client, logger=logger)
    except Exception as exc:
        logger.debug(f"[game_info] wiki_lookup failed: {exc}")
        return ""
    text = str(raw or "").strip()
    if not text or text.startswith("未找到"):
        return ""
    # wiki_lookup 返回 JSON 候选；尽量抽成可读摘要，失败则原样附上。
    try:
        payload = json.loads(text)
    except Exception:
        return f"【{game} 的剧情/设定 · Wiki】\n{text[:600]}"
    candidates = payload.get("top_candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not candidates:
        return ""
    lines = [f"【{game} 的剧情/设定 · Wiki】"]
    for idx, cand in enumerate(candidates[:3], 1):
        if not isinstance(cand, dict):
            continue
        title = str(cand.get("title", "") or "").strip()
        summary = str(cand.get("summary", "") or cand.get("extract", "") or "").strip()
        url = str(cand.get("url", "") or "").strip()
        if title:
            lines.append(f"{idx}. {title}")
        if summary:
            lines.append(f"   {summary[:240]}")
        if url:
            lines.append(f"   链接：{url}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _steam_update_block(
    game: str,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
    timeout: float,
) -> str:
    """Steam 官方公告块：解析 appid → 取公告 → 格式化；解析不到返回 ""（由社区搜索兜底）。"""
    appid, matched = await _resolve_steam_appid(
        game, http_client=http_client, logger=logger, timeout=timeout
    )
    if appid is None:
        return ""
    news = await _fetch_steam_news(
        appid, http_client=http_client, logger=logger, timeout=timeout
    )
    return _format_steam_updates(game, matched, news)


def _norm_name(value: str) -> str:
    return re.sub(r"[\s:：·\-_、,，.]+", "", str(value or "").strip().lower())


def _match_official_source(game: str) -> Optional[dict[str, Any]]:
    """把游戏名匹配到精选官方源；大小写/空格/标点不敏感，支持别名与子串包含。"""
    g = _norm_name(game)
    if not g:
        return None
    for entry in _OFFICIAL_GAME_SOURCES:
        for name in entry.get("names", []):
            n = _norm_name(name)
            if not n:
                continue
            # 完全相等，或较短一方长度≥2 时允许子串包含（避免单字误匹配）。
            if n == g or (len(n) >= 2 and n in g) or (len(g) >= 2 and g in n):
                return entry
    return None


async def _official_update_block(
    game: str,
    *,
    logger: Any,
    timeout: float,
    proxy: Optional[str],
) -> str:
    """头部游戏的官方公告块：抓取精选官方页正文；即使正文抽取不全，也保留官方链接作为权威来源。"""
    entry = _match_official_source(game)
    if not entry:
        return ""
    urls = list(entry.get("urls", []))[:2]
    if not urls:
        return ""

    async def _one(url: str) -> tuple[str, str, str]:
        try:
            res = await fetch_web_page(url, timeout=timeout, max_chars=600, proxy=proxy)
        except Exception as exc:
            logger.debug(f"[game_info] official fetch failed {url}: {exc}")
            return url, "", ""
        return url, str(res.get("title", "") or "").strip(), str(res.get("text", "") or "").strip()

    fetched = await asyncio.gather(*[_one(u) for u in urls])
    lines = [f"【{game} 的更新/公告 · {entry.get('label', '官方')}（官方源）】"]
    for url, title, text in fetched:
        if title:
            lines.append(title)
        if text:
            lines.append(text[:300])
        lines.append(f"官方链接：{url}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def game_info(
    game: str,
    aspect: str,
    query: str = "",
    *,
    http_client: httpx.AsyncClient,
    logger: Any = None,
    plugin_config: Any = None,
) -> str:
    """游戏信息聚合查询主入口。

    aspect: update=更新公告 / guide=攻略 / story=剧情设定 / tips=技巧。
    数据源：Steam 官方公告（update）+ 社区定向搜索 + Wiki（story）。
    返回结构化可读文本，交由调用方 agent 综合成最终回复。
    """
    logger = _resolve_logger(logger)
    game = str(game or "").strip()
    if not game:
        return "请提供游戏名称。"
    aspect = _normalize_aspect(aspect)
    aspect_label = _ASPECT_LABEL.get(aspect, "攻略")
    query = str(query or "").strip()
    timeout = _timeout(plugin_config)
    community_sites = _community_sites(plugin_config)
    proxy = str(getattr(plugin_config, "personification_web_proxy", "") or "").strip() or None

    async def _community_block() -> str:
        kw = _ASPECT_SEARCH_KEYWORDS[aspect]
        results = await _community_search(
            f"{game} {kw} {query}".strip(),
            http_client=http_client,
            logger=logger,
            community_sites=community_sites,
        )
        return _format_community(aspect_label, game, results)

    # 主数据源与社区搜索互不依赖，并行拉取（本工具 latency_class=slow）。
    if aspect == "story":
        # 剧情优先 Wiki，并补社区搜索覆盖 wiki 收录不全的小众游戏。
        blocks = await asyncio.gather(
            _format_story(game, query, http_client=http_client, logger=logger),
            _community_block(),
        )
    elif aspect == "update":
        # Steam 官方公告 + 头部游戏精选官方源 + 社区（三路并行，覆盖 Steam/非 Steam 平台）。
        blocks = await asyncio.gather(
            _steam_update_block(game, http_client=http_client, logger=logger, timeout=timeout),
            _official_update_block(game, logger=logger, timeout=timeout, proxy=proxy),
            _community_block(),
        )
    else:
        blocks = [await _community_block()]

    sections = [b for b in blocks if b]
    if not sections:
        return (
            f"没有查到「{game}」关于{aspect_label}的可靠信息，可能是名称不准确、"
            "暂无相关公告，或当前网络不可达。可以换个更准确的游戏名或具体一点的关键词再试。"
        )
    return "\n\n".join(sections)
