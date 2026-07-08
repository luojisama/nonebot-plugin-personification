from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime_with_memory(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=True,
    )
    data_store.init_data_store(cfg)

    memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
    store = memory_store_mod.MemoryStore(plugin_config=cfg, logger=SimpleNamespace(warning=lambda *_a, **_k: None))
    store.initialize()

    # 写入 5 条记忆：3 条 bot 自言（episodic / self_log）、2 条真正的群知识/事实
    store.write_memory_item({
        "memory_id": "mem-1",
        "memory_type": "episodic",
        "summary": "bot 回了一句嘲讽",
        "group_id": "g1",
        "source_kind": "self_log",
        "confidence": 0.9,
    })
    store.write_memory_item({
        "memory_id": "mem-2",
        "memory_type": "episodic",
        "summary": "bot 又回了一句",
        "group_id": "g1",
        "source_kind": "self_reply",
        "confidence": 0.8,
    })
    store.write_memory_item({
        "memory_id": "mem-3",
        "memory_type": "fact",
        "summary": "用户 A 喜欢东方系列",
        "group_id": "g1",
        "user_id": "A",
        "source_kind": "user_persona",
        "confidence": 0.7,
    })
    store.write_memory_item({
        "memory_id": "mem-4",
        "memory_type": "group_knowledge",
        "summary": "「肉鸽」= roguelike",
        "group_id": "g1",
        "source_kind": "auto_extract",
        "confidence": 0.8,
    })
    store.write_memory_item({
        "memory_id": "mem-5",
        "memory_type": "episodic",
        "summary": "bot 第三次自言",
        "group_id": "g1",
        "source_kind": "self_log",
        "confidence": 0.6,
    })

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"1": SimpleNamespace()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(memory_store=store, profile_service=None),
    )
    return SimpleNamespace(plugin_config=cfg, app_module=app_module, store=store)


def _build_client(rt):
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


def test_default_filter_hides_self_log_and_episodic(_runtime_with_memory) -> None:
    client = _build_client(_runtime_with_memory)
    _login(client, _runtime_with_memory)
    res = client.get("/personification/api/memory/recent?limit=40")
    assert res.status_code == 200
    body = res.json()
    assert body["include_self"] is False
    assert body["hidden_self_count"] == 3
    summaries = [it["summary"] for it in body["items"]]
    # 自言 3 条不在结果里
    assert all("bot " not in s and "bot 第" not in s for s in summaries), summaries
    # 真正的 2 条画像/知识在
    assert any("东方系列" in s for s in summaries)
    assert any("肉鸽" in s for s in summaries)
    fact = next(it for it in body["items"] if it["memory_id"] == "mem-3")
    assert fact["memory_type_label"] == "事实记忆"
    assert fact["source_kind_label"] == "用户画像"


def test_include_self_returns_all(_runtime_with_memory) -> None:
    client = _build_client(_runtime_with_memory)
    _login(client, _runtime_with_memory)
    res = client.get("/personification/api/memory/recent?limit=40&include_self=true")
    assert res.status_code == 200
    body = res.json()
    assert body["include_self"] is True
    assert body["hidden_self_count"] == 0
    assert len(body["items"]) == 5


def test_source_kind_filter(_runtime_with_memory) -> None:
    client = _build_client(_runtime_with_memory)
    _login(client, _runtime_with_memory)
    # 仅看 user_persona 来源
    res = client.get("/personification/api/memory/recent?limit=40&source_kind=user_persona")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["source_kind"] == "user_persona"
    assert items[0]["source_kind_label"] == "用户画像"


def test_memory_graph_returns_chinese_display_labels(_runtime_with_memory) -> None:
    client = _build_client(_runtime_with_memory)
    _login(client, _runtime_with_memory)
    res = client.get("/personification/api/memory/graph?group_id=g1&limit=20")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True
    node = next(n for n in body["nodes"] if n["id"] == "m:mem-3")
    assert node["kind_label"] == "记忆条目"
    assert node["memory_type_label"] == "事实记忆"


def test_raw_chat_requires_group_id(_runtime_with_memory) -> None:
    client = _build_client(_runtime_with_memory)
    _login(client, _runtime_with_memory)
    res = client.get("/personification/api/memory/raw-chat")
    assert res.status_code == 400


def test_raw_chat_reads_chat_history(_runtime_with_memory) -> None:
    store = _runtime_with_memory.store
    # 注入几条 chat_history.db 的消息
    store.append_group_message(
        group_id="g_raw",
        role="user",
        content={"text": "群友 A 发的话"},
        metadata={"user_id": "A", "nickname": "AA"},
        created_at=time.time() - 60,
    )
    store.append_group_message(
        group_id="g_raw",
        role="assistant",
        content={"text": "bot 的回复"},
        metadata={"user_id": "bot"},
        created_at=time.time(),
    )

    client = _build_client(_runtime_with_memory)
    _login(client, _runtime_with_memory)
    res = client.get("/personification/api/memory/raw-chat?group_id=g_raw&limit=10")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True
    assert len(body["messages"]) == 2
    roles = {m["role"] for m in body["messages"]}
    assert roles == {"user", "assistant"}
    # 时间倒序
    assert body["messages"][0]["created_at"] >= body["messages"][1]["created_at"]


def test_raw_chat_empty_group_returns_ok(_runtime_with_memory) -> None:
    client = _build_client(_runtime_with_memory)
    _login(client, _runtime_with_memory)
    res = client.get("/personification/api/memory/raw-chat?group_id=g_never_existed&limit=10")
    assert res.status_code == 200
    body = res.json()
    assert body["messages"] == []
