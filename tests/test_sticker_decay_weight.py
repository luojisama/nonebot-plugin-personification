"""表情包衰减权重：刚发过的 1 小时内压制，1-24 小时线性恢复，24+ 完全放开。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from ._loader import load_personification_module


sf = load_personification_module("plugin.personification.core.sticker_feedback")


def _state_with_last_sent(name: str, hours_ago: float) -> dict:
    last_sent = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "items": {
            name: {
                "sent_count": 1,
                "positive_count": 0,
                "last_sent_at": last_sent,
            },
        },
    }


def test_decay_floor_under_one_hour() -> None:
    state = _state_with_last_sent("sticker_a", hours_ago=0.5)
    mult = sf.get_sticker_decay_multiplier("sticker_a", state)
    assert mult == 0.3, f"30 分钟前发过的应压制到 0.3，得到 {mult}"


def test_decay_linear_recovery_at_mid_window() -> None:
    state = _state_with_last_sent("sticker_b", hours_ago=12.5)
    mult = sf.get_sticker_decay_multiplier("sticker_b", state)
    # 在 1-24h 线性区间中点附近，应接近 (0.3 + 1.0) / 2 ≈ 0.65
    assert 0.55 <= mult <= 0.75, f"12.5h 前应处于线性恢复中段，得到 {mult}"


def test_decay_fully_recovered_after_24h() -> None:
    state = _state_with_last_sent("sticker_c", hours_ago=25)
    mult = sf.get_sticker_decay_multiplier("sticker_c", state)
    assert mult == 1.0, f"24h+ 应完全恢复，得到 {mult}"


def test_decay_at_exact_1h_boundary() -> None:
    state = _state_with_last_sent("sticker_d", hours_ago=1.01)
    mult = sf.get_sticker_decay_multiplier("sticker_d", state)
    # 刚过 1h，应非常接近 0.3 但略高
    assert 0.3 <= mult < 0.35, f"1h 边界应刚开始恢复，得到 {mult}"


def test_never_sent_returns_one() -> None:
    """从未发过的表情包不应被压制。"""
    state = {"items": {}}
    assert sf.get_sticker_decay_multiplier("new_sticker", state) == 1.0


def test_no_last_sent_returns_one() -> None:
    """有记录但 last_sent_at 为空：当作未发过。"""
    state = {
        "items": {
            "x": {"sent_count": 1, "positive_count": 0, "last_sent_at": ""},
        },
    }
    assert sf.get_sticker_decay_multiplier("x", state) == 1.0


def test_malformed_last_sent_returns_one() -> None:
    state = {
        "items": {
            "x": {"sent_count": 1, "positive_count": 0, "last_sent_at": "not-a-date"},
        },
    }
    assert sf.get_sticker_decay_multiplier("x", state) == 1.0


def test_decay_with_explicit_now_ts() -> None:
    """now_ts 注入用于确定性测试。"""
    state = _state_with_last_sent("sticker_e", hours_ago=2)
    later = time.time() + 23 * 3600  # 把当前时间往后推 23 小时
    # 现在 last_sent 距离 later 是 ~25 小时 → 应完全恢复
    mult = sf.get_sticker_decay_multiplier("sticker_e", state, now_ts=later)
    assert mult == 1.0


def test_monotonic_increase_from_1h_to_24h() -> None:
    """衰减系数应在 1-24h 单调递增。"""
    name = "sticker_mono"
    samples = []
    for h in [1.5, 5, 10, 18, 23]:
        state = _state_with_last_sent(name, hours_ago=h)
        samples.append(sf.get_sticker_decay_multiplier(name, state))
    for i in range(len(samples) - 1):
        assert samples[i] <= samples[i + 1], f"非单调：{samples}"
