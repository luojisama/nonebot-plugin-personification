from __future__ import annotations

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
evidence = load_personification_module(
    "plugin.personification.core.avatar_relation_evidence"
)


def test_avatar_relation_evidence_is_canonical_bounded_and_deletable(tmp_path) -> None:
    db_path = db.init_db_sync(tmp_path)
    stored = evidence.record_avatar_relation_evidence(
        group_id="20001",
        left_user_id="10002",
        right_user_id="10001",
        relation="coordinated_pair",
        confidence=0.91,
        evidence_tags=["matching_palette", "complementary_composition"],
        asset_kinds=["illustration", "acg_character"],
        avatar_hashes={"10001": "a" * 64, "10002": "b" * 64},
        observed_at=100,
        ttl_seconds=1000,
        db_path=db_path,
    )

    assert stored is True
    rows = evidence.list_avatar_relation_evidence(
        "20001", now=101, db_path=db_path
    )
    assert len(rows) == 1
    assert rows[0]["left_user_id"] == "10001"
    assert rows[0]["right_user_id"] == "10002"
    assert rows[0]["relation"] == "coordinated_pair"
    assert rows[0]["evidence_tags"] == [
        "matching_palette",
        "complementary_composition",
    ]
    assert "avatar_hash" not in str(rows)
    assert evidence.delete_user_avatar_relation_evidence(
        "10001", db_path=db_path
    ) == 1
    assert evidence.list_avatar_relation_evidence(
        "20001", now=101, db_path=db_path
    ) == []


def test_avatar_relation_evidence_rejects_invalid_scope_or_hash(tmp_path) -> None:
    db_path = db.init_db_sync(tmp_path)
    common = {
        "group_id": "20001",
        "left_user_id": "10001",
        "right_user_id": "10002",
        "relation": "coordinated_pair",
        "confidence": 0.9,
        "evidence_tags": ["matching_palette"],
        "asset_kinds": ["illustration", "illustration"],
        "avatar_hashes": {"10001": "a" * 64, "10002": "bad"},
        "db_path": db_path,
    }
    assert evidence.record_avatar_relation_evidence(**common) is False
    assert evidence.record_avatar_relation_evidence(
        **{**common, "left_user_id": "10002", "avatar_hashes": {"10002": "a" * 64}}
    ) is False
