from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

from ..tool_registry import ToolRegistry
from .tool_contracts import direct_tool_result_from_contract
from .wrappers import _extract_persona_system_prompt, _render_tool_result_for_user, _wrap_tool_result_in_persona


@dataclass
class AgentResult:
    text: str
    pending_actions: List[dict]
    direct_output: bool = False
    bypass_length_limits: bool = False
    quality_checks: list[dict[str, Any]] = field(default_factory=list)


def direct_tool_result_agent_result(
    *,
    registry: ToolRegistry | None,
    tool_name: str,
    result_text: str,
    pending_actions: List[dict],
) -> AgentResult | None:
    result = direct_tool_result_from_contract(
        registry=registry,
        tool_name=tool_name,
        result_text=result_text,
    )
    if result is None:
        return None
    return AgentResult(
        text=result.text,
        pending_actions=pending_actions,
        direct_output=result.direct_output,
        bypass_length_limits=result.bypass_length_limits,
    )


async def synthesize_max_steps_result(
    *,
    registry: ToolRegistry | None,
    tool_name: str,
    result_text: str,
    user_query_text: str,
    messages: list[dict],
    pending_actions: List[dict],
    tool_caller: Any,
    turn_plan: Any = None,
) -> AgentResult:
    if not str(result_text or "").strip():
        return AgentResult(
            text="[NO_REPLY]",
            pending_actions=pending_actions,
        )
    direct_result = direct_tool_result_agent_result(
        registry=registry,
        tool_name=tool_name,
        result_text=result_text,
        pending_actions=pending_actions,
    )
    if direct_result is not None:
        return direct_result
    rendered_tool_result = _render_tool_result_for_user(
        tool_name,
        result_text,
        user_query_text,
    )
    return AgentResult(
        text=await _wrap_tool_result_in_persona(
            tool_caller=tool_caller,
            rendered_tool_result=rendered_tool_result,
            user_query_text=user_query_text,
            persona_system=_extract_persona_system_prompt(messages),
            turn_plan=turn_plan,
        ),
        pending_actions=pending_actions,
        direct_output=False,
        bypass_length_limits=False,
    )


__all__ = [
    "AgentResult",
    "direct_tool_result_agent_result",
    "synthesize_max_steps_result",
]
