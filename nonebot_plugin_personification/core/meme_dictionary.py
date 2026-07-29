from __future__ import annotations

import json
import hashlib
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
                    managed_by='manual',
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
        _upsert_manual_sense(
            conn,
            term=term,
            meaning=meaning,
            aliases=aliases,
            scope=scope,
            group_id=group_id,
            risk_level=risk_level,
            confidence=confidence,
            safe_usage=safe_usage,
            updated_at=now_ts,
            preserve_existing=preserve_existing,
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
        conn.execute(
            "DELETE FROM meme_senses WHERE term=? AND scope=? AND group_id=?",
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


def query_meme_dictionary(
    group_id: str,
    message_text: str,
    *,
    top_k: int = 8,
    game_context: Any = "",
    version_context: str = "",
    context_text: str = "",
) -> list[dict[str, Any]]:
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
        senses = _candidate_senses(
            scope=entry["scope"],
            group_id=entry["group_id"],
            term=entry["term"],
            game_context=game_context,
            version_context=version_context,
            context_text=f"{message_text} {context_text}".strip(),
        )
        if not senses:
            continue
        selected = senses[0]
        enriched = {
            **entry,
            "meaning": selected["meaning"],
            "aliases": selected["aliases"],
            "risk_level": selected["risk_level"],
            "confidence": selected["confidence"],
            "safe_usage": selected["safe_usage"],
            "status": selected["status"],
            "sense_id": selected["sense_id"],
            "game_context": selected["game_context"],
            "version_context": selected["version_context"],
            "usage_context": selected["usage_context"],
            "source_count": selected["source_count"],
            "platform_count": selected["platform_count"],
            "senses": senses,
        }
        matched.append((float(enriched.get("confidence", 0) or 0), hit_len, enriched))
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
        status = str(entry.get("status", "manual_locked") or "manual_locked")
        if status == "understand_only":
            usage = "只理解不主动使用"
        elif status == "disputed":
            usage = "存在冲突，只能作为不确定背景"
        elif status in {"observed", "stale", "rejected"}:
            usage = "不可用于主动表达"
        elif status == "manual_locked" and confidence < 0.8:
            usage = "可轻量试探使用" if confidence >= 0.6 else "只理解不主动使用"
        else:
            usage = "可自然使用" if confidence >= 0.6 else "只理解不主动使用"
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
        "managed_by": str(row["managed_by"] or "manual") if "managed_by" in row.keys() else "manual",
    }


def _upsert_manual_sense(
    conn: Any,
    *,
    term: str,
    meaning: str,
    aliases: list[str],
    scope: str,
    group_id: str,
    risk_level: str,
    confidence: float,
    safe_usage: str,
    updated_at: float,
    preserve_existing: bool,
) -> None:
    raw = f"legacy\0{scope}\0{group_id}\0{term}".encode("utf-8")
    sense_id = "legacy_" + hashlib.sha256(raw).hexdigest()[:32]
    values = (
        sense_id,
        scope,
        group_id,
        term,
        meaning,
        json.dumps(aliases, ensure_ascii=False),
        safe_usage,
        risk_level,
        confidence,
        updated_at,
        updated_at,
        updated_at,
    )
    if preserve_existing:
        conn.execute(
            """INSERT OR IGNORE INTO meme_senses(
                   sense_id,scope,group_id,term,meaning,aliases_json,game_context_json,
                   version_context,usage_context,safe_usage,risk_level,status,confidence,
                   source_count,platform_count,auto_managed,first_seen_at,last_verified_at,
                   reverify_after,expires_at,manual_locked,revision,updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, '{}', '', '', ?, ?, 'manual_locked', ?, 0, 0, 0, ?, ?, 0, 0, 1, 1, ?)""",
            values,
        )
    else:
        conn.execute(
            """INSERT INTO meme_senses(
                   sense_id,scope,group_id,term,meaning,aliases_json,game_context_json,
                   version_context,usage_context,safe_usage,risk_level,status,confidence,
                   source_count,platform_count,auto_managed,first_seen_at,last_verified_at,
                   reverify_after,expires_at,manual_locked,revision,updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, '{}', '', '', ?, ?, 'manual_locked', ?, 0, 0, 0, ?, ?, 0, 0, 1, 1, ?)
               ON CONFLICT(sense_id) DO UPDATE SET meaning=excluded.meaning,aliases_json=excluded.aliases_json,
                   safe_usage=excluded.safe_usage,risk_level=excluded.risk_level,confidence=excluded.confidence,
                   status='manual_locked',manual_locked=1,auto_managed=0,revision=meme_senses.revision+1,
                   updated_at=excluded.updated_at""",
            values,
        )


def _context_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        raw = [value.get("canonical_name"), *list(value.get("aliases") or [])]
    else:
        raw = [value]
    return {str(item or "").strip().casefold().replace(" ", "") for item in raw if str(item or "").strip()}


def _candidate_senses(
    *,
    scope: str,
    group_id: str,
    term: str,
    game_context: Any,
    version_context: str,
    context_text: str = "",
) -> list[dict[str, Any]]:
    requested_games = _context_names(game_context)
    requested_version = str(version_context or "").strip().casefold().replace(" ", "")
    normalized_context = str(context_text or "").casefold().replace(" ", "")
    with connect_sync() as conn:
        rows = conn.execute(
            """SELECT * FROM meme_senses WHERE scope=? AND group_id=? AND term=?
               AND status IN ('manual_locked','verified','understand_only','disputed','stale')""",
            (scope, group_id, term),
        ).fetchall()
    result: list[dict[str, Any]] = []
    priority = {"manual_locked": 5, "verified": 4, "understand_only": 3, "disputed": 2, "stale": 1}
    for row in rows:
        game = _json_object(row["game_context_json"])
        sense_games = _context_names(game)
        if sense_games:
            if requested_games:
                if not sense_games.intersection(requested_games):
                    continue
            elif not any(name in normalized_context for name in sense_games):
                continue
        sense_version = str(row["version_context"] or "").strip().casefold().replace(" ", "")
        if sense_version:
            if requested_version:
                if sense_version != requested_version:
                    continue
            elif sense_version not in normalized_context:
                continue
        result.append({
            "sense_id": str(row["sense_id"] or ""),
            "term": str(row["term"] or ""),
            "meaning": str(row["meaning"] or ""),
            "aliases": _json_list(row["aliases_json"]),
            "game_context": game,
            "version_context": str(row["version_context"] or ""),
            "usage_context": str(row["usage_context"] or ""),
            "safe_usage": str(row["safe_usage"] or ""),
            "risk_level": str(row["risk_level"] or "low"),
            "status": str(row["status"] or "observed"),
            "confidence": float(row["confidence"] or 0),
            "source_count": int(row["source_count"] or 0),
            "platform_count": int(row["platform_count"] or 0),
        })
    result.sort(key=lambda item: (priority.get(item["status"], 0), item["confidence"]), reverse=True)
    return result


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "delete_meme_entry",
    "ensure_public_meme_seeds",
    "format_meme_hint",
    "list_meme_entries",
    "query_meme_dictionary",
    "upsert_meme_entry",
]
