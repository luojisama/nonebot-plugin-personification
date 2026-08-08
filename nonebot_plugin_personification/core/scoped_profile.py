from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .db import get_db_path


SCHEMA_VERSION = 2
MAX_CLAIM_VALUE_CHARS = 200
MAX_CLAIM_EVIDENCE_REFS = 32
MAX_EVIDENCE_WINDOWS = 32
MAX_EVIDENCE_SIDE = 5
MAX_EVIDENCE_ROW_DISTANCE = 64
MAX_EVIDENCE_TIME_DISTANCE_SECONDS = 6 * 60 * 60
MAX_MENTION_DISTANCE_SECONDS = 15 * 60
MAX_SAME_THREAD_DISTANCE_SECONDS = 30 * 60

GROUP_CONTEXTUAL_KEYS = frozenset(
    {
        "nickname_pref",
        "communication_style",
        "social_mode",
        "relationship",
        "recent_focus",
        "content_pref",
        "interaction_advice",
        "group_role",
    }
)
CLAIM_CLASS_ALLOWLIST = frozenset({"stable", "contextual"})
CLAIM_SOURCE_ALLOWLIST = frozenset(
    {
        "user_confirmed",
        "legacy_structured",
        "global_generated",
        "group_generated",
        "evidence_derived",
        "imported",
    }
)
EVIDENCE_RELATION_ALLOWLIST = frozenset({"anchor", "reply", "mention", "same_thread"})
GENERATION_STATUS_ALLOWLIST = frozenset(
    {
        "not_started",
        "idle",
        "pending",
        "running",
        "completed",
        "success",
        "failed",
        "error",
        "skipped",
        "stale",
    }
)

_SOURCE_PRIORITY = {
    "user_confirmed": 0,
    "legacy_structured": 1,
    "imported": 1,
    "evidence_derived": 1,
    "global_generated": 1,
    "group_generated": 1,
}
_RELATION_PRIORITY = {"reply": 0, "mention": 1, "same_thread": 2, "anchor": 3}
_REF_RETENTION_PRIORITY = {"anchor": 0, "reply": 1, "mention": 2, "same_thread": 3}
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CLAIM_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_LEGACY_KEY_ALIASES = {
    "职业": "occupation",
    "职业推测": "occupation",
    "年龄": "age_group",
    "年龄推测": "age_group",
    "性别": "gender",
    "性别推测": "gender",
    "作息": "routine",
    "作息特征": "routine",
    "兴趣": "interests",
    "兴趣领域": "interests",
    "沟通风格": "communication_style",
    "说话风格": "communication_style",
    "情绪基线": "emotion_baseline",
    "社交模式": "social_mode",
    "知识结构": "knowledge",
    "称呼": "nickname_pref",
    "昵称": "nickname_pref",
    "称呼与昵称": "nickname_pref",
    "关系": "relationship",
    "关系与亲密度": "relationship",
    "雷区": "taboos",
    "禁忌": "taboos",
    "雷区与禁忌": "taboos",
    "记忆锚点": "memory_anchors",
    "近期关注": "recent_focus",
    "内容偏好": "content_pref",
    "互动建议": "interaction_advice",
    "人物描述": "portrait",
    "群角色": "group_role",
}
_LEGACY_UNKNOWN_VALUES = frozenset({"信息不足", "未知", "不明"})


class ProfileEvidenceError(ValueError):
    """The requested anchor is not safe profile evidence."""


@dataclass(frozen=True, slots=True)
class ProfileEvidenceMessage:
    row_id: int
    message_id: str
    group_id: str
    user_id: str
    actor: str
    relation: str
    timestamp: float
    content: str

    def to_ref(self) -> dict[str, Any]:
        """Return the persistable reference; raw content deliberately stays in memory."""
        return {
            "row_id": self.row_id,
            "message_id": self.message_id,
            "relation": self.relation,
            "timestamp": self.timestamp,
            "content_sha256": hashlib.sha256(self.content.encode("utf-8")).hexdigest(),
        }


@dataclass(frozen=True, slots=True)
class ProfileEvidenceWindow:
    anchor: ProfileEvidenceMessage
    before: tuple[ProfileEvidenceMessage, ...] = ()
    after: tuple[ProfileEvidenceMessage, ...] = ()

    @property
    def messages(self) -> tuple[ProfileEvidenceMessage, ...]:
        return (*self.before, self.anchor, *self.after)

    def to_refs(self) -> dict[str, Any]:
        return {
            "before": [message.to_ref() for message in self.before],
            "anchor": self.anchor.to_ref(),
            "after": [message.to_ref() for message in self.after],
        }


# Short aliases keep the selector API convenient without changing the persisted schema.
EvidenceMessage = ProfileEvidenceMessage
EvidenceWindow = ProfileEvidenceWindow


def _normalize_identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be a string or integer identifier")
    normalized = str(value).strip()
    if not normalized or not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid {field}")
    return normalized


def _optional_identifier(value: Any) -> str:
    try:
        return _normalize_identifier(value, "identifier")
    except ValueError:
        return ""


def _normalize_claim_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    alias = _LEGACY_KEY_ALIASES.get(raw)
    if alias:
        return alias
    normalized = raw.lower().replace("-", "_").replace(" ", "_")
    return normalized if _CLAIM_KEY_RE.fullmatch(normalized) else ""


def _bounded_text(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return " ".join(str(value).split())[:limit]


def _non_negative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _normalize_confidence(value: Any, *, source: str) -> float:
    if source == "user_confirmed":
        return 1.0
    result = _finite_float(value, 0.5)
    return round(min(1.0, max(0.0, result)), 6)


def _normalize_evidence_ref(value: Any) -> dict[str, Any] | None:
    if isinstance(value, ProfileEvidenceMessage):
        raw: Mapping[str, Any] = value.to_ref()
    elif isinstance(value, Mapping):
        raw = value
    else:
        return None

    row_id = _non_negative_int(raw.get("row_id"), 0)
    relation = str(raw.get("relation", "") or "").strip().lower()
    if row_id <= 0 or relation not in EVIDENCE_RELATION_ALLOWLIST:
        return None
    message_id = _bounded_text(raw.get("message_id", ""), 256)
    timestamp = _finite_float(raw.get("timestamp"), 0.0)
    digest = str(raw.get("content_sha256", "") or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        content = raw.get("content")
        if not isinstance(content, str):
            return None
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "row_id": row_id,
        "message_id": message_id,
        "relation": relation,
        "timestamp": timestamp,
        "content_sha256": digest,
    }


def _normalize_ref_list(values: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    by_row: dict[int, dict[str, Any]] = {}
    for value in values:
        ref = _normalize_evidence_ref(value)
        if ref is None:
            continue
        existing = by_row.get(ref["row_id"])
        if existing is None or (
            _RELATION_PRIORITY[ref["relation"]],
            ref["timestamp"],
            ref["message_id"],
        ) < (
            _RELATION_PRIORITY[existing["relation"]],
            existing["timestamp"],
            existing["message_id"],
        ):
            by_row[ref["row_id"]] = ref
    retained = sorted(
        by_row.values(),
        key=lambda ref: (
            _REF_RETENTION_PRIORITY[ref["relation"]],
            -ref["timestamp"],
            -ref["row_id"],
        ),
    )[:limit]
    return sorted(retained, key=lambda ref: (ref["timestamp"], ref["row_id"]))


def _normalize_evidence_windows(values: Any) -> list[dict[str, Any]]:
    if isinstance(values, ProfileEvidenceWindow):
        candidates: Sequence[Any] = [values]
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        candidates = values
    else:
        return []

    by_anchor: dict[int, dict[str, Any]] = {}
    for value in candidates:
        if isinstance(value, ProfileEvidenceWindow):
            raw: Mapping[str, Any] = value.to_refs()
        elif isinstance(value, Mapping):
            raw = value
        else:
            continue
        anchor = _normalize_evidence_ref(raw.get("anchor"))
        if anchor is None:
            continue
        window = {
            "before": _normalize_ref_list(raw.get("before", []), limit=MAX_EVIDENCE_SIDE),
            "anchor": anchor,
            "after": _normalize_ref_list(raw.get("after", []), limit=MAX_EVIDENCE_SIDE),
        }
        previous = by_anchor.get(anchor["row_id"])
        if previous is None or _canonical_json(window) < _canonical_json(previous):
            by_anchor[anchor["row_id"]] = window
    ordered = sorted(
        by_anchor.values(),
        key=lambda window: (window["anchor"]["timestamp"], window["anchor"]["row_id"]),
    )
    return ordered[-MAX_EVIDENCE_WINDOWS:]


def _iter_claims(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping):
                yield {**item, "key": key}
            else:
                yield {"key": key, "value": item}
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping):
                yield item


def _claims_with_default_source(value: Any, source: str) -> Iterable[Mapping[str, Any]]:
    for item in _iter_claims(value):
        claim = dict(item)
        if not str(claim.get("source", "") or "").strip():
            claim["source"] = source
        yield claim


def _normalize_claim(value: Mapping[str, Any], *, scope_kind: str) -> dict[str, Any] | None:
    key = _normalize_claim_key(value.get("key"))
    if not key or (scope_kind == "group" and key not in GROUP_CONTEXTUAL_KEYS):
        return None
    claim_value = _bounded_text(value.get("value"), MAX_CLAIM_VALUE_CHARS)
    if not claim_value or claim_value in _LEGACY_UNKNOWN_VALUES:
        return None
    source = str(value.get("source", "") or "").strip().lower()
    if source not in CLAIM_SOURCE_ALLOWLIST:
        return None
    return {
        "key": key,
        "value": claim_value,
        "class": "stable" if scope_kind == "global" else "contextual",
        "source": source,
        "confidence": _normalize_confidence(value.get("confidence"), source=source),
        "evidence_refs": _normalize_ref_list(
            value.get("evidence_refs", []),
            limit=MAX_CLAIM_EVIDENCE_REFS,
        ),
    }


def _claim_preference(claim: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _SOURCE_PRIORITY.get(str(claim.get("source", "")), 99),
        -float(claim.get("confidence", 0.0) or 0.0),
        str(claim.get("source", "")),
        str(claim.get("value", "")),
        _canonical_json(claim.get("evidence_refs", [])),
    )


def _normalize_claims(value: Any, *, scope_kind: str) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for raw_claim in _iter_claims(value):
        claim = _normalize_claim(raw_claim, scope_kind=scope_kind)
        if claim is None:
            continue
        previous = by_key.get(claim["key"])
        if previous is None or _claim_preference(claim) < _claim_preference(previous):
            by_key[claim["key"]] = claim
    return [by_key[key] for key in sorted(by_key)]


def _normalize_base(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    digest = str(raw.get("digest", "") or "").strip().lower()
    return {
        "global_revision": _non_negative_int(raw.get("global_revision"), 0),
        "digest": digest if _SHA256_RE.fullmatch(digest) else "",
    }


def _normalize_generation(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    status = str(raw.get("status", "idle") or "idle").strip().lower()
    if status not in GENERATION_STATUS_ALLOWLIST:
        status = "idle"
    return {
        "last_processed_group_message_row_id": _non_negative_int(
            raw.get("last_processed_group_message_row_id"),
            0,
        ),
        "status": status,
        "generated_at": _finite_float(raw.get("generated_at"), 0.0),
    }


def _scope_from_document(document: Mapping[str, Any]) -> tuple[str, str]:
    raw_scope = document.get("scope", "global")
    if isinstance(raw_scope, Mapping):
        kind = str(raw_scope.get("kind", "") or "").strip().lower()
        raw_group_id = raw_scope.get("group_id", document.get("group_id", ""))
    else:
        kind = str(raw_scope or "").strip().lower()
        raw_group_id = document.get("group_id", "")
    if kind not in {"global", "group"}:
        raise ValueError("scope.kind must be global or group")
    group_id = _normalize_identifier(raw_group_id, "group_id") if kind == "group" else ""
    return kind, group_id


def _normalize_v2_document(document: Mapping[str, Any]) -> dict[str, Any]:
    scope_kind, group_id = _scope_from_document(document)
    scope: dict[str, Any] = {"kind": scope_kind}
    if scope_kind == "group":
        scope["group_id"] = group_id
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": _non_negative_int(document.get("revision"), 0),
        "scope": scope,
        "base": _normalize_base(document.get("base")),
        "claims": _normalize_claims(document.get("claims", []), scope_kind=scope_kind),
        "evidence_windows": _normalize_evidence_windows(document.get("evidence_windows", [])),
        "generation": _normalize_generation(document.get("generation")),
    }


def _legacy_profile_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = value.get("profile_json")
    return nested if isinstance(nested, Mapping) else value


def _legacy_claims(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    profile = _legacy_profile_mapping(value)
    result: list[dict[str, Any]] = []
    structured = profile.get("structured")
    if isinstance(structured, Mapping):
        for key, claim_value in structured.items():
            result.append(
                {
                    "key": key,
                    "value": claim_value,
                    "class": "stable",
                    "source": "legacy_structured",
                    "confidence": 0.5,
                    "evidence_refs": [],
                }
            )
    corrections = profile.get("user_corrections")
    if isinstance(corrections, Mapping):
        for key, claim_value in corrections.items():
            result.append(
                {
                    "key": key,
                    "value": claim_value,
                    "class": "stable",
                    "source": "user_confirmed",
                    "confidence": 1.0,
                    "evidence_refs": [],
                }
            )
    return result


def build_global_profile_document(
    legacy_profile_json: Mapping[str, Any] | None = None,
    claims: Any = None,
    *,
    revision: Any | None = None,
    base: Mapping[str, Any] | None = None,
    evidence_windows: Any = None,
    generation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema-v2 global document and deterministically upgrade legacy fields."""
    legacy = legacy_profile_json if isinstance(legacy_profile_json, Mapping) else {}
    raw_claims: list[Mapping[str, Any]] = []
    if legacy.get("schema_version") == SCHEMA_VERSION and "claims" in legacy:
        raw_claims.extend(_iter_claims(legacy.get("claims")))
    raw_claims.extend(_legacy_claims(legacy))
    raw_claims.extend(_claims_with_default_source(claims, "global_generated"))
    document = {
        "schema_version": SCHEMA_VERSION,
        "revision": legacy.get("revision", 0) if revision is None else revision,
        "scope": {"kind": "global"},
        "base": legacy.get("base", {}) if base is None else base,
        "claims": raw_claims,
        "evidence_windows": legacy.get("evidence_windows", []) if evidence_windows is None else evidence_windows,
        "generation": legacy.get("generation", {}) if generation is None else generation,
    }
    return _normalize_v2_document(document)


def profile_document_digest(document: Mapping[str, Any]) -> str:
    normalized = normalize(document)
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def build_group_profile_document(
    group_id: Any,
    claims: Any = None,
    *,
    global_document: Mapping[str, Any] | None = None,
    revision: Any = 0,
    base: Mapping[str, Any] | None = None,
    evidence_windows: Any = None,
    generation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a group document containing only the allowed contextual claim keys."""
    normalized_group_id = _normalize_identifier(group_id, "group_id")
    normalized_base: Mapping[str, Any] = base or {}
    if global_document is not None:
        global_profile = normalize(global_document)
        if global_profile["scope"]["kind"] != "global":
            raise ValueError("global_document must have global scope")
        normalized_base = {
            "global_revision": global_profile["revision"],
            "digest": profile_document_digest(global_profile),
        }
    document = {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "scope": {"kind": "group", "group_id": normalized_group_id},
        "base": normalized_base,
        "claims": list(_claims_with_default_source(claims, "group_generated")),
        "evidence_windows": evidence_windows or [],
        "generation": generation or {},
    }
    return _normalize_v2_document(document)


def normalize(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical schema-v2 representation of a profile document."""
    if not isinstance(document, Mapping):
        raise TypeError("profile document must be a mapping")
    if "scope" not in document and (
        isinstance(document.get("structured"), Mapping)
        or isinstance(document.get("user_corrections"), Mapping)
        or isinstance(document.get("profile_json"), Mapping)
    ):
        return build_global_profile_document(document)
    return _normalize_v2_document(document)


normalize_profile_document = normalize


def effective(
    global_document: Mapping[str, Any],
    group_document: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Merge claims; confirmed globals cannot be shadowed by group context."""
    global_profile = normalize(global_document)
    if global_profile["scope"]["kind"] != "global":
        raise ValueError("global_document must have global scope")
    merged = {claim["key"]: dict(claim) for claim in global_profile["claims"]}
    if group_document is None:
        return [merged[key] for key in sorted(merged)]

    group_profile = normalize(group_document)
    if group_profile["scope"]["kind"] != "group":
        raise ValueError("group_document must have group scope")
    for claim in group_profile["claims"]:
        key = claim["key"]
        if key not in GROUP_CONTEXTUAL_KEYS:
            continue
        global_claim = merged.get(key)
        if global_claim is not None and global_claim["source"] == "user_confirmed":
            continue
        merged[key] = dict(claim)
    return [merged[key] for key in sorted(merged)]


effective_profile_claims = effective


def render(
    document: Mapping[str, Any],
    group_document: Mapping[str, Any] | None = None,
) -> str:
    """Render claims in canonical key order with fixed metadata formatting."""
    profile = normalize(document)
    if group_document is None:
        claims = profile["claims"]
        scope = profile["scope"]["kind"]
        if scope == "group":
            scope = f"group:{profile['scope']['group_id']}"
        header = f"profile scope={scope} revision={profile['revision']}"
    else:
        group_profile = normalize(group_document)
        if group_profile["scope"]["kind"] != "group":
            raise ValueError("group_document must have group scope")
        claims = effective(profile, group_profile)
        header = (
            f"profile scope=effective:{group_profile['scope']['group_id']} "
            f"global_revision={profile['revision']} group_revision={group_profile['revision']}"
        )
    lines = [header]
    for claim in claims:
        lines.append(
            f"- {claim['key']} [{claim['class']}; source={claim['source']}; "
            f"confidence={claim['confidence']:.3f}]: {claim['value']}"
        )
    return "\n".join(lines)


render_profile_document = render


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_mentions(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in decoded:
        normalized = _optional_identifier(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _same_nonempty(left: Any, right: Any) -> bool:
    left_value = str(left or "").strip()
    right_value = str(right or "").strip()
    return bool(left_value and right_value and left_value == right_value)


def _reply_points_to(source: sqlite3.Row, target: sqlite3.Row) -> bool:
    reply_to_message_id = str(source["reply_to_msg_id"] or "").strip()
    if reply_to_message_id:
        return _same_nonempty(reply_to_message_id, target["message_id"])
    reply_to_user_id = _optional_identifier(source["reply_to_user_id"])
    target_user_id = _normalize_identifier(target["user_id"], "user_id")
    source_key = (_finite_float(source["timestamp"], 0.0), int(source["id"]))
    target_key = (_finite_float(target["timestamp"], 0.0), int(target["id"]))
    return bool(
        reply_to_user_id
        and reply_to_user_id == target_user_id
        and source_key > target_key
        and source_key[0] - target_key[0] <= MAX_MENTION_DISTANCE_SECONDS
    )


def _within_time_distance(left: sqlite3.Row, right: sqlite3.Row, limit: float) -> bool:
    return abs(
        _finite_float(left["timestamp"], 0.0)
        - _finite_float(right["timestamp"], 0.0)
    ) <= limit


def _row_relation(anchor: sqlite3.Row, context: sqlite3.Row) -> str:
    anchor_user = _normalize_identifier(anchor["user_id"], "user_id")
    context_user = _normalize_identifier(context["user_id"], "user_id")
    if _reply_points_to(context, anchor) or _reply_points_to(anchor, context):
        return "reply"
    anchor_mentions = _parse_mentions(anchor["mentioned_ids"])
    context_mentions = _parse_mentions(context["mentioned_ids"])
    if (
        anchor_user in context_mentions or context_user in anchor_mentions
    ) and _within_time_distance(anchor, context, MAX_MENTION_DISTANCE_SECONDS):
        return "mention"
    if _same_nonempty(anchor["thread_id"], context["thread_id"]) and _within_time_distance(
        anchor,
        context,
        MAX_SAME_THREAD_DISTANCE_SECONDS,
    ):
        return "same_thread"
    return ""


def _evidence_from_row(
    row: sqlite3.Row,
    *,
    actor: str,
    relation: str,
) -> ProfileEvidenceMessage:
    return ProfileEvidenceMessage(
        row_id=int(row["id"]),
        message_id=_bounded_text(row["message_id"], 256),
        group_id=_normalize_identifier(row["group_id"], "group_id"),
        user_id=_normalize_identifier(row["user_id"], "user_id"),
        actor=actor,
        relation=relation,
        timestamp=_finite_float(row["timestamp"], 0.0),
        content=str(row["content"] or ""),
    )


def _selector_limit(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return min(MAX_EVIDENCE_SIDE, parsed)


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def select_profile_evidence(
    anchor_row_id: Any,
    db_path: str | Path | None = None,
    before_limit: int = MAX_EVIDENCE_SIDE,
    after_limit: int = MAX_EVIDENCE_SIDE,
) -> ProfileEvidenceWindow:
    """Select related human messages around one safe group-message anchor."""
    row_id = _non_negative_int(anchor_row_id, 0)
    if row_id <= 0 or isinstance(anchor_row_id, bool):
        raise ProfileEvidenceError("anchor_row_id must be a positive integer")
    before_cap = _selector_limit(before_limit, "before_limit")
    after_cap = _selector_limit(after_limit, "after_limit")
    path = Path(db_path) if db_path is not None else Path(get_db_path())

    with _readonly_connection(path) as connection:
        anchor = connection.execute(
            """
            SELECT id, group_id, user_id, content, is_bot, reply_to_msg_id,
                   reply_to_user_id, mentioned_ids, message_id, thread_id,
                   source_kind, timestamp
            FROM group_messages
            WHERE id=?
            """,
            (row_id,),
        ).fetchone()
        if anchor is None:
            raise ProfileEvidenceError("anchor group message does not exist")
        source_kind = str(anchor["source_kind"] or "").strip().lower()
        if int(anchor["is_bot"] or 0) != 0 or source_kind != "user":
            raise ProfileEvidenceError("anchor must be a human user group message")
        try:
            group_id = _normalize_identifier(anchor["group_id"], "group_id")
            anchor_user_id = _normalize_identifier(anchor["user_id"], "user_id")
        except ValueError as exc:
            raise ProfileEvidenceError(str(exc)) from exc

        rows = connection.execute(
            """
            WITH neighborhood(id) AS (
                SELECT id FROM (
                    SELECT id
                    FROM group_messages
                    WHERE TRIM(group_id)=? AND id<?
                      AND timestamp BETWEEN ? AND ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                UNION
                SELECT id FROM (
                    SELECT id
                    FROM group_messages
                    WHERE TRIM(group_id)=? AND id>?
                      AND timestamp BETWEEN ? AND ?
                    ORDER BY id ASC
                    LIMIT ?
                )
            ), direct_reply(id) AS (
                SELECT id
                FROM group_messages
                WHERE TRIM(group_id)=? AND id<>?
                  AND timestamp BETWEEN ? AND ?
                  AND (
                    (?<>'' AND TRIM(reply_to_msg_id)=?)
                    OR (?<>'' AND TRIM(message_id)=?)
                    OR (
                      TRIM(reply_to_user_id)=? AND timestamp>=?
                      AND timestamp-?<=?
                    )
                    OR (
                      ?<>'' AND TRIM(user_id)=? AND timestamp<=?
                      AND ?-timestamp<=?
                    )
                  )
                ORDER BY ABS(timestamp-?) ASC, id ASC
                LIMIT 32
            )
            SELECT id, group_id, user_id, content, is_bot, reply_to_msg_id,
                   reply_to_user_id, mentioned_ids, message_id, thread_id,
                   source_kind, timestamp
            FROM group_messages
            WHERE TRIM(group_id)=? AND id<>? AND is_bot=0
              AND LOWER(TRIM(source_kind))='user'
              AND id IN (
                SELECT id FROM neighborhood
                UNION
                SELECT id FROM direct_reply
              )
            ORDER BY timestamp ASC, id ASC
            """,
            (
                group_id,
                row_id,
                _finite_float(anchor["timestamp"], 0.0) - MAX_EVIDENCE_TIME_DISTANCE_SECONDS,
                _finite_float(anchor["timestamp"], 0.0) + MAX_EVIDENCE_TIME_DISTANCE_SECONDS,
                MAX_EVIDENCE_ROW_DISTANCE,
                group_id,
                row_id,
                _finite_float(anchor["timestamp"], 0.0) - MAX_EVIDENCE_TIME_DISTANCE_SECONDS,
                _finite_float(anchor["timestamp"], 0.0) + MAX_EVIDENCE_TIME_DISTANCE_SECONDS,
                MAX_EVIDENCE_ROW_DISTANCE,
                group_id,
                row_id,
                _finite_float(anchor["timestamp"], 0.0) - MAX_EVIDENCE_TIME_DISTANCE_SECONDS,
                _finite_float(anchor["timestamp"], 0.0) + MAX_EVIDENCE_TIME_DISTANCE_SECONDS,
                str(anchor["message_id"] or "").strip(),
                str(anchor["message_id"] or "").strip(),
                str(anchor["reply_to_msg_id"] or "").strip(),
                str(anchor["reply_to_msg_id"] or "").strip(),
                anchor_user_id,
                _finite_float(anchor["timestamp"], 0.0),
                _finite_float(anchor["timestamp"], 0.0),
                MAX_MENTION_DISTANCE_SECONDS,
                _optional_identifier(anchor["reply_to_user_id"]),
                _optional_identifier(anchor["reply_to_user_id"]),
                _finite_float(anchor["timestamp"], 0.0),
                _finite_float(anchor["timestamp"], 0.0),
                MAX_MENTION_DISTANCE_SECONDS,
                _finite_float(anchor["timestamp"], 0.0),
                group_id,
                row_id,
            ),
        ).fetchall()

    anchor_evidence = _evidence_from_row(anchor, actor="self", relation="anchor")
    anchor_key = (anchor_evidence.timestamp, anchor_evidence.row_id)
    before: list[ProfileEvidenceMessage] = []
    after: list[ProfileEvidenceMessage] = []
    for row in rows:
        try:
            row_group_id = _normalize_identifier(row["group_id"], "group_id")
            context_user_id = _normalize_identifier(row["user_id"], "user_id")
        except ValueError:
            continue
        if row_group_id != group_id or context_user_id == anchor_user_id:
            continue
        relation = _row_relation(anchor, row)
        if not relation:
            continue
        evidence = _evidence_from_row(row, actor="context", relation=relation)
        if (evidence.timestamp, evidence.row_id) < anchor_key:
            before.append(evidence)
        else:
            after.append(evidence)

    def _one_relation_per_context_user(
        values: list[ProfileEvidenceMessage],
    ) -> list[ProfileEvidenceMessage]:
        selected: dict[str, ProfileEvidenceMessage] = {}
        for item in values:
            previous = selected.get(item.user_id)
            item_distance = abs(item.timestamp - anchor_evidence.timestamp)
            if previous is None or (
                _RELATION_PRIORITY[item.relation],
                item_distance,
                abs(item.row_id - anchor_evidence.row_id),
            ) < (
                _RELATION_PRIORITY[previous.relation],
                abs(previous.timestamp - anchor_evidence.timestamp),
                abs(previous.row_id - anchor_evidence.row_id),
            ):
                selected[item.user_id] = item
        return list(selected.values())

    before = sorted(
        _one_relation_per_context_user(before),
        key=lambda item: (
            _RELATION_PRIORITY[item.relation],
            -item.timestamp,
            -item.row_id,
        ),
    )[:before_cap]
    after = sorted(
        _one_relation_per_context_user(after),
        key=lambda item: (
            _RELATION_PRIORITY[item.relation],
            item.timestamp,
            item.row_id,
        ),
    )[:after_cap]
    before.sort(key=lambda item: (item.timestamp, item.row_id))
    after.sort(key=lambda item: (item.timestamp, item.row_id))
    return ProfileEvidenceWindow(
        anchor=anchor_evidence,
        before=tuple(before),
        after=tuple(after),
    )


__all__ = [
    "CLAIM_CLASS_ALLOWLIST",
    "CLAIM_SOURCE_ALLOWLIST",
    "EVIDENCE_RELATION_ALLOWLIST",
    "EvidenceMessage",
    "EvidenceWindow",
    "GENERATION_STATUS_ALLOWLIST",
    "GROUP_CONTEXTUAL_KEYS",
    "MAX_CLAIM_EVIDENCE_REFS",
    "MAX_CLAIM_VALUE_CHARS",
    "MAX_EVIDENCE_SIDE",
    "ProfileEvidenceError",
    "ProfileEvidenceMessage",
    "ProfileEvidenceWindow",
    "SCHEMA_VERSION",
    "build_global_profile_document",
    "build_group_profile_document",
    "effective",
    "effective_profile_claims",
    "normalize",
    "normalize_profile_document",
    "profile_document_digest",
    "render",
    "render_profile_document",
    "select_profile_evidence",
]
