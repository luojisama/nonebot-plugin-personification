from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module


projector = load_personification_module("plugin.personification.core.social_memory")


class _Store:
    plugin_config = SimpleNamespace(
        personification_social_memory_enabled=True,
        personification_social_memory_summary_ttl_days=14,
    )

    def __init__(self) -> None:
        self.items: list[dict] = []

    def palace_enabled(self) -> bool:
        return True

    def write_memory_item(self, item):  # noqa: ANN001
        self.items.append(dict(item))
        return str(item["memory_id"])


def _packet() -> str:
    return json.dumps(
        {
            "trust": "untrusted_data_only",
            "items": [
                {"platform": "bilibili", "content_id": "1", "content_type": "video", "source_group_id": "s1", "canonical_url": "https://www.bilibili.com/video/BV1", "detail_status": "ready", "caption_or_body": "第一条详情证据"},
                {"platform": "xiaoheihe", "content_id": "2", "content_type": "article", "source_group_id": "s2", "canonical_url": "https://xiaoheihe.cn/app/bbs/link/2", "detail_status": "ready", "caption_or_body": "第二条详情证据"},
                {"platform": "tieba", "content_id": "3", "content_type": "post", "source_group_id": "s3", "canonical_url": "https://tieba.baidu.com/p/3", "detail_status": "ready", "caption_or_body": "第三条详情证据"},
            ],
            "semantic_validation": {
                "status": "confirmed",
                "supporting_source_group_count": 3,
                "supporting_origins": ["bilibili", "xiaoheihe", "tieba"],
                "consensus_meaning": "这是多个渠道支持的同一梗义。",
                "consensus_sense_id": "sense_1",
            },
            "slang_claims": [
                {"source_cluster_id": "s1", "evidence_refs": [{"platform": "bilibili"}]},
                {"source_cluster_id": "s2", "evidence_refs": [{"platform": "xiaoheihe"}]},
                {"source_cluster_id": "s3", "evidence_refs": [{"platform": "tieba"}]},
            ],
        },
        ensure_ascii=False,
    )


def test_social_projection_writes_group_ttl_and_global_verified_bridge() -> None:
    store = _Store()
    result = asyncio.run(
        projector.project_social_evidence(
            memory_store=store,
            packet=_packet(),
            group_id="g1",
            user_id="u1",
            turn_plan=SimpleNamespace(research_need="deep", memory_need="deep"),
        )
    )
    assert result.status == "committed"
    assert len(store.items) == 4
    summaries = [item for item in store.items if item["memory_type"] != "concept_anchor"]
    assert all(item["group_id"] == "g1" for item in summaries)
    assert all(item["expires_at"] > 0 for item in summaries)
    bridge = next(item for item in store.items if item["memory_type"] == "concept_anchor")
    assert bridge["cross_group_allowed"] is True
    assert bridge["auto_context_eligible"] is True
    assert all("video" not in item and "audio" not in item for item in store.items)


def test_social_projection_does_not_write_search_cards_without_detail() -> None:
    store = _Store()
    packet = json.loads(_packet())
    for item in packet["items"]:
        item["detail_status"] = "detail_content_unavailable"
        item.pop("caption_or_body", None)
    result = asyncio.run(
        projector.project_social_evidence(
            memory_store=store,
            packet=packet,
            group_id="g1",
            user_id="u1",
            turn_plan=SimpleNamespace(research_need="deep", memory_need="deep"),
        )
    )
    assert result.status == "skipped"
    assert result.diagnostic_codes == ["social_memory_skipped_no_detail"]
    assert store.items == []


def test_social_projection_does_not_count_unrelated_search_hits_as_support() -> None:
    store = _Store()
    packet = json.loads(_packet())
    packet["semantic_validation"]["supporting_source_group_count"] = 2
    packet["semantic_validation"]["supporting_origins"] = ["bilibili", "tieba"]
    packet["slang_claims"] = packet["slang_claims"][:2]
    result = asyncio.run(
        projector.project_social_evidence(
            memory_store=store,
            packet=packet,
            group_id="g1",
            user_id="u1",
            turn_plan=SimpleNamespace(research_need="deep", memory_need="deep"),
        )
    )
    assert result.status == "candidate"
    assert not any(item["memory_type"] == "concept_anchor" for item in store.items)
    assert all(item["auto_context_eligible"] is False for item in store.items)
