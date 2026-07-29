from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .db import connect_sync, get_db_path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover - secure fail-closed fallback
    AESGCM = None  # type: ignore[assignment]


POLICY_CLASSIFIER_VERSION = "user-policy-v1"
POLICY_AUTO_RESET_SECONDS = 30 * 24 * 60 * 60
POLICY_LEVEL_1_SECONDS = 12 * 60 * 60
POLICY_LEVEL_2_SECONDS = 24 * 60 * 60
POLICY_EVIDENCE_RETENTION_SECONDS = 7 * 24 * 60 * 60
POLICY_DIRECT_CLOSURE_COOLDOWN_SECONDS = 30 * 24 * 60 * 60

POLICY_TIERS = {"allow", "level_1", "level_2", "permanent"}
POLICY_MANUAL_MODES = {"inherit", "allow", "block"}
POLICY_VERDICTS = {"allow", "boundary_topic", "confirmed_violation", "critical_violation"}
POLICY_CATEGORIES = {
    "none", "sexual", "gambling", "drugs", "terrorism_extremism",
    "graphic_violence", "political_sensitive", "targeted_abuse",
    "harassment", "threat", "other",
}
POLICY_INTENTS = {
    "ordinary", "joke", "neutral_mention", "news_reference",
    "request_to_engage", "advocacy", "targeted_attack",
    "credible_threat", "uncertain",
}
POLICY_SEVERITIES = {"none", "low", "medium", "high", "critical"}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{7,}(?!\d)")
_TOKEN_RE = re.compile(r"(?i)(?:token|cookie|authorization|api[_ -]?key)\s*[:=]\s*\S+")
_SAFE_METADATA_KEYS = {
    "bot_id", "group_id", "message_id", "trace_id", "is_direct",
    "source_kind", "protocol", "implementation", "classifier_route",
    "confirmation",
}


@dataclass(frozen=True)
class PolicyAssessment:
    verdict: str = "boundary_topic"
    category: str = "other"
    intent: str = "uncertain"
    severity: str = "low"
    confidence: float = 0.0
    reason_code: str = "classifier_unavailable"
    classifier_version: str = POLICY_CLASSIFIER_VERSION
    confirmed: bool = False

    @property
    def is_violation(self) -> bool:
        return self.confirmed and self.verdict in {"confirmed_violation", "critical_violation"}

    @property
    def should_quarantine(self) -> bool:
        return self.verdict != "allow"


@dataclass(frozen=True)
class PolicyState:
    user_id: str
    auto_stage: int = 0
    auto_tier: str = "allow"
    auto_expires_at: float = 0.0
    violation_count: int = 0
    last_violation_at: float = 0.0
    manual_mode: str = "inherit"
    manual_expires_at: float = 0.0
    reason_code: str = ""
    revision: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    updated_by: str = ""

    def effective_manual_mode(self, *, now: float | None = None) -> str:
        current = float(now if now is not None else time.time())
        if self.manual_mode not in {"allow", "block"}:
            return "inherit"
        if self.manual_expires_at > 0 and self.manual_expires_at <= current:
            return "inherit"
        return self.manual_mode

    def is_auto_blocked(self, *, now: float | None = None) -> bool:
        current = float(now if now is not None else time.time())
        if self.auto_tier == "permanent":
            return True
        return self.auto_tier in {"level_1", "level_2"} and self.auto_expires_at > current

    def is_blocked(self, *, now: float | None = None) -> bool:
        manual = self.effective_manual_mode(now=now)
        if manual == "block":
            return True
        if manual == "allow":
            return False
        return self.is_auto_blocked(now=now)

    def effective_tier(self, *, now: float | None = None) -> str:
        manual = self.effective_manual_mode(now=now)
        if manual == "block":
            return "manual_block"
        if manual == "allow":
            return "manual_allow"
        return self.auto_tier if self.is_auto_blocked(now=now) else "allow"

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        result = dict(self.__dict__)
        result["effective_tier"] = self.effective_tier(now=now)
        result["blocked"] = self.is_blocked(now=now)
        return result


@dataclass(frozen=True)
class PolicyEventResult:
    state: PolicyState
    assessment: PolicyAssessment
    duplicate: bool = False
    escalated: bool = False
    counts_violation: bool = False


@dataclass(frozen=True)
class PolicyAuthorization:
    blocked: bool
    tier: str
    allow_reply: bool
    allow_visible_reaction: bool
    allow_agent_action: bool
    allow_proactive: bool
    allow_qzone: bool
    allow_profile_write: bool
    allow_history_write: bool
    allow_memory_write: bool
    allow_relation_write: bool
    allow_context_read: bool


class PolicyRevisionConflict(RuntimeError):
    pass


def normalize_policy_assessment(value: Any) -> PolicyAssessment:
    payload = value if isinstance(value, dict) else {}
    verdict = str(payload.get("verdict", "") or "").strip().lower()
    category = str(payload.get("category", "") or "").strip().lower()
    intent = str(payload.get("intent", "") or "").strip().lower()
    severity = str(payload.get("severity", "") or "").strip().lower()
    reason = str(payload.get("reason_code", "") or "").strip().lower()[:64]
    try:
        confidence = min(1.0, max(0.0, float(payload.get("confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    if verdict not in POLICY_VERDICTS:
        verdict, reason = "boundary_topic", "invalid_verdict"
    if category not in POLICY_CATEGORIES:
        category = "other"
    if intent not in POLICY_INTENTS:
        intent = "uncertain"
    if severity not in POLICY_SEVERITIES:
        severity = "low"
    confirmed = bool(payload.get("confirmed", verdict in {"allow", "boundary_topic"}))
    if verdict in {"confirmed_violation", "critical_violation"} and confidence < 0.75:
        verdict, reason, confirmed = "boundary_topic", "violation_confidence_low", False
    return PolicyAssessment(
        verdict=verdict,
        category=category,
        intent=intent,
        severity=severity,
        confidence=confidence,
        reason_code=reason or "classified",
        classifier_version=str(payload.get("classifier_version", "") or POLICY_CLASSIFIER_VERSION)[:64],
        confirmed=confirmed,
    )


def _redact_policy_excerpt(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    text = _URL_RE.sub("[链接]", text)
    text = _LONG_NUMBER_RE.sub("[编号]", text)
    text = _TOKEN_RE.sub("[凭证]", text)
    return text[:160]


def _sanitize_policy_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in (value or {}).items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key not in _SAFE_METADATA_KEYS:
            continue
        if isinstance(item, bool):
            result[normalized_key] = item
        elif isinstance(item, (int, float)):
            result[normalized_key] = item
        elif item is not None:
            result[normalized_key] = _redact_policy_excerpt(str(item))
    return result


class PolicyEvidenceCipher:
    def __init__(self, key: bytes | None) -> None:
        self._key = bytes(key or b"")
        self.available = AESGCM is not None and len(self._key) == 32

    def encrypt(self, plaintext: str, *, aad: str) -> str:
        if not self.available:
            return ""
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, plaintext.encode(), aad.encode())
        return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, ciphertext: str, *, aad: str) -> str:
        if not self.available or not ciphertext:
            return ""
        try:
            raw = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
            return AESGCM(self._key).decrypt(raw[:12], raw[12:], aad.encode()).decode()
        except Exception:
            return ""

    def fingerprint(self, plaintext: str) -> str:
        if not self.available or not plaintext:
            return ""
        return hmac.new(
            self._key,
            plaintext.encode("utf-8", "surrogatepass"),
            hashlib.sha256,
        ).hexdigest()


def load_or_create_policy_evidence_key(data_dir: str | Path) -> bytes | None:
    path = Path(data_dir) / "policy_evidence.key"
    try:
        if path.is_file():
            key = base64.urlsafe_b64decode(path.read_bytes().strip())
            return key if len(key) == 32 else None
        path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            loaded = base64.urlsafe_b64decode(path.read_bytes().strip())
            return loaded if len(loaded) == 32 else None
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.urlsafe_b64encode(key))
            handle.flush()
            os.fsync(handle.fileno())
        return key
    except Exception:
        return None


class UserPolicyService:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        evidence_key: bytes | None = None,
        classifier: Any = None,
        logger: Any = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self.evidence_cipher = PolicyEvidenceCipher(evidence_key)
        self.classifier = classifier
        self.logger = logger

    def _connect(self):  # noqa: ANN202
        return connect_sync(self.db_path or get_db_path())

    @staticmethod
    def _default_state(user_id: str, now: float) -> PolicyState:
        return PolicyState(user_id=user_id, created_at=now, updated_at=now)

    @classmethod
    def _state_from_row(cls, row: Any, user_id: str, now: float) -> PolicyState:
        if row is None:
            return cls._default_state(user_id, now)
        auto_tier = str(row["auto_tier"] or "allow")
        manual_mode = str(row["manual_mode"] or "inherit")
        return PolicyState(
            user_id=str(row["user_id"] or user_id),
            auto_stage=max(0, min(3, int(row["auto_stage"] or 0))),
            auto_tier=auto_tier if auto_tier in POLICY_TIERS else "allow",
            auto_expires_at=float(row["auto_expires_at"] or 0),
            violation_count=max(0, int(row["violation_count"] or 0)),
            last_violation_at=float(row["last_violation_at"] or 0),
            manual_mode=manual_mode if manual_mode in POLICY_MANUAL_MODES else "inherit",
            manual_expires_at=float(row["manual_expires_at"] or 0),
            reason_code=str(row["reason_code"] or ""),
            revision=max(0, int(row["revision"] or 0)),
            created_at=float(row["created_at"] or now),
            updated_at=float(row["updated_at"] or now),
            updated_by=str(row["updated_by"] or ""),
        )

    @staticmethod
    def _normalize_expiry(state: PolicyState, now: float) -> tuple[PolicyState, bool]:
        manual_expired = (
            state.manual_mode in {"allow", "block"}
            and state.manual_expires_at > 0
            and state.manual_expires_at <= now
        )
        auto_reset = (
            state.last_violation_at > 0
            and now - state.last_violation_at >= POLICY_AUTO_RESET_SECONDS
            and (state.auto_stage > 0 or state.violation_count > 0 or state.auto_tier != "allow")
        )
        if not manual_expired and not auto_reset:
            return state, False
        return replace(
            state,
            auto_stage=0 if auto_reset else state.auto_stage,
            auto_tier="allow" if auto_reset else state.auto_tier,
            auto_expires_at=0.0 if auto_reset else state.auto_expires_at,
            violation_count=0 if auto_reset else state.violation_count,
            last_violation_at=0.0 if auto_reset else state.last_violation_at,
            manual_mode="inherit" if manual_expired else state.manual_mode,
            manual_expires_at=0.0 if manual_expired else state.manual_expires_at,
            reason_code="auto_reset" if auto_reset else state.reason_code,
            revision=state.revision + 1,
            updated_at=now,
            updated_by="policy_expiry",
        ), True

    @staticmethod
    def _save_state(conn: Any, state: PolicyState) -> None:
        conn.execute(
            """
            INSERT INTO user_policy_state(
                user_id,auto_stage,auto_tier,auto_expires_at,violation_count,
                last_violation_at,manual_mode,manual_expires_at,reason_code,
                revision,created_at,updated_at,updated_by
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                auto_stage=excluded.auto_stage,auto_tier=excluded.auto_tier,
                auto_expires_at=excluded.auto_expires_at,violation_count=excluded.violation_count,
                last_violation_at=excluded.last_violation_at,manual_mode=excluded.manual_mode,
                manual_expires_at=excluded.manual_expires_at,reason_code=excluded.reason_code,
                revision=excluded.revision,updated_at=excluded.updated_at,updated_by=excluded.updated_by
            """,
            (
                state.user_id, state.auto_stage, state.auto_tier,
                state.auto_expires_at, state.violation_count,
                state.last_violation_at, state.manual_mode,
                state.manual_expires_at, state.reason_code, state.revision,
                state.created_at, state.updated_at, state.updated_by,
            ),
        )

    def get_state(self, user_id: str, *, now: float | None = None) -> PolicyState:
        uid = str(user_id or "").strip()
        current = float(now if now is not None else time.time())
        if not uid:
            return self._default_state("", current)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM user_policy_state WHERE user_id=?", (uid,)).fetchone()
            state, changed = self._normalize_expiry(self._state_from_row(row, uid, current), current)
            if changed and row is not None:
                conn.execute("BEGIN IMMEDIATE")
                locked_row = conn.execute(
                    "SELECT * FROM user_policy_state WHERE user_id=?", (uid,)
                ).fetchone()
                state, changed = self._normalize_expiry(
                    self._state_from_row(locked_row, uid, current), current
                )
                if changed and locked_row is not None:
                    self._save_state(conn, state)
                conn.commit()
            return state

    def authorize(self, user_id: str, *, now: float | None = None) -> PolicyAuthorization:
        state = self.get_state(user_id, now=now)
        blocked = state.is_blocked(now=now)
        allowed = not blocked
        return PolicyAuthorization(
            blocked, state.effective_tier(now=now),
            allowed, allowed, allowed, allowed, allowed,
            allowed, allowed, allowed, allowed, allowed,
        )

    @staticmethod
    def _advance(
        state: PolicyState,
        assessment: PolicyAssessment,
        *,
        now: float,
        actor: str,
    ) -> tuple[PolicyState, bool, bool]:
        if not assessment.is_violation or state.is_blocked(now=now):
            return state, False, False
        stage, count = state.auto_stage, state.violation_count
        tier, expires_at, escalated = state.auto_tier, state.auto_expires_at, False
        if stage <= 0:
            if assessment.verdict == "critical_violation" or assessment.severity == "critical":
                stage, count, tier, expires_at, escalated = 1, 0, "level_1", now + POLICY_LEVEL_1_SECONDS, True
            else:
                count += 1
                if count >= 3:
                    stage, count, tier, expires_at, escalated = 1, 0, "level_1", now + POLICY_LEVEL_1_SECONDS, True
        elif stage == 1:
            stage, count, tier, expires_at, escalated = 2, 0, "level_2", now + POLICY_LEVEL_2_SECONDS, True
        elif stage == 2:
            stage, count, tier, expires_at, escalated = 3, 0, "permanent", 0.0, True
        return replace(
            state,
            auto_stage=stage,
            auto_tier=tier,
            auto_expires_at=expires_at,
            violation_count=count,
            last_violation_at=now,
            reason_code=assessment.reason_code,
            revision=state.revision + 1,
            created_at=state.created_at or now,
            updated_at=now,
            updated_by=actor or "policy_classifier",
        ), escalated, True

    def apply_assessment(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        surface: str,
        assessment: PolicyAssessment,
        content: str = "",
        actor: str = "policy_classifier",
        metadata: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> PolicyEventResult:
        uid = str(user_id or "").strip()
        raw_event_key = str(idempotency_key or "").strip()
        event_key = raw_event_key
        if len(event_key) > 160:
            event_key = f"{event_key[:95]}:{hashlib.sha256(event_key.encode()).hexdigest()}"
        current = float(now if now is not None else time.time())
        normalized = normalize_policy_assessment(
            assessment.__dict__ if isinstance(assessment, PolicyAssessment) else assessment
        )
        if not uid or not event_key or normalized.verdict == "allow":
            return PolicyEventResult(self.get_state(uid, now=current), normalized)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM user_policy_events WHERE idempotency_key=?", (event_key,)
            ).fetchone()
            row = conn.execute("SELECT * FROM user_policy_state WHERE user_id=?", (uid,)).fetchone()
            state, reset_changed = self._normalize_expiry(
                self._state_from_row(row, uid, current), current
            )
            if existing is not None:
                if reset_changed and row is not None:
                    self._save_state(conn, state)
                conn.commit()
                return PolicyEventResult(state, normalized, duplicate=True)
            state, escalated, counted = self._advance(
                state, normalized, now=current, actor=actor
            )
            if counted or (reset_changed and row is not None):
                self._save_state(conn, state)
            content_hash = (
                self.evidence_cipher.fingerprint(str(content or ""))
                if normalized.is_violation
                else ""
            )
            conn.execute(
                """
                INSERT INTO user_policy_events(
                    idempotency_key,user_id,event_kind,surface,verdict,category,intent,
                    severity,confidence,reason_code,counts_violation,resulting_tier,
                    resulting_expires_at,content_hash,classifier_version,actor,
                    metadata_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_key, uid, "escalation" if escalated else "assessment",
                    str(surface or "")[:48], normalized.verdict, normalized.category,
                    normalized.intent, normalized.severity, normalized.confidence,
                    normalized.reason_code, 1 if counted else 0,
                    state.effective_tier(now=current), state.auto_expires_at,
                    content_hash, normalized.classifier_version, str(actor or "")[:64],
                    json.dumps(
                        _sanitize_policy_metadata(metadata),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ), current,
                ),
            )
            excerpt = _redact_policy_excerpt(content) if normalized.is_violation else ""
            ciphertext = self.evidence_cipher.encrypt(excerpt, aad=event_key) if excerpt else ""
            if ciphertext:
                conn.execute(
                    """
                    INSERT INTO user_policy_evidence(
                        idempotency_key,ciphertext,key_version,expires_at,created_at
                    ) VALUES(?,?,1,?,?)
                    """,
                    (event_key, ciphertext, current + POLICY_EVIDENCE_RETENTION_SECONDS, current),
                )
            conn.commit()
        return PolicyEventResult(state, normalized, escalated=escalated, counts_violation=counted)

    def set_manual_override(
        self,
        *,
        user_id: str,
        mode: str,
        actor: str,
        reason_code: str = "manual_override",
        expires_at: float = 0.0,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> PolicyState:
        uid, normalized_mode = str(user_id or "").strip(), str(mode or "").strip().lower()
        if not uid or normalized_mode not in POLICY_MANUAL_MODES:
            raise ValueError("invalid policy override")
        current = float(now if now is not None else time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM user_policy_state WHERE user_id=?", (uid,)).fetchone()
            state, _ = self._normalize_expiry(self._state_from_row(row, uid, current), current)
            if expected_revision is not None and state.revision != int(expected_revision):
                conn.rollback()
                raise PolicyRevisionConflict(f"expected revision {expected_revision}, got {state.revision}")
            state = replace(
                state,
                manual_mode=normalized_mode,
                manual_expires_at=max(0.0, float(expires_at or 0)),
                reason_code=str(reason_code or "manual_override")[:64],
                revision=state.revision + 1,
                created_at=state.created_at or current,
                updated_at=current,
                updated_by=str(actor or "admin")[:64],
            )
            self._save_state(conn, state)
            self._insert_control_event(conn, state, current, "manual_override")
            conn.commit()
            return state

    @staticmethod
    def _insert_control_event(conn: Any, state: PolicyState, now: float, kind: str) -> None:
        conn.execute(
            """
            INSERT INTO user_policy_events(
                idempotency_key,user_id,event_kind,surface,verdict,category,intent,
                severity,confidence,reason_code,counts_violation,resulting_tier,
                resulting_expires_at,content_hash,classifier_version,actor,
                metadata_json,created_at
            ) VALUES(?,?,?,'control','','none','ordinary','none',1,?,0,?,?,'',?,?,?,?)
            """,
            (
                f"{kind}:{state.user_id}:{state.revision}", state.user_id, kind,
                state.reason_code, state.effective_tier(now=now),
                state.manual_expires_at, POLICY_CLASSIFIER_VERSION,
                state.updated_by, "{}", now,
            ),
        )

    def clear_auto(
        self,
        *,
        user_id: str,
        actor: str,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> PolicyState:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("invalid policy user")
        current = float(now if now is not None else time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM user_policy_state WHERE user_id=?", (uid,)).fetchone()
            state = self._state_from_row(row, uid, current)
            if expected_revision is not None and state.revision != int(expected_revision):
                conn.rollback()
                raise PolicyRevisionConflict(f"expected revision {expected_revision}, got {state.revision}")
            state = replace(
                state,
                auto_stage=0, auto_tier="allow", auto_expires_at=0.0,
                violation_count=0, last_violation_at=0.0,
                reason_code="auto_cleared", revision=state.revision + 1,
                created_at=state.created_at or current, updated_at=current,
                updated_by=str(actor or "admin")[:64],
            )
            self._save_state(conn, state)
            self._insert_control_event(conn, state, current, "auto_clear")
            conn.commit()
            return state

    def claim_direct_closure(
        self,
        *,
        user_id: str,
        channel_key: str,
        event_key: str,
        now: float | None = None,
        cooldown_seconds: float = POLICY_DIRECT_CLOSURE_COOLDOWN_SECONDS,
    ) -> bool:
        uid = str(user_id or "").strip()
        channel = str(channel_key or "").strip()[:160]
        key = str(event_key or "").strip()[:160]
        if not uid or not channel or not key:
            return False
        current = float(now if now is not None else time.time())
        expires_at = current + max(1.0, float(cooldown_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM user_policy_closures WHERE expires_at<=?", (current,))
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO user_policy_closures(
                    user_id,channel_key,event_key,claimed_at,expires_at
                ) VALUES(?,?,?,?,?)
                """,
                (uid, channel, key, current, expires_at),
            )
            claimed = int(cursor.rowcount or 0) > 0
            conn.commit()
        return claimed

    def list_states(self, *, limit: int = 200, now: float | None = None) -> list[dict[str, Any]]:
        current = float(now if now is not None else time.time())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM user_policy_state ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [
            self.get_state(str(row["user_id"]), now=current).to_dict(now=current)
            for row in rows
        ]

    def list_events(
        self,
        user_id: str,
        *,
        limit: int = 100,
        include_evidence: bool = False,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        uid = str(user_id or "").strip()
        current = float(now if now is not None else time.time())
        with self._connect() as conn:
            conn.execute("DELETE FROM user_policy_evidence WHERE expires_at<=?", (current,))
            rows = conn.execute(
                "SELECT * FROM user_policy_events WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (uid, max(1, min(int(limit), 500))),
            ).fetchall()
            evidence: dict[str, Any] = {}
            if include_evidence and rows:
                keys = [str(row["idempotency_key"]) for row in rows]
                placeholders = ",".join("?" for _ in keys)
                found = conn.execute(
                    f"SELECT * FROM user_policy_evidence WHERE idempotency_key IN ({placeholders})",
                    tuple(keys),
                ).fetchall()
                evidence = {str(row["idempotency_key"]): row for row in found}
            conn.commit()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
            except Exception:
                item["metadata"] = {}
            if include_evidence:
                stored = evidence.get(str(item.get("idempotency_key", "")))
                item["evidence_excerpt"] = (
                    self.evidence_cipher.decrypt(
                        str(stored["ciphertext"]), aad=str(item["idempotency_key"])
                    )
                    if stored is not None and float(stored["expires_at"] or 0) > current
                    else ""
                )
            result.append(item)
        return result


__all__ = [
    "POLICY_AUTO_RESET_SECONDS", "POLICY_CLASSIFIER_VERSION",
    "POLICY_DIRECT_CLOSURE_COOLDOWN_SECONDS",
    "POLICY_EVIDENCE_RETENTION_SECONDS", "POLICY_LEVEL_1_SECONDS",
    "POLICY_LEVEL_2_SECONDS", "PolicyAssessment", "PolicyAuthorization",
    "PolicyEventResult", "PolicyEvidenceCipher", "PolicyRevisionConflict",
    "PolicyState", "UserPolicyService", "load_or_create_policy_evidence_key",
    "normalize_policy_assessment",
]
