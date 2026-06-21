from __future__ import annotations

from pathlib import Path


def test_config_search_is_ime_aware_and_searches_aliases() -> None:
    app_core_js = (
        Path(__file__).resolve().parents[1] / "nonebot_plugin_personification" / "webui" / "static" / "app-core.js"
    ).read_text(encoding="utf-8")
    app_config_js = (
        Path(__file__).resolve().parents[1] / "nonebot_plugin_personification" / "webui" / "static" / "app-config.js"
    ).read_text(encoding="utf-8")

    assert "oncompositionstart=\"onConfigSearchCompositionStart(this)\"" in app_config_js
    assert "oncompositionend=\"onConfigSearchCompositionEnd(this)\"" in app_config_js
    assert "oninput=\"onConfigSearchInput(this,event)\"" in app_config_js
    assert "event.isComposing" in app_core_js
    assert "state.configSearchComposing" in app_core_js
    assert "entry.aliases" in app_config_js
    assert "configSearchHaystack" in app_config_js
