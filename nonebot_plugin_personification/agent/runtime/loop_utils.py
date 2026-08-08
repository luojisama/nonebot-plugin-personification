from __future__ import annotations

from typing import Any, Awaitable, Callable

from .fallbacks import (
    TOOL_RESULT_EMPTY_EVIDENCE,
    TOOL_RESULT_OPERATIONAL_FAILURE,
    _tool_result_outcome,
    tool_signature,
)


_BUILTIN_SEARCH_CALLER_NAMES = frozenset(
    {
        "GeminiToolCaller",
        "AnthropicToolCaller",
        "OpenAICodexToolCaller",
        # 三个 CLI 协议 caller 内部也已注入对应原生 search 工具（impl.py:3203/3741/4005）
        "GeminiCliToolCaller",
        "AntigravityCliToolCaller",
        "ClaudeCodeToolCaller",
    }
)


def summarize_tool_response_raw(raw: Any) -> str:
    if not isinstance(raw, dict):
        if raw is None:
            return "raw=none"
        return f"raw_type={type(raw).__name__}"

    output = raw.get("output", [])
    if isinstance(output, list):
        output_items = len(output)
        output_types = ",".join(
            str(item.get("type", "?"))
            for item in output[:3]
            if isinstance(item, dict)
        ) or "none"
    else:
        output_items = "n/a"
        output_types = "n/a"
    usage = raw.get("usage", {})
    output_tokens = usage.get("output_tokens", "?") if isinstance(usage, dict) else "?"
    status = raw.get("status", "?")
    model = raw.get("model", "?")
    return (
        f"status={status} model={model} output_items={output_items} "
        f"output_types={output_types} output_tokens={output_tokens}"
    )


def tool_result_trace_status(result: Any) -> str:
    text = str(result or "").strip()
    if not text:
        return "warn"
    if text.startswith("工具调用失败") or (text.startswith("工具 ") and text.endswith("不存在")):
        return "error"
    outcome = _tool_result_outcome(text)
    if outcome == TOOL_RESULT_OPERATIONAL_FAILURE:
        return "error"
    if outcome == TOOL_RESULT_EMPTY_EVIDENCE:
        return "warn"
    return "ok"


def caller_supports_builtin_search(tool_caller: Any) -> bool:
    return tool_caller.__class__.__name__ in _BUILTIN_SEARCH_CALLER_NAMES


async def safe_ack(
    ack_sender: Callable[[str], Awaitable[None]],
    text: str,
    logger: Any,
) -> None:
    try:
        await ack_sender(text)
    except Exception as exc:
        logger.debug(f"[agent] ack send failed: {exc}")


def record_reply_trace_stage(
    *,
    key: str,
    label: str,
    status: str = "info",
    detail: Any = "",
    hint: str = "",
) -> None:
    try:
        from ...core import reply_turn_trace

        reply_turn_trace.record_stage(
            key=key,
            label=label,
            status=status,
            detail=detail,
            hint=hint,
        )
    except Exception:
        pass


__all__ = [
    "caller_supports_builtin_search",
    "record_reply_trace_stage",
    "safe_ack",
    "summarize_tool_response_raw",
    "tool_result_trace_status",
    "tool_signature",
]
