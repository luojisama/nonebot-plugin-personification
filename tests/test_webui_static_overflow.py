from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "webui" / "static"


def _source(filename: str) -> str:
    return (STATIC / filename).read_text(encoding="utf-8")


def _admin_source() -> str:
    return "\n".join(
        _source(filename)
        for filename in (
            "app-admin-common.js",
            "app-dashboard.js",
            "app-health-qq.js",
            "app-qzone.js",
            "app-identity-policy.js",
            "app-persona-builder.js",
            "app-groups.js",
        )
    )


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    match = re.search(r"\n(?:async\s+)?function\s+\w+\(", source[start + 1 :])
    return source[start:] if match is None else source[start : start + 1 + match.start()]


def test_overflow_utilities_keep_atomic_and_wrappable_values_separate() -> None:
    css = _source("style.css")

    for selector in (
        ".u-atomic",
        ".u-ellipsis",
        ".u-wrap",
        ".u-pre-wrap",
        ".u-tabular",
        ".table-wrap",
        ".table-scroll",
        ".data-table.compact",
        ".data-table.wide",
        ".data-table.xwide",
        ".col-id",
        ".col-time",
        ".col-date",
        ".col-number",
        ".col-status",
        ".col-actions",
        ".col-model",
        ".col-summary",
        ".col-description",
        ".favorability-badge",
        ".btn--wrap",
    ):
        assert selector in css

    assert "word-break:break-all" not in css.replace(" ", "")
    assert re.search(r"td\s+code\s*\{[^}]*break-all", css, re.DOTALL) is None
    tag_rule = re.search(r"\.tag\s*\{([^}]+)\}", css)
    assert tag_rule is not None
    assert "white-space:nowrap" not in tag_rule.group(1).replace(" ", "")
    assert ".btn {" in css and "white-space:nowrap" in css
    assert ".btn--wrap { white-space:normal" in css


def test_every_static_table_uses_a_named_focusable_scroll_region() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in sorted(STATIC.glob("app-*.js")))
    table_count = len(re.findall(r"<table\b", sources))
    regions = re.findall(
        r'<div class="table-wrap table-scroll(?: [^"]*)?" tabindex="0" role="region" aria-label="[^"]+">\s*<table\b',
        sources,
    )

    assert table_count == 48
    assert len(regions) == table_count
    assert len(re.findall(r'<table\b[^>]*class="[^"]*\bdata-table\b', sources)) == table_count
    assert re.findall(r'<th\b(?![^>]*\bscope="(?:col|row)")', sources) == []


def test_priority_renderers_keep_field_semantics() -> None:
    admin = _admin_source()
    activity = _source("app-activity.js")
    content = _source("app-content.js")
    operations = _source("app-operations.js")
    tools = _source("app-tools.js")
    auth = _source("app-auth.js")

    for name in ("renderPersonas", "renderGroupSwitch", "renderGroups", "renderGroupDetail", "renderQQ"):
        block = _function(admin, name)
        assert "table-wrap table-scroll" in block, name
        assert 'role="region"' in block, name

    personas = _function(admin, "renderPersonas")
    assert "col-id" in personas
    assert "col-status" in personas
    assert "col-actions" in personas
    assert "u-atomic u-tabular" in personas

    badge = _function(admin, "renderFavorabilityBadge")
    assert "favorability-badge" in badge
    assert "u-tabular" in badge
    assert 'title="好感度 ' in badge
    assert "虚拟默认值，尚未创建档案" in badge
    assert "已关闭" in badge

    favorability = _function(admin, "renderFavorabilityCard")
    assert "col-time" in favorability
    assert "col-number" in favorability
    assert "col-status" in favorability
    assert "col-description" in favorability
    assert "浏览此页面不会创建好感度档案" in favorability

    for source, name in (
        (operations, "renderAgentStatusContent"),
        (activity, "renderTraceProcess"),
        (activity, "renderTraces"),
        (content, "renderMemory"),
        (content, "renderMemoryVectorPanel"),
        (tools, "renderSkills"),
        (tools, "renderPluginManager"),
        (tools, "renderTestAll"),
        (auth, "renderDevices"),
    ):
        block = _function(source, name)
        assert "table-wrap table-scroll" in block, name
        assert "data-table" in block, name


def test_long_technical_values_and_dynamic_buttons_keep_mobile_contracts() -> None:
    admin = _admin_source()
    activity = _source("app-activity.js")
    mcp = _source("app-mcp.js")
    creator = _source("app-tool-creator.js")
    css = _source("style.css")

    assert 'class="u-ellipsis" title="${escapeAttr(row.trace_id)}"' in _function(
        _source("app-operations.js"), "renderAgentStatusContent"
    )
    assert 'title="${escapeAttr(e.trace_id || "-")}"' in _function(activity, "renderTraces")
    assert 'title="${escapeAttr(id)}"' in _function(admin, "renderQzoneReconciliation")
    assert 'title="${escapeAttr(feedId)}"' in _function(admin, "renderQzoneHistoryReconciliation")
    assert "mcp-installation-id u-ellipsis" in mcp
    assert "mcp-command-plan" in mcp and "plan.tokens.map" in mcp
    assert ".mcp-command-plan>div" in css and "overflow-x:auto" in css
    command_rule = re.search(r"\.mcp-command-plan code\s*\{([^}]+)\}", css)
    assert command_rule is not None
    assert "white-space:pre" in command_rule.group(1)
    assert "text-overflow:ellipsis" not in command_rule.group(1)
    assert ".mcp-record-title h3,.mcp-record-title code" in css
    assert ".mcp-record-title h3,.mcp-record-title code { width:100%; white-space:normal" not in css
    assert ".mcp-tool-card header code { max-width:100%; white-space:normal" not in css
    assert 'class="btn btn--wrap small"' in creator
    assert ".sticker-edit-form { flex:1 1 280px; min-width:0" in css
    assert 'class="sticker-edit-form"' in _source("app-content.js")


def test_operation_summary_accessibility_contract_stays_intact() -> None:
    core = _source("app-core.js")
    css = _source("style.css")
    summary = re.search(r'<summary class="operation-summary">(.+?)</summary>', core, re.DOTALL)

    assert summary is not None
    markup = summary.group(1)
    tokens = (
        'class="operation-summary-mark"',
        'class="operation-summary-copy"',
        '<code class="operation-code">',
        'class="operation-chevron"',
    )
    assert [markup.index(token) for token in tokens] == sorted(markup.index(token) for token in tokens)
    operation_code = re.search(r"\.operation-code\s*\{([^}]+)\}", css)
    assert operation_code is not None
    for contract in ("white-space:nowrap", "word-break:normal", "text-overflow:ellipsis"):
        assert contract in operation_code.group(1)
    assert ".operation-summary:focus-visible" in css
    assert "min-height:54px" in css
    assert "min-height:68px" in css


def test_rapid_navigation_never_renders_a_stale_lazy_view() -> None:
    core = _source("app-core.js")
    load_view = _function(core, "loadView")
    navigate = _function(core, "navigateToView")

    assert "if(navigationId!==_navigationId)return false;" in load_view
    assert 'if (e && e.name === "AbortError") return false;' in load_view
    assert "return navigationId === _navigationId;" in load_view
    assert "const loaded=await loadView();" in navigate
    assert "if(loaded&&state.view===nextView){render();enterViewLifecycle(nextView);}" in navigate
    assert 'if((e&&e.name==="AbortError")||state.view!==nextView)return;' in navigate


def test_persona_prompt_loads_the_asset_that_defines_its_renderer() -> None:
    core = _source("app-core.js")

    assert 'persona_prompt:["app-tools.js"]' in core
    assert "function renderPersonaPrompt()" in _source("app-tools.js")


def test_successful_login_does_not_mislabel_a_view_render_failure() -> None:
    verify = _function(_source("app-auth.js"), "doVerify")

    assert verify.count("catch") >= 2
    assert 'msg.textContent = "验证失败：" + e.message' in verify
    assert 'text:`登录成功，但“${failedView}”页面加载失败：${e.message}`' in verify
    assert 'state.view = "dashboard"' in verify


def test_trace_budget_labels_describe_limits_instead_of_elapsed_usage() -> None:
    activity = _source("app-activity.js")

    assert activity.count('actual_steps: "运行步数上限"') == 2
    assert activity.count('actual_seconds: "运行预算秒数"') == 2
    assert 'actual_seconds: "实际秒数"' not in activity
