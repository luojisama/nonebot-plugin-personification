from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module

tts_service_mod = load_personification_module("plugin.personification.core.tts_service")


class _Logger:
    def debug(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    def warning(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


def _service(style_planner=None, **overrides):  # noqa: ANN001, ANN003
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


def test_tts_global_switch_disables_service_availability() -> None:
    service = _service(personification_tts_global_enabled=False)

    assert service.is_enabled() is False
    assert service.is_available() is False
