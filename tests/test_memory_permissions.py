from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _store(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=True,
        personification_memory_recall_top_k=8,
        personification_memory_search_scan_limit=300,
    )
    data_store.init_data_store(cfg)
    memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
    store = memory_store_mod.MemoryStore(plugin_config=cfg, logger=SimpleNamespace(warning=lambda *_a, **_k: None))
    store.initialize()
    return store


def test_group_recall_filters_private_and_sensitive_memories(_store) -> None:
    _store.write_memory_item(
        {
            "memory_id": "public",
            "memory_type": "fact",
            "summary": "公开偏好：喜欢塔防游戏",
            "group_id": "g1",
            "permission_type": "public_preference",
            "confidence": 0.9,
        }
    )
    _store.write_memory_item(
        {
            "memory_id": "private",
            "memory_type": "fact",
            "summary": "私人事实：真实住址在测试路",
            "group_id": "g1",
            "permission_type": "private_fact",
            "confidence": 0.9,
        }
    )
    _store.write_memory_item(
        {
            "memory_id": "sensitive",
            "memory_type": "fact",
            "summary": "敏感事实：测试病史",
            "group_id": "g1",
            "permission_type": "sensitive_memory",
            "confidence": 0.9,
        }
    )

    results = _store.recall_memories(query="测试 偏好 住址 病史 塔防", group_id="g1", context_type="group")
    summaries = [item["summary"] for item in results]

    assert any("塔防" in summary for summary in summaries)
    assert all("住址" not in summary for summary in summaries)
    assert all("病史" not in summary for summary in summaries)


def test_group_recall_redacts_conflict_memory(_store) -> None:
    _store.write_memory_item(
        {
            "memory_id": "conflict",
            "memory_type": "conflict_memory",
            "summary": "冲突避雷：不要复述具体争吵细节",
            "group_id": "g1",
            "permission_type": "conflict_memory",
            "confidence": 0.9,
        }
    )

    results = _store.recall_memories(query="争吵 避雷", group_id="g1", context_type="group")

    assert results
    assert results[0]["permission_type"] == "conflict_memory"
    assert "中性克制" in results[0]["summary"]
    assert "不要复述具体争吵细节" not in results[0]["summary"]


def test_private_recall_can_return_private_fact(_store) -> None:
    _store.write_memory_item(
        {
            "memory_id": "private",
            "memory_type": "fact",
            "summary": "私人事实：喜欢深夜整理代码",
            "user_id": "u1",
            "permission_type": "private_fact",
            "confidence": 0.9,
        }
    )

    results = _store.recall_memories(query="深夜 整理代码", user_id="u1", context_type="private")

    assert any("深夜整理代码" in item["summary"] for item in results)


def test_recall_can_find_old_semantic_memory_beyond_recent_window(_store) -> None:
    import time

    _store.write_memory_item(
        {
            "memory_id": "old-semantic",
            "memory_type": "semantic",
            "summary": "长期事实：用户喜欢蓝色列车模型",
            "user_id": "u1",
            "permission_type": "private_fact",
            "confidence": 0.95,
            "salience": 0.95,
            "time_created": time.time() - 86400 * 60,
        }
    )
    for idx in range(150):
        _store.write_memory_item(
            {
                "memory_id": f"recent-{idx}",
                "memory_type": "episodic_turn",
                "summary": f"普通最近对话片段 {idx}",
                "user_id": "u1",
                "permission_type": "private_fact",
                "confidence": 0.55,
                "salience": 0.35,
            }
        )

    results = _store.recall_memories(
        query="蓝色列车模型",
        user_id="u1",
        context_type="private",
        limit=12,
    )

    assert any(item.get("memory_id") == "old-semantic" for item in results)


def test_group_recall_does_not_cross_isolated_group_scope(_store) -> None:
    _store.write_memory_item(
        {
            "memory_id": "g2-only",
            "memory_type": "group_knowledge",
            "summary": "群二专属暗号：蓝色列车",
            "group_id": "g2",
            "permission_type": "group_meme",
            "confidence": 0.95,
            "salience": 0.95,
            "cross_group_allowed": False,
        }
    )

    results = _store.recall_memories(query="蓝色列车", group_id="g1", context_type="group")

    assert all(item.get("memory_id") != "g2-only" for item in results)
