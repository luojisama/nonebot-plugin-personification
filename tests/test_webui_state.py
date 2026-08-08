from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    render_restore = core.index("root.innerHTML = renderLayout();")
    restore_call = core.index("restoreScrollState();", render_restore)
    render_attach = core.index("attachLayout();", restore_call)
    assert render_restore < restore_call < render_attach


def test_console_uses_stable_shell_and_releases_heavy_view_state() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    auth = (STATIC / "app-auth.js").read_text(encoding="utf-8")
    operations = (STATIC / "app-operations.js").read_text(encoding="utf-8")

    assert core.count("root.innerHTML = renderLayout();") == 1
    assert 'root.querySelector("#view-content")' in core
    assert 'const content=document.getElementById("view-content")' in core
    assert "content.innerHTML=renderView()" in core
    assert "function renderShellChrome()" in core
    assert 'id="shell-alert"' in core
    assert 'id="shell-loading"' in core
    assert 'id="view-content"' in core
    assert "function leaveViewLifecycle" in core
    for cleanup in (
        "state.groupRawChat=null",
        "state.traceDetail=null",
        "state.mcpPreview=null",
        "state.interactionResult=null",
        "state.memoryGraph=null",
    ):
        assert cleanup in core
    assert "destroyMemoryGraphCanvas" in core
    assert "let _layoutDelegationAttached = false" in auth
    assert 'document.addEventListener("click"' in auth
    assert "scrollListenerBound" in auth
    assert "setInterval(" not in operations
    assert "scheduleAgentStatusPoll" in operations
    assert "document.hidden" in operations
    assert "AbortController" in operations
    assert 'document.getElementById("agent-status-island")' in operations


def test_shell_scrim_does_not_take_a_desktop_grid_column() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    render_layout = core[core.index("function renderLayout() {") : core.index("function renderView() {")]

    layout_start = render_layout.index('<div class="layout">')
    scrim_start = render_layout.index('id="shell-scrim"')
    sidebar_start = render_layout.index("${renderSidebar()}")
    main_start = render_layout.index("<main ")

    assert scrim_start < layout_start < sidebar_start < main_start
    assert render_layout.count('id="shell-scrim"') == 1


def test_admin_pages_are_split_and_browser_metrics_stay_local_and_bounded() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    operations = (STATIC / "app-operations.js").read_text(encoding="utf-8")
    bundles = (
        "app-admin-common.js",
        "app-dashboard.js",
        "app-health-qq.js",
        "app-qzone.js",
        "app-identity-policy.js",
        "app-persona-builder.js",
        "app-groups.js",
    )

    legacy = (STATIC / "app-admin.js").read_text(encoding="utf-8")
    assert (STATIC / "app-admin.js").stat().st_size < 10_000
    assert "bootstrapLegacyAdminBundle" in legacy
    assert "__personificationLegacyAdminReady" in legacy
    assert "正在兼容当前已打开的旧标签页" in legacy
    for filename in bundles:
        assert filename in legacy
    assert '"app-admin.js"' not in core
    assert (STATIC / "app-admin-common.js").stat().st_size < 10_000
    assert all((STATIC / filename).stat().st_size < 50_000 for filename in bundles[1:])
    for filename in bundles:
        assert filename in core
    assert "function ensureAsset(filename)" in core
    assert "_BROWSER_METRIC_SERIES_MAX=32" in core
    assert "_BROWSER_METRIC_SAMPLES_MAX=64" in core
    assert '.split("?",1)[0]' in core
    assert 'observe("longtask"' in core
    assert 'observe("layout-shift"' in core
    assert 'observe("largest-contentful-paint"' in core
    assert 'observe("event"' in core
    assert "browserPerformanceSnapshot" in operations
    assert "仅保存在本标签页" in operations


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
    assert 'if (res.status === 401) {\n        clearInMemorySensitiveState();' in core
    assert auth.count("clearInMemorySensitiveState();") >= 4
    assert "function clearMcpSensitiveState()" in mcp
    assert "_mcpPendingInstall = null;" in mcp
    assert "const submittedDraft = configDraft(field);" in config
    assert "if (configDraft(field) === submittedDraft) clearConfigDraft(field);" in config
    assert "requestIdentity" in config
    assert "Provider 参数已变化，未覆盖当前草稿" in config
