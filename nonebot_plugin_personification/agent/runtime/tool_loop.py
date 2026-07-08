from __future__ import annotations

import json
from typing import Any, Callable

from ...core.metrics import record_timing
from .loop_utils import summarize_tool_response_raw


def _tool_call_arguments(tool_call: Any) -> dict[str, Any]:
    return dict(getattr(tool_call, "arguments", None) or {})


def _tool_call_name(tool_call: Any) -> str:
    return str(getattr(tool_call, "name", "") or "").strip()


def response_content_len(response: Any) -> int:
    return len(str(getattr(response, "content", "") or "").strip())


def selected_tool_names(active_schemas: list[dict], schema_tool_name: Callable[[dict], str]) -> list[str]:
    return [
        name
        for schema in active_schemas
        for name in [schema_tool_name(schema)]
        if name
    ]


def record_model_response_usage(*, response: Any, tool_caller: Any) -> None:
    try:
        usage = getattr(response, "usage", None) or {}
        if not isinstance(usage, dict) or not (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            return
        from ...core import llm_context as _llm_ctx
        from ...core import token_ledger as _ledger

        ctx = _llm_ctx.current_llm_context()
        # 从 tool_caller 类名推导 provider，比从 model 名推导更准确
        # （特别针对 Codex 使用 chatgpt OAuth、model 字段含 "gpt" 易误判的情况）
        caller_cls = type(tool_caller).__name__.lower()
        if "codex" in caller_cls:
            provider_label = "codex"
        elif "anthropic" in caller_cls or "claudecode" in caller_cls or "claude" in caller_cls:
            provider_label = "anthropic"
        elif "geminicli" in caller_cls:
            provider_label = "gemini"
        elif "gemini" in caller_cls:
            provider_label = "gemini"
        elif "openai" in caller_cls:
            provider_label = "openai"
        else:
            provider_label = ""  # 让 token_ledger 从 model 名自行推导
        _ledger.record_llm_call(
            model=str(getattr(response, "model_used", "") or ""),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            group_id=str(ctx.get("group_id", "") or ""),
            user_id=str(ctx.get("user_id", "") or ""),
            purpose=str(ctx.get("purpose", "") or "agent"),
            provider=provider_label,
        )
    except Exception:
        pass


def observe_model_step(
    *,
    response: Any,
    tool_caller: Any,
    logger: Any,
    step: int,
    selected_names: list[str],
    runtime_chat_intent: str,
    model_elapsed_ms: int,
    record_trace: Callable[..., None],
) -> int:
    record_model_response_usage(response=response, tool_caller=tool_caller)
    content_len = response_content_len(response)
    tool_calls = list(getattr(response, "tool_calls", []) or [])
    finish_reason = str(getattr(response, "finish_reason", "") or "")
    logger.info(
        f"[agent] step={step} finish_reason={finish_reason} "
        f"tool_calls={len(tool_calls)} content_len={content_len} "
        f"model_elapsed_ms={model_elapsed_ms}"
    )
    record_timing(
        "agent.model_step_ms",
        model_elapsed_ms,
        intent=runtime_chat_intent or "unknown",
        finish_reason=finish_reason,
    )
    record_trace(
        key="agent_model_step",
        label=f"Agent 模型步 {step}",
        status="ok" if content_len > 0 or tool_calls else "warn",
        detail=(
            f"intent={runtime_chat_intent or '-'} step={step} "
            f"tools={','.join(selected_names[:8]) if selected_names else '-'} "
            f"finish={finish_reason} tool_calls={len(tool_calls)} "
            f"content_len={content_len} elapsed_ms={model_elapsed_ms}"
        ),
    )
    if finish_reason == "stop" and not tool_calls and content_len == 0:
        logger.warning(
            "[agent] provider returned empty stop response "
            + summarize_tool_response_raw(getattr(response, "raw", None))
        )
    return content_len


def append_assistant_tool_calls_message(*, messages: list[dict], response: Any) -> None:
    tool_calls = list(getattr(response, "tool_calls", []) or [])
    if not tool_calls:
        return
    messages.append(
        {
            "role": "assistant",
            # 必须用空字符串而非 None：部分严格 provider（Rust 反序列化）
            # 会把 null 当作"缺失字段"直接 400 拒绝。
            "content": getattr(response, "content", None) if getattr(response, "content", None) else "",
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(_tool_call_arguments(tool_call), ensure_ascii=False),
                    },
                }
                for tool_call in tool_calls
            ],
        }
    )


def append_single_tool_call_exchange(
    *,
    messages: list[dict],
    tool_caller: Any,
    call_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }
            ],
        }
    )
    messages.append(tool_caller.build_tool_result_message(call_id, tool_name, result))


def append_tool_result_message(
    *,
    messages: list[dict],
    tool_caller: Any,
    tool_call: Any,
    result: Any,
) -> None:
    messages.append(
        tool_caller.build_tool_result_message(
            tool_call.id,
            tool_call.name,
            result,
        )
    )


def trace_tool_call(*, tool_call: Any, step: int, record_trace: Callable[..., None]) -> dict[str, Any]:
    tool_args = _tool_call_arguments(tool_call)
    record_trace(
        key="agent_tool_call",
        label="Agent 工具选择",
        status="info",
        detail=(
            f"step={step} tool={_tool_call_name(tool_call)} "
            f"arg_keys={','.join(sorted(str(key)[:40] for key in tool_args.keys())) or '-'}"
        ),
    )
    return tool_args


def trace_tool_result(
    *,
    tool_name: str,
    result: Any,
    step: int,
    elapsed_ms: int,
    record_trace: Callable[..., None],
    status_for_result: Callable[[Any], str],
) -> None:
    record_trace(
        key="agent_tool_result",
        label="Agent 工具结果",
        status=status_for_result(result),
        detail=(
            f"step={step} tool={tool_name} "
            f"result_len={len(str(result or ''))} elapsed_ms={elapsed_ms}"
        ),
    )


__all__ = [
    "append_assistant_tool_calls_message",
    "append_single_tool_call_exchange",
    "append_tool_result_message",
    "observe_model_step",
    "record_model_response_usage",
    "response_content_len",
    "selected_tool_names",
    "trace_tool_call",
    "trace_tool_result",
]
