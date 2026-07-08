from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ._loader import load_personification_module


def _load_proactive_flow():
    for name in [
        "plugin.personification.core.db",
        "plugin.personification.core.data_store",
        "plugin.personification.core.context_policy",
        "plugin.personification.core.emotion_state",
        "plugin.personification.core.session_store",
        "plugin.personification.core.qq_expression_library",
        "plugin.personification.core.time_ctx",
        "plugin.personification.agent.inner_state",
        "plugin.personification.agent.tool_registry",
    ]:
        load_personification_module(name)
    return load_personification_module("plugin.personification.flows.proactive_flow")


def test_proactive_messages_include_shared_time_context() -> None:
    proactive_flow = _load_proactive_flow()
    now = datetime(2026, 6, 24, 21, 35, tzinfo=ZoneInfo("Asia/Shanghai"))

    messages = proactive_flow._build_time_anchored_messages(
        system_prompt="角色设定",
        user_prompt="主动私聊决策",
        now=now,
    )

    assert messages[0]["role"] == "system"
    assert "[personification:current_time_context]" in messages[0]["content"]
    assert "2026-06-24 21:35:00" in messages[0]["content"]
    assert "晚上" in messages[0]["content"]
    assert messages[1:] == [
        {"role": "system", "content": "角色设定"},
        {"role": "user", "content": "主动私聊决策"},
    ]


def test_proactive_time_context_injection_is_idempotent() -> None:
    proactive_flow = _load_proactive_flow()
    now = datetime(2026, 6, 24, 8, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

    once = proactive_flow._build_time_anchored_messages(
        system_prompt="角色设定",
        user_prompt="主动私聊决策",
        now=now,
    )
    twice = proactive_flow.inject_current_time_context(once, now=now)

    assert twice == once
