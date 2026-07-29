from __future__ import annotations

from pathlib import Path


def test_config_search_is_ime_aware_and_searches_aliases() -> None:
    app_core_js = (
        (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification") / "webui" / "static" / "app-core.js"
    ).read_text(encoding="utf-8")
    app_config_js = (
        (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification") / "webui" / "static" / "app-config.js"
    ).read_text(encoding="utf-8")

    assert "oncompositionstart=\"onConfigSearchCompositionStart(this)\"" in app_config_js
    assert "oncompositionend=\"onConfigSearchCompositionEnd(this)\"" in app_config_js
    assert "oninput=\"onConfigSearchInput(this,event)\"" in app_config_js
    assert "event.isComposing" in app_core_js
    assert "state.configSearchComposing" in app_core_js
    assert "entry.aliases" in app_config_js
    assert "entry.search_index" in app_config_js
    assert "configSearchHaystack" in app_config_js
    assert "configSearchEntryScore" in app_config_js
    assert "configEditDistanceWithin" in app_config_js


def test_config_api_pool_model_probe_dropdown_is_present() -> None:
    app_config_js = (
        (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification") / "webui" / "static" / "app-config.js"
    ).read_text(encoding="utf-8")
    assert "probeApiProviderModels" in app_config_js
    assert 'api("/config/provider-models"' in app_config_js
    assert "<datalist" in app_config_js
    assert "data-provider-model-select" in app_config_js
    assert "selectApiProviderModel" in app_config_js
    assert "syncApiProviderModelSelect" in app_config_js
    assert "normalizeApiProviderModels" in app_config_js
    assert "updateApiProviderModelControls" in app_config_js
    assert "apiProviderModelProbeCache" in app_config_js
    assert "hydrateApiProviderModelProbe" in app_config_js
    assert "cacheApiProviderModelProbe(field, index, probedProvider)" in app_config_js
    assert "item.id || item.model || item.name || item.slug" in app_config_js
    assert "探测模型" in app_config_js
    assert "sanitizeApiProvider" in app_config_js
    assert "delete out._model_options" in app_config_js
    assert "delete out._model_probe_done" in app_config_js
    assert "_model_probe_done: true" in app_config_js
    assert "先探测模型" in app_config_js
    assert "未探测到可选模型" in app_config_js
    assert "options.length || probeDone" not in app_config_js
    assert "const models = normalizeApiProviderModels(result.models)" in app_config_js


def test_config_api_pool_exposes_timeout_and_total_attempts() -> None:
    app_config_js = (
        (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification") / "webui" / "static" / "app-config.js"
    ).read_text(encoding="utf-8")

    assert "timeout: 200" in app_config_js
    assert "max_retries: 5" in app_config_js
    assert 'fieldHtml("timeout", "单次超时（秒）"' in app_config_js
    assert 'fieldHtml("max_retries", "总尝试次数"' in app_config_js
    assert 'name === "max_retries"' in app_config_js


def test_config_frontend_persists_and_renders_operation_diagnostics() -> None:
    app_config_js = (
        (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification") / "webui" / "static" / "app-config.js"
    ).read_text(encoding="utf-8")

    assert "function configRememberDiagnostic" in app_config_js
    assert "state.configDiagnostics = [operation" in app_config_js
    assert "renderOperationHistory(" in app_config_js
    assert "配置操作诊断" in app_config_js
    assert "configRememberDiagnostic(result" in app_config_js
    assert "configRememberDiagnostic(e" in app_config_js
    assert '"模型探测失败：" + e.message' not in app_config_js
    assert '"保存失败：" + e.message' not in app_config_js


def test_config_fields_render_from_memory_only_drafts() -> None:
    root = (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification")
    app_core_js = (root / "webui" / "static" / "app-core.js").read_text(encoding="utf-8")
    app_config_js = (root / "webui" / "static" / "app-config.js").read_text(encoding="utf-8")

    assert "configDrafts: {}" in app_core_js
    assert "function configDraftValue" in app_config_js
    assert "const cur = configDraftValue(e)" in app_config_js
    assert "strListValue(configDraftValue(e))" in app_config_js
    assert "updateConfigDraft(" in app_config_js
    assert "syncStrListDraft(" in app_config_js
    assert "if (result.success)" in app_config_js
    success_block = app_config_js.split("if (result.success)", 1)[1].split("else {", 1)[0]
    assert "clearConfigDraft(field)" in success_block
    assert "sessionStorage" not in app_config_js
    assert "localStorage" not in app_config_js


def test_api_pool_mutations_all_update_the_draft() -> None:
    app_config_js = (
        (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification") / "webui" / "static" / "app-config.js"
    ).read_text(encoding="utf-8")

    for helper in (
        "setApiPoolDraft",
        "syncApiPoolDraft",
        "syncApiPoolRawDraft",
        "refreshApiPoolEditor",
        "addApiProvider",
        "removeApiProvider",
        "toggleApiPoolRaw",
        "selectApiProviderModel",
        "syncApiProviderModelSelect",
        "probeApiProviderModels",
    ):
        assert f"function {helper}" in app_config_js or f"async function {helper}" in app_config_js
    assert 'oninput="syncApiPoolDraft(' in app_config_js
    assert 'onchange="syncApiPoolDraft(' in app_config_js
    assert 'oninput="syncApiPoolRawDraft(this)"' in app_config_js
    assert "const draft = apiPoolDraftState(e.field_name)" in app_config_js
    assert "await saveField(field, sanitizeApiProviders(providers), {preserveDraft:true})" in app_config_js
