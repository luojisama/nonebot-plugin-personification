from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from ._loader import load_personification_module


diary_flow = load_personification_module("plugin.personification.flows.diary_flow")
simple_loop = load_personification_module(
    "plugin.personification.agent.runtime.simple_loop"
)


class _Resp:
    def __init__(self, content: str, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _ToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict) -> None:
        self.id = call_id
        self.name = name
        self.arguments = arguments


class _Logger:
    def __init__(self) -> None:
        self.infos: list[str] = []

    def info(self, message: str) -> None:
        self.infos.append(str(message))

    def warning(self, _message: str) -> None:
        return None

    def debug(self, _message: str) -> None:
        return None

    def error(self, _message: str) -> None:
        return None


class _Store:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def load_sync(self, _name: str):  # noqa: ANN201
        return self.payload


class _Bot:
    async def get_group_list(self):  # noqa: ANN201
        return []


def test_qzone_post_similarity_rejects_repeated_daily_motifs() -> None:
    recent = [
        "明天先带点疲惫，靠炸鸡排和摸鱼慢慢回血。",
        "明天大概会先累一下，但先想去吃块炸鸡排回血。",
    ]

    assert diary_flow._is_too_similar_to_recent_qzone_post(
        "明天先靠炸鸡排和摸鱼给自己回血一下",
        recent,
    )
    assert not diary_flow._is_too_similar_to_recent_qzone_post(
        "刚看到新番截图，突然想把桌面也收拾一下",
        recent,
    )


def test_generate_ai_diary_injects_recent_posts_and_enables_builtin_search(monkeypatch) -> None:  # noqa: ANN001
    logger = _Logger()
    monkeypatch.setattr(
        diary_flow,
        "get_data_store",
        lambda: _Store({"recent_contents": ["明天先靠炸鸡排和摸鱼慢慢回血"]}),
    )
    calls: list[dict] = []

    async def _call_ai(messages, **kwargs):  # noqa: ANN001
        calls.append({"messages": messages, "kwargs": kwargs})
        return json.dumps({"content": "新游戏更新看着有点想摸一下", "image_prompt": ""}, ensure_ascii=False)

    result = asyncio.run(
        diary_flow.generate_ai_diary(
            _Bot(),
            load_prompt=lambda: "你是绪山真寻。",
            call_ai_api=_call_ai,
            logger=logger,
        )
    )

    assert result == "新游戏更新看着有点想摸一下"
    assert calls[0]["kwargs"]["use_builtin_search"] is True
    prompt = calls[0]["messages"][1]["content"]
    assert "明天先靠炸鸡排和摸鱼慢慢回血" in prompt
    assert "游戏、动漫、轻新闻" in prompt
    assert "不要写成镜头旁白" in prompt
    assert "状态报告" in prompt


def test_generate_ai_diary_skips_too_similar_recent_post(monkeypatch) -> None:  # noqa: ANN001
    logger = _Logger()
    monkeypatch.setattr(
        diary_flow,
        "get_data_store",
        lambda: _Store({"recent_contents": ["明天先靠炸鸡排和摸鱼慢慢回血"]}),
    )

    async def _call_ai(_messages, **_kwargs):  # noqa: ANN001
        return json.dumps({"content": "明天先靠炸鸡排和摸鱼给自己回血一下", "image_prompt": ""}, ensure_ascii=False)

    result = asyncio.run(
        diary_flow.generate_ai_diary(
            _Bot(),
            load_prompt=lambda: "你是绪山真寻。",
            call_ai_api=_call_ai,
            logger=logger,
        )
    )

    assert result == ""
    assert any("repeats recent content" in item for item in logger.infos)


def test_run_tool_loop_text_executes_tool_then_returns_content(monkeypatch) -> None:  # noqa: ANN001
    """工具循环：首轮返回 tool_calls 执行工具，次轮返回最终文本。"""

    class _Caller:
        def __init__(self) -> None:
            self.rounds = 0
            self.builtin_search_seen: list = []

        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            self.builtin_search_seen.append(use_builtin_search)
            self.rounds += 1
            if self.rounds == 1:
                return _Resp("", [_ToolCall("c1", "web_search", {"query": "天气"})])
            return _Resp(json.dumps({"content": "今天有点冷", "image_prompt": ""}, ensure_ascii=False))

        def build_tool_result_message(self, call_id, name, result):  # noqa: ANN001
            return {"role": "tool", "tool_call_id": call_id, "name": name, "content": result}

    class _Registry:
        def get(self, _name):  # noqa: ANN001
            return object()

        def openai_schemas(self):  # noqa: ANN201
            return [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]

    executed: list = []

    async def _fake_exec(*, registry, tool_name, tool_args, rewritten_query, user_images, logger):  # noqa: ANN001
        executed.append(tool_name)
        return tool_args, "tianqi 5 度"

    monkeypatch.setattr(simple_loop, "_execute_tool_with_retries", _fake_exec)
    monkeypatch.setattr(
        simple_loop,
        "select_tool_schemas",
        lambda registry, *, has_images, chat_intent="": registry.openai_schemas(),
    )

    caller = _Caller()
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "写说说"}]
    out = asyncio.run(
        simple_loop.run_tool_loop_text(
            messages,
            registry=_Registry(),
            tool_caller=caller,
            logger=_Logger(),
            max_steps=4,
            use_builtin_search=True,
        )
    )

    assert json.loads(out)["content"] == "今天有点冷"
    assert caller.rounds == 2
    assert executed == ["web_search"]
    assert caller.builtin_search_seen[0] is True
    # 工具结果被回填进消息历史
    assert any(m.get("role") == "tool" for m in messages)


def test_build_qzone_post_rewrites_ooc_search_talk() -> None:
    """生成内容若漏出"根据搜索结果"等搜索腔，应被去 AI 腔重写。"""

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            assert tools == []  # rewrite 走空工具
            return _Resp("外面风有点大")

        def build_tool_result_message(self, *_a):  # noqa: ANN001
            return {}

    result = asyncio.run(
        diary_flow._build_qzone_post_with_optional_image(
            content="根据搜索结果，今天天气不错",
            image_prompt="",
            tool_caller=_Caller(),
            logger=_Logger(),
            recent_posts=[],
            persona_system="你是某角色",
        )
    )

    assert result == "外面风有点大"


def test_build_qzone_post_drops_when_ooc_rewrite_fails() -> None:
    """去 AI 腔重写失败（仍是搜索腔）则丢弃该条，宁缺勿发。"""

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            return _Resp("我查了一下资料发现")  # 仍是 OOC

        def build_tool_result_message(self, *_a):  # noqa: ANN001
            return {}

    result = asyncio.run(
        diary_flow._build_qzone_post_with_optional_image(
            content="根据搜索结果，今天天气不错",
            image_prompt="",
            tool_caller=_Caller(),
            logger=_Logger(),
            recent_posts=[],
            persona_system="x",
        )
    )

    assert result == ""


def test_review_qzone_post_rewrites_net_slang_tic() -> None:
    """『也太……了吧』等营业感叹腔会被改写成平铺直叙。"""

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            assert tools == []
            return _Resp("上班还能摸鱼打两局游戏")

        def build_tool_result_message(self, *_a):  # noqa: ANN001
            return {}

    result = asyncio.run(
        diary_flow._review_qzone_post(
            "上班能打游戏也太爽了吧",
            tool_caller=_Caller(),
            persona_system="你是某角色",
            logger=_Logger(),
        )
    )

    assert result == "上班还能摸鱼打两局游戏"
    assert not diary_flow._NET_SLANG_TIC_RE.search(result)


def test_review_qzone_post_rewrites_stiff_qzone_tic() -> None:
    """器官拟人/先后对仗这类模板化机灵句会被改松一点。"""

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            assert tools == []
            assert "模板化的机灵句" in messages[1]["content"]
            return _Resp("有点想吃夜宵了")

        def build_tool_result_message(self, *_a):  # noqa: ANN001
            return {}

    result = asyncio.run(
        diary_flow._review_qzone_post(
            "脑子没在加班，胃先开始催了。",
            tool_caller=_Caller(),
            persona_system="你是某角色",
            logger=_Logger(),
        )
    )

    assert result == "有点想吃夜宵了"
    assert not diary_flow._QZONE_STIFF_TIC_RE.search(result)


def test_review_qzone_post_keeps_clean_text_untouched() -> None:
    """不含营业感叹腔/搜索腔的正文不调用改写，原样返回。"""

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            raise AssertionError("clean text should not be rewritten")

        def build_tool_result_message(self, *_a):  # noqa: ANN001
            return {}

    result = asyncio.run(
        diary_flow._review_qzone_post(
            "下午三点的阳光有点刺眼",
            tool_caller=_Caller(),
            persona_system="x",
            logger=_Logger(),
        )
    )

    assert result == "下午三点的阳光有点刺眼"


def test_build_qzone_post_can_attach_local_sticker_image(tmp_path) -> None:
    """有 image_prompt 时，QZone 配图可优先使用外置表情包库静态图。"""
    payload = b"\x89PNG\r\n\x1a\nlocal-qzone-sticker"
    image_path = tmp_path / "mood.png"
    image_path.write_bytes(payload)
    (tmp_path / "stickers.json").write_text(
        json.dumps(
            {
                "mood.png": {
                    "description": "窗边发呆的日常氛围图",
                    "use_hint": "适合日常碎碎念配图",
                    "mood_tags": ["淡定"],
                    "scene_tags": ["冷场时"],
                    "proactive_send": True,
                    "style": "anime",
                    "weight": 1.5,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class _Caller:
        async def chat_with_tools(self, *_args, **_kwargs):  # noqa: ANN001
            raise AssertionError("clean post with local sticker should not call LLM")

    result = asyncio.run(
        diary_flow._build_qzone_post_with_optional_image(
            content="下午三点的阳光有点刺眼",
            image_prompt="quiet daily anime image by the window",
            tool_caller=_Caller(),
            plugin_config=SimpleNamespace(personification_sticker_path=str(tmp_path)),
            logger=_Logger(),
            recent_posts=[],
            persona_system="x",
        )
    )

    assert result.startswith("下午三点的阳光有点刺眼")
    assert f"[IMAGE_B64]{base64.b64encode(payload).decode('ascii')}[/IMAGE_B64]" in result


def test_recent_chat_context_filters_bot_self_messages() -> None:
    """发空间参考聊天时跳过 bot 自己发的消息，避免采样其它插件输出。"""

    class _HistoryBot:
        self_id = "99999"

        async def get_group_list(self):  # noqa: ANN201
            return [{"group_id": 1, "group_name": "测试群"}]

        async def get_group_msg_history(self, *, group_id, count):  # noqa: ANN001, ANN201
            return {
                "messages": [
                    {
                        "sender": {"user_id": "99999", "nickname": "bot"},
                        "message": [{"type": "text", "data": {"text": "其它插件自动播报"}}],
                    },
                    {
                        "sender": {"user_id": "10001", "nickname": "好友"},
                        "message": [{"type": "text", "data": {"text": "今晚风好大"}}],
                    },
                ]
            }

    result = asyncio.run(diary_flow.get_recent_chat_context(_HistoryBot(), _Logger()))
    assert "今晚风好大" in result
    assert "其它插件自动播报" not in result


def test_generate_once_reinforces_persona_in_system_prompt() -> None:
    """发空间生成必须保留角色人设：旧的"不扮演角色"措辞已去除，并注入人设快照。"""
    captured: dict = {}

    async def _call(messages, **_kwargs):  # noqa: ANN001
        captured["messages"] = messages
        return json.dumps({"content": "楼下的猫又来蹭饭了", "image_prompt": ""}, ensure_ascii=False)

    asyncio.run(
        diary_flow._generate_once(
            "你是白咲真寻，一个有点中二又傲娇的少女。",
            "写一条说说",
            call_ai_api=_call,
            use_builtin_search=False,
        )
    )
    system = captured["messages"][0]["content"]
    assert "你是白咲真寻" in system  # 原始人设被保留
    assert "你不在群聊中扮演角色" not in system  # 误导性旧措辞已移除
    assert "继续严格保持你的人设" in system  # 改为强化人设
    assert "## 人设快照" in system  # 注入身份/风格快照


def test_generate_once_recovers_persona_profile_from_dict() -> None:
    """dict 形态人设（YAML 模式）的 persona_profile 身份/风格会被抽进人设快照。"""
    captured: dict = {}

    async def _call(messages, **_kwargs):  # noqa: ANN001
        captured["messages"] = messages
        return json.dumps({"content": "今天也想早点下班", "image_prompt": ""}, ensure_ascii=False)

    persona = {
        "system": "你是高冷知性的小姐姐。",
        "persona_profile": {
            "identity_rules": ["高冷知性，话不多"],
            "style_rules": ["短句、克制、偶尔毒舌"],
            "boundary_rules": ["群聊接话规则X"],
        },
    }
    asyncio.run(
        diary_flow._generate_once(persona, "写说说", call_ai_api=_call)
    )
    system = captured["messages"][0]["content"]
    assert isinstance(system, dict)
    snapshot = system["system"].split("## 人设快照", 1)[1]
    assert "高冷知性，话不多" in snapshot and "短句、克制、偶尔毒舌" in snapshot
    assert "群聊接话规则X" not in snapshot  # group-chat 边界规则不带入发空间


def test_format_qzone_quota_block_tight_budget() -> None:
    block = diary_flow._format_qzone_quota_block(
        {"used": 28, "limit": 30, "remaining": 2, "days_in_month": 30, "days_left": 12}
    )
    assert "上限 30 条" in block and "已发 28 条" in block and "剩余 2 条" in block
    assert "节奏建议" in block and "克制" in block


def test_maybe_generate_proactive_injects_quota(monkeypatch) -> None:  # noqa: ANN001
    """额度快照会被注入主动发空间的决策 prompt，agent 据此自行决定 skip/post。"""

    class _Store:
        def load_sync(self, _name):  # noqa: ANN001
            return {}

    monkeypatch.setattr(diary_flow, "get_data_store", lambda: _Store())

    class _Bot:
        async def get_group_list(self):  # noqa: ANN201
            return []

    captured: dict = {}

    async def _call(messages, **_kwargs):  # noqa: ANN001
        captured["messages"] = messages
        return json.dumps({"action": "skip", "content": "", "reason": "额度紧"}, ensure_ascii=False)

    quota = {"used": 27, "limit": 30, "remaining": 3, "days_in_month": 30, "days_left": 10}
    result = asyncio.run(
        diary_flow.maybe_generate_proactive_qzone_post(
            _Bot(),
            load_prompt=lambda: "你是某角色",
            call_ai_api=_call,
            logger=_Logger(),
            quota=quota,
        )
    )
    user_prompt = captured["messages"][1]["content"]
    assert "本月发空间额度" in user_prompt and "已发 27 条" in user_prompt
    assert "不要写成镜头旁白" in user_prompt
    assert "手机上随手敲的一句" in user_prompt
    assert result == ""  # action=skip
