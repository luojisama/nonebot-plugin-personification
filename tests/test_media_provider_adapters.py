from __future__ import annotations

from ._loader import load_personification_module


adapters = load_personification_module("plugin.personification.core.media_provider_adapters")


def test_explicit_media_protocol_overrides_model_name() -> None:
    adapter = adapters.resolve_media_provider_adapter(
        {"api_type": "openai", "model": "deepseek-chat", "media_protocol": "openai_mimo_v25"}
    )
    assert adapter.protocol == "openai_mimo_v25"
    assert adapter.supports_video is True
    assert adapter.source == "explicit"

    disabled = adapters.resolve_media_provider_adapter(
        {"api_type": "gemini", "model": "gemini-2.5-pro", "media_protocol": "none"}
    )
    assert disabled.supports_video is False
    assert disabled.source == "explicit"

    agy = adapters.resolve_media_provider_adapter(
        {
            "api_type": "antigravity_cli",
            "model": "custom-fullmodal",
            "media_protocol": "antigravity_native",
        }
    )
    assert agy.protocol == "antigravity_native"
    assert agy.supports_video is True
    assert agy.supports_audio is True


def test_antigravity_auto_remains_fail_closed_without_explicit_capability() -> None:
    adapter = adapters.resolve_media_provider_adapter(
        {"api_type": "antigravity_cli", "model": "gemini-looking-name", "media_protocol": "auto"}
    )
    assert adapter.supports_video is False
    assert adapter.source == "unsupported"


def test_auto_only_accepts_confirmed_official_video_contracts() -> None:
    assert adapters.resolve_media_provider_adapter(
        {"api_type": "gemini", "model": "gemini-2.5-pro", "api_url": ""}
    ).protocol == "gemini_native"
    assert adapters.resolve_media_provider_adapter(
        {"api_type": "openai", "model": "qwen3.5-omni-plus"}
    ).protocol == "openai_qwen_omni"
    assert adapters.resolve_media_provider_adapter(
        {"api_type": "openai", "model": "mimo-v2.5"}
    ).protocol == "openai_mimo_v25"

    for model in ("gpt-5.4", "claude-opus-4-7", "deepseek-chat", "unknown-video"):
        assert adapters.resolve_media_provider_adapter(
            {"api_type": "openai", "model": model}
        ).supports_video is False


def test_custom_gemini_gateway_requires_explicit_media_protocol() -> None:
    inferred = adapters.resolve_media_provider_adapter(
        {
            "api_type": "gemini",
            "api_url": "https://gateway.example/v1beta",
            "model": "gemini-2.5-pro",
            "media_protocol": "auto",
        }
    )
    assert inferred.supports_video is False

    explicit = adapters.resolve_media_provider_adapter(
        {
            "api_type": "gemini",
            "api_url": "https://gateway.example/v1beta",
            "model": "gemini-2.5-pro",
            "media_protocol": "gemini_native",
        }
    )
    assert explicit.supports_video is True
