from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

memory_store = load_personification_module("plugin.personification.core.memory_store")


def test_pack_float16_vector_returns_two_bytes_per_value() -> None:
    payload = memory_store._pack_float16_vector([0.0, 1.0, -1.0])

    assert isinstance(payload, bytes)
    assert len(payload) == 6


def test_fuse_recall_candidates_uses_rrf_and_merges_reasons() -> None:
    candidate_a_fts = memory_store.MemorySearchCandidate(
        memory_id="m-a",
        payload={"memory_id": "m-a", "summary": "A"},
        base_score=0.5,
        match_reasons=["全文命中"],
        source="fts",
    )
    candidate_a_embedding = memory_store.MemorySearchCandidate(
        memory_id="m-a",
        payload={"memory_id": "m-a", "summary": "A"},
        base_score=0.45,
        match_reasons=["语义相近"],
        source="embedding",
    )
    candidate_b = memory_store.MemorySearchCandidate(
        memory_id="m-b",
        payload={"memory_id": "m-b", "summary": "B"},
        base_score=0.51,
        match_reasons=["实体命中"],
        source="entity",
    )

    fused = memory_store._fuse_recall_candidates(
        [[candidate_b], [candidate_a_fts], [candidate_a_embedding]],
        limit=2,
        rrf_k=1,
    )

    assert [item.memory_id for item in fused] == ["m-a", "m-b"]
    assert fused[0].source == "fts+embedding"
    assert "全文命中" in fused[0].match_reasons
    assert "语义相近" in fused[0].match_reasons
    assert any(reason.startswith("RRF混合召回") for reason in fused[0].match_reasons)


def test_memory_store_writes_vector_chunks_for_rag_recall(tmp_path) -> None:
    data_store = load_personification_module("plugin.personification.core.data_store")
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=True,
        personification_memory_rag_enabled=True,
        personification_memory_vector_backend="sqlite_exact",
        personification_memory_rag_candidate_limit=80,
        personification_memory_recall_top_k=8,
        personification_memory_search_scan_limit=300,
    )
    data_store.init_data_store(cfg)
    store = memory_store.MemoryStore(plugin_config=cfg, logger=None)
    store.initialize()

    store.write_memory_item(
        {
            "memory_id": "rag-memory",
            "memory_type": "semantic",
            "summary": "长期事实：用户喜欢月面基地模型和银色轨道车",
            "snippets": ["月面基地模型", "银色轨道车"],
            "user_id": "u1",
            "permission_type": "private_fact",
            "confidence": 0.95,
            "salience": 0.9,
        }
    )

    with memory_store._connect(tmp_path / "memory_palace" / "memory_palace.db") as conn:
        row = conn.execute(
            "SELECT COUNT(1) AS cnt FROM memory_vector_chunks WHERE memory_id='rag-memory'"
        ).fetchone()
    assert int(row["cnt"]) >= 1

    results = store.recall_memories(query="月面基地模型 银色轨道车", user_id="u1", context_type="private", limit=5)

    assert any(item.get("memory_id") == "rag-memory" for item in results)
    hit = next(item for item in results if item.get("memory_id") == "rag-memory")
    assert "vector" in str(hit.get("search_source", ""))
