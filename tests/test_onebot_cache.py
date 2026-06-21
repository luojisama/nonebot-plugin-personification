from __future__ import annotations

import asyncio
import time

import pytest

from ._loader import load_personification_module

onebot_cache = load_personification_module("plugin.personification.core.onebot_cache")


class _FakeBot:
    def __init__(
        self,
        *,
        fail: bool = False,
        nickname: str = "测试昵称",
        group_name: str = "测试群",
    ) -> None:
        self.fail = fail
        self.nickname = nickname
        self.group_name = group_name
        self.stranger_calls = 0
        self.group_calls = 0

    async def get_stranger_info(self, *, user_id: int) -> dict:
        self.stranger_calls += 1
        if self.fail:
            raise RuntimeError("bot offline")
        return {"user_id": user_id, "nickname": self.nickname}

    async def get_group_info(self, *, group_id: int) -> dict:
        self.group_calls += 1
        if self.fail:
            raise RuntimeError("bot offline")
        return {"group_id": group_id, "group_name": self.group_name}


@pytest.fixture(autouse=True)
def _clear_cache():
    onebot_cache._clear_caches_for_testing()
    yield
    onebot_cache._clear_caches_for_testing()


def test_get_user_nickname_caches_repeated_calls() -> None:
    bot = _FakeBot(nickname="艾莉")
    nick1 = asyncio.run(onebot_cache.get_user_nickname(bot, "12345"))
    nick2 = asyncio.run(onebot_cache.get_user_nickname(bot, "12345"))
    assert nick1 == "艾莉"
    assert nick2 == "艾莉"
    assert bot.stranger_calls == 1, "命中缓存时不应再次调用 bot"


def test_get_group_name_caches_repeated_calls() -> None:
    bot = _FakeBot(group_name="技术交流群")
    name1 = asyncio.run(onebot_cache.get_group_name(bot, "98765"))
    name2 = asyncio.run(onebot_cache.get_group_name(bot, "98765"))
    assert name1 == "技术交流群"
    assert name2 == "技术交流群"
    assert bot.group_calls == 1


def test_bot_failure_degrades_to_empty_string() -> None:
    bot = _FakeBot(fail=True)
    nick = asyncio.run(onebot_cache.get_user_nickname(bot, "12345"))
    gname = asyncio.run(onebot_cache.get_group_name(bot, "98765"))
    assert nick == ""
    assert gname == ""


def test_nil_bot_returns_empty_string_without_call() -> None:
    nick = asyncio.run(onebot_cache.get_user_nickname(None, "12345"))
    gname = asyncio.run(onebot_cache.get_group_name(None, "98765"))
    assert nick == ""
    assert gname == ""


def test_empty_id_skips_bot() -> None:
    bot = _FakeBot()
    nick = asyncio.run(onebot_cache.get_user_nickname(bot, ""))
    gname = asyncio.run(onebot_cache.get_group_name(bot, ""))
    assert nick == ""
    assert gname == ""
    assert bot.stranger_calls == 0
    assert bot.group_calls == 0


def test_ttl_expiration_triggers_refetch(monkeypatch) -> None:
    bot = _FakeBot(nickname="原昵称")
    # 第一次写入缓存（用真实 time.time，expires_at = now + 1）
    nick1 = asyncio.run(onebot_cache.get_user_nickname(bot, "111", ttl=1))
    assert nick1 == "原昵称"
    assert bot.stranger_calls == 1
    # 把模块内的 time.time 改成"真实时间 + 2"，超过 ttl=1 边界
    base = time.time() + 2
    monkeypatch.setattr(onebot_cache.time, "time", lambda: base)
    bot.nickname = "新昵称"
    nick2 = asyncio.run(onebot_cache.get_user_nickname(bot, "111", ttl=1))
    assert nick2 == "新昵称"
    assert bot.stranger_calls == 2, "ttl 过期后应重新调用 bot"


def test_failure_result_is_cached_too() -> None:
    """bot 抛异常时也缓存空串，避免反复打失败请求。"""
    bot = _FakeBot(fail=True)
    nick1 = asyncio.run(onebot_cache.get_user_nickname(bot, "222"))
    nick2 = asyncio.run(onebot_cache.get_user_nickname(bot, "222"))
    assert nick1 == ""
    assert nick2 == ""
    assert bot.stranger_calls == 1, "失败结果应被缓存，不应每次都重试 bot"
