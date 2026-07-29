from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


turn_media = load_personification_module("plugin.personification.core.turn_media")
reply_buffer = load_personification_module("plugin.personification.handlers.reply_buffer")


def _image(url: str, file_id: str, *, sub_type: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        type="image",
        data={"url": url, "file": file_id, "sub_type": sub_type},
    )


def _event(user_id: str, message_id: str, image: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        message_id=message_id,
        group_id="group-1",
        sender=SimpleNamespace(user_id=user_id, card=f"speaker-{user_id}", nickname=""),
        message=[image],
    )


def test_turn_media_serialization_never_persists_data_url() -> None:
    event = _event("u1", "m1", _image("data:image/png;base64,YWJj", "file-a"))

    refs = turn_media.extract_turn_media_from_event(event)
    serialized = turn_media.serialize_turn_media(refs)

    assert len(serialized) == 1
    assert serialized[0]["ref"] == ""
    assert serialized[0]["file_id"] == "file-a"
    assert serialized[0]["content_hash"]
    assert "data:image" not in str(serialized)


def test_batched_media_keeps_each_owner_and_selected_origin() -> None:
    first = _event("u1", "m1", _image("https://img.example/a.png", "file-a"))
    second = _event("u2", "m2", _image("https://img.example/b.png", "file-b"))

    first_payload = reply_buffer._serialize_batched_event(
        {"event": first},
        selected_event=second,
    )
    second_payload = reply_buffer._serialize_batched_event(
        {"event": second},
        selected_event=second,
    )

    assert first_payload["media"][0]["owner_user_id"] == "u1"
    assert first_payload["media"][0]["message_id"] == "m1"
    assert first_payload["media"][0]["origin"] == "batch"
    assert second_payload["media"][0]["owner_user_id"] == "u2"
    assert second_payload["media"][0]["message_id"] == "m2"
    assert second_payload["media"][0]["origin"] == "current"
    assert first_payload["media"][0]["media_id"] != second_payload["media"][0]["media_id"]


def test_reply_buffer_state_aggregates_multi_user_media_without_owner_drift() -> None:
    class _Message(list):
        pass

    class _MessageSegment:
        @staticmethod
        def text(value: str) -> SimpleNamespace:
            return SimpleNamespace(type="text", data={"text": value})

    logger = SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    first = _event("u1", "m1", _image("https://img.example/a.png", "file-a"))
    second = _event("u2", "m2", _image("https://img.example/b.png", "file-b"))
    entry = reply_buffer._new_entry(0.0)
    entry["items"] = [
        {"event": first, "state": {}, "is_direct_mention": False, "is_reply_to_bot": False},
        {"event": second, "state": {}, "is_direct_mention": False, "is_reply_to_bot": False},
    ]
    captured: dict[str, object] = {}

    async def _process(_bot, _event, state):  # noqa: ANN001
        captured.update(state)

    asyncio.run(
        reply_buffer.run_buffer_timer(
            "bot:group-1",
            SimpleNamespace(self_id="bot"),
            msg_buffer={"bot:group-1": entry},
            process_response_logic=_process,
            message_event_cls=SimpleNamespace,
            message_cls=_Message,
            message_segment_cls=_MessageSegment,
            logger=logger,
            delay=0,
            response_timeout_seconds=30,
        )
    )

    media = captured["turn_media_context"]  # type: ignore[index]
    assert [(item["owner_user_id"], item["origin"]) for item in media] == [
        ("u1", "batch"),
        ("u2", "current"),
    ]
    assert [item["message_id"] for item in media] == ["m1", "m2"]


def test_quoted_and_current_media_keep_distinct_owners_and_messages() -> None:
    event = _event("current-user", "current-message", _image("https://img.example/current.png", "current-file"))
    event.reply = SimpleNamespace(
        message_id="quoted-message",
        sender=SimpleNamespace(user_id="quoted-user"),
        message=[_image("https://img.example/quoted.png", "quoted-file")],
    )

    refs = turn_media.extract_turn_media_from_event(event)
    by_origin = {item.origin: item for item in refs}

    assert by_origin["current"].owner_user_id == "current-user"
    assert by_origin["current"].message_id == "current-message"
    assert by_origin["quoted"].owner_user_id == "quoted-user"
    assert by_origin["quoted"].message_id == "quoted-message"
    assert by_origin["current"].file_id == "current-file"
    assert by_origin["quoted"].file_id == "quoted-file"


def test_visual_grounding_separates_image_subjects_from_chat_participants() -> None:
    event = _event("u1", "m1", _image("https://img.example/anime.png", "anime-file"))
    refs = turn_media.attach_safe_visual_summary(
        turn_media.extract_turn_media_from_event(event),
        "动漫插画里有多人，人物视线朝向画面中央。",
    )

    grounding = turn_media.render_turn_media_grounding(refs)

    assert "owner_user_id=u1" in grounding
    assert "画中主体只是媒体内容，不是聊天参与者" in grounding
    assert "不证明群友在现实中围观、施压" in grounding
    assert "动漫插画里有多人" in grounding
