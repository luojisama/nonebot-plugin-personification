from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
qq_policy = load_personification_module("plugin.personification.core.qq_user_policy")
user_policy = load_personification_module("plugin.personification.core.user_policy")


@dataclass
class _Segment:
    type: str
    data: dict[str, str]


class _Event:
    def __init__(
        self,
        message_id: int,
        text: str,
        *,
        user_id: str = "10001",
        group_id: str | None = "20001",
        to_me: bool = False,
        at_bot: bool = False,
    ) -> None:
        self.message_id = message_id
        self.user_id = user_id
        self.self_id = "bot-1"
        self.to_me = to_me
        self.reply = None
        self._text = text
        self.message = [_Segment("text", {"text": text})] if text else []
        if at_bot:
            self.message.insert(0, _Segment("at", {"qq": "bot-1"}))
        if group_id is not None:
            self.group_id = group_id

    def get_plaintext(self) -> str:
        return self._text


class _Classifier:
    def __init__(self, assessment) -> None:  # noqa: ANN001
        self.assessment = assessment
        self.calls = 0

    async def classify(self, _event):  # noqa: ANN001, ANN201
        self.calls += 1
        await asyncio.sleep(0.01)
        return self.assessment


def _assessment(verdict: str = "allow"):  # noqa: ANN202
    if verdict == "allow":
        return user_policy.PolicyAssessment(
            verdict="allow",
            category="none",
            intent="ordinary",
            severity="none",
            confidence=0.99,
            reason_code="ordinary",
            confirmed=True,
        )
    return user_policy.PolicyAssessment(
        verdict="boundary_topic",
        category="political_sensitive",
        intent="neutral_mention",
        severity="low",
        confidence=0.95,
        reason_code="boundary_topic",
        confirmed=True,
    )


def _gate(tmp_path, assessment=None):  # noqa: ANN001, ANN202
    db_path = db.init_db_sync(tmp_path)
    classifier = _Classifier(assessment or _assessment())
    service = user_policy.UserPolicyService(
        db_path=db_path,
        evidence_key=b"q" * 32,
        classifier=classifier,
    )
    return qq_policy.QQUserPolicyGate(service), service, classifier


def test_concurrent_rules_share_one_classifier_call(tmp_path) -> None:  # noqa: ANN001
    async def run() -> None:
        gate, _service, classifier = _gate(tmp_path)
        event = _Event(1, "普通聊天")

        decisions = await asyncio.gather(
            *(gate.evaluate(event, bot_self_id="bot-1") for _ in range(12))
        )

        assert classifier.calls == 1
        assert all(item.allow_normal_processing for item in decisions)
        assert len(gate._inflight) == 0

    asyncio.run(run())


def test_active_blacklist_skips_classifier_and_fails_closed(tmp_path) -> None:  # noqa: ANN001
    async def run() -> None:
        gate, service, classifier = _gate(tmp_path)
        service.set_manual_override(
            user_id="10001",
            mode="block",
            actor="admin",
            now=1000,
        )

        decision = await gate.evaluate(_Event(1, "不应送入模型"), bot_self_id="bot-1")

        assert decision.disposition == qq_policy.QQ_POLICY_SILENT
        assert decision.authorization.blocked is True
        assert classifier.calls == 0

    asyncio.run(run())


def test_legacy_permanent_blacklist_skips_classifier(tmp_path) -> None:  # noqa: ANN001
    async def run() -> None:
        db_path = db.init_db_sync(tmp_path)
        classifier = _Classifier(_assessment())
        service = user_policy.UserPolicyService(
            db_path=db_path,
            evidence_key=b"q" * 32,
            classifier=classifier,
        )
        gate = qq_policy.QQUserPolicyGate(
            service,
            legacy_block_checker=lambda user_id: user_id == "10001",
        )

        decision = await gate.evaluate(_Event(1, "不应分类"), bot_self_id="bot-1")

        assert decision.disposition == qq_policy.QQ_POLICY_SILENT
        assert decision.authorization.tier == "legacy_permanent"
        assert classifier.calls == 0

    asyncio.run(run())


def test_unaddressed_boundary_is_silent_and_direct_closure_is_one_shot(tmp_path) -> None:  # noqa: ANN001
    async def run() -> None:
        gate, _service, classifier = _gate(tmp_path, _assessment("boundary"))

        unaddressed = await gate.evaluate(_Event(1, "边界讨论"), bot_self_id="bot-1")
        first_direct = await gate.claim_direct_closure(
            await gate.evaluate(
                _Event(2, "要求参与", at_bot=True),
                bot_self_id="bot-1",
            )
        )
        repeated_direct = await gate.claim_direct_closure(
            await gate.evaluate(
                _Event(3, "继续要求参与", to_me=True),
                bot_self_id="bot-1",
            )
        )
        private_direct = await gate.claim_direct_closure(
            await gate.evaluate(
                _Event(4, "私聊要求参与", group_id=None),
                bot_self_id="bot-1",
            )
        )

        assert unaddressed.disposition == qq_policy.QQ_POLICY_SILENT
        assert first_direct.disposition == qq_policy.QQ_POLICY_DIRECT_CLOSURE
        assert repeated_direct.disposition == qq_policy.QQ_POLICY_SILENT
        assert private_direct.disposition == qq_policy.QQ_POLICY_DIRECT_CLOSURE
        assert classifier.calls == 4

    asyncio.run(run())


def test_classifier_unavailable_fails_open_without_persisting_quarantine(tmp_path) -> None:  # noqa: ANN001
    async def run() -> None:
        unavailable = user_policy.PolicyAssessment(
            reason_code="classifier_unavailable",
            confirmed=False,
        )
        gate, service, _classifier = _gate(tmp_path, unavailable)

        private_decision = await gate.evaluate(
            _Event(50, "分类服务超时", group_id=None),
            bot_self_id="bot-1",
        )
        group_decision = await gate.evaluate(
            _Event(51, "分类服务仍不可用"),
            bot_self_id="bot-1",
        )

        assert private_decision.disposition == qq_policy.QQ_POLICY_ALLOW
        assert group_decision.disposition == qq_policy.QQ_POLICY_ALLOW
        assert private_decision.allow_normal_processing is True
        assert group_decision.allow_normal_processing is True
        assert private_decision.assessment.reason_code == "classifier_unavailable"
        assert group_decision.assessment.reason_code == "classifier_unavailable"

        with sqlite3.connect(service.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM user_policy_events").fetchone()[0] == 0

    asyncio.run(run())


def test_non_text_event_only_checks_existing_authorization(tmp_path) -> None:  # noqa: ANN001
    async def run() -> None:
        gate, _service, classifier = _gate(tmp_path, _assessment("boundary"))
        event = _Event(1, "")
        event.message = [_Segment("image", {"file": "opaque"})]

        decision = await gate.evaluate(event, bot_self_id="bot-1")

        assert decision.allow_normal_processing is True
        assert decision.assessment.reason_code == "non_text_event"
        assert classifier.calls == 0

    asyncio.run(run())


def test_bot_self_message_never_enters_user_classifier(tmp_path) -> None:  # noqa: ANN001
    async def run() -> None:
        gate, _service, classifier = _gate(tmp_path, _assessment("boundary"))
        event = _Event(1, "Bot 自己发出的消息", user_id="bot-1")

        decision = await gate.evaluate(event, bot_self_id="bot-1")

        assert decision.allow_normal_processing is True
        assert decision.assessment.reason_code == "bot_self_message"
        assert classifier.calls == 0

    asyncio.run(run())
