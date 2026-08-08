from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ._loader import load_personification_module


group_routes = load_personification_module("plugin.personification.webui.routes.group_routes")
schemas = load_personification_module("plugin.personification.webui.schemas")


def test_group_frontend_persists_and_renders_operation_diagnostics() -> None:
    root = Path(__file__).resolve().parents[1] / "webui" / "static"
    source = (root / "app-groups.js").read_text(encoding="utf-8") + (root / "app-admin-common.js").read_text(encoding="utf-8")
    assert 'rememberAdminOperation("group"' in source
    assert 'renderAdminOperations("group","群管理操作诊断")' in source
    assert 'renderAdminOperations("group","群开关操作诊断")' in source
    assert 'sessionStorage.setItem(ADMIN_OPERATION_STORAGE_KEY' in source
    for legacy in ("重建失败：\" + e.message", "分析失败：\" + e.message", "保存作息失败：\" + e.message"):
        assert legacy not in source


class _DataStore:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def load_sync(self, namespace: str):
        return self.data.get(namespace)

    def save_sync(self, namespace: str, value: object) -> None:
        self.data[namespace] = value


class _Caller:
    def __init__(self, content: str = "", exc: Exception | None = None) -> None:
        self.content = content
        self.exc = exc

    async def chat_with_tools(self, *args, **kwargs):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=self.content)


class _KnowledgeStore:
    def __init__(self, *, fail_after: int = -1) -> None:
        self.items: list[dict] = []
        self.fail_after = fail_after

    def write_memory_item(self, item: dict) -> None:
        if self.fail_after >= 0 and len(self.items) >= self.fail_after:
            raise RuntimeError("raw-persistence-secret")
        self.items.append(dict(item))


def _request(method: str = "POST") -> Request:
    return Request({"type": "http", "method": method, "path": "/", "headers": [], "client": ("127.0.0.1", 1)})


def _admin():
    return schemas.AdminIdentity(qq="10001", device_id="device-1", label="test")


def _runtime(*, caller=None, memory_store=None):
    inner = SimpleNamespace(agent_tool_caller=caller) if caller is not None else SimpleNamespace()
    return SimpleNamespace(
        plugin_config=SimpleNamespace(),
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        runtime_bundle=SimpleNamespace(
            memory_store=memory_store,
            reply_processor_deps=SimpleNamespace(runtime=inner),
        ),
    )


def _endpoint(runtime, path: str, method: str):
    router = group_routes.build_group_router(runtime=runtime)
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


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


def _install_rate_store(monkeypatch) -> None:
    data_store = load_personification_module("plugin.personification.core.data_store")
    monkeypatch.setattr(data_store, "get_data_store", lambda: _DataStore())


def _install_source(monkeypatch, *, style: bool) -> None:
    module_name = (
        "plugin.personification.core.group_style_autobuild"
        if style
        else "plugin.personification.core.group_knowledge_autobuild"
    )
    module = load_personification_module(module_name)
    monkeypatch.setattr(module, "_load_messages_since", lambda **_kwargs: [{"text": "x"}] * 20)
    monkeypatch.setattr(module, "_format_chat_summary", lambda _rows: "safe summary")


def test_whitelist_success_preserves_fields_and_failure_is_safe(monkeypatch) -> None:
    utils = load_personification_module("plugin.personification.utils")
    monkeypatch.setattr(group_routes.webui_audit_log, "record", lambda **_kwargs: None)
    monkeypatch.setattr(utils, "set_group_enabled", lambda group_id, enabled: None)
    runtime = _runtime()
    endpoint = _endpoint(runtime, "/api/groups/{group_id}/whitelist", "POST")
    disable = _endpoint(runtime, "/api/groups/{group_id}/whitelist", "DELETE")

    body = asyncio.run(endpoint("g1", _request(), _admin()))
    assert body["success"] is True
    assert body["enabled"] is True
    assert body["group_id"] == "g1"
    assert body["authority"] == "group_config.enabled"
    assert body["code"] == "group_whitelist_enabled"
    _assert_contract(body, ok=True)

    disabled = asyncio.run(disable("g1", _request("DELETE"), _admin()))
    assert disabled["success"] is True
    assert disabled["enabled"] is False
    assert disabled["code"] == "group_whitelist_disabled"
    _assert_contract(disabled, ok=True)

    def _fail(_group_id: str, _enabled: bool) -> None:
        raise RuntimeError("raw-whitelist-secret")

    monkeypatch.setattr(utils, "set_group_enabled", _fail)
    status, report = _failure(endpoint("g1", _request(), _admin()))
    assert status == 500
    assert report["code"] == "group_whitelist_persist_failed"
    assert report["phase"] == "persistence"
    assert report["outcome_unknown"] is True
    assert "raw-whitelist-secret" not in str(report)
    _assert_contract(report, ok=False)


def test_alias_save_delete_diagnostics_and_validation_are_safe(monkeypatch) -> None:
    entries: dict[str, dict] = {}
    monkeypatch.setattr(group_routes.webui_audit_log, "record", lambda **_kwargs: None)

    def _save(_group_id, user_id, aliases, *, note, updated_by):
        entry = {"user_id": user_id, "aliases": [str(aliases)], "note": note, "updated_by": updated_by}
        entries[user_id] = entry
        return entry

    monkeypatch.setattr(group_routes, "set_group_member_aliases", _save)
    monkeypatch.setattr(group_routes, "list_group_member_aliases", lambda _group_id: dict(entries))
    monkeypatch.setattr(group_routes, "delete_group_member_aliases", lambda _group_id, user_id: entries.pop(user_id, None) is not None)
    runtime = _runtime()
    save = _endpoint(runtime, "/api/groups/{group_id}/aliases/{user_id}", "PUT")
    delete = _endpoint(runtime, "/api/groups/{group_id}/aliases/{user_id}", "DELETE")

    saved = asyncio.run(save("g1", "u1", _request("PUT"), {"aliases": "alpha"}, _admin()))
    assert saved["success"] is True
    assert saved["entry"]["user_id"] == "u1"
    assert saved["code"] == "group_alias_saved"
    _assert_contract(saved, ok=True)

    deleted = asyncio.run(delete("g1", "u1", _request("DELETE"), _admin()))
    assert deleted["success"] is True
    assert deleted["deleted"] is True
    assert deleted["code"] == "group_alias_deleted"

    def _invalid(*_args, **_kwargs):
        raise ValueError("raw-alias-secret")

    monkeypatch.setattr(group_routes, "set_group_member_aliases", _invalid)
    status, report = _failure(save("g1", "u1", _request("PUT"), {"aliases": "x"}, _admin()))
    assert status == 400
    assert report["code"] == "group_alias_invalid"
    assert report["outcome_unknown"] is False
    assert "raw-alias-secret" not in str(report)

    def _delete_failure(*_args, **_kwargs):
        raise RuntimeError("raw-alias-delete-secret")

    monkeypatch.setattr(group_routes, "delete_group_member_aliases", _delete_failure)
    status, report = _failure(delete("g1", "u1", _request("DELETE"), _admin()))
    assert status == 500
    assert report["code"] == "group_alias_delete_failed"
    assert report["outcome_unknown"] is True
    assert "raw-alias-delete-secret" not in str(report)


def test_schedule_save_and_generation_stage_diagnostics(monkeypatch) -> None:
    utils = load_personification_module("plugin.personification.utils")
    monkeypatch.setattr(group_routes.webui_audit_log, "record", lambda **_kwargs: None)
    monkeypatch.setattr(utils, "set_group_schedule_enabled", lambda *_args: None)
    monkeypatch.setattr(utils, "set_group_schedule_prompt", lambda *_args: None)
    runtime = _runtime()
    save = _endpoint(runtime, "/api/groups/{group_id}/schedule", "PUT")
    generate = _endpoint(runtime, "/api/groups/{group_id}/schedule/auto-generate", "POST")

    saved = asyncio.run(save("g1", _request("PUT"), {"enabled": True, "schedule_prompt": "night"}, _admin()))
    assert saved["success"] is True
    assert saved["enabled"] is True
    assert saved["schedule_prompt"] == "night"
    assert saved["code"] == "group_schedule_saved"

    def _schedule_failure(*_args):
        raise RuntimeError("raw-schedule-secret")

    monkeypatch.setattr(utils, "set_group_schedule_prompt", _schedule_failure)
    status, report = _failure(save("g1", _request("PUT"), {"enabled": True, "schedule_prompt": "night"}, _admin()))
    assert status == 500
    assert report["code"] == "group_schedule_persist_failed"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert "raw-schedule-secret" not in str(report)
    monkeypatch.setattr(utils, "set_group_schedule_prompt", lambda *_args: None)

    purposes: list[str] = []

    async def _model(_runtime, _group_id, _messages, *, purpose):
        purposes.append(purpose)
        return "draft" if purpose == "group_schedule_synthesis" else "research"

    monkeypatch.setattr(group_routes, "_call_group_schedule_model", _model)
    generated = asyncio.run(generate("g1", _request(), {}, _admin()))
    assert generated["schedule_prompt"] == "draft"
    assert len(generated["subagents"]) == 3
    assert generated["code"] == "group_schedule_generated"
    assert purposes == ["group_schedule_research"] * 3 + ["group_schedule_synthesis"]
    assert next(item for item in generated["steps"] if item["key"] == "persist")["status"] == "skipped"

    async def _research_failure(*_args, **_kwargs):
        raise RuntimeError("raw-research-secret")

    monkeypatch.setattr(group_routes, "_call_group_schedule_model", _research_failure)
    status, report = _failure(generate("g1", _request(), {}, _admin()))
    assert status == 502
    assert report["code"] == "group_schedule_research_failed"
    assert report["phase"] == "research"
    assert "raw-research-secret" not in str(report)

    calls = 0

    async def _synthesis_failure(*_args, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs["purpose"] == "group_schedule_synthesis":
            raise RuntimeError("raw-synthesis-secret")
        return "research"

    monkeypatch.setattr(group_routes, "_call_group_schedule_model", _synthesis_failure)
    status, report = _failure(generate("g1", _request(), {}, _admin()))
    assert calls == 4
    assert status == 502
    assert report["code"] == "group_schedule_synthesis_failed"
    assert report["phase"] == "synthesis"
    assert "raw-synthesis-secret" not in str(report)


@pytest.mark.parametrize(
    ("caller", "code", "phase", "status"),
    [
        (_Caller(exc=RuntimeError("raw-caller-secret")), "group_style_caller_failed", "caller", 502),
        (_Caller(content=""), "group_style_empty_response", "model_output", 502),
        (_Caller(content="not-json raw-json-secret"), "group_style_json_invalid", "json_parse", 422),
        (_Caller(content='{"tone":"only"}'), "group_style_schema_invalid", "schema_validation", 422),
    ],
)
def test_group_style_rebuild_reports_model_stages(monkeypatch, caller, code: str, phase: str, status: int) -> None:
    _install_rate_store(monkeypatch)
    _install_source(monkeypatch, style=True)
    monkeypatch.setattr(group_routes.webui_audit_log, "record", lambda **_kwargs: None)
    runtime = _runtime(caller=caller, memory_store=SimpleNamespace())
    endpoint = _endpoint(runtime, "/api/groups/{group_id}/style/rebuild", "POST")

    actual_status, report = _failure(endpoint("g1", _request(), {}, _admin()))
    assert actual_status == status
    assert report["code"] == code
    assert report["phase"] == phase
    assert report["retryable"] is True
    assert report["partial"] is False
    assert report["outcome_unknown"] is False
    assert "raw-" not in str(report)
    _assert_contract(report, ok=False)


def test_group_style_rebuild_success_and_persist_failure(monkeypatch) -> None:
    _install_rate_store(monkeypatch)
    _install_source(monkeypatch, style=True)
    monkeypatch.setattr(group_routes.webui_audit_log, "record", lambda **_kwargs: None)
    style_module = load_personification_module("plugin.personification.core.group_style_autobuild")
    valid = '{"tone":"自然","pace":"快","catchphrases":["确实"],"taboos":[],"typical_length":"短"}'
    runtime = _runtime(caller=_Caller(content=valid), memory_store=SimpleNamespace())
    endpoint = _endpoint(runtime, "/api/groups/{group_id}/style/rebuild", "POST")
    snapshot = {"id": 9, "group_id": "g1", "style_text": "safe", "style_json": {}, "created_at": 1.0}

    monkeypatch.setattr(style_module, "_save_snapshot", lambda *_args, **_kwargs: 9)
    monkeypatch.setattr(style_module, "list_style_snapshots", lambda *_args, **_kwargs: [snapshot])
    success = asyncio.run(endpoint("g1", _request(), {}, _admin()))
    assert success["success"] is True
    assert success["new_snapshot"]["id"] == 9
    assert success["snapshots"] == [snapshot]
    assert success["code"] == "group_style_rebuilt"
    _assert_contract(success, ok=True)

    def _persist_failure(*_args, **_kwargs):
        raise RuntimeError("raw-style-persist-secret")

    monkeypatch.setattr(style_module, "_save_snapshot", _persist_failure)
    status, report = _failure(endpoint("g2", _request(), {}, _admin()))
    assert status == 500
    assert report["code"] == "group_style_persist_failed"
    assert report["phase"] == "persistence"
    assert report["retryable"] is False
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert "raw-style-persist-secret" not in str(report)


def test_group_knowledge_rebuild_confirms_persistence_and_marks_partial_failure(monkeypatch) -> None:
    _install_rate_store(monkeypatch)
    _install_source(monkeypatch, style=False)
    monkeypatch.setattr(group_routes.webui_audit_log, "record", lambda **_kwargs: None)
    content = (
        '[{"term":"term-a","definition":"first","aliases":[],"scope":"group","risk_level":"low"},'
        '{"term":"term-b","definition":"second","aliases":[],"scope":"group","risk_level":"low"}]'
    )
    store = _KnowledgeStore()
    runtime = _runtime(caller=_Caller(content=content), memory_store=store)
    endpoint = _endpoint(runtime, "/api/groups/{group_id}/knowledge/rebuild", "POST")

    success = asyncio.run(endpoint("g1", _request(), {}, _admin()))
    assert success["success"] is True
    assert success["saved"] == 2
    assert success["code"] == "group_knowledge_rebuilt"
    assert len(store.items) == 2
    _assert_contract(success, ok=True)

    failing_store = _KnowledgeStore(fail_after=1)
    failing_runtime = _runtime(caller=_Caller(content=content), memory_store=failing_store)
    failing_endpoint = _endpoint(failing_runtime, "/api/groups/{group_id}/knowledge/rebuild", "POST")
    status, report = _failure(failing_endpoint("g2", _request(), {}, _admin()))
    assert status == 500
    assert report["code"] == "group_knowledge_persist_failed"
    assert report["phase"] == "persistence"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert report["retryable"] is False
    assert "raw-persistence-secret" not in str(report)
