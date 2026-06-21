from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"1": SimpleNamespace()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(memory_store=None, profile_service=None),
    )
    return SimpleNamespace(app_module=app_module)


def _client(rt):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(rt.app_module.build_router())
    return TestClient(app)


def _login(client, rt) -> None:
    sent: list = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {"message_id": 1}

    rt.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    res = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert res.status_code == 200, res.text
    code = re.search(r"\b(\d{6})\b", str(sent[-1].get("message", ""))).group(1)
    res2 = client.post("/personification/api/auth/verify", json={"qq": "10001", "code": code, "device_label": "t"})
    assert res2.status_code == 200, res2.text
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def test_group_meme_routes_upsert_list_delete(_runtime) -> None:
    client = _client(_runtime)
    _login(client, _runtime)

    res = client.post(
        "/personification/api/groups/g1/memes",
        json={"term": "猫车", "meaning": "群里指测试车翻车", "aliases": ["上猫车"], "confidence": 0.8},
    )
    assert res.status_code == 200, res.text

    listed = client.get("/personification/api/groups/g1/memes")
    assert listed.status_code == 200
    terms = {item["term"] for item in listed.json()["memes"]}
    assert "猫车" in terms

    deleted = client.delete("/personification/api/groups/g1/memes/%E7%8C%AB%E8%BD%A6")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
