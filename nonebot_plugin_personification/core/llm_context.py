from __future__ import annotations

import contextvars
from typing import Any


_LLM_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "personification_llm_context", default=None
)

LLM_RETRY_POLICY_SINGLE_ATTEMPT = "single_attempt"


def set_llm_context(
    *,
    group_id: str = "",
    user_id: str = "",
    purpose: str = "",
    retry_policy: str = "",
) -> contextvars.Token:
    """在进入 LLM 调用栈前调用；返回 token 供 reset。"""
    value = {
        "group_id": str(group_id or ""),
        "user_id": str(user_id or ""),
        "purpose": str(purpose or ""),
    }
    if retry_policy:
        value["retry_policy"] = str(retry_policy)
    return _LLM_CONTEXT.set(value)


def reset_llm_context(token: contextvars.Token) -> None:
    try:
        _LLM_CONTEXT.reset(token)
    except Exception:
        pass


def current_llm_context() -> dict[str, Any]:
    value = _LLM_CONTEXT.get()
    return value if isinstance(value, dict) else {}


def use_single_attempt_retry_policy() -> bool:
    return str(current_llm_context().get("retry_policy", "") or "") == LLM_RETRY_POLICY_SINGLE_ATTEMPT


__all__ = [
    "LLM_RETRY_POLICY_SINGLE_ATTEMPT",
    "set_llm_context",
    "reset_llm_context",
    "current_llm_context",
    "use_single_attempt_retry_policy",
]
