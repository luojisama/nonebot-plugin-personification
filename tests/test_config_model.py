from __future__ import annotations

import warnings

from ._loader import load_personification_module

config_mod = load_personification_module("plugin.personification.config")


def test_config_defaults_do_not_emit_deprecated_web_search_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config_mod.Config()

    assert not [
        item
        for item in caught
        if issubclass(item.category, DeprecationWarning)
        and "personification_web_search" in str(item.message)
    ]


def test_qzone_cookie_deprecated_alias_copies_to_prefixed_field() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = config_mod.Config(qzone_cookie="uin=o123; skey=abc")

    assert cfg.personification_qzone_cookie == "uin=o123; skey=abc"
    assert any(
        issubclass(item.category, DeprecationWarning)
        and "qzone_cookie" in str(item.message)
        for item in caught
    )


def test_llm_review_features_default_disabled() -> None:
    cfg = config_mod.Config()

    assert cfg.personification_core_values_enabled is True
    assert "公共安全" in cfg.personification_core_values_prompt
    assert "不要长篇说教" in cfg.personification_core_values_prompt
    assert cfg.personification_response_review_enabled is False
    assert cfg.personification_turn_planner_enabled is False
    assert cfg.personification_turn_planner_shadow_enabled is False
    assert cfg.personification_evidence_synthesizer_enabled is False
    assert cfg.personification_persona_responder_json_enabled is False
    assert cfg.personification_real_embedding_enabled is False
    assert cfg.personification_embedding_provider == "hash_bow"
    assert cfg.personification_embedding_model == ""
    assert cfg.personification_memory_vector_backend == "sqlite_exact"
    assert cfg.personification_memory_rag_enabled is True
    assert cfg.personification_memory_rag_candidate_limit == 80
    assert cfg.personification_deep_research_v2_enabled is False
    assert cfg.personification_parallel_research_pages_per_worker == 20
    assert cfg.personification_tts_llm_decision_enabled is False
    assert cfg.personification_response_review_model_role == "review"
    assert cfg.personification_tts_llm_decision_model_role == "agent"
    assert cfg.personification_strict_main_model is False


def test_qzone_social_and_frequency_defaults() -> None:
    cfg = config_mod.Config()

    assert cfg.personification_qzone_check_interval == 30
    assert cfg.personification_qzone_proactive_enabled is True
    assert cfg.personification_qzone_monthly_limit == 30
    assert cfg.personification_qzone_min_interval_hours == 6.0
    assert cfg.personification_qzone_social_enabled is True
    assert cfg.personification_qzone_social_check_interval == 30
    assert cfg.personification_qzone_social_scope == "recent_interactions"
    assert cfg.personification_qzone_social_like_limit == 0
    assert cfg.personification_qzone_social_comment_limit == 0
    assert cfg.personification_qzone_social_max_feeds_per_scan == 5
    assert cfg.personification_qzone_third_party_chime_in_enabled is True
    assert cfg.personification_qzone_inbound_enabled is True
    assert cfg.personification_qzone_inbound_check_interval == 3
    assert cfg.personification_qzone_inbound_max_feeds_per_scan == 20
    assert cfg.personification_qzone_inbound_max_comments_per_feed == 20
    assert cfg.personification_qzone_outbound_reply_enabled is True
    assert cfg.personification_qzone_outbound_reply_check_interval == 3
    assert cfg.personification_proactive_enabled is True
    assert cfg.personification_proactive_require_user_profile is True
