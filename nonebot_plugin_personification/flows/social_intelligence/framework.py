"""SocialTrigger + Registry + SocialContext。

SocialTrigger 把"什么时候触发"（cron / interval / event）与"触发后做什么"
（handler）解耦；handler 通过 SocialContext 拿到运行所需的全部依赖
（bot 实例、tool_caller、persona_store、配置等），便于单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable


@dataclass
class SocialContext:
    """场景 handler 收到的运行时上下文。"""

    plugin_config: Any
    logger: Any
    get_bots: Callable[[], dict]
    get_whitelisted_groups: Callable[[], list[str]]
    tool_caller: Any  # 用于 Agent 决策与文案包装（建议传 agent_tool_caller）
    persona_store: Any
    data_dir: Any
    get_now: Callable[[], Any]
    load_prompt: Callable[[str | None], Any] | None = None
    tool_registry: Any = None
    agent_max_steps: int = 4
    qq_outbound_ledger: Any = None


async def dispatch_social_outbound(
    ctx: SocialContext,
    *,
    bot: Any,
    conversation_kind: str,
    conversation_id: str,
    surface: str,
    content: Any,
    user_target: str = "",
) -> bool:
    """Dispatch one social message and report only strict OneBot confirmation."""
    target = str(conversation_id or "").strip()
    if conversation_kind not in {"group", "private"}:
        raise ValueError("conversation_kind must be group or private")
    if conversation_kind == "group":
        send = lambda: bot.send_group_msg(group_id=int(target), message=content)
        event = SimpleNamespace(group_id=target, user_id=user_target)
    else:
        send = lambda: bot.send_private_msg(user_id=int(target), message=content)
        event = SimpleNamespace(user_id=target)
    if ctx.qq_outbound_ledger is None:
        await send()
        return True

    from ...core.qq_outbound import build_outbound_context

    outbound_context = build_outbound_context(
        bot=bot,
        event=event,
        surface=surface,
        user_target=user_target or (target if conversation_kind == "private" else ""),
    )
    receipt = await ctx.qq_outbound_ledger.dispatch(outbound_context, content, send)
    return receipt.status == "sent"


async def run_social_text_agent(
    ctx: SocialContext,
    *,
    messages: list[dict[str, Any]],
    trigger_reason: str,
    chat_intent_hint: str = "",
    use_builtin_search_hint: bool = False,
) -> str:
    """Run social-intelligence text generation through the full Agent path."""
    if ctx.tool_caller is None or ctx.tool_registry is None:
        return ""
    from ...core.agent_bridge import run_text_agent
    from ...core.social_surface_renderer import (
        OutputKind,
        SocialSurfaceRenderer,
        resolve_surface_spec,
    )

    surface, spec = resolve_surface_spec(trigger_reason)
    if not callable(ctx.load_prompt):
        ctx.logger.warning(f"[social] persona loader unavailable surface={surface}")
        return ""
    try:
        prompt_data = ctx.load_prompt(None)
    except Exception as exc:
        ctx.logger.warning(f"[social] load persona failed surface={surface}: {exc}")
        return ""
    renderer = SocialSurfaceRenderer(spec)
    projected_persona = renderer.project_persona(prompt_data)
    if not projected_persona:
        ctx.logger.warning(f"[social] empty persona projection surface={surface}")
        return ""
    messages = renderer.prepare_messages(
        messages,
        prompt_data=prompt_data,
        output_kind=OutputKind.PERSONA_TEXT,
    )

    return await run_text_agent(
        messages=messages,
        plugin_config=ctx.plugin_config,
        logger=ctx.logger,
        tool_caller=ctx.tool_caller,
        registry=ctx.tool_registry,
        max_steps=ctx.agent_max_steps,
        use_builtin_search_hint=use_builtin_search_hint,
        trigger_reason=trigger_reason,
        chat_intent_hint=chat_intent_hint or trigger_reason,
        surface=surface,
        output_kind=OutputKind.PERSONA_TEXT,
    )


@dataclass
class SocialTrigger:
    """单个主动社交场景的触发器配置。

    schedule_kind / schedule_args 决定 APScheduler 怎么注册：
    - kind="cron" + args={"hour": 8, "minute": 0}
    - kind="interval" + args={"minutes": 30}
    - kind="event" 则不由 scheduler 注册，由 message hook 在适当时机调
      handler（话题延续场景用）。
    """

    name: str
    handler: Callable[[SocialContext], Awaitable[None]]
    schedule_kind: str  # "cron" / "interval" / "event"
    schedule_args: dict = field(default_factory=dict)
    enabled: Callable[[Any], bool] = field(default=lambda _config: True)


_REGISTRY: dict[str, SocialTrigger] = {}


def register_social_trigger(trigger: SocialTrigger) -> None:
    """注册一个 SocialTrigger；重名会覆盖（便于热重载）。"""
    _REGISTRY[trigger.name] = trigger


def list_social_triggers() -> list[SocialTrigger]:
    return list(_REGISTRY.values())


def clear_social_triggers_for_testing() -> None:
    _REGISTRY.clear()


__all__ = [
    "SocialContext",
    "SocialTrigger",
    "dispatch_social_outbound",
    "run_social_text_agent",
    "register_social_trigger",
    "list_social_triggers",
    "clear_social_triggers_for_testing",
]
