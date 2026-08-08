from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .avatar_relation_evidence import record_avatar_relation_evidence
from .avatar_candidate import sanitize_image
from .evidence_envelope import EvidenceEnvelope
from .media_understanding import (
    analyze_images_with_primary_route_joint_only,
    get_primary_image_route_fingerprint,
)
from .protocol_adapter import get_protocol_adapter
from .safe_image_download import download_public_image
from .user_profile_meta import qq_avatar_url
from .visual_capabilities import VISUAL_ROUTE_VISION


AVATAR_PAIR_ANALYSIS_SCHEMA_VERSION = 1
AVATAR_PAIR_PROMPT_VERSION = 1
AVATAR_PAIR_MAX_CANDIDATES = 6
AVATAR_PAIR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_PAIR_CACHE_MAX_ENTRIES = 256
AVATAR_PAIR_DEFINITE_TTL_SECONDS = 7 * 24 * 60 * 60
AVATAR_PAIR_UNCERTAIN_TTL_SECONDS = 60 * 60
AVATAR_PAIR_FAILURE_TTL_SECONDS = 5 * 60
AVATAR_PAIR_ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}
AVATAR_PAIR_RELATIONS = {
    "near_duplicate",
    "coordinated_pair",
    "same_character",
    "same_series",
    "unrelated",
    "uncertain",
}
AVATAR_PAIR_ASSET_KINDS = {
    "real_person",
    "illustration",
    "acg_character",
    "logo",
    "other",
    "unknown",
}
AVATAR_PAIR_EVIDENCE_TAGS = {
    "exact_bytes",
    "near_identical_composition",
    "shared_layout",
    "complementary_composition",
    "matching_palette",
    "matching_symbols",
    "same_character_features",
    "same_series_style",
    "no_clear_link",
    "real_person_present",
    "insufficient_detail",
}
_MODEL_EVIDENCE_TAGS = AVATAR_PAIR_EVIDENCE_TAGS - {"exact_bytes"}
_PAIR_FIELDS = {"relation", "asset_kinds", "evidence_tags", "confidence"}
_URL_RE = re.compile(r"(?:https?://|www\.|data:)", re.IGNORECASE)

_PAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relation": {"type": "string", "enum": sorted(AVATAR_PAIR_RELATIONS)},
        "asset_kinds": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {"type": "string", "enum": sorted(AVATAR_PAIR_ASSET_KINDS)},
        },
        "evidence_tags": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(_MODEL_EVIDENCE_TAGS)},
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": sorted(_PAIR_FIELDS),
}
_PAIR_PROMPT = """\
联合比较同一条请求中的两张 QQ 头像。第一张和第二张仅代表两个图像资产。
只输出一个严格 JSON object，不要 Markdown、解释或额外字段：
{"relation":"near_duplicate|coordinated_pair|same_character|same_series|unrelated|uncertain","asset_kinds":["real_person|illustration|acg_character|logo|other|unknown","real_person|illustration|acg_character|logo|other|unknown"],"evidence_tags":["允许的枚举标签"],"confidence":0.0}

evidence_tags 只允许：near_identical_composition, shared_layout, complementary_composition,
matching_palette, matching_symbols, same_character_features, same_series_style, no_clear_link,
real_person_present, insufficient_detail。exact_bytes 由代码保留，模型禁止输出。

判定边界：
- coordinated_pair 只表示构图、元素或风格上的图像视觉配套，不能表示两位用户现实中是情侣、朋友、认识或同一人。
- 画中主体不等于使用头像的用户。禁止推断任何真人的身份，禁止做 same-person 判断。
- 任一图片含真人或疑似真人时，asset_kind 使用 real_person，加入 real_person_present，relation 禁止 same_character。
- 证据不足、画面太小或结论未达到高置信阈值时使用 uncertain 和 insufficient_detail。
"""
_PAIR_SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(_PAIR_SCHEMA, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
_PAIR_PROMPT_FINGERPRINT = hashlib.sha256(_PAIR_PROMPT.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Candidate:
    user_id: str
    label: str


@dataclass
class _CacheEntry:
    expires_at: float
    users: frozenset[str]
    assessment: dict[str, Any] | None


_PAIR_CACHE: "OrderedDict[tuple[Any, ...], _CacheEntry]" = OrderedDict()
_PAIR_INFLIGHT: dict[tuple[Any, ...], asyncio.Task[dict[str, Any] | None]] = {}
_PAIR_INFLIGHT_USERS: dict[tuple[Any, ...], frozenset[str]] = {}
_USER_GENERATIONS: dict[str, int] = {}


def _reply_runtime(runtime: Any) -> Any:
    holder = getattr(runtime, "runtime_bundle", None) or runtime
    deps = getattr(holder, "reply_processor_deps", None)
    inner = getattr(deps, "runtime", None) if deps is not None else None
    return inner or holder


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def _normalize_label(value: Any, user_id: str) -> str:
    label = " ".join(str(value or "").strip().split())[:48]
    if not label or _URL_RE.search(label) or label.isdigit():
        return ""
    if len(user_id) >= 4 and user_id in label:
        return ""
    return label


def _normalize_candidates(candidates: Sequence[Mapping[str, Any]] | None) -> list[_Candidate]:
    normalized: list[_Candidate] = []
    seen_users: set[str] = set()
    seen_labels: set[str] = set()
    for raw in list(candidates or [])[:AVATAR_PAIR_MAX_CANDIDATES]:
        if not isinstance(raw, Mapping):
            continue
        user_id = _normalize_id(raw.get("user_id"))
        label = _normalize_label(raw.get("label"), user_id)
        if not user_id or not label or user_id in seen_users or label in seen_labels:
            continue
        seen_users.add(user_id)
        seen_labels.add(label)
        normalized.append(_Candidate(user_id=user_id, label=label))
    return normalized


async def _policy_allows_context(policy_authorizer: Any, user_id: str) -> bool:
    if policy_authorizer is None:
        return True
    try:
        authorization = policy_authorizer(str(user_id or ""))
        if isawaitable(authorization):
            authorization = await authorization
    except Exception:
        return False
    return not bool(getattr(authorization, "blocked", True)) and bool(
        getattr(authorization, "allow_context_read", False)
    )


async def filter_avatar_candidates_by_policy(
    candidates: Sequence[Mapping[str, Any]] | None,
    policy_authorizer: Any,
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for candidate in _normalize_candidates(candidates):
        if await _policy_allows_context(policy_authorizer, candidate.user_id):
            filtered.append({"user_id": candidate.user_id, "label": candidate.label})
    return filtered


def _message_segment_parts(segment: Any) -> tuple[str, Mapping[str, Any]]:
    if isinstance(segment, Mapping):
        segment_type = str(segment.get("type", "") or "").strip().lower()
        data = segment.get("data")
    else:
        segment_type = str(getattr(segment, "type", "") or "").strip().lower()
        data = getattr(segment, "data", None)
    return segment_type, data if isinstance(data, Mapping) else {}


def _profile_label(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("card", "nickname", "sender_name", "speaker", "name"):
            label = " ".join(str(value.get(key, "") or "").strip().split())
            if label and not label.isdigit() and not _URL_RE.search(label):
                return label[:36]
        return ""
    for key in ("card", "nickname", "sender_name", "speaker", "name"):
        label = " ".join(str(getattr(value, key, "") or "").strip().split())
        if label and not label.isdigit() and not _URL_RE.search(label):
            return label[:36]
    return ""


def build_avatar_pair_candidates(
    *,
    event: Any,
    current_user_id: Any,
    current_user_label: Any = "",
    bot_self_id: Any = "",
    batched_events: Sequence[Mapping[str, Any]] | None = None,
    recent_messages: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Build a bounded, label-only candidate list from current group context."""

    if str(getattr(event, "message_type", "") or "").strip().lower() != "group":
        return []
    bot_id = _normalize_id(bot_self_id)
    current_id = _normalize_id(current_user_id)
    name_map: dict[str, str] = {}
    for item in list(batched_events or []) + list(recent_messages or []):
        if not isinstance(item, Mapping):
            continue
        user_id = _normalize_id(item.get("user_id"))
        label = _profile_label(item)
        if user_id and label and user_id != bot_id:
            name_map[user_id] = label

    sender = getattr(event, "sender", None)
    current_label = _profile_label(sender) or _profile_label(
        {"nickname": current_user_label}
    ) or name_map.get(current_id, "")
    if current_id and current_label:
        name_map[current_id] = current_label

    mentioned_ids: list[str] = []
    message = getattr(event, "message", None)
    try:
        segments = list(message or [])
    except TypeError:
        segments = []
    for segment in segments:
        segment_type, data = _message_segment_parts(segment)
        if segment_type != "at":
            continue
        target = _normalize_id(data.get("qq") or data.get("user_id"))
        if target and target != bot_id and target not in mentioned_ids:
            mentioned_ids.append(target)

    reply = getattr(event, "reply", None)
    reply_sender = getattr(reply, "sender", None)
    reply_id = _normalize_id(
        getattr(reply_sender, "user_id", "")
        or (reply_sender.get("user_id") if isinstance(reply_sender, Mapping) else "")
        or getattr(reply, "user_id", "")
        or (reply.get("user_id") if isinstance(reply, Mapping) else "")
        or (reply.get("sender_id") if isinstance(reply, Mapping) else "")
    )
    if reply_id and reply_id != bot_id:
        reply_label = _profile_label(reply_sender) or _profile_label(reply) or name_map.get(reply_id, "")
        if reply_label:
            name_map[reply_id] = reply_label

    ordered: list[tuple[str, str, str]] = []
    if current_id and current_id != bot_id:
        ordered.append((current_id, name_map.get(current_id, ""), "当前发言者"))
    for target in mentioned_ids:
        ordered.append((target, name_map.get(target, ""), "被提及成员"))
    if reply_id and reply_id != bot_id:
        ordered.append((reply_id, name_map.get(reply_id, ""), "被引用成员"))
    for item in reversed(list(batched_events or [])):
        if isinstance(item, Mapping):
            ordered.append((_normalize_id(item.get("user_id")), _profile_label(item), "本轮成员"))
    for item in reversed(list(recent_messages or [])):
        if not isinstance(item, Mapping):
            continue
        source_kind = str(item.get("source_kind", "") or "").strip().lower()
        if bool(item.get("is_bot")) or source_kind in {"bot_reply", "assistant", "system", "plugin"}:
            continue
        ordered.append((_normalize_id(item.get("user_id")), _profile_label(item), "近期成员"))

    candidates: list[dict[str, str]] = []
    seen_users: set[str] = set()
    used_labels: set[str] = set()
    generic_counts: dict[str, int] = {}
    for user_id, raw_label, generic_label in ordered:
        if not user_id or user_id == bot_id or user_id in seen_users:
            continue
        base_label = _normalize_label(raw_label or name_map.get(user_id, ""), user_id)
        if not base_label:
            generic_counts[generic_label] = generic_counts.get(generic_label, 0) + 1
            suffix = generic_counts[generic_label]
            base_label = generic_label if suffix == 1 else f"{generic_label}{suffix}"
        label = base_label
        duplicate_index = 2
        while label in used_labels:
            label = f"{base_label}（成员{duplicate_index}）"
            duplicate_index += 1
        seen_users.add(user_id)
        used_labels.add(label)
        candidates.append({"user_id": user_id, "label": label})
        if len(candidates) >= AVATAR_PAIR_MAX_CANDIDATES:
            break
    return candidates if len(candidates) >= 2 else []


def _group_scope_from_event(event: Any) -> tuple[str, str]:
    if str(getattr(event, "message_type", "") or "").strip().lower() != "group":
        return "", ""
    group_id = _normalize_id(getattr(event, "group_id", ""))
    sender_id = _normalize_id(getattr(event, "user_id", ""))
    if not sender_id:
        getter = getattr(event, "get_user_id", None)
        if callable(getter):
            try:
                sender_id = _normalize_id(getter())
            except Exception:
                sender_id = ""
    return group_id, sender_id


async def get_group_member_info(
    bot: Any,
    group_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    """Adapter-backed membership proof kept as a narrow test seam."""

    result = await get_protocol_adapter(bot).get_group_member_info(
        group_id=group_id,
        user_id=user_id,
    )
    return dict(result.data or {}) if result.ok else None


def normalize_avatar_pair_assessment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PAIR_FIELDS:
        raise ValueError("avatar pair assessment does not match the required schema")
    relation = str(value.get("relation", "") or "").strip().lower()
    if relation not in AVATAR_PAIR_RELATIONS:
        raise ValueError("avatar pair relation is invalid")
    raw_kinds = value.get("asset_kinds")
    if not isinstance(raw_kinds, list) or len(raw_kinds) != 2:
        raise ValueError("avatar pair asset_kinds must contain exactly two items")
    asset_kinds = [str(item or "").strip().lower() for item in raw_kinds]
    if any(item not in AVATAR_PAIR_ASSET_KINDS for item in asset_kinds):
        raise ValueError("avatar pair asset_kind is invalid")
    raw_tags = value.get("evidence_tags")
    if not isinstance(raw_tags, list) or any(not isinstance(item, str) for item in raw_tags):
        raise ValueError("avatar pair evidence_tags must be an array of strings")
    evidence_tags = list(dict.fromkeys(str(item).strip().lower() for item in raw_tags))
    if any(item not in AVATAR_PAIR_EVIDENCE_TAGS for item in evidence_tags):
        raise ValueError("avatar pair evidence_tag is invalid")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("avatar pair confidence must be numeric")
    confidence = float(confidence)
    if not math.isfinite(confidence):
        raise ValueError("avatar pair confidence must be finite")
    confidence = max(0.0, min(confidence, 1.0))

    contains_real_person = "real_person" in asset_kinds
    if contains_real_person and "real_person_present" not in evidence_tags:
        evidence_tags.append("real_person_present")
    requirements: dict[str, tuple[float, set[str]]] = {
        "near_duplicate": (0.96, {"exact_bytes", "near_identical_composition"}),
        "coordinated_pair": (
            0.80,
            {"shared_layout", "complementary_composition", "matching_symbols"},
        ),
        "same_character": (0.88, {"same_character_features"}),
        "same_series": (0.82, {"same_series_style"}),
        "unrelated": (0.80, {"no_clear_link"}),
    }
    threshold, required_evidence = requirements.get(relation, (0.0, set()))
    insufficient = relation != "uncertain" and (
        confidence < threshold or not required_evidence.intersection(evidence_tags)
    )
    if contains_real_person and relation == "same_character":
        insufficient = True
    if insufficient:
        relation = "uncertain"
        confidence = min(confidence, 0.69)
        if "insufficient_detail" not in evidence_tags:
            evidence_tags.append("insufficient_detail")
    return {
        "relation": relation,
        "asset_kinds": asset_kinds,
        "evidence_tags": evidence_tags,
        "confidence": confidence,
    }


def _parse_pair_response(raw: str) -> dict[str, Any]:
    payload = json.loads(str(raw or "").strip())
    if isinstance(payload, dict) and "exact_bytes" in list(payload.get("evidence_tags") or []):
        raise ValueError("exact_bytes is reserved for code-side byte comparison")
    return normalize_avatar_pair_assessment(payload)


def _base_limitations(*, real_person: bool = False, uncertain: bool = False) -> list[str]:
    limitations = [
        "结论只描述两张头像图像本身的视觉关联，头像主体不等于对应用户。",
        "不能据此判断两位用户现实中是情侣、朋友、认识或同一人。",
    ]
    if real_person:
        limitations.append("真人画面不用于现实身份识别或 same-person 判断。")
    if uncertain:
        limitations.append("当前图像证据不足，无法给出更确定的视觉关联结论。")
    return limitations


def _safe_pair_payload(
    left_label: str,
    right_label: str,
    assessment: dict[str, Any] | None,
    *,
    unavailable_summary: str = "这两张头像现在没法一起看清。",
) -> dict[str, Any]:
    del left_label, right_label
    if assessment is None:
        return EvidenceEnvelope(
            allowed_claims=(),
            forbidden_inferences=tuple(
                [
                    *_base_limitations(uncertain=True),
                    "不得提及系统、工具、接口、摘要、安全分析、内部机制或内容策略。",
                ]
            ),
            confidence=0.0,
            natural_fallback=unavailable_summary,
            available=False,
        ).to_dict()
    relation = str(assessment.get("relation", "uncertain") or "uncertain")
    summaries = {
        "near_duplicate": "两张头像的画面相同或近乎相同。",
        "coordinated_pair": "两张头像在构图、元素或风格上呈现视觉配套。",
        "same_character": "两张头像可能描绘同一虚构角色。",
        "same_series": "两张头像可能来自同一作品或视觉系列。",
        "unrelated": "未发现足够可靠的头像视觉关联。",
        "uncertain": "现有图像证据不足以判断头像视觉关联。",
    }
    contains_real = "real_person" in list(assessment.get("asset_kinds") or [])
    summary = summaries.get(relation, summaries["uncertain"])
    return EvidenceEnvelope(
        allowed_claims=(summary,),
        forbidden_inferences=tuple(
            [
                *_base_limitations(
                    real_person=contains_real,
                    uncertain=relation == "uncertain",
                ),
                "不得提及系统、工具、接口、摘要、安全分析、内部机制或内容策略。",
            ]
        ),
        confidence=float(assessment.get("confidence", 0.0) or 0.0),
        natural_fallback=summary,
        available=True,
    ).to_dict()


def _cache_get(key: tuple[Any, ...]) -> tuple[bool, dict[str, Any] | None]:
    entry = _PAIR_CACHE.get(key)
    if entry is None:
        return False, None
    if time.time() >= entry.expires_at:
        _PAIR_CACHE.pop(key, None)
        return False, None
    _PAIR_CACHE.move_to_end(key)
    return True, dict(entry.assessment) if entry.assessment is not None else None


def _cache_set(
    key: tuple[Any, ...],
    *,
    users: frozenset[str],
    assessment: dict[str, Any] | None,
    ttl: int,
) -> None:
    _PAIR_CACHE[key] = _CacheEntry(
        expires_at=time.time() + int(ttl),
        users=users,
        assessment=dict(assessment) if assessment is not None else None,
    )
    _PAIR_CACHE.move_to_end(key)
    while len(_PAIR_CACHE) > AVATAR_PAIR_CACHE_MAX_ENTRIES:
        _PAIR_CACHE.popitem(last=False)


def _generation_snapshot(users: frozenset[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((user_id, _USER_GENERATIONS.get(user_id, 0)) for user_id in users))


def _generation_is_current(snapshot: tuple[tuple[str, int], ...]) -> bool:
    return all(_USER_GENERATIONS.get(user_id, 0) == generation for user_id, generation in snapshot)


async def _download_avatar(
    user_id: str,
    downloader: Callable[..., Awaitable[Any]],
) -> tuple[bytes, str, str]:
    downloaded = await downloader(
        qq_avatar_url(user_id),
        max_bytes=AVATAR_PAIR_MAX_BYTES,
        allowed_mimes=set(AVATAR_PAIR_ALLOWED_MIMES),
    )
    sanitized, mime, _width, _height, _suffix = sanitize_image(
        bytes(downloaded.content),
        str(downloaded.content_type or ""),
    )
    return sanitized, mime, hashlib.sha256(sanitized).hexdigest()


async def analyze_group_user_avatar_pair(
    *,
    runtime: Any,
    bot: Any,
    group_id: str | int,
    sender_user_id: str | int,
    left_user_id: str | int,
    left_label: str,
    right_user_id: str | int,
    right_label: str,
    downloader: Callable[..., Awaitable[Any]] | None = None,
    analyzer: Callable[..., Awaitable[tuple[str, str]]] | None = None,
    membership_getter: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
    policy_authorizer: Any = None,
) -> dict[str, Any]:
    runtime = _reply_runtime(runtime)
    group_key = _normalize_id(group_id)
    sender_key = _normalize_id(sender_user_id)
    left_key = _normalize_id(left_user_id)
    right_key = _normalize_id(right_user_id)
    safe_left_label = _normalize_label(left_label, left_key)
    safe_right_label = _normalize_label(right_label, right_key)
    if (
        bot is None
        or not group_key
        or not sender_key
        or not left_key
        or not right_key
        or left_key == right_key
        or not safe_left_label
        or not safe_right_label
    ):
        return _safe_pair_payload(
            safe_left_label or "候选一",
            safe_right_label or "候选二",
            None,
            unavailable_summary="这两张头像现在没法一起比较。",
        )

    users = frozenset({left_key, right_key})
    for user_id in sorted(users):
        if not await _policy_allows_context(policy_authorizer, user_id):
            return _safe_pair_payload(
                safe_left_label,
                safe_right_label,
                None,
                unavailable_summary="这两张头像现在不适合拿来比较。",
            )
    generation = _generation_snapshot(users)
    prove_membership = membership_getter or get_group_member_info
    for user_id in sorted(users):
        if user_id == sender_key:
            continue
        proof = await prove_membership(bot, group_key, user_id)
        if not proof:
            return _safe_pair_payload(
                safe_left_label,
                safe_right_label,
                None,
                unavailable_summary="我这边确认不了其中一位是不是这个群的。",
            )
    if not _generation_is_current(generation):
        return _safe_pair_payload(safe_left_label, safe_right_label, None)

    download = downloader or download_public_image
    try:
        downloaded = await asyncio.gather(
            _download_avatar(left_key, download),
            _download_avatar(right_key, download),
        )
    except Exception:
        return _safe_pair_payload(
            safe_left_label,
            safe_right_label,
            None,
            unavailable_summary="这两张头像现在有一张看不清。",
        )
    if not _generation_is_current(generation):
        return _safe_pair_payload(safe_left_label, safe_right_label, None)

    records = sorted(
        [
            (left_key, downloaded[0][0], downloaded[0][1], downloaded[0][2]),
            (right_key, downloaded[1][0], downloaded[1][1], downloaded[1][2]),
        ],
        key=lambda item: item[0],
    )
    route_fingerprint = get_primary_image_route_fingerprint(runtime, route_name=VISUAL_ROUTE_VISION)
    pair_identity = tuple((user_id, content_hash) for user_id, _content, _mime, content_hash in records)
    avatar_hashes = {
        user_id: content_hash
        for user_id, _content, _mime, content_hash in records
    }

    async def persist_if_allowed(assessment: dict[str, Any] | None) -> bool:
        if assessment is None:
            return False
        for user_id in sorted(users):
            if not await _policy_allows_context(policy_authorizer, user_id):
                return False
        try:
            await asyncio.to_thread(
                record_avatar_relation_evidence,
                group_id=group_key,
                left_user_id=left_key,
                right_user_id=right_key,
                relation=assessment.get("relation"),
                confidence=assessment.get("confidence"),
                evidence_tags=assessment.get("evidence_tags"),
                asset_kinds=assessment.get("asset_kinds"),
                avatar_hashes=avatar_hashes,
            )
        except Exception:
            pass
        return True
    cache_key = (
        AVATAR_PAIR_ANALYSIS_SCHEMA_VERSION,
        _PAIR_SCHEMA_FINGERPRINT,
        AVATAR_PAIR_PROMPT_VERSION,
        _PAIR_PROMPT_FINGERPRINT,
        route_fingerprint,
        pair_identity,
    )
    hit, cached = _cache_get(cache_key)
    if hit:
        if cached is not None and not await persist_if_allowed(cached):
            return _safe_pair_payload(
                safe_left_label,
                safe_right_label,
                None,
                unavailable_summary="这两张头像现在不适合拿来比较。",
            )
        return _safe_pair_payload(safe_left_label, safe_right_label, cached)

    async def compute() -> dict[str, Any] | None:
        if records[0][1] == records[1][1]:
            assessment = normalize_avatar_pair_assessment(
                {
                    "relation": "near_duplicate",
                    "asset_kinds": ["unknown", "unknown"],
                    "evidence_tags": ["exact_bytes"],
                    "confidence": 1.0,
                }
            )
        else:
            data_urls = [
                f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
                for _user_id, content, mime, _content_hash in records
            ]
            analyze = analyzer or analyze_images_with_primary_route_joint_only
            try:
                raw, route = await analyze(
                    runtime=runtime,
                    prompt=_PAIR_PROMPT,
                    image_refs=data_urls,
                    route_name=VISUAL_ROUTE_VISION,
                    image_detail="low",
                )
                if route == "joint_vision_unavailable" or not str(raw or "").strip():
                    raise ValueError("joint vision unavailable")
                assessment = _parse_pair_response(raw)
            except Exception:
                if _generation_is_current(generation):
                    _cache_set(
                        cache_key,
                        users=users,
                        assessment=None,
                        ttl=AVATAR_PAIR_FAILURE_TTL_SECONDS,
                    )
                return None
            finally:
                del data_urls
        if not _generation_is_current(generation):
            return None
        ttl = (
            AVATAR_PAIR_UNCERTAIN_TTL_SECONDS
            if assessment.get("relation") == "uncertain"
            else AVATAR_PAIR_DEFINITE_TTL_SECONDS
        )
        _cache_set(cache_key, users=users, assessment=assessment, ttl=ttl)
        return assessment

    task = _PAIR_INFLIGHT.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(compute())
        _PAIR_INFLIGHT[cache_key] = task
        _PAIR_INFLIGHT_USERS[cache_key] = users

        def done(completed: asyncio.Task[dict[str, Any] | None]) -> None:
            if _PAIR_INFLIGHT.get(cache_key) is completed:
                _PAIR_INFLIGHT.pop(cache_key, None)
                _PAIR_INFLIGHT_USERS.pop(cache_key, None)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done)
    try:
        assessment = await asyncio.shield(task)
    except asyncio.CancelledError:
        if _generation_is_current(generation):
            raise
        assessment = None
    if not _generation_is_current(generation):
        assessment = None
    if assessment is not None and not await persist_if_allowed(assessment):
        assessment = None
    return _safe_pair_payload(safe_left_label, safe_right_label, assessment)


def clear_user_avatar_pair_analysis(user_id: str | int) -> int:
    uid = _normalize_id(user_id)
    if not uid:
        return 0
    _USER_GENERATIONS[uid] = _USER_GENERATIONS.get(uid, 0) + 1
    removed = 0
    for key, entry in list(_PAIR_CACHE.items()):
        if uid in entry.users:
            _PAIR_CACHE.pop(key, None)
            removed += 1
    for key, task in list(_PAIR_INFLIGHT.items()):
        if uid not in _PAIR_INFLIGHT_USERS.get(key, frozenset()):
            continue
        if not task.done():
            task.cancel()
        removed += 1
    try:
        from .avatar_relation_evidence import delete_user_avatar_relation_evidence

        removed += delete_user_avatar_relation_evidence(uid)
    except Exception:
        pass
    return removed


def build_group_user_avatar_pair_insight_tool(
    *,
    runtime: Any,
    bot: Any,
    event: Any,
    candidates: Sequence[Mapping[str, Any]],
    policy_authorizer: Any = None,
) -> Any | None:
    from ..agent.tool_registry import AgentTool

    group_key, sender_key = _group_scope_from_event(event)
    normalized = _normalize_candidates(candidates)
    if bot is None or not group_key or not sender_key or len(normalized) < 2:
        return None
    by_label = {candidate.label: candidate for candidate in normalized}
    labels = list(by_label)
    used = False

    async def handler(left_label: str = "", right_label: str = "", **extra: Any) -> str:
        nonlocal used
        left = by_label.get(str(left_label or "").strip())
        right = by_label.get(str(right_label or "").strip())
        if extra or left is None or right is None or left.label == right.label or left.user_id == right.user_id:
            payload = _safe_pair_payload(
                left.label if left is not None else "候选一",
                right.label if right is not None else "候选二",
                None,
                unavailable_summary="这两个名字现在对不上，没法比较头像。",
            )
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if used:
            payload = _safe_pair_payload(
                left.label,
                right.label,
                None,
                unavailable_summary="这轮先看刚才那一组头像。",
            )
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        used = True
        try:
            payload = await analyze_group_user_avatar_pair(
                runtime=runtime,
                bot=bot,
                group_id=group_key,
                sender_user_id=sender_key,
                left_user_id=left.user_id,
                left_label=left.label,
                right_user_id=right.user_id,
                right_label=right.label,
                policy_authorizer=policy_authorizer,
            )
        except Exception:
            payload = _safe_pair_payload(left.label, right.label, None)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return AgentTool(
        name="inspect_group_user_avatar_pair",
        description=(
            "只读联合比较当前群内两个候选成员的 QQ 头像图像是否近重复、视觉配套、可能是同一虚构角色或同一系列。"
            "仅在当前对话确实要求比较这两张头像时调用，每轮最多一次；参数只能选择 schema 给出的两个不同 label。"
            "coordinated_pair 仅表示头像画面视觉配套，绝不能推断两位用户现实中是情侣、朋友、认识或同一人，"
            "也不能用真人头像做身份或 same-person 判断。"
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "left_label": {"type": "string", "enum": labels},
                "right_label": {"type": "string", "enum": labels},
            },
            "required": ["left_label", "right_label"],
        },
        handler=handler,
        local=True,
        metadata={
            "intent_tags": ["avatar", "lookup", "banter"],
            "evidence_kind": "visual_summary",
            "requires_network": True,
            "requires_image": False,
            "latency_class": "slow",
            "risk_level": "low",
            "read_only": True,
            "side_effect": "none",
            "final_behavior": "constrained_persona_output",
            "retryable": True,
            "source_kind": "first_party_runtime",
        },
        per_session_quota=1,
    )


def register_group_user_avatar_pair_insight_tool(
    registry: Any,
    *,
    runtime: Any,
    bot: Any,
    event: Any,
    candidates: Sequence[Mapping[str, Any]],
    policy_authorizer: Any = None,
) -> bool:
    if registry is None:
        return False
    tool = build_group_user_avatar_pair_insight_tool(
        runtime=runtime,
        bot=bot,
        event=event,
        candidates=candidates,
        policy_authorizer=policy_authorizer,
    )
    if tool is None:
        return False
    registry.register(tool)
    return True


def _clear_pair_cache_for_testing() -> None:
    for task in list(_PAIR_INFLIGHT.values()):
        if not task.done():
            task.cancel()
    _PAIR_CACHE.clear()
    _PAIR_INFLIGHT.clear()
    _PAIR_INFLIGHT_USERS.clear()
    _USER_GENERATIONS.clear()


__all__ = [
    "AVATAR_PAIR_ANALYSIS_SCHEMA_VERSION",
    "AVATAR_PAIR_ALLOWED_MIMES",
    "AVATAR_PAIR_EVIDENCE_TAGS",
    "AVATAR_PAIR_MAX_BYTES",
    "AVATAR_PAIR_RELATIONS",
    "analyze_group_user_avatar_pair",
    "build_avatar_pair_candidates",
    "build_group_user_avatar_pair_insight_tool",
    "clear_user_avatar_pair_analysis",
    "filter_avatar_candidates_by_policy",
    "normalize_avatar_pair_assessment",
    "register_group_user_avatar_pair_insight_tool",
]
