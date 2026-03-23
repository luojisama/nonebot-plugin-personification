from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional


HookPhase = Literal[
    "preprocess",
    "system_prelude",
    "system_context",
    "system_postlude",
    "message",
]


@dataclass
class HookContext:
    """
    Prompt 注入钩子的上下文快照。

    允许钩子在不触碰 reply_processor 细节的情况下读取状态，
    并在少数需要保留旧行为的场景下直接调整 messages / message_content / trigger_reason。
    """

    user_id: str
    user_name: str
    group_id: str
    is_private: bool
    is_random_chat: bool
    is_yaml_mode: bool
    is_group_idle_active: bool
    group_idle_topic: str
    has_image_input: bool
    message_text: str
    message_content: str
    trigger_reason: str
    current_time_str: str
    session_messages: list[dict]
    messages: list[dict]
    plugin_config: Any
    session: Any
    persona: Any
    runtime: Any
    bot: Any
    event: Any


PromptHook = Callable[[HookContext], Awaitable[Optional[str]]]


@dataclass
class _HookEntry:
    name: str
    hook: PromptHook
    priority: int
    phase: HookPhase = "system_context"


class PromptHookRegistry:
    """
    全局 Prompt 注入钩子注册表。

    - 按 phase + priority 执行
    - 单个钩子异常时静默降级，不影响其它钩子
    """

    def __init__(self) -> None:
        self._entries: list[_HookEntry] = []

    def register(
        self,
        name: str,
        hook: PromptHook,
        priority: int = 50,
        phase: HookPhase = "system_context",
    ) -> None:
        self._entries.append(_HookEntry(name=name, hook=hook, priority=priority, phase=phase))
        self._entries.sort(key=lambda entry: (entry.phase, entry.priority, entry.name))

    async def run_all(self, ctx: HookContext, *, phase: HookPhase = "system_context") -> list[str]:
        results: list[str] = []
        for entry in self._entries:
            if entry.phase != phase:
                continue
            try:
                chunk = await entry.hook(ctx)
                if chunk and chunk.strip():
                    results.append(chunk.strip())
            except Exception as e:
                logging.getLogger("personification.hooks").warning(
                    f"[prompt_hook] '{entry.name}' failed: {e}"
                )
        return results


_registry = PromptHookRegistry()


def get_hook_registry() -> PromptHookRegistry:
    return _registry


def register_prompt_hook(
    name: str,
    hook: PromptHook,
    priority: int = 50,
    phase: HookPhase = "system_context",
) -> None:
    _registry.register(name, hook, priority=priority, phase=phase)
