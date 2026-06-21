"""拟人化发送层：打字延迟、错别字注入、引用/@ 决策。

设计约束：
- 全部为纯函数或仅依赖入参的轻量状态，便于单元测试；
- 不发起任何网络/LLM 调用；协议交互由 core.protocol_capabilities 承担；
- 延迟计算必须扣除 LLM 已耗时，避免"生成慢 + 模拟打字"双重延迟。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

# ──────────────────────── 打字延迟 ────────────────────────

_BASE_READ_RANGE = (0.5, 1.2)
_NIGHT_FACTOR = 1.5
_GAP_MIN = 0.6


def typing_enabled(plugin_config: Any) -> bool:
    return bool(getattr(plugin_config, "personification_humanize_typing_enabled", True))


def compute_typing_delay(
    text: str,
    *,
    cps: float,
    max_delay: float,
    already_elapsed: float,
    night: bool = False,
    rng: random.Random | None = None,
) -> float:
    """首条消息的模拟"阅读+打字"延迟（秒）。

    delay = 阅读基础时间 + len/cps - LLM 已耗时，clamp 到 [0, max_delay]。
    深夜（作息休息时段）乘 _NIGHT_FACTOR，模拟回得更慢。
    """
    r = rng or random
    cps = max(1.0, float(cps or 7.0))
    max_delay = max(0.0, float(max_delay or 0.0))
    base = r.uniform(*_BASE_READ_RANGE)
    typing_time = len(str(text or "")) / cps
    total = base + typing_time
    if night:
        total *= _NIGHT_FACTOR
    total -= max(0.0, float(already_elapsed or 0.0))
    return max(0.0, min(total, max_delay))


def compute_gap_delay(
    next_text: str,
    *,
    cps: float,
    max_delay: float,
    rng: random.Random | None = None,
) -> float:
    """段间延迟：按下一段长度模拟打字时间，带轻微抖动。"""
    r = rng or random
    cps = max(1.0, float(cps or 7.0))
    typing_time = len(str(next_text or "")) / cps + r.uniform(0.2, 0.6)
    return max(_GAP_MIN, min(typing_time, max(0.0, float(max_delay or 0.0))))


# ──────────────────────── 错别字注入 ────────────────────────

# 安全的同音/易混字对：只挑日常闲聊高频、错了也不会引起歧义事故的字。
_TYPO_PAIRS: dict[str, str] = {
    "的": "得",
    "得": "的",
    "在": "再",
    "再": "在",
    "做": "作",
    "作": "做",
    "他": "她",
    "她": "他",
    "那": "哪",
    "哪": "那",
    "象": "像",
    "像": "象",
}

_TYPO_SKIP_PATTERN = re.compile(r"[0-9a-zA-Z_/:#@\[\]<>{}]|https?://")


def maybe_inject_typo(
    text: str,
    *,
    probability: float,
    rng: random.Random | None = None,
) -> tuple[str, str | None]:
    """按概率给闲聊短句注入一个错别字。

    返回 (可能带错字的文本, 修正提示或 None)；修正提示形如 ``*的``，
    由调用方决定是否跟发。仅处理 6-40 字、不含数字/链接/代码符号的文本。
    """
    r = rng or random
    raw = str(text or "")
    prob = max(0.0, float(probability or 0.0))
    if prob <= 0.0 or r.random() >= prob:
        return raw, None
    if not (6 <= len(raw) <= 40) or _TYPO_SKIP_PATTERN.search(raw):
        return raw, None
    candidates = [(idx, ch) for idx, ch in enumerate(raw) if ch in _TYPO_PAIRS]
    if not candidates:
        return raw, None
    idx, correct = r.choice(candidates)
    wrong = _TYPO_PAIRS[correct]
    mutated = raw[:idx] + wrong + raw[idx + 1:]
    return mutated, f"*{correct}"


# ──────────────────────── 引用 / @ 决策 ────────────────────────

# group_id -> (user_id, ts)：避免对同一用户连续引用，显得机械。
_last_quote: dict[str, tuple[str, float]] = {}
_QUOTE_REPEAT_WINDOW = 600.0


def should_quote_reply(
    *,
    plugin_config: Any,
    state: dict[str, Any],
    event: Any,
    group_id: str,
    user_id: str,
    is_private: bool,
    has_newer_batch: bool,
    now_fn: Any = time.time,
) -> Any:
    """判断本轮是否对触发消息使用引用回复；返回 message_id 或 None。

    触发条件（满足其一）：
    - buffer 抢占后回复的批次已被更新批次跟上（has_newer_batch）；
    - 合并批内被回复消息之后还有 ≥ min_gap 条新消息。
    """
    if is_private:
        return None
    if not bool(getattr(plugin_config, "personification_humanize_quote_reply_enabled", True)):
        return None
    message_id = getattr(event, "message_id", None)
    if message_id is None:
        return None
    min_gap = max(1, int(getattr(plugin_config, "personification_humanize_quote_reply_min_gap", 4) or 4))
    newer_in_batch = 0
    batched = state.get("batched_events") or []
    if isinstance(batched, list) and len(batched) > 1:
        for pos, item in enumerate(batched):
            if getattr(item, "message_id", None) == message_id:
                newer_in_batch = len(batched) - 1 - pos
                break
    if not has_newer_batch and newer_in_batch < min_gap:
        return None
    now = now_fn()
    last = _last_quote.get(str(group_id))
    if last and last[0] == str(user_id) and now - last[1] < _QUOTE_REPEAT_WINDOW:
        return None
    _last_quote[str(group_id)] = (str(user_id), now)
    return message_id


def should_at_target(
    *,
    plugin_config: Any,
    state: dict[str, Any],
    event: Any,
    user_id: str,
    is_private: bool,
    quote_message_id: Any,
) -> str | None:
    """多人混战场景判断是否 @ 回复对象；返回要 @ 的 user_id 或 None。

    条件：群聊、未使用引用（互斥）、合并批内最后发言者不是回复对象。
    """
    if is_private or quote_message_id is not None:
        return None
    if not bool(getattr(plugin_config, "personification_humanize_at_enabled", True)):
        return None
    batched = state.get("batched_events") or []
    if not isinstance(batched, list) or len(batched) < 2:
        return None
    last_speaker = str(getattr(batched[-1], "user_id", "") or "")
    target = str(user_id or "")
    if not target or not last_speaker or last_speaker == target:
        return None
    senders = {str(getattr(item, "user_id", "") or "") for item in batched}
    if len(senders) < 2:
        return None
    return target


def reset_humanize_state() -> None:
    """测试用：清空模块内的轻量状态。"""
    _last_quote.clear()


__all__ = [
    "compute_typing_delay",
    "compute_gap_delay",
    "maybe_inject_typo",
    "should_quote_reply",
    "should_at_target",
    "typing_enabled",
    "reset_humanize_state",
]
