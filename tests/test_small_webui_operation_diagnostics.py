from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ._loader import load_personification_module


auth_routes = load_personification_module("plugin.personification.webui.routes.auth_routes")
group_routes = load_personification_module("plugin.personification.webui.routes.group_routes")
log_routes = load_personification_module("plugin.personification.webui.routes.log_routes")
persona_routes = load_personification_module("plugin.personification.webui.routes.persona_routes")
schemas = load_personification_module("plugin.personification.webui.schemas")


def _runtime(**bundle_values):
    return SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        runtime_bundle=SimpleNamespace(**bundle_values),
    )


def _admin():
    return schemas.AdminIdentity(qq="10001", device_id="admin-device", label="test")


def _request(method: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 10000),
        }
    )


def _endpoint(module, runtime, path: str, method: str):
    builders = {
        auth_routes: auth_routes.build_auth_router,
        group_routes: group_routes.build_group_router,
        log_routes: log_routes.build_log_router,
        persona_routes: persona_routes.build_persona_router,
    }
    router = builders[module](runtime=runtime)
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


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


def _failure(awaitable) -> tuple[int, dict]:
    with pytest.raises(HTTPException) as caught:
        asyncio.run(awaitable)
    return caught.value.status_code, caught.value.detail


def test_device_writes_preserve_fields_and_record_audit(monkeypatch) -> None:
    audit_actions: list[str] = []
    monkeypatch.setattr(
        auth_routes.webui_audit_log,
        "record",
        lambda **kwargs: audit_actions.append(str(kwargs.get("action", ""))),
    )
    monkeypatch.setattr(auth_routes.webui_auth_store, "list_pending_devices", lambda: [{"id": "pending-1"}])
    monkeypatch.setattr(auth_routes.webui_auth_store, "approve_device", lambda _device_id: True)
    runtime = _runtime()

    approve = _endpoint(auth_routes, runtime, "/api/auth/devices/{device_id}/approve", "POST")
    approved = asyncio.run(approve("pending-1", _admin()))
    assert approved["success"] is True
    assert approved["code"] == "device_approved"
    _assert_contract(approved, ok=True)

    monkeypatch.setattr(auth_routes.webui_auth_store, "list_devices", lambda _qq: [{"id": "device-1", "ua": "UA", "label": "main"}])
    monkeypatch.setattr(auth_routes.webui_auth_store, "list_pending_devices", lambda: [])
    monkeypatch.setattr(auth_routes.webui_auth_store, "revoke_device", lambda _device_id: True)
    revoke = _endpoint(auth_routes, runtime, "/api/auth/devices/{device_id}", "DELETE")
    revoked = asyncio.run(revoke("device-1", _admin()))
    assert revoked["success"] is True
    assert revoked["code"] == "device_revoked"

    monkeypatch.setattr(auth_routes.webui_auth_store, "list_trusted_devices", lambda _qq: [{"id": "trust-1"}])
    monkeypatch.setattr(auth_routes.webui_auth_store, "remove_trusted_device", lambda _trust_id: True)
    untrust = _endpoint(auth_routes, runtime, "/api/auth/trusted-devices/{trust_id}", "DELETE")
    untrusted = asyncio.run(untrust("trust-1", _admin()))
    assert untrusted["success"] is True
    assert untrusted["code"] == "device_untrusted"
    assert audit_actions == ["device_approve", "device_revoke", "device_untrust"]


def test_device_persistence_failure_is_safe_unknown_and_audit_failure_is_partial(monkeypatch) -> None:
    monkeypatch.setattr(auth_routes.webui_auth_store, "list_pending_devices", lambda: [{"id": "pending-1"}])

    def _persist_failure(_device_id: str) -> bool:
        raise RuntimeError("raw-device-persistence-secret")

    monkeypatch.setattr(auth_routes.webui_auth_store, "approve_device", _persist_failure)
    runtime = _runtime()
    approve = _endpoint(auth_routes, runtime, "/api/auth/devices/{device_id}/approve", "POST")
    status, report = _failure(approve("pending-1", _admin()))
    assert status == 500
    assert report["code"] == "device_approve_persist_failed"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert "raw-device-persistence-secret" not in str(report)
    _assert_contract(report, ok=False)

    monkeypatch.setattr(auth_routes.webui_auth_store, "list_devices", lambda _qq: [{"id": "device-1", "ua": "UA", "label": "main"}])
    monkeypatch.setattr(auth_routes.webui_auth_store, "list_pending_devices", lambda: [])
    monkeypatch.setattr(auth_routes.webui_auth_store, "revoke_device", lambda _device_id: True)

    def _audit_failure(**_kwargs) -> None:
        raise RuntimeError("raw-device-audit-secret")

    monkeypatch.setattr(auth_routes.webui_audit_log, "record", _audit_failure)
    revoke = _endpoint(auth_routes, runtime, "/api/auth/devices/{device_id}", "DELETE")
    result = asyncio.run(revoke("device-1", _admin()))
    assert result["success"] is True
    assert result["partial"] is True
    assert result["outcome_unknown"] is False
    assert "raw-device-audit-secret" not in str(result)

    monkeypatch.setattr(auth_routes.webui_auth_store, "list_devices", lambda _qq: [])
    status, report = _failure(revoke("missing", _admin()))
    assert status == 404
    assert report["code"] == "device_revoke_target_not_found"
    assert report["partial"] is False
    assert report["outcome_unknown"] is False


def test_plugin_log_clear_diagnostics_preserve_deleted(monkeypatch) -> None:
    runtime = _runtime()
    endpoint = _endpoint(log_routes, runtime, "/api/logs/clear", "DELETE")
    monkeypatch.setattr(log_routes.plugin_runtime_logs, "clear_all", lambda: 7)
    monkeypatch.setattr(log_routes.webui_audit_log, "record", lambda **_kwargs: None)

    result = asyncio.run(endpoint(_request("DELETE"), _admin()))
    assert result["deleted"] == 7
    assert result["code"] == "plugin_logs_cleared"
    _assert_contract(result, ok=True)

    def _clear_failure() -> int:
        raise RuntimeError("raw-log-clear-secret")

    monkeypatch.setattr(log_routes.plugin_runtime_logs, "clear_all", _clear_failure)
    status, report = _failure(endpoint(_request("DELETE"), _admin()))
    assert status == 500
    assert report["code"] == "plugin_logs_clear_failed"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert "raw-log-clear-secret" not in str(report)


def test_persona_correction_diagnostics_preserve_profile(monkeypatch) -> None:
    class _Store:
        async def apply_user_correction(self, _user_id, _corrections):
            return SimpleNamespace(data="corrected profile")

    runtime = _runtime(persona_store=_Store())
    endpoint = _endpoint(persona_routes, runtime, "/api/personas/{user_id}/correction", "POST")
    monkeypatch.setattr(persona_routes.webui_audit_log, "record", lambda **_kwargs: None)
    result = asyncio.run(endpoint("u1", {"corrections": {"职业": "设计师"}}, _admin()))
    assert result["success"] is True
    assert result["profile_text"] == "corrected profile"
    assert result["code"] == "persona_correction_saved"
    _assert_contract(result, ok=True)

    class _FailingStore:
        async def apply_user_correction(self, _user_id, _corrections):
            raise RuntimeError("raw-persona-secret")

    failed_endpoint = _endpoint(
        persona_routes,
        _runtime(persona_store=_FailingStore()),
        "/api/personas/{user_id}/correction",
        "POST",
    )
    status, report = _failure(failed_endpoint("u1", {"corrections": {"职业": "设计师"}}, _admin()))
    assert status == 500
    assert report["code"] == "persona_correction_persist_failed"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert "raw-persona-secret" not in str(report)

    unavailable = _endpoint(
        persona_routes,
        _runtime(),
        "/api/personas/{user_id}/correction",
        "POST",
    )
    status, report = _failure(unavailable("u1", {"corrections": {"职业": "设计师"}}, _admin()))
    assert status == 503
    assert report["code"] == "persona_correction_unavailable"
    assert report["outcome_unknown"] is False


def test_group_meme_write_diagnostics_preserve_entry_and_deleted(monkeypatch) -> None:
    runtime = _runtime()
    save = _endpoint(group_routes, runtime, "/api/groups/{group_id}/memes", "POST")
    delete = _endpoint(group_routes, runtime, "/api/groups/{group_id}/memes/{term}", "DELETE")
    monkeypatch.setattr(group_routes.webui_audit_log, "record", lambda **_kwargs: None)
    monkeypatch.setattr(group_routes, "upsert_meme_entry", lambda _payload: True)
    monkeypatch.setattr(group_routes, "delete_meme_entry", lambda **_kwargs: True)

    result = asyncio.run(
        save(
            "g1",
            _request("POST"),
            {"term": "猫车", "meaning": "测试车翻车", "aliases": ["上猫车"]},
            _admin(),
        )
    )
    assert result["success"] is True
    assert result["entry"]["term"] == "猫车"
    assert result["entry"]["group_id"] == "g1"
    assert result["code"] == "group_meme_saved"
    _assert_contract(result, ok=True)

    deleted = asyncio.run(delete("g1", "猫车", _request("DELETE"), "group", _admin()))
    assert deleted["success"] is True
    assert deleted["deleted"] is True
    assert deleted["code"] == "group_meme_deleted"

    def _delete_failure(**_kwargs) -> bool:
        raise RuntimeError("raw-meme-delete-secret")

    monkeypatch.setattr(group_routes, "delete_meme_entry", _delete_failure)
    status, report = _failure(delete("g1", "猫车", _request("DELETE"), "group", _admin()))
    assert status == 500
    assert report["code"] == "group_meme_delete_failed"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert "raw-meme-delete-secret" not in str(report)


def test_device_and_log_frontends_persist_diagnostic_cards() -> None:
    root = Path(__file__).resolve().parents[1]
    auth_source = (root / "webui" / "static" / "app-auth.js").read_text(encoding="utf-8")
    activity_source = (root / "webui" / "static" / "app-activity.js").read_text(encoding="utf-8")

    assert "SMALL_OPERATION_STORAGE_KEY" in auth_source
    assert "sessionStorage.setItem(SMALL_OPERATION_STORAGE_KEY" in auth_source
    assert 'renderSmallOperations("device", "设备操作诊断")' in auth_source
    assert 'rememberSmallOperation("device", result' in auth_source
    assert 'renderSmallOperations("logs", "日志操作诊断")' in activity_source
    assert 'rememberSmallOperation("logs", res' in activity_source
    assert "renderOperationHistory(" in auth_source
    assert "完成契约" in activity_source
    assert "completion.tool_execution" in activity_source
    assert "completion.evidence_delivery" in activity_source
    assert "completion.outbound_delivery" in activity_source
