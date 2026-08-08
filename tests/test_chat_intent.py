from __future__ import annotations
import asyncio

from ._loader import load_personification_module

chat_intent = load_personification_module("plugin.personification.core.chat_intent")


def test_normalize_intent_text_strips_whitespace_and_newlines() -> None:
    assert chat_intent.normalize_intent_text("  你好\n\n 世界 \r\n ") == "你好 世界"


def test_metadata_fallback_turn_semantic_frame_respects_group_metadata() -> None:
    group_frame = chat_intent.metadata_fallback_turn_semantic_frame_for_session(is_group=True, is_random_chat=True)
    private_frame = chat_intent.metadata_fallback_turn_semantic_frame_for_session(is_group=False, is_random_chat=False)

    assert group_frame.chat_intent == "banter"
    assert group_frame.ambiguity_level == "high"
    assert group_frame.recommend_silence is True
    assert private_frame.chat_intent == "banter"
    assert private_frame.ambiguity_level == "low"
    assert private_frame.recommend_silence is False


def test_parse_turn_semantic_frame_payload_handles_valid_and_invalid_dicts() -> None:
    valid = chat_intent._parse_turn_semantic_frame_payload(
        {
            "chat_intent": "image_generation",
            "plugin_question_intent": "latest",
            "ambiguity_level": "medium",
            "recommend_silence": True,
            "requires_emotional_care": True,
            "sticker_appropriate": False,
            "meta_question": True,
            "domain_focus": "plugin",
            "evidence_policy": "strict",
            "emotional_support": {
                "needed": True,
                "listen": True,
                "validate": True,
                "advice_permission": "ask_first",
                "risk_level": "concern",
            },
            "user_attitude": "认真追问",
            "bot_emotion": "平静",
            "emotion_intensity": "high",
            "expression_style": "直接一点",
            "tts_style_hint": "自然",
            "sticker_mood_hint": "淡定|表达疑惑",
            "group_atmosphere_positive": True,
            "interaction_interesting": True,
            "future_commitment_candidate": True,
            "confidence": 0.8,
            "reason": "test",
        }
    )
    invalid = chat_intent._parse_turn_semantic_frame_payload({"chat_intent": "unknown"})

    assert valid is not None
    assert valid.chat_intent == "image_generation"
    assert valid.plugin_question_intent == "latest"
    assert valid.ambiguity_level == "medium"
    assert valid.recommend_silence is True
    assert valid.requires_emotional_care is True
    assert valid.sticker_appropriate is False
    assert valid.evidence_policy == "strict"
    assert valid.emotional_support.risk_level == "concern"
    assert valid.group_atmosphere_positive is True
    assert valid.interaction_interesting is True
    assert valid.future_commitment_candidate is True
    assert invalid is None


def test_low_confidence_undirected_random_group_frame_recommends_silence() -> None:
    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"chat_intent":"banter","plugin_question_intent":"capability","ambiguity_level":"low",'
                        '"recommend_silence":false,"domain_focus":"social","confidence":0.2}'
                    )
                },
            )()

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "这句很不确定",
            is_group=True,
            is_random_chat=True,
            message_target="others",
            tool_caller=_Caller(),
        )
    )

    assert frame.recommend_silence is True
    assert frame.ambiguity_level == "high"


def test_low_confidence_directed_group_frame_is_not_forced_silent() -> None:
    captured: dict[str, object] = {}

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            captured["messages"] = messages
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"chat_intent":"banter","ambiguity_level":"low",'
                        '"recommend_silence":true,"confidence":0.2}'
                    )
                },
            )()

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "这句很不确定，但我正在叫你",
            is_group=True,
            is_random_chat=True,
            is_direct_mention=True,
            message_target="target_bot",
            tool_caller=_Caller(),
        )
    )

    system_prompt = captured["messages"][0]["content"]  # type: ignore[index]
    user_prompt = captured["messages"][1]["content"]  # type: ignore[index]
    assert frame.ambiguity_level == "high"
    assert frame.recommend_silence is False
    assert "结构化消息目标：target_bot" in user_prompt
    assert "陌生的游戏黑话" in system_prompt
    assert "不能只因为本轮是随机插话" in system_prompt
    assert "目标只是 unclear 时" in system_prompt
    assert "不能等同于 target_others" in system_prompt
    assert "粥/三角洲" not in system_prompt
    assert "dd/DD" not in system_prompt


def test_semantic_frame_prompt_includes_media_context_discipline() -> None:
    captured: dict[str, object] = {}

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            captured["messages"] = messages
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"chat_intent":"banter","plugin_question_intent":"capability",'
                        '"ambiguity_level":"high","recommend_silence":true,'
                        '"domain_focus":"social","confidence":0.86,"reason":"媒体占位"}'
                    )
                },
            )()

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "[图片·表情包]",
            is_group=True,
            is_random_chat=True,
            recent_context="刚才群友在讨论一道雷劈在眼前，后面有人说吓哭了。",
            tool_caller=_Caller(),
        )
    )

    system_prompt = captured["messages"][0]["content"]  # type: ignore[index]
    assert "媒体占位纪律" in system_prompt
    assert "不要假装知道画面内容" in system_prompt
    assert "最近上下文已经说明原因" in system_prompt
    assert "相邻图片、表情或截图不能覆盖直接 cue 的文字问题" in system_prompt
    assert "其它插件 interaction episode" in system_prompt
    assert "不要因为插件结果包含专业名词" in system_prompt
    assert "‘尚未分析’不等于‘无法查看’" in system_prompt
    assert frame.recommend_silence is True


def test_semantic_frame_prompt_treats_direct_mention_as_turn_cue_not_formal_qa() -> None:
    captured: dict[str, object] = {}

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            captured["messages"] = messages
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"chat_intent":"banter","plugin_question_intent":"capability",'
                        '"ambiguity_level":"low","recommend_silence":false,'
                        '"user_attitude":"调侃甩锅","bot_emotion":"不服气",'
                        '"expression_style":"短句反击","confidence":0.9}'
                    )
                },
            )()

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "不小心把糯米撒你身上，你嗷嗷叫半天",
            is_group=True,
            is_direct_mention=True,
            tool_caller=_Caller(),
        )
    )

    system_prompt = captured["messages"][0]["content"]  # type: ignore[index]
    user_prompt = captured["messages"][1]["content"]  # type: ignore[index]
    assert "只表示这轮在叫你回应" in system_prompt
    assert "调侃、甩锅、轻挑衅" in system_prompt
    assert "是否明确 @/直呼 bot：是" in user_prompt
    assert frame.chat_intent == "banter"
    assert frame.recommend_silence is False


def test_semantic_frame_treats_avatar_visibility_as_runtime_capability() -> None:
    captured: dict[str, object] = {}

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            captured["messages"] = messages
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"chat_intent":"plugin_question","plugin_question_intent":"runtime_capability",'
                        '"ambiguity_level":"low","recommend_silence":false,"confidence":0.95}'
                    )
                },
            )()

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "你能看见我头像吗",
            is_group=False,
            tool_caller=_Caller(),
        )
    )

    system_prompt = captured["messages"][0]["content"]  # type: ignore[index]
    assert "runtime_capability 只覆盖" in system_prompt
    assert "头像中的可观察画面事实" in system_prompt
    assert "安全头像摘要" not in system_prompt
    assert "其它用户资料、记忆、群信息或会话能力问题仍使用 plugin_question/capability" in system_prompt
    assert frame.chat_intent == "plugin_question"
    assert frame.plugin_question_intent == "runtime_capability"


def test_semantic_frame_prompt_uses_structured_favorability_signals() -> None:
    captured: dict[str, object] = {}

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            captured["messages"] = messages
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"chat_intent":"banter","group_atmosphere_positive":true,'
                        '"interaction_interesting":true,"confidence":0.9}'
                    )
                },
            )()

    frame = asyncio.run(
        chat_intent.infer_turn_semantic_frame_with_llm(
            "今天聊得真不错",
            is_group=True,
            tool_caller=_Caller(),
        )
    )
    prompt = captured["messages"][0]["content"]  # type: ignore[index]

    assert '"group_atmosphere_positive":false' in prompt
    assert "普通无冲突聊天不能机械设为 true" in prompt
    assert "成功回复本身不等于有趣" in prompt
    assert frame.group_atmosphere_positive is True
    assert frame.interaction_interesting is True


def test_parse_address_mode_field() -> None:
    """LLM 输出的 address_mode 被解析进语义帧；非法/缺失回退 auto。"""
    assert chat_intent._parse_turn_semantic_frame_payload(
        {"chat_intent": "banter", "address_mode": "at"}
    ).address_mode == "at"
    assert chat_intent._parse_turn_semantic_frame_payload(
        {"chat_intent": "banter", "address_mode": "at_quote"}
    ).address_mode == "at_quote"
    assert chat_intent._parse_turn_semantic_frame_payload(
        {"chat_intent": "banter", "address_mode": "不存在"}
    ).address_mode == "auto"
    assert chat_intent._parse_turn_semantic_frame_payload(
        {"chat_intent": "banter"}
    ).address_mode == "auto"
    assert chat_intent.TurnSemanticFrame().address_mode == "auto"


def test_semantic_frame_rejects_unknown_domain_and_evidence_values() -> None:
    frame = chat_intent._parse_turn_semantic_frame_payload(
        {"chat_intent": "banter", "domain_focus": "knowledge", "evidence_policy": "maximum"}
    )

    assert frame.domain_focus == "general"
    assert frame.evidence_policy == "none"


def test_semantic_frame_maps_legacy_knowledge_domain() -> None:
    frame = chat_intent._parse_turn_semantic_frame_payload(
        {"chat_intent": "explanation", "domain_focus": "knowledge"}
    )

    assert frame.domain_focus == "general"
