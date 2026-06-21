from __future__ import annotations

from ._loader import load_personification_module

ranker = load_personification_module("plugin.personification.core.search_ranker")


def test_rank_memory_payload_prioritizes_structured_knowledge_types() -> None:
    base_payload = {
        "confidence": 0.5,
        "stability": 0.5,
        "salience": 0.5,
        "group_id": "g1",
    }

    plain = ranker.rank_memory_payload(
        dict(base_payload, memory_type="semantic"),
        query="角色偏好",
        base_score=0.3,
        requested_group_id="g1",
        requested_user_id="u1",
    )
    persona = ranker.rank_memory_payload(
        dict(base_payload, memory_type="persona_knowledge"),
        query="角色偏好",
        base_score=0.3,
        requested_group_id="g1",
        requested_user_id="u1",
    )
    group = ranker.rank_memory_payload(
        dict(base_payload, memory_type="group_knowledge"),
        query="群内说法",
        base_score=0.3,
        requested_group_id="g1",
        requested_user_id="u1",
    )

    assert persona > plain
    assert group > plain
