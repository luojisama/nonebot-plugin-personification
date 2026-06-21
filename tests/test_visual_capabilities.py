from __future__ import annotations

from ._loader import load_personification_module

visual_capabilities = load_personification_module("plugin.personification.core.visual_capabilities")


def test_heuristic_supports_vision_rejects_non_codex_models_on_codex_backend() -> None:
    assert visual_capabilities.heuristic_supports_vision("openai_codex", "gpt-5.4-mini") is False
    assert visual_capabilities.heuristic_supports_vision("openai_codex", "gpt-5.3-codex") is True


def test_heuristic_supports_vision_rejects_third_party_openai_gateway_with_text_only_model() -> None:
    """回归测试：api_type=openai 但 model 是 deepseek/qwen 等不支持视觉的模型，
    必须返回 False。否则路由会把 image_url 多模态消息直接发给上游导致 400
    'unknown variant image_url, expected text'。"""
    assert visual_capabilities.heuristic_supports_vision("openai", "deepseek-v4-flash") is False
    assert visual_capabilities.heuristic_supports_vision("openai", "qwen-turbo") is False
    assert visual_capabilities.heuristic_supports_vision("openai", "kimi-k2") is False
    assert visual_capabilities.heuristic_supports_vision("openai", "glm-4-flash") is False
    # 反之，已知支持视觉的官方模型即使协议是 openai 也要识别
    assert visual_capabilities.heuristic_supports_vision("openai", "gpt-4o-mini") is True
    assert visual_capabilities.heuristic_supports_vision("openai", "gpt-5-pro") is True
    # 显式 vision 关键字
    assert visual_capabilities.heuristic_supports_vision("openai", "qwen2.5-vl-vision-7b") is True


def test_heuristic_supports_vision_native_apis_keep_returning_true() -> None:
    assert visual_capabilities.heuristic_supports_vision("anthropic", "claude-3-haiku") is True
    assert visual_capabilities.heuristic_supports_vision("gemini", "gemini-2.0-flash") is True
    assert visual_capabilities.heuristic_supports_vision("gemini_cli", "gemini-3-flash-preview") is True
    assert visual_capabilities.heuristic_supports_vision("antigravity_cli", "gemini-3-flash-preview") is True
    assert visual_capabilities.heuristic_supports_vision("claude_code", "claude-opus-4-7") is True


def test_probe_response_matches_expected_color_order() -> None:
    assert visual_capabilities._probe_response_matches_expected("红绿蓝黄") is True
    assert visual_capabilities._probe_response_matches_expected("左上红，右上绿，左下蓝，右下黄") is True
    assert visual_capabilities._probe_response_matches_expected("ok") is False


def test_mimo_v25_models_are_treated_as_multimodal_except_audio_embedding_variants() -> None:
    assert visual_capabilities.heuristic_supports_vision("openai", "mimo-v2.5") is True
    assert visual_capabilities.heuristic_supports_vision("openai", "mimo2.5") is True
    assert visual_capabilities.heuristic_supports_vision("openai", "mimo-v2-omni") is True
    assert visual_capabilities.heuristic_supports_vision("openai", "mimo-v2.5-pro") is True
    assert visual_capabilities.heuristic_supports_vision("anthropic", "mimo-v2.5-pro") is True
    assert visual_capabilities.heuristic_supports_vision("openai", "mimo-v2.5-tts") is False
    assert visual_capabilities.heuristic_supports_vision("openai", "mimo-v2.5-embedding") is False


def test_visual_route_probe_cache_overrides_model_heuristic() -> None:
    visual_capabilities.set_visual_capability(
        "test_route",
        "openai",
        "mimo-v2.5",
        False,
        source="test",
        detail="forced by test",
    )
    assert visual_capabilities.provider_supports_vision(
        "openai",
        "mimo-v2.5",
        route_name="test_route",
    ) is False
    assert visual_capabilities.provider_supports_vision(
        "openai",
        "mimo-v2.5",
        route_name="other_route",
    ) is True
