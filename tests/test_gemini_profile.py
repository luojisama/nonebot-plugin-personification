from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

gemini_profile = load_personification_module("plugin.personification.core.gemini_profile")


def test_gemini_policy_mentions_native_search_and_multimodal_context() -> None:
    prompt = gemini_profile.build_gemini_route_policy_prompt(
        api_type="gemini_cli",
        model="gemini-3.1-pro-preview",
        has_visual_context=True,
        has_video_context=True,
        native_search_enabled=True,
    )

    assert "Gemini 路由优化" in prompt
    assert "长上下文" in prompt
    assert "广知识面" in prompt
    assert "原生联网查询" in prompt
    assert "视频输入" in prompt


def test_gemini_policy_is_empty_for_non_gemini_route() -> None:
    assert (
        gemini_profile.build_gemini_route_policy_prompt(
            api_type="openai",
            model="gpt-5.4-mini",
        )
        == ""
    )


def test_gemini_route_gets_larger_context_budget() -> None:
    assert gemini_profile.context_token_budget_for_route("gemini", "gemini-2.5-pro") > 2000
    assert gemini_profile.context_token_budget_for_route("antigravity_cli", "gemini-3-flash-preview") > 2000
    assert gemini_profile.context_keep_recent_for_route("gemini", "gemini-2.5-pro") > 6
    assert gemini_profile.context_token_budget_for_route("openai", "gpt-5.4-mini") == 2000


def test_default_builtin_search_auto_enables_for_gemini_only() -> None:
    gemini_cfg = SimpleNamespace(
        personification_api_type="gemini",
        personification_model="gemini-2.5-pro",
        personification_builtin_search=True,
        personification_model_builtin_search_enabled=False,
    )
    openai_cfg = SimpleNamespace(
        personification_api_type="openai",
        personification_model="gpt-5.4-mini",
        personification_builtin_search=True,
        personification_model_builtin_search_enabled=False,
    )
    disabled_cfg = SimpleNamespace(
        personification_api_type="gemini",
        personification_model="gemini-2.5-pro",
        personification_builtin_search=False,
        personification_model_builtin_search_enabled=False,
    )

    assert gemini_profile.should_enable_default_builtin_search(gemini_cfg) is True
    assert gemini_profile.should_enable_default_builtin_search(openai_cfg) is False
    assert gemini_profile.should_enable_default_builtin_search(disabled_cfg) is False


def test_default_builtin_search_respects_provider_native_search_flag() -> None:
    cfg = SimpleNamespace(
        personification_api_type="openai",
        personification_model="gpt-5.4-mini",
        personification_builtin_search=True,
        personification_model_builtin_search_enabled=False,
    )

    assert (
        gemini_profile.should_enable_default_builtin_search(
            cfg,
            get_configured_api_providers=lambda: [
                {
                    "api_type": "gemini",
                    "model": "gemini-2.5-pro",
                    "supports_native_search": False,
                }
            ],
        )
        is False
    )
