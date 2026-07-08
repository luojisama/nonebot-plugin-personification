from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module


action_executor_mod = load_personification_module("plugin.personification.agent.action_executor")
chat_intent_mod = load_personification_module("plugin.personification.core.chat_intent")
planner_mod = load_personification_module("plugin.personification.agent.runtime.planner")
qq_library = load_personification_module("plugin.personification.core.qq_expression_library")
qq_diagnostics = load_personification_module("plugin.personification.core.qq_expression_diagnostics")
qq_tools = load_personification_module("plugin.personification.core.qq_expression_tools")
event_rules = load_personification_module("plugin.personification.handlers.event_rules")
reply_buffer = load_personification_module("plugin.personification.handlers.reply_buffer")
tool_catalog = load_personification_module("plugin.personification.agent.runtime.tool_catalog")
tool_registry_mod = load_personification_module("plugin.personification.agent.tool_registry")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.api_results: dict[str, object] = {}

    async def send(self, _event, message):  # noqa: ANN001
        self.sent.append(str(message))

    async def call_api(self, api: str, **kwargs):  # noqa: ANN001
        self.calls.append((api, kwargs))
        if api in self.api_results:
            return self.api_results[api]
        return {}


class _Logger:
    def warning(self, *_args, **_kwargs):  # noqa: ANN001
        return None


class _Seg:
    def __init__(self, type_: str, data: dict) -> None:
        self.type = type_
        self.data = data


def _config(mode: str = "auto", *, qq_expression_enabled: bool = True):
    return SimpleNamespace(
        personification_protocol_extensions=mode,
        personification_qq_expression_enabled=qq_expression_enabled,
    )


def _tool_map(executor, bot):  # noqa: ANN001
    return {
        tool.name: tool
        for tool in qq_tools.build_send_qq_expression_tools(
            executor=executor,
            bot=bot,
            plugin_config=executor.config,
        )
    }


def test_resolve_qq_face_name_alias() -> None:
    face_id, label = qq_tools.resolve_qq_face_id(face_name="笑哭")

    assert face_id == 182
    assert label == "笑哭"


def test_send_qq_face_tool_queues_and_executor_sends() -> None:
    async def _run() -> tuple[list[dict], list[str], dict]:
        bot = FakeBot()
        executor = action_executor_mod.ActionExecutor(bot, object(), _config(), _Logger())
        queued: list[dict] = []
        executor.bind_pending_actions(queued)
        result = await _tool_map(executor, bot)["send_qq_face"].handler(face_name="捂脸")
        for action in queued:
            await executor.execute(action["type"], action["params"])
        return queued, bot.sent, json.loads(result)

    queued, sent, payload = asyncio.run(_run())

    assert payload["ok"] is True
    assert payload["queued"] is True
    assert queued == [
        {
            "type": "send_qq_face",
            "params": {"face_id": 264, "text": "", "history_text": "[QQ表情:捂脸]"},
        }
    ]
    assert sent == ["[CQ:face,id=264]"]


def test_send_favorite_expression_fetches_url_and_queues_image() -> None:
    async def _run() -> tuple[list[dict], list[str], list[tuple[str, dict]], dict]:
        bot = FakeBot()
        bot.api_results["fetch_custom_face"] = {"url": ["https://example.test/a.png"]}
        executor = action_executor_mod.ActionExecutor(bot, object(), _config(), _Logger())
        queued: list[dict] = []
        executor.bind_pending_actions(queued)
        result = await _tool_map(executor, bot)["send_qq_favorite_expression"].handler(count=3)
        for action in queued:
            await executor.execute(action["type"], action["params"])
        return queued, bot.sent, bot.calls, json.loads(result)

    queued, sent, calls, payload = asyncio.run(_run())

    assert calls == [("fetch_custom_face", {"count": 3})]
    assert payload["kind"] == "qq_favorite_expression"
    assert queued == [
        {
            "type": "send_qq_image_expression",
            "params": {
                "url": "https://example.test/a.png",
                "text": "",
                "summary": "",
                "history_text": "[QQ收藏表情]",
            },
        }
    ]
    assert sent and sent[0].startswith("[CQ:image,file=https://example.test/a.png")


def test_send_favorite_expression_string_false_keeps_index_pick() -> None:
    async def _run() -> list[dict]:
        bot = FakeBot()
        bot.api_results["fetch_custom_face"] = {
            "url": ["https://example.test/a.png", "https://example.test/b.png"]
        }
        executor = action_executor_mod.ActionExecutor(bot, object(), _config(), _Logger())
        queued: list[dict] = []
        executor.bind_pending_actions(queued)
        await _tool_map(executor, bot)["send_qq_favorite_expression"].handler(
            count=2,
            index=2,
            random_pick="false",
        )
        return queued

    queued = asyncio.run(_run())

    assert queued[0]["params"]["url"] == "https://example.test/b.png"


def test_send_recommended_expression_uses_word_parameter() -> None:
    async def _run() -> tuple[list[dict], list[tuple[str, dict]], dict]:
        bot = FakeBot()
        bot.api_results["get_recommend_face"] = {"url": ["https://example.test/happy.png"]}
        executor = action_executor_mod.ActionExecutor(bot, object(), _config(), _Logger())
        queued: list[dict] = []
        executor.bind_pending_actions(queued)
        result = await _tool_map(executor, bot)["send_qq_recommended_expression"].handler(query="开心")
        return queued, bot.calls, json.loads(result)

    queued, calls, payload = asyncio.run(_run())

    assert calls == [("get_recommend_face", {"word": "开心"})]
    assert payload["queued"] is True
    assert queued[0]["params"]["url"] == "https://example.test/happy.png"
    assert queued[0]["params"]["history_text"] == "[QQ推荐表情:开心]"


def test_send_recommended_expression_falls_back_to_message_parameter() -> None:
    class _FallbackBot(FakeBot):
        async def call_api(self, api: str, **kwargs):  # noqa: ANN001
            self.calls.append((api, kwargs))
            if api == "get_recommend_face" and "word" in kwargs:
                raise RuntimeError("unsupported parameter")
            if api in self.api_results:
                return self.api_results[api]
            return {}

    async def _run() -> tuple[list[dict], list[tuple[str, dict]], dict]:
        bot = _FallbackBot()
        bot.api_results["get_recommend_face"] = {"url": ["https://example.test/happy.png"]}
        executor = action_executor_mod.ActionExecutor(bot, object(), _config(), _Logger())
        queued: list[dict] = []
        executor.bind_pending_actions(queued)
        result = await _tool_map(executor, bot)["send_qq_recommended_expression"].handler(query="开心")
        return queued, bot.calls, json.loads(result)

    queued, calls, payload = asyncio.run(_run())

    assert calls == [
        ("get_recommend_face", {"word": "开心"}),
        ("get_recommend_face", {"message": "开心"}),
    ]
    assert payload["queued"] is True
    assert queued[0]["params"]["url"] == "https://example.test/happy.png"


def test_render_recommended_expression_uses_word_parameter() -> None:
    async def _run() -> tuple[str, list[tuple[str, dict]]]:
        bot = FakeBot()
        bot.api_results["get_recommend_face"] = {"url": ["https://example.test/happy.png"]}
        rendered = await qq_library.render_qq_expression_message(
            "[QQ推荐表情:开心]",
            message_segment_cls=action_executor_mod.MessageSegment,
            bot=bot,
            plugin_config=_config(),
        )
        return str(rendered.message), bot.calls

    message, calls = asyncio.run(_run())

    assert calls == [("get_recommend_face", {"word": "开心"})]
    assert message.startswith("[CQ:image,file=https://example.test/happy.png")


def test_render_qq_expression_marker_as_inline_face_segment() -> None:
    rendered = asyncio.run(
        qq_library.render_qq_expression_message(
            "好好好[QQ表情:笑哭]",
            message_segment_cls=action_executor_mod.MessageSegment,
            plugin_config=_config(),
        )
    )

    assert str(rendered.message) == "好好好[CQ:face,id=182]"
    assert rendered.history_text == "好好好[QQ表情:笑哭]"
    assert rendered.expression_names == ("笑哭",)


def test_render_qq_expression_marker_disabled_strips_marker() -> None:
    rendered = asyncio.run(
        qq_library.render_qq_expression_message(
            "好好好[QQ表情:笑哭]",
            message_segment_cls=action_executor_mod.MessageSegment,
            plugin_config=_config(qq_expression_enabled=False),
        )
    )

    assert str(rendered.message) == "好好好"
    assert rendered.history_text == "好好好"
    assert rendered.expression_names == ()


def test_render_qq_expression_marker_supports_three_faces() -> None:
    rendered = asyncio.run(
        qq_library.render_qq_expression_message(
            "[QQ表情:捂脸*3]",
            message_segment_cls=action_executor_mod.MessageSegment,
            plugin_config=_config(),
        )
    )

    assert str(rendered.message) == "[CQ:face,id=264][CQ:face,id=264][CQ:face,id=264]"
    assert rendered.history_text == "[QQ表情:捂脸x3]"


def test_render_qq_super_expression_falls_back_to_plain_face_without_mface_metadata() -> None:
    qq_library.reset_qq_expression_state()
    rendered = asyncio.run(
        qq_library.render_qq_expression_message(
            "[QQ超级表情:笑哭]",
            message_segment_cls=action_executor_mod.MessageSegment,
            plugin_config=_config(),
        )
    )

    text = str(rendered.message)
    assert text == "[CQ:face,id=182]"
    assert "large=" not in text
    assert rendered.history_text == "[QQ表情:笑哭]"


def test_render_qq_super_expression_uses_cached_mface_metadata() -> None:
    qq_library.reset_qq_expression_state()
    qq_library.remember_qq_mface_segment(
        {
            "emoji_id": "e-vomit",
            "emoji_package_id": "p-super",
            "key": "k-super",
            "summary": "呕吐",
        },
        kind="super",
    )

    rendered = asyncio.run(
        qq_library.render_qq_expression_message(
            "[QQ超级表情:呕吐]",
            message_segment_cls=action_executor_mod.MessageSegment,
            plugin_config=_config(),
        )
    )

    text = str(rendered.message)
    assert "[CQ:mface" in text
    assert "emoji_id=e-vomit" in text
    assert "emoji_package_id=p-super" in text
    assert "key=k-super" in text
    assert rendered.history_text == "[QQ超级表情:呕吐]"


def test_incoming_expression_segments_render_semantic_text() -> None:
    assert qq_library.semantic_text_for_qq_expression_segment("face", {"id": "182"}) == "[QQ表情:笑哭]"
    token = qq_library.semantic_text_for_qq_expression_segment(
        "mface",
        {
            "emoji_id": "e-vomit",
            "emoji_package_id": "p-super",
            "key": "k-super",
            "summary": "呕吐",
        },
        default_mface_kind="super",
    )
    assert token == "[QQ超级表情:呕吐]"
    assert (
        qq_library.semantic_text_for_qq_expression_segment(
            "image",
            {"emoji_id": "fav-1", "emoji_package_id": "fav-pack", "key": "fav-key", "summary": "拍桌"},
            default_mface_kind="favorite",
        )
        == "[QQ收藏表情:拍桌]"
    )


def test_expression_action_history_text_describes_sent_expression() -> None:
    assert (
        qq_tools.qq_action_history_text({"type": "send_qq_face", "params": {"face_id": 182}})
        == "[QQ表情:笑哭]"
    )
    assert (
        qq_tools.qq_action_history_text(
            {"type": "send_qq_image_expression", "params": {"summary": "拍桌"}}
        )
        == "[QQ收藏表情:拍桌]"
    )


def test_recordable_group_message_extracts_incoming_expression_semantics() -> None:
    event = SimpleNamespace(
        message=[
            _Seg("face", {"id": "182"}),
            _Seg(
                "mface",
                {
                    "emoji_id": "e-vomit",
                    "emoji_package_id": "p-super",
                    "key": "k-super",
                    "summary": "呕吐",
                },
            ),
        ],
        get_plaintext=lambda: "",
    )

    content, image_count, visual_summary = event_rules._extract_recordable_group_message(event)

    assert content == "[QQ表情:笑哭][QQ超级表情:呕吐]"
    assert image_count == 0
    assert "[QQ超级表情:呕吐]" in visual_summary


def test_reply_buffer_plain_text_extracts_incoming_expression_semantics() -> None:
    event = SimpleNamespace(
        message=[
            _Seg("text", {"text": "看这个"}),
            _Seg("face", {"id": "264"}),
            _Seg(
                "mface",
                {
                    "emoji_id": "e-vomit",
                    "emoji_package_id": "p-super",
                    "key": "k-super",
                    "summary": "呕吐",
                },
            ),
        ],
    )

    assert reply_buffer._extract_plain_text(event) == "看这个[QQ表情:捂脸][QQ超级表情:呕吐]"


def test_build_qq_expression_test_messages_cover_three_command_kinds() -> None:
    async def _run():
        bot = FakeBot()
        bot.api_results["fetch_custom_face"] = {"url": ["https://example.test/fav.png"]}
        face = await qq_diagnostics.build_qq_expression_test_message(
            "小黄脸",
            message_segment_cls=action_executor_mod.MessageSegment,
            bot=bot,
            plugin_config=_config(),
        )
        super_face = await qq_diagnostics.build_qq_expression_test_message(
            "超级表情",
            message_segment_cls=action_executor_mod.MessageSegment,
            bot=bot,
            plugin_config=_config(),
        )
        favorite = await qq_diagnostics.build_qq_expression_test_message(
            "收藏表情",
            message_segment_cls=action_executor_mod.MessageSegment,
            bot=bot,
            plugin_config=_config(),
        )
        return face, super_face, favorite, bot.calls

    face, super_face, favorite, calls = asyncio.run(_run())

    assert face.ok is True
    assert face.marker == "[QQ表情:笑哭]"
    assert str(face.render.message) == "[CQ:face,id=182]"
    assert super_face.ok is True
    assert super_face.marker == "[QQ超级表情:笑哭]"
    assert str(super_face.render.message) == "[CQ:face,id=182]"
    assert super_face.render.history_text == "[QQ表情:笑哭]"
    assert favorite.ok is True
    assert favorite.marker == "[QQ收藏表情:随机]"
    assert str(favorite.render.message).startswith("[CQ:image,file=https://example.test/fav.png")
    assert calls == [("fetch_custom_face", {"count": 20})]


def test_qq_expression_prompt_warns_about_ambiguous_smile() -> None:
    prompt = qq_library.build_qq_expression_prompt()

    assert "[QQ表情:笑哭]" in prompt
    assert "微笑" in prompt
    assert "冷淡" in prompt


def test_expression_tool_result_queued() -> None:
    result = json.dumps({"ok": True, "queued": True}, ensure_ascii=False)

    assert qq_tools.expression_tool_result_queued(result) is True
    assert qq_tools.expression_tool_result_queued('{"ok": false, "queued": true}') is False


def test_tool_catalog_expression_intent_exposes_only_expression_tools() -> None:
    registry = tool_registry_mod.ToolRegistry()

    async def _noop(**_kwargs):  # noqa: ANN001
        return "ok"

    for name in ("send_qq_face", "send_qq_favorite_expression", "web_search"):
        registry.register(
            tool_registry_mod.AgentTool(
                name=name,
                description="",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=_noop,
            )
        )

    schemas = tool_catalog.select_tool_schemas(registry, has_images=False, chat_intent="expression")
    names = {tool_catalog.schema_tool_name(schema) for schema in schemas}

    assert names == {"send_qq_face", "send_qq_favorite_expression"}


def test_chat_intent_accepts_expression_payload() -> None:
    class _Caller:
        async def chat_with_tools(self, *args, **kwargs):  # noqa: ANN001, ARG002
            return SimpleNamespace(
                content=(
                    '{"chat_intent":"expression","plugin_question_intent":"capability",'
                    '"ambiguity_level":"low","recommend_silence":false,'
                    '"requires_emotional_care":false,"sticker_appropriate":true,'
                    '"meta_question":false,"domain_focus":"social","user_attitude":"想让 bot 发表情",'
                    '"bot_emotion":"轻松","emotion_intensity":"low","expression_style":"只发表情",'
                    '"tts_style_hint":"自然","sticker_mood_hint":"搞笑|接梗",'
                    '"conversation_scenario":"normal","address_mode":"none",'
                    '"confidence":0.9,"reason":"明确要求发表情"}'
                )
            )

    frame = asyncio.run(
        chat_intent_mod.infer_turn_semantic_frame_with_llm(
            "发个笑哭表情",
            tool_caller=_Caller(),
        )
    )

    assert frame.chat_intent == "expression"
    assert frame.to_intent_decision().chat_intent == "expression"


def test_turn_plan_expression_maps_to_semantic_frame() -> None:
    plan = planner_mod.parse_turn_plan_payload(
        {
            "reply_action": "reply",
            "memory_need": "none",
            "research_need": "none",
            "vision_need": "none",
            "qzone_continue": False,
            "output_mode": "chat_short",
            "tool_intent": ["expression"],
            "ambiguity_level": "low",
            "message_target": "bot",
            "session_goal": "发表情",
            "confidence": 0.9,
            "reason": "explicit_expression",
        }
    )
    frame = planner_mod.turn_plan_to_semantic_frame(plan)

    assert plan.tool_intent == ["expression"]
    assert frame.chat_intent == "expression"
