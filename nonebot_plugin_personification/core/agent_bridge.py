from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agent.runtime.runner import run_agent
from ..agent.tool_registry import ToolRegistry
from ..agent.query_rewriter import QueryRewriteContext


class _NullLogger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass
class TextAgentExecutor:
    """Executor for non-chat surfaces that need the full Agent loop but send later."""

    logger: Any
    pending_actions: list[dict[str, Any]] = field(default_factory=list)

    def bind_pending_actions(self, actions: list[dict[str, Any]]) -> None:
        self.pending_actions = actions

    def queue_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        item = {"type": str(action or "").strip(), "params": dict(params or {})}
        self.pending_actions.append(item)
        return item

    async def execute(self, action: str, params: dict[str, Any]) -> str:
        _ = params
        try:
            self.logger.debug(f"[agent_bridge] ignored text-agent action: {action}")
        except Exception:
            pass
        return f"文本 Agent 已忽略发送动作：{action}"


def clone_tool_registry(registry: Any) -> ToolRegistry:
    cloned = ToolRegistry()
    if registry is None:
        return cloned
    active = getattr(registry, "active", None)
    tools = active() if callable(active) else []
    for tool in tools:
        cloned.register(tool)
    return cloned


async def run_text_agent(
    *,
    messages: list[dict[str, Any]],
    plugin_config: Any,
    logger: Any,
    tool_caller: Any,
    registry: Any,
    max_steps: int | None = None,
    use_builtin_search_hint: bool = False,
    trigger_reason: str = "",
    chat_intent_hint: str = "",
) -> str:
    """Run the complete Agent loop for flows that only need a text result.

    This intentionally reuses ``run_agent`` instead of the lightweight loop, so
    intent inference, query rewriting, tool selection, evidence synthesis and
    semantic fallback stay aligned with live chat. Sending-side tools are not
    registered here; callers remain responsible for delivering the final text.
    """

    logger = logger or _NullLogger()
    if not (
        getattr(plugin_config, "personification_agent_enabled", True)
        and tool_caller is not None
        and registry is not None
    ):
        return ""

    agent_messages = list(messages or [])
    if use_builtin_search_hint:
        agent_messages.append(
            {
                "role": "system",
                "content": (
                    "当前任务允许你在需要事实、新鲜信息或背景查证时调用搜索/知识工具；"
                    "不要臆造不确定事实。"
                ),
            }
        )
    if chat_intent_hint:
        agent_messages.append(
            {
                "role": "system",
                "content": f"当前后台任务类型提示：{chat_intent_hint}。仍由你自行决定是否需要调用工具。",
            }
        )

    executor = TextAgentExecutor(logger=logger)
    result = await run_agent(
        messages=agent_messages,
        registry=clone_tool_registry(registry),
        tool_caller=tool_caller,
        executor=executor,
        plugin_config=plugin_config,
        logger=logger,
        max_steps=max_steps,
        query_rewrite_context=QueryRewriteContext(trigger_reason=trigger_reason),
    )
    text = str(getattr(result, "text", "") or "").strip()
    if text in {"[NO_REPLY]", "<NO_REPLY>", "[SILENCE]", "<SILENCE>"}:
        return ""
    return text
