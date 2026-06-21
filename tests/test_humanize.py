from __future__ import annotations

import random
from types import SimpleNamespace

from ._loader import load_personification_module

humanize = load_personification_module("plugin.personification.handlers.reply_pipeline.humanize")


def _config(**overrides):
    defaults = {
        "personification_humanize_typing_enabled": True,
        "personification_humanize_quote_reply_enabled": True,
        "personification_humanize_quote_reply_min_gap": 4,
        "personification_humanize_at_enabled": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _event(message_id=100, user_id="10001"):
    return SimpleNamespace(message_id=message_id, user_id=user_id)


def setup_function(_fn) -> None:
    humanize.reset_humanize_state()


# ──────────────────────── 打字延迟 ────────────────────────

def test_typing_delay_subtracts_llm_elapsed() -> None:
    rng = random.Random(42)
    slow = humanize.compute_typing_delay("一二三四五六七八九十" * 3, cps=7, max_delay=10, already_elapsed=0.0, rng=rng)
    rng = random.Random(42)
    fast = humanize.compute_typing_delay("一二三四五六七八九十" * 3, cps=7, max_delay=10, already_elapsed=3.0, rng=rng)
    assert slow - fast == 3.0


def test_typing_delay_clamped_to_max() -> None:
    delay = humanize.compute_typing_delay("长" * 500, cps=5, max_delay=5.0, already_elapsed=0.0)
    assert delay == 5.0


def test_typing_delay_zero_when_llm_already_slow() -> None:
    delay = humanize.compute_typing_delay("短句", cps=7, max_delay=5.0, already_elapsed=60.0)
    assert delay == 0.0


def test_typing_delay_night_factor_increases() -> None:
    rng_day = random.Random(7)
    rng_night = random.Random(7)
    day = humanize.compute_typing_delay("正常长度的一句话啊", cps=7, max_delay=30, already_elapsed=0.0, night=False, rng=rng_day)
    night = humanize.compute_typing_delay("正常长度的一句话啊", cps=7, max_delay=30, already_elapsed=0.0, night=True, rng=rng_night)
    assert night > day


def test_gap_delay_within_bounds() -> None:
    delay = humanize.compute_gap_delay("下一段内容比较短", cps=7, max_delay=5.0)
    assert 0.6 <= delay <= 5.0


# ──────────────────────── 错别字 ────────────────────────

def test_typo_injected_deterministically() -> None:
    rng = random.Random(1)
    mutated, correction = humanize.maybe_inject_typo("我觉得他说的有道理", probability=1.0, rng=rng)
    assert correction is not None and correction.startswith("*")
    assert mutated != "我觉得他说的有道理"
    assert len(mutated) == len("我觉得他说的有道理")


def test_typo_skips_explanatory_unsafe_text() -> None:
    rng = random.Random(1)
    for text in ("访问 https://a.com 看的吧", "id是10086的那个", "短的话", "这" * 50):
        mutated, correction = humanize.maybe_inject_typo(text, probability=1.0, rng=rng)
        assert mutated == text and correction is None


def test_typo_zero_probability_never_fires() -> None:
    rng = random.Random(1)
    mutated, correction = humanize.maybe_inject_typo("我觉得他说的有道理", probability=0.0, rng=rng)
    assert mutated == "我觉得他说的有道理" and correction is None


# ──────────────────────── 引用决策 ────────────────────────

def test_quote_on_newer_batch() -> None:
    mid = humanize.should_quote_reply(
        plugin_config=_config(),
        state={},
        event=_event(message_id=55),
        group_id="g1",
        user_id="u1",
        is_private=False,
        has_newer_batch=True,
    )
    assert mid == 55


def test_quote_requires_min_gap_in_batch() -> None:
    target = _event(message_id=1, user_id="u1")
    batch_small = {"batched_events": [target, _event(2), _event(3)]}
    assert humanize.should_quote_reply(
        plugin_config=_config(),
        state=batch_small,
        event=target,
        group_id="g1",
        user_id="u1",
        is_private=False,
        has_newer_batch=False,
    ) is None
    batch_big = {"batched_events": [target] + [_event(i) for i in range(2, 7)]}
    assert humanize.should_quote_reply(
        plugin_config=_config(),
        state=batch_big,
        event=target,
        group_id="g2",
        user_id="u1",
        is_private=False,
        has_newer_batch=False,
    ) == 1


def test_quote_skipped_in_private_and_dedups_same_user() -> None:
    assert humanize.should_quote_reply(
        plugin_config=_config(),
        state={},
        event=_event(),
        group_id="private_u1",
        user_id="u1",
        is_private=True,
        has_newer_batch=True,
    ) is None
    now = [1000.0]
    kwargs = dict(
        plugin_config=_config(),
        state={},
        group_id="g1",
        user_id="u1",
        is_private=False,
        has_newer_batch=True,
        now_fn=lambda: now[0],
    )
    assert humanize.should_quote_reply(event=_event(1), **kwargs) == 1
    # 同一用户 10 分钟内不再引用
    assert humanize.should_quote_reply(event=_event(2), **kwargs) is None
    now[0] += 700
    assert humanize.should_quote_reply(event=_event(3), **kwargs) == 3


# ──────────────────────── @ 决策 ────────────────────────

def test_at_target_when_last_speaker_differs() -> None:
    state = {"batched_events": [_event(1, "u1"), _event(2, "u2")]}
    assert humanize.should_at_target(
        plugin_config=_config(),
        state=state,
        event=_event(1, "u1"),
        user_id="u1",
        is_private=False,
        quote_message_id=None,
    ) == "u1"


def test_at_skipped_when_quoting_or_single_speaker() -> None:
    state = {"batched_events": [_event(1, "u1"), _event(2, "u2")]}
    assert humanize.should_at_target(
        plugin_config=_config(),
        state=state,
        event=_event(1, "u1"),
        user_id="u1",
        is_private=False,
        quote_message_id=1,
    ) is None
    solo = {"batched_events": [_event(1, "u1"), _event(2, "u1")]}
    assert humanize.should_at_target(
        plugin_config=_config(),
        state=solo,
        event=_event(1, "u1"),
        user_id="u1",
        is_private=False,
        quote_message_id=None,
    ) is None
