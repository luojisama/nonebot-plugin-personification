from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


turn_media = load_personification_module("plugin.personification.core.turn_media")
reply_buffer = load_personification_module("plugin.personification.handlers.reply_buffer")


def _image(url: str, file_id: str, *, sub_type: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        type="image",
        data={"url": url, "file": file_id, "sub_type": sub_type},
    )


def _record(file_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="record", data={"file": file_id})


def _video(file_id: str, *, url: str = "") -> SimpleNamespace:
    return SimpleNamespace(type="video", data={"file": file_id, "url": url})


def _file(file_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(type="file", data={"file": file_id, "name": name})


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
    assert serialized[0]["group_id"] == "group-1"
    assert serialized[0]["resolution_code"] == ""
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


def test_quoted_file_media_is_hydrated_through_get_msg() -> None:
    event = _event(
        "current-user",
        "current-message",
        SimpleNamespace(type="text", data={"text": "概括引用的视频"}),
    )
    event.reply = SimpleNamespace(
        message_id="42",
        sender=SimpleNamespace(user_id="quoted-user"),
        message=[],
    )
    calls: list[dict[str, object]] = []

    class _Bot:
        async def get_msg(self, **kwargs):  # noqa: ANN003, ANN201
            calls.append(dict(kwargs))
            return {
                "message_id": 42,
                "sender": {"user_id": "quoted-user"},
                "message": [
                    {"type": "file", "data": {"file": "quoted-token", "name": "clip.mp4"}},
                ],
            }

    refs = asyncio.run(turn_media.resolve_onebot_quoted_media_refs(event, _Bot()))

    assert calls == [{"message_id": 42}]
    assert len(refs) == 1
    assert refs[0].kind == "video"
    assert refs[0].origin == "quoted"
    assert refs[0].owner_user_id == "quoted-user"
    assert refs[0].message_id == "42"
    assert refs[0].file_id == "quoted-token"


def test_reply_segment_message_id_can_hydrate_quoted_file_media() -> None:
    event = SimpleNamespace(
        user_id="current-user",
        message_id="current-message",
        sender=SimpleNamespace(user_id="current-user"),
        message=[
            SimpleNamespace(type="reply", data={"id": "77"}),
            SimpleNamespace(type="text", data={"text": "看这个视频"}),
        ],
    )

    class _Bot:
        async def call_api(self, api: str, **kwargs):  # noqa: ANN003, ANN201
            assert api == "get_msg"
            assert kwargs == {"message_id": 77}
            return {
                "data": {
                    "message_id": 77,
                    "sender": {"user_id": "quoted-user"},
                    "message": [
                        {"type": "video", "data": {"file": "video-token"}},
                    ],
                }
            }

    refs = asyncio.run(turn_media.resolve_onebot_quoted_media_refs(event, _Bot()))

    assert len(refs) == 1
    assert refs[0].origin == "quoted"
    assert refs[0].message_id == "77"
    assert refs[0].file_id == "video-token"


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


def test_record_segment_keeps_audio_provenance_without_eager_conversion() -> None:
    event = _event("speaker", "record-message", _record("opaque-record-token"))

    refs = turn_media.extract_turn_media_from_event(event)

    assert len(refs) == 1
    assert refs[0].kind == "audio"
    assert refs[0].file_id == "opaque-record-token"
    assert refs[0].ref == "opaque-record-token"
    assert refs[0].owner_user_id == "speaker"
    assert refs[0].message_id == "record-message"


def test_onebot_record_is_resolved_to_wav_only_on_demand() -> None:
    event = _event("speaker", "record-message", _record("opaque-record-token"))
    refs = turn_media.extract_turn_media_from_event(event)
    calls: list[dict[str, str]] = []

    class _Bot:
        async def get_record(self, **kwargs):  # noqa: ANN003, ANN201
            calls.append(dict(kwargs))
            return {"file": "C:\\tmp\\record.wav"}

    resolved = asyncio.run(turn_media.resolve_onebot_audio_refs(refs, _Bot()))

    assert calls == [{"file": "opaque-record-token", "out_format": "wav"}]
    assert resolved[0].ref == "C:\\tmp\\record.wav"
    assert resolved[0].file_id == "opaque-record-token"
    assert resolved[0].origin == "current"


def test_onebot_record_resolution_failure_preserves_original_reference() -> None:
    event = _event("speaker", "record-message", _record("opaque-record-token"))
    refs = turn_media.extract_turn_media_from_event(event)

    class _Bot:
        async def call_api(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("adapter unavailable")

    resolved = asyncio.run(turn_media.resolve_onebot_audio_refs(refs, _Bot()))

    assert resolved[0] == refs[0]


def test_file_segment_with_video_extension_enters_video_media_context() -> None:
    event = _event(
        "speaker",
        "file-message",
        _file("opaque-file-token", "gameplay.MP4"),
    )

    refs = turn_media.extract_turn_media_from_event(event)

    assert len(refs) == 1
    assert refs[0].kind == "video"
    assert refs[0].ref == "opaque-file-token"
    assert refs[0].file_id == "opaque-file-token"
    assert refs[0].owner_user_id == "speaker"
    assert refs[0].message_id == "file-message"


def test_non_video_file_segment_is_not_exposed_to_video_understanding() -> None:
    event = _event(
        "speaker",
        "file-message",
        _file("opaque-file-token", "notes.txt"),
    )

    assert turn_media.extract_turn_media_from_event(event) == []


def test_onebot_video_token_is_resolved_through_get_file_on_demand(tmp_path: Path) -> None:
    video_path = tmp_path / "gameplay.mp4"
    video_path.write_bytes(b"video")
    event = _event(
        "speaker",
        "video-message",
        _video("opaque-video-token"),
    )
    refs = turn_media.extract_turn_media_from_event(event)
    calls: list[dict[str, str]] = []

    class _Bot:
        async def get_file(self, **kwargs):  # noqa: ANN003, ANN201
            calls.append(dict(kwargs))
            return {
                "file": str(video_path),
                "url": "",
                "file_name": "gameplay.mp4",
            }

    resolved = asyncio.run(turn_media.resolve_onebot_video_refs(refs, _Bot()))

    assert calls == [{"file": "opaque-video-token"}]
    assert resolved[0].ref == str(video_path.resolve())
    assert resolved[0].file_id == "opaque-video-token"
    assert resolved[0].origin == "current"
    assert resolved[0].content_hash == refs[0].content_hash


def test_file_form_video_prefers_adapter_https_url() -> None:
    event = _event(
        "speaker",
        "file-message",
        _file("opaque-file-token", "clip.mp4"),
    )
    refs = turn_media.extract_turn_media_from_event(event)

    class _Bot:
        async def call_api(self, api: str, **kwargs):  # noqa: ANN003, ANN201
            assert api == "get_file"
            assert kwargs == {"file": "opaque-file-token"}
            return {
                "file": "C:\\remote-host\\clip.mp4",
                "url": "https://multimedia.nt.qq.com.cn/download/opaque",
                "file_name": "clip.mp4",
            }

    resolved = asyncio.run(turn_media.resolve_onebot_video_refs(refs, _Bot()))

    assert resolved[0].ref == "https://multimedia.nt.qq.com.cn/download/opaque"
    assert resolved[0].resolution_code == "onebot_get_file_url"


def test_private_file_video_falls_back_to_download_url_for_cross_host_path() -> None:
    event = SimpleNamespace(
        user_id="speaker",
        message_id="file-message",
        sender=SimpleNamespace(user_id="speaker"),
        message=[_file("opaque-file-token", "clip.mp4")],
    )
    refs = turn_media.extract_turn_media_from_event(event)
    calls: list[tuple[str, dict[str, str]]] = []

    class _Bot:
        async def call_api(self, api: str, **kwargs):  # noqa: ANN003, ANN201
            calls.append((api, dict(kwargs)))
            if api == "get_file":
                return {"data": {"file": "C:\\napcat-host\\clip.mp4"}}
            assert api == "get_private_file_url"
            return {
                "data": {
                    "url": "https://multimedia.nt.qq.com.cn/download/private-opaque"
                }
            }

    resolved = asyncio.run(turn_media.resolve_onebot_video_refs(refs, _Bot()))

    assert calls == [
        ("get_file", {"file": "opaque-file-token"}),
        ("get_private_file_url", {"file_id": "opaque-file-token"}),
    ]
    assert resolved[0].ref == "https://multimedia.nt.qq.com.cn/download/private-opaque"
    assert resolved[0].resolution_code == "onebot_private_file_url"


def test_group_file_video_uses_group_download_url_with_preserved_group() -> None:
    event = _event(
        "speaker",
        "file-message",
        _file("opaque-group-file-token", "clip.mp4"),
    )
    refs = turn_media.extract_turn_media_from_event(event)
    calls: list[tuple[str, dict[str, str]]] = []

    class _Bot:
        async def call_api(self, api: str, **kwargs):  # noqa: ANN003, ANN201
            calls.append((api, dict(kwargs)))
            if api == "get_file":
                return {"file": "/napcat-host/clip.mp4"}
            assert api == "get_group_file_url"
            return {"url": "https://multimedia.nt.qq.com.cn/download/group-opaque"}

    resolved = asyncio.run(turn_media.resolve_onebot_video_refs(refs, _Bot()))

    assert refs[0].group_id == "group-1"
    assert calls == [
        ("get_file", {"file": "opaque-group-file-token"}),
        (
            "get_group_file_url",
            {"file_id": "opaque-group-file-token", "group_id": "group-1"},
        ),
    ]
    assert resolved[0].ref == "https://multimedia.nt.qq.com.cn/download/group-opaque"
    assert resolved[0].resolution_code == "onebot_group_file_url"


def test_resolved_video_url_does_not_call_onebot_again() -> None:
    event = _event(
        "speaker",
        "video-message",
        _video("opaque-token", url="https://cdn.example/video.mp4"),
    )
    refs = turn_media.extract_turn_media_from_event(event)

    class _Bot:
        async def call_api(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            raise AssertionError("resolved HTTPS video must not call get_file")

    resolved = asyncio.run(turn_media.resolve_onebot_video_refs(refs, _Bot()))

    assert resolved == refs


def test_onebot_video_resolution_failure_preserves_original_reference() -> None:
    event = _event(
        "speaker",
        "video-message",
        _video("opaque-video-token"),
    )
    refs = turn_media.extract_turn_media_from_event(event)

    class _Bot:
        async def call_api(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("adapter unavailable")

    resolved = asyncio.run(turn_media.resolve_onebot_video_refs(refs, _Bot()))

    assert resolved[0].ref == refs[0].ref
    assert resolved[0].file_id == refs[0].file_id
    assert resolved[0].resolution_code == "onebot_video_resolve_failed"

    summary = turn_media.summarize_media_resolution(resolved)
    assert summary == {
        "videos": 1,
        "video_usable": 0,
        "video_failed": 1,
        "audios": 0,
        "resolution_codes": ["onebot_video_resolve_failed"],
    }
