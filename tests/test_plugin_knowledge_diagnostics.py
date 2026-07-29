from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ._loader import load_personification_module


plugin_knowledge_routes = load_personification_module(
    "plugin.personification.webui.routes.plugin_knowledge_routes"
)


class _Store:
    def __init__(self) -> None:
        self.failures: dict[str, Exception] = {}

    def _fail(self, operation: str) -> None:
        if operation in self.failures:
            raise self.failures[operation]

    def load_index_sync(self):
        self._fail("list")
        return {
            "plugins": {
                "demo": {
                    "display_name": "Demo",
                    "summary": "safe summary",
                    "keywords": ["demo"],
                    "category": "local",
                }
            }
        }

    def load_plugin_entry_sync(self, plugin_name: str):
        self._fail("detail")
        if plugin_name == "missing":
            return None
        return {"display_name": "Demo", "summary": "safe detail"}

    def load_runtime_snapshot_sync(self, _plugin_name: str):
        self._fail("runtime_snapshot")
        return {"commands": ["demo"]}

    def load_source_snapshot_sync(self, _plugin_name: str):
        self._fail("source_snapshot")
        return {"source_chunk_count": 1, "source_coverage": {"full_input": True}}

    def search_plugins(self, _query: str, *, top_k: int):
        self._fail("search")
        return ["demo"][:top_k]


def _runtime(store: _Store | None):
    return SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        runtime_bundle=SimpleNamespace(
            reply_processor_deps=SimpleNamespace(
                runtime=SimpleNamespace(knowledge_store=store),
            )
        ),
    )


def _endpoint(runtime, path: str):
    router = plugin_knowledge_routes.build_plugin_knowledge_router(runtime=runtime)
    for route in router.routes:
        if route.path == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")


def _failure(call) -> tuple[int, dict]:
    with pytest.raises(HTTPException) as caught:
        asyncio.run(call)
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


def test_plugin_knowledge_read_success_preserves_fields_and_adds_diagnostics() -> None:
    runtime = _runtime(_Store())
    list_all = _endpoint(runtime, "/api/plugin-knowledge/list")
    detail = _endpoint(runtime, "/api/plugin-knowledge/detail/{plugin_name}")
    search = _endpoint(runtime, "/api/plugin-knowledge/search")

    listed = asyncio.run(list_all(None))
    assert listed["available"] is True
    assert listed["total"] == 1
    assert listed["plugins"][0]["plugin_name"] == "demo"
    assert listed["diagnostic"]["code"] == "plugin_knowledge_list_loaded"
    _assert_contract(listed, ok=True)

    loaded = asyncio.run(detail("demo", None))
    assert loaded["plugin_name"] == "demo"
    assert loaded["entry"]["display_name"] == "Demo"
    assert loaded["runtime_snapshot"] == {"commands": ["demo"]}
    assert loaded["source_snapshot"]["source_chunk_count"] == 1
    assert loaded["diagnostic"]["code"] == "plugin_knowledge_detail_loaded"
    _assert_contract(loaded, ok=True)

    found = asyncio.run(search("demo", 10, None))
    assert found["results"] == ["demo"]
    assert found["query"] == "demo"
    assert found["available"] is True
    assert found["diagnostic"]["code"] == "plugin_knowledge_search_complete"
    _assert_contract(found, ok=True)


@pytest.mark.parametrize(
    ("operation", "path", "args", "expected_code"),
    [
        ("list", "/api/plugin-knowledge/list", (None,), "plugin_knowledge_index_read_failed"),
        ("detail", "/api/plugin-knowledge/detail/{plugin_name}", ("demo", None), "plugin_knowledge_entry_read_failed"),
        ("search", "/api/plugin-knowledge/search", ("demo", 10, None), "plugin_knowledge_search_failed"),
    ],
)
def test_plugin_knowledge_read_failures_are_structured_and_safe(
    operation: str,
    path: str,
    args: tuple,
    expected_code: str,
) -> None:
    store = _Store()
    store.failures[operation] = RuntimeError(
        "https://api.example.test/index?access_token=raw-plugin-knowledge-secret"
    )
    endpoint = _endpoint(_runtime(store), path)

    status, report = _failure(endpoint(*args))

    assert status == 500
    assert report["code"] == expected_code
    assert report["trace_id"]
    assert report["details"] == [
        {"label": "异常类型", "value": "RuntimeError", "status": "error"}
    ]
    assert "api.example.test" not in str(report)
    assert "raw-plugin-knowledge-secret" not in str(report)
    _assert_contract(report, ok=False)


def test_plugin_knowledge_detail_snapshot_failure_returns_safe_partial_result() -> None:
    store = _Store()
    store.failures["source_snapshot"] = RuntimeError("raw-source-snapshot-secret")
    endpoint = _endpoint(_runtime(store), "/api/plugin-knowledge/detail/{plugin_name}")

    body = asyncio.run(endpoint("demo", None))

    assert body["entry"]["display_name"] == "Demo"
    assert body["runtime_snapshot"] == {"commands": ["demo"]}
    assert body["source_snapshot"] is None
    assert body["source_coverage"]["analysis_scope"] == "full_readable_python_source"
    assert body["code"] == "plugin_knowledge_detail_partial"
    assert body["partial"] is True
    assert body["trace_id"]
    assert "raw-source-snapshot-secret" not in str(body)
    _assert_contract(body, ok=True)


def test_plugin_knowledge_unavailable_and_not_found_are_structured() -> None:
    unavailable_list = _endpoint(_runtime(None), "/api/plugin-knowledge/list")
    unavailable_search = _endpoint(_runtime(None), "/api/plugin-knowledge/search")
    detail = _endpoint(_runtime(_Store()), "/api/plugin-knowledge/detail/{plugin_name}")

    listed = asyncio.run(unavailable_list(None))
    assert listed["plugins"] == []
    assert listed["total"] == 0
    assert listed["available"] is False
    assert listed["code"] == "plugin_knowledge_store_unavailable"
    _assert_contract(listed, ok=False)

    searched = asyncio.run(unavailable_search("demo", 10, None))
    assert searched["results"] == []
    assert searched["query"] == "demo"
    assert searched["available"] is False
    assert searched["code"] == "plugin_knowledge_store_unavailable"

    status, report = _failure(detail("missing", None))
    assert status == 404
    assert report["code"] == "plugin_knowledge_not_found"
    assert report["details"][0]["value"] == "missing"
    _assert_contract(report, ok=False)
