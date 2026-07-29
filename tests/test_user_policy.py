from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
user_policy = load_personification_module("plugin.personification.core.user_policy")


@pytest.fixture
def service(tmp_path):  # noqa: ANN001, ANN201
    db_path = db.init_db_sync(tmp_path)
    return user_policy.UserPolicyService(db_path=db_path, evidence_key=b"p" * 32), db_path


def _violation(*, critical: bool = False):  # noqa: ANN202
    return user_policy.PolicyAssessment(
        verdict="critical_violation" if critical else "confirmed_violation",
        category="threat" if critical else "harassment",
        intent="credible_threat" if critical else "targeted_attack",
        severity="critical" if critical else "high",
        confidence=0.96,
        reason_code="credible_threat" if critical else "repeated_targeted_abuse",
        confirmed=True,
    )


def _apply(service, event_id: str, *, now: float, critical: bool = False):  # noqa: ANN001, ANN202
    return service.apply_assessment(
        user_id="10001",
        idempotency_key=event_id,
        surface="qq_group",
        assessment=_violation(critical=critical),
        content="待审事件",
        now=now,
    )


def test_policy_tables_and_default_authorization(service) -> None:  # noqa: ANN001
    policy, db_path = service

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'user_policy_%'"
            )
        }

    authorization = policy.authorize("10001", now=1000)
    assert tables == {
        "user_policy_state",
        "user_policy_events",
        "user_policy_evidence",
        "user_policy_closures",
    }
    assert authorization.blocked is False
    assert authorization.tier == "allow"
    assert all(
        (
            authorization.allow_reply,
            authorization.allow_visible_reaction,
            authorization.allow_agent_action,
            authorization.allow_proactive,
            authorization.allow_qzone,
            authorization.allow_profile_write,
            authorization.allow_history_write,
            authorization.allow_memory_write,
            authorization.allow_relation_write,
            authorization.allow_context_read,
        )
    )


def test_three_strikes_then_reoffence_escalates_and_auto_resets(service) -> None:  # noqa: ANN001
    policy, _ = service
    started = 1_000_000.0

    first = _apply(policy, "event-1", now=started)
    second = _apply(policy, "event-2", now=started + 1)
    third = _apply(policy, "event-3", now=started + 2)
    duplicate = _apply(policy, "event-3", now=started + 3)

    assert first.state.violation_count == 1
    assert second.state.violation_count == 2
    assert third.escalated is True
    assert third.state.auto_stage == 1
    assert third.state.auto_tier == "level_1"
    assert third.state.auto_expires_at == started + 2 + user_policy.POLICY_LEVEL_1_SECONDS
    assert duplicate.duplicate is True
    assert duplicate.state.revision == third.state.revision

    blocked_attempt = _apply(policy, "event-blocked", now=started + 4)
    assert blocked_attempt.counts_violation is False
    assert blocked_attempt.state.auto_stage == 1

    level_2_at = third.state.auto_expires_at + 1
    level_2 = _apply(policy, "event-4", now=level_2_at)
    assert level_2.state.auto_stage == 2
    assert level_2.state.auto_tier == "level_2"
    assert level_2.state.auto_expires_at == level_2_at + user_policy.POLICY_LEVEL_2_SECONDS

    permanent_at = level_2.state.auto_expires_at + 1
    permanent = _apply(policy, "event-5", now=permanent_at)
    assert permanent.state.auto_stage == 3
    assert permanent.state.auto_tier == "permanent"
    assert permanent.state.is_blocked(now=permanent_at) is True

    reset = policy.get_state(
        "10001",
        now=permanent_at + user_policy.POLICY_AUTO_RESET_SECONDS + 1,
    )
    assert reset.auto_stage == 0
    assert reset.auto_tier == "allow"
    assert reset.violation_count == 0
    assert reset.is_blocked(now=reset.updated_at) is False


def test_critical_first_event_can_only_enter_level_1(service) -> None:  # noqa: ANN001
    policy, _ = service

    result = _apply(policy, "critical-1", now=2_000_000, critical=True)

    assert result.escalated is True
    assert result.state.auto_stage == 1
    assert result.state.auto_tier == "level_1"
    assert result.state.auto_expires_at == 2_000_000 + user_policy.POLICY_LEVEL_1_SECONDS


def test_manual_override_is_revision_guarded_and_does_not_auto_reset(service) -> None:  # noqa: ANN001
    policy, _ = service

    blocked = policy.set_manual_override(
        user_id="10001",
        mode="block",
        actor="admin:42",
        reason_code="admin_permanent",
        now=1000,
    )
    assert blocked.manual_mode == "block"
    assert blocked.is_blocked(now=1000 + user_policy.POLICY_AUTO_RESET_SECONDS * 2) is True

    with pytest.raises(user_policy.PolicyRevisionConflict):
        policy.set_manual_override(
            user_id="10001",
            mode="allow",
            actor="admin:42",
            expected_revision=blocked.revision - 1,
            now=1001,
        )

    allowed = policy.set_manual_override(
        user_id="10001",
        mode="allow",
        actor="admin:42",
        expected_revision=blocked.revision,
        now=1002,
    )
    assert allowed.is_blocked(now=1002) is False
    assert allowed.revision == blocked.revision + 1


def test_evidence_is_encrypted_redacted_and_expires(service) -> None:  # noqa: ANN001
    policy, db_path = service
    raw = "token=top-secret https://example.com/path 联系 123456789，后续说明"

    policy.apply_assessment(
        user_id="10001",
        idempotency_key="encrypted-event",
        surface="qq_private",
        assessment=_violation(),
        content=raw,
        metadata={
            "group_id": "20001",
            "is_direct": True,
            "content": raw,
            "token": "metadata-secret",
        },
        now=1000,
    )

    with sqlite3.connect(db_path) as conn:
        ciphertext = conn.execute(
            "SELECT ciphertext FROM user_policy_evidence WHERE idempotency_key='encrypted-event'"
        ).fetchone()[0]
    assert "top-secret" not in ciphertext
    assert "example.com" not in ciphertext
    assert "123456789" not in ciphertext

    visible = policy.list_events("10001", include_evidence=True, now=1001)
    assert visible[0]["evidence_excerpt"] == "[凭证] [链接] 联系 [编号]，后续说明"
    assert visible[0]["metadata"] == {"group_id": "20001", "is_direct": True}

    expired = policy.list_events(
        "10001",
        include_evidence=True,
        now=1000 + user_policy.POLICY_EVIDENCE_RETENTION_SECONDS + 1,
    )
    assert expired[0]["evidence_excerpt"] == ""


def test_evidence_fails_closed_without_valid_encryption_key(tmp_path) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    policy = user_policy.UserPolicyService(db_path=db_path, evidence_key=b"short")

    policy.apply_assessment(
        user_id="10001",
        idempotency_key="no-key-event",
        surface="qq_group",
        assessment=_violation(),
        content="不得明文落盘",
        now=1000,
    )

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM user_policy_evidence").fetchone()[0]
    assert count == 0


def test_boundary_topic_never_persists_content_evidence(service) -> None:  # noqa: ANN001
    policy, db_path = service
    assessment = user_policy.PolicyAssessment(
        verdict="boundary_topic",
        category="political_sensitive",
        intent="neutral_mention",
        severity="low",
        confidence=0.93,
        reason_code="ordinary_mention",
        confirmed=True,
    )

    policy.apply_assessment(
        user_id="10001",
        idempotency_key="boundary-event",
        surface="qq_group",
        assessment=assessment,
        content="普通讨论不应保存",
        now=1000,
    )

    with sqlite3.connect(db_path) as conn:
        event = conn.execute(
            "SELECT content_hash FROM user_policy_events WHERE idempotency_key='boundary-event'"
        ).fetchone()
        evidence_count = conn.execute("SELECT COUNT(*) FROM user_policy_evidence").fetchone()[0]
    assert event[0] == ""
    assert evidence_count == 0


def test_concurrent_idempotency_and_distinct_events_are_atomic(service) -> None:  # noqa: ANN001
    policy, _ = service

    with ThreadPoolExecutor(max_workers=12) as pool:
        duplicates = list(
            pool.map(lambda _index: _apply(policy, "same-event", now=1000), range(24))
        )

    assert sum(not item.duplicate for item in duplicates) == 1
    assert policy.get_state("10001", now=1000).violation_count == 1
    assert len(policy.list_events("10001", now=1000)) == 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda item: _apply(policy, item[0], now=item[1]),
                (("distinct-1", 1001), ("distinct-2", 1002)),
            )
        )

    state = policy.get_state("10001", now=1003)
    assert len(results) == 2
    assert state.auto_stage == 1
    assert state.auto_tier == "level_1"
    assert len(policy.list_events("10001", now=1003)) == 3


def test_direct_closure_claim_is_atomic_and_expires(service) -> None:  # noqa: ANN001
    policy, _ = service

    with ThreadPoolExecutor(max_workers=12) as pool:
        claims = list(
            pool.map(
                lambda index: policy.claim_direct_closure(
                    user_id="10001",
                    channel_key="qq_group:20001",
                    event_key=f"event-{index}",
                    now=1000,
                    cooldown_seconds=30,
                ),
                range(24),
            )
        )

    assert sum(claims) == 1
    assert policy.claim_direct_closure(
        user_id="10001",
        channel_key="qq_group:20001",
        event_key="before-expiry",
        now=1029,
        cooldown_seconds=30,
    ) is False
    assert policy.claim_direct_closure(
        user_id="10001",
        channel_key="qq_group:20001",
        event_key="after-expiry",
        now=1031,
        cooldown_seconds=30,
    ) is True
