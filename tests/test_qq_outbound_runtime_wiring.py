from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _call_keywords(relative_path: str, function_name: str) -> list[set[str]]:
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    matches: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else ""
        )
        if name == function_name:
            matches.append({keyword.arg for keyword in node.keywords if keyword.arg})
    return matches


def test_runtime_builder_injects_single_ledger_into_all_runtime_owners() -> None:
    runtime_builder = "core/runtime_builder.py"
    assert len(_call_keywords(runtime_builder, "QQOutboundLedger")) == 1
    for function_name, expected_calls in (
        ("build_agent_runtime_deps", 1),
        ("TtsService", 1),
        ("build_yaml_response_processor", 2),
        ("RuntimeDeps", 1),
        ("PluginRuntimeBundle", 1),
    ):
        calls = _call_keywords(runtime_builder, function_name)
        assert len(calls) == expected_calls
        assert all("qq_outbound_ledger" in keywords for keywords in calls)
    recovery_calls = _call_keywords(runtime_builder, "recover_interrupted_dispatches")
    assert len(recovery_calls) == 1


def test_runtime_reload_preserves_native_mcp_tools() -> None:
    tree = ast.parse((ROOT / "core/runtime_builder.py").read_text(encoding="utf-8"))
    source_kinds: set[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "extension_source_kinds"
            for target in node.targets
        ):
            continue
        source_kinds = set(ast.literal_eval(node.value))
        break

    assert source_kinds is not None
    assert "mcp_builtin" in source_kinds


def test_flow_and_agent_production_builders_forward_ledger() -> None:
    for function_name in ("build_proactive_checker", "build_group_idle_checker"):
        calls = _call_keywords("flows/__init__.py", function_name)
        assert len(calls) == 1
        assert "qq_outbound_ledger" in calls[0]

    for relative_path in (
        "handlers/reply_pipeline/pipeline_context.py",
        "handlers/yaml_pipeline/processor.py",
    ):
        calls = _call_keywords(relative_path, "ActionExecutor")
        assert len(calls) == 1
        assert "qq_outbound_ledger" in calls[0]
        recall_calls = _call_keywords(relative_path, "register_qq_recall_tool")
        assert len(recall_calls) == 1
        assert {"executor", "bot", "event", "cutoff"} <= recall_calls[0]
