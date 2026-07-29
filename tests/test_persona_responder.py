from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

responder = load_personification_module("plugin.personification.agent.runtime.responder")


def test_parse_persona_response_json_and_apply_style_signals() -> None:
    parsed = responder.parse_persona_response(
        '{"reply_text":"别复读了，讲重点。","info_added":"redirect",'
        '"echoed_user_phrase":true,"tts_style_hint":"轻快",'
        '"sticker_mood_hint":"吐槽","sticker_appropriate":false}'
    )

    assert parsed is not None
    assert parsed.reply_text == "别复读了，讲重点。"
    assert parsed.info_added == "redirect"
    assert parsed.echoed_user_phrase is True

    frame = SimpleNamespace(output_mode="chat_short")
    responder.apply_persona_response_to_semantic_frame(parsed, frame)

    assert frame.persona_response_info_added == "redirect"
    assert frame.persona_response_echoed_user_phrase is True
    assert frame.tts_style_hint == "轻快"
    assert frame.sticker_mood_hint == "吐槽"
    assert frame.sticker_appropriate is False


def test_persona_responder_instruction_is_inserted_into_system_message() -> None:
    messages = [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}]
    frame = SimpleNamespace(output_mode="structured_help", session_goal="帮用户找资料", bot_emotion="有点好奇")

    updated = responder.with_persona_responder_instruction(
        messages,
        semantic_frame=frame,
        is_direct_mention=True,
        relationship_hint="最近经常一起聊游戏",
        recent_bot_replies=["刚刚吐槽过一次"],
    )

    assert updated[0]["role"] == "system"
    assert "PersonaResponder JSON 输出要求" in updated[0]["content"]
    assert "作者旁白/角色方向" in updated[0]["content"]
    assert "output_mode=structured_help" in updated[0]["content"]
    assert "直呼/提及时禁止输出 [NO_REPLY]" in updated[0]["content"]
    assert "帮用户找资料" in updated[0]["content"]
    assert "最近经常一起聊游戏" in updated[0]["content"]
    assert "不要把失败或不确定状态写成 reply_text" in updated[0]["content"]
    assert "空证据可见输出纪律" in updated[0]["content"]
    assert "info_added 设为 'redirect'" in updated[0]["content"]
    assert messages[0]["content"] == "base"
