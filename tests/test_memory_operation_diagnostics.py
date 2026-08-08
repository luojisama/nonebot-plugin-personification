from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ._loader import load_personification_module


memory_routes = load_personification_module("plugin.personification.webui.routes.memory_routes")
schemas = load_personification_module("plugin.personification.webui.schemas")


def _admin():
    return schemas.AdminIdentity(qq="10001", device_id="device-1", label="test")


def _runtime(store):
    return SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        runtime_bundle=SimpleNamespace(memory_store=store),
    )


def _endpoint(runtime, path: str, method: str):
    router = memory_routes.build_memory_router(runtime=runtime)
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _failure(awaitable) -> tuple[int, dict]:
    with pytest.raises(HTTPException) as caught:
        asyncio.run(awaitable)
    return caught.value.status_code, caught.value.detail


def _assert_contract(report: dict, *, ok: bool) -> None:
    required = {
        "ok",
        "code",
        "phase",
        "title",
        "message",
        "details",
        "steps",
        "retryable",
        "partial",
        "outcome_unknown",
    }
    assert required <= report.keys()
    assert report["ok"] is ok
    assert isinstance(report["details"], list)
    assert isinstance(report["steps"], list)


@pytest.fixture
def _store(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=True,
        personification_memory_rag_enabled=True,
        personification_memory_vector_backend="sqlite_exact",
        personification_memory_recall_top_k=8,
        personification_memory_search_scan_limit=300,
    )
    data_store.init_data_store(cfg)
    module = load_personification_module("plugin.personification.core.memory_store")
    store = module.MemoryStore(plugin_config=cfg, logger=SimpleNamespace(warning=lambda *_a, **_k: None))
    store.initialize()
    return store


def test_memory_router_remains_original_read_only_surface() -> None:
    router = memory_routes.build_memory_router(runtime=_runtime(SimpleNamespace()))
    actual = {(route.path, method) for route in router.routes for method in route.methods}
    assert actual == {
        ("/api/memory/vector-index", "GET"),
        ("/api/memory/vector-index/rebuild", "POST"),
        ("/api/memory/search-test", "GET"),
        ("/api/memory/recent", "GET"),
        ("/api/memory/raw-chat", "GET"),
        ("/api/memory/inner-state", "GET"),
        ("/api/memory/detail/{memory_id}", "GET"),
        ("/api/memory/graph", "GET"),
        ("/api/memory/palace-zones", "GET"),
    }


class _MutationFailureStore:
    def rebuild_vector_index(self, *, limit: int = 0):
        raise RuntimeError("raw-rebuild-secret")

    def get_vector_index_status(self):
        return {"stale_count": 1}

    def recall_memories(self, **_kwargs):
        raise RuntimeError("raw-recall-secret")


def test_rebuild_and_recall_failures_report_unknown_mutation_state() -> None:
    runtime = _runtime(_MutationFailureStore())
    rebuild = _endpoint(runtime, "/api/memory/vector-index/rebuild", "POST")
    recall = _endpoint(runtime, "/api/memory/search-test", "GET")

    rebuild_status, rebuild_report = _failure(rebuild(0, _admin()))
    assert rebuild_status == 500
    assert rebuild_report["code"] == "memory_vector_rebuild_unconfirmed"
    assert rebuild_report["partial"] is True
    assert rebuild_report["outcome_unknown"] is True
    assert "raw-rebuild-secret" not in str(rebuild_report)
    _assert_contract(rebuild_report, ok=False)

    recall_status, recall_report = _failure(recall("query", "g1", "u1", "group", 8, _admin()))
    assert recall_status == 500
    assert recall_report["code"] == "memory_recall_test_unconfirmed"
    assert recall_report["partial"] is True
    assert recall_report["outcome_unknown"] is True
    assert "raw-recall-secret" not in str(recall_report)
    _assert_contract(recall_report, ok=False)


def test_rebuild_and_recall_success_preserve_existing_response_fields(_store) -> None:
    _store.write_memory_item({"memory_id": "rag-item", "summary": "蓝色列车模型", "user_id": "u1"})
    runtime = _runtime(_store)
    rebuild = _endpoint(runtime, "/api/memory/vector-index/rebuild", "POST")
    recall = _endpoint(runtime, "/api/memory/search-test", "GET")

    rebuilt = asyncio.run(rebuild(0, _admin()))
    assert rebuilt["status"] == "ok"
    assert "rebuilt" in rebuilt
    assert isinstance(rebuilt["index"], dict)
    assert rebuilt["code"] == "memory_vector_rebuilt"
    assert rebuilt["diagnostic"]["code"] == "memory_vector_rebuilt"

    recalled = asyncio.run(recall("蓝色列车模型", "", "u1", "private", 8, _admin()))
    assert recalled["query"] == "蓝色列车模型"
    assert recalled["count"] == len(recalled["items"])
    assert any(item["memory_id"] == "rag-item" for item in recalled["items"])
    assert recalled["code"] == "memory_recall_test_completed"
    assert recalled["diagnostic"]["code"] == "memory_recall_test_completed"


def test_private_store_api_unavailable_is_structured(monkeypatch) -> None:
    memory_store = load_personification_module("plugin.personification.core.memory_store")
    monkeypatch.delattr(memory_store, "_connect")
    store = SimpleNamespace(ensure_group_space=lambda _group_id: None)
    endpoint = _endpoint(_runtime(store), "/api/memory/raw-chat", "GET")

    status, report = _failure(endpoint("g1", 20, 0.0, _admin()))

    assert status == 503
    assert report["code"] == "memory_private_store_api_unavailable"
    assert report["partial"] is False
    assert report["outcome_unknown"] is False
    _assert_contract(report, ok=False)


def test_memory_frontend_is_read_only_and_renders_detailed_diagnostics() -> None:
    source = (Path(__file__).resolve().parents[1] / "webui" / "static" / "app-content.js").read_text(encoding="utf-8")

    assert "MEMORY_DIAGNOSTICS_STORAGE_KEY" in source
    assert "sessionStorage.setItem(MEMORY_DIAGNOSTICS_STORAGE_KEY" in source
    assert "renderMemoryDiagnosticsCard()" in source
    assert "renderOperationHistory(memoryDiagnostics()" in source
    assert "rememberMemoryDiagnostic(result)" in source
    assert "operationDiagnosticFromError(error, title)" in source
    for forbidden in (
        'api("/memory/batch"',
        'api("/memory/" + encodeURIComponent',
        'method: updating ? "PATCH" : "POST"',
        "startCreateMemory",
        "startEditMemory",
        "deleteMemoryById",
        "batchDeleteMemories",
        "memorySelectedIds",
        "memoryEditor",
    ):
        assert forbidden not in source
    for legacy in (
        '"重建失败：" + e.message',
        '"召回测试失败：" + e.message',
        'alertFlash("err", e.message)',
        "'导出失败：' + e.message",
    ):
        assert legacy not in source


def test_memory_route_has_no_raw_exception_http_responses() -> None:
    source = Path(memory_routes.__file__).read_text(encoding="utf-8")
    assert "detail=str(exc)" not in source
    assert "detail=f\"memory_store 私有接口不可用：{exc}\"" not in source
