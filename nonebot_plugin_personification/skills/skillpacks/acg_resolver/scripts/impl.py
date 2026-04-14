from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

import httpx

try:
    from .....agent.tool_registry import AgentTool
    from .....core.web_grounding import do_web_search
    from ...vision_analyze.scripts.impl import analyze_images
    from ...wiki_search.scripts.impl import wiki_lookup_candidates
except ImportError:  # pragma: no cover
    from nonebot_plugin_personification.agent.tool_registry import AgentTool  # type: ignore
    from nonebot_plugin_personification.core.web_grounding import do_web_search  # type: ignore
    from nonebot_plugin_personification.skills.skillpacks.vision_analyze.scripts.impl import analyze_images  # type: ignore
    from nonebot_plugin_personification.skills.skillpacks.wiki_search.scripts.impl import wiki_lookup_candidates  # type: ignore


_TIME_SENSITIVE_QUERY_RE = re.compile(r"(最新|现在|当前|今天|刚刚|最近|进展|后续|活动|联动|版本|更新)")


class _SilentLogger:
    def warning(self, *_args, **_kwargs) -> None:
        return None


def _normalize_lookup_key(text: str) -> str:
    value = re.sub(r"\s+", "", str(text or "").strip().lower())
    return re.sub(r"[\-_/|:：,，。！？!?（）()【】\[\]<>]+", "", value)


def _web_search_enabled(runtime: Any) -> bool:
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return True
    enabled = bool(getattr(plugin_config, "personification_tool_web_search_enabled", True))
    mode = str(getattr(plugin_config, "personification_tool_web_search_mode", "enabled") or "enabled").strip().lower()
    return enabled and mode != "disabled"


def _should_fetch_web_evidence(query: str, top_candidates: list[dict[str, Any]]) -> bool:
    if not top_candidates:
        return True
    top_confidence = max(float(item.get("confidence", 0.0) or 0.0) for item in top_candidates)
    return top_confidence < 0.72 or bool(_TIME_SENSITIVE_QUERY_RE.search(str(query or "")))


async def _collect_web_evidence(runtime: Any, query: str) -> str:
    if not _web_search_enabled(runtime):
        return ""
    get_now = getattr(runtime, "get_now", None) or (lambda: datetime.now())
    logger = getattr(runtime, "logger", None) or _SilentLogger()
    return str(await do_web_search(query, get_now=get_now, logger=logger)).strip()


def _augment_candidates_with_web(
    *,
    query: str,
    candidates: list[dict[str, Any]],
    web_summary: str,
) -> list[dict[str, Any]]:
    summary = str(web_summary or "").strip()
    if not summary:
        return candidates
    normalized_summary = _normalize_lookup_key(summary)
    matched_any = False
    for candidate in candidates:
        names = [str(candidate.get("name", "") or "")]
        names.extend(str(alias) for alias in list(candidate.get("aliases") or []))
        if any(_normalize_lookup_key(name) and _normalize_lookup_key(name) in normalized_summary for name in names):
            candidate["confidence"] = round(min(0.99, float(candidate.get("confidence", 0.0) or 0.0) + 0.08), 3)
            candidate["why_matched"] = f"{candidate.get('why_matched', 'wiki')}+web"
            matched_any = True
    if matched_any:
        return candidates
    candidates.append(
        {
            "name": str(query or "").strip(),
            "type": "unknown",
            "franchise": "",
            "aliases": [],
            "why_matched": "web_search:evidence",
            "source_summary": summary[:220],
            "confidence": 0.48 if _TIME_SENSITIVE_QUERY_RE.search(str(query or "")) else 0.34,
        }
    )
    return candidates


async def resolve_acg_entity(
    *,
    runtime: Any,
    query: str,
    image_context: bool = False,
    images: list[str] | None = None,
) -> str:
    q = str(query or "").strip()
    if not q:
        return json.dumps(
            {
                "normalized_query": "",
                "top_candidates": [],
                "recommended_interpretation": "",
                "ambiguity_level": "none",
            },
            ensure_ascii=False,
        )

    shared_client = getattr(runtime, "http_client", None)
    if shared_client is not None:
        wiki_payload = await wiki_lookup_candidates(q, http_client=shared_client, logger=runtime.logger)
    else:
        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            wiki_payload = await wiki_lookup_candidates(q, http_client=http_client, logger=runtime.logger)

    top_candidates = []
    for item in list(wiki_payload.get("top_candidates", []) or [])[:3]:
        if not isinstance(item, dict):
            continue
        top_candidates.append(
            {
                "name": str(item.get("title", "") or ""),
                "type": str(item.get("type", "unknown") or "unknown"),
                "franchise": str(item.get("franchise", "") or ""),
                "aliases": list(item.get("aliases", []) or []),
                "why_matched": f"wiki:{item.get('source', '')}",
                "source_summary": str(item.get("summary", "") or ""),
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
        )

    visual_payload: dict[str, Any] | None = None
    if image_context:
        try:
            visual_raw = await analyze_images(runtime=runtime, query=q, images=images)
            parsed = json.loads(visual_raw)
            if isinstance(parsed, dict):
                visual_payload = parsed
        except Exception:
            visual_payload = None

    if visual_payload:
        for candidate in list(visual_payload.get("franchise_candidates", []) or [])[:2]:
            if not isinstance(candidate, dict):
                continue
            top_candidates.append(
                {
                    "name": str(candidate.get("name", "") or ""),
                    "type": "franchise",
                    "franchise": str(candidate.get("name", "") or ""),
                    "aliases": [],
                    "why_matched": str(candidate.get("why", "vision_match") or "vision_match"),
                    "source_summary": "来自图片视觉候选",
                    "confidence": float(candidate.get("confidence", 0.0) or 0.0),
                }
            )

    web_summary = ""
    if _should_fetch_web_evidence(q, top_candidates):
        try:
            web_summary = await _collect_web_evidence(runtime, q)
        except Exception:
            web_summary = ""
        if web_summary:
            top_candidates = _augment_candidates_with_web(query=q, candidates=top_candidates, web_summary=web_summary)

    deduped_candidates: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in sorted(top_candidates, key=lambda candidate: float(candidate.get("confidence", 0.0) or 0.0), reverse=True):
        key = _normalize_lookup_key(item.get("name", ""))
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        deduped_candidates.append(item)

    ambiguity_level = "high"
    if len(deduped_candidates) == 1:
        ambiguity_level = "low"
    elif len(deduped_candidates) in {2, 3}:
        ambiguity_level = "medium"

    payload = {
        "normalized_query": q,
        "top_candidates": deduped_candidates[:4],
        "recommended_interpretation": deduped_candidates[0]["name"] if deduped_candidates else "",
        "ambiguity_level": ambiguity_level,
    }
    if visual_payload:
        payload["image_context"] = {
            "scene_summary": visual_payload.get("scene_summary", ""),
            "visual_evidence": visual_payload.get("visual_evidence", []),
            "ambiguity_notes": visual_payload.get("ambiguity_notes", []),
        }
    if web_summary:
        payload["web_context"] = {
            "summary": web_summary[:320],
            "used_for": "time_sensitive_or_low_confidence_disambiguation",
        }
    return json.dumps(payload, ensure_ascii=False)


def build_resolver_tool(runtime: Any) -> AgentTool:
    async def _handler(query: str, image_context: bool = False, images: list[str] | None = None) -> str:
        return await resolve_acg_entity(runtime=runtime, query=query, image_context=image_context, images=images)

    return AgentTool(
        name="resolve_acg_entity",
        description=(
            "对动漫、游戏、角色、作品名、术语等高歧义实体做证据式消解。"
            "可结合 wiki、外网线索和当前图片上下文，输出多个候选及推荐解释。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "待消解实体"},
                "image_context": {"type": "boolean", "description": "是否结合当前图片上下文"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "可选图片引用列表"},
            },
            "required": ["query"],
        },
        handler=_handler,
    )

