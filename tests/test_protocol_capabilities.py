from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

caps = load_personification_module("plugin.personification.core.protocol_capabilities")


class FakeBot:
    def __init__(
        self,
        app_name: str = "NapCat.Onebot",
        fail_apis: set[str] | None = None,
        *,
        app_version: str = "",
        protocol_version: str = "v11",
    ) -> None:
        self.self_id = "12345"
        self.app_name = app_name
        self.app_version = app_version
        self.protocol_version = protocol_version
        self.fail_apis = fail_apis or set()
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **kwargs):
        self.calls.append((api, kwargs))
        if api == "get_version_info":
            return {
                "app_name": self.app_name,
                "app_version": self.app_version,
                "protocol_version": self.protocol_version,
            }
        if api in self.fail_apis:
            raise RuntimeError(f"{api} not supported")
        return {"result": True}


def _config(mode: str = "auto"):
    return SimpleNamespace(personification_protocol_extensions=mode)


def setup_function(_fn) -> None:
    caps.reset_capability_cache()


def test_flavor_detection_from_app_name() -> None:
    assert asyncio.run(caps.detect_flavor(FakeBot("NapCat.Onebot"))) == "napcat"
    caps.reset_capability_cache()
    assert asyncio.run(caps.detect_flavor(FakeBot("Lagrange.OneBot"))) == "lagrange"
    caps.reset_capability_cache()
    assert asyncio.run(caps.detect_flavor(FakeBot("LLOneBot"))) == "llonebot"
    caps.reset_capability_cache()
    assert asyncio.run(caps.detect_flavor(FakeBot("go-cqhttp"))) == "gocq"
    caps.reset_capability_cache()
    assert asyncio.run(caps.detect_flavor(FakeBot("Whatever"))) == "unknown"


def test_emoji_react_napcat_uses_set_msg_emoji_like() -> None:
    bot = FakeBot("NapCat.Onebot")
    ok = asyncio.run(caps.emoji_react(bot, _config(), message_id=42, face_id=76, group_id="g1"))
    assert ok is True
    api, kwargs = bot.calls[-1]
    assert api == "set_msg_emoji_like"
    assert kwargs == {"message_id": 42, "emoji_id": 76, "set": True}


def test_emoji_react_lagrange_uses_set_group_reaction() -> None:
    bot = FakeBot("Lagrange.OneBot")
    ok = asyncio.run(caps.emoji_react(bot, _config(), message_id=42, face_id=76, group_id="777"))
    assert ok is True
    api, kwargs = bot.calls[-1]
    assert api == "set_group_reaction"
    assert kwargs == {"group_id": 777, "message_id": 42, "code": "76", "is_add": True}


def test_emoji_react_gocq_and_none_mode_disabled() -> None:
    bot = FakeBot("go-cqhttp")
    assert asyncio.run(caps.emoji_react(bot, _config(), message_id=1, face_id=76, group_id="g")) is False
    bot2 = FakeBot("NapCat.Onebot")
    assert asyncio.run(caps.emoji_react(bot2, _config("none"), message_id=1, face_id=76, group_id="g")) is False
    assert bot2.calls == []


def test_unavailable_api_is_cooled_down() -> None:
    bot = FakeBot("NapCat.Onebot", fail_apis={"set_msg_emoji_like"})
    assert asyncio.run(caps.emoji_react(bot, _config(), message_id=1, face_id=76)) is False
    calls_after_first = len(bot.calls)
    assert asyncio.run(caps.emoji_react(bot, _config(), message_id=2, face_id=76)) is False
    # 第二次不再发起 set_msg_emoji_like 请求
    assert len(bot.calls) == calls_after_first


def test_unknown_implementation_does_not_probe_extensions() -> None:
    bot = FakeBot("Whatever")
    assert asyncio.run(caps.emoji_react(bot, _config(), message_id=1, face_id=76)) is False
    assert [name for name, _params in bot.calls] == ["get_version_info"]


def test_llonebot_version_gates_typing() -> None:
    old = FakeBot("LLOneBot", app_version="7.10.0")
    assert asyncio.run(caps.set_typing(old, _config(), user_id="10001")) is False
    assert [name for name, _params in old.calls] == ["get_version_info"]
    caps.reset_capability_cache()
    current = FakeBot("LLOneBot", app_version="7.12.3")
    assert asyncio.run(caps.set_typing(current, _config(), user_id="10001")) is True
    assert current.calls[-1] == ("set_input_status", {"user_id": 10001, "event_type": 1})


def test_poke_fallback_chain() -> None:
    bot = FakeBot("NapCat.Onebot", fail_apis={"group_poke"})
    ok = asyncio.run(caps.poke(bot, _config(), user_id="10001", group_id="777"))
    assert ok is True
    assert [c[0] for c in bot.calls if c[0] != "get_version_info"] == ["group_poke", "send_poke"]


def test_set_typing_only_napcat_family() -> None:
    napcat = FakeBot("NapCat.Onebot")
    assert asyncio.run(caps.set_typing(napcat, _config(), user_id="10001")) is True
    assert napcat.calls[-1] == ("set_input_status", {"user_id": 10001, "event_type": 1})
    caps.reset_capability_cache()
    lagrange = FakeBot("Lagrange.OneBot")
    assert asyncio.run(caps.set_typing(lagrange, _config(), user_id="10001")) is False
    assert all(c[0] == "get_version_info" for c in lagrange.calls)


def test_forced_mode_skips_detection() -> None:
    bot = FakeBot("Whatever")
    ok = asyncio.run(caps.emoji_react(bot, _config("napcat"), message_id=1, face_id=76))
    assert ok is True
    assert all(c[0] != "get_version_info" for c in bot.calls)
