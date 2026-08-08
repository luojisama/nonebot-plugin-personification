from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass
class FavorabilityTurnSignals:
    group_atmosphere_positive: bool = False
    interaction_interesting: bool = False

    def merge(self, other: "FavorabilityTurnSignals") -> None:
        self.group_atmosphere_positive = bool(
            self.group_atmosphere_positive or other.group_atmosphere_positive
        )
        self.interaction_interesting = bool(
            self.interaction_interesting or other.interaction_interesting
        )


def signals_from_semantic_frame(
    semantic_frame: Any,
    *,
    is_private: bool,
    minimum_confidence: float = 0.55,
) -> FavorabilityTurnSignals:
    try:
        confidence = float(getattr(semantic_frame, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.0
    if confidence < minimum_confidence:
        return FavorabilityTurnSignals()
    return FavorabilityTurnSignals(
        group_atmosphere_positive=(
            not is_private and bool(getattr(semantic_frame, "group_atmosphere_positive", False))
        ),
        interaction_interesting=bool(
            getattr(semantic_frame, "interaction_interesting", False)
        ),
    )


def extract_legacy_favorability_markers(
    text: Any,
) -> tuple[str, FavorabilityTurnSignals]:
    raw = str(text or "")
    signals = FavorabilityTurnSignals(
        group_atmosphere_positive="[氛围好]" in raw or "<氛围好>" in raw,
        interaction_interesting="[有趣]" in raw or "<有趣>" in raw,
    )
    for marker in ("[氛围好]", "<氛围好>", "[有趣]", "<有趣>"):
        raw = raw.replace(marker, "")
    return raw.strip(), signals


def build_favorability_context_block(
    *,
    user_level: str,
    user_attitude: str,
    group_attitude: str = "",
    is_private: bool,
) -> str:
    level = str(user_level or "").strip()
    attitude = str(user_attitude or "").strip() or "态度普通，像平常一样交流。"
    relation_style = "用自然平衡语气回应。"
    preferred_length = "默认回复 1-2 句。"
    if level in {"挚友", "亲密"}:
        relation_style = "适度使用更亲近的称呼或语气词，体现熟悉感。"
        preferred_length = "可以扩展到 2-4 句，增加情感反馈。"
    elif level in {"陌生", "路人", "初见"}:
        relation_style = "保持礼貌和边界感，避免过度亲昵。"
        preferred_length = "优先 1-2 句，直接回答重点。"
    if is_private:
        relation_style += " 私聊场景可更自然连续，不必强调围观感。"
    lines = [
        "## 当前关系表达边界",
        f"- 你对该用户的个人态度：{attitude}",
        f"- 关系表达策略：{relation_style}",
        f"- 长度偏好：{preferred_length}",
    ]
    group_text = str(group_attitude or "").strip()
    if group_text and not is_private:
        lines.append(f"- 当前群聊整体氛围带给你的感受：{group_text}")
    return "\n".join(lines)


def build_favorability_turn_id(
    *,
    trace_id: Any = "",
    message_id: Any = "",
    group_id: Any = "",
    user_id: Any = "",
) -> str:
    stable_message_id = str(message_id or "").strip()
    fallback_trace_id = str(trace_id or "").strip()
    if stable_message_id:
        parts = ["message", stable_message_id, str(group_id or "").strip(), str(user_id or "").strip()]
    elif fallback_trace_id:
        parts = ["trace", fallback_trace_id, str(group_id or "").strip(), str(user_id or "").strip()]
    else:
        return ""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"reply-turn-{digest[:32]}"


def commit_favorability_turn(
    *,
    service: Any,
    user_id: str,
    group_id: str,
    is_private: bool,
    is_direct: bool,
    is_random_chat: bool,
    signals: FavorabilityTurnSignals,
    turn_id: str,
    now: Any = None,
) -> list[dict[str, Any]]:
    if service is None or not bool(getattr(service, "enabled", True)):
        return []
    results: list[dict[str, Any]] = []
    uid = str(user_id or "").strip()
    gid = "" if is_private else str(group_id or "").strip()
    if not uid:
        return results
    if signals.group_atmosphere_positive and gid and hasattr(service, "apply_group_good_atmosphere"):
        results.append(
            service.apply_group_good_atmosphere(
                gid,
                now=now,
                reason="统一语义帧或兼容控制标记判定群聊氛围良好",
                event_id=f"{turn_id}:group-atmosphere" if turn_id else "",
            )
        )
    if signals.interaction_interesting and hasattr(service, "apply_user_interesting_chat"):
        results.append(
            service.apply_user_interesting_chat(
                uid,
                now=now,
                group_id=gid,
                reason="统一语义帧或兼容控制标记判定本轮互动有趣",
                event_id=f"{turn_id}:interesting" if turn_id else "",
            )
        )
    if hasattr(service, "apply_user_reply_interaction"):
        results.append(
            service.apply_user_reply_interaction(
                uid,
                now=now,
                group_id=gid,
                is_direct=is_direct,
                is_random_chat=is_random_chat,
                event_id=f"{turn_id}:reply" if turn_id else "",
            )
        )
    return results


__all__ = [
    "FavorabilityTurnSignals",
    "build_favorability_context_block",
    "build_favorability_turn_id",
    "commit_favorability_turn",
    "extract_legacy_favorability_markers",
    "signals_from_semantic_frame",
]
