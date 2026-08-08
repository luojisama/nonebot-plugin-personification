from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

import httpx

_VALID_RESOURCE_TYPES = {"教程/文档", "图片/壁纸", "视频", "工具/软件", "论坛/社区", "通用资源"}
_VALID_SEARCH_KINDS = {"web", "github", "official", "image"}
_MAX_SEARCH_TASKS = 3

_SEARCH_ENGINES = [
    {
        "name": "DuckDuckGo",
        "url": "https://html.duckduckgo.com/html/?q={query}",
        "result_pattern": r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        "snippet_pattern": r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    },
    {
        "name": "Bing",
        "url": "https://www.bing.com/search?q={query}&setlang=zh-CN",
        "result_pattern": r'<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>',
        "snippet_pattern": r'<p[^>]*class="[^"]*b_caption[^"]*"[^>]*><span>(.*?)</span>',
    },
]
_IMAGE_SEARCH_ENGINE = {
    "name": "Bing Images",
    "url": "https://www.bing.com/images/search?q={query}&form=HDRSC2&setlang=zh-CN",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "personification-resource-agent/1.0",
}
_MAX_RESULTS_PER_ENGINE = 5
_FETCH_TIMEOUT = 8.0
_LOW_QUALITY_DOMAINS = {"zhidao.baidu.com", "wenda.so.com", "wenwen.sogou.com"}
_AD_PATTERNS = re.compile(r"(ad|sponsor|广告|推广|联系我们|copyright)", re.IGNORECASE)
_GITHUB_OWNER_REPO_RE = re.compile(r"^/([^/]+)/([^/]+)")
_GITHUB_RESERVED_SEGMENTS = {
    "about",
    "account",
    "apps",
    "collections",
    "contact",
    "customer-stories",
    "enterprise",
    "events",
    "explore",
    "features",
    "issues",
    "login",
    "marketplace",
    "models",
    "notifications",
    "orgs",
    "organizations",
    "pricing",
    "pulls",
    "search",
    "settings",
    "site",
    "sponsors",
    "topics",
    "users",
}


@dataclass(slots=True)
class _ResourceSearchTask:
    kind: str
    query: str
    limit: int = 5
    sort: str = "best_match"
    purpose: str = ""


class _SilentLogger:
    def debug(self, _msg: str) -> None:
        return None


def _get_logger(logger: Any = None) -> Any:
    return logger or _SilentLogger()


def _strip_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = _strip_code_fence(text)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _normalize_resource_type(value: str, fallback: str = "通用资源") -> str:
    resource_type = str(value or "").strip()
    if resource_type in _VALID_RESOURCE_TYPES:
        return resource_type
    return fallback


def _normalize_search_kind(value: str, fallback: str = "web") -> str:
    kind = str(value or "").strip().lower()
    if kind in _VALID_SEARCH_KINDS:
        return kind
    return fallback


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).replace("&amp;", "&").strip()


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_low_quality(url: str, title: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower()
        if any(low in domain for low in _LOW_QUALITY_DOMAINS):
            return True
    except Exception:
        return True
    return bool(_AD_PATTERNS.search(title or ""))


def _get_domain_label(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return "未知来源"
    friendly = {
        "github.com": "GitHub",
        "bilibili.com": "哔哩哔哩",
        "youtube.com": "YouTube",
        "pixiv.net": "Pixiv",
        "zhihu.com": "知乎",
        "csdn.net": "CSDN",
        "juejin.cn": "掘金",
        "runoob.com": "菜鸟教程",
        "developer.mozilla.org": "MDN",
        "docs.python.org": "Python 官方文档",
        "stackoverflow.com": "Stack Overflow",
        "reddit.com": "Reddit",
    }
    for domain, label in friendly.items():
        if domain in netloc:
            return label
    return netloc.split(".")[0].capitalize() if netloc else "未知来源"


async def _fetch_engine(
    engine: dict[str, str],
    query: str,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> list[dict[str, Any]]:
    url = engine["url"].format(query=quote_plus(query))
    results: list[dict[str, Any]] = []
    try:
        response = await http_client.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
        html = response.text
        titles = re.findall(engine["result_pattern"], html)
        snippets = re.findall(engine.get("snippet_pattern", ""), html) if engine.get("snippet_pattern") else []
        for index, match in enumerate(titles[:_MAX_RESULTS_PER_ENGINE]):
            if isinstance(match, tuple):
                link, title = match[0], match[1]
            else:
                link, title = match, ""
            title_text = _strip_tags(title)
            snippet_text = _strip_tags(snippets[index]) if index < len(snippets) else ""
            if not _is_valid_url(link):
                continue
            if _is_low_quality(link, title_text):
                continue
            results.append(
                {
                    "url": link,
                    "title": title_text or link,
                    "snippet": snippet_text[:200],
                    "source": _get_domain_label(link),
                    "engine": engine["name"],
                }
            )
    except Exception as exc:
        logger.debug(f"[resource_collector] search failed engine={engine['name']}: {exc}")
    return results


def _deduplicate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_urls: set[str] = set()
    domain_count: dict[str, int] = {}
    deduped: list[dict[str, Any]] = []
    for item in results:
        url = str(item.get("url", "") or "")
        if not url or url in seen_urls:
            continue
        try:
            domain = urlparse(url).netloc
        except Exception:
            domain = url
        domain_limit = 4 if "github.com" in domain.lower() else 2
        if domain_count.get(domain, 0) >= domain_limit:
            continue
        seen_urls.add(url)
        domain_count[domain] = domain_count.get(domain, 0) + 1
        deduped.append(item)
    return deduped


def _normalize_limit(limit: int, *, default: int = 5, max_limit: int = 10) -> int:
    try:
        return max(1, min(max_limit, int(limit)))
    except Exception:
        return default


def _extract_search_core(query: str) -> str:
    core = str(query or "").strip()
    core = re.sub(r"^[\s:：>\-#|]+", "", core)
    core = re.sub(r"^[^:：\n]{1,24}[：:]\s*", "", core)
    core = re.sub(r"^[\s:：>\-#|]+", "", core)
    core = re.sub(
        r"^(?:帮我|帮忙|麻烦你|麻烦|请你|请|给我)?(?:找找|搜搜|查查|找|搜|搜下|搜一下|搜索|搜集|收集|整理)(?:一下|一些|点)?",
        "",
        core,
        flags=re.IGNORECASE,
    )
    core = re.sub(r"^(?:有没有|哪里有|哪里能下|给个链接(?:吧)?|来点)", "", core)
    core = re.sub(r"\s+", " ", core).strip(" :：，,。.!！?？")
    return core


def _build_search_queries(query: str, resource_type: str, search_kind: str = "web") -> list[str]:
    _ = resource_type, search_kind
    raw = str(query or "").strip()
    core = _extract_search_core(raw)
    if core:
        return [core]
    return [raw] if raw else []


def _score_web_result(item: dict[str, Any], query: str, search_kind: str, resource_type: str) -> tuple[int, int, int, int]:
    url = str(item.get("url", "") or "")
    title = str(item.get("title", "") or "")
    domain = urlparse(url).netloc.lower()
    combined = f"{title} {url}".lower()
    github_hint = search_kind == "github"
    official_hint = search_kind == "official"
    image_hint = search_kind == "image"

    domain_score = 0
    if github_hint and "github.com" in domain:
        domain_score = 5
    elif official_hint and "github.com" not in domain and "wikipedia.org" not in domain:
        domain_score = 3
    elif image_hint and any(site in domain for site in ("pixiv.net", "wallhaven.cc", "zerochan.net", "konachan.com")):
        domain_score = 4

    token_score = 0
    tokens = [token.lower() for token in re.split(r"\s+", _extract_search_core(query)) if token.strip()]
    for token in tokens[:5]:
        if token and token in combined:
            token_score += 1

    kind_score = 0
    if image_hint and any(marker in combined for marker in ("wallpaper", "壁纸", "图片", "fanart", "插画")):
        kind_score = 2
    elif official_hint and any(marker in combined for marker in ("官网", "official", "home")):
        kind_score = 2

    engine_score = 1 if str(item.get("engine", "") or "").lower() == "bing" else 0
    _ = resource_type
    return (domain_score, token_score, kind_score, engine_score)


def _prioritize_web_results(
    results: list[dict[str, Any]],
    query: str,
    search_kind: str,
    resource_type: str,
) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: _score_web_result(item, query, search_kind, resource_type),
        reverse=True,
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_jsonish_attr(value: str) -> dict[str, Any] | None:
    raw = html.unescape(str(value or "").strip())
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _extract_bing_image_items(html_text: str) -> list[dict[str, Any]]:
    text = str(html_text or "")
    items: list[dict[str, Any]] = []
    for match in re.finditer(r"\bm=(['\"])(.*?)\1", text, flags=re.DOTALL):
        data = _decode_jsonish_attr(match.group(2))
        if isinstance(data, dict):
            items.append(data)
    if items:
        return items

    # Some Bing image responses inline the metadata as raw escaped JSON instead
    # of an m="..." attribute. Keep this as a conservative fallback.
    for match in re.finditer(r'"murl"\s*:\s*"((?:\\.|[^"\\])*)"', text):
        try:
            image_url = json.loads(f'"{match.group(1)}"')
        except Exception:
            image_url = html.unescape(match.group(1)).replace("\\/", "/")
        if image_url:
            items.append({"murl": image_url})
    return items


def _image_direct_url_from_item(item: dict[str, Any]) -> str:
    for key in ("murl", "mediaurl", "imgurl", "url"):
        value = str(item.get(key, "") or "").strip()
        if _is_valid_url(value):
            return value
    return ""


def _image_page_url_from_item(item: dict[str, Any]) -> str:
    for key in ("purl", "p", "hostPageUrl", "page_url"):
        value = str(item.get(key, "") or "").strip()
        if _is_valid_url(value):
            return value
    return ""


def _image_thumbnail_url_from_item(item: dict[str, Any]) -> str:
    for key in ("turl", "thumbnailUrl", "thumb", "thumbnail_url"):
        value = str(item.get(key, "") or "").strip()
        if _is_valid_url(value):
            return value
    return ""


def _score_image_result(item: dict[str, Any], query: str) -> tuple[int, int, int, int]:
    image_url = str(item.get("image_url", "") or item.get("url", "") or "")
    page_url = str(item.get("page_url", "") or "")
    title = str(item.get("title", "") or "")
    combined = f"{title} {image_url} {page_url}".lower()
    try:
        image_domain = urlparse(image_url).netloc.lower()
        page_domain = urlparse(page_url).netloc.lower()
    except Exception:
        image_domain = ""
        page_domain = ""

    dimension_score = 0
    width = _safe_int(item.get("width"))
    height = _safe_int(item.get("height"))
    if width and height:
        long_edge = max(width, height)
        short_edge = min(width, height)
        if long_edge >= 1600 and short_edge >= 800:
            dimension_score = 4
        elif long_edge >= 1000 and short_edge >= 600:
            dimension_score = 3
        elif long_edge >= 700 and short_edge >= 400:
            dimension_score = 2
        elif long_edge >= 400:
            dimension_score = 1

    domain_score = 0
    preferred_domains = (
        "pixiv.net",
        "wallhaven.cc",
        "zerochan.net",
        "konachan.com",
        "yande.re",
        "artstation.com",
        "deviantart.com",
        "static.zerochan.net",
        "images.",
        "img.",
        "cdn.",
    )
    if any(domain in image_domain or domain in page_domain for domain in preferred_domains):
        domain_score = 3

    direct_score = 1
    path = urlparse(image_url).path.lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".webp")):
        direct_score = 3
    elif path.endswith((".gif", ".bmp")):
        direct_score = 1

    token_score = 0
    for token in [t.lower() for t in re.split(r"\s+", _extract_search_core(query)) if len(t.strip()) >= 2][:6]:
        if token in combined:
            token_score += 1
    return (dimension_score, domain_score, direct_score, token_score)


def _normalize_image_search_results(raw_items: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        image_url = _image_direct_url_from_item(raw)
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        title = (
            str(raw.get("t", "") or raw.get("title", "") or raw.get("desc", "") or "").strip()
            or _extract_search_core(query)
            or "图片结果"
        )
        page_url = _image_page_url_from_item(raw)
        result = {
            "type": "image",
            "title": title[:120],
            "url": image_url,
            "image_url": image_url,
            "thumbnail_url": _image_thumbnail_url_from_item(raw),
            "page_url": page_url,
            "source": _get_domain_label(page_url or image_url),
            "width": _safe_int(raw.get("ow") or raw.get("width")),
            "height": _safe_int(raw.get("oh") or raw.get("height")),
            "engine": _IMAGE_SEARCH_ENGINE["name"],
        }
        results.append(result)
    return sorted(results, key=lambda item: _score_image_result(item, query), reverse=True)[:limit]


async def _search_image_payload(
    *,
    query: str,
    limit: int,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> dict[str, Any]:
    search_query = str(query or "").strip()
    if not search_query:
        return _build_payload(
            ok=False,
            query="",
            source_type="image",
            results=[],
            error="missing_query",
        )
    limit = _normalize_limit(limit)
    url = _IMAGE_SEARCH_ENGINE["url"].format(query=quote_plus(_extract_search_core(search_query) or search_query))
    diagnostics: dict[str, Any] = {
        "engine": _IMAGE_SEARCH_ENGINE["name"],
        "http_status": 0,
        "content_type": "",
        "response_bytes": 0,
        "raw_item_count": 0,
        "direct_image_count": 0,
        "error_type": "",
    }
    try:
        response = await http_client.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
        response_text = str(getattr(response, "text", "") or "")
        headers = getattr(response, "headers", {}) or {}
        diagnostics["http_status"] = int(getattr(response, "status_code", 200) or 0)
        diagnostics["content_type"] = str(headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()[:80]
        diagnostics["response_bytes"] = len(response_text.encode("utf-8", errors="replace"))
        raw_items = _extract_bing_image_items(response_text)
        diagnostics["raw_item_count"] = len(raw_items)
    except Exception as exc:
        logger.debug(f"[resource_collector] image search failed engine={_IMAGE_SEARCH_ENGINE['name']}: {exc}")
        diagnostics["error_type"] = type(exc).__name__[:80]
        raw_items = []
    results = _normalize_image_search_results(raw_items, search_query, limit)
    diagnostics["direct_image_count"] = len(results)
    payload = _build_payload(
        ok=bool(results),
        query=search_query,
        source_type="image",
        results=results,
        error="" if results else "no_results",
    )
    payload["image_search_diagnostics"] = diagnostics
    return payload


def _build_payload(
    *,
    ok: bool,
    query: str,
    source_type: str,
    results: list[dict[str, Any]],
    degraded: bool = False,
    notes: list[str] | None = None,
    error: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "query": str(query or ""),
        "source_type": source_type,
        "results": results,
        "degraded": bool(degraded),
    }
    if notes:
        payload["notes"] = [str(item) for item in notes if str(item).strip()]
    if error:
        payload["error"] = str(error)
    return payload


def dumps_json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _call_llm_json(
    *,
    tool_caller: Any,
    logger: Any,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any] | None:
    if tool_caller is None:
        return None
    try:
        response = await tool_caller.chat_with_tools(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            [],
            False,
        )
    except Exception as exc:
        logger.debug(f"[resource_collector] llm call failed: {exc}")
        return None
    if response.tool_calls:
        return None
    return _parse_json_object(response.content or "")


async def _call_llm_text(
    *,
    tool_caller: Any,
    logger: Any,
    system_prompt: str,
    user_prompt: str,
) -> str:
    if tool_caller is None:
        return ""
    try:
        response = await tool_caller.chat_with_tools(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            [],
            False,
        )
    except Exception as exc:
        logger.debug(f"[resource_collector] llm summary failed: {exc}")
        return ""
    if response.tool_calls:
        return ""
    return str(response.content or "").strip()


def _normalize_plan_tasks(items: Any) -> list[_ResourceSearchTask]:
    tasks: list[_ResourceSearchTask] = []
    if not isinstance(items, list):
        return tasks
    for item in items[:_MAX_SEARCH_TASKS]:
        if not isinstance(item, dict):
            continue
        query = _extract_search_core(str(item.get("query", "") or ""))
        if not query:
            continue
        tasks.append(
            _ResourceSearchTask(
                kind=_normalize_search_kind(str(item.get("kind", "") or "web")),
                query=query,
                limit=_normalize_limit(item.get("limit", 5)),
                sort="stars" if str(item.get("sort", "") or "").strip().lower() == "stars" else "best_match",
                purpose=str(item.get("purpose", "") or "").strip(),
            )
        )
    return tasks


async def _plan_resource_search(
    *,
    raw_query: str,
    context_hint: str,
    resource_type_hint: str,
    tool_caller: Any,
    logger: Any,
) -> dict[str, Any]:
    normalized_type_hint = _normalize_resource_type(resource_type_hint, fallback="")
    plan = await _call_llm_json(
        tool_caller=tool_caller,
        logger=logger,
        system_prompt=(
            "你是资源搜索规划器。"
            "你的任务是理解用户真正想找的资源，生成搜索计划。"
            "不要依赖固定关键词做机械分类，要根据语义理解意图。"
            "你必须只输出 JSON 对象，不要输出解释。"
            "JSON schema: "
            "{\"decision\":\"search|clarify\","
            "\"normalized_query\":\"string\","
            "\"resource_type\":\"教程/文档|图片/壁纸|视频|工具/软件|论坛/社区|通用资源\","
            "\"clarify_question\":\"string\","
            "\"search_plan\":[{\"kind\":\"web|github|official|image\",\"query\":\"string\",\"limit\":5,\"sort\":\"best_match|stars\",\"purpose\":\"string\"}]}"
            "当用户需求足够明确时返回 decision=search，并给出 1 到 3 个搜索任务。"
            "当需求明显缺关键对象时返回 decision=clarify。"
            "搜索任务应该是可执行的精确查询，不要给泛泛空话。"
        ),
        user_prompt=(
            f"用户原始需求：{raw_query.strip()}\n"
            f"上下文提示：{context_hint.strip() or '[NONE]'}\n"
            f"外部类型提示：{normalized_type_hint or '[NONE]'}"
        ),
    )
    if not isinstance(plan, dict):
        default_query = _extract_search_core(raw_query)
        if not default_query:
            return {
                "decision": "clarify",
                "normalized_query": "",
                "resource_type": normalized_type_hint or "通用资源",
                "clarify_question": "你想让我找什么资源？说一下主题、作品名或资源类型。",
                "search_plan": [],
            }
        return {
            "decision": "search",
            "normalized_query": default_query,
            "resource_type": normalized_type_hint or "通用资源",
            "clarify_question": "",
            "search_plan": [
                {
                    "kind": "web",
                    "query": default_query,
                    "limit": 5,
                    "sort": "best_match",
                    "purpose": "直接搜索用户请求的资源",
                }
            ],
        }

    decision = str(plan.get("decision", "") or "").strip().lower()
    if decision not in {"search", "clarify"}:
        decision = "search"
    normalized_query = _extract_search_core(str(plan.get("normalized_query", "") or raw_query))
    normalized_resource_type = _normalize_resource_type(
        str(plan.get("resource_type", "") or normalized_type_hint or "通用资源")
    )
    tasks = _normalize_plan_tasks(plan.get("search_plan"))
    if decision == "clarify" and not str(plan.get("clarify_question", "") or "").strip():
        plan["clarify_question"] = "你想找哪一类资源？比如攻略、下载地址、Wiki、视频教程，或者具体作品名。"
    if decision == "search" and not tasks and normalized_query:
        tasks = [_ResourceSearchTask(kind="web", query=normalized_query, limit=5, purpose="默认搜索")]

    return {
        "decision": decision,
        "normalized_query": normalized_query,
        "resource_type": normalized_resource_type,
        "clarify_question": str(plan.get("clarify_question", "") or "").strip(),
        "search_plan": [
            {
                "kind": task.kind,
                "query": task.query,
                "limit": task.limit,
                "sort": task.sort,
                "purpose": task.purpose,
            }
            for task in tasks
        ],
    }


async def _summarize_resource_results(
    *,
    raw_query: str,
    normalized_query: str,
    resource_type: str,
    search_plan: list[dict[str, Any]],
    payloads: list[dict[str, Any]],
    tool_caller: Any,
    logger: Any,
    max_count: int,
) -> str:
    summary = await _call_llm_text(
        tool_caller=tool_caller,
        logger=logger,
        system_prompt=(
            "你是资源整理助手。"
            "基于给定的搜索计划和搜索结果，为用户输出最终可读答案。"
            "不要编造链接，不要引用不存在的结果。"
            "如果结果充足，先用一句话概括，再列出最多 5 条最有用资源，每条包含标题、来源、简短说明和链接。"
            "如果结果不足或为空，直接说明没找到可靠结果，并给出 2 到 3 个更合适的搜索方向。"
            "用简洁中文输出。"
        ),
        user_prompt=(
            f"用户原始需求：{raw_query}\n"
            f"规范化需求：{normalized_query}\n"
            f"资源类型：{resource_type}\n"
            f"最多返回数量：{max_count}\n"
            f"搜索计划：{json.dumps(search_plan, ensure_ascii=False)}\n"
            f"搜索结果：{json.dumps(payloads, ensure_ascii=False)}"
        ),
    )
    return summary.strip()


async def _search_web_payload(
    *,
    query: str,
    limit: int,
    http_client: httpx.AsyncClient,
    logger: Any,
    search_kind: str = "web",
    resource_type: str = "通用资源",
) -> dict[str, Any]:
    search_query = str(query or "").strip()
    if not search_query:
        return _build_payload(
            ok=False,
            query="",
            source_type=search_kind,
            results=[],
            error="missing_query",
        )
    limit = _normalize_limit(limit)
    query_variants = _build_search_queries(search_query, resource_type, search_kind=search_kind)
    nested = await asyncio.gather(
        *[
            _fetch_engine(engine, variant, http_client, logger)
            for variant in query_variants
            for engine in _SEARCH_ENGINES
        ],
        return_exceptions=True,
    )
    raw_results: list[dict[str, Any]] = []
    for item in nested:
        if isinstance(item, list):
            raw_results.extend(item)

    prioritized = _prioritize_web_results(
        _deduplicate(raw_results),
        search_query,
        search_kind,
        resource_type,
    )
    results = prioritized[:limit]
    return _build_payload(
        ok=bool(results),
        query=search_query,
        source_type=search_kind,
        results=results,
        error="" if results else "no_results",
    )


def _canonicalize_github_repo_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.netloc.lower().removeprefix("www.") != "github.com":
        return None
    match = _GITHUB_OWNER_REPO_RE.match(parsed.path or "")
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if owner.lower() in _GITHUB_RESERVED_SEGMENTS or repo.lower() in _GITHUB_RESERVED_SEGMENTS:
        return None
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return f"https://github.com/{owner}/{repo}"


def _github_entity_from_query(query: str) -> str:
    core = _extract_search_core(query)
    core = re.sub(r"\bsite:github\.com\b", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"\s+", " ", core)
    return core.strip(" :：，,。.!！?？")


async def _search_github_api(
    *,
    query: str,
    limit: int,
    sort: str,
    http_client: httpx.AsyncClient,
    github_token: str,
) -> list[dict[str, Any]]:
    search_terms = _github_entity_from_query(query)
    if not search_terms:
        return []

    params: dict[str, Any] = {
        "q": search_terms,
        "per_page": limit,
    }
    if sort == "stars":
        params["sort"] = "stars"
        params["order"] = "desc"

    headers = dict(_GITHUB_HEADERS)
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    response = await http_client.get(
        "https://api.github.com/search/repositories",
        params=params,
        headers=headers,
        timeout=_FETCH_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items", []) if isinstance(payload, dict) else []
    results: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name", "") or "").strip()
        html_url = str(item.get("html_url", "") or "").strip()
        if not full_name or not html_url:
            continue
        results.append(
            {
                "type": "github_repository",
                "full_name": full_name,
                "url": html_url,
                "title": full_name,
                "snippet": str(item.get("description", "") or "").strip(),
                "source": "GitHub API",
                "stars": int(item.get("stargazers_count", 0) or 0),
                "language": str(item.get("language", "") or "").strip(),
                "updated_at": str(item.get("updated_at", "") or "").strip(),
                "owner": str((item.get("owner") or {}).get("login", "") or "").strip()
                if isinstance(item.get("owner"), dict)
                else "",
            }
        )
    return results


async def _search_github_payload(
    *,
    query: str,
    limit: int,
    sort: str,
    http_client: httpx.AsyncClient,
    logger: Any,
    github_token: str = "",
) -> dict[str, Any]:
    search_query = str(query or "").strip()
    if not search_query:
        return _build_payload(
            ok=False,
            query="",
            source_type="github",
            results=[],
            error="missing_query",
        )

    limit = _normalize_limit(limit)
    try:
        results = await _search_github_api(
            query=search_query,
            limit=limit,
            sort=sort,
            http_client=http_client,
            github_token=github_token,
        )
        if results:
            return _build_payload(
                ok=True,
                query=search_query,
                source_type="github",
                results=results,
            )
    except Exception as exc:
        logger.debug(f"[resource_collector] github api search failed: {exc}")

    fallback = await _search_web_payload(
        query=search_query,
        limit=limit * 2,
        http_client=http_client,
        logger=logger,
        search_kind="github",
        resource_type="工具/软件",
    )
    github_results: list[dict[str, Any]] = []
    for item in fallback.get("results", []):
        if not isinstance(item, dict):
            continue
        canonical_url = _canonicalize_github_repo_url(str(item.get("url", "") or ""))
        if not canonical_url:
            continue
        github_results.append(
            {
                "type": "github_repository",
                "full_name": canonical_url.removeprefix("https://github.com/"),
                "url": canonical_url,
                "title": str(item.get("title", "") or canonical_url.removeprefix("https://github.com/")),
                "snippet": str(item.get("snippet", "") or "").strip(),
                "source": str(item.get("source", "GitHub") or "GitHub"),
                "engine": str(item.get("engine", "") or "").strip(),
            }
        )

    github_results = _deduplicate(github_results)[:limit]
    return _build_payload(
        ok=bool(github_results),
        query=search_query,
        source_type="github",
        results=github_results,
        degraded=True,
        notes=["github_api_unavailable_or_empty"] if github_results else ["github_api_unavailable_or_empty", "no_results"],
        error="" if github_results else "no_results",
    )


async def _execute_resource_plan(
    *,
    tasks: list[_ResourceSearchTask],
    http_client: httpx.AsyncClient,
    logger: Any,
    github_token: str,
    resource_type: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for task in tasks[:_MAX_SEARCH_TASKS]:
        if task.kind == "github":
            payload = await _search_github_payload(
                query=task.query,
                limit=task.limit,
                sort=task.sort,
                http_client=http_client,
                logger=logger,
                github_token=github_token,
            )
        elif task.kind == "official":
            payload = await _search_web_payload(
                query=task.query,
                limit=task.limit,
                http_client=http_client,
                logger=logger,
                search_kind="official",
                resource_type=resource_type,
            )
        elif task.kind == "image":
            payload = await _search_image_payload(
                query=task.query,
                limit=task.limit,
                http_client=http_client,
                logger=logger,
            )
            if not payload.get("results"):
                payload = await _search_web_payload(
                    query=task.query,
                    limit=task.limit,
                    http_client=http_client,
                    logger=logger,
                    search_kind="image",
                    resource_type=resource_type,
                )
        else:
            payload = await _search_web_payload(
                query=task.query,
                limit=task.limit,
                http_client=http_client,
                logger=logger,
                search_kind="web",
                resource_type=resource_type,
            )
        payload["planned_kind"] = task.kind
        payload["planned_query"] = task.query
        payload["purpose"] = task.purpose
        payloads.append(payload)
    return payloads


def _format_payload_as_text(payload: dict[str, Any], query: str, resource_type: str, max_count: int) -> str:
    results = payload.get("results", [])
    if not isinstance(results, list) or not results:
        return f"没找到关于「{query}」的{resource_type}，换个关键词试试？"
    limited = results[:max_count]
    lines = [f"为你整理了 {len(limited)} 条「{query}」相关{resource_type}：", ""]
    for index, item in enumerate(limited, 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or item.get("full_name", "") or item.get("url", ""))
        source = str(item.get("source", "未知来源") or "未知来源")
        lines.append(f"{index}. 【{source}】{title[:80]}")
        snippet = str(item.get("snippet", "") or "").strip()
        if snippet:
            lines.append(f"   {snippet}")
        if item.get("stars") not in (None, ""):
            lines.append(f"   stars: {item.get('stars')}")
        url = str(item.get("url", "") or "").strip()
        if url:
            lines.append(f"   {url}")
        lines.append("")
    return "\n".join(lines).rstrip()


async def search_web(
    query: str,
    limit: int = 5,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> str:
    payload = await _search_web_payload(
        query=query,
        limit=limit,
        http_client=http_client,
        logger=logger,
        search_kind="web",
        resource_type="通用资源",
    )
    return dumps_json_payload(payload)


async def search_official_site(
    query: str,
    limit: int = 5,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> str:
    payload = await _search_web_payload(
        query=query,
        limit=limit,
        http_client=http_client,
        logger=logger,
        search_kind="official",
        resource_type="通用资源",
    )
    return dumps_json_payload(payload)


async def search_images(
    query: str,
    limit: int = 5,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> str:
    payload = await _search_image_payload(
        query=query,
        limit=limit,
        http_client=http_client,
        logger=logger,
    )
    if not payload.get("results"):
        image_diagnostics = payload.get("image_search_diagnostics")
        payload = await _search_web_payload(
            query=query,
            limit=limit,
            http_client=http_client,
            logger=logger,
            search_kind="image",
            resource_type="图片/壁纸",
        )
        if isinstance(image_diagnostics, dict):
            payload["image_search_diagnostics"] = image_diagnostics
    return dumps_json_payload(payload)


async def search_github_repos(
    query: str,
    limit: int = 5,
    sort: str = "best_match",
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
    github_token: str = "",
) -> str:
    resolved_sort = "stars" if str(sort or "").strip().lower() == "stars" else "best_match"
    payload = await _search_github_payload(
        query=query,
        limit=limit,
        sort=resolved_sort,
        http_client=http_client,
        logger=logger,
        github_token=github_token,
    )
    return dumps_json_payload(payload)


async def confirm_resource_request(
    raw_query: str,
    context_hint: str = "",
    *,
    tool_caller: Any = None,
    logger: Any = None,
) -> dict[str, str]:
    resolved_logger = _get_logger(logger)
    effective_query = str(raw_query or "").strip() or str(context_hint or "").strip()
    if not effective_query:
        return {
            "confirmed_query": "",
            "resource_type": "通用资源",
            "confirm_message": "你想让我找什么资源？描述一下主题、作品名或者资源类型。",
        }
    plan = await _plan_resource_search(
        raw_query=effective_query,
        context_hint=context_hint,
        resource_type_hint="",
        tool_caller=tool_caller,
        logger=resolved_logger,
    )
    if plan.get("decision") == "clarify":
        return {
            "confirmed_query": str(plan.get("normalized_query", "") or effective_query),
            "resource_type": str(plan.get("resource_type", "通用资源") or "通用资源"),
            "confirm_message": str(plan.get("clarify_question", "") or "你具体想找什么资源？"),
        }
    confirmed_query = str(plan.get("normalized_query", "") or effective_query).strip()
    resource_type = _normalize_resource_type(str(plan.get("resource_type", "") or "通用资源"))
    return {
        "confirmed_query": confirmed_query,
        "resource_type": resource_type,
        "confirm_message": f"我会按「{confirmed_query}」去找{resource_type}，先给你整理几条最有用的结果。",
    }


async def collect_resources(
    query: str,
    resource_type: str = "通用资源",
    max_count: int = 5,
    *,
    http_client: httpx.AsyncClient,
    logger: Any,
    github_token: str = "",
    tool_caller: Any = None,
) -> str:
    resolved_logger = _get_logger(logger)
    search_query = str(query or "").strip()
    if not search_query:
        return "请先提供要搜集的资源主题。"
    max_count = _normalize_limit(max_count)
    plan = await _plan_resource_search(
        raw_query=search_query,
        context_hint="",
        resource_type_hint=resource_type,
        tool_caller=tool_caller,
        logger=resolved_logger,
    )
    if plan.get("decision") == "clarify":
        return str(plan.get("clarify_question", "") or "你具体想找什么资源？")

    normalized_query = str(plan.get("normalized_query", "") or search_query).strip()
    resolved_type = _normalize_resource_type(str(plan.get("resource_type", "") or resource_type or "通用资源"))
    tasks = _normalize_plan_tasks(plan.get("search_plan"))
    if not tasks:
        tasks = [_ResourceSearchTask(kind="web", query=normalized_query, limit=max_count, purpose="默认搜索")]

    payloads = await _execute_resource_plan(
        tasks=tasks,
        http_client=http_client,
        logger=resolved_logger,
        github_token=github_token,
        resource_type=resolved_type,
    )

    merged_results: list[dict[str, Any]] = []
    for payload in payloads:
        results = payload.get("results", [])
        if isinstance(results, list):
            merged_results.extend(results)
    merged_payload = _build_payload(
        ok=bool(merged_results),
        query=normalized_query,
        source_type="resource_plan",
        results=_deduplicate(merged_results)[:max_count],
        degraded=any(bool(payload.get("degraded")) for payload in payloads),
        notes=[
            f"{payload.get('planned_kind', 'web')}:{payload.get('planned_query', '')}"
            for payload in payloads
        ],
        error="" if merged_results else "no_results",
    )

    summary = await _summarize_resource_results(
        raw_query=search_query,
        normalized_query=normalized_query,
        resource_type=resolved_type,
        search_plan=plan.get("search_plan", []),
        payloads=payloads,
        tool_caller=tool_caller,
        logger=resolved_logger,
        max_count=max_count,
    )
    if summary:
        return summary
    return _format_payload_as_text(merged_payload, normalized_query, resolved_type, max_count)


def dumps_confirmation_payload(payload: dict[str, str]) -> str:
    return json.dumps(payload, ensure_ascii=False)
