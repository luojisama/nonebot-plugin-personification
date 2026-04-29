from __future__ import annotations

from ._loader import load_personification_module

visual_capabilities = load_personification_module("plugin.personification.core.visual_capabilities")


def test_heuristic_supports_vision_rejects_non_codex_models_on_codex_backend() -> None:
    assert visual_capabilities.heuristic_supports_vision("openai_codex", "gpt-5.4-mini") is False
    assert visual_capabilities.heuristic_supports_vision("openai_codex", "gpt-5.3-codex") is True


def test_probe_response_matches_expected_color_order() -> None:
    assert visual_capabilities._probe_response_matches_expected("红绿蓝黄") is True
    assert visual_capabilities._probe_response_matches_expected("左上红，右上绿，左下蓝，右下黄") is True
    assert visual_capabilities._probe_response_matches_expected("ok") is False
