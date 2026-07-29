from __future__ import annotations

import re
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "nonebot_plugin_personification")
STATIC = ROOT / "webui" / "static"


def test_all_details_use_process_namespaced_explicit_ui_state() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    app = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "_WEBUI_INSTANCE_ID = secrets.token_urlsafe" in app
    assert "__PERSONIFICATION_WEBUI_INSTANCE_ID__" in app
    assert "PERSONIFICATION_WEBUI_INSTANCE_ID=__PERSONIFICATION_WEBUI_INSTANCE_ID__" in index
    assert 'const _DETAIL_STORAGE_PREFIX = "personification_webui_details_v1:"' in core
    assert "`${_DETAIL_STORAGE_PREFIX}${_WEBUI_INSTANCE_ID}`" in core
    assert "function prepareDetailState" in core
    assert 'querySelectorAll("details")' in core
    assert "details.dataset.detailKey = detailIdentity(details, ordinal)" in core
    assert "Object.prototype.hasOwnProperty.call(_detailOpenState, key)" in core
    assert "_detailOpenState[current.dataset.detailKey] = Boolean(current.open)" in core
    assert 'data-detail-key="${escapeAttr(detailKey)}"' in core
    assert "MutationObserver" in core
    assert "sessionStorage.clear" not in core
    assert "sessionStorage.removeItem(QZONE_OPERATION_STORAGE_KEY)" not in core


def test_sidebar_scroll_capture_waits_for_synchronous_restore() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")

    assert "const _restoredScrollNodes = new WeakSet()" in core
    assert "main && _restoredScrollNodes.has(main)" in core
    assert "nav && _restoredScrollNodes.has(nav)" in core
    assert "main.scrollTop = mainScrollTop;\n    _restoredScrollNodes.add(main);" in core
    assert "nav.scrollTop = sidebarScrollTop;\n    _restoredScrollNodes.add(nav);" in core
    assert "if (main && main.isConnected" in core
    assert "if (nav && nav.isConnected) nav.scrollTop = sidebarScrollTop" in core
    render_restore = core.index("root.innerHTML = renderLayout();\n  restoreScrollState();")
    render_attach = core.index("\n  attachLayout();", render_restore)
    assert render_restore < render_attach


def test_common_svg_icons_keep_a_fixed_baseline_without_active_translation() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert '"settings": \'<path d="M12.22 2h-.44' in core
    icon_rule = re.search(r"\.ui-icon,\.nav-icon,\.operation-status-icon\s*\{([^}]+)\}", css)
    assert icon_rule is not None
    assert "display:block" in icon_rule.group(1)
    assert "aspect-ratio:1/1" in icon_rule.group(1)
    active_rule = re.search(r"aside nav a:hover \.nav-icon,aside nav a\.active \.nav-icon\s*\{([^}]+)\}", css)
    assert active_rule is not None
    assert "translateX" not in active_rule.group(1)


def test_sensitive_memory_and_newer_config_drafts_survive_only_their_owner_session() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    auth = (STATIC / "app-auth.js").read_text(encoding="utf-8")
    config = (STATIC / "app-config.js").read_text(encoding="utf-8")
    mcp = (STATIC / "app-mcp.js").read_text(encoding="utf-8")

    assert "function clearInMemorySensitiveState()" in core
    assert "state.configDrafts = {};" in core
    assert 'if (res.status === 401) {\n      clearInMemorySensitiveState();' in core
    assert auth.count("clearInMemorySensitiveState();") >= 4
    assert "function clearMcpSensitiveState()" in mcp
    assert "_mcpPendingInstall = null;" in mcp
    assert "const submittedDraft = configDraft(field);" in config
    assert "if (configDraft(field) === submittedDraft) clearConfigDraft(field);" in config
    assert "requestIdentity" in config
    assert "Provider 参数已变化，未覆盖当前草稿" in config
