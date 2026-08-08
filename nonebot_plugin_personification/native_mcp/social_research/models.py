from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


PLATFORMS = ("bilibili", "douyin", "tieba", "xiaoheihe")
QUALITY_MODES = ("balanced", "strict", "ranking_only")


DEFAULT_PLATFORM_CONFIG: dict[str, Any] = {
    "quality_mode": "balanced",
    "marketing_threshold": 0.75,
    "min_play_count": 3000,
    "min_comment_count": 5,
    "min_reply_count": 3,
    "max_results": 10,
    "comment_limit": 50,
    "danmaku_limit": 200,
    "cache_ttl_seconds": 21600,
    "request_timeout_seconds": 12,
}


_MARKETING_PATTERNS = (
    re.compile(r"(?:加|联系|私聊|私信)(?:我|微信|vx|v信|qq)", re.IGNORECASE),
    re.compile(r"(?:代练|陪玩|带打|上分|低价|优惠|下单|购买|店铺|代理|推广|返利|抽奖)", re.IGNORECASE),
    re.compile(r"(?:https?://|www\.|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.IGNORECASE),
    re.compile(r"(?:vx|v信|微信|qq)\s*[:：]?\s*[A-Za-z0-9_-]{5,}", re.IGNORECASE),
)


def clean_text(value: Any, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def normalize_url(value: Any) -> str:
    text = clean_text(value, 1000)
    if text.startswith("//"):
        return "https:" + text
    return text if text.startswith(("https://", "http://")) else ""


def stable_fingerprint(*parts: Any) -> str:
    payload = "\x1f".join(clean_text(part, 4000).lower() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def marketing_score(item: dict[str, Any]) -> tuple[float, list[str]]:
    text = " ".join(
        clean_text(value, 3000)
        for value in (item.get("title"), item.get("caption_or_body"))
        if value
    )
    reasons: list[str] = []
    score = 0.0
    if item.get("commercial_label"):
        score += 0.6
        reasons.append("platform_commercial_label")
    matched = 0
    for pattern in _MARKETING_PATTERNS:
        if pattern.search(text):
            matched += 1
    if matched:
        score += min(0.6, matched * 0.2)
        reasons.append(f"promotional_signals:{matched}")
    duplicate_ratio = float(item.get("author_duplicate_ratio", 0) or 0)
    if duplicate_ratio >= 0.7:
        score += 0.25
        reasons.append("high_author_duplication")
    return min(1.0, score), reasons


def apply_quality_filter(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    score, reasons = marketing_score(result)
    result["marketing_score"] = round(score, 4)
    result["marketing_reasons"] = reasons
    stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
    mode = str(config.get("quality_mode") or "balanced")
    marketing_threshold = float(config.get("marketing_threshold", 0.75) or 0.75)
    filtered_reason = ""
    if score >= marketing_threshold:
        filtered_reason = "marketing_risk"
    elif result.get("content_type") == "video":
        play_count = int(stats.get("play_count", 0) or 0)
        comment_count = int(stats.get("comment_count", 0) or 0)
        low_play = play_count < int(config.get("min_play_count", 3000) or 3000)
        low_comments = comment_count < int(config.get("min_comment_count", 5) or 5)
        if mode == "strict" and (low_play or low_comments):
            filtered_reason = "low_video_engagement"
        elif mode == "balanced" and low_play and low_comments:
            filtered_reason = "low_video_engagement"
    else:
        reply_count = int(stats.get("reply_count", stats.get("comment_count", 0)) or 0)
        other_engagement = sum(int(stats.get(key, 0) or 0) for key in ("like_count", "favorite_count"))
        low_replies = reply_count < int(config.get("min_reply_count", 3) or 3)
        if mode == "strict" and low_replies:
            filtered_reason = "low_community_engagement"
        elif mode == "balanced" and low_replies and other_engagement == 0:
            filtered_reason = "low_community_engagement"
    result["retained"] = not filtered_reason or mode == "ranking_only"
    result["filtered_reason"] = filtered_reason
    heat = min(1.0, (int(stats.get("play_count", 0) or 0) / 100000) + (int(stats.get("comment_count", 0) or 0) / 1000))
    result["quality_score"] = round(max(0.0, 1.0 - score) * 0.7 + heat * 0.3, 4)
    return result


@dataclass
class ContentPacket:
    items: list[dict[str, Any]] = field(default_factory=list)
    platform_statuses: dict[str, Any] = field(default_factory=dict)
    partial: bool = False
    warnings: list[str] = field(default_factory=list)
    filtered_counts: dict[str, int] = field(default_factory=dict)
    aggregation: dict[str, Any] = field(default_factory=dict)
    source_groups: list[dict[str, Any]] = field(default_factory=list)
    ttl_seconds: int = 21600
    packet_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    retrieved_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "packet_id": self.packet_id,
            "trust": "untrusted_data_only",
            "retrieved_at": self.retrieved_at,
            "expires_at": self.retrieved_at + max(60, int(self.ttl_seconds)),
            "partial": bool(self.partial),
            "platform_statuses": dict(self.platform_statuses),
            "items": list(self.items),
            "filtered_counts": dict(self.filtered_counts),
            "warnings": list(dict.fromkeys(clean_text(item, 300) for item in self.warnings if clean_text(item, 300))),
            "aggregation": dict(self.aggregation),
            "source_groups": list(self.source_groups),
        }


def validate_platforms(value: Any) -> list[str]:
    if value is None:
        return list(PLATFORMS)
    if not isinstance(value, list):
        raise ValueError("platforms must be an array")
    result = [str(item) for item in value]
    if not result or len(result) > len(PLATFORMS) or any(item not in PLATFORMS for item in result):
        raise ValueError("platforms contains an unsupported value")
    return list(dict.fromkeys(result))


def json_size_guard(value: Any, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("result exceeds the MCP response limit")
    return text


__all__ = [
    "ContentPacket",
    "DEFAULT_PLATFORM_CONFIG",
    "PLATFORMS",
    "QUALITY_MODES",
    "apply_quality_filter",
    "clean_text",
    "json_size_guard",
    "normalize_url",
    "stable_fingerprint",
    "validate_platforms",
]
