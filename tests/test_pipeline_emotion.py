from __future__ import annotations

from ._loader import load_personification_module

pipeline_emotion = load_personification_module("plugin.personification.handlers.reply_pipeline.pipeline_emotion")


def test_should_speak_in_random_chat_solo_speaker_returns_true(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return False

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="others",
        solo_speaker_follow=True,
    )
    assert result is True


def test_should_speak_in_random_chat_newer_batch_returns_false(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return True

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="others",
        solo_speaker_follow=False,
    )
    assert result is False


def test_should_speak_in_random_chat_target_bot_returns_true(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return False

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="bot",
        solo_speaker_follow=False,
    )
    assert result is True


def test_should_speak_in_random_chat_default_returns_true(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return False

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="others",
        solo_speaker_follow=False,
    )
    assert result is True
