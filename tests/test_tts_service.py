from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module

tts_service_mod = load_personification_module("plugin.personification.core.tts_service")
send_outcome = load_personification_module("plugin.personification.core.send_outcome")
db = load_personification_module("plugin.personification.core.db")
qq_outbound = load_personification_module("plugin.personification.core.qq_outbound")


class _Logger:
    def debug(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    def warning(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


def _service(style_planner=None, *, qq_outbound_ledger=None, **overrides):  # noqa: ANN001, ANN003
    config = SimpleNamespace(
        personification_tts_enabled=True,
        personification_tts_global_enabled=True,
        personification_tts_auto_enabled=True,
        personification_tts_llm_decision_enabled=True,
        personification_tts_decision_timeout=8,
        personification_tts_builtin_safety_enabled=True,
        personification_tts_forbidden_policy="",
        personification_tts_api_key="key",
        personification_tts_api_url="https://api.xiaomimimo.com/v1",
        personification_tts_model="mimo-v2.5-tts",
        personification_tts_mode="preset",
        personification_tts_default_voice="mimo_default",
        personification_tts_voice_design_prompt="",
        personification_tts_voice_clone="",
        personification_tts_voice_clone_path="",
        personification_tts_default_format="wav",
        personification_tts_timeout=60,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return tts_service_mod.TtsService(
        plugin_config=config,
        logger=_Logger(),
        get_http_client=lambda: None,
        data_dir=Path(__file__).resolve().parent / ".tmp",
        style_planner=style_planner,
        qq_outbound_ledger=qq_outbound_ledger,
    )


def test_tts_preset_payload_uses_builtin_voice() -> None:
    service = _service()
    decision = service.infer_style_decision("你好", voice_hint="冰糖", style_hint="开心")

    payload = service._build_payload(
        decision.text,
        mode=decision.mode,
        model=decision.model,
        voice=decision.voice,
        style=decision.style,
        user_hint="轻快一点",
        voice_prompt=decision.voice_prompt,
        voice_clone=decision.voice_clone,
    )

    assert payload["model"] == "mimo-v2.5-tts"
    assert payload["audio"] == {"format": "wav", "voice": "冰糖"}
    assert payload["messages"] == [
        {"role": "user", "content": "轻快一点"},
        {"role": "assistant", "content": "(开心)你好"},
    ]


def test_tts_voice_design_payload_uses_user_voice_prompt_without_voice_field() -> None:
    service = _service(personification_tts_mode="design")
    decision = service.infer_style_decision(
        "今天也辛苦啦",
        voice_prompt_hint="明亮活泼的少女声，语速稍快，咬字轻巧。",
    )

    payload = service._build_payload(
        decision.text,
        mode=decision.mode,
        model=decision.model,
        voice=decision.voice,
        style=decision.style,
        user_hint="自然一点",
        voice_prompt=decision.voice_prompt,
        voice_clone=decision.voice_clone,
    )

    assert payload["model"] == "mimo-v2.5-tts-voicedesign"
    assert payload["audio"] == {"format": "wav"}
    assert payload["messages"][0] == {
        "role": "user",
        "content": "明亮活泼的少女声，语速稍快，咬字轻巧。\n朗读要求：自然一点",
    }
    assert payload["messages"][1] == {"role": "assistant", "content": "今天也辛苦啦"}


def test_tts_voice_clone_payload_uses_audio_data_url_as_voice() -> None:
    clone_voice = "data:audio/wav;base64,QUJD"
    service = _service(personification_tts_mode="clone")
    decision = service.infer_style_decision("测试克隆音色", voice_clone_hint=clone_voice)

    payload = service._build_payload(
        decision.text,
        mode=decision.mode,
        model=decision.model,
        voice=decision.voice,
        style=decision.style,
        user_hint=None,
        voice_prompt=decision.voice_prompt,
        voice_clone=decision.voice_clone,
    )

    assert payload["model"] == "mimo-v2.5-tts-voiceclone"
    assert payload["audio"] == {"format": "wav", "voice": clone_voice}
    assert payload["messages"] == [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "测试克隆音色"},
    ]


def test_tts_delivery_decision_accepts_voice_action_and_style_hint() -> None:
    async def _planner(messages):  # noqa: ANN001
        assert "自定义违禁策略" in messages[0]["content"]
        payload = json.loads(messages[1]["content"])
        assert payload["final_text"] == "晚点再说吧"
        return '{"action":"voice","style_hint":"小声一点","visible_message":"","reason":"适合语音"}'

    service = _service(style_planner=_planner, personification_tts_forbidden_policy="不要朗读测试禁区内容")

    decision = asyncio.run(
        service.decide_tts_delivery(
            text="晚点再说吧",
            is_private=False,
            group_config={"tts_enabled": True},
            raw_message_text="你回我一下",
            fallback_style_hint="自然",
        )
    )

    assert decision.action == "voice"
    assert decision.style_hint == "小声一点"
    assert decision.reason == "适合语音"


def test_tts_delivery_decision_blocks_command_with_visible_message() -> None:
    async def _planner(_messages):  # noqa: ANN001
        return {
            "action": "block",
            "style_hint": "",
            "visible_message": "这段不适合读出来。",
            "reason": "unsafe",
        }

    service = _service(style_planner=_planner)

    decision = asyncio.run(
        service.decide_tts_delivery(
            text="测试文本",
            is_private=True,
            command_triggered=True,
        )
    )

    assert decision.action == "block"
    assert decision.visible_message == "这段不适合读出来。"


def test_tts_delivery_decision_falls_back_to_text_when_llm_fails() -> None:
    async def _planner(_messages):  # noqa: ANN001
        raise RuntimeError("boom")

    service = _service(style_planner=_planner)

    decision = asyncio.run(
        service.decide_tts_delivery(
            text="正常回复",
            is_private=False,
            group_config={"tts_enabled": True},
            fallback_style_hint="自然",
        )
    )

    assert decision.action == "text"
    assert decision.style_hint == "自然"
    assert "decision_failed" in decision.reason


def test_tts_delivery_decision_disabled_does_not_call_planner_for_command() -> None:
    called = False

    async def _planner(_messages):  # noqa: ANN001
        nonlocal called
        called = True
        return '{"action":"block","reason":"should_not_run"}'

    service = _service(
        style_planner=_planner,
        personification_tts_llm_decision_enabled=False,
    )

    decision = asyncio.run(
        service.decide_tts_delivery(
            text="正常朗读",
            is_private=True,
            command_triggered=True,
            fallback_style_hint="自然",
        )
    )

    assert called is False
    assert decision.action == "voice"
    assert decision.style_hint == "自然"
    assert decision.reason == "llm_decision_disabled"


def test_tts_delivery_receipts_wrap_real_send_only(monkeypatch) -> None:  # noqa: ANN001
    service = _service()
    events: list[str] = []

    async def _synthesize(*_args, **_kwargs):  # noqa: ANN001
        events.append("synthesized")
        return [Path("first.wav"), Path("second.wav")]

    class _Bot:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _event, _message):  # noqa: ANN001
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("second failed")

    monkeypatch.setattr(service, "synthesize", _synthesize)

    with pytest.raises(RuntimeError, match="second failed"):
        asyncio.run(
            service.send_tts(
                bot=_Bot(),
                event=object(),
                message_segment_cls=SimpleNamespace(record=lambda value: value),
                text="两段语音",
                pause_range=(0, 0),
                on_delivery_started=lambda: events.append("started"),
                on_delivery_confirmed=lambda: events.append("confirmed"),
            )
        )

    assert events == ["synthesized", "started", "confirmed", "started"]


def test_tts_fake_ledger_dispatches_each_record_segment(monkeypatch) -> None:  # noqa: ANN001
    class _Ledger:
        def __init__(self) -> None:
            self.calls: list[tuple[object, str, str]] = []

        async def dispatch(self, context, content, send):  # noqa: ANN001, ANN202
            result = await send()
            self.calls.append((context, str(content), result["message_id"]))
            return SimpleNamespace(status="sent", message_id=result["message_id"])

    class _Bot:
        self_id = "bot-tts"

        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _event, _message):  # noqa: ANN001
            self.calls += 1
            return {"message_id": f"tts-message-{self.calls}"}

    ledger = _Ledger()
    service = _service(qq_outbound_ledger=ledger)

    async def _synthesize(*_args, **_kwargs):  # noqa: ANN001
        return [Path("first.wav"), Path("second.wav")]

    monkeypatch.setattr(service, "synthesize", _synthesize)
    events: list[str] = []
    delivered = asyncio.run(
        service.send_tts(
            bot=_Bot(),
            event=SimpleNamespace(group_id=20001, user_id=10001),
            message_segment_cls=SimpleNamespace(record=lambda value: f"record:{value}"),
            text="两段语音",
            pause_range=(0, 0),
            operation_id="tts-operation",
            user_target="10002",
            on_delivery_started=lambda: events.append("started"),
            on_delivery_confirmed=lambda: events.append("confirmed"),
        )
    )

    assert delivered is True
    assert [call[2] for call in ledger.calls] == ["tts-message-1", "tts-message-2"]
    assert [call[0].operation_id for call in ledger.calls] == ["tts-operation", "tts-operation"]
    assert [call[0].surface for call in ledger.calls] == ["reply_tts", "reply_tts"]
    assert [call[0].user_target for call in ledger.calls] == ["10002", "10002"]
    assert events == ["started", "confirmed", "started", "confirmed"]


def test_tts_real_ledger_records_message_ids_per_segment(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    service = _service(qq_outbound_ledger=qq_outbound.QQOutboundLedger(db_path))

    async def _synthesize(*_args, **_kwargs):  # noqa: ANN001
        return [Path("first.wav"), Path("second.wav")]

    class _Bot:
        self_id = "bot-tts"

        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _event, _message):  # noqa: ANN001
            self.calls += 1
            return {"data": {"message_id": f"real-tts-{self.calls}"}}

    monkeypatch.setattr(service, "synthesize", _synthesize)
    delivered = asyncio.run(
        service.send_tts(
            bot=_Bot(),
            event=SimpleNamespace(group_id=20001, user_id=10001),
            message_segment_cls=SimpleNamespace(record=lambda value: f"record:{value}"),
            text="两段语音",
            pause_range=(0, 0),
            operation_id="real-tts-operation",
            surface="reply_tts_test",
        )
    )

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT part_index, message_id, status, surface
            FROM qq_outbound_ledger
            WHERE operation_id=?
            ORDER BY part_index
            """,
            ("real-tts-operation",),
        ).fetchall()
    assert delivered is True
    assert rows == [
        (0, "real-tts-1", "sent", "reply_tts_test"),
        (1, "real-tts-2", "sent", "reply_tts_test"),
    ]


def test_tts_missing_message_id_reports_unknown_without_confirmation(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    service = _service(qq_outbound_ledger=qq_outbound.QQOutboundLedger(db_path))

    async def _synthesize(*_args, **_kwargs):  # noqa: ANN001
        return [Path("voice-1.wav"), Path("voice-2.wav")]

    class _Bot:
        self_id = "bot-tts"

        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _event, _message):  # noqa: ANN001
            self.calls += 1
            return {"status": "ok"}

    monkeypatch.setattr(service, "synthesize", _synthesize)
    events: list[str] = []
    bot = _Bot()
    delivered = asyncio.run(
        service.send_tts(
            bot=bot,
            event=SimpleNamespace(user_id=10001),
            message_segment_cls=SimpleNamespace(record=lambda value: f"record:{value}"),
            text="一段语音",
            operation_id="tts-missing-message-id",
            on_delivery_started=lambda: events.append("started"),
            on_delivery_confirmed=lambda: events.append("confirmed"),
            on_delivery_unknown=lambda: events.append("unknown"),
        )
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, message_id, error_code FROM qq_outbound_ledger WHERE operation_id=?",
            ("tts-missing-message-id",),
        ).fetchone()
    assert delivered is True
    assert events == ["started", "unknown"]
    assert bot.calls == 1
    assert row == ("unknown", None, "message_id_missing")


def test_tts_ledger_send_exception_stays_unknown(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    service = _service(qq_outbound_ledger=qq_outbound.QQOutboundLedger(db_path))

    async def _synthesize(*_args, **_kwargs):  # noqa: ANN001
        return [Path("voice.wav")]

    class _Bot:
        self_id = "bot-tts"

        async def send(self, _event, _message):  # noqa: ANN001
            raise RuntimeError("send failed")

    monkeypatch.setattr(service, "synthesize", _synthesize)
    events: list[str] = []
    with pytest.raises(RuntimeError, match="send failed"):
        asyncio.run(
            service.send_tts(
                bot=_Bot(),
                event=SimpleNamespace(user_id=10001),
                message_segment_cls=SimpleNamespace(record=lambda value: f"record:{value}"),
                text="一段语音",
                operation_id="tts-send-error",
                on_delivery_started=lambda: events.append("started"),
                on_delivery_confirmed=lambda: events.append("confirmed"),
            )
        )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, message_id, error_code FROM qq_outbound_ledger WHERE operation_id=?",
            ("tts-send-error",),
        ).fetchone()
    assert events == ["started"]
    assert row == ("unknown", None, "RuntimeError")


def test_likely_delivered_send_timeout_is_outcome_unknown() -> None:
    exc = RuntimeError("send failed")
    exc.info = {"retcode": 1200, "wording": "invoke timeout"}  # type: ignore[attr-defined]

    assert send_outcome.is_likely_delivered_send_timeout(exc)
    assert not send_outcome.is_likely_delivered_send_timeout(RuntimeError("connection refused"))


def test_tts_global_switch_disables_service_availability() -> None:
    service = _service(personification_tts_global_enabled=False)

    assert service.is_enabled() is False
    assert service.is_available() is False
