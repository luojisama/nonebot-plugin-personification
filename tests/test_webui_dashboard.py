from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime_with_data(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)

    ledger = load_personification_module("plugin.personification.core.token_ledger")
    ledger.record_llm_call(model="gpt-x", prompt_tokens=100, completion_tokens=50, group_id="g1")
    ledger.record_llm_call(model="gpt-x", prompt_tokens=200, completion_tokens=80, group_id="g2")
    ledger.record_llm_call(model="gpt-y", prompt_tokens=30, completion_tokens=10, group_id="g1")

    # 准备一份全局画像和一份群内画像
    memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
    store = memory_store_mod.MemoryStore(plugin_config=cfg, logger=SimpleNamespace(warning=lambda *_a, **_k: None))
    store.initialize()
    store.upsert_core_profile(user_id="u_alpha", profile_text="全局画像 Alpha")
    store.upsert_local_profile(group_id="g1", user_id="u_alpha", profile_text="g1 中是常驻成员")

    profile_service_mod = load_personification_module("plugin.personification.core.profile_service")
    profile_service = profile_service_mod.ProfileService(memory_store=store)

    runtime_bundle = SimpleNamespace(profile_service=profile_service, memory_store=store)

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"1": SimpleNamespace()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=runtime_bundle,
    )
    return SimpleNamespace(plugin_config=cfg, app_module=app_module, runtime_bundle=runtime_bundle, store=store, ledger=ledger)


def _build_client(runtime_context):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(runtime_context.app_module.build_router())
    return TestClient(app)


def _login(client) -> None:
    sent: list = []

    class _Bot:
        async def call_api(self, _name: str, **kwargs):
            sent.append(kwargs)
            return {"message_id": 1}

    runtime = client.app  # not used
    # 重新绑定 get_bots 让 verify code 能送达
    app_module = load_personification_module("plugin.personification.webui.app")
    ctx = app_module.get_runtime_context()
    ctx.get_bots = lambda: {"1": _Bot()}
    res = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert res.status_code == 200, res.text
    code = re.search(r"\b(\d{6})\b", str(sent[-1].get("message", ""))).group(1)
    res2 = client.post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": code, "device_label": "测试"},
    )
    assert res2.status_code == 200, res2.text


def test_dashboard_metrics_returns_token_summary(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/metrics/summary?window=month")
    assert res.status_code == 200
    body = res.json()
    assert body["total"]["total_tokens"] == 100 + 50 + 200 + 80 + 30 + 10
    assert body["total"]["call_count"] == 3
    models = {row["model"]: row for row in body["by_model"]}
    assert models["gpt-x"]["total_tokens"] == 430
    assert models["gpt-y"]["total_tokens"] == 40
    groups = {row["group_id"]: row for row in body["by_group"]}
    assert groups["g1"]["total_tokens"] == 100 + 50 + 30 + 10
    assert groups["g2"]["total_tokens"] == 280


def test_dashboard_window_validation(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/metrics/summary?window=year")
    assert res.status_code == 422


def test_personas_list_and_detail(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/personas")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert any(p["user_id"] == "u_alpha" for p in body["profiles"])

    res2 = client.get("/personification/api/personas/u_alpha")
    assert res2.status_code == 200
    detail = res2.json()
    assert detail["core_profile"]["profile_text"] == "全局画像 Alpha"
    assert len(detail["local_profiles"]) == 1
    assert detail["local_profiles"][0]["group_id"] == "g1"


def test_groups_list_and_detail(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/groups")
    assert res.status_code == 200
    body = res.json()
    assert any(group["group_id"] == "g1" for group in body["groups"])

    res2 = client.get("/personification/api/groups/g1/personas")
    assert res2.status_code == 200
    detail = res2.json()
    assert any(p["user_id"] == "u_alpha" for p in detail["profiles"])

    res3 = client.get("/personification/api/groups/g1/style")
    assert res3.status_code == 200
    style = res3.json()
    assert style["group_id"] == "g1"
    assert style["style_text"] == ""  # 暂未写入


def test_profile_service_prompt_block(_runtime_with_data) -> None:
    svc = _runtime_with_data.runtime_bundle.profile_service
    block = svc.build_prompt_block(user_id="u_alpha", group_id="g1")
    assert "## 用户档案" in block
    assert "全局画像 Alpha" in block
    assert "g1 中是常驻成员" in block

    block2 = svc.build_prompt_block(user_id="u_missing")
    assert block2 == ""
