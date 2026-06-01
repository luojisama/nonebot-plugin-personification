from __future__ import annotations

from typing import Any, Callable

GEMINI_API_TYPES = {"gemini", "gemini_official", "gemini_cli", "antigravity_cli"}
DEFAULT_CONTEXT_TOKEN_BUDGET = 2000
GEMINI_CONTEXT_TOKEN_BUDGET = 6000
DEFAULT_CONTEXT_KEEP_RECENT = 6
GEMINI_CONTEXT_KEEP_RECENT = 10


def normalize_route_api_type(api_type: Any) -> str:
    return str(api_type or "").strip().lower().replace("-", "_")


def is_gemini_route(api_type: Any = "", model: Any = "") -> bool:
    api = normalize_route_api_type(api_type)
    model_text = str(model or "").strip().lower()
    return api in GEMINI_API_TYPES or "gemini" in model_text


def primary_route_signature(
    plugin_config: Any = None,
    get_configured_api_providers: Callable[[], list[dict[str, Any]]] | None = None,
) -> tuple[str, str]:
    providers: list[dict[str, Any]] = []
    if callable(get_configured_api_providers):
        try:
            providers = [
                item
                for item in list(get_configured_api_providers() or [])
                if isinstance(item, dict)
            ]
        except Exception:
            providers = []
    if providers:
        primary = providers[0]
        return (
            str(primary.get("api_type", "") or "").strip(),
            str(primary.get("model", "") or "").strip(),
        )
    return (
        str(getattr(plugin_config, "personification_api_type", "") or "").strip(),
        str(getattr(plugin_config, "personification_model", "") or "").strip(),
    )


def context_token_budget_for_route(api_type: Any = "", model: Any = "") -> int:
    if is_gemini_route(api_type, model):
        return GEMINI_CONTEXT_TOKEN_BUDGET
    return DEFAULT_CONTEXT_TOKEN_BUDGET


def context_keep_recent_for_route(api_type: Any = "", model: Any = "") -> int:
    if is_gemini_route(api_type, model):
        return GEMINI_CONTEXT_KEEP_RECENT
    return DEFAULT_CONTEXT_KEEP_RECENT


def _provider_supports_native_search(provider: dict[str, Any]) -> bool:
    if not is_gemini_route(provider.get("api_type", ""), provider.get("model", "")):
        return False
    return bool(provider.get("supports_native_search", True))


def has_gemini_native_search_route(
    plugin_config: Any = None,
    get_configured_api_providers: Callable[[], list[dict[str, Any]]] | None = None,
) -> bool:
    providers: list[dict[str, Any]] = []
    if callable(get_configured_api_providers):
        try:
            providers = [
                item
                for item in list(get_configured_api_providers() or [])
                if isinstance(item, dict)
            ]
        except Exception:
            providers = []
    if providers:
        return _provider_supports_native_search(providers[0])

    api_type, model = primary_route_signature(plugin_config)
    return is_gemini_route(api_type, model)


def should_enable_default_builtin_search(
    plugin_config: Any,
    get_configured_api_providers: Callable[[], list[dict[str, Any]]] | None = None,
) -> bool:
    if bool(getattr(plugin_config, "personification_model_builtin_search_enabled", False)):
        return True
    if not bool(getattr(plugin_config, "personification_builtin_search", True)):
        return False
    return has_gemini_native_search_route(
        plugin_config,
        get_configured_api_providers=get_configured_api_providers,
    )


def build_gemini_route_policy_prompt(
    *,
    api_type: Any = "",
    model: Any = "",
    has_visual_context: bool = False,
    has_video_context: bool = False,
    native_search_enabled: bool = False,
) -> str:
    if not is_gemini_route(api_type, model):
        return ""

    lines = [
        "## Gemini 路由优化（高优先级）",
        "- 当前主路由是 Gemini 系列；可以利用长上下文、广知识面和多模态能力，但回复仍保持当前人设本人，不要变成助手或百科口吻。",
        "- 先综合最新消息、关系语境、较早上下文摘要和多模态线索，再给自然短回复；不要输出推理过程、资料报告或工具说明。",
    ]
    if native_search_enabled:
        lines.append(
            "- 遇到时效性、版本、价格、攻略、新闻、出处、资源状态等事实问题时，优先用 Gemini 原生联网查询/检索结果核对；没有证据就说不确定，别编。"
        )
    else:
        lines.append("- 可利用较广知识面处理常识和背景，但对时效性事实不确定时要说明不确定，不要硬编。")
    if has_visual_context:
        lines.append("- 有图片输入时优先直接理解画面、OCR、截图界面和表情语气；除非对方明确要求，不要把它写成视觉分析报告。")
    if has_video_context:
        lines.append("- 有视频输入时按时间顺序理解动作、字幕/OCR、声音文字线索和关键帧变化；输出仍像聊天接话，不要写成视频报告。")
    lines.append("- 长上下文只用来保持连续性；以最新一轮为主，不要翻旧账或把旧话题强行拉回。")
    return "\n".join(lines)


__all__ = [
    "build_gemini_route_policy_prompt",
    "context_keep_recent_for_route",
    "context_token_budget_for_route",
    "has_gemini_native_search_route",
    "is_gemini_route",
    "normalize_route_api_type",
    "primary_route_signature",
    "should_enable_default_builtin_search",
]
