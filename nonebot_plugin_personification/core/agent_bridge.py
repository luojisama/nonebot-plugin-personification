from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agent.runtime.runner import run_agent
from ..agent.tool_registry import ToolRegistry
from ..agent.query_rewriter import QueryRewriteContext
from .social_surface_renderer import (
    OutputKind,
    SocialSurfaceRenderer,
    SurfaceSpec,
    infer_surface_name,
    resolve_surface_spec,
)


TEXT_AGENT_TOOL_PROFILE_DEFAULT = "default"
TEXT_AGENT_TOOL_PROFILE_NONE = "none"
TEXT_AGENT_TOOL_PROFILE_QZONE_READ_ONLY = "qzone_read_only"
_QZONE_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "datetime",
        "game_info",
        "get_ai_news",
        "get_baike_entry",
        "get_daily_news",
        "get_epic_games",
        "get_exchange_rate",
        "get_gold_price",
        "get_history_today",
        "get_tech_news",
        "get_trending",
        "resolve_acg_entity",
        "search_web",
        "weather",
        "web_search",
        "wiki_lookup",
    }
)
_QZONE_TRUSTED_TOOL_SOURCES = frozenset({"builtin", "bundled"})


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


def clone_tool_registry(
    registry: Any,
    *,
    tool_profile: str = TEXT_AGENT_TOOL_PROFILE_DEFAULT,
) -> ToolRegistry:
    cloned = ToolRegistry()
    if registry is None:
        return cloned
    profile = str(tool_profile or TEXT_AGENT_TOOL_PROFILE_DEFAULT).strip().lower()
    if profile == TEXT_AGENT_TOOL_PROFILE_NONE:
        return cloned
    if profile not in {TEXT_AGENT_TOOL_PROFILE_DEFAULT, TEXT_AGENT_TOOL_PROFILE_QZONE_READ_ONLY}:
        raise ValueError(f"unsupported text Agent tool profile: {profile}")
    active = getattr(registry, "active", None)
    tools = active() if callable(active) else []
    for tool in tools:
        tool_name = str(getattr(tool, "name", "") or "")
        if profile == TEXT_AGENT_TOOL_PROFILE_QZONE_READ_ONLY:
            metadata = dict(getattr(tool, "metadata", None) or {})
            source_kind = str(metadata.get("source_kind", "") or "").strip().lower()
            side_effect = str(metadata.get("side_effect", "none") or "none").strip().lower()
            risk_level = str(metadata.get("risk_level", "low") or "low").strip().lower()
            parameters = getattr(tool, "parameters", None)
            root_type = str(parameters.get("type", "object") or "object").strip().lower() if isinstance(parameters, dict) else ""
            if (
                tool_name not in _QZONE_READ_ONLY_TOOL_NAMES
                or side_effect != "none"
                or risk_level in {"admin", "high"}
                or source_kind not in _QZONE_TRUSTED_TOOL_SOURCES
                or root_type != "object"
            ):
                continue
        cloned.register(tool)
    return cloned


def tool_schemas_for_profile(
    registry: Any,
    *,
    tool_profile: str,
) -> list[dict]:
    return clone_tool_registry(registry, tool_profile=tool_profile).openai_schemas()


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
    surface: str = "",
    output_kind: OutputKind | str | None = None,
    structured_output: bool = False,
    tool_profile: str = TEXT_AGENT_TOOL_PROFILE_DEFAULT,
) -> str:
    """Run the complete Agent loop for flows that only need a text result.

    This intentionally reuses ``run_agent`` instead of the lightweight loop, so
    intent inference, query rewriting, tool selection, evidence synthesis and
    semantic fallback stay aligned with live chat. Callers select an explicit
    tool profile when a non-chat surface requires a smaller capability set.
    """

    surface_name = infer_surface_name(surface, trigger_reason)
    surface_name, registered_spec = resolve_surface_spec(surface_name)
    if output_kind is None:
        resolved_output_kind = registered_spec.output_kind
    else:
        try:
            resolved_output_kind = output_kind if isinstance(output_kind, OutputKind) else OutputKind(str(output_kind).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unsupported text Agent output kind: {output_kind!r}") from exc
    if structured_output:
        if output_kind is not None and resolved_output_kind is not OutputKind.STRUCTURED_DECISION:
            raise ValueError("structured_output conflicts with output_kind")
        resolved_output_kind = OutputKind.STRUCTURED_DECISION
    renderer = SocialSurfaceRenderer(
        SurfaceSpec(
            output_kind=resolved_output_kind,
            persona_scope=registered_spec.persona_scope,
        )
    )
    logger = logger or _NullLogger()
    if not (
        getattr(plugin_config, "personification_agent_enabled", True)
        and tool_caller is not None
        and registry is not None
    ):
        return ""

    agent_messages = renderer.prepare_messages(messages)
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
        registry=clone_tool_registry(registry, tool_profile=tool_profile),
        tool_caller=tool_caller,
        executor=executor,
        plugin_config=plugin_config,
        logger=logger,
        max_steps=max_steps,
        query_rewrite_context=QueryRewriteContext(trigger_reason=trigger_reason),
        is_group=(
            True
            if registered_spec.persona_scope.value == "group_social"
            else None
            if registered_spec.persona_scope.value == "chat"
            else False
        ),
        surface="" if surface_name == "agent_bridge" else surface_name,
        finalize_quality=renderer.finalize_quality,
        structured_output=resolved_output_kind is OutputKind.STRUCTURED_DECISION,
        allow_builtin_search=tool_profile == TEXT_AGENT_TOOL_PROFILE_DEFAULT,
    )
    text = str(getattr(result, "text", "") or "")
    if (
        resolved_output_kind in {OutputKind.PERSONA_TEXT, OutputKind.DIRECT_ACTION}
        and text.strip() in {"[NO_REPLY]", "<NO_REPLY>", "[SILENCE]", "<SILENCE>"}
    ):
        return ""
    return renderer.finalize_text(text, logger=logger, surface=surface_name)


__all__ = [
    "TEXT_AGENT_TOOL_PROFILE_DEFAULT",
    "TEXT_AGENT_TOOL_PROFILE_NONE",
    "TEXT_AGENT_TOOL_PROFILE_QZONE_READ_ONLY",
    "OutputKind",
    "TextAgentExecutor",
    "clone_tool_registry",
    "run_text_agent",
    "tool_schemas_for_profile",
]
