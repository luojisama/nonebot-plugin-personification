from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from ._loader import load_personification_module

event_rules = load_personification_module("plugin.personification.handlers.event_rules")
target_inference = load_personification_module("plugin.personification.core.target_inference")


class _FakeLogger:
    def info(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def warning(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def debug(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        pass


class _GroupEvent:
    def __init__(self, text: str, *, user_id: str = "10001", self_id: str = "bot-1") -> None:
        self.user_id = user_id
        self.group_id = "20001"
        self.self_id = self_id
        self.to_me = False
        self.message = []
        self.reply = None
        self._text = text

    def get_plaintext(self) -> str:
        return self._text


class _PolicyDecision:
    def __init__(self, *, disposition: str, allowed: bool) -> None:
        self.disposition = disposition
        self.allow_normal_processing = allowed

    def to_dict(self) -> dict:
        return {"disposition": self.disposition}


class _PolicyGate:
    def __init__(self, decision: _PolicyDecision, *, current: bool = True) -> None:
        self.decision = decision
        self.current = current
        self.evaluate_calls = 0

    async def evaluate(self, _event, *, bot_self_id: str = ""):  # noqa: ANN001, ANN201
        self.evaluate_calls += 1
        return self.decision

    async def allows_current(self, _event) -> bool:  # noqa: ANN001
        return self.current


def _base_kwargs(**overrides):  # noqa: ANN001
    kwargs = {
        "sign_in_available": False,
        "get_user_data": lambda _uid: {},
        "user_blacklist": {},
        "logger": _FakeLogger(),
        "group_event_cls": _GroupEvent,
        "private_event_cls": SimpleNamespace,
        "is_group_whitelisted": lambda *_a, **_k: True,
        "plugin_whitelist": [],
        "load_prompt": lambda _gid: {},
        "load_proactive_state": lambda: {},
        "is_rest_time": lambda **_k: True,
        "probability": 0.30,
        "group_chat_follow_probability": 0.30,
        "looks_like_private_command": lambda _text: False,
        "get_recent_group_msgs": lambda _gid, _limit: [],
    }
    kwargs.update(overrides)
    return kwargs


def test_active_followup_does_not_override_structural_target_others(monkeypatch) -> None:  # noqa: ANN001
    event = _GroupEvent("这个话题继续聊")
    state: dict = {}
    now = time.time()

    monkeypatch.setattr(event_rules.random, "random", lambda: 0.5)
    monkeypatch.setattr(event_rules, "infer_message_target", lambda *_a, **_k: target_inference.TARGET_OTHERS)

    result = asyncio.run(
        event_rules.personification_rule(
            event,
            state,
            **_base_kwargs(
                load_proactive_state=lambda: {
                    "group_chat_active_20001": {
                        "until": now + 60,
                        "topic": "这个话题",
                        "last_user_id": "10001",
                    }
                },
                group_chat_follow_probability=0.30,
            ),
        )
    )

    assert result is False
    assert state["is_random_chat"] is False
    assert state["active_followup"]["topic"] == "这个话题"


def test_target_others_unrelated_still_does_not_trigger(monkeypatch) -> None:  # noqa: ANN001
    event = _GroupEvent("完全另一件事", user_id="10002")
    state: dict = {}
    now = time.time()

    monkeypatch.setattr(event_rules.random, "random", lambda: 0.0)
    monkeypatch.setattr(event_rules, "infer_message_target", lambda *_a, **_k: target_inference.TARGET_OTHERS)

    result = asyncio.run(
        event_rules.personification_rule(
            event,
            state,
            **_base_kwargs(
                load_proactive_state=lambda: {
                    "group_chat_active_20001": {
                        "until": now + 60,
                        "topic": "刚才的话题",
                        "last_user_id": "10001",
                    }
                },
                group_chat_follow_probability=1.0,
            ),
        )
    )

    assert result is False
    assert state["is_random_chat"] is False


def test_two_person_dialogue_replay_is_structurally_silent(monkeypatch) -> None:  # noqa: ANN001
    event = _GroupEvent("你也躺过去", user_id="u1")
    event.message_id = "m5"
    event.time = 1040
    state: dict = {}
    provider_calls: list[str] = []
    recent = [
        {
            "message_id": "m1",
            "user_id": "u1",
            "mentioned_ids": ["u2"],
            "source_kind": "user",
            "time": 1000,
        },
        {"message_id": "m2", "user_id": "u2", "content": "[图片]", "source_kind": "user", "time": 1010},
        {"message_id": "m3", "user_id": "u2", "content": "一墙之隔", "source_kind": "user", "time": 1020},
        {"message_id": "m4", "user_id": "u1", "content": "看路边有人躺着", "source_kind": "user", "time": 1030},
    ]
    monkeypatch.setattr(event_rules.random, "random", lambda: provider_calls.append("random") or 0.0)

    result = asyncio.run(
        event_rules.personification_rule(
            event,
            state,
            **_base_kwargs(
                probability=1.0,
                get_recent_group_msgs=lambda _gid, _limit: recent,
            ),
        )
    )

    assert result is False
    assert state["message_target"] == target_inference.TARGET_OTHERS
    assert state["message_target_reason"] == "carried_human_dialogue"
    assert state["message_target_anchor_id"] == "m1"
    assert provider_calls == []


def test_reply_to_bot_target_gets_structural_probability_boost(monkeypatch) -> None:  # noqa: ANN001
    event = _GroupEvent("那我接着问一句", user_id="10002")
    state: dict = {}

    monkeypatch.setattr(event_rules.random, "random", lambda: 0.55)
    monkeypatch.setattr(event_rules, "infer_message_target", lambda *_a, **_k: target_inference.TARGET_BOT)

    result = asyncio.run(
        event_rules.personification_rule(
            event,
            state,
            **_base_kwargs(probability=0.30),
        )
    )

    assert result is True
    assert state["is_random_chat"] is True
    assert state["message_target"] == target_inference.TARGET_BOT


def test_group_plugin_command_is_record_only_not_random_reply(monkeypatch) -> None:  # noqa: ANN001
    event = _GroupEvent("/天气 北京")
    state: dict = {}

    monkeypatch.setattr(event_rules.random, "random", lambda: 0.0)

    result = asyncio.run(
        event_rules.personification_rule(
            event,
            state,
            **_base_kwargs(probability=1.0),
        )
    )

    assert result is False
    assert state["is_random_chat"] is False
    assert state["message_target"] == target_inference.TARGET_OTHERS


def test_policy_gate_runs_before_prompt_history_and_favorability_reads() -> None:
    gate = _PolicyGate(_PolicyDecision(disposition="silent", allowed=False))
    touched: list[str] = []
    event = _GroupEvent("blocked")

    result = asyncio.run(
        event_rules.personification_rule(
            event,
            {},
            **_base_kwargs(
                sign_in_available=True,
                get_user_data=lambda _uid: touched.append("favorability") or {},
                load_prompt=lambda _gid: touched.append("prompt") or {},
                get_recent_group_msgs=lambda _gid, _limit: touched.append("history") or [],
                user_policy_gate=gate,
            ),
        )
    )

    assert result is False
    assert touched == []
    assert gate.evaluate_calls == 1


def test_direct_boundary_routes_only_to_dedicated_closure() -> None:
    gate = _PolicyGate(_PolicyDecision(disposition="direct_closure", allowed=False))
    event = _GroupEvent("direct boundary")
    state: dict = {}

    result = asyncio.run(
        event_rules.personification_rule(
            event,
            state,
            **_base_kwargs(user_policy_gate=gate),
        )
    )

    assert result is True
    assert state["user_policy_decision"] == {"disposition": "direct_closure"}


def test_record_sticker_and_poke_rules_fail_closed_for_policy() -> None:
    denied = _PolicyGate(_PolicyDecision(disposition="silent", allowed=False), current=False)
    event = _GroupEvent("blocked")
    event.target_id = event.self_id
    event.notice_type = "notify"
    event.sub_type = "poke"

    record = asyncio.run(event_rules.record_msg_rule(event, user_policy_gate=denied))
    sticker = asyncio.run(
        event_rules.sticker_chat_rule(
            event,
            is_group_whitelisted=lambda *_args: True,
            plugin_whitelist=[],
            probability=1.0,
            user_policy_gate=denied,
        )
    )
    poke = asyncio.run(
        event_rules.poke_notice_rule(
            event,
            is_group_whitelisted=lambda *_args: True,
            plugin_whitelist=[],
            probability=1.0,
            logger=_FakeLogger(),
            user_policy_gate=denied,
        )
    )

    assert record is False
    assert sticker is False
    assert poke is False


def test_private_event_never_enters_group_record_rule() -> None:
    event = SimpleNamespace(
        user_id="10001",
        self_id="bot-1",
        get_plaintext=lambda: "private",
    )
    gate = _PolicyGate(_PolicyDecision(disposition="allow", allowed=True))

    result = asyncio.run(event_rules.record_msg_rule(event, user_policy_gate=gate))

    assert result is False
    assert gate.evaluate_calls == 0


def test_direct_closure_does_not_bypass_whitelist_or_own_private_commands() -> None:
    gate = _PolicyGate(_PolicyDecision(disposition="direct_closure", allowed=False))
    group_result = asyncio.run(
        event_rules.personification_rule(
            _GroupEvent("boundary"),
            {},
            **_base_kwargs(
                user_policy_gate=gate,
                is_group_whitelisted=lambda *_args: False,
            ),
        )
    )
    private_event = SimpleNamespace(
        user_id="10001",
        self_id="bot-1",
        get_plaintext=lambda: "/other command",
    )
    private_result = asyncio.run(
        event_rules.personification_rule(
            private_event,
            {},
            **_base_kwargs(
                user_policy_gate=gate,
                private_event_cls=SimpleNamespace,
                looks_like_private_command=lambda text: text.startswith("/"),
            ),
        )
    )

    assert group_result is False
    assert private_result is False
