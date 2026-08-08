from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


data_store = load_personification_module("plugin.personification.core.data_store")
emotion_state = load_personification_module("plugin.personification.core.emotion_state")
sticker_feedback = load_personification_module("plugin.personification.core.sticker_feedback")


def _frame(label: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_attitude=f"态度-{label}",
        bot_emotion=f"情绪-{label}",
        emotion_intensity="medium",
        expression_style=f"表达-{label}",
        tts_style_hint="自然",
        sticker_mood_hint="淡定|表达疑惑",
    )


def test_emotion_state_atomic_mutation_completes_and_preserves_concurrent_users(tmp_path) -> None:  # noqa: ANN001
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))

    async def _run() -> dict:
        await asyncio.wait_for(
            asyncio.gather(
                emotion_state.update_emotion_state_after_turn(
                    tmp_path,
                    user_id="10001",
                    semantic_frame=_frame("一"),
                    assistant_text="回复一",
                    is_private=True,
                ),
                emotion_state.update_emotion_state_after_turn(
                    tmp_path,
                    user_id="10002",
                    semantic_frame=_frame("二"),
                    assistant_text="回复二",
                    is_private=True,
                ),
            ),
            timeout=2.0,
        )
        return await emotion_state.load_emotion_state(tmp_path)

    state = asyncio.run(_run())

    assert state["per_user"]["10001"]["last_reply"] == "回复一"
    assert state["per_user"]["10002"]["last_reply"] == "回复二"


def test_sticker_feedback_atomic_mutation_completes_without_lost_updates(tmp_path) -> None:  # noqa: ANN001
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))

    async def _run() -> dict:
        await asyncio.wait_for(
            asyncio.gather(
                *(sticker_feedback.record_sticker_sent("cheer") for _ in range(8))
            ),
            timeout=2.0,
        )
        return await sticker_feedback.load_sticker_feedback()

    state = asyncio.run(_run())

    assert state["items"]["cheer"]["sent_count"] == 8
