"""结构化的可见回复长度策略。

长度档位只读取已经完成的 TurnPlan、媒体解析和 Agent 状态，不能从用户
原文的关键词推断意图。最终裁剪仍由 normal/YAML 的发送出口执行；这个
模块只负责把两条链路需要的策略统一起来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_EVIDENCE_TOOL_INTENTS = {
    "lookup_web",
    "lookup_plugin",
    "runtime_capability",
    "vision",
    "memory",
}
_MEDIA_KEYS = {
    "images",
    "videos",
    "audios",
    "image_usable",
    "video_usable",
    "audio_usable",
}


@dataclass(frozen=True)
class ReplyLengthPolicy:
    """本轮最终可见文本的长度档位。``0`` 表示保持现有无限制契约。"""

    mode: str
    max_chars: int
    reason: str
    legacy_cap: int = 0


def _positive_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (str, bytes)):
        return [value] if str(value).strip() else []
    try:
        return list(value)
    except TypeError:
        return [value]


def _tool_intents(turn_plan: Any) -> set[str]:
    raw = getattr(turn_plan, "tool_intent", ()) if turn_plan is not None else ()
    return {
        str(item or "").strip().lower()
        for item in _items(raw)
        if str(item or "").strip()
    }


def _has_media_context(media_context: Any) -> bool:
    if isinstance(media_context, Mapping):
        for key in _MEDIA_KEYS:
            value = media_context.get(key)
            if isinstance(value, bool) and value:
                return True
            if _positive_int(value) > 0:
                return True
            if isinstance(value, (list, tuple, set, frozenset, Mapping)) and bool(value):
                return True
        return False
    for item in _items(media_context):
        if isinstance(item, Mapping):
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        else:
            kind = str(getattr(item, "kind", "") or getattr(item, "type", "")).strip().lower()
        if kind in {"image", "video", "audio", "record", "gif", "mface", "file"}:
            return True
    return False


def _has_tool_calls(tool_calls: Any) -> bool:
    if isinstance(tool_calls, Mapping):
        return bool(tool_calls)
    return bool(_items(tool_calls))


def resolve_reply_length_policy(
    plugin_config: Any,
    *,
    turn_plan: Any = None,
    budget_profile: Any = None,
    media_context: Any = None,
    tool_calls: Any = None,
    evidence_delivery_required: bool = False,
    bypass_length_limits: bool = False,
) -> ReplyLengthPolicy:
    """Resolve the effective cap for one turn.

    ``personification_max_output_chars`` remains a backwards-compatible global
    hard ceiling. New per-surface limits are used only when that legacy value is
    zero, or as the stricter ceiling when it is configured.
    """

    legacy_cap = _positive_int(getattr(plugin_config, "personification_max_output_chars", 0))
    if bypass_length_limits or evidence_delivery_required:
        return ReplyLengthPolicy("bypass", 0, "evidence_delivery_or_direct_output", legacy_cap)

    tool_intents = _tool_intents(turn_plan)
    research_need = str(getattr(turn_plan, "research_need", "") or "").strip().lower()
    vision_need = str(getattr(turn_plan, "vision_need", "") or "").strip().lower()
    output_mode = str(getattr(turn_plan, "output_mode", "") or "").strip().lower()
    budget_mode = str(getattr(budget_profile, "mode", budget_profile or "") or "").strip().lower()

    media_evidence = _has_media_context(media_context)
    has_tool_calls = _has_tool_calls(tool_calls)
    evidence = bool(
        tool_intents & _EVIDENCE_TOOL_INTENTS
        or research_need not in {"", "none"}
        or vision_need not in {"", "none"}
        or media_evidence
        or has_tool_calls
        or budget_mode in {"research", "answer", "balanced"}
    )
    # A plain light-chat TurnPlan is deliberately the only short default. A
    # structured answer without evidence still gets the normal answer budget.
    if not evidence and (
        budget_mode == "light_chat"
        or output_mode in {"", "chat_short", "qzone_reply"}
    ):
        mode = "chat"
        configured = _positive_int(
            getattr(plugin_config, "personification_chat_max_output_chars", 60),
            60,
        )
        reason = "light_chat_without_evidence"
    else:
        mode = "evidence"
        configured = _positive_int(
            getattr(plugin_config, "personification_tool_max_output_chars", 600),
            600,
        )
        if tool_intents & _EVIDENCE_TOOL_INTENTS:
            reason = "tool_intent"
        elif research_need not in {"", "none"}:
            reason = "research_need"
        elif vision_need not in {"", "none"}:
            reason = "vision_need"
        elif media_evidence:
            reason = "media_evidence"
        elif has_tool_calls:
            reason = "agent_tool_call"
        else:
            reason = "budget_profile"

    max_chars = min(configured, legacy_cap) if legacy_cap and configured else (legacy_cap or configured)
    return ReplyLengthPolicy(mode, max_chars, reason, legacy_cap)


def render_reply_length_trace(policy: ReplyLengthPolicy, *, before_chars: int, after_chars: int) -> str:
    truncated = bool(before_chars > after_chars and policy.max_chars > 0)
    return (
        f"length_mode={policy.mode} limit_chars={policy.max_chars or '-'} "
        f"before_chars={max(0, int(before_chars))} after_chars={max(0, int(after_chars))} "
        f"truncated={str(truncated).lower()} reason={policy.reason}"
    )


def render_reply_length_prompt_hint(policy: ReplyLengthPolicy) -> str:
    """Return a short, model-facing reminder without exposing internal state."""

    if policy.mode == "bypass":
        return "证据交付或直接媒体动作必须完整交付；不要主动截断来源链接、结构化证据或媒体标记。"
    if policy.mode == "evidence":
        limit = f"{policy.max_chars} 字" if policy.max_chars > 0 else "不设字数硬上限"
        return (
            f"当前是工具/检索/视觉证据回复档位，最终可见正文允许在 {limit} 内完整说明证据。"
            "视觉事实必须写进正文，不要只放隐藏块；只输出纯文本，禁止 Markdown 标题、列表、XML 或内部状态标签。"
        )
    limit = f"{policy.max_chars} 字" if policy.max_chars > 0 else "不设字数硬上限"
    return f"当前是日常交流档位，回复保持自然短句，最终可见正文控制在 {limit} 内；禁止 Markdown、XML 或内部状态标签。"


def truncate_reply_text(text: str, max_chars: int) -> str:
    """Apply one turn-level cap, preferring a nearby sentence boundary."""

    value = str(text or "")
    try:
        limit = max(0, int(max_chars or 0))
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0 or len(value) <= limit:
        return value
    candidate = value[:limit]
    for index in range(len(candidate) - 1, max(0, len(candidate) - 60), -1):
        if candidate[index] in "。！？!?\n":
            return candidate[: index + 1]
    for index in range(len(candidate) - 1, max(0, len(candidate) - 30), -1):
        if candidate[index] in "，；,;":
            return candidate[: index + 1]
    return candidate


__all__ = [
    "ReplyLengthPolicy",
    "render_reply_length_prompt_hint",
    "render_reply_length_trace",
    "resolve_reply_length_policy",
    "truncate_reply_text",
]
