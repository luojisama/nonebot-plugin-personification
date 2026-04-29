from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

model_router = load_personification_module("plugin.personification.core.model_router")


def test_collect_available_models_uses_codex_model_cache() -> None:
    config = SimpleNamespace(
        personification_api_type="openai",
        personification_model="legacy-model",
        personification_lite_model="",
        personification_fallback_model="",
        personification_vision_fallback_model="",
        personification_labeler_model="",
        personification_model_overrides={},
        personification_codex_auth_path="",
    )
    original_loader = model_router._load_codex_cached_models
    model_router._load_codex_cached_models = lambda *_args, **_kwargs: [
        {"model": "gpt-5.5", "source": "Codex /model"}
    ]
    try:
        models = model_router.collect_available_models(
            config,
            get_configured_api_providers=lambda: [
                {"name": "codex_primary", "api_type": "openai_codex", "model": "gpt-5.4-mini"}
            ],
        )
    finally:
        model_router._load_codex_cached_models = original_loader

    assert {"model": "gpt-5.5", "source": "Codex /model"} in models
    assert all(item["source"] != "codex_primary" for item in models)
