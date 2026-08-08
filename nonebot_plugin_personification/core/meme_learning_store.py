from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import connect_sync


SENSE_STATUSES = frozenset({
    "observed",
    "understand_only",
    "verified",
    "disputed",
    "stale",
    "rejected",
    "manual_locked",
})


@dataclass(frozen=True)
class LearningThresholds:
    auto_understand_min_sources: int = 2
    auto_use_min_sources: int = 3
    auto_use_min_platforms: int = 2
    claim_min_confidence: float = 0.72
    semantic_equivalence_min_confidence: float = 0.80
    reverify_after_days: int = 30
    stale_after_days: int = 90

    def normalized(self) -> "LearningThresholds":
        return LearningThresholds(
            auto_understand_min_sources=max(2, int(self.auto_understand_min_sources)),
            auto_use_min_sources=max(2, int(self.auto_use_min_sources)),
            auto_use_min_platforms=max(2, int(self.auto_use_min_platforms)),
            claim_min_confidence=max(0.0, min(1.0, float(self.claim_min_confidence))),
            semantic_equivalence_min_confidence=max(
                0.0, min(1.0, float(self.semantic_equivalence_min_confidence))
            ),
            reverify_after_days=max(1, int(self.reverify_after_days)),
            stale_after_days=max(int(self.reverify_after_days) + 1, int(self.stale_after_days)),
        )


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, type(fallback)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback
    return fallback


def _normalize(value: Any) -> str:
    return _clean(value, 1000).casefold().replace(" ", "")


def _game_context(claim: dict[str, Any]) -> dict[str, Any]:
    raw = claim.get("game_context") if isinstance(claim.get("game_context"), dict) else {}
    aliases = []
    for value in list(raw.get("aliases") or [])[:8]:
        text = _clean(value, 100)
        if text and text not in aliases:
            aliases.append(text)
    return {"canonical_name": _clean(raw.get("canonical_name"), 100), "aliases": aliases}


def _game_names(value: dict[str, Any]) -> set[str]:
    return {
        _normalize(item)
        for item in [value.get("canonical_name"), *list(value.get("aliases") or [])]
        if _normalize(item)
    }


def _same_context(sense: dict[str, Any], claim: dict[str, Any]) -> bool:
    left_game = _game_names(sense.get("game_context") or {})
    right_game = _game_names(_game_context(claim))
    if left_game or right_game:
        if not left_game.intersection(right_game):
            return False
    return _normalize(sense.get("version_context")) == _normalize(claim.get("version_context"))


def _sense_identity(claim: dict[str, Any], *, scope: str, group_id: str) -> str:
    game = _game_context(claim)
    raw = "\0".join((
        scope,
        group_id,
        _normalize(claim.get("term")),
        _normalize(game.get("canonical_name")),
        _normalize(claim.get("version_context")),
        _normalize(claim.get("meaning")),
    ))
    return "sense_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _row_to_sense(row: Any) -> dict[str, Any]:
    return {
        "sense_id": str(row["sense_id"] or ""),
        "scope": str(row["scope"] or "public"),
        "group_id": str(row["group_id"] or ""),
        "term": str(row["term"] or ""),
        "meaning": str(row["meaning"] or ""),
        "aliases": _json(row["aliases_json"], []),
        "game_context": _json(row["game_context_json"], {}),
        "version_context": str(row["version_context"] or ""),
        "usage_context": str(row["usage_context"] or ""),
        "safe_usage": str(row["safe_usage"] or ""),
        "risk_level": str(row["risk_level"] or "low"),
        "status": str(row["status"] or "observed"),
        "confidence": float(row["confidence"] or 0),
        "source_count": int(row["source_count"] or 0),
        "platform_count": int(row["platform_count"] or 0),
        "auto_managed": bool(row["auto_managed"]),
        "first_seen_at": float(row["first_seen_at"] or 0),
        "last_verified_at": float(row["last_verified_at"] or 0),
        "reverify_after": float(row["reverify_after"] or 0),
        "expires_at": float(row["expires_at"] or 0),
        "manual_locked": bool(row["manual_locked"]),
        "revision": int(row["revision"] or 1),
        "updated_at": float(row["updated_at"] or 0),
    }


class MemeLearningStore:
    def __init__(self, thresholds: LearningThresholds | None = None) -> None:
        self.thresholds = (thresholds or LearningThresholds()).normalized()

    def list_senses(
        self,
        *,
        term: str = "",
        status: str = "",
        scope: str = "",
        group_id: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if term:
            clauses.append("term=?")
            params.append(_clean(term, 80))
        if status:
            if status not in SENSE_STATUSES:
                raise ValueError("invalid sense status")
            clauses.append("status=?")
            params.append(status)
        if scope:
            clauses.append("scope=?")
            params.append(_clean(scope, 20))
        if group_id:
            clauses.append("group_id=?")
            params.append(_clean(group_id, 100))
        query = "SELECT * FROM meme_senses"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(2000, int(limit))))
        with connect_sync() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_row_to_sense(row) for row in rows]

    def get_sense(self, sense_id: str, *, include_detail: bool = False) -> dict[str, Any] | None:
        with connect_sync() as conn:
            row = conn.execute("SELECT * FROM meme_senses WHERE sense_id=?", (_clean(sense_id, 80),)).fetchone()
            if row is None:
                return None
            sense = _row_to_sense(row)
            if include_detail:
                evidence = conn.execute(
                    """SELECT claim_id,packet_id,platform,content_id,canonical_url,content_type,
                              discussion_id,evidence_type,quote,content_fingerprint,media_fingerprint,
                              source_cluster_id,extractor_version,model_route,confidence,source_quality,
                              published_at,retrieved_at,created_at
                       FROM meme_evidence_claims WHERE sense_id=? ORDER BY created_at DESC""",
                    (sense["sense_id"],),
                ).fetchall()
                events = conn.execute(
                    "SELECT * FROM meme_learning_events WHERE sense_id=? ORDER BY created_at DESC LIMIT 200",
                    (sense["sense_id"],),
                ).fetchall()
                sense["evidence"] = [dict(item) for item in evidence]
                sense["events"] = [
                    {**dict(item), "detail": _json(item["detail_json"], {})}
                    for item in events
                ]
            return sense

    def list_events(self, *, sense_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        params: list[Any] = []
        query = "SELECT * FROM meme_learning_events"
        if sense_id:
            query += " WHERE sense_id=?"
            params.append(_clean(sense_id, 80))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(2000, int(limit))))
        with connect_sync() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [{**dict(row), "detail": _json(row["detail_json"], {})} for row in rows]

    async def ingest_claims(
        self,
        claims: list[dict[str, Any]],
        *,
        semantic_pipeline: Any,
        scope: str = "public",
        group_id: str = "",
        model_route: str = "",
        extractor_version: str = "v1",
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = float(now if now is not None else time.time())
        results: list[dict[str, Any]] = []
        for claim in claims[:50]:
            if float(claim.get("extractor_confidence", 0) or 0) < self.thresholds.claim_min_confidence:
                continue
            if not claim.get("evidence_refs") or not claim.get("source_cluster_id"):
                continue
            term = _clean(claim.get("term"), 80)
            meaning = _clean(claim.get("meaning"), 500)
            if not term or not meaning:
                continue
            candidates = [
                item for item in self.list_senses(term=term, scope=scope, group_id=group_id, limit=100)
                if item["status"] != "rejected" and _same_context(item, claim)
            ]
            chosen: dict[str, Any] | None = next(
                (item for item in candidates if _normalize(item["meaning"]) == _normalize(meaning)),
                None,
            )
            conflicts: list[dict[str, Any]] = []
            for candidate in candidates:
                if chosen is not None and candidate["sense_id"] == chosen["sense_id"]:
                    continue
                comparison = await semantic_pipeline.compare_senses(candidate, claim)
                if not comparison:
                    continue
                relation = str(comparison.get("relation") or "")
                confidence = float(comparison.get("confidence", 0) or 0)
                if relation in {"same", "compatible"} and confidence >= self.thresholds.semantic_equivalence_min_confidence:
                    if chosen is None:
                        chosen = candidate
                elif relation == "conflict" and confidence >= self.thresholds.semantic_equivalence_min_confidence:
                    conflicts.append(candidate)
            if chosen is None:
                chosen = self._create_sense(claim, scope=scope, group_id=group_id, now=timestamp)
            inserted = self._insert_evidence(
                chosen,
                claim,
                model_route=model_route,
                extractor_version=extractor_version,
                now=timestamp,
            )
            refreshed = self._refresh_state(chosen["sense_id"], now=timestamp)
            if inserted:
                for conflict in conflicts:
                    self._apply_conflict(refreshed, conflict, now=timestamp)
            current = self.get_sense(chosen["sense_id"], include_detail=False)
            if current is not None:
                results.append(current)
        return results

    def _create_sense(self, claim: dict[str, Any], *, scope: str, group_id: str, now: float) -> dict[str, Any]:
        sense_id = _sense_identity(claim, scope=scope, group_id=group_id)
        with connect_sync() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO meme_senses(
                    sense_id,scope,group_id,term,meaning,aliases_json,game_context_json,
                    version_context,usage_context,safe_usage,risk_level,status,confidence,
                    source_count,platform_count,auto_managed,first_seen_at,last_verified_at,
                    reverify_after,expires_at,manual_locked,revision,updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observed', 0, 0, 0, 1, ?, 0, 0, 0, 0, 1, ?)
                """,
                (
                    sense_id,
                    scope,
                    group_id,
                    _clean(claim.get("term"), 80),
                    _clean(claim.get("meaning"), 500),
                    json.dumps(list(claim.get("aliases") or [])[:12], ensure_ascii=False),
                    json.dumps(_game_context(claim), ensure_ascii=False),
                    _clean(claim.get("version_context"), 100),
                    _clean(claim.get("usage_context"), 300),
                    _clean(claim.get("safe_usage"), 300),
                    _clean(claim.get("risk_level"), 20) or "low",
                    now,
                    now,
                ),
            )
            conn.commit()
        sense = self.get_sense(sense_id)
        if sense is None:
            raise RuntimeError("failed to create meme sense")
        self._event(sense_id, "observed", "", "observed", {"term": sense["term"]}, now=now)
        return sense

    def _insert_evidence(
        self,
        sense: dict[str, Any],
        claim: dict[str, Any],
        *,
        model_route: str,
        extractor_version: str,
        now: float,
    ) -> bool:
        ref = next((item for item in list(claim.get("evidence_refs") or []) if isinstance(item, dict)), None)
        if ref is None:
            return False
        source_cluster_id = _clean(claim.get("source_cluster_id"), 100)
        claim_id = "claim_" + hashlib.sha256(
            f"{sense['sense_id']}\0{source_cluster_id}".encode("utf-8")
        ).hexdigest()[:32]
        source = claim.get("source") if isinstance(claim.get("source"), dict) else {}
        discussion_type = _clean(ref.get("type") or ref.get("evidence_type"), 30)
        with connect_sync() as conn:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO meme_evidence_claims(
                    claim_id,sense_id,packet_id,platform,content_id,canonical_url,content_type,
                    discussion_id,evidence_type,author_fingerprint,quote,content_fingerprint,
                    media_fingerprint,source_cluster_id,extractor_version,model_route,confidence,
                    source_quality,published_at,retrieved_at,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    sense["sense_id"],
                    _clean(ref.get("packet_id"), 100),
                    _clean(ref.get("platform"), 30),
                    _clean(ref.get("content_id"), 300),
                    _clean(source.get("canonical_url"), 1000),
                    _clean(source.get("content_type"), 30),
                    _clean(ref.get("discussion_id"), 100),
                    discussion_type,
                    _clean(ref.get("quote"), 300),
                    _clean(source.get("content_fingerprint"), 200),
                    _clean(source.get("media_fingerprint"), 200),
                    source_cluster_id,
                    _clean(extractor_version, 80),
                    _clean(model_route, 120),
                    max(0.0, min(1.0, float(claim.get("extractor_confidence", 0) or 0))),
                    max(0.0, min(1.0, float(source.get("quality_score", 0.5) or 0.5))),
                    float(source.get("published_at", 0) or 0),
                    float(source.get("retrieved_at", 0) or 0),
                    now,
                ),
            )
            inserted = conn.total_changes > before
            conn.commit()
        if inserted:
            self._event(
                sense["sense_id"],
                "evidence_added",
                sense["status"],
                sense["status"],
                {"platform": ref.get("platform"), "content_id": ref.get("content_id"), "source_cluster_id": source_cluster_id},
                now=now,
            )
        return inserted

    def _refresh_state(self, sense_id: str, *, now: float) -> dict[str, Any]:
        sense = self.get_sense(sense_id)
        if sense is None:
            raise KeyError(sense_id)
        with connect_sync() as conn:
            aggregates = conn.execute(
                """SELECT COUNT(DISTINCT source_cluster_id) AS source_count,
                          COUNT(DISTINCT platform) AS platform_count,
                          AVG(confidence) AS avg_confidence, AVG(source_quality) AS avg_quality
                   FROM meme_evidence_claims WHERE sense_id=?""",
                (sense_id,),
            ).fetchone()
        source_count = int(aggregates["source_count"] or 0)
        platform_count = int(aggregates["platform_count"] or 0)
        avg_confidence = float(aggregates["avg_confidence"] or 0)
        avg_quality = float(aggregates["avg_quality"] or 0)
        confidence = min(
            0.99,
            0.35
            + min(0.30, source_count * 0.10)
            + min(0.10, platform_count * 0.04)
            + avg_confidence * 0.20
            + avg_quality * 0.05,
        )
        old_status = sense["status"]
        if sense["manual_locked"] or old_status == "manual_locked":
            new_status = "manual_locked"
        elif old_status in {"disputed", "rejected"}:
            new_status = old_status
        elif source_count >= self.thresholds.auto_use_min_sources and platform_count >= self.thresholds.auto_use_min_platforms:
            new_status = "verified"
        elif source_count >= self.thresholds.auto_understand_min_sources:
            new_status = "understand_only"
        else:
            new_status = "observed"
        last_verified_at = sense["last_verified_at"]
        reverify_after = sense["reverify_after"]
        expires_at = sense["expires_at"]
        if new_status == "verified":
            last_verified_at = now
            reverify_after = now + self.thresholds.reverify_after_days * 86400
            expires_at = now + self.thresholds.stale_after_days * 86400
        with connect_sync() as conn:
            conn.execute(
                """UPDATE meme_senses SET status=?,confidence=?,source_count=?,platform_count=?,
                       last_verified_at=?,reverify_after=?,expires_at=?,revision=revision+1,updated_at=?
                   WHERE sense_id=?""",
                (
                    new_status,
                    confidence,
                    source_count,
                    platform_count,
                    last_verified_at,
                    reverify_after,
                    expires_at,
                    now,
                    sense_id,
                ),
            )
            conn.commit()
        if old_status != new_status:
            self._event(sense_id, "status_changed", old_status, new_status, {
                "source_count": source_count, "platform_count": platform_count
            }, now=now)
        refreshed = self.get_sense(sense_id)
        if refreshed is None:
            raise KeyError(sense_id)
        if new_status in {"understand_only", "verified"}:
            self._sync_compat_root(refreshed, now=now)
        return refreshed

    def _apply_conflict(self, current: dict[str, Any], opposing: dict[str, Any], *, now: float) -> None:
        opposing = self._refresh_state(opposing["sense_id"], now=now)
        if current["source_count"] >= 2 and opposing["source_count"] >= 2:
            for sense in (current, opposing):
                if sense["manual_locked"]:
                    self._event(
                        sense["sense_id"], "conflict_review", sense["status"], sense["status"],
                        {"opposing_sense_id": opposing["sense_id"] if sense is current else current["sense_id"]}, now=now,
                    )
                    continue
                old = sense["status"]
                with connect_sync() as conn:
                    conn.execute(
                        "UPDATE meme_senses SET status='disputed',revision=revision+1,updated_at=? WHERE sense_id=?",
                        (now, sense["sense_id"]),
                    )
                    conn.commit()
                self._event(
                    sense["sense_id"], "conflict", old, "disputed",
                    {"opposing_sense_id": opposing["sense_id"] if sense is current else current["sense_id"]}, now=now,
                )
        elif current["source_count"] >= 2 and not opposing["manual_locked"]:
            old = opposing["status"]
            with connect_sync() as conn:
                conn.execute(
                    "UPDATE meme_senses SET status='disputed',revision=revision+1,updated_at=? WHERE sense_id=?",
                    (now, opposing["sense_id"]),
                )
                conn.commit()
            self._event(
                opposing["sense_id"], "conflict_superseded", old, "disputed",
                {"opposing_sense_id": current["sense_id"]}, now=now,
            )
        elif not opposing["manual_locked"]:
            with connect_sync() as conn:
                conn.execute(
                    "UPDATE meme_senses SET confidence=MAX(0,confidence-0.08),revision=revision+1,updated_at=? WHERE sense_id=?",
                    (now, opposing["sense_id"]),
                )
                conn.commit()
            self._event(
                opposing["sense_id"], "counterevidence", opposing["status"], opposing["status"],
                {"opposing_sense_id": current["sense_id"]}, now=now,
            )

    def _sync_compat_root(self, sense: dict[str, Any], *, now: float) -> None:
        with connect_sync() as conn:
            manual = conn.execute(
                """SELECT 1 FROM meme_senses WHERE scope=? AND group_id=? AND term=?
                   AND manual_locked=1 LIMIT 1""",
                (sense["scope"], sense["group_id"], sense["term"]),
            ).fetchone()
            if manual is not None:
                return
            conn.execute(
                """INSERT INTO meme_dictionary(
                       term,aliases,meaning,tone,risk_level,examples,scope,group_id,
                       confidence,evidence_message_ids,safe_usage,managed_by,updated_at
                   ) VALUES (?, ?, ?, '[]', ?, '[]', ?, ?, ?, '[]', ?, 'auto', ?)
                   ON CONFLICT(scope,group_id,term) DO UPDATE SET
                       aliases=excluded.aliases,meaning=excluded.meaning,risk_level=excluded.risk_level,
                       confidence=excluded.confidence,safe_usage=excluded.safe_usage,managed_by='auto',
                       updated_at=excluded.updated_at""",
                (
                    sense["term"],
                    json.dumps(sense["aliases"], ensure_ascii=False),
                    sense["meaning"],
                    sense["risk_level"],
                    sense["scope"],
                    sense["group_id"],
                    sense["confidence"],
                    sense["safe_usage"],
                    now,
                ),
            )
            conn.commit()

    def _event(
        self,
        sense_id: str,
        event_type: str,
        old_status: str,
        new_status: str,
        detail: dict[str, Any],
        *,
        now: float,
        actor: str = "system",
    ) -> None:
        with connect_sync() as conn:
            conn.execute(
                "INSERT INTO meme_learning_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    sense_id,
                    _clean(event_type, 50),
                    _clean(old_status, 30),
                    _clean(new_status, 30),
                    _clean(actor, 100),
                    json.dumps(detail, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()

    def run_maintenance(self, *, now: float | None = None) -> int:
        timestamp = float(now if now is not None else time.time())
        with connect_sync() as conn:
            rows = conn.execute(
                "SELECT sense_id,status FROM meme_senses WHERE status='verified' AND expires_at>0 AND expires_at<=?",
                (timestamp,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE meme_senses SET status='stale',revision=revision+1,updated_at=? WHERE sense_id=?",
                    (timestamp, row["sense_id"]),
                )
            conn.commit()
        for row in rows:
            self._event(row["sense_id"], "stale", row["status"], "stale", {}, now=timestamp)
        return len(rows)

    def set_manual_status(
        self,
        sense_id: str,
        *,
        status: str,
        actor: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if status not in {"manual_locked", "rejected", "observed"}:
            raise ValueError("invalid manual sense status")
        current = self.get_sense(sense_id)
        if current is None:
            raise KeyError(sense_id)
        if expected_revision is not None and current["revision"] != int(expected_revision):
            raise RuntimeError("revision_conflict")
        now = time.time()
        locked = int(status == "manual_locked")
        with connect_sync() as conn:
            result = conn.execute(
                """UPDATE meme_senses SET status=?,manual_locked=?,auto_managed=?,revision=revision+1,updated_at=?
                   WHERE sense_id=? AND revision=?""",
                (status, locked, 0 if locked else current["auto_managed"], now, sense_id, current["revision"]),
            )
            if result.rowcount != 1:
                raise RuntimeError("revision_conflict")
            conn.commit()
        self._event(sense_id, "manual_status", current["status"], status, {}, now=now, actor=actor)
        updated = self.get_sense(sense_id, include_detail=True)
        if updated is None:
            raise KeyError(sense_id)
        if status == "manual_locked":
            self._write_manual_root(updated, now=now)
        elif current["manual_locked"] or current["status"] == "manual_locked":
            with connect_sync() as conn:
                conn.execute(
                    "DELETE FROM meme_dictionary WHERE scope=? AND group_id=? AND term=?",
                    (current["scope"], current["group_id"], current["term"]),
                )
                conn.commit()
        return updated

    def merge_senses(
        self,
        *,
        target_sense_id: str,
        source_sense_ids: list[str],
        expected_revisions: dict[str, int],
        actor: str,
    ) -> dict[str, Any]:
        target = self.get_sense(target_sense_id)
        sources = [self.get_sense(value) for value in source_sense_ids if value != target_sense_id]
        if target is None or any(item is None for item in sources) or not sources:
            raise KeyError("sense_not_found")
        concrete_sources = [item for item in sources if item is not None]
        all_senses = [target, *concrete_sources]
        if any(item["term"] != target["term"] or item["scope"] != target["scope"] or item["group_id"] != target["group_id"] for item in all_senses):
            raise ValueError("only senses under the same dictionary root can be merged")
        if any(item["revision"] != int(expected_revisions.get(item["sense_id"], -1)) for item in all_senses):
            raise RuntimeError("revision_conflict")
        now = time.time()
        with connect_sync() as conn:
            for source in concrete_sources:
                evidence = conn.execute(
                    "SELECT * FROM meme_evidence_claims WHERE sense_id=?",
                    (source["sense_id"],),
                ).fetchall()
                for row in evidence:
                    new_claim_id = "claim_" + hashlib.sha256(
                        f"{target_sense_id}\0{row['source_cluster_id']}".encode("utf-8")
                    ).hexdigest()[:32]
                    values = dict(row)
                    values["claim_id"] = new_claim_id
                    values["sense_id"] = target_sense_id
                    columns = list(values)
                    conn.execute(
                        f"INSERT OR IGNORE INTO meme_evidence_claims({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(values[column] for column in columns),
                    )
                conn.execute("DELETE FROM meme_evidence_claims WHERE sense_id=?", (source["sense_id"],))
                conn.execute(
                    """UPDATE meme_senses SET status='rejected',manual_locked=0,auto_managed=0,
                           revision=revision+1,updated_at=? WHERE sense_id=? AND revision=?""",
                    (now, source["sense_id"], source["revision"]),
                )
            conn.commit()
        merged = self._refresh_state(target_sense_id, now=now)
        merged = self.set_manual_status(
            target_sense_id,
            status="manual_locked",
            actor=actor,
            expected_revision=merged["revision"],
        )
        for source in concrete_sources:
            self._event(
                source["sense_id"], "merged", source["status"], "rejected",
                {"target_sense_id": target_sense_id}, now=now, actor=actor,
            )
        return merged

    def split_sense(
        self,
        sense_id: str,
        *,
        claim_ids: list[str],
        patch: dict[str, Any],
        expected_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        current = self.get_sense(sense_id)
        if current is None:
            raise KeyError(sense_id)
        if current["revision"] != int(expected_revision):
            raise RuntimeError("revision_conflict")
        selected = list(dict.fromkeys(_clean(value, 80) for value in claim_ids if _clean(value, 80)))
        if not selected:
            raise ValueError("claim_ids are required")
        now = time.time()
        new_id = "sense_" + uuid.uuid4().hex
        game_context = patch.get("game_context") if isinstance(patch.get("game_context"), dict) else current["game_context"]
        with connect_sync() as conn:
            rows = conn.execute(
                f"SELECT claim_id FROM meme_evidence_claims WHERE sense_id=? AND claim_id IN ({','.join('?' for _ in selected)})",
                (sense_id, *selected),
            ).fetchall()
            if len(rows) != len(selected):
                raise ValueError("one or more evidence claims do not belong to the sense")
            conn.execute(
                """INSERT INTO meme_senses(
                       sense_id,scope,group_id,term,meaning,aliases_json,game_context_json,
                       version_context,usage_context,safe_usage,risk_level,status,confidence,
                       source_count,platform_count,auto_managed,first_seen_at,last_verified_at,
                       reverify_after,expires_at,manual_locked,revision,updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_locked', 0, 0, 0, 0, ?, ?, 0, 0, 1, 1, ?)""",
                (
                    new_id,
                    current["scope"],
                    current["group_id"],
                    _clean(patch.get("term") or current["term"], 80),
                    _clean(patch.get("meaning") or current["meaning"], 500),
                    json.dumps(patch.get("aliases") if isinstance(patch.get("aliases"), list) else current["aliases"], ensure_ascii=False),
                    json.dumps(game_context, ensure_ascii=False),
                    _clean(patch.get("version_context", current["version_context"]), 100),
                    _clean(patch.get("usage_context", current["usage_context"]), 300),
                    _clean(patch.get("safe_usage", current["safe_usage"]), 300),
                    _clean(patch.get("risk_level", current["risk_level"]), 20),
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                f"UPDATE meme_evidence_claims SET sense_id=? WHERE sense_id=? AND claim_id IN ({','.join('?' for _ in selected)})",
                (new_id, sense_id, *selected),
            )
            conn.commit()
        self._refresh_state(sense_id, now=now)
        split = self._refresh_state(new_id, now=now)
        self._event(sense_id, "split", current["status"], current["status"], {"new_sense_id": new_id}, now=now, actor=actor)
        self._event(new_id, "split_created", "", split["status"], {"source_sense_id": sense_id}, now=now, actor=actor)
        detail = self.get_sense(new_id, include_detail=True)
        if detail is None:
            raise KeyError(new_id)
        return detail

    def _write_manual_root(self, sense: dict[str, Any], *, now: float) -> None:
        with connect_sync() as conn:
            conn.execute(
                """INSERT INTO meme_dictionary(
                       term,aliases,meaning,tone,risk_level,examples,scope,group_id,
                       confidence,evidence_message_ids,safe_usage,managed_by,updated_at
                   ) VALUES (?, ?, ?, '[]', ?, '[]', ?, ?, ?, '[]', ?, 'manual', ?)
                   ON CONFLICT(scope,group_id,term) DO UPDATE SET
                       aliases=excluded.aliases,meaning=excluded.meaning,risk_level=excluded.risk_level,
                       confidence=excluded.confidence,safe_usage=excluded.safe_usage,managed_by='manual',
                       updated_at=excluded.updated_at""",
                (
                    sense["term"],
                    json.dumps(sense["aliases"], ensure_ascii=False),
                    sense["meaning"],
                    sense["risk_level"],
                    sense["scope"],
                    sense["group_id"],
                    sense["confidence"],
                    sense["safe_usage"],
                    now,
                ),
            )
            conn.commit()


__all__ = ["LearningThresholds", "MemeLearningStore", "SENSE_STATUSES"]
