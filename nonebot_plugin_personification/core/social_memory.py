"""Project trusted *shape* of social evidence into bounded memory records.

Platform adapters and video models never write directly to the memory palace.
This projector stores only a short, untrusted summary plus provenance metadata;
raw posts, comments, media and page text stay in the turn-scoped MCP packet.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .embedding_index import normalize_text


@dataclass
class SocialMemoryProjection:
    status: str = "skipped"
    summary_memory_ids: list[str] = field(default_factory=list)
    sense_ids: list[str] = field(default_factory=list)
    source_group_count: int = 0
    source_origin_count: int = 0
    diagnostic_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary_memory_ids": list(self.summary_memory_ids),
            "sense_ids": list(self.sense_ids),
            "source_group_count": self.source_group_count,
            "source_origin_count": self.source_origin_count,
            "diagnostic_codes": list(dict.fromkeys(self.diagnostic_codes)),
        }


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


_SOCIAL_HOSTS = {
    "bilibili": {"bilibili.com", "www.bilibili.com"},
    "douyin": {"douyin.com", "www.douyin.com"},
    "tieba": {"tieba.baidu.com"},
    "xiaoheihe": {"xiaoheihe.cn", "www.xiaoheihe.cn"},
}


def _url(value: Any, *, platform: str = "") -> str:
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
        host = str(parts.hostname or "").lower()
        allowed_hosts = _SOCIAL_HOSTS.get(str(platform or "").lower())
        if (
            parts.scheme.lower() != "https"
            or not host
            or (allowed_hosts is not None and host not in allowed_hosts)
            or parts.username
            or parts.password
        ):
            return ""
        path = parts.path.rstrip("/")
        patterns = {
            "bilibili": r"/video/(?:BV|av)[A-Za-z0-9_-]+",
            "douyin": r"/(?:video|note)/[0-9]+",
            "tieba": r"/p/[0-9]+",
            "xiaoheihe": r"/app/bbs/link/[0-9]+",
        }
        pattern = patterns.get(str(platform or "").lower())
        if pattern and not re.fullmatch(pattern, path, re.IGNORECASE):
            return ""
        return urlunsplit(("https", host, path, "", ""))[:500]
    except Exception:
        return ""


def _packet(value: Any) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(str(value or "")) if not isinstance(value, dict) else value
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _analysis_summary(value: Any) -> str:
    if isinstance(value, str):
        return _clean(value, 240)
    if not isinstance(value, dict):
        return ""
    for key in ("summary", "consensus_meaning", "meaning", "digest", "observation"):
        raw = value.get(key)
        if isinstance(raw, str) and _clean(raw, 240):
            return _clean(raw, 240)
    for key in ("timeline", "captions", "transcript", "content"):
        raw = value.get(key)
        if isinstance(raw, list):
            text = "；".join(_clean(item, 80) for item in raw if _clean(item, 80))
            if text:
                return text[:240]
    return ""


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _source_rows(packet: dict[str, Any]) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    groups: set[str] = set()
    origins: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw in list(packet.get("items") or []):
        if not isinstance(raw, dict):
            continue
        platform = _clean(raw.get("platform"), 60).lower()
        content_id = _clean(raw.get("content_id"), 120)
        group = _clean(raw.get("source_group_id"), 160) or f"{platform}:{content_id}"
        if group:
            groups.add(group)
        if platform:
            origins.add(platform)
        row = {
            "platform": platform,
            "content_id": content_id,
            "source_group_id": group,
            "canonical_url": _url(raw.get("canonical_url"), platform=platform),
            "title": _clean(raw.get("title"), 120),
            "detail_status": _clean(raw.get("detail_status"), 40).lower(),
            "detail_summary": _analysis_summary(raw.get("caption_or_body") or raw.get("description") or raw.get("body")),
        }
        rows.append(row)
    for raw in list(packet.get("source_groups") or []):
        if not isinstance(raw, dict):
            continue
        group = _clean(raw.get("group_id"), 160)
        if group:
            groups.add(group)
        for platform in list(raw.get("platforms") or []):
            value = _clean(platform, 60).lower()
            if value:
                origins.add(value)
    return groups, origins, rows


async def project_social_evidence(
    *,
    memory_store: Any,
    packet: str | dict[str, Any],
    group_id: str = "",
    user_id: str = "",
    turn_plan: Any = None,
    handoff: Any = None,
    analyses: list[dict[str, Any]] | None = None,
    logger: Any = None,
    record_trace: Callable[..., None] | None = None,
) -> SocialMemoryProjection:
    """Write bounded social summaries for one explicitly researched turn."""

    data = _packet(packet)
    projection = SocialMemoryProjection()
    if memory_store is None or not bool(getattr(memory_store, "palace_enabled", lambda: False)()):
        projection.diagnostic_codes.append("social_memory_disabled")
        return projection
    config = getattr(memory_store, "plugin_config", None)
    if not bool(getattr(config, "personification_social_memory_enabled", True)):
        projection.diagnostic_codes.append("social_memory_disabled")
        return projection
    plan_goal = str(getattr(turn_plan, "session_goal", "") or "").strip()
    media_followup = str(data.get("media_followup", "") or "")
    explicit_research = bool(
        getattr(turn_plan, "research_need", "")
        or getattr(turn_plan, "memory_need", "")
        or getattr(turn_plan, "evidence_policy", "") not in {"", "none"}
        or "research" in plan_goal.lower()
        or isinstance(data.get("semantic_validation"), dict)
        or media_followup
        or handoff is not None
    )
    if not explicit_research:
        projection.diagnostic_codes.append("social_memory_not_research_turn")
        return projection
    groups, origins, rows = _source_rows(data)
    analysis_rows = list(analyses or [])
    detail_rows: list[dict[str, Any]] = []
    if handoff is not None:
        analysis_rows = list(getattr(handoff, "analyses", []) or analysis_rows)
        detail_rows = [
            dict(item)
            for item in list(getattr(handoff, "detail_evidence", []) or [])
            if isinstance(item, dict)
        ]
    detail_by_group: dict[str, str] = {}
    for raw in detail_rows:
        group = _clean(raw.get("source_group_id"), 160)
        summary = _analysis_summary(raw.get("summary") or raw)
        if group and summary:
            detail_by_group[group] = summary
    for item in analysis_rows:
        if isinstance(item, dict):
            group = _clean(item.get("source_group_id"), 160)
            platform = _clean(item.get("platform"), 60).lower()
            if group:
                groups.add(group)
            if platform:
                origins.add(platform)
    projection.source_group_count = len(groups)
    projection.source_origin_count = len(origins)
    semantic = data.get("semantic_validation") if isinstance(data.get("semantic_validation"), dict) else {}
    semantic_status = _clean(semantic.get("status"), 30).lower()
    meaning = _clean(semantic.get("consensus_meaning"), 240)
    sense_id = _clean(semantic.get("consensus_sense_id"), 160)
    if sense_id:
        projection.sense_ids.append(sense_id)
    ready_packet_groups = {
        row["source_group_id"]
        for row in rows
        if row.get("detail_status") == "ready" and row.get("detail_summary")
    }
    analysis_by_group: dict[str, str] = {}
    for item in analysis_rows:
        if not isinstance(item, dict):
            continue
        group = _clean(item.get("source_group_id"), 160)
        summary = _analysis_summary(item.get("observation", item))
        if group and summary:
            analysis_by_group[group] = summary
    detail_groups = ready_packet_groups | set(detail_by_group) | set(analysis_by_group)
    # A coverage/semantic result alone is not detail evidence.  Only a ready
    # detail item, a bounded detail projection, or a video observation can
    # authorize a memory write.
    has_detail = bool(detail_groups)
    if not has_detail:
        projection.status = "skipped"
        projection.diagnostic_codes.append("social_memory_skipped_no_detail")
        return projection
    if semantic_status == "conflict":
        projection.diagnostic_codes.append("social_memory_skipped_conflict")
    claims = [item for item in list(data.get("slang_claims") or []) if isinstance(item, dict)]
    claim_groups = {
        _clean(item.get("source_cluster_id") or item.get("source_group_id"), 160)
        for item in claims
        if _clean(item.get("source_cluster_id") or item.get("source_group_id"), 160)
    }
    claim_origins: set[str] = set()
    for claim in claims:
        for ref in list(claim.get("evidence_refs") or []):
            if not isinstance(ref, dict):
                continue
            origin = _clean(ref.get("evidence_origin") or ref.get("platform"), 100).lower()
            if origin:
                claim_origins.add(origin)
    semantic_origins = {
        _clean(item, 100).lower()
        for item in list(semantic.get("supporting_origins") or [])
        if _clean(item, 100)
    }
    try:
        supporting_group_count = int(semantic.get("supporting_source_group_count", 0) or 0)
    except (TypeError, ValueError):
        supporting_group_count = 0
    supporting_group_count = max(supporting_group_count, len(claim_groups))
    supporting_origins = semantic_origins | claim_origins
    # Global promotion is based on supporting claims, never the number of
    # unrelated search hits that happened to share the result packet.
    auto_eligible = bool(
        semantic_status == "confirmed"
        and meaning
        and supporting_group_count >= 3
        and len(supporting_origins) >= 2
        and claim_groups
    )
    ttl_days = max(1, min(90, int(getattr(config, "personification_social_memory_summary_ttl_days", 14) or 14)))
    now = time.time()
    expires_at = now + ttl_days * 86400
    source_refs = [row["canonical_url"] for row in rows if row.get("canonical_url")]
    source_refs = list(dict.fromkeys(source_refs))[:3]
    # One bounded summary per source group/content fingerprint, never raw media.
    written_groups: set[str] = set()
    wrote_verified = False
    for row in rows:
        source_group = row["source_group_id"]
        if not source_group or source_group in written_groups or source_group not in detail_groups:
            continue
        written_groups.add(source_group)
        summary = str(row.get("detail_summary") or "").strip()[:240]
        if not summary:
            summary = detail_by_group.get(source_group, "")[:240]
        if not summary:
            summary = analysis_by_group.get(source_group, "")[:240]
        if not summary:
            summary = meaning
        if not summary:
            for analysis in analysis_rows:
                if str(analysis.get("source_group_id") or "") == source_group:
                    summary = _analysis_summary(analysis.get("observation", analysis))
                    if summary:
                        break
        if not summary:
            summary = _clean(row.get("title"), 240)
        if not summary:
            continue
        row_verified = bool(auto_eligible and source_group in claim_groups)
        summary_status = "verified" if row_verified else "candidate"
        fingerprint = hashlib.sha256(f"{source_group}\0{normalize_text(summary)}".encode("utf-8")).hexdigest()
        memory_id = _stable_id("social", str(group_id or ""), source_group, fingerprint)
        payload = {
            "memory_id": memory_id,
            "memory_type": "group_meme" if semantic_status in {"confirmed", "conflict"} else "group_knowledge",
            "palace_zone": "group",
            "summary": summary[:240],
            "source_kind": "social_mcp_summary",
            "source_group_ids": [source_group],
            "source_origins": sorted(origins)[:8],
            "evidence_refs": source_refs,
            "source_refs": source_refs,
            "trust": "untrusted_data_only",
            "summary_status": summary_status,
            "auto_context_eligible": row_verified,
            "content_fingerprint": fingerprint,
            "semantic_scope": "group",
            "user_id": str(user_id or ""),
            "group_id": str(group_id or ""),
            "group_scope": "isolated",
            "cross_group_allowed": False,
            "expires_at": expires_at,
            "time_created": now,
            "supports_recall": True,
            "supports_autofill": False,
            "confidence": 0.86 if summary_status == "verified" else 0.62,
            "salience": 0.62,
            "stability": 0.28,
            "topic_tags": ["social_evidence"],
            "entity_tags": [row.get("platform", ""), row.get("content_id", "")],
            "snippets": [summary[:120]],
        }
        if not str(group_id or "").strip():
            # A private social lookup has no group scope to bind to.  Keep it
            # user-private instead of creating a blank-group record that could
            # otherwise look globally visible to a later group recall.
            payload["permission_type"] = "private_fact"
            payload["group_scope"] = "isolated"
        try:
            await asyncio.to_thread(memory_store.write_memory_item, payload)
            projection.summary_memory_ids.append(memory_id)
            wrote_verified = wrote_verified or row_verified
        except Exception as exc:
            if logger is not None:
                try:
                    logger.debug(f"[memory] social projection failed: {type(exc).__name__}")
                except Exception:
                    pass
    # A verified cross-group bridge is a compact semantic record, not a copy of
    # every post.  It is only created when both source thresholds are met.
    if auto_eligible and meaning:
        bridge_id = _stable_id("social-global", meaning, *sorted(claim_groups))
        bridge = {
            "memory_id": bridge_id,
            "memory_type": "concept_anchor",
            "palace_zone": "topic",
            "tier": "semantic",
            "summary": meaning[:240],
            "source_kind": "social_mcp_summary",
            "source_group_ids": sorted(claim_groups)[:12],
            "source_origins": sorted(supporting_origins)[:8],
            "evidence_refs": source_refs,
            "source_refs": source_refs,
            "trust": "untrusted_data_only",
            "summary_status": "verified",
            "auto_context_eligible": True,
            "content_fingerprint": hashlib.sha256(normalize_text(meaning).encode("utf-8")).hexdigest(),
            "semantic_scope": "global",
            "sense_id": sense_id,
            "group_id": "",
            "group_scope": "shared",
            "cross_group_allowed": True,
            "expires_at": 0,
            "time_created": now,
            "supports_recall": True,
            "supports_autofill": True,
            "confidence": 0.9,
            "salience": 0.78,
            "stability": 0.72,
            "topic_tags": ["social_evidence", "verified_claim"],
            "entity_tags": [],
            "snippets": [meaning[:120]],
        }
        try:
            await asyncio.to_thread(memory_store.write_memory_item, bridge)
            projection.summary_memory_ids.append(bridge_id)
            wrote_verified = True
            projection.diagnostic_codes.append("social_memory_global_promoted")
        except Exception:
            pass
    if projection.summary_memory_ids and wrote_verified:
        projection.status = "committed"
        projection.diagnostic_codes.append("social_memory_committed")
    elif projection.summary_memory_ids:
        projection.status = "candidate"
        projection.diagnostic_codes.append("social_memory_candidate_created")
    else:
        projection.status = "candidate"
        projection.diagnostic_codes.append("social_memory_candidate_created")
    if record_trace is not None:
        record_trace(
            key="social_memory_projected",
            label="社交证据记忆投影",
            status="ok" if projection.summary_memory_ids else "warn",
            detail=(
                f"status={projection.status} groups={projection.source_group_count} "
                f"origins={projection.source_origin_count} records={len(projection.summary_memory_ids)} "
                f"global_verified={str(auto_eligible).lower()}"
            ),
        )
    return projection


class SocialMemoryProjector:
    """Small shared service façade for runtime and background callers."""

    def __init__(self, memory_store: Any, logger: Any = None) -> None:
        self.memory_store = memory_store
        self.logger = logger

    async def project(self, **kwargs: Any) -> dict[str, Any]:
        result = await project_social_evidence(
            memory_store=self.memory_store,
            logger=kwargs.pop("logger", self.logger),
            **kwargs,
        )
        return result.as_dict()


__all__ = ["SocialMemoryProjection", "SocialMemoryProjector", "project_social_evidence"]
