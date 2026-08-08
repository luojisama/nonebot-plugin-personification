from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class AgentBudgetProfile:
    mode: str
    suggested_max_steps: int
    suggested_time_budget_seconds: float | None
    reason: str
    source: str = "shadow"


_LOOKUP_TOOL_INTENTS = {"lookup_web", "lookup_plugin", "runtime_capability", "memory", "vision"}
_BUDGET_MODE_ALIASES = {
    "": "shadow",
    "shadow": "shadow",
    "observe": "shadow",
    "observed": "shadow",
    "dry_run": "shadow",
    "dry-run": "shadow",
    "off": "shadow",
    "disabled": "shadow",
    "false": "shadow",
    "0": "shadow",
    "关闭": "shadow",
    "禁用": "shadow",
    "adaptive": "adaptive",
    "apply": "adaptive",
    "enabled": "adaptive",
    "on": "adaptive",
    "true": "adaptive",
    "1": "adaptive",
    "自适应": "adaptive",
    "启用": "adaptive",
}


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _tool_intent_set(value: Any) -> set[str]:
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list | tuple | set):
        raw_items = list(value)
    else:
        raw_items = []
    return {str(item or "").strip() for item in raw_items if str(item or "").strip()}


def _limit_steps(actual_max_steps: int | None, suggested: int) -> int:
    try:
        actual = int(actual_max_steps or 0)
    except (TypeError, ValueError):
        actual = 0
    suggested = max(0, int(suggested or 0))
    return min(actual, suggested) if actual > 0 else suggested


def _limit_seconds(actual_time_budget_seconds: float | None, suggested: float | None) -> float | None:
    if suggested is None:
        return None
    try:
        value = max(0.0, float(suggested))
    except (TypeError, ValueError):
        return None
    if actual_time_budget_seconds is None:
        return value
    try:
        actual = max(0.0, float(actual_time_budget_seconds))
    except (TypeError, ValueError):
        return value
    return min(actual, value)


def normalize_agent_budget_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _BUDGET_MODE_ALIASES.get(text, "shadow")


def apply_agent_budget_profile(
    profile: AgentBudgetProfile,
    *,
    mode: Any = "shadow",
    actual_max_steps: int | None = None,
    actual_time_budget_seconds: float | None = None,
) -> tuple[int, float | None, AgentBudgetProfile, bool]:
    normalized_mode = normalize_agent_budget_mode(mode)
    sourced_profile = replace(profile, source=normalized_mode)
    try:
        current_steps = max(0, int(actual_max_steps or 0))
    except (TypeError, ValueError):
        current_steps = 0
    if normalized_mode != "adaptive":
        return current_steps, actual_time_budget_seconds, sourced_profile, False
    try:
        suggested_steps = max(0, int(profile.suggested_max_steps or 0))
    except (TypeError, ValueError):
        suggested_steps = current_steps
    return (
        suggested_steps,
        profile.suggested_time_budget_seconds
        if profile.suggested_time_budget_seconds is not None
        else actual_time_budget_seconds,
        sourced_profile,
        True,
    )


def derive_agent_budget_profile(
    *,
    turn_plan: Any = None,
    intent_decision: Any = None,
    actual_max_steps: int | None = None,
    actual_time_budget_seconds: float | None = None,
) -> AgentBudgetProfile:
    speech_act = _norm(getattr(turn_plan, "speech_act", ""))
    reply_action = _norm(getattr(turn_plan, "reply_action", ""))
    output_mode = _norm(getattr(turn_plan, "output_mode", ""))
    research_need = _norm(getattr(turn_plan, "research_need", ""))
    tool_intents = _tool_intent_set(getattr(turn_plan, "tool_intent", []))
    chat_intent = _norm(getattr(intent_decision, "chat_intent", ""))

    if reply_action == "silence" or speech_act == "silence":
        return AgentBudgetProfile(
            mode="silence_fast",
            suggested_max_steps=_limit_steps(actual_max_steps, 0),
            suggested_time_budget_seconds=_limit_seconds(actual_time_budget_seconds, 3.0),
            reason="speech_act_silence",
        )

    if speech_act == "execute_action" or chat_intent in {"expression", "image_generation"} or tool_intents & {"expression", "image_gen"}:
        target_seconds = 60.0 if chat_intent == "image_generation" or "image_gen" in tool_intents else 25.0
        return AgentBudgetProfile(
            mode="action",
            suggested_max_steps=_limit_steps(actual_max_steps, 3),
            suggested_time_budget_seconds=_limit_seconds(actual_time_budget_seconds, target_seconds),
            reason="execute_action",
        )

    if (
        speech_act == "source_summary"
        or chat_intent == "lookup"
        or research_need in {"medium", "high"}
        or bool(tool_intents & _LOOKUP_TOOL_INTENTS and research_need != "none")
    ):
        target_seconds = 120.0 if research_need == "high" else 90.0
        return AgentBudgetProfile(
            mode="research",
            suggested_max_steps=_limit_steps(actual_max_steps, actual_max_steps or 6),
            suggested_time_budget_seconds=_limit_seconds(actual_time_budget_seconds, target_seconds),
            reason="research_or_summary",
        )

    if (
        speech_act == "answer"
        or chat_intent in {"explanation", "plugin_question"}
        or output_mode in {"chat_answer", "structured_help"}
    ):
        return AgentBudgetProfile(
            mode="answer",
            suggested_max_steps=_limit_steps(actual_max_steps, 4),
            suggested_time_budget_seconds=_limit_seconds(actual_time_budget_seconds, 45.0),
            reason="answer_mode",
        )

    if (
        speech_act in {"participate", "tease", ""}
        and research_need in {"", "none", "low"}
        and not (tool_intents & _LOOKUP_TOOL_INTENTS)
        and chat_intent in {"", "banter"}
    ):
        return AgentBudgetProfile(
            mode="light_chat",
            suggested_max_steps=_limit_steps(actual_max_steps, 2),
            suggested_time_budget_seconds=_limit_seconds(actual_time_budget_seconds, 18.0),
            reason="light_chat",
        )

    return AgentBudgetProfile(
        mode="balanced",
        suggested_max_steps=_limit_steps(actual_max_steps, 4),
        suggested_time_budget_seconds=_limit_seconds(actual_time_budget_seconds, 60.0),
        reason="default_balanced",
    )


def render_agent_budget_trace_detail(
    profile: AgentBudgetProfile,
    *,
    actual_max_steps: int | None = None,
    actual_time_budget_seconds: float | None = None,
) -> str:
    suggested_seconds = (
        "-"
        if profile.suggested_time_budget_seconds is None
        else f"{profile.suggested_time_budget_seconds:g}"
    )
    actual_seconds = "-" if actual_time_budget_seconds is None else f"{max(0.0, float(actual_time_budget_seconds)):g}"
    return (
        f"budget={profile.mode} "
        f"suggested_steps={profile.suggested_max_steps} "
        f"actual_steps={int(actual_max_steps or 0)} "
        f"suggested_seconds={suggested_seconds} "
        f"actual_seconds={actual_seconds} "
        f"source={profile.source} "
        f"reason={profile.reason}"
    )


__all__ = [
    "AgentBudgetProfile",
    "apply_agent_budget_profile",
    "derive_agent_budget_profile",
    "normalize_agent_budget_mode",
    "render_agent_budget_trace_detail",
]
