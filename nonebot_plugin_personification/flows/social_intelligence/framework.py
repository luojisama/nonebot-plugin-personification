"""SocialTrigger + Registry + SocialContext。

SocialTrigger 把"什么时候触发"（cron / interval / event）与"触发后做什么"
（handler）解耦；handler 通过 SocialContext 拿到运行所需的全部依赖
（bot 实例、tool_caller、persona_store、配置等），便于单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class SocialContext:
    """场景 handler 收到的运行时上下文。"""

    plugin_config: Any
    logger: Any
    get_bots: Callable[[], dict]
    get_whitelisted_groups: Callable[[], list[str]]
    tool_caller: Any  # 用于 LLM 决策与文案包装（建议传 lite_tool_caller）
    persona_store: Any
    data_dir: Any
    get_now: Callable[[], Any]


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
    "register_social_trigger",
    "list_social_triggers",
    "clear_social_triggers_for_testing",
]
