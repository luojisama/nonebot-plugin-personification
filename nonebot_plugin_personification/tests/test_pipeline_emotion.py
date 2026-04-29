from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

pipeline_emotion = load_personification_module("plugin.personification.handlers.reply_pipeline.pipeline_emotion")


def test_should_speak_in_random_chat_accepts_recent_context_alias(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    async def _fake_decide(  # noqa: ANN001
        _call_ai_api,
        *,
        raw_message_text,
        recent_context,
        relationship_hint,
        repeat_clusters,
        recent_bot_replies,
        has_newer_batch,
        message_intent,
        ambiguity_level,
        message_target,
        solo_speaker_follow,
    ) -> bool:
        captured.update(
            {
                "raw_message_text": raw_message_text,
                "recent_context": recent_context,
                "relationship_hint": relationship_hint,
                "repeat_clusters": repeat_clusters,
                "recent_bot_replies": recent_bot_replies,
                "has_newer_batch": has_newer_batch,
                "message_intent": message_intent,
                "ambiguity_level": ambiguity_level,
                "message_target": message_target,
                "solo_speaker_follow": solo_speaker_follow,
            }
        )
        return True

    monkeypatch.setattr(pipeline_emotion, "decide_random_chat_speak", _fake_decide)

    runtime = SimpleNamespace(
        lite_call_ai_api=None,
        call_ai_api=object(),
    )

    result = asyncio.run(
        pipeline_emotion.should_speak_in_random_chat(
            runtime=runtime,
            state={},
            raw_message_text="吓哭了刚做噩梦了",
            message_text="吓哭了刚做噩梦了",
            message_content="吓哭了刚做噩梦了",
            recent_context="群里刚在聊睡觉和做梦",
            relationship_hint="你们平时会接这种情绪话题",
            repeat_clusters=None,
            recent_bot_replies=["前一条机器人回复"],
            message_intent="banter",
            ambiguity_level="low",
            message_target="bot",
            solo_speaker_follow=False,
            knowledge_store=object(),
        )
    )

    assert result is True
    assert captured["recent_context"] == "群里刚在聊睡觉和做梦"
    assert captured["raw_message_text"] == "吓哭了刚做噩梦了"
