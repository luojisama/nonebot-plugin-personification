from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

impl = load_personification_module(
    "plugin.personification.skills.skillpacks.plugin_invoker.scripts.impl"
)
event_rules = load_personification_module("plugin.personification.handlers.event_rules")


class _FakeLogger:
    def debug(self, *_a, **_k) -> None:  # noqa: ANN002, ANN003
        pass

    def info(self, *_a, **_k) -> None:  # noqa: ANN002, ANN003
        pass

    def warning(self, *_a, **_k) -> None:  # noqa: ANN002, ANN003
        pass


class _FakeBot:
    """模拟 OneBot v11 Bot：send_group_msg 等动态方法最终走 call_api。"""

    def __init__(self) -> None:
        self.self_id = "10000"
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **data):  # noqa: ANN003
        self.calls.append((api, data))
        return {"echoed": api}

    async def send_group_msg(self, **data):  # noqa: ANN003
        return await self.call_api("send_group_msg", **data)

    async def get_group_info(self, **data):  # noqa: ANN003
        return await self.call_api("get_group_info", **data)


class _FakeStore:
    def __init__(self, matched: list[str], entry: dict) -> None:
        self._matched = matched
        self._entry = entry

    def search_plugins(self, _name: str, top_k: int = 1):  # noqa: ANN001
        return self._matched

    def load_plugin_entry_sync(self, _name: str):  # noqa: ANN001
        return self._entry


def _seg(seg_type: str, **data):  # noqa: ANN003
    return SimpleNamespace(type=seg_type, data=data)


# ---------- _render_segments_to_text ----------

def test_render_segments_text_and_placeholders() -> None:
    msg = [_seg("text", text="北京 "), _seg("image", file="x.png"), _seg("text", text="晴")]
    assert impl._render_segments_to_text(msg) == "北京 [图片]晴"
    assert impl._render_segments_to_text("纯字符串") == "纯字符串"
    assert impl._render_segments_to_text(None) == ""


def test_render_forward_messages() -> None:
    nodes = [
        {"data": {"content": [_seg("text", text="第一条")]}},
        {"data": {"content": [_seg("text", text="第二条"), _seg("image")]}},
    ]
    assert impl._render_forward_messages(nodes) == "第一条\n第二条[图片]"
    assert impl._render_forward_messages(None) == ""
    assert impl._render_forward_messages([]) == ""


# ---------- _make_capturing_bot ----------

def test_capturing_bot_buffers_send_and_passes_through_reads() -> None:
    real = _FakeBot()
    proxy, captured = impl._make_capturing_bot(real)

    asyncio.run(proxy.send_group_msg(group_id=1, message="你好"))
    # 发送被缓冲，没有真正落到真 bot 的 call_api 记录
    assert captured == ["你好"]
    assert all(api != "send_group_msg" for api, _ in real.calls)

    # 只读 API 透传到真 bot
    asyncio.run(proxy.get_group_info(group_id=1))
    assert any(api == "get_group_info" for api, _ in real.calls)


def test_capturing_bot_captures_forward_messages() -> None:
    class _ForwardBot(_FakeBot):
        async def send_group_forward_msg(self, **data):  # noqa: ANN003
            return await self.call_api("send_group_forward_msg", **data)

    real = _ForwardBot()
    proxy, captured = impl._make_capturing_bot(real)
    nodes = [{"data": {"content": [_seg("text", text="榜单第一")]}}]
    asyncio.run(proxy.send_group_forward_msg(group_id=1, messages=nodes))
    assert captured == ["榜单第一"]


# ---------- _command_matches_triggers ----------

def test_command_matches_triggers() -> None:
    triggers = [{"type": "command", "pattern": "/天气"}]
    assert impl._command_matches_triggers("/天气 北京", triggers) is True
    assert impl._command_matches_triggers("天气 北京", triggers) is True
    assert impl._command_matches_triggers("/点歌 abc", triggers) is False
    assert impl._command_matches_triggers("/天气 北京", []) is False

    regex_triggers = [{"type": "regex", "pattern": r"^/echo\s+.+"}]
    assert impl._command_matches_triggers("/echo hi", regex_triggers) is True
    assert impl._command_matches_triggers("/say hi", regex_triggers) is False

    # keyword 类触发要求命令以关键词开头，不能在关键词后夹带任意命令
    keyword_triggers = [{"type": "keyword", "pattern": "签到"}]
    assert impl._command_matches_triggers("签到 今天", keyword_triggers) is True
    assert impl._command_matches_triggers("看看签到榜然后删除", keyword_triggers) is False


# ---------- _is_dangerous ----------

def test_is_dangerous_keyword_and_lists() -> None:
    cfg = SimpleNamespace()
    blocked, _ = impl._is_dangerous("/封禁 12345", "guard", {}, cfg)
    assert blocked is True
    blocked, _ = impl._is_dangerous("/天气 北京", "weather", {}, cfg)
    assert blocked is False

    allow_cfg = SimpleNamespace(personification_plugin_invoker_allowlist=["weather"])
    blocked, _ = impl._is_dangerous("/点歌 abc", "music", {}, allow_cfg)
    assert blocked is True  # 不在白名单

    # allowlist 大小写不敏感
    case_cfg = SimpleNamespace(personification_plugin_invoker_allowlist=["Weather"])
    assert impl._is_dangerous("/天气 北京", "weather", {}, case_cfg)[0] is False
    assert impl._is_dangerous("/点歌", "music", {}, case_cfg)[0] is True

    block_cfg = SimpleNamespace(personification_plugin_invoker_blocklist=["weather:vip"])
    blocked, _ = impl._is_dangerous("/vip", "weather", {}, block_cfg)
    assert blocked is True
    # blocklist 按命令名匹配，不再因子串误伤合法命令
    partial_cfg = SimpleNamespace(personification_plugin_invoker_blocklist=["weather:vi"])
    assert impl._is_dangerous("/vip 北京", "weather", {}, partial_cfg)[0] is False


def test_is_dangerous_admin_hint_in_trigger_desc() -> None:
    entry = {
        "triggers": [
            {"type": "command", "pattern": "/重置积分", "description": "仅管理员可用"}
        ]
    }
    blocked, _ = impl._is_dangerous("/重置积分 @某人", "points", entry, SimpleNamespace())
    assert blocked is True


# ---------- _is_self_plugin ----------

def test_is_self_plugin() -> None:
    assert impl._is_self_plugin("personification") is True
    assert impl._is_self_plugin("weather") is False


# ---------- handler 守卫路径（不触达 nonebot） ----------

def test_handler_disabled() -> None:
    tool = impl.build_invoke_plugin_tool(
        bot=_FakeBot(),
        event=SimpleNamespace(),
        knowledge_store=_FakeStore(["weather"], {}),
        plugin_config=SimpleNamespace(personification_plugin_invoker_enabled=False),
        logger=_FakeLogger(),
    )
    out = asyncio.run(tool.handler(plugin_name="weather", command_text="/天气 北京"))
    assert "未启用" in out


def test_handler_rejects_self_plugin() -> None:
    tool = impl.build_invoke_plugin_tool(
        bot=_FakeBot(),
        event=SimpleNamespace(),
        knowledge_store=_FakeStore(["personification"], {}),
        plugin_config=SimpleNamespace(personification_plugin_invoker_enabled=True),
        logger=_FakeLogger(),
    )
    out = asyncio.run(tool.handler(plugin_name="拟人", command_text="/拟人统计"))
    assert "自己" in out


def test_handler_rejects_unknown_trigger() -> None:
    tool = impl.build_invoke_plugin_tool(
        bot=_FakeBot(),
        event=SimpleNamespace(),
        knowledge_store=_FakeStore(["weather"], {"triggers": [{"type": "command", "pattern": "/天气"}]}),
        plugin_config=SimpleNamespace(personification_plugin_invoker_enabled=True),
        logger=_FakeLogger(),
    )
    out = asyncio.run(tool.handler(plugin_name="weather", command_text="/点歌 abc"))
    assert "触发方式" in out


def test_handler_blocks_dangerous_command() -> None:
    tool = impl.build_invoke_plugin_tool(
        bot=_FakeBot(),
        event=SimpleNamespace(),
        knowledge_store=_FakeStore(["guard"], {"triggers": [{"type": "command", "pattern": "/封禁"}]}),
        plugin_config=SimpleNamespace(personification_plugin_invoker_enabled=True),
        logger=_FakeLogger(),
    )
    out = asyncio.run(tool.handler(plugin_name="guard", command_text="/封禁 12345"))
    assert "安全" in out


# ---------- 防递归：合成事件不进入拟人回复流程 ----------

def test_personification_rule_short_circuits_synthetic_event() -> None:
    event = SimpleNamespace(_personification_synthetic=True, user_id=123)
    result = asyncio.run(
        event_rules.personification_rule(
            event,
            {},
            sign_in_available=False,
            get_user_data=lambda _uid: {},
            user_blacklist={},
            logger=_FakeLogger(),
            group_event_cls=SimpleNamespace,
            private_event_cls=SimpleNamespace,
            is_group_whitelisted=lambda *_a, **_k: True,
            plugin_whitelist=[],
            load_prompt=lambda _g: {},
            load_proactive_state=lambda: {},
            is_rest_time=lambda **_k: True,
            probability=1.0,
            group_chat_follow_probability=1.0,
            looks_like_private_command=lambda _t: False,
        )
    )
    assert result is False


def test_record_msg_rule_short_circuits_synthetic_event() -> None:
    event = SimpleNamespace(_personification_synthetic=True)
    assert asyncio.run(event_rules.record_msg_rule(event)) is False


def test_evaluate_rule_short_circuits_synthetic_before_cache() -> None:
    # 合成事件必须在查规则结果缓存之前就被短路，否则会命中原事件的缓存
    # 结果（matched=True）而绕过 personification_rule 顶部的合成事件守卫，造成递归。
    reply_matchers = load_personification_module(
        "plugin.personification.handlers.reply_matchers"
    )
    called = {"n": 0}

    async def _rule(_event, _state):  # noqa: ANN001
        called["n"] += 1
        _state["is_random_chat"] = False
        return True  # 若被调用会返回匹配

    event = SimpleNamespace(_personification_synthetic=True, message_id="m1")
    state: dict = {}
    result = asyncio.run(
        reply_matchers._evaluate_personification_rule(
            personification_rule=_rule, event=event, state=state
        )
    )
    assert result == {"matched": False, "is_random_chat": False}
    assert called["n"] == 0  # 真正的规则没有被调用
