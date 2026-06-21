from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ._loader import load_personification_module


def _load_si_submodule(suffix: str):
    return load_personification_module(
        f"plugin.personification.flows.social_intelligence.{suffix}"
    )


# 预加载 data_store（quota 模块用相对 import 引用），保证 sys.modules 有它
ds_mod = load_personification_module("plugin.personification.core.data_store")
framework = _load_si_submodule("framework")
quota = _load_si_submodule("quota")
gate = _load_si_submodule("gate")


# ========== framework ==========

def test_register_and_list_triggers() -> None:
    framework.clear_social_triggers_for_testing()
    t1 = framework.SocialTrigger(
        name="x", handler=lambda ctx: None, schedule_kind="cron", schedule_args={"hour": 8}
    )
    t2 = framework.SocialTrigger(
        name="y", handler=lambda ctx: None, schedule_kind="interval", schedule_args={"minutes": 30}
    )
    framework.register_social_trigger(t1)
    framework.register_social_trigger(t2)
    names = sorted(t.name for t in framework.list_social_triggers())
    assert names == ["x", "y"]


def test_register_same_name_overwrites() -> None:
    framework.clear_social_triggers_for_testing()
    framework.register_social_trigger(
        framework.SocialTrigger("a", lambda ctx: None, "cron", {"hour": 1})
    )
    framework.register_social_trigger(
        framework.SocialTrigger("a", lambda ctx: None, "cron", {"hour": 9})
    )
    triggers = framework.list_social_triggers()
    assert len(triggers) == 1
    assert triggers[0].schedule_args == {"hour": 9}


# ========== quota ==========

@pytest.fixture(autouse=True)
def _stub_data_store(monkeypatch):
    """用内存 dict 替换 data_store；避免污染真实库。"""
    store_data: dict[str, Any] = {}

    class _Stub:
        def load_sync(self, name):
            return store_data.get(name)

        def save_sync(self, name, data):
            store_data[name] = data

    monkeypatch.setattr(ds_mod, "get_data_store", lambda: _Stub())
    yield


def test_quota_default_zero_means_exceeded() -> None:
    assert quota.is_quota_exceeded(
        "u1", scenario="x", daily_quota_per_user=0, cooldown_seconds=0
    ) is True


def test_quota_empty_user_id_treated_as_exceeded() -> None:
    assert quota.is_quota_exceeded(
        "", scenario="x", daily_quota_per_user=5, cooldown_seconds=0
    ) is True


def test_quota_within_limit_allows() -> None:
    assert quota.is_quota_exceeded(
        "u2", scenario="x", daily_quota_per_user=2, cooldown_seconds=0
    ) is False


def test_quota_mark_sent_then_increments() -> None:
    quota.mark_sent("u3", scenario="x", now=1000.0)
    assert quota.is_quota_exceeded(
        "u3", scenario="x", daily_quota_per_user=1, cooldown_seconds=0, now=1001.0
    ) is True
    assert quota.is_quota_exceeded(
        "u3", scenario="x", daily_quota_per_user=5, cooldown_seconds=0, now=1001.0
    ) is False


def test_quota_cooldown_enforced() -> None:
    quota.mark_sent("u4", scenario="morning", now=10_000.0)
    # 在冷却内
    assert quota.is_quota_exceeded(
        "u4",
        scenario="morning",
        daily_quota_per_user=5,
        cooldown_seconds=3600,
        now=10_100.0,
    ) is True
    # 冷却过后
    assert quota.is_quota_exceeded(
        "u4",
        scenario="morning",
        daily_quota_per_user=5,
        cooldown_seconds=3600,
        now=10_000.0 + 3700.0,
    ) is False


def test_quota_different_scenarios_independent_cooldown() -> None:
    quota.mark_sent("u5", scenario="morning", now=20_000.0)
    # 同用户但 evening scenario 没发过，冷却不应阻塞
    assert quota.is_quota_exceeded(
        "u5",
        scenario="evening",
        daily_quota_per_user=5,
        cooldown_seconds=3600,
        now=20_100.0,
    ) is False


# ========== gate ==========

def test_gate_returns_allow_when_no_tool_caller() -> None:
    allow, rewritten, _reason = asyncio.run(
        gate.gate_should_send(
            tool_caller=None, logger=MagicMock(), scenario="x", user_id="u",
            draft="hi", persona_snippet="", now_str=""
        )
    )
    assert allow is True
    assert rewritten is None


def test_gate_default_allow_on_exception() -> None:
    bad_caller = MagicMock()

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream down")

    bad_caller.chat_with_tools = boom
    allow, _, reason = asyncio.run(
        gate.gate_should_send(
            tool_caller=bad_caller, logger=MagicMock(), scenario="x", user_id="u",
            draft="hi", persona_snippet="", now_str=""
        )
    )
    assert allow is True
    assert "failed" in reason


def test_gate_parses_plain_json() -> None:
    fake = MagicMock()

    async def chat(messages, tools, use_builtin_search):
        resp = MagicMock()
        resp.content = '{"allow": false, "reason": "用户睡觉", "rewritten": ""}'
        return resp

    fake.chat_with_tools = chat
    allow, rewritten, reason = asyncio.run(
        gate.gate_should_send(
            tool_caller=fake, logger=MagicMock(), scenario="x", user_id="u",
            draft="hi", persona_snippet="", now_str=""
        )
    )
    assert allow is False
    assert rewritten is None
    assert reason == "用户睡觉"


def test_gate_parses_json_with_markdown_fence() -> None:
    fake = MagicMock()

    async def chat(messages, tools, use_builtin_search):
        resp = MagicMock()
        resp.content = '```json\n{"allow": true, "reason": "ok", "rewritten": "你好啊"}\n```'
        return resp

    fake.chat_with_tools = chat
    allow, rewritten, _reason = asyncio.run(
        gate.gate_should_send(
            tool_caller=fake, logger=MagicMock(), scenario="x", user_id="u",
            draft="嗨", persona_snippet="", now_str=""
        )
    )
    assert allow is True
    assert rewritten == "你好啊"


def test_gate_parses_json_inside_prose() -> None:
    fake = MagicMock()

    async def chat(messages, tools, use_builtin_search):
        resp = MagicMock()
        resp.content = '思考后我觉得：{"allow": true, "reason": "时机ok", "rewritten": ""}'
        return resp

    fake.chat_with_tools = chat
    allow, rewritten, _reason = asyncio.run(
        gate.gate_should_send(
            tool_caller=fake, logger=MagicMock(), scenario="x", user_id="u",
            draft="hi", persona_snippet="", now_str=""
        )
    )
    assert allow is True
    assert rewritten is None


# ========== news_push ==========

news_push = _load_si_submodule("scenarios.news_push")


def test_string_list_parses_list_and_csv_and_empty() -> None:
    assert news_push._string_list(["123", "456"]) == ["123", "456"]
    assert news_push._string_list("11,22, 33") == ["11", "22", "33"]
    assert news_push._string_list("") == []
    assert news_push._string_list(None) == []
    assert news_push._string_list(["", " ", "x"]) == ["x"]


def test_news_push_early_returns_when_master_switch_off() -> None:
    ctx = MagicMock()
    ctx.plugin_config.personification_social_intelligence_enabled = False
    asyncio.run(news_push.news_push_handler(ctx))
    # 总开关关闭：不应触发 bot 调用或 LLM 调用
    ctx.get_bots.assert_not_called()
    ctx.tool_caller.chat_with_tools.assert_not_called()


def test_news_push_early_returns_when_scenario_disabled() -> None:
    ctx = MagicMock()
    ctx.plugin_config.personification_social_intelligence_enabled = True
    ctx.plugin_config.personification_social_news_enabled = False
    asyncio.run(news_push.news_push_handler(ctx))
    ctx.get_bots.assert_not_called()


# ========== pending_topics & topic_extractor ==========

pending_topics_mod = _load_si_submodule("pending_topics")
topic_extractor = _load_si_submodule("topic_extractor")


def test_prefilter_rejects_short_or_irrelevant_text() -> None:
    assert topic_extractor.pre_filter("") is False
    assert topic_extractor.pre_filter("好") is False
    assert topic_extractor.pre_filter("今天天气真好") is False
    # 太长（>400）也跳过
    assert topic_extractor.pre_filter("x" * 401) is False


def test_prefilter_accepts_future_promise_keywords() -> None:
    assert topic_extractor.pre_filter("我下周三要去上海出差") is True
    assert topic_extractor.pre_filter("明天有个面试，紧张") is True
    assert topic_extractor.pre_filter("打算周末搬家") is True
    assert topic_extractor.pre_filter("过几天就出差了") is True


def test_pending_topic_add_and_list() -> None:
    tid = pending_topics_mod.add_pending_topic(
        user_id="user1",
        topic="下周去上海",
        raw_quote="我下周三去上海出差",
        time_hint_ts=2_000_000_000.0,
    )
    assert isinstance(tid, str) and len(tid) == 12
    items = pending_topics_mod.list_pending_topics()
    assert len(items) == 1
    assert items[0]["user_id"] == "user1"
    assert items[0]["topic"] == "下周去上海"


def test_pending_topic_add_is_idempotent_per_quote() -> None:
    tid1 = pending_topics_mod.add_pending_topic(
        user_id="u", topic="t", raw_quote="同样的话", time_hint_ts=1.0
    )
    tid2 = pending_topics_mod.add_pending_topic(
        user_id="u", topic="t-改名了", raw_quote="同样的话", time_hint_ts=999.0
    )
    assert tid1 == tid2
    items = pending_topics_mod.list_pending_topics()
    assert len(items) == 1
    # 第二次不覆盖
    assert items[0]["topic"] == "t"


def test_find_due_topics_filters_by_window_and_status() -> None:
    pending_topics_mod.add_pending_topic(
        user_id="a", topic="过期已 followup", raw_quote="x1", time_hint_ts=1000.0
    )
    pending_topics_mod.mark_followed_up(
        pending_topics_mod.add_pending_topic(
            user_id="a", topic="已 followup", raw_quote="x2", time_hint_ts=2000.0
        ),
        now=2100.0,
    )
    pending_topics_mod.add_pending_topic(
        user_id="b", topic="未来事件但远", raw_quote="远", time_hint_ts=99_999_999.0
    )
    pending_topics_mod.add_pending_topic(
        user_id="c", topic="正好到时间", raw_quote="正", time_hint_ts=1234500.0
    )
    due = pending_topics_mod.find_due_topics(now=1234500.0, window_seconds=86400.0)
    user_ids = sorted(item["user_id"] for item in due)
    # 'a' 第一条：time_hint 1000，离 now 1234500 远 → 不在窗口；
    # 'a' 第二条：已 followed_up 排除；'b' 远 → 不在窗口；'c' 在窗口
    assert user_ids == ["c"]


def test_mark_skipped_excludes_from_due() -> None:
    tid = pending_topics_mod.add_pending_topic(
        user_id="z", topic="x", raw_quote="zzz", time_hint_ts=5000.0
    )
    pending_topics_mod.mark_skipped(tid)
    assert pending_topics_mod.find_due_topics(now=5000.0, window_seconds=86400.0) == []


# ========== festival_greetings ==========

festival_mod = _load_si_submodule("scenarios.festival_greetings")


def test_today_festival_table_hits_known_dates() -> None:
    from datetime import datetime
    assert festival_mod._today_festival(datetime(2026, 10, 1, 9)) == "国庆节"
    assert festival_mod._today_festival(datetime(2026, 12, 25, 9)) == "圣诞节"
    assert festival_mod._today_festival(datetime(2026, 2, 14, 9)) == "情人节"


def test_today_festival_returns_none_on_blank_day() -> None:
    from datetime import datetime
    assert festival_mod._today_festival(datetime(2026, 7, 7, 9)) is None


def test_extract_birthday_various_formats() -> None:
    assert festival_mod._extract_birthday("生日：3月8日") == (3, 8)
    assert festival_mod._extract_birthday("生日是 03-08") == (3, 8)
    assert festival_mod._extract_birthday("出生于 1995-03-08") == (3, 8)
    assert festival_mod._extract_birthday("birthday: 12/25") == (12, 25)
    assert festival_mod._extract_birthday("Birthday 1/1") == (1, 1)


def test_extract_birthday_rejects_invalid_or_missing() -> None:
    assert festival_mod._extract_birthday("") is None
    assert festival_mod._extract_birthday("没提生日") is None
    # 月份非法不接受
    assert festival_mod._extract_birthday("生日：13月45日") is None


def test_select_birthday_users_matches_today() -> None:
    from datetime import datetime
    candidates = [
        ("u1", "喜欢游戏，生日：3月8日"),
        ("u2", "在北京"),
        ("u3", "birthday 10/01"),
    ]
    result = festival_mod._select_birthday_users(candidates, datetime(2026, 10, 1, 9))
    assert [r[0] for r in result] == ["u3"]
    result2 = festival_mod._select_birthday_users(candidates, datetime(2026, 3, 8, 9))
    assert [r[0] for r in result2] == ["u1"]
