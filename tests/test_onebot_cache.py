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
        group_list: list[dict] | None = None,
        stranger_extra: dict | None = None,
    ) -> None:
        self.fail = fail
        self.nickname = nickname
        self.group_name = group_name
        self.group_list = group_list or []
        self.stranger_extra = stranger_extra or {}
        self.stranger_calls = 0
        self.group_calls = 0
        self.group_list_calls = 0
        self.group_member_calls = 0

    async def get_stranger_info(self, *, user_id: int) -> dict:
        self.stranger_calls += 1
        if self.fail:
            raise RuntimeError("bot offline")
        return {"user_id": user_id, "nickname": self.nickname, **self.stranger_extra}

    async def get_group_info(self, *, group_id: int) -> dict:
        self.group_calls += 1
        if self.fail:
            raise RuntimeError("bot offline")
        return {"group_id": group_id, "group_name": self.group_name}

    async def get_group_list(self) -> list[dict]:
        self.group_list_calls += 1
        if self.fail:
            raise RuntimeError("bot offline")
        return self.group_list

    async def get_group_member_info(self, *, group_id: int, user_id: int) -> dict:
        self.group_member_calls += 1
        if self.fail:
            raise RuntimeError("bot offline")
        return {"group_id": group_id, "user_id": user_id, "nickname": self.nickname}


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


def test_get_user_profile_normalizes_extended_fields_and_caches() -> None:
    bot = _FakeBot(
        nickname="艾莉",
        stranger_extra={
            "sex": "female",
            "age": 18,
            "qid": "q123",
            "longNick": "今天也在写代码",
            "level": "42",
        },
    )
    profile1 = asyncio.run(onebot_cache.get_user_profile(bot, "12345"))
    profile2 = asyncio.run(onebot_cache.get_user_profile(bot, "12345"))
    assert profile1["nickname"] == "艾莉"
    assert profile1["sex"] == "female"
    assert profile1["age"] == 18
    assert profile1["qid"] == "q123"
    assert profile1["signature"] == "今天也在写代码"
    assert profile1["level"] == "42"
    assert profile1["avatar_url"].endswith("dst_uin=12345&spec=640")
    assert profile1["homepage_url"] == "https://user.qzone.qq.com/12345"
    assert profile2 == profile1
    assert bot.stranger_calls == 1


def test_get_user_profile_for_nonnumeric_id_uses_deterministic_urls_without_protocol_call() -> None:
    bot = _FakeBot(nickname="不应调用")
    profile = asyncio.run(onebot_cache.get_user_profile(bot, "u_alpha"))
    assert profile["user_id"] == "u_alpha"
    assert profile["avatar_url"].endswith("dst_uin=u_alpha&spec=640")
    assert profile["homepage_url"] == "https://user.qzone.qq.com/u_alpha"
    assert bot.stranger_calls == 0


def test_get_group_name_caches_repeated_calls() -> None:
    bot = _FakeBot(group_name="技术交流群")
    name1 = asyncio.run(onebot_cache.get_group_name(bot, "98765"))
    name2 = asyncio.run(onebot_cache.get_group_name(bot, "98765"))
    assert name1 == "技术交流群"
    assert name2 == "技术交流群"
    assert bot.group_calls == 1


def test_get_group_name_map_prefers_group_list() -> None:
    bot = _FakeBot(
        group_name="详情接口群名",
        group_list=[
            {"group_id": 1001, "group_name": "群列表一群"},
            {"group_id": "1002", "group_name": "群列表二群"},
        ],
    )
    names = asyncio.run(onebot_cache.get_group_name_map(bot, ["1001", "1002"]))
    assert names == {"1001": "群列表一群", "1002": "群列表二群"}
    assert bot.group_list_calls == 1
    assert bot.group_calls == 0


def test_get_group_name_map_uses_call_api_fallback() -> None:
    class _CallApiBot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_api(self, api: str, **kwargs) -> list[dict]:
            self.calls.append((api, kwargs))
            return [{"group_id": 2001, "group_name": "call_api 群"}]

    bot = _CallApiBot()
    names = asyncio.run(onebot_cache.get_group_name_map(bot, ["2001"]))
    assert names["2001"] == "call_api 群"
    assert bot.calls == [("get_group_list", {})]


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


def test_cache_is_scoped_by_bot_self_id() -> None:
    first = _FakeBot(nickname="甲", group_name="甲群")
    second = _FakeBot(nickname="乙", group_name="乙群")
    first.self_id = "10001"
    second.self_id = "10002"
    assert asyncio.run(onebot_cache.get_user_nickname(first, "123")) == "甲"
    assert asyncio.run(onebot_cache.get_user_nickname(second, "123")) == "乙"
    assert asyncio.run(onebot_cache.get_group_name(first, "456")) == "甲群"
    assert asyncio.run(onebot_cache.get_group_name(second, "456")) == "乙群"
    assert first.stranger_calls == second.stranger_calls == 1
    assert first.group_calls == second.group_calls == 1


def test_concurrent_group_misses_share_one_protocol_call() -> None:
    bot = _FakeBot(group_name="并发群")

    async def run() -> list[str]:
        return await asyncio.gather(*(onebot_cache.get_group_name(bot, "789") for _ in range(8)))

    assert asyncio.run(run()) == ["并发群"] * 8
    assert bot.group_calls == 1


def test_failure_cache_uses_short_ttl(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(onebot_cache.time, "time", lambda: now)
    bot = _FakeBot(fail=True)
    assert asyncio.run(onebot_cache.get_group_name(bot, "98765")) == ""
    assert asyncio.run(onebot_cache.get_group_name(bot, "98765")) == ""
    assert bot.group_calls == 1
    now += onebot_cache._FAILURE_TTL_SECONDS + 1
    assert asyncio.run(onebot_cache.get_group_name(bot, "98765")) == ""
    assert bot.group_calls == 2


def test_group_member_proof_is_bot_group_user_scoped_and_cached_for_five_minutes(monkeypatch) -> None:  # noqa: ANN001
    now = 1000.0
    monkeypatch.setattr(onebot_cache.time, "time", lambda: now)
    bot = _FakeBot(nickname="成员")
    bot.self_id = "90001"
    first = asyncio.run(onebot_cache.get_group_member_info(bot, "20001", "10001"))
    second = asyncio.run(onebot_cache.get_group_member_info(bot, "20001", "10001"))
    other_group = asyncio.run(onebot_cache.get_group_member_info(bot, "20002", "10001"))
    other_user = asyncio.run(onebot_cache.get_group_member_info(bot, "20001", "10002"))
    assert first == second == {"group_id": 20001, "user_id": 10001, "nickname": "成员"}
    assert other_group and other_user
    assert bot.group_member_calls == 3

    now += onebot_cache._GROUP_MEMBER_SUCCESS_TTL_SECONDS + 1
    assert asyncio.run(onebot_cache.get_group_member_info(bot, "20001", "10001"))
    assert bot.group_member_calls == 4


def test_group_member_proof_failure_is_cached_for_fifteen_seconds(monkeypatch) -> None:  # noqa: ANN001
    now = 1000.0
    monkeypatch.setattr(onebot_cache.time, "time", lambda: now)
    bot = _FakeBot(fail=True)
    assert asyncio.run(onebot_cache.get_group_member_info(bot, "20001", "10001")) is None
    assert asyncio.run(onebot_cache.get_group_member_info(bot, "20001", "10001")) is None
    assert bot.group_member_calls == 1
    now += onebot_cache._FAILURE_TTL_SECONDS + 1
    assert asyncio.run(onebot_cache.get_group_member_info(bot, "20001", "10001")) is None
    assert bot.group_member_calls == 2


def test_group_member_proof_uses_call_api_fallback_and_deduplicates_inflight() -> None:
    class _CallApiBot:
        self_id = "90001"

        def __init__(self) -> None:
            self.calls = 0

        async def call_api(self, api: str, **kwargs) -> dict:
            assert api == "get_group_member_info"
            self.calls += 1
            await asyncio.sleep(0)
            return {"group_id": kwargs["group_id"], "user_id": kwargs["user_id"]}

    bot = _CallApiBot()

    async def _run() -> list[dict | None]:
        return await asyncio.gather(
            *(onebot_cache.get_group_member_info(bot, "20001", "10001") for _index in range(8))
        )

    results = asyncio.run(_run())
    assert all(item == {"group_id": 20001, "user_id": 10001} for item in results)
    assert bot.calls == 1
