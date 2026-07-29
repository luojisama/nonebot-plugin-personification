from __future__ import annotations

import asyncio
import base64
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable


MIN_CHARACTER_CONFIDENCE = 0.85
MIN_PORTRAIT_QUALITY = 0.45
TARGET_VERIFIED_CANDIDATES = 12
MAX_VISUAL_REVIEWS = 60
REVIEW_BATCH_SIZE = 8
VISUAL_REVIEW_TIMEOUT_SECONDS = 45.0

VisualReviewer = Callable[[str, str], Awaitable[tuple[str, str]]]

_NOISE_HINTS = (
    "声优", "配音", "演员", "真人", "cosplay", "coser", "logo", "封面",
    "海报", "角色列表", "人物列表", "cast", "voice actor", "seiyuu",
)
_TRUSTED_SOURCE_HINTS = ("official", "官网", "官方", "wiki", "萌娘", "fandom", "百科")


def _clip(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _normalized(value: Any) -> str:
    return re.sub(r"[\s\-_/|:：,，。！？!?（）()【】\[\]<>]+", "", str(value or "").lower())


def _aliases(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        normalized = _normalized(value)
        if normalized and normalized not in output:
            output.append(normalized)
    return output


def avatar_text_relevance(
    candidate: dict[str, Any],
    *,
    work_aliases: Iterable[str],
    character_aliases: Iterable[str],
) -> float:
    title = _normalized(candidate.get("title"))
    page = _normalized(candidate.get("page_url"))
    source = _normalized(candidate.get("source"))
    evidence = f"{title}{page}{source}"
    work_keys = _aliases(work_aliases)
    character_keys = _aliases(character_aliases)
    title_character = any(key in title for key in character_keys)
    evidence_character = any(key in evidence for key in character_keys)
    title_work = any(key in title for key in work_keys)
    evidence_work = any(key in evidence for key in work_keys)

    score = 0.05
    if title_character and title_work:
        score = 0.95
    elif title_character and evidence_work:
        score = 0.85
    elif title_character:
        score = 0.72
    elif evidence_character and evidence_work:
        score = 0.62
    elif evidence_character:
        score = 0.42
    elif evidence_work:
        score = 0.20

    if any(_normalized(hint) in evidence for hint in _NOISE_HINTS):
        score -= 0.40
    if any(_normalized(hint) in source for hint in _TRUSTED_SOURCE_HINTS):
        score += 0.05
    return round(max(0.0, min(1.0, score)), 4)


def _extract_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(text[start : end + 1])
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {}


def _strict_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _score(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return number


def normalize_avatar_visual_review(raw: str, *, route: str = "") -> dict[str, Any]:
    payload = _extract_json(raw)
    target_match = str(payload.get("target_match") or "").strip().lower()
    confidence = _score(payload, "character_confidence")
    portrait_quality = _score(payload, "portrait_quality")
    single_subject = _strict_bool(payload, "single_subject")
    real_person = _strict_bool(payload, "is_cosplay_or_real_person")
    logo_or_cover = _strict_bool(payload, "is_logo_cover_or_ui")
    content_safe = _strict_bool(payload, "content_safe")
    contradictions = payload.get("contradictions")
    valid = (
        target_match in {"yes", "no", "uncertain"}
        and confidence is not None
        and portrait_quality is not None
        and single_subject is not None
        and real_person is not None
        and logo_or_cover is not None
        and content_safe is not None
        and isinstance(contradictions, list)
    )
    if not str(raw or "").strip() or route in {"vision_unavailable", "missing_images"}:
        status = "unavailable"
    elif not valid:
        status = "invalid_response"
    elif (
        target_match == "yes"
        and confidence >= MIN_CHARACTER_CONFIDENCE
        and portrait_quality >= MIN_PORTRAIT_QUALITY
        and single_subject
        and not real_person
        and not logo_or_cover
        and content_safe
        and not contradictions
    ):
        status = "verified"
    elif target_match == "uncertain":
        status = "uncertain"
    else:
        status = "rejected"
    return {
        "vision_status": status,
        "vision_route": str(route or ""),
        "target_match": target_match if valid else "",
        "recognized_identity": _clip(payload.get("recognized_identity"), 120),
        "character_confidence": round(confidence or 0.0, 4),
        "portrait_quality": round(portrait_quality or 0.0, 4),
        "single_subject": bool(single_subject) if single_subject is not None else False,
        "is_cosplay_or_real_person": bool(real_person) if real_person is not None else False,
        "is_logo_cover_or_ui": bool(logo_or_cover) if logo_or_cover is not None else False,
        "content_safe": bool(content_safe) if content_safe is not None else False,
        "contradictions": [_clip(item, 120) for item in list(contradictions or [])[:5]],
        "review_reason": _clip(payload.get("reason")),
    }


def _source_trust(candidate: dict[str, Any]) -> float:
    source = _normalized(candidate.get("source"))
    return 1.0 if any(_normalized(hint) in source for hint in _TRUSTED_SOURCE_HINTS) else 0.45


def _final_score(candidate: dict[str, Any]) -> float:
    return round(
        float(candidate.get("character_confidence", 0) or 0) * 0.50
        + float(candidate.get("portrait_quality", 0) or 0) * 0.20
        + _source_trust(candidate) * 0.15
        + float(candidate.get("text_score", 0) or 0) * 0.10
        + float(candidate.get("aspect_score", candidate.get("fit_score", 0)) or 0) * 0.05,
        4,
    )


def _review_prompt(*, work_title: str, character_name: str, aliases: dict[str, list[str]]) -> str:
    schema = {
        "target_match": "yes|no|uncertain",
        "recognized_identity": "识别到的角色或空",
        "character_confidence": 0.0,
        "portrait_quality": 0.0,
        "single_subject": True,
        "is_cosplay_or_real_person": False,
        "is_logo_cover_or_ui": False,
        "content_safe": True,
        "contradictions": [],
        "reason": "简短依据",
    }
    return f"""你是角色头像候选审核器。判断图片主体是否明确为指定虚构角色。
作品：{work_title}
角色：{character_name}
作品别名：{json.dumps(list(aliases.get('work_aliases') or [])[:8], ensure_ascii=False)}
角色别名：{json.dumps(list(aliases.get('character_aliases') or [])[:10], ensure_ascii=False)}

允许官方图、动画截图和可明确识别的同人图。拒绝声优/演员、真人、cosplay、Logo、作品封面、UI、多人主体和其他角色。
无法可靠确认时必须 target_match=uncertain，不得根据文件名或提问暗示猜测。
只输出符合此结构的 JSON：{json.dumps(schema, ensure_ascii=False)}"""


def _data_url(path: Path, mime: str) -> str:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime or 'image/jpeg'};base64,{payload}"


async def review_avatar_candidates(
    *,
    runtime: Any,
    candidates: list[dict[str, Any]],
    work_title: str,
    character_name: str,
    aliases: dict[str, list[str]],
    candidate_path: Callable[[dict[str, Any]], Path],
    reviewer: VisualReviewer | None = None,
    target_verified: int = TARGET_VERIFIED_CANDIDATES,
    max_reviews: int = MAX_VISUAL_REVIEWS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    work_aliases = list(aliases.get("work_aliases") or [work_title])
    character_aliases = list(aliases.get("character_aliases") or [character_name])
    prepared: list[dict[str, Any]] = []
    for item in candidates:
        candidate = dict(item)
        candidate["text_score"] = avatar_text_relevance(
            candidate,
            work_aliases=work_aliases,
            character_aliases=character_aliases,
        )
        candidate["text_status"] = "selected"
        candidate.update(normalize_avatar_visual_review("", route="not_reviewed"))
        prepared.append(candidate)
    prepared.sort(key=lambda item: (float(item["text_score"]), float(item.get("aspect_score", 0))), reverse=True)
    selected = prepared[: max(0, int(max_reviews))]
    prompt = _review_prompt(work_title=work_title, character_name=character_name, aliases=aliases)

    async def default_reviewer(review_prompt: str, image_ref: str) -> tuple[str, str]:
        from .media_understanding import analyze_images_with_route_or_fallback
        from .visual_capabilities import VISUAL_ROUTE_VISION

        return await analyze_images_with_route_or_fallback(
            runtime=runtime,
            prompt=review_prompt,
            image_refs=[image_ref],
            route_name=VISUAL_ROUTE_VISION,
            image_detail="high",
            fallback_vision_caller=getattr(runtime, "vision_caller", None),
        )

    effective_reviewer = reviewer or default_reviewer
    verified = 0
    reviewed = 0
    cursor = 0
    required_verified = max(10, int(target_verified))
    while cursor < len(selected):
        if verified >= required_verified:
            break
        remaining = required_verified - verified
        batch_size = min(REVIEW_BATCH_SIZE, remaining, len(selected) - cursor)
        batch = selected[cursor : cursor + batch_size]
        cursor += batch_size

        async def one(candidate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            try:
                raw, route = await asyncio.wait_for(
                    effective_reviewer(
                        prompt,
                        _data_url(candidate_path(candidate), str(candidate.get("mime") or "image/jpeg")),
                    ),
                    timeout=VISUAL_REVIEW_TIMEOUT_SECONDS,
                )
                return candidate, normalize_avatar_visual_review(raw, route=route)
            except Exception as exc:
                return candidate, {
                    **normalize_avatar_visual_review("", route="error"),
                    "vision_status": "error",
                    "review_reason": _clip(exc),
                }

        results = await asyncio.gather(*(one(candidate) for candidate in batch))
        for candidate, review in results:
            candidate.update(review)
            candidate["fit_score"] = _final_score(candidate) if review["vision_status"] == "verified" else 0.0
            reviewed += 1
            if review["vision_status"] == "verified":
                verified += 1

    prepared.sort(
        key=lambda item: (
            item.get("vision_status") == "verified",
            float(item.get("fit_score", 0) or 0),
            float(item.get("text_score", 0) or 0),
        ),
        reverse=True,
    )
    status_counts = Counter(str(item.get("vision_status") or "not_reviewed") for item in prepared)
    usable_reviews = sum(status_counts.get(status, 0) for status in ("verified", "rejected", "uncertain"))
    summary = {
        "safe_count": len(prepared),
        "reviewed_count": reviewed,
        "verified_count": status_counts.get("verified", 0),
        "status_counts": dict(status_counts),
        "vision_available": usable_reviews > 0,
    }
    return prepared, summary


__all__ = [
    "MAX_VISUAL_REVIEWS",
    "MIN_CHARACTER_CONFIDENCE",
    "TARGET_VERIFIED_CANDIDATES",
    "avatar_text_relevance",
    "normalize_avatar_visual_review",
    "review_avatar_candidates",
]
