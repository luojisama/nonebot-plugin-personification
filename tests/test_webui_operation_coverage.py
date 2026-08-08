from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "webui" / "routes"
STATIC = ROOT / "webui" / "static"


def test_webui_routes_never_return_raw_exception_text() -> None:
    patterns = (
        re.compile(r"detail\s*=\s*str\((?:exc|e)\)"),
        re.compile(r"detail\s*=\s*f[\"'][^\n]*\{(?:exc|e)\}"),
        re.compile(r"[\"']error[\"']\s*:\s*str\((?:exc|e)\)"),
    )
    findings: list[str] = []
    for path in ROUTES.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                findings.append(f"{path.name}:{line}: {match.group(0)}")
    assert findings == []


def test_management_surfaces_keep_stable_operation_codes() -> None:
    expected = {
        "qzone_routes.py": (
            "qzone_post_published", "qzone_publish_outcome_unknown", "qzone_login_started",
            "qzone_cookie_installed", "scan_completed",
        ),
        "data_transfer_routes.py": ("data_export_created", "data_import_plan_ready", "data_import_applied"),
        "plugin_manager_routes.py": ("plugin_update_available", "plugin_update_applied", "plugin_update_pull_outcome_unknown"),
        "qq_routes.py": ("qq_nickname_updated", "qq_group_left", "qq_operation_timeout"),
        "config_routes.py": ("config_value_updated", "provider_model_probe_complete"),
        "group_routes.py": ("group_schedule_saved", "group_style_rebuilt", "group_knowledge_rebuilt", "group_meme_saved"),
        "persona_template_routes.py": ("persona_template_build_complete", "persona_template_applied", "persona_profile_assets_partial"),
        "skill_routes.py": ("skill_runtime_reloaded", "remote_skill_source_added"),
        "sticker_routes.py": ("sticker_uploaded", "sticker_delete_manifest_partial"),
        "memory_routes.py": ("memory_vector_rebuilt", "memory_recall_test_completed"),
        "health_routes.py": ("health_interaction_replied", "qzone_forward_outcome_unknown"),
        "test_routes.py": ("provider_test_complete", "provider_test_all_partial", "persona_prompt_file_loaded"),
        "auth_routes.py": ("device_approved", "device_revoked"),
        "log_routes.py": ("plugin_logs_cleared",),
        "persona_routes.py": ("persona_correction_saved",),
        "mcp_routes.py": ("mcp_installed", "mcp_server_toggled", "mcp_tool_toggled", "mcp_reload_complete", "mcp_reload_partial"),
    }
    missing: list[str] = []
    for filename, codes in expected.items():
        source = (ROUTES / filename).read_text(encoding="utf-8")
        missing.extend(f"{filename}: {code}" for code in codes if code not in source)
    assert missing == []


def test_management_frontend_does_not_collapse_known_failures_to_message_only() -> None:
    sources = "\n".join(
        (STATIC / filename).read_text(encoding="utf-8")
        for filename in ("app-admin-common.js", "app-dashboard.js", "app-health-qq.js", "app-qzone.js", "app-identity-policy.js", "app-persona-builder.js", "app-groups.js", "app-config.js", "app-content.js", "app-tools.js", "app-mcp.js", "app-auth.js", "app-activity.js", "app-operations.js")
    )
    legacy_patterns = (
        r"(?:切换失败|保存失败|添加失败|审核失败|重载失败|检查失败|更新失败)[：:]?\s*[\"']?\s*\+\s*e\.message",
        r"state\.qzone(?:Post|Action|Auth)Result\s*=\s*\{\s*ok\s*:\s*false\s*,\s*(?:error|message)\s*:\s*e\.message",
    )
    findings = [pattern for pattern in legacy_patterns if re.search(pattern, sources)]
    assert findings == []


def test_external_write_diagnostics_expose_retry_safety() -> None:
    for filename in ("qzone_routes.py", "health_routes.py", "qq_routes.py", "data_transfer_routes.py", "plugin_manager_routes.py"):
        source = (ROUTES / filename).read_text(encoding="utf-8")
        assert "retryable" in source, filename
        assert "outcome_unknown" in source, filename
        assert "operation_id" in source, filename


def test_operation_histories_are_deduplicated_single_open_accordions() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    history_surfaces = "\n".join(
        (STATIC / filename).read_text(encoding="utf-8")
        for filename in ("app-admin-common.js", "app-health-qq.js", "app-qzone.js", "app-identity-policy.js", "app-persona-builder.js", "app-groups.js", "app-auth.js", "app-config.js", "app-content.js")
    )

    assert "function operationDiagnosticFingerprint" in core
    assert "function renderOperationHistory" in core
    assert "index === 0" in core
    assert '<details class="operation-diagnostic' in core
    assert 'data-operation-group' in core
    assert 'item.open = false' in core
    assert history_surfaces.count("renderOperationHistory(") >= 6


def test_operation_diagnostic_summary_is_compact_and_accessible() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    summary = re.search(r'<summary class="operation-summary">(.+?)</summary>', core, re.DOTALL)
    assert summary is not None
    markup = summary.group(1)
    ordered_tokens = (
        'class="operation-summary-mark"',
        'class="operation-summary-copy"',
        '<code class="operation-code">',
        'class="operation-chevron"',
    )
    positions = [markup.index(token) for token in ordered_tokens]
    assert positions == sorted(positions)
    assert "PHASE / " in markup

    assert core.count('role="status"') == 1
    assert 'id="operation-live-region" class="sr-only" role="status"' in core
    assert '<div class="operation-diagnostic-body">' in core
    assert '<code class="operation-full-code">' not in core
    assert core.count('<code class="operation-code">') == 1

    code_rule = re.search(r"\.operation-code\s*\{([^}]+)\}", css)
    assert code_rule is not None
    assert "white-space:nowrap" in code_rule.group(1)
    assert "word-break:normal" in code_rule.group(1)
    assert "text-overflow:ellipsis" in code_rule.group(1)
    assert ".operation-summary:focus-visible" in css
    assert "min-height:54px" in css
    assert "min-height:68px" in css
    assert ".operation-summary-copy small { display:none; }" not in css


def test_navigation_uses_distinct_accessible_svg_icons() -> None:
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    auth = (STATIC / "app-auth.js").read_text(encoding="utf-8")

    assert "const ICON_PATHS" in core
    assert "function renderIcon" in core
    assert "renderIcon('menu')" in core
    assert "切换到浅色主题" in core and "切换到深色主题" in core
    assert "切换到浅色主题" in auth and "切换到深色主题" in auth
    for icon in ("heart-pulse", "sparkles", "brain", "database", "plug", "terminal", "archive", "user-cog"):
        assert f"'{icon}'" in core
