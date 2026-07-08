from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


def _write_demo_plugin(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    init_lines = [
        "from nonebot import on_command",
        "",
        'demo_cmd = on_command("demo")',
        "",
        "async def handle_demo():",
        '    return "ok"',
    ]
    init_lines.extend(f"VALUE_{index} = {index}" for index in range(150))
    init_lines.append('END_MARKER = "tail is still present"')
    (root / "__init__.py").write_text("\n".join(init_lines), encoding="utf-8")
    (root / "helpers.py").write_text(
        "\n".join(
            [
                "def helper_a():",
                '    return "a"',
                "",
                "def helper_b():",
                '    return "b"',
            ]
        ),
        encoding="utf-8",
    )


def _flatten_unit_chunk_ids(units: list[dict]) -> list[str]:
    ids: list[str] = []
    for unit in units:
        for chunk in unit.get("chunks") or []:
            ids.append(str(chunk.get("chunk_id", "") or ""))
    return ids


def test_source_snapshot_reads_complete_python_files(tmp_path: Path) -> None:
    snapshot_mod = load_personification_module("plugin.personification.core.plugin_knowledge.snapshot")
    root = tmp_path / "demo_plugin"
    _write_demo_plugin(root)

    snapshot = snapshot_mod.extract_plugin_source_snapshot(root)

    assert snapshot is not None
    assert snapshot["source_complete"] is True
    assert snapshot["source_truncated"] is False
    assert snapshot["source_coverage"]["full_input"] is True
    assert snapshot["source_coverage"]["analysis_scope"] == "full_readable_python_source"
    assert {item["path"] for item in snapshot["files"]} == {"__init__.py", "helpers.py"}
    assert "END_MARKER" in "\n".join(chunk["text"] for chunk in snapshot["chunks"])
    assert snapshot["source_chunk_count"] == len(snapshot["chunks"])
    assert snapshot["source_chars"] == sum(len(chunk["text"]) for chunk in snapshot["chunks"])


def test_analysis_units_cover_every_chunk_and_fallback_when_module_bundle_is_incomplete(tmp_path: Path) -> None:
    snapshot_mod = load_personification_module("plugin.personification.core.plugin_knowledge.snapshot")
    analysis_mod = load_personification_module("plugin.personification.core.plugin_knowledge.analysis")
    root = tmp_path / "demo_plugin"
    _write_demo_plugin(root)
    snapshot = snapshot_mod.extract_plugin_source_snapshot(root)
    assert snapshot is not None
    snapshot["analysis_strategy"] = "module_bundles"

    mode, units = analysis_mod._build_analysis_units(snapshot)
    expected_ids = sorted(str(chunk["chunk_id"]) for chunk in snapshot["chunks"])
    unit_ids = _flatten_unit_chunk_ids(units)

    assert mode == "module_bundle_multistage"
    assert sorted(unit_ids) == expected_ids
    assert len(unit_ids) == len(set(unit_ids))

    broken = dict(snapshot)
    broken_bundles = [dict(bundle) for bundle in snapshot["module_bundles"]]
    assert broken_bundles
    if len(broken_bundles[-1].get("chunk_ids") or []) > 1:
        broken_bundles[-1]["chunk_ids"] = list(broken_bundles[-1]["chunk_ids"][:-1])
    else:
        broken_bundles.pop()
    broken["module_bundles"] = broken_bundles

    fallback_mode, fallback_units = analysis_mod._build_analysis_units(broken)
    fallback_ids = _flatten_unit_chunk_ids(fallback_units)

    assert fallback_mode == "chunk_batch_multistage"
    assert sorted(fallback_ids) == expected_ids
    assert len(fallback_ids) == len(set(fallback_ids))


def test_plugin_knowledge_routes_expose_full_input_coverage(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    route_mod = load_personification_module("plugin.personification.webui.routes.plugin_knowledge_routes")
    store_mod = load_personification_module("plugin.personification.core.knowledge_store")
    store = store_mod.PluginKnowledgeStore(tmp_path)
    source_coverage = {
        "analysis_scope": "full_readable_python_source",
        "analysis_strategy": "module_bundles",
        "analysis_mode": "module_bundle_multistage",
        "full_input": True,
        "source_complete": True,
        "source_truncated": False,
        "source_file_count": 2,
        "source_chunk_count": 3,
        "source_chars": 1200,
        "analysis_unit_count": 2,
        "unique_analyzed_chunk_count": 3,
        "coverage_percent": 100.0,
        "note": "full input",
    }
    source_snapshot = {
        "files": [
            {"path": "__init__.py", "line_count": 20, "size": 600, "symbols": ["demo_cmd"]},
            {"path": "helpers.py", "line_count": 10, "size": 600, "symbols": ["helper_a"]},
        ],
        "chunks": [
            {"chunk_id": "__init__.py#1", "file": "__init__.py", "start_line": 1, "end_line": 20, "text": "demo"},
            {"chunk_id": "helpers.py#1", "file": "helpers.py", "start_line": 1, "end_line": 5, "text": "helper_a"},
            {"chunk_id": "helpers.py#2", "file": "helpers.py", "start_line": 6, "end_line": 10, "text": "helper_b"},
        ],
        "source_chars": 1200,
        "source_chunk_count": 3,
        "analysis_strategy": "module_bundles",
        "analysis_scope": "full_readable_python_source",
        "source_complete": True,
        "source_truncated": False,
        "source_coverage": source_coverage,
    }
    entry = {
        "display_name": "Demo",
        "summary": "demo summary",
        "keywords": ["demo"],
        "features": {"chat": {"title": "聊天", "summary": "处理 demo 命令", "files": ["__init__.py"]}},
        "analysis_mode": "module_bundle_multistage",
        "analysis_scope": "full_readable_python_source",
        "source_file_count": 2,
        "source_chunk_count": 3,
        "source_chars": 1200,
        "source_complete": True,
        "source_truncated": False,
        "source_coverage": source_coverage,
        "updated_at": "2026-06-30T00:00:00+08:00",
    }
    store._save_source_snapshot_sync("demo", source_snapshot)
    store._save_plugin_entry_sync("demo", "local", entry)

    runtime = SimpleNamespace(
        runtime_bundle=SimpleNamespace(
            reply_processor_deps=SimpleNamespace(
                runtime=SimpleNamespace(knowledge_store=store),
            ),
        ),
    )
    app = FastAPI()
    app.dependency_overrides[route_mod.require_admin] = lambda: SimpleNamespace(qq="10001", label="test")
    app.include_router(route_mod.build_plugin_knowledge_router(runtime=runtime))
    client = TestClient(app)

    listed = client.get("/api/plugin-knowledge/list")
    assert listed.status_code == 200, listed.text
    item = listed.json()["plugins"][0]
    assert item["source_coverage"]["full_input"] is True
    assert item["source_coverage"]["source_chars"] == 1200
    assert item["analysis_scope"] == "full_readable_python_source"

    detail = client.get("/api/plugin-knowledge/detail/demo")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["source_coverage"]["full_input"] is True
    assert body["source_coverage"]["source_chunk_count"] == 3
    assert body["source_snapshot"]["source_truncated"] is False
