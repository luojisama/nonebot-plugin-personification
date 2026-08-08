from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime_with_style(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=True,
    )
    data_store.init_data_store(cfg)
    return SimpleNamespace(plugin_config=cfg, tmp_path=tmp_path)


def test_init_db_migrates_old_group_style_schema(tmp_path: Path) -> None:
    """旧版 group_style_snapshots 以 group_id 为 PK 单行；
    init_db_sync 在升级时应自动 DROP 重建，避免 CREATE INDEX(created_at) 失败。
    """
    import sqlite3

    db_mod = load_personification_module("plugin.personification.core.db")
    db_path = tmp_path / db_mod.DB_FILENAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 模拟旧 schema：group_id 为 PK，没有 id/created_at
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE group_style_snapshots (
                group_id TEXT PRIMARY KEY,
                style_text TEXT NOT NULL DEFAULT '',
                style_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()

    # 升级路径：不应抛 "no such column: created_at"
    db_mod.init_db_sync(tmp_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(group_style_snapshots)").fetchall()
        cols = [r[1] for r in rows]
        assert "id" in cols
        assert "created_at" in cols
        idx_names = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='group_style_snapshots'"
            ).fetchall()
        ]
        assert "idx_group_style_gid_ts" in idx_names


def test_save_snapshot_caps_at_keep_n(_runtime_with_style) -> None:
    style_mod = load_personification_module("plugin.personification.core.group_style_autobuild")
    for i in range(5):
        style_mod._save_snapshot(
            "g1",
            f"风格快照#{i}",
            {"tone": f"t{i}"},
            keep=3,
        )
        time.sleep(0.01)
    snaps = style_mod.list_style_snapshots("g1", limit=10)
    assert len(snaps) == 3
    # 时序倒序——最新的 #4 在最前
    assert snaps[0]["style_text"] == "风格快照#4"
    assert snaps[2]["style_text"] == "风格快照#2"


def test_latest_style_text_returns_most_recent(_runtime_with_style) -> None:
    style_mod = load_personification_module("plugin.personification.core.group_style_autobuild")
    style_mod._save_snapshot("g2", "first", {"tone": "calm"})
    time.sleep(0.02)
    style_mod._save_snapshot("g2", "second", {"tone": "wild"})
    assert style_mod.get_latest_style_text("g2") == "second"


def test_style_to_text_renders_segments(_runtime_with_style) -> None:
    style_mod = load_personification_module("plugin.personification.core.group_style_autobuild")
    text = style_mod._style_to_text({
        "tone": "调皮活泼",
        "pace": "快节奏",
        "catchphrases": ["哈哈", "草", "笑死"],
        "taboos": ["政治"],
        "typical_length": "短句为主",
    })
    assert "语气：调皮活泼" in text
    assert "口头禅：哈哈、草、笑死" in text
    assert "禁忌：政治" in text


def test_build_group_style_via_mocked_caller(_runtime_with_style) -> None:
    style_mod = load_personification_module("plugin.personification.core.group_style_autobuild")

    class _MockResponse:
        content = '{"tone":"放松","pace":"中等","catchphrases":["awsl"],"taboos":[],"typical_length":"短"}'

    class _MockCaller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            return _MockResponse()

    out = asyncio.run(style_mod.build_group_style(
        tool_caller=_MockCaller(),
        memory_store=SimpleNamespace(),
        group_id="g3",
        chat_summary="一些对话片段...",
    ))
    assert out["style_json"]["tone"] == "放松"
    assert "awsl" in out["style_text"]
    snaps = style_mod.list_style_snapshots("g3", limit=5)
    assert len(snaps) == 1


def test_build_group_style_handles_invalid_json(_runtime_with_style) -> None:
    style_mod = load_personification_module("plugin.personification.core.group_style_autobuild")

    class _BadResponse:
        content = "这不是 JSON"

    class _BadCaller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            return _BadResponse()

    out = asyncio.run(style_mod.build_group_style(
        tool_caller=_BadCaller(),
        memory_store=SimpleNamespace(),
        group_id="g4",
        chat_summary="对话",
    ))
    assert out == {}


def test_group_style_source_messages_skip_bot_replies(_runtime_with_style) -> None:
    memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
    group_knowledge_mod = load_personification_module("plugin.personification.core.group_knowledge_autobuild")
    store = memory_store_mod.init_memory_store(_runtime_with_style.plugin_config)
    store.append_group_message(
        group_id="g_skip_bot",
        role="assistant",
        content="等下，这也太模板了吧",
        metadata={"source_kind": "bot_reply", "user_id": "bot"},
    )
    store.append_group_message(
        group_id="g_skip_bot",
        role="user",
        content="今天要打活动",
        metadata={"source_kind": "user", "user_id": "u1"},
    )

    rows = group_knowledge_mod._load_messages_since(
        memory_store=store,
        group_id="g_skip_bot",
        since_ts=0,
        limit=20,
    )

    assert [row["text"] for row in rows] == ["今天要打活动"]


# ---- WebUI 集成：style 端点 + rebuild ----


@pytest.fixture
def _webui_runtime_with_style(tmp_path: Path, monkeypatch):
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
    # 注入足够的群对话样本
    for i in range(40):
        store.append_group_message(
            group_id="g_style",
            role="user" if i % 2 == 0 else "assistant",
            content={"text": f"消息 #{i}"},
            metadata={"user_id": f"u{i%5}"},
            created_at=time.time() - (40 - i),
        )

    class _StyleMockCaller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            return SimpleNamespace(content='{"tone":"专业冷静","pace":"中","catchphrases":["确实"],"taboos":[],"typical_length":"中等"}')

    runtime_bundle = SimpleNamespace(
        memory_store=store,
        reply_processor_deps=SimpleNamespace(runtime=SimpleNamespace(agent_tool_caller=_StyleMockCaller())),
        profile_service=None,
    )

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"1": SimpleNamespace()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=runtime_bundle,
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


def test_style_endpoint_returns_snapshots_list(_webui_runtime_with_style) -> None:
    style_mod = load_personification_module("plugin.personification.core.group_style_autobuild")
    style_mod._save_snapshot("g_style", "snap_a", {"tone": "a"})
    time.sleep(0.01)
    style_mod._save_snapshot("g_style", "snap_b", {"tone": "b"})

    client = _build_client(_webui_runtime_with_style)
    _login(client, _webui_runtime_with_style)
    res = client.get("/personification/api/groups/g_style/style")
    assert res.status_code == 200
    body = res.json()
    assert len(body["snapshots"]) == 2
    assert body["style_text"] == "snap_b"  # 最新
    assert body["snapshots"][0]["style_text"] == "snap_b"


def test_style_rebuild_writes_new_snapshot(_webui_runtime_with_style) -> None:
    client = _build_client(_webui_runtime_with_style)
    _login(client, _webui_runtime_with_style)
    res = client.post("/personification/api/groups/g_style/style/rebuild", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["new_snapshot"]["style_json"]["tone"] == "专业冷静"
    assert len(body["snapshots"]) >= 1


def test_style_rebuild_rejects_insufficient_messages(_webui_runtime_with_style) -> None:
    # 用一个没消息的群
    client = _build_client(_webui_runtime_with_style)
    _login(client, _webui_runtime_with_style)
    res = client.post("/personification/api/groups/g_empty/style/rebuild", json={})
    assert res.status_code == 400
    report = res.json()["detail"]
    assert report["code"] == "group_style_sample_insufficient"
    assert report["phase"] == "source"
    assert report["retryable"] is False
    assert report["partial"] is False
    assert report["outcome_unknown"] is False


def test_style_rebuild_rate_limit(_webui_runtime_with_style) -> None:
    client = _build_client(_webui_runtime_with_style)
    _login(client, _webui_runtime_with_style)
    # 同设备 5 分钟内最多 3 次，第 4 次应该被拒
    for _ in range(3):
        res = client.post("/personification/api/groups/g_style/style/rebuild", json={})
        assert res.status_code == 200, res.text
    res4 = client.post("/personification/api/groups/g_style/style/rebuild", json={})
    assert res4.status_code == 429
