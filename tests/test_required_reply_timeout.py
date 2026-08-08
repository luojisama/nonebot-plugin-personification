from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


processor = load_personification_module("plugin.personification.handlers.reply_pipeline.processor")
reply_buffer = load_personification_module("plugin.personification.handlers.reply_buffer")
reply_commit = load_personification_module("plugin.personification.handlers.reply_commit")
reply_turn_trace = load_personification_module("plugin.personification.core.reply_turn_trace")
yaml_processor = load_personification_module("plugin.personification.handlers.yaml_pipeline.processor")
yaml_handler = load_personification_module("plugin.personification.handlers.yaml_response_handler")


def test_cancelled_reply_processor_does_not_finish_as_no_reply(monkeypatch) -> None:  # noqa: ANN001
    finished: list[dict[str, object]] = []

    async def _cancelled_impl(*_args, **_kwargs):  # noqa: ANN001
        raise asyncio.CancelledError

    monkeypatch.setattr(processor, "_process_response_logic_impl", _cancelled_impl)
    monkeypatch.setattr(reply_turn_trace, "current_trace_id", lambda: "")
    monkeypatch.setattr(reply_turn_trace, "start_trace", lambda **_kwargs: "trace-required")
    monkeypatch.setattr(reply_turn_trace, "set_current_trace_id", lambda _trace_id: object())
    monkeypatch.setattr(reply_turn_trace, "reset_current_trace_id", lambda _token: None)
    monkeypatch.setattr(reply_turn_trace, "record_stage", lambda **_kwargs: None)
    monkeypatch.setattr(reply_turn_trace, "get_trace", lambda _trace_id: {})
    monkeypatch.setattr(reply_turn_trace, "finish_trace", lambda **kwargs: finished.append(kwargs))

    state: dict[str, object] = {}
    deps = SimpleNamespace(runtime=SimpleNamespace(plugin_config=SimpleNamespace(personification_turn_trace_enabled=True)))
    event = SimpleNamespace(user_id=1, message_id=2)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(processor.process_response_logic(SimpleNamespace(), event, state, deps))

    assert state["reply_trace_id"] == "trace-required"
    assert finished == []


@pytest.mark.parametrize(
    ("provider_code", "expected_key", "expected_diagnosis"),
    [
        ("provider_auth_failed", "provider_failure", "provider_auth_failed"),
        ("", "unhandled_exception", "internal_exception"),
    ],
)
def test_reply_processor_classifies_and_redacts_outer_failures(
    monkeypatch,
    provider_code: str,
    expected_key: str,
    expected_diagnosis: str,
) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []
    finished: list[dict[str, object]] = []
    error = RuntimeError("https://private.example/chat?api_key=top-secret raw body")
    if provider_code:
        error.code = provider_code
        error.route_attempts = (
            {
                "provider": "zellon",
                "api_type": "gemini",
                "model": "gemini-3-flash-agent",
                "status_code": 400,
                "code": provider_code,
                "wire_tools_count": 47,
                "tool_schema_hash": "safehash1234",
                "request_count": 1,
                "api_url": "https://private.example/v1beta?key=top-secret",
                "response_body": "raw body top-secret",
            },
        )

    async def _failed_impl(*_args, **_kwargs):  # noqa: ANN001
        raise error

    monkeypatch.setattr(processor, "_process_response_logic_impl", _failed_impl)
    monkeypatch.setattr(reply_turn_trace, "current_trace_id", lambda: "")
    monkeypatch.setattr(reply_turn_trace, "start_trace", lambda **_kwargs: "trace-failure")
    monkeypatch.setattr(reply_turn_trace, "set_current_trace_id", lambda _trace_id: object())
    monkeypatch.setattr(reply_turn_trace, "reset_current_trace_id", lambda _token: None)
    monkeypatch.setattr(reply_turn_trace, "record_stage", lambda **kwargs: stages.append(kwargs))
    monkeypatch.setattr(reply_turn_trace, "get_trace", lambda _trace_id: {"outcome": "failed"})
    monkeypatch.setattr(reply_turn_trace, "finish_trace", lambda **kwargs: finished.append(kwargs))
    deps = SimpleNamespace(runtime=SimpleNamespace(plugin_config=SimpleNamespace(personification_turn_trace_enabled=True)))

    with pytest.raises(RuntimeError):
        asyncio.run(processor.process_response_logic(
            SimpleNamespace(),
            SimpleNamespace(user_id=1, message_id=2),
            {},
            deps,
        ))

    stage = next(item for item in stages if item["key"] == expected_key)
    assert finished[-1]["diagnosis_code"] == expected_diagnosis
    assert "top-secret" not in str(stage)
    assert "private.example" not in str(stage)
    if provider_code:
        assert "provider:zellon" in str(stage)
        assert "model:gemini-3-flash-agent" in str(stage)
        assert "tools:47" in str(stage)


def test_required_image_timeout_stays_silent_and_finishes_failed(monkeypatch) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []
    finished: list[dict[str, object]] = []
    monkeypatch.setattr(reply_turn_trace, "record_stage", lambda **kwargs: stages.append(kwargs))
    monkeypatch.setattr(reply_turn_trace, "finish_trace", lambda **kwargs: finished.append(kwargs))

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, _event, message):  # noqa: ANN001
            self.sent.append(str(message))

    event = SimpleNamespace(
        message=[SimpleNamespace(type="text", data={"text": "你翻译一下"}), SimpleNamespace(type="image", data={})]
    )
    bot = _Bot()

    asyncio.run(
        reply_buffer._handle_reply_timeout(
            bot=bot,
            event=event,
            state={"reply_required": True, "reply_trace_id": "trace-required"},
            session_key="bot:private_1",
            timeout_seconds=180.0,
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
            commit_lock=asyncio.Lock(),
        )
    )

    assert bot.sent == []
    assert stages[-1]["trace_id"] == "trace-required"
    assert stages[-1]["key"] == "reply_timeout"
    assert "delivery_state=not_started" in str(stages[-1]["detail"])
    assert finished[-1]["outcome"] == "failed"
    assert finished[-1]["diagnosis_code"] == "reply_timeout"
    assert finished[-1]["detail"]["silent"] is True


def test_timeout_after_confirmed_delivery_does_not_send_fallback(monkeypatch) -> None:  # noqa: ANN001
    finished: list[dict[str, object]] = []
    stages: list[dict[str, object]] = []
    monkeypatch.setattr(reply_turn_trace, "record_stage", lambda **kwargs: stages.append(kwargs))
    monkeypatch.setattr(reply_turn_trace, "finish_trace", lambda **kwargs: finished.append(kwargs))

    class _Bot:
        async def send(self, _event, _message):  # noqa: ANN001
            raise AssertionError("confirmed delivery must not trigger fallback")

    state = {
        "reply_required": True,
        "reply_trace_id": "trace-confirmed",
        "reply_delivery_started": True,
        "reply_delivery_confirmed": True,
        "reply_delivery_complete": True,
    }
    reply_commit.begin_reply_lifecycle(state)
    reply_commit.mark_reply_phase(state, "post_send_bookkeeping")

    asyncio.run(
        reply_buffer._handle_reply_timeout(
            bot=_Bot(),
            event=SimpleNamespace(message=[]),
            state=state,
            session_key="bot:private_1",
            timeout_seconds=180.0,
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
    )

    assert finished[-1]["outcome"] == "ok"
    assert finished[-1]["diagnosis_code"] == "post_send_timeout"
    assert "last_phase=post_send_bookkeeping" in str(stages[-1]["detail"])
    assert finished[-1]["detail"]["last_phase"] == "post_send_bookkeeping"
    assert isinstance(finished[-1]["detail"]["elapsed_ms"], int)


def test_timeout_after_first_segment_is_partial_not_ok(monkeypatch) -> None:  # noqa: ANN001
    finished: list[dict[str, object]] = []
    monkeypatch.setattr(reply_turn_trace, "record_stage", lambda **_kwargs: None)
    monkeypatch.setattr(reply_turn_trace, "finish_trace", lambda **kwargs: finished.append(kwargs))

    asyncio.run(
        reply_buffer._handle_reply_timeout(
            bot=SimpleNamespace(),
            event=SimpleNamespace(message=[]),
            state={
                "reply_required": True,
                "reply_trace_id": "trace-partial",
                "reply_delivery_started": True,
                "reply_delivery_confirmed": True,
            },
            session_key="bot:private_1",
            timeout_seconds=180.0,
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
    )

    assert finished[-1]["outcome"] == "partial"
    assert finished[-1]["diagnosis_code"] == "partial_reply_timeout"


def test_timeout_with_unknown_send_outcome_does_not_retry(monkeypatch) -> None:  # noqa: ANN001
    finished: list[dict[str, object]] = []
    monkeypatch.setattr(reply_turn_trace, "record_stage", lambda **_kwargs: None)
    monkeypatch.setattr(reply_turn_trace, "finish_trace", lambda **kwargs: finished.append(kwargs))

    class _Bot:
        async def send(self, _event, _message):  # noqa: ANN001
            raise AssertionError("unknown send outcome must not be retried")

    asyncio.run(
        reply_buffer._handle_reply_timeout(
            bot=_Bot(),
            event=SimpleNamespace(message=[]),
            state={
                "reply_required": True,
                "reply_trace_id": "trace-unknown",
                "reply_delivery_started": True,
            },
            session_key="bot:private_1",
            timeout_seconds=180.0,
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
    )

    assert finished[-1]["outcome"] == "outcome_unknown"
    assert finished[-1]["diagnosis_code"] == "send_outcome_unknown"


def test_yaml_wrapper_preserves_required_reply_delivery_contract(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    async def _process(*_args, **kwargs) -> None:  # noqa: ANN002, ANN003
        captured.update(kwargs)

    monkeypatch.setattr(yaml_handler, "process_yaml_response_logic", _process)
    wrapper = yaml_processor.build_yaml_response_processor(
        get_current_time=lambda: None,
        format_time_context=lambda _now: "",
        bot_statuses={},
        get_group_config=lambda _group_id: {},
        plugin_config=SimpleNamespace(),
        get_schedule_prompt_injection=lambda: "",
        schedule_disabled_override_prompt=lambda: "",
        build_grounding_context=lambda _query: None,
        call_ai_api=lambda *_args, **_kwargs: None,
        parse_yaml_response=lambda _text: {},
        message_segment_cls=SimpleNamespace,
        sanitize_history_text=str,
        private_session_prefix="private_",
        build_private_session_id=lambda user_id: f"private_{user_id}",
        build_group_session_id=str,
        append_session_message=lambda *_args, **_kwargs: None,
        record_group_msg=None,
        logger=SimpleNamespace(),
        user_blacklist={},
    )
    semantic_frame = SimpleNamespace(chat_intent="chat")
    delivery_state: dict[str, object] = {}
    inner_state = {"mood": "calm"}
    emotion_state = {"tone": "warm"}

    asyncio.run(wrapper(
        SimpleNamespace(),
        SimpleNamespace(),
        "group-1",
        "user-1",
        "name",
        "friend",
        {},
        [],
        semantic_frame=semantic_frame,
        reply_commit_state=delivery_state,
        reply_required=True,
        response_deadline=123.5,
        prepared_inner_state=inner_state,
        prepared_emotion_state=emotion_state,
    ))

    assert captured["semantic_frame"] is semantic_frame
    assert captured["reply_commit_state"] is delivery_state
    assert captured["reply_required"] is True
    assert captured["response_deadline"] == 123.5
    assert captured["prepared_inner_state"] is inner_state
    assert captured["prepared_emotion_state"] is emotion_state
