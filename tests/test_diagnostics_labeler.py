from __future__ import annotations

import asyncio

from ._loader import load_personification_module

diagnostics = load_personification_module("plugin.personification.core.diagnostics")


class _Config:
    personification_labeler_api_type = "openai"
    personification_labeler_api_url = ""
    personification_labeler_api_key = ""
    personification_labeler_model = ""


def test_empty_labeler_config_reuses_primary_model_in_diagnostics() -> None:
    checks = asyncio.run(diagnostics._llm_subconfig_checks(_Config()))
    labeler = next(item for item in checks if item["key"] == "sub_labeler")

    assert labeler["status"] == "info"
    assert "复用主模型" in labeler["detail"]
