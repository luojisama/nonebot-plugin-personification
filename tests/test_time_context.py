from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ._loader import load_personification_module

time_ctx = load_personification_module("plugin.personification.core.time_ctx")


def test_time_context_uses_configured_timezone() -> None:
    time_ctx.init_time_context("Asia/Tokyo")
    now = datetime(2026, 5, 2, 15, 30, tzinfo=ZoneInfo("Asia/Tokyo"))

    block = time_ctx.build_current_time_context_block(now)

    assert "Asia/Tokyo" in block
    assert "2026-05-02 15:30:00" in block
    assert "周六" in block


def test_inject_current_time_context_is_idempotent() -> None:
    time_ctx.init_time_context("Asia/Shanghai")
    messages = [{"role": "user", "content": "更新用户画像"}]

    once = time_ctx.inject_current_time_context(messages)
    twice = time_ctx.inject_current_time_context(once)

    assert len(once) == 2
    assert once == twice
    assert once[0]["role"] == "system"
    assert "当前时间信息" in once[0]["content"]
