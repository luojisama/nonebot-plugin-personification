"""免配置的联网搜索后端。

提供 Wikipedia / SearXNG / DuckDuckGo 三条免 key 通道，统一返回
`List[SearchResult]`。Wikipedia 一定走得通，SearXNG 走公共实例池，
DuckDuckGo 是兜底。所有 helper 都接 personification_web_proxy 配置。

设计要点：
- 单一返回类型 SearchResult（title/url/domain/snippet/source/score）
- 所有 helper 失败时返回 [] 而不是抛异常，调用方决定降级
- 超时分别控制，单一引擎卡死不影响其他并行调用
- SearXNG 实例池用并行 HEAD 探测选第一个能通的，复用 git mirror 那套思路
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional
from urllib.parse import quote, urlparse

import httpx


DEFAULT_SEARXNG_INSTANCES: tuple[str, ...] = (
    "https://searx.be",
    "https://searx.tiekoetter.com",
    "https://search.ononoki.org",
    "https://priv.au",
)

_WIKIPEDIA_UA = (
    "PersonificationBot/1.0 (https://github.com/luojisama/personification) "
    "httpx/python"
)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_MAX_SNIPPET_CHARS = 400


@dataclass
class SearchResult:
    title: str
    url: str
    domain: str
    snippet: str
    source: str
    score: float = 0.0
    extras: dict = field(default_factory=dict)


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    return host.lower().lstrip("www.")


def _clip_snippet(text: str, *, max_chars: int = _MAX_SNIPPET_CHARS) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) > max_chars:
        s = s[: max_chars].rstrip() + "…"
    return s


def _client_kwargs(*, timeout: float, proxy: Optional[str], **extra: Any) -> dict:
    kw: dict[str, Any] = {"timeout": timeout, **extra}
    if proxy:
        kw["proxy"] = proxy
    return kw


# ──────────────────── Wikipedia ────────────────────

async def wikipedia_search(
    query: str,
    *,
    lang: str = "zh",
    max_results: int = 5,
    proxy: Optional[str] = None,
    logger: Any = None,
) -> List[SearchResult]:
    """走 MediaWiki API 检索 + 拉摘要。lang='zh' 命中 0 条时自动追一次 'en'。"""
    query = (query or "").strip()
    if not query:
        return []
    results = await _wikipedia_search_one(
        query, lang=lang, max_results=max_results, proxy=proxy, logger=logger
    )
    if not results and lang == "zh":
        results = await _wikipedia_search_one(
            query, lang="en", max_results=max_results, proxy=proxy, logger=logger
        )
    return results


async def _wikipedia_search_one(
    query: str,
    *,
    lang: str,
    max_results: int,
    proxy: Optional[str],
    logger: Any,
) -> List[SearchResult]:
    base = f"https://{lang}.wikipedia.org/w/api.php"
    headers = {"User-Agent": _WIKIPEDIA_UA, "Accept": "application/json"}
    search_params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": query,
        "srlimit": max(1, min(int(max_results or 5), 10)),
        "srprop": "snippet|titlesnippet",
        "utf8": "1",
        "formatversion": "2",
    }
    try:
        async with httpx.AsyncClient(
            **_client_kwargs(timeout=8.0, proxy=proxy, headers=headers, follow_redirects=True)
        ) as client:
            resp = await client.get(base, params=search_params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            hits = data.get("query", {}).get("search", []) or []
            if not hits:
                return []
            titles = [str(h.get("title", "")).strip() for h in hits if h.get("title")]
            extract_map = await _wikipedia_fetch_extracts(
                client, base, titles=titles, lang=lang
            )
    except Exception as exc:
        if logger:
            logger.warning(f"拟人插件：Wikipedia({lang}) 搜索失败: {exc}")
        return []

    out: List[SearchResult] = []
    for idx, hit in enumerate(hits):
        title = str(hit.get("title", "")).strip()
        if not title:
            continue
        snippet_html = str(hit.get("snippet", ""))
        snippet_clean = re.sub(r"<[^>]+>", "", snippet_html)
        snippet = extract_map.get(title) or snippet_clean
        url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
        out.append(
            SearchResult(
                title=title,
                url=url,
                domain=f"{lang}.wikipedia.org",
                snippet=_clip_snippet(snippet),
                source=f"wikipedia.{lang}",
                score=max(0.0, 1.0 - idx * 0.1),
            )
        )
    return out


async def _wikipedia_fetch_extracts(
    client: httpx.AsyncClient,
    base: str,
    *,
    titles: List[str],
    lang: str,
) -> dict[str, str]:
    if not titles:
        return {}
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "exintro": "1",
        "explaintext": "1",
        "exchars": "800",
        "titles": "|".join(titles[:10]),
        "redirects": "1",
        "utf8": "1",
        "formatversion": "2",
    }
    try:
        resp = await client.get(base, params=params)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        pages = data.get("query", {}).get("pages", []) or []
        out: dict[str, str] = {}
        for page in pages:
            t = str(page.get("title", "")).strip()
            extract = str(page.get("extract", "") or "").strip()
            if t and extract:
                out[t] = extract
        return out
    except Exception:
        return {}


# ──────────────────── SearXNG ────────────────────

async def _probe_searxng_instance(
    instance: str, *, proxy: Optional[str], timeout: float = 4.0
) -> bool:
    target = instance.rstrip("/") + "/"
    try:
        async with httpx.AsyncClient(
            **_client_kwargs(timeout=timeout, proxy=proxy, follow_redirects=True)
        ) as client:
            resp = await client.head(target, headers={"User-Agent": _BROWSER_UA})
            return 0 < resp.status_code < 500
    except Exception:
        return False


async def _pick_searxng_instance(
    instances: List[str], *, proxy: Optional[str]
) -> Optional[str]:
    if not instances:
        return None
    probes = await asyncio.gather(
        *(_probe_searxng_instance(i, proxy=proxy) for i in instances),
        return_exceptions=True,
    )
    for inst, ok in zip(instances, probes):
        if ok is True:
            return inst.rstrip("/")
    return None


async def searxng_search(
    query: str,
    *,
    instances: Optional[List[str]] = None,
    max_results: int = 6,
    proxy: Optional[str] = None,
    logger: Any = None,
) -> List[SearchResult]:
    """公共 SearXNG 实例并行探测后按 ?format=json 拉结果。失败返回 []。"""
    query = (query or "").strip()
    if not query:
        return []
    pool = [str(i).strip().rstrip("/") for i in (instances or DEFAULT_SEARXNG_INSTANCES) if i]
    inst = await _pick_searxng_instance(pool, proxy=proxy)
    if not inst:
        if logger:
            logger.debug("拟人插件：SearXNG 所有实例均不可达，跳过")
        return []
    params = {
        "q": query,
        "format": "json",
        "safesearch": "0",
        "categories": "general",
    }
    try:
        async with httpx.AsyncClient(
            **_client_kwargs(timeout=10.0, proxy=proxy, follow_redirects=True)
        ) as client:
            resp = await client.get(
                f"{inst}/search",
                params=params,
                headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception as exc:
        if logger:
            logger.warning(f"拟人插件：SearXNG({inst}) 检索失败: {exc}")
        return []

    hits = data.get("results", []) if isinstance(data, dict) else []
    out: List[SearchResult] = []
    for idx, hit in enumerate(hits[: max(1, max_results * 3)]):
        if not isinstance(hit, dict):
            continue
        url = str(hit.get("url") or "").strip()
        title = str(hit.get("title") or "").strip()
        snippet = hit.get("content") or hit.get("snippet") or ""
        if not url or not title:
            continue
        score_raw = hit.get("score")
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = max(0.0, 1.0 - idx * 0.05)
        out.append(
            SearchResult(
                title=title,
                url=url,
                domain=_domain_of(url),
                snippet=_clip_snippet(snippet),
                source="searxng",
                score=score,
            )
        )
        if len(out) >= max_results:
            break
    return out


# ──────────────────── DuckDuckGo ────────────────────

async def duckduckgo_search(
    query: str,
    *,
    max_results: int = 6,
    proxy: Optional[str] = None,
    logger: Any = None,
) -> List[SearchResult]:
    """DDG Instant API + HTML scrape 兜底，输出结构化结果。"""
    query = (query or "").strip()
    if not query:
        return []
    instant: List[SearchResult] = []
    try:
        async with httpx.AsyncClient(
            **_client_kwargs(timeout=10.0, proxy=proxy, follow_redirects=True)
        ) as client:
            # Instant Answer API
            try:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": "1",
                        "skip_disambig": "1",
                    },
                    headers={"User-Agent": "Mozilla/5.0 (compatible; PersonificationBot/1.0)"},
                )
                data: dict = {}
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                if data.get("AbstractText"):
                    instant.append(
                        SearchResult(
                            title=str(data.get("Heading") or query),
                            url=str(data.get("AbstractURL") or "https://duckduckgo.com/"),
                            domain=_domain_of(str(data.get("AbstractURL") or "")) or "duckduckgo.com",
                            snippet=_clip_snippet(data["AbstractText"]),
                            source="duckduckgo.instant",
                            score=0.9,
                        )
                    )
                for idx, topic in enumerate(data.get("RelatedTopics") or []):
                    if not isinstance(topic, dict):
                        continue
                    text = str(topic.get("Text") or "").strip()
                    url = str(topic.get("FirstURL") or "").strip()
                    if not text or not url:
                        continue
                    instant.append(
                        SearchResult(
                            title=text.split(" - ")[0][:80],
                            url=url,
                            domain=_domain_of(url),
                            snippet=_clip_snippet(text),
                            source="duckduckgo.instant",
                            score=max(0.0, 0.7 - idx * 0.05),
                        )
                    )
            except Exception as exc:
                if logger:
                    logger.debug(f"拟人插件：DDG Instant 失败: {exc}")

            if len(instant) >= max_results:
                return instant[:max_results]

            # HTML scrape fallback
            try:
                html_resp = await client.get(
                    "https://duckduckgo.com/html/",
                    params={"q": query},
                    headers={
                        "User-Agent": _BROWSER_UA,
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                )
                if html_resp.status_code == 200:
                    html_results = _parse_ddg_html(html_resp.text, max_results=max_results)
                    instant.extend(html_results)
            except Exception as exc:
                if logger:
                    logger.debug(f"拟人插件：DDG HTML scrape 失败: {exc}")
    except Exception as exc:
        if logger:
            logger.warning(f"拟人插件：DuckDuckGo 检索失败: {exc}")

    # 按 score 排序去重 url
    seen: set[str] = set()
    final: List[SearchResult] = []
    for r in sorted(instant, key=lambda x: -x.score):
        if r.url in seen:
            continue
        seen.add(r.url)
        final.append(r)
        if len(final) >= max_results:
            break
    return final


def _parse_ddg_html(html: str, *, max_results: int) -> List[SearchResult]:
    raw_blocks = re.findall(
        r'<div[^>]*class="[^"]*result__body[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    out: List[SearchResult] = []
    for idx, block in enumerate(raw_blocks[: max_results * 2]):
        title_match = re.search(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet_match = re.search(
            r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>'
            r'|<div[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            continue
        raw_url = title_match.group(1)
        url = _resolve_ddg_redirect(raw_url)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_match.group(2) or "")).strip()
        snippet = ""
        if snippet_match:
            raw_snip = snippet_match.group(1) or snippet_match.group(2) or ""
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw_snip)).strip()
        if not title or not url:
            continue
        out.append(
            SearchResult(
                title=title[:120],
                url=url,
                domain=_domain_of(url),
                snippet=_clip_snippet(snippet) or _clip_snippet(title),
                source="duckduckgo.html",
                score=max(0.0, 0.5 - idx * 0.03),
            )
        )
        if len(out) >= max_results:
            break
    return out


def _resolve_ddg_redirect(raw_url: str) -> str:
    """DDG HTML 给的 URL 形如 //duckduckgo.com/l/?uddg=ENCODED。"""
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    m = re.search(r"[?&]uddg=([^&]+)", raw_url)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return raw_url


# ──────────────────── 统一调度 ────────────────────

async def free_search(
    query: str,
    *,
    engines: List[str],
    max_results: int = 6,
    proxy: Optional[str] = None,
    searxng_instances: Optional[List[str]] = None,
    logger: Any = None,
) -> List[SearchResult]:
    """按 engines 顺序并行调用各免 key 引擎，合并去重。"""
    query = (query or "").strip()
    if not query:
        return []
    enabled = [str(e).strip().lower() for e in (engines or []) if e]
    tasks: List[asyncio.Task] = []
    if "wikipedia" in enabled:
        tasks.append(
            asyncio.create_task(
                wikipedia_search(query, max_results=max_results, proxy=proxy, logger=logger)
            )
        )
    if "searxng" in enabled:
        tasks.append(
            asyncio.create_task(
                searxng_search(
                    query,
                    instances=searxng_instances,
                    max_results=max_results,
                    proxy=proxy,
                    logger=logger,
                )
            )
        )
    if "duckduckgo" in enabled:
        tasks.append(
            asyncio.create_task(
                duckduckgo_search(query, max_results=max_results, proxy=proxy, logger=logger)
            )
        )
    if not tasks:
        return []
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)
    flat: List[SearchResult] = []
    for res in results_lists:
        if isinstance(res, list):
            flat.extend(res)
    return flat


__all__ = [
    "SearchResult",
    "DEFAULT_SEARXNG_INSTANCES",
    "wikipedia_search",
    "searxng_search",
    "duckduckgo_search",
    "free_search",
]
