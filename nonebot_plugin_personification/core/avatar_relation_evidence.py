from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from .db import connect_sync


AVATAR_RELATION_EVIDENCE_SCHEMA_VERSION = 1
AVATAR_RELATION_EVIDENCE_TTL_SECONDS = 7 * 24 * 60 * 60
AVATAR_RELATION_EVIDENCE_RELATIONS = frozenset({
    "near_duplicate",
    "coordinated_pair",
    "same_character",
    "same_series",
    "unrelated",
    "uncertain",
})
AVATAR_RELATION_EVIDENCE_TAGS = frozenset({
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
})
AVATAR_RELATION_EVIDENCE_ASSET_KINDS = frozenset({
    "real_person",
    "illustration",
    "acg_character",
    "logo",
    "other",
    "unknown",
})
_HYPOTHESIS_TEXT = {
    "near_duplicate": "两张头像的画面相同或近乎相同",
    "coordinated_pair": "两张头像在构图、元素或风格上呈现视觉配套",
    "same_character": "两张头像可能描绘同一虚构角色",
    "same_series": "两张头像可能来自同一作品或视觉系列",
}


def _numeric_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isascii() and text.isdecimal() and int(text) > 0 else ""


def _canonical_pair(left_user_id: Any, right_user_id: Any) -> tuple[str, str]:
    left = _numeric_id(left_user_id)
    right = _numeric_id(right_user_id)
    if not left or not right or left == right:
        return "", ""
    return tuple(sorted((left, right)))


def _bounded_enum_list(value: Any, *, allowed: Iterable[str], limit: int) -> list[str]:
    allowed_values = set(allowed)
    result: list[str] = []
    for item in list(value or [])[:limit]:
        normalized = str(item or "").strip().lower()
        if normalized in allowed_values and normalized not in result:
            result.append(normalized)
    return result


def record_avatar_relation_evidence(
    *,
    group_id: Any,
    left_user_id: Any,
    right_user_id: Any,
    relation: Any,
    confidence: Any,
    evidence_tags: Any,
    asset_kinds: Any,
    avatar_hashes: dict[str, str],
    observed_at: float | None = None,
    ttl_seconds: float = AVATAR_RELATION_EVIDENCE_TTL_SECONDS,
    db_path: str | Path | None = None,
) -> bool:
    group = _numeric_id(group_id)
    left, right = _canonical_pair(left_user_id, right_user_id)
    normalized_relation = str(relation or "").strip().lower()
    if not group or not left or normalized_relation not in AVATAR_RELATION_EVIDENCE_RELATIONS:
        return False
    try:
        normalized_confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError, OverflowError):
        normalized_confidence = 0.0
    tags = _bounded_enum_list(
        evidence_tags,
        allowed=AVATAR_RELATION_EVIDENCE_TAGS,
        limit=12,
    )
    kinds = _bounded_enum_list(
        asset_kinds,
        allowed=AVATAR_RELATION_EVIDENCE_ASSET_KINDS,
        limit=2,
    )
    now = float(observed_at if observed_at is not None else time.time())
    hashes = {
        _numeric_id(key): str(value or "").strip().lower()
        for key, value in dict(avatar_hashes or {}).items()
        if _numeric_id(key)
    }
    left_hash = hashes.get(left, "")
    right_hash = hashes.get(right, "")
    if any(len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value) for value in (left_hash, right_hash)):
        return False
    with connect_sync(Path(db_path) if db_path is not None else None) as conn:
        conn.execute(
            """
            INSERT INTO avatar_relation_evidence(
                group_id,left_user_id,right_user_id,relation,confidence,
                evidence_tags,asset_kinds,left_avatar_hash,right_avatar_hash,
                schema_version,observed_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id,left_user_id,right_user_id) DO UPDATE SET
                relation=excluded.relation,
                confidence=excluded.confidence,
                evidence_tags=excluded.evidence_tags,
                asset_kinds=excluded.asset_kinds,
                left_avatar_hash=excluded.left_avatar_hash,
                right_avatar_hash=excluded.right_avatar_hash,
                schema_version=excluded.schema_version,
                observed_at=excluded.observed_at,
                expires_at=excluded.expires_at
            """,
            (
                group,
                left,
                right,
                normalized_relation,
                normalized_confidence,
                json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                json.dumps(kinds, ensure_ascii=False, separators=(",", ":")),
                left_hash,
                right_hash,
                AVATAR_RELATION_EVIDENCE_SCHEMA_VERSION,
                now,
                now + max(60.0, float(ttl_seconds)),
            ),
        )
        conn.commit()
    return True


def list_avatar_relation_evidence(
    group_id: Any,
    *,
    limit: int = 100,
    now: float | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    group = _numeric_id(group_id)
    if not group:
        return []
    current = float(now if now is not None else time.time())
    with connect_sync(Path(db_path) if db_path is not None else None) as conn:
        rows = conn.execute(
            """
            SELECT group_id,left_user_id,right_user_id,relation,confidence,
                   evidence_tags,asset_kinds,schema_version,observed_at,expires_at
            FROM avatar_relation_evidence
            WHERE group_id=? AND expires_at>?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (group, current, max(1, min(500, int(limit)))),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("evidence_tags", "asset_kinds"):
            try:
                item[key] = json.loads(str(item.get(key, "[]") or "[]"))
            except Exception:
                item[key] = []
        result.append(item)
    return result


def delete_user_avatar_relation_evidence(
    user_id: Any,
    *,
    db_path: str | Path | None = None,
) -> int:
    uid = _numeric_id(user_id)
    if not uid:
        return 0
    with connect_sync(Path(db_path) if db_path is not None else None) as conn:
        cursor = conn.execute(
            "DELETE FROM avatar_relation_evidence WHERE left_user_id=? OR right_user_id=?",
            (uid, uid),
        )
        conn.commit()
        return max(0, int(cursor.rowcount or 0))


def _safe_display_name(value: Any, *, fallback: str) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    return (normalized[:32] or fallback[:32] or "未知")


def render_avatar_relation_hypotheses(
    group_id: Any,
    *,
    recent_messages: list[dict[str, Any]] | None = None,
    excluded_user_ids: Iterable[Any] | None = None,
    limit: int = 2,
    now: float | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Render bounded visual priors without asserting any real-world relation."""

    excluded = {
        _numeric_id(value)
        for value in list(excluded_user_ids or [])
        if _numeric_id(value)
    }
    names: dict[str, str] = {}
    for message in reversed(list(recent_messages or [])):
        if not isinstance(message, dict):
            continue
        user_id = _numeric_id(message.get("user_id"))
        if not user_id or user_id in excluded or user_id in names:
            continue
        names[user_id] = _safe_display_name(
            message.get("nickname")
            or message.get("speaker")
            or message.get("user_name"),
            fallback=user_id,
        )

    rows = list_avatar_relation_evidence(
        group_id,
        limit=max(1, min(20, int(limit) * 4)),
        now=now,
        db_path=db_path,
    )
    lines: list[str] = []
    for row in rows:
        left = _numeric_id(row.get("left_user_id"))
        right = _numeric_id(row.get("right_user_id"))
        relation = str(row.get("relation", "") or "").strip().lower()
        if not left or not right or left in excluded or right in excluded:
            continue
        hypothesis = _HYPOTHESIS_TEXT.get(relation)
        if not hypothesis:
            continue
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence", 0.0))))
        except (TypeError, ValueError, OverflowError):
            confidence = 0.0
        lines.append(
            f"- {names.get(left, left)} / {names.get(right, right)}：{hypothesis}"
            f"（图像置信度 {confidence:.2f}）"
        )
        if len(lines) >= max(1, min(5, int(limit))):
            break
    if not lines:
        return ""
    return "\n".join(
        [
            "## 头像图像关系假设（仅头像图像先验）",
            *lines,
            "使用边界：这些只描述头像画面，不表示现实关系、身份或两位用户是否认识；不得据此建立现实关系事实。",
        ]
    )


__all__ = [
    "AVATAR_RELATION_EVIDENCE_ASSET_KINDS",
    "AVATAR_RELATION_EVIDENCE_RELATIONS",
    "AVATAR_RELATION_EVIDENCE_SCHEMA_VERSION",
    "AVATAR_RELATION_EVIDENCE_TAGS",
    "AVATAR_RELATION_EVIDENCE_TTL_SECONDS",
    "delete_user_avatar_relation_evidence",
    "list_avatar_relation_evidence",
    "record_avatar_relation_evidence",
    "render_avatar_relation_hypotheses",
]
