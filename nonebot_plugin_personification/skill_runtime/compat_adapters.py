from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from ..agent.tool_registry import AgentTool


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_SEARCH_PATTERNS: dict[str, tuple[str, str]] = {
    "duckduckgo.com": (
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    ),
    "bing.com": (
        r'<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>',
        r'<p[^>]*class="[^"]*b_caption[^"]*"[^>]*><span>(.*?)</span>',
    ),
    "google.com": (
        r'<a[^>]+href="/url\?q=([^"&]+)[^"]*"[^>]*>(.*?)</a>',
        r'<div[^>]*class="[^"]*(?:VwiC3b|s3v9rd)[^"]*"[^>]*>(.*?)</div>',
    ),
    "google.com.hk": (
        r'<a[^>]+href="/url\?q=([^"&]+)[^"]*"[^>]*>(.*?)</a>',
        r'<div[^>]*class="[^"]*(?:VwiC3b|s3v9rd)[^"]*"[^>]*>(.*?)</div>',
    ),
    "so.com": (
        r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>',
    ),
    "search.yahoo.com": (
        r'<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        r'<div[^>]*class="[^"]*compText[^"]*"[^>]*>(.*?)</div>',
    ),
    "search.brave.com": (
        r'<a[^>]+href="([^"]+)"[^>]*data-type="web-result"[^>]*>(.*?)</a>',
        r'<div[^>]*class="[^"]*snippet[^"]*"[^>]*>(.*?)</div>',
    ),
    "sogou.com": (
        r'<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        r'<p[^>]*class="[^"]*str-text-info[^"]*"[^>]*>(.*?)</p>',
    ),
    "baidu.com": (
        r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        r'<div[^>]*class="[^"]*(?:c-abstract|content-right_8Zs40)[^"]*"[^>]*>(.*?)</div>',
    ),
}


def _slugify_tool_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", str(name or "").strip()).strip("_").lower()
    return value or "remote_skill"


def _strip_html(text: str) -> str:
    value = re.sub(r"<[^>]+>", " ", str(text or ""))
    value = (
        value.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", value).strip()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_metadata(skill_dir: Path) -> dict[str, Any]:
    for filename in ("_meta.json", "metadata.json"):
        loaded = _load_json(skill_dir / filename)
        if loaded:
            return loaded
    return {}


def _load_doc_context(skill_dir: Path, *, max_chars: int = 12000) -> str:
    chunks: list[str] = []
    for candidate in [skill_dir / "SKILL.md", skill_dir / "CHANGELOG.md", skill_dir / "CHANNELLOG.md"]:
        if candidate.exists():
            try:
                chunks.append(candidate.read_text(encoding="utf-8")[:4000])
            except Exception:
                continue
    references_dir = skill_dir / "references"
    if references_dir.exists() and references_dir.is_dir():
        for path in sorted(references_dir.glob("*.md"))[:3]:
            try:
                chunks.append(path.read_text(encoding="utf-8")[:2500])
            except Exception:
                continue
    merged = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    return merged[:max_chars]


def _get_pattern_for_url(url: str) -> tuple[str, str] | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for domain, patterns in _SEARCH_PATTERNS.items():
        if host == domain or host.endswith(f".{domain}"):
            return patterns
    return None


def _is_valid_result_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def _fetch_search_results(
    *,
    engine_name: str,
    url_template: str,
    query: str,
    limit: int,
    http_client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    search_url = url_template.replace("{keyword}", quote_plus(query))
    response = await http_client.get(search_url, headers=_HEADERS, timeout=15.0, follow_redirects=True)
    response.raise_for_status()
    html_text = response.text
    patterns = _get_pattern_for_url(search_url)
    if patterns is None:
        return [
            {
                "title": f"{engine_name} 搜索入口",
                "url": search_url,
                "snippet": f"该引擎当前未内置结果解析，返回搜索入口供继续使用。",
                "source": engine_name,
                "engine": engine_name,
                "type": "search_page",
            }
        ]

    result_pattern, snippet_pattern = patterns
    raw_results = re.findall(result_pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
    raw_snippets = re.findall(snippet_pattern, html_text, flags=re.IGNORECASE | re.DOTALL) if snippet_pattern else []
    results: list[dict[str, Any]] = []
    for index, match in enumerate(raw_results[: max(1, limit)]):
        if isinstance(match, tuple):
            url, title = match[0], match[1]
        else:
            url, title = match, ""
        url = _strip_html(url)
        title = _strip_html(title)
        if not _is_valid_result_url(url):
            continue
        snippet = ""
        if index < len(raw_snippets):
            snippet = _strip_html(raw_snippets[index])
        results.append(
            {
                "title": title or url,
                "url": url,
                "snippet": snippet[:240],
                "source": engine_name,
                "engine": engine_name,
                "type": "web_result",
            }
        )
    if results:
        return results
    return [
        {
            "title": f"{engine_name} 搜索入口",
            "url": search_url,
            "snippet": "未成功解析结果，返回搜索入口。",
            "source": engine_name,
            "engine": engine_name,
            "type": "search_page",
        }
    ]


def _deduplicate_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in results:
        url = str(item.get("url", "") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def _select_engines(
    engines: list[dict[str, Any]],
    *,
    engine_name: str,
    region: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    normalized_engine = str(engine_name or "").strip().lower()
    normalized_region = str(region or "").strip().lower()
    for engine in engines:
        if not isinstance(engine, dict):
            continue
        if normalized_engine and normalized_engine != str(engine.get("name", "") or "").strip().lower():
            continue
        if normalized_region and normalized_region != str(engine.get("region", "") or "").strip().lower():
            continue
        selected.append(engine)
    return selected or engines[: min(4, len(engines))]


def _build_search_engine_tools(
    *,
    skill_dir: Path,
    frontmatter: dict[str, Any],
    metadata: dict[str, Any],
) -> list[AgentTool]:
    config = _load_json(skill_dir / "config.json")
    engines = config.get("engines", [])
    if not isinstance(engines, list) or not engines:
        return []

    tool_name = _slugify_tool_name(frontmatter.get("name") or metadata.get("name") or skill_dir.name)
    description = str(frontmatter.get("description") or metadata.get("description") or "").strip()
    if not description:
        description = "兼容导入的多搜索引擎技能，支持按引擎或区域执行搜索并返回结构化结果。"

    async def _handler(
        query: str,
        engine: str = "",
        region: str = "",
        limit: int = 5,
    ) -> str:
        search_query = str(query or "").strip()
        if not search_query:
            return json.dumps({"ok": False, "query": "", "results": [], "error": "missing_query"}, ensure_ascii=False)
        resolved_limit = max(1, min(10, int(limit or 5)))
        selected_engines = _select_engines(engines, engine_name=engine, region=region)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            nested = await asyncio.gather(
                *[
                    _fetch_search_results(
                        engine_name=str(item.get("name", "") or "Unknown"),
                        url_template=str(item.get("url", "") or ""),
                        query=search_query,
                        limit=resolved_limit,
                        http_client=client,
                    )
                    for item in selected_engines
                    if str(item.get("url", "") or "").strip()
                ],
                return_exceptions=True,
            )
        merged: list[dict[str, Any]] = []
        for item in nested:
            if isinstance(item, list):
                merged.extend(item)
        payload = {
            "ok": bool(merged),
            "skill": tool_name,
            "query": search_query,
            "engine": str(engine or "").strip(),
            "region": str(region or "").strip(),
            "results": _deduplicate_results(merged, resolved_limit),
            "error": "" if merged else "no_results",
        }
        return json.dumps(payload, ensure_ascii=False)

    return [
        AgentTool(
            name=tool_name,
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "engine": {"type": "string", "description": "可选的具体搜索引擎名称，例如 DuckDuckGo、Bing CN"},
                    "region": {"type": "string", "description": "可选区域过滤，通常为 cn 或 global"},
                    "limit": {"type": "integer", "description": "返回结果数量，1 到 10", "default": 5},
                },
                "required": ["query"],
            },
            handler=_handler,
            local=True,
            enabled=lambda: True,
        )
    ]


def _build_doc_consult_tool(
    *,
    skill_dir: Path,
    frontmatter: dict[str, Any],
    metadata: dict[str, Any],
    runtime: Any,
) -> list[AgentTool]:
    doc_context = _load_doc_context(skill_dir)
    if not doc_context:
        return []
    tool_name = _slugify_tool_name(frontmatter.get("name") or metadata.get("name") or skill_dir.name)
    if tool_name.endswith("_skill"):
        consult_name = tool_name
    else:
        consult_name = f"{tool_name}_consult"
    description = str(frontmatter.get("description") or metadata.get("description") or "").strip()
    if not description:
        description = f"基于导入技能 {skill_dir.name} 的文档进行问答。"
    tool_caller = getattr(runtime, "tool_caller", None) if runtime is not None else None

    async def _handler(query: str) -> str:
        question = str(query or "").strip()
        if not question:
            return "请提供要咨询的问题。"
        if tool_caller is not None:
            try:
                response = await tool_caller.chat_with_tools(
                    [
                        {
                            "role": "system",
                            "content": (
                                "你是技能文档兼容适配器。"
                                "只能根据给定技能文档回答问题，不要编造文档里没有的信息。"
                                "如果文档不足以支持直接执行，就明确说明这是文档型 skill，需要额外执行器支持。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"技能文档：\n{doc_context}\n\n用户问题：{question}",
                        },
                    ],
                    [],
                    False,
                )
                if str(response.content or "").strip():
                    return str(response.content).strip()
            except Exception:
                pass
        excerpt = doc_context[:1800]
        return f"这是兼容导入的文档型 skill，目前没有独立执行脚本。\n文档摘要：\n{excerpt}"

    return [
        AgentTool(
            name=consult_name,
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要咨询的技能使用问题"},
                },
                "required": ["query"],
            },
            handler=_handler,
            local=True,
            enabled=lambda: True,
        )
    ]


def build_compat_tools(
    *,
    skill_dir: Path,
    frontmatter: dict[str, Any],
    runtime: Any,
) -> list[AgentTool]:
    metadata = _load_metadata(skill_dir)
    tools = _build_search_engine_tools(skill_dir=skill_dir, frontmatter=frontmatter, metadata=metadata)
    if tools:
        return tools
    return _build_doc_consult_tool(skill_dir=skill_dir, frontmatter=frontmatter, metadata=metadata, runtime=runtime)
