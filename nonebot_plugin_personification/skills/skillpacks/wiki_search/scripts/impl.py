from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import httpx


_WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"
_MOEGIRL_API = "https://zh.moegirl.org.cn/api.php"
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_FETCH_TIMEOUT = 12.0
_FANDOM_TIMEOUT = 7.0
_MAX_CONTENT_CHARS = 600
_MAX_COMBINED_CHARS = 1200
_DEFAULT_FANDOM_WIKIS = {
    "原神": "genshin-impact",
    "genshin": "genshin-impact",
    "崩坏：星穹铁道": "honkai-star-rail",
    "崩坏星穹铁道": "honkai-star-rail",
    "崩铁": "honkai-star-rail",
    "星铁": "honkai-star-rail",
    "明日方舟": "mrfz",
    "鸣潮": "wuthering-waves",
    "绝区零": "zenless-zone-zero",
    "英雄联盟": "leagueoflegends",
    "lol": "leagueoflegends",
    "崩坏3": "honkaiimpact3",
}
_SOURCE_WEIGHTS = {
    "萌娘百科": 0.6,
    "维基百科": 0.5,
    "Fandom": 0.25,
}


@dataclass(slots=True)
class _WikiCandidate:
    source: str
    title: str
    snippet: str
    page_key: str
    api_url: str
    url: str | None = None
    rank_hint: int = 0
    score: float = 0.0


@dataclass(slots=True)
class _WikiSnippet:
    source: str
    title: str
    text: str
    url: str | None = None


class _SilentLogger:
    def debug(self, _msg: str) -> None:
        return None


def _get_logger(logger: Any = None) -> Any:
    return logger or _SilentLogger()


def _normalize_space(text: str | None) -> str:
    value = str(text or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\[[0-9]+\]", " ", value)
    value = value.replace("&quot;", '"')
    value = value.replace("&amp;", "&")
    value = value.replace("&#39;", "'")
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _clip_text(text: str | None, limit: int = _MAX_CONTENT_CHARS) -> str:
    value = _normalize_space(text)
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _normalize_lookup_key(text: str | None) -> str:
    value = _normalize_space(text).lower()
    value = re.sub(r"[\s\-_/|:：,，。！？!?（）()【】\[\]<>]+", "", value)
    return value


def _char_overlap_ratio(query: str, target: str) -> float:
    if not query or not target:
        return 0.0
    query_chars = [char for char in query if char.strip()]
    if not query_chars:
        return 0.0
    target_set = set(target)
    hits = sum(1 for char in query_chars if char in target_set)
    return hits / max(1, len(query_chars))


def _merge_mapping(extra_fandom_wikis: dict[str, str] | None = None) -> dict[str, str]:
    mapping = dict(_DEFAULT_FANDOM_WIKIS)
    if extra_fandom_wikis:
        mapping.update(
            {
                str(key).strip(): str(value).strip()
                for key, value in extra_fandom_wikis.items()
                if str(key).strip() and str(value).strip()
            }
        )
    return mapping


def _detect_fandom_wiki(query: str, mapping: dict[str, str]) -> str | None:
    lowered = _normalize_lookup_key(query)
    if not lowered:
        return None
    for keyword in sorted(mapping, key=len, reverse=True):
        if _normalize_lookup_key(keyword) in lowered:
            return mapping[keyword]
    return None


def _build_fandom_api_url(subdomain: str) -> str:
    return f"https://{subdomain}.fandom.com/api.php"


async def _request_json(
    url: str,
    *,
    params: dict[str, Any],
    timeout: float,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if http_client is not None:
        resp = await http_client.get(url, params=params, headers=_DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    async with httpx.AsyncClient(headers=_DEFAULT_HEADERS, follow_redirects=True) as client:
        resp = await client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()


async def _fetch_from_source(
    api_url: str,
    query: str,
    *,
    source: str,
    timeout: float,
    limit: int = 5,
    http_client: httpx.AsyncClient | None = None,
) -> list[_WikiCandidate]:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": max(1, limit),
        "format": "json",
        "utf8": 1,
    }
    data = await _request_json(api_url, params=params, timeout=timeout, http_client=http_client)
    search = data.get("query", {}).get("search", [])
    candidates: list[_WikiCandidate] = []
    for index, item in enumerate(search):
        if not isinstance(item, dict):
            continue
        title = _normalize_space(item.get("title"))
        if not title:
            continue
        snippet = _clip_text(item.get("snippet"))
        page_key = str(item.get("pageid") or title).strip() or title
        candidates.append(
            _WikiCandidate(
                source=source,
                title=title,
                snippet=snippet,
                page_key=page_key,
                api_url=api_url,
                rank_hint=index,
            )
        )
    return candidates


async def _fetch_fandom_candidates(
    query: str,
    *,
    mapping: dict[str, str],
    logger: Any,
    http_client: httpx.AsyncClient | None = None,
) -> list[_WikiCandidate]:
    subdomain = _detect_fandom_wiki(query, mapping)
    if not subdomain:
        return []
    try:
        return await _fetch_from_source(
            _build_fandom_api_url(subdomain),
            query,
            source="Fandom",
            timeout=_FANDOM_TIMEOUT,
            limit=4,
            http_client=http_client,
        )
    except Exception as e:
        logger.debug(f"wiki_search fandom lookup failed: {e}")
        return []


def _rank_candidates(query: str, candidates: list[_WikiCandidate]) -> list[_WikiCandidate]:
    query_key = _normalize_lookup_key(query)
    unique: dict[tuple[str, str], _WikiCandidate] = {}
    for candidate in candidates:
        unique_key = (candidate.source, _normalize_lookup_key(candidate.title))
        existing = unique.get(unique_key)
        if existing is None or len(candidate.snippet) > len(existing.snippet):
            unique[unique_key] = candidate

    ranked: list[_WikiCandidate] = []
    for candidate in unique.values():
        title_key = _normalize_lookup_key(candidate.title)
        snippet_key = _normalize_lookup_key(candidate.snippet)
        score = 0.0
        if title_key and title_key == query_key:
            score += 4.0
        if query_key and title_key and query_key in title_key:
            score += 2.8
        if query_key and title_key and title_key in query_key:
            score += 1.2
        score += SequenceMatcher(None, query_key, title_key).ratio() * 2.5
        score += _char_overlap_ratio(query_key, title_key) * 2.2
        score += _char_overlap_ratio(query_key, snippet_key) * 0.9
        score += _SOURCE_WEIGHTS.get(candidate.source, 0.0)
        score += max(0.0, 0.6 - candidate.rank_hint * 0.12)
        candidate.score = round(score, 4)
        ranked.append(candidate)

    ranked.sort(key=lambda item: (-item.score, item.rank_hint, len(item.title)))
    return ranked


async def _fetch_candidate_extract(
    candidate: _WikiCandidate,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> _WikiSnippet | None:
    params = {
        "action": "query",
        "prop": "extracts|info",
        "titles": candidate.title,
        "redirects": 1,
        "inprop": "url",
        "explaintext": 1,
        "exintro": 1,
        "format": "json",
        "utf8": 1,
    }
    timeout = _FANDOM_TIMEOUT if candidate.source == "Fandom" else _FETCH_TIMEOUT
    data = await _request_json(
        candidate.api_url,
        params=params,
        timeout=timeout,
        http_client=http_client,
    )
    pages = data.get("query", {}).get("pages", {})
    if not isinstance(pages, dict):
        return None
    for page in pages.values():
        if not isinstance(page, dict) or "missing" in page:
            continue
        resolved_title = _normalize_space(page.get("title")) or candidate.title
        text = _clip_text(page.get("extract"))
        if not text:
            continue
        url = _normalize_space(page.get("fullurl")) or candidate.url or None
        return _WikiSnippet(
            source=candidate.source,
            title=resolved_title,
            text=text,
            url=url,
        )
    return None


def _is_broad_query(query: str, candidates: list[_WikiCandidate]) -> bool:
    if len(candidates) < 2:
        return False
    top = candidates[0]
    second = candidates[1]
    query_key = _normalize_lookup_key(query)
    title_ratio = SequenceMatcher(None, query_key, _normalize_lookup_key(top.title)).ratio()
    if top.score < 3.0:
        return True
    if top.score - second.score < 0.9:
        return True
    return title_ratio < 0.7 and len(query_key) >= 6


def _has_strong_precise_match(query: str, candidates: list[_WikiCandidate]) -> bool:
    if not candidates:
        return False
    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    query_key = _normalize_lookup_key(query)
    title_key = _normalize_lookup_key(top.title)
    title_ratio = SequenceMatcher(None, query_key, title_key).ratio()
    if not title_key:
        return False
    if title_key == query_key or query_key in title_key:
        return second is None or top.score - second.score >= 0.6 or top.score >= 4.0
    if title_ratio >= 0.84 and top.score >= 3.2:
        return second is None or top.score - second.score >= 1.0
    return False


def _render_snippets(snippets: list[_WikiSnippet]) -> str:
    lines: list[str] = []
    total_chars = 0
    for snippet in snippets:
        line = f"{snippet.source}·{snippet.title}: {snippet.text}"
        if snippet.url:
            line += f" ({snippet.url})"
        if total_chars + len(line) > _MAX_COMBINED_CHARS:
            remaining = max(0, _MAX_COMBINED_CHARS - total_chars)
            if remaining <= 8:
                break
            line = line[: remaining - 1].rstrip() + "…"
        lines.append(line)
        total_chars += len(line)
        if total_chars >= _MAX_COMBINED_CHARS:
            break
    return "\n".join(lines)


def _candidate_type_hint(title: str, text: str) -> str:
    raw = f"{title} {text}".lower()
    if any(token in raw for token in ("角色", "人物", "cv", "声优", "主角")):
        return "character"
    if any(token in raw for token in ("动画", "漫画", "游戏", "作品", "系列")):
        return "franchise"
    if any(token in raw for token in ("术语", "设定", "组织", "道具")):
        return "term"
    return "unknown"


async def wiki_lookup_candidates(
    query: str,
    *,
    extra_fandom_wikis: dict[str, str] | None = None,
    http_client: httpx.AsyncClient | None = None,
    logger: Any = None,
) -> dict[str, Any]:
    q = _normalize_space(query)
    if not q:
        return {
            "query": "",
            "top_candidates": [],
            "recommended_interpretation": "",
            "ambiguity_level": "none",
            "summary": "",
        }

    logger = _get_logger(logger)
    mapping = _merge_mapping(extra_fandom_wikis)
    raw_results = await asyncio.gather(
        _fetch_from_source(
            _WIKIPEDIA_API,
            q,
            source="维基百科",
            timeout=_FETCH_TIMEOUT,
            http_client=http_client,
        ),
        _fetch_from_source(
            _MOEGIRL_API,
            q,
            source="萌娘百科",
            timeout=_FETCH_TIMEOUT,
            http_client=http_client,
        ),
        _fetch_fandom_candidates(
            q,
            mapping=mapping,
            logger=logger,
            http_client=http_client,
        ),
        return_exceptions=True,
    )

    candidates: list[_WikiCandidate] = []
    for item in raw_results:
        if isinstance(item, Exception):
            logger.debug(f"wiki_search source lookup failed: {item}")
            continue
        candidates.extend(item)

    ranked = _rank_candidates(q, candidates)
    if not ranked:
        return {
            "query": q,
            "top_candidates": [],
            "recommended_interpretation": "",
            "ambiguity_level": "high",
            "summary": "",
        }

    target_count = 1 if _has_strong_precise_match(q, ranked) and not _is_broad_query(q, ranked) else min(3, len(ranked))
    selected: list[_WikiCandidate] = []
    seen_titles: set[tuple[str, str]] = set()
    for candidate in ranked:
        key = (candidate.source, _normalize_lookup_key(candidate.title))
        if key in seen_titles:
            continue
        seen_titles.add(key)
        selected.append(candidate)
        if len(selected) >= target_count:
            break

    snippets: list[_WikiSnippet] = []
    for candidate in selected:
        try:
            snippet = await _fetch_candidate_extract(candidate, http_client=http_client)
        except Exception as e:
            logger.debug(f"wiki_search fetch extract failed: {e}")
            snippet = None
        if snippet is None:
            fallback_text = _clip_text(candidate.snippet)
            if fallback_text:
                snippet = _WikiSnippet(
                    source=candidate.source,
                    title=candidate.title,
                    text=fallback_text,
                    url=candidate.url,
                )
        if snippet is not None:
            snippets.append(snippet)

    top_candidates: list[dict[str, Any]] = []
    for candidate, snippet in zip(selected, snippets):
        top_candidates.append(
            {
                "title": snippet.title,
                "type": _candidate_type_hint(snippet.title, snippet.text),
                "franchise": "",
                "aliases": [],
                "summary": snippet.text,
                "source": snippet.source,
                "url": snippet.url or "",
                "confidence": round(min(max(candidate.score / 5.5, 0.0), 0.99), 3),
            }
        )

    return {
        "query": q,
        "top_candidates": top_candidates,
        "recommended_interpretation": top_candidates[0]["title"] if top_candidates else "",
        "ambiguity_level": "low" if len(top_candidates) <= 1 else "medium",
        "summary": _render_snippets(snippets),
    }


async def wiki_lookup(
    query: str,
    *,
    extra_fandom_wikis: dict[str, str] | None = None,
    http_client: httpx.AsyncClient | None = None,
    logger: Any = None,
) -> str:
    payload = await wiki_lookup_candidates(
        query,
        extra_fandom_wikis=extra_fandom_wikis,
        http_client=http_client,
        logger=logger,
    )
    if not payload.get("top_candidates"):
        return "未找到足够可靠的 Wiki 条目。"
    return json.dumps(payload, ensure_ascii=False)


async def fetch_wiki_summary(
    query: str,
    *,
    extra_fandom_wikis: dict[str, str] | None = None,
    http_client: httpx.AsyncClient | None = None,
    logger: Any = None,
) -> str:
    result = await wiki_lookup(
        query,
        extra_fandom_wikis=extra_fandom_wikis,
        http_client=http_client,
        logger=logger,
    )
    if result.startswith("未找到") or result.startswith("没有找到"):
        return ""
    try:
        parsed = json.loads(result)
    except Exception:
        return _clip_text(result, _MAX_COMBINED_CHARS)
    if not isinstance(parsed, dict):
        return ""
    return _clip_text(str(parsed.get("summary", "") or ""), _MAX_COMBINED_CHARS)
