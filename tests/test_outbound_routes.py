from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
qq_outbound = load_personification_module(
    "plugin.personification.core.qq_outbound"
)
qq_recall = load_personification_module("plugin.personification.core.qq_recall")
route_mod = load_personification_module(
    "plugin.personification.webui.routes.outbound_routes"
)
schemas = load_personification_module("plugin.personification.webui.schemas")


def _seed(ledger) -> None:  # noqa: ANN001
    asyncio.run(
        ledger.dispatch(
            qq_outbound.OutboundContext(
                operation_id="route-operation",
                bot_id="90001",
                conversation_kind="group",
                conversation_id="20001",
                user_target="10001",
                surface="normal_reply",
            ),
            "hello token=secret https://secret.example/path",
            lambda: {"message_id": 123},
        )
    )


def _client(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    ledger = qq_outbound.QQOutboundLedger(
        db.init_db_sync(tmp_path),
        content_hmac_key=b"k" * 32,
    )
    _seed(ledger)
    bot = SimpleNamespace(self_id="90001")
    runtime = SimpleNamespace(
        runtime_bundle=SimpleNamespace(
            qq_outbound_ledger=ledger,
            get_bots=lambda: {"90001": bot},
        ),
        get_bots=lambda: {"90001": bot},
        plugin_config=SimpleNamespace(),
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )
    captured: dict = {}
    captured["audits"] = []
    monkeypatch.setattr(
        route_mod.webui_audit_log,
        "record",
        lambda **kwargs: captured["audits"].append(kwargs),
    )

    class _RecallService:
        def __init__(self, received_ledger, **kwargs):  # noqa: ANN001, ANN003
            captured["ledger"] = received_ledger
            captured["init"] = kwargs

        async def recall_operation(self, **kwargs):  # noqa: ANN003, ANN202
            captured["call"] = kwargs
            return qq_recall.QQRecallResult(
                "succeeded",
                "ok",
                outbound_operation_id="route-operation",
                total_count=1,
                recalled_count=1,
            )

    monkeypatch.setattr(route_mod, "QQRecallService", _RecallService)
    app = FastAPI()
    app.include_router(route_mod.build_outbound_router(runtime=runtime))
    app.dependency_overrides[route_mod.require_admin] = lambda: schemas.AdminIdentity(
        qq="80001",
        device_id="device",
        label="test",
    )
    return TestClient(app), ledger, captured


def test_recent_outbound_route_only_returns_redacted_ledger_fields(tmp_path, monkeypatch) -> None:
    client, _ledger, _captured = _client(tmp_path, monkeypatch)

    response = client.get(
        "/api/outbound/recent?bot_id=90001&conversation_kind=group&conversation_id=20001&status=sent&recalled=false"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    rows = response.json()["messages"]
    assert len(rows) == 1
    assert rows[0]["operation_id"] == "route-operation"
    assert rows[0]["message_id"] == "123"
    assert "secret.example" not in rows[0]["preview"]
    assert "content_hmac" not in rows[0]
    assert "user_target" not in rows[0]


def test_admin_recall_route_requires_exact_confirmation_and_scope(tmp_path, monkeypatch) -> None:
    client, ledger, captured = _client(tmp_path, monkeypatch)
    body = {
        "bot_id": "90001",
        "conversation_kind": "group",
        "conversation_id": "20001",
        "confirmation": "wrong",
    }
    denied = client.post("/api/outbound/route-operation/recall", json=body)
    assert denied.status_code == 400
    assert "call" not in captured
    assert captured["audits"][-1]["outcome"] == "denied"
    assert captured["audits"][-1]["detail"]["code"] == "confirmation_mismatch"

    body["confirmation"] = "RECALL route-operation"
    response = client.post("/api/outbound/route-operation/recall", json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["diagnostic"]["ok"] is True
    assert payload["diagnostic"]["retryable"] is False
    assert response.headers["cache-control"] == "no-store, private"
    assert captured["ledger"] is ledger
    assert captured["call"]["operation_id"] == "route-operation"
    assert captured["call"]["conversation_kind"] == "group"
    assert captured["call"]["conversation_id"] == "20001"
    assert captured["call"]["requester_user_id"] == "80001"
    assert "message_id" not in captured["call"]

    offline = client.post(
        "/api/outbound/route-operation/recall",
        json={**body, "bot_id": "90002"},
    )
    assert offline.status_code == 404
    assert captured["audits"][-1]["outcome"] == "denied"
    assert captured["audits"][-1]["detail"]["code"] == "bot_unavailable"
