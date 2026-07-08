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


def test_active_followup_can_continue_related_target_others(monkeypatch) -> None:  # noqa: ANN001
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

    assert result is True
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
