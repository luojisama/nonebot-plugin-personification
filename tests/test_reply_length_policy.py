from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module


policy = load_personification_module("plugin.personification.core.reply_length_policy")


def _plan(**values):
    defaults = {
        "tool_intent": (),
        "research_need": "none",
        "vision_need": "none",
        "output_mode": "chat_short",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_light_chat_uses_daily_limit_without_keyword_routing() -> None:
    result = policy.resolve_reply_length_policy(
        SimpleNamespace(personification_max_output_chars=0),
        turn_plan=_plan(),
        budget_profile=SimpleNamespace(mode="light_chat"),
    )

    assert result.mode == "chat"
    assert result.max_chars == 60
    assert result.reason == "light_chat_without_evidence"


def test_visual_media_uses_evidence_limit_even_without_tool_intent() -> None:
    result = policy.resolve_reply_length_policy(
        SimpleNamespace(personification_max_output_chars=0),
        turn_plan=_plan(),
        budget_profile=SimpleNamespace(mode="light_chat"),
        media_context={"videos": ["probe.mp4"]},
    )

    assert result.mode == "evidence"
    assert result.max_chars == 600
    assert result.reason == "media_evidence"


def test_tool_or_research_reply_over_sixty_is_kept_until_six_hundred() -> None:
    result = policy.resolve_reply_length_policy(
        SimpleNamespace(personification_max_output_chars=0),
        turn_plan=_plan(tool_intent=("lookup_web",)),
        budget_profile=SimpleNamespace(mode="research"),
    )

    assert result.mode == "evidence"
    assert result.max_chars == 600


def test_legacy_global_limit_remains_hard_ceiling_for_both_modes() -> None:
    config = SimpleNamespace(
        personification_max_output_chars=37,
        personification_chat_max_output_chars=60,
        personification_tool_max_output_chars=600,
    )

    chat = policy.resolve_reply_length_policy(config, turn_plan=_plan())
    evidence = policy.resolve_reply_length_policy(
        config,
        turn_plan=_plan(vision_need="required"),
        media_context={"images": ["image"]},
    )

    assert chat.max_chars == 37
    assert evidence.max_chars == 37
    assert chat.legacy_cap == evidence.legacy_cap == 37


def test_evidence_delivery_and_bypass_keep_existing_unlimited_behavior() -> None:
    config = SimpleNamespace(personification_max_output_chars=0)
    required = policy.resolve_reply_length_policy(
        config,
        turn_plan=_plan(vision_need="required"),
        evidence_delivery_required=True,
    )
    bypass = policy.resolve_reply_length_policy(config, turn_plan=_plan(), bypass_length_limits=True)

    assert required.mode == bypass.mode == "bypass"
    assert required.max_chars == bypass.max_chars == 0


def test_trace_contains_only_stable_length_metadata() -> None:
    result = policy.resolve_reply_length_policy(
        SimpleNamespace(personification_max_output_chars=0),
        turn_plan=_plan(vision_need="required"),
        media_context={"videos": ["opaque-reference"]},
    )
    trace = policy.render_reply_length_trace(result, before_chars=820, after_chars=600)

    assert trace == (
        "length_mode=evidence limit_chars=600 before_chars=820 after_chars=600 "
        "truncated=true reason=vision_need"
    )
    assert "opaque-reference" not in trace


def test_truncate_reply_text_prefers_punctuation_and_counts_one_turn() -> None:
    text = "第一句完整。第二句也很长，后面还有很多细节不能全部发送。"

    assert policy.truncate_reply_text(text, 8) == "第一句完整。"
    assert len(policy.truncate_reply_text(text, 10)) <= 10
    assert policy.truncate_reply_text(text, 0) == text
