from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .db import connect_sync


_SEED_FILE = Path(__file__).resolve().parents[1] / "data" / "meme_seeds.json"
_VALID_SCOPES = {"public", "group", "concept"}
_VALID_RISKS = {"low", "medium", "high"}
_seeds_loaded = False
_MAX_LIST_LIMIT = 10000


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return _json_list(parsed)
    if value:
        return [str(value).strip()]
    return []


def _normalize_scope(value: Any) -> str:
    scope = str(value or "public").strip().lower()
    return scope if scope in _VALID_SCOPES else "public"


def _normalize_risk(value: Any) -> str:
    risk = str(value or "low").strip().lower()
    return risk if risk in _VALID_RISKS else "low"


def ensure_public_meme_seeds() -> int:
    if not _SEED_FILE.exists():
        return 0
    try:
        data = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    saved = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        if upsert_meme_entry({**item, "scope": "public", "group_id": ""}, preserve_existing=True):
            saved += 1
    return saved


def upsert_meme_entry(payload: dict[str, Any], *, preserve_existing: bool = False) -> bool:
    term = str(payload.get("term", "") or "").strip()
    meaning = str(payload.get("meaning", "") or payload.get("definition", "") or "").strip()
    if not term or not meaning:
        return False
    scope = _normalize_scope(payload.get("scope"))
    group_id = str(payload.get("group_id", "") or "").strip() if scope in {"group", "concept"} else ""
    aliases = _json_list(payload.get("aliases", []))
    tone = _json_list(payload.get("tone", []))
    examples = _json_list(payload.get("examples", []))
    evidence = _json_list(payload.get("evidence_message_ids", []))
    risk_level = _normalize_risk(payload.get("risk_level"))
    safe_usage = str(payload.get("safe_usage", "") or "").strip()
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.7) or 0.7)))
    except (TypeError, ValueError):
        confidence = 0.7
    now_ts = float(payload.get("updated_at", 0) or time.time())
    with connect_sync() as conn:
        if preserve_existing:
            conn.execute(
                """
                INSERT OR IGNORE INTO meme_dictionary(
                    term, aliases, meaning, tone, risk_level, examples, scope, group_id,
                    confidence, evidence_message_ids, safe_usage, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    term,
                    json.dumps(aliases, ensure_ascii=False),
                    meaning,
                    json.dumps(tone, ensure_ascii=False),
                    risk_level,
                    json.dumps(examples, ensure_ascii=False),
                    scope,
                    group_id,
                    confidence,
                    json.dumps(evidence, ensure_ascii=False),
                    safe_usage,
                    now_ts,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO meme_dictionary(
                    term, aliases, meaning, tone, risk_level, examples, scope, group_id,
                    confidence, evidence_message_ids, safe_usage, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, group_id, term) DO UPDATE SET
                    aliases=excluded.aliases,
                    meaning=excluded.meaning,
                    tone=excluded.tone,
                    risk_level=excluded.risk_level,
                    examples=excluded.examples,
                    confidence=excluded.confidence,
                    evidence_message_ids=excluded.evidence_message_ids,
                    safe_usage=excluded.safe_usage,
                    updated_at=excluded.updated_at
                """,
                (
                    term,
                    json.dumps(aliases, ensure_ascii=False),
                    meaning,
                    json.dumps(tone, ensure_ascii=False),
                    risk_level,
                    json.dumps(examples, ensure_ascii=False),
                    scope,
                    group_id,
                    confidence,
                    json.dumps(evidence, ensure_ascii=False),
                    safe_usage,
                    now_ts,
                ),
            )
        changed = conn.total_changes > 0
        conn.commit()
    return changed


def delete_meme_entry(*, term: str, scope: str = "group", group_id: str = "") -> bool:
    with connect_sync() as conn:
        before = conn.total_changes
        conn.execute(
            "DELETE FROM meme_dictionary WHERE term=? AND scope=? AND group_id=?",
            (str(term or "").strip(), _normalize_scope(scope), str(group_id or "").strip()),
        )
        changed = conn.total_changes > before
        conn.commit()
    return changed


def _resolve_list_limit(value: Any, *, default: int = 100) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = int(default)
    if limit <= 0:
        return 0
    return max(1, min(limit, _MAX_LIST_LIMIT))


def list_meme_entries(*, group_id: str = "", scope: str = "", limit: int = 100) -> list[dict[str, Any]]:
    global _seeds_loaded
    if not _seeds_loaded:
        ensure_public_meme_seeds()
        _seeds_loaded = True
    clauses: list[str] = []
    params: list[Any] = []
    normalized_scope = _normalize_scope(scope) if scope else ""
    if normalized_scope:
        clauses.append("scope=?")
        params.append(normalized_scope)
    if group_id:
        clauses.append("(group_id=? OR group_id='')")
        params.append(str(group_id))
    query = "SELECT * FROM meme_dictionary"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY scope DESC, updated_at DESC"
    resolved_limit = _resolve_list_limit(limit)
    if resolved_limit:
        query += " LIMIT ?"
        params.append(resolved_limit)
    with connect_sync() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_row_to_entry(row) for row in rows]


def query_meme_dictionary(group_id: str, message_text: str, *, top_k: int = 8) -> list[dict[str, Any]]:
    text = str(message_text or "").strip().lower()
    if not text:
        return []
    entries = list_meme_entries(group_id=str(group_id), limit=0)
    matched: list[tuple[float, int, dict[str, Any]]] = []
    for entry in entries:
        candidates = [entry["term"], *entry.get("aliases", [])]
        hit_len = 0
        for candidate in candidates:
            normalized = str(candidate or "").strip().lower()
            if normalized and normalized in text:
                hit_len = max(hit_len, len(normalized))
        if hit_len <= 0:
            continue
        matched.append((float(entry.get("confidence", 0) or 0), hit_len, entry))
    matched.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in matched[: max(1, int(top_k or 8))]]


def format_meme_hint(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = ["群聊梗/概念锚点参考（先理解语境，再决定是否使用）："]
    for entry in entries[:8]:
        term = str(entry.get("term", "") or "").strip()
        meaning = str(entry.get("meaning", "") or "").strip()
        confidence = float(entry.get("confidence", 0) or 0)
        risk = str(entry.get("risk_level", "low") or "low")
        scope = str(entry.get("scope", "") or "")
        usage = "只理解不主动使用" if confidence < 0.6 else ("可轻量试探使用" if confidence < 0.8 else "可自然使用")
        safe_usage = str(entry.get("safe_usage", "") or "").strip()
        if term and meaning:
            suffix = f"；{safe_usage}" if safe_usage else ""
            lines.append(f"- {term}: {meaning}（scope={scope}, confidence={confidence:.2f}, risk={risk}, {usage}{suffix}）")
    if len(lines) <= 1:
        return ""
    lines.append("不要解释笑点；risk=high 或 confidence<0.6 时只当作理解背景，避免主动复述或玩梗。")
    return "\n".join(lines)


def _row_to_entry(row: Any) -> dict[str, Any]:
    return {
        "term": str(row["term"] or ""),
        "aliases": _json_list(row["aliases"]),
        "meaning": str(row["meaning"] or ""),
        "tone": _json_list(row["tone"]),
        "risk_level": str(row["risk_level"] or "low"),
        "examples": _json_list(row["examples"]),
        "scope": str(row["scope"] or "public"),
        "group_id": str(row["group_id"] or ""),
        "confidence": float(row["confidence"] or 0),
        "evidence_message_ids": _json_list(row["evidence_message_ids"]),
        "safe_usage": str(row["safe_usage"] or ""),
        "updated_at": float(row["updated_at"] or 0),
    }


__all__ = [
    "delete_meme_entry",
    "ensure_public_meme_seeds",
    "format_meme_hint",
    "list_meme_entries",
    "query_meme_dictionary",
    "upsert_meme_entry",
]
