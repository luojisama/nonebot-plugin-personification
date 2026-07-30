from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from ...agent.action_executor import ActionExecutor
from ...agent.loop import run_agent
from ...agent.query_rewriter import QueryRewriteContext
from ...agent.tool_registry import ToolRegistry

from ...core.context_policy import build_prompt_injection_guard
from ...core.error_utils import log_exception
from ...core.image_input import provider_supports_vision
from ...core.memory_defaults import DEFAULT_PRIVATE_HISTORY_TURNS, MAX_PRIVATE_HISTORY_TURNS
from ...core.message_parts import build_user_message_content
from ...core.message_relations import build_event_relation_metadata
from ...core.persona_profile import load_persona_profile, render_persona_snapshot
from ...core.prompt_loader import pick_ack_phrase
from ...core.qq_expression_tools import qq_action_history_text, register_send_qq_expression_tools
from ...core.gemini_profile import build_gemini_route_policy_prompt
from ...core.reply_text_policy import normalize_visible_reply_text
from ...core.reply_style_policy import build_reply_style_policy_prompt
from ...core.visual_capabilities import VISUAL_ROUTE_REPLY_PLAIN
from ...skill_runtime.runtime_api import SkillRuntime
from ...skills.skillpacks.friend_request_tool.scripts.main import build_friend_request_tool_for_runtime
from ...skills.skillpacks.group_info_tool.scripts.main import build_group_info_tool_for_runtime
from ...skills.skillpacks.plugin_invoker.scripts.main import build_invoke_plugin_tool_for_runtime
from ...skills.skillpacks.resource_collector.scripts.main import build_send_image_tools
from ...skills.skillpacks.sticker_tool.scripts.impl import build_send_sticker_tool


_FRIEND_IDS_CACHE: Dict[str, tuple[float, set[str]]] = {}
_IMAGE_CLASSIFY_CACHE: Dict[str, IncomingImageClassification] = {}
_IMAGE_CLASSIFY_CACHE_MAX = 256
_DEFAULT_RESPONSE_TIMEOUT_SECONDS = 180.0
_AGENT_TIME_BUDGET_RESERVE_SECONDS = 30.0


@dataclass(frozen=True)
class IncomingImageClassification:
    kind: str = "sticker"
    confidence: float = 0.0
    reason: str = ""
    source: str = "fallback"

    @property
    def is_sticker_like(self) -> bool:
        return self.kind != "photo"

    @property
    def text_label(self) -> str:
        return "[图片·表情包]" if self.is_sticker_like else "[图片·照片]"


def _parse_image_classifier_payload(raw: Any) -> IncomingImageClassification | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind", "") or "").strip().lower()
    if kind not in {"photo", "sticker"}:
        return None
    try:
        confidence = float(payload.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return IncomingImageClassification(
        kind=kind,
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(payload.get("reason", "") or "").strip(),
        source="lite_classifier",
    )


def _parse_image_classifier_kind(raw: Any) -> str | None:
    parsed = _parse_image_classifier_payload(raw)
    if parsed is not None:
        return parsed.kind
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if re.search(r"\bphoto\b", text):
        return "photo"
    if re.search(r"\bsticker\b", text):
        return "sticker"
    return None


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def clear_image_classify_cache() -> None:
    _IMAGE_CLASSIFY_CACHE.clear()


def _latest_user_text_for_agent_memory(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(list(messages or [])):
        if str(message.get("role", "") or "") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text[:800]
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = str(item.get("text", "") or "").strip()
                    if text:
                        parts.append(text)
            merged = " ".join(parts).strip()
            if merged:
                return merged[:800]
    return ""


async def _recall_agent_candidate_memories(
    *,
    runtime: Any,
    event: Any,
    messages: List[Dict[str, Any]],
    turn_plan: Any = None,
) -> list[dict[str, Any]]:
    if not bool(getattr(runtime.plugin_config, "personification_evidence_synthesizer_enabled", False)):
        return []
    memory_store = getattr(runtime, "memory_store", None)
    if memory_store is None or not bool(getattr(runtime.plugin_config, "personification_memory_palace_enabled", False)):
        return []
    memory_need = str(getattr(turn_plan, "memory_need", "") or "").strip()
    if memory_need == "none":
        return []
    query = _latest_user_text_for_agent_memory(messages)
    if not query:
        return []
    group_id = str(getattr(event, "group_id", "") or "").strip()
    user_id = str(getattr(event, "user_id", "") or "").strip()
    mode = "deep" if memory_need == "deep" else "auto"
    try:
        return await asyncio.to_thread(
            memory_store.recall_memories,
            query=query,
            scope="auto",
            user_id=user_id,
            group_id=group_id,
            limit=12,
            mode=mode,
            context_type="group" if group_id else "private",
        )
    except Exception as exc:
        runtime.logger.debug(f"[agent] evidence memory recall failed: {exc}")
        return []


def _remember_image_classification(
    cache_key: str,
    classification: IncomingImageClassification,
) -> IncomingImageClassification:
    key = str(cache_key or "").strip()
    if not key:
        return classification
    if key in _IMAGE_CLASSIFY_CACHE:
        _IMAGE_CLASSIFY_CACHE.pop(key, None)
    elif len(_IMAGE_CLASSIFY_CACHE) >= _IMAGE_CLASSIFY_CACHE_MAX:
        _IMAGE_CLASSIFY_CACHE.pop(next(iter(_IMAGE_CLASSIFY_CACHE)), None)
    _IMAGE_CLASSIFY_CACHE[key] = classification
    return classification


def _get_cached_image_classification(cache_key: str) -> IncomingImageClassification | None:
    key = str(cache_key or "").strip()
    if not key:
        return None
    cached = _IMAGE_CLASSIFY_CACHE.get(key)
    if cached is None:
        return None
    _IMAGE_CLASSIFY_CACHE.pop(key, None)
    _IMAGE_CLASSIFY_CACHE[key] = cached
    return IncomingImageClassification(
        kind=cached.kind,
        confidence=cached.confidence,
        reason=cached.reason,
        source="cache",
    )


def _image_classifier_callers(runtime: Any) -> list[tuple[str, Any]]:
    callers: list[tuple[str, Any]] = []
    seen: set[int] = set()
    for label in ("lite_tool_caller", "agent_tool_caller"):
        caller = getattr(runtime, label, None)
        if caller is None:
            continue
        marker = id(caller)
        if marker in seen:
            continue
        seen.add(marker)
        callers.append((label, caller))
    return callers


def _runtime_plugin_config(runtime: Any) -> Any:
    return getattr(runtime, "plugin_config", None)


def _lite_route_supports_vision(runtime: Any) -> bool:
    plugin_config = _runtime_plugin_config(runtime)
    lite_model = str(getattr(plugin_config, "personification_lite_model", "") or "").strip()
    if not lite_model:
        return primary_route_supports_vision(runtime, VISUAL_ROUTE_REPLY_PLAIN)
    api_type, _primary_model = get_primary_provider_signature(runtime)
    return provider_supports_vision(
        api_type,
        lite_model,
        route_name=VISUAL_ROUTE_REPLY_PLAIN,
    )


def _classifier_caller_supports_vision(runtime: Any, caller_label: str) -> bool:
    if caller_label == "lite_tool_caller":
        return _lite_route_supports_vision(runtime)
    return primary_route_supports_vision(runtime, VISUAL_ROUTE_REPLY_PLAIN)


async def classify_incoming_image(
    *,
    runtime: Any,
    image_url: str,
    source_kind: str = "",
    width: int = 0,
    height: int = 0,
    file_id: str = "",
) -> IncomingImageClassification:
    normalized_source_kind = str(source_kind or "").strip().lower()
    normalized_url = str(image_url or "").strip().lower()
    image_width = _int_or_zero(width)
    image_height = _int_or_zero(height)

    if normalized_source_kind == "mface":
        return IncomingImageClassification(
            kind="sticker",
            confidence=1.0,
            reason="mface_short_circuit",
            source="rule",
        )
    if ".gif" in normalized_url or "anim" in normalized_url:
        return IncomingImageClassification(
            kind="sticker",
            confidence=1.0,
            reason="gif_short_circuit",
            source="rule",
        )
    if image_width == 0 and image_height == 0:
        return IncomingImageClassification(
            kind="sticker",
            confidence=1.0,
            reason="missing_size_short_circuit",
            source="rule",
        )

    cache_key = str(file_id or "").strip() or f"{image_width}x{image_height}"
    cached = _get_cached_image_classification(cache_key)
    if cached is not None:
        return cached

    conservative_fallback = IncomingImageClassification(
        kind="sticker",
        confidence=0.0,
        reason="classifier_fallback",
        source="fallback",
    )
    prompt = (
        "你是聊天场景图片分类器。"
        "只输出一个词：sticker 或 photo。"
        "只有明确属于现实世界相机拍摄的人像、物体、风景或现场照片时才输出 photo。"
        "表情包、贴图、梗图、动漫插画、漫画、游戏截图、聊天截图、海报、配字图一律输出 sticker。"
        "拿不准时务必输出 sticker。"
    )
    request_messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": build_user_message_content(
                text="这是表情包/贴图还是真实拍摄的照片？",
                image_urls=[image_url],
                image_detail="low",
            ),
        },
    ]
    attempted_vision_classify = False
    for caller_label, caller in _image_classifier_callers(runtime):
        if not _classifier_caller_supports_vision(runtime, caller_label):
            continue
        attempted_vision_classify = True
        try:
            response = await caller.chat_with_tools(
                messages=request_messages,
                tools=[],
                use_builtin_search=False,
            )
        except Exception as exc:
            log_exception(
                runtime.logger,
                f"[reply_processor] incoming image classify failed via {caller_label}",
                exc,
                level="debug",
            )
            continue
        if bool(getattr(response, "vision_unavailable", False)):
            continue
        parsed_kind = _parse_image_classifier_kind(getattr(response, "content", "") or "")
        if parsed_kind is not None:
            return _remember_image_classification(
                cache_key,
                IncomingImageClassification(
                    kind=parsed_kind,
                    confidence=0.9 if parsed_kind == "photo" else 0.8,
                    reason="llm_classifier",
                    source=caller_label,
                ),
            )

    if attempted_vision_classify:
        return _remember_image_classification(cache_key, conservative_fallback)

    size_based_kind = "sticker" if max(image_width, image_height) <= 1280 else "photo"
    return _remember_image_classification(
        cache_key,
        IncomingImageClassification(
            kind=size_based_kind,
            confidence=0.35,
            reason="size_fallback",
            source="size_fallback",
        ),
    )

def private_history_window_limit(plugin_config: Any) -> int:
    raw = getattr(plugin_config, "personification_private_history_turns", DEFAULT_PRIVATE_HISTORY_TURNS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_PRIVATE_HISTORY_TURNS
    return max(12, min(value, MAX_PRIVATE_HISTORY_TURNS))


def build_group_session_relation_metadata(
    event: Any,
    *,
    bot_self_id: str,
    group_id: str,
    user_id: str,
    source_kind: str,
) -> dict[str, Any]:
    relation_metadata = build_event_relation_metadata(
        event,
        bot_self_id=bot_self_id,
        source_kind=source_kind,
    )
    relation_metadata["group_id"] = str(group_id)
    relation_metadata["user_id"] = str(user_id)
    return relation_metadata


def build_tts_user_hint(*, is_private: bool, group_style: str = "") -> str:
    scene = "私聊" if is_private else "群聊"
    hint = f"这是{scene}场景下的回复，请自然朗读，整体语速略快一点。"
    style = str(group_style or "").strip()
    if style:
        hint += f" 参考群聊风格：{style[:80]}"
    return hint


def looks_like_sticker_message(text: str) -> bool:
    plain = str(text or "")
    return (
        "[图片·表情包]" in plain
        or "[表情id:" in plain
        or "[表情:" in plain
        or "[QQ表情" in plain
        or "[QQ超级表情" in plain
        or "[QQ收藏表情" in plain
        or "[QQ推荐表情" in plain
        or "[表情包]" in plain
        or "[多张表情]" in plain
    )


def looks_like_photo_message(text: str) -> bool:
    return "[图片·照片]" in str(text or "")


# 单个表情占位符的形态（incoming sticker placeholder）。
_STICKER_PLACEHOLDER_ALT = (
    r"\[图片·表情包\]"
    r"|\[对方发送了一个表情包(?:：[^\]]*)?\]"
    r"|\[QQ表情[^\]]*\]"
    r"|\[QQ超级表情[^\]]*\]"
    r"|\[QQ收藏表情[^\]]*\]"
    r"|\[QQ推荐表情[^\]]*\]"
    r"|\[QQ表情id:[^\]]*\]"
    r"|\[表情包\]"
    r"|\[表情:[^\]]*\]"
    r"|\[表情id:[^\]]*\]"
)
# 连续 2+ 个表情占位符（中间可夹空白/分隔符）——批量刷表情时出现。
_STICKER_RUN_RE = re.compile(
    rf"(?:{_STICKER_PLACEHOLDER_ALT})(?:\s*(?:{_STICKER_PLACEHOLDER_ALT}))+"
)


def fold_consecutive_sticker_placeholders(text: str) -> str:
    """把"一连串表情占位符"折叠成单个中性标记。

    多人/连续刷表情时，喂给模型的消息文本会出现 N 个相邻的 `[图片·表情包]` 等占位符，
    模型看到"一张接一张"的信号容易被勾出"怎么发个没完"之类的吐槽。这里在源头把连续
    占位符折叠成一个中性的 `[多张表情]`，消除数量/连续性线索（单个表情保持原样）。
    """
    plain = str(text or "")
    if not plain:
        return plain
    return _STICKER_RUN_RE.sub("[多张表情]", plain)


def get_primary_provider_signature(runtime: Any) -> tuple[str, str]:
    provider_getter = getattr(runtime, "get_configured_api_providers", None)
    providers = provider_getter() if callable(provider_getter) else []
    primary_api_type = ""
    primary_model = ""
    if providers:
        primary_api_type = str(providers[0].get("api_type", "") or "").strip().lower()
        primary_model = str(providers[0].get("model", "") or "").strip()
    if not primary_api_type:
        primary_api_type = str(
            getattr(_runtime_plugin_config(runtime), "personification_api_type", "") or ""
        ).strip().lower()
    if not primary_model:
        primary_model = str(
            getattr(_runtime_plugin_config(runtime), "personification_model", "") or ""
        ).strip()
    return primary_api_type, primary_model


def primary_route_supports_vision(runtime: Any, route_name: str) -> bool:
    provider_getter = getattr(runtime, "get_configured_api_providers", None)
    providers = list(provider_getter() or []) if callable(provider_getter) else []
    if providers:
        return any(
            provider_supports_vision(
                str(provider.get("api_type", "") or "").strip().lower(),
                str(provider.get("model", "") or "").strip(),
                route_name=route_name,
            )
            for provider in providers
            if isinstance(provider, dict)
        )
    api_type, model = get_primary_provider_signature(runtime)
    return provider_supports_vision(api_type, model, route_name=route_name)


def strip_injected_visual_summary(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    return re.sub(r"\[(?:图片视觉描述|表情包语义|动态表情语义|媒体语义)（系统注入[^）]*）[：:].*?\]", "", raw).strip()


def stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                parts.append(str(item.get("text")))
        return "".join(parts)
    return str(content or "")


def count_user_interactions(messages: List[Dict[str, Any]], user_id: str) -> int:
    marker = f"({user_id})"
    count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content_text = stringify_message_content(message.get("content", ""))
        if marker in content_text:
            count += 1
    return count


def restore_current_user_message_content(
    session_messages: List[Dict[str, Any]],
    current_user_content: Any,
) -> List[Dict[str, Any]]:
    restored = [dict(message) for message in session_messages]
    for index in range(len(restored) - 1, -1, -1):
        if str(restored[index].get("role", "") or "").strip() != "user":
            continue
        updated = dict(restored[index])
        updated["content"] = current_user_content
        restored[index] = updated
        break
    return restored


def normalize_reply_key(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized[:80]


def extract_reply_sender_meta(reply: Any) -> tuple[str, bool]:
    sender = getattr(reply, "sender", None)
    if sender is None and isinstance(reply, dict):
        sender = reply.get("sender")

    sender_name = ""
    sender_id = ""
    if isinstance(sender, dict):
        sender_name = str(sender.get("card") or sender.get("nickname") or "").strip()
        sender_id = str(sender.get("user_id") or "").strip()
    elif sender is not None:
        sender_name = str(
            getattr(sender, "card", None) or getattr(sender, "nickname", None) or ""
        ).strip()
        sender_id = str(getattr(sender, "user_id", "") or "").strip()

    if not sender_name:
        sender_name = str(
            getattr(reply, "sender_name", None)
            or (reply.get("sender_name") if isinstance(reply, dict) else "")
            or sender_id
        ).strip()

    self_id = str(getattr(reply, "self_id", "") or "").strip()
    if not self_id and isinstance(reply, dict):
        self_id = str(reply.get("self_id", "") or "").strip()

    is_bot_reply = bool(self_id and sender_id and self_id == sender_id)
    return sender_name or "未知", is_bot_reply


def should_suppress_group_topic_loop(
    reply_content: str,
    session_messages: List[Dict[str, Any]],
) -> bool:
    reply_key = normalize_reply_key(reply_content)
    if not reply_key:
        return False

    recent_assistant = [
        normalize_reply_key(stringify_message_content(msg.get("content", "")))
        for msg in session_messages[-10:]
        if isinstance(msg, dict) and msg.get("role") == "assistant"
    ]
    recent_assistant = [key for key in recent_assistant if key]
    if len(recent_assistant) >= 2 and reply_key in recent_assistant[-2:]:
        return True
    if len(recent_assistant) >= 3 and recent_assistant[-1] == recent_assistant[-2] == reply_key:
        return True
    return False


def batch_has_newer_messages(state: Dict[str, Any]) -> bool:
    runtime_ref = state.get("batch_runtime_ref")
    if not isinstance(runtime_ref, dict):
        return False
    entry = runtime_ref.get("entry")
    generation = int(runtime_ref.get("generation", 0) or 0)
    if not isinstance(entry, dict):
        return False
    if int(entry.get("current_generation", 0) or 0) != generation:
        return True
    if int(entry.get("superseded_generation", 0) or 0) >= generation:
        return True
    return bool(entry.get("newer_batch_for_current"))


def should_use_agent_for_reply(
    *,
    plugin_config: Any,
    tool_registry: Any,
    agent_tool_caller: Any,
    message_intent: str,
    ambiguity_level: str = "",
    is_direct_mention: bool = False,
    has_image_input: bool,
) -> bool:
    _ = message_intent, ambiguity_level, is_direct_mention, has_image_input
    return bool(
        getattr(plugin_config, "personification_agent_enabled", True)
        and tool_registry
        and agent_tool_caller
    )


def compute_agent_time_budget(
    *,
    started_at: float | None,
    total_timeout_seconds: float = _DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    reserve_seconds: float = _AGENT_TIME_BUDGET_RESERVE_SECONDS,
) -> float | None:
    available = float(total_timeout_seconds) - float(reserve_seconds)
    if started_at is not None:
        available -= max(0.0, time.monotonic() - float(started_at))
    return max(0.0, available)


def clone_tool_registry(registry: ToolRegistry) -> ToolRegistry:
    cloned = ToolRegistry()
    for tool in registry.active():
        cloned.register(tool)
    return cloned


async def get_cached_friend_ids(bot: Any, logger: Any, ttl_seconds: int = 300) -> set[str]:
    cache_key = str(getattr(bot, "self_id", "") or "default")
    now_ts = time.time()
    cached = _FRIEND_IDS_CACHE.get(cache_key)
    if cached and now_ts - cached[0] < ttl_seconds:
        return set(cached[1])

    friend_ids: set[str] = set()
    try:
        friends = await bot.get_friend_list()
        if isinstance(friends, list):
            for item in friends:
                if isinstance(item, dict) and item.get("user_id") is not None:
                    friend_ids.add(str(item.get("user_id")))
    except Exception as e:
        log_exception(
            logger,
            "[reply_processor] get_friend_list failed",
            e,
            level="debug",
        )

    _FRIEND_IDS_CACHE[cache_key] = (now_ts, set(friend_ids))
    return friend_ids


async def run_agent_if_enabled(
    *,
    bot: Any,
    event: Any,
    messages: List[Dict[str, Any]],
    persona: Any,
    runtime: Any,
    interaction_count: int = 0,
    current_image_urls: List[str] | None = None,
    trigger_reason: str = "",
    direct_image_input: bool = False,
    repeat_clusters: list[dict[str, Any]] | None = None,
    relationship_hint: str = "",
    recent_bot_replies: list[str] | None = None,
    precomputed_intent: Any = None,
    turn_plan: Any = None,
    started_at: float | None = None,
    is_direct_mention: bool = False,
    response_timeout_seconds: float = _DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    task_exc_logger: Callable[[str, Any], Any] | None = None,
) -> tuple[str | None, bool, bool]:
    if not (
        getattr(runtime.plugin_config, "personification_agent_enabled", True)
        and runtime.tool_registry
        and runtime.agent_tool_caller
    ):
        return None, False, False

    executor = ActionExecutor(bot, event, runtime.plugin_config, runtime.logger)
    runtime_registry = clone_tool_registry(runtime.tool_registry)
    register_send_qq_expression_tools(
        runtime_registry,
        executor=executor,
        bot=bot,
        plugin_config=runtime.plugin_config,
    )
    try:
        skill_runtime_for_images = SkillRuntime(
            plugin_config=runtime.plugin_config,
            logger=runtime.logger,
            get_now=lambda: int(time.time()),
            vision_caller=getattr(runtime, "vision_caller", None),
            tool_caller=runtime.agent_tool_caller,
        )
        for tool in build_send_image_tools(skill_runtime_for_images, executor):
            runtime_registry.register(tool)
    except Exception as exc:
        runtime.logger.debug(f"拟人插件：注册联网搜图发送工具失败: {exc}")
    try:
        from ...core.sticker_library import resolve_sticker_dir

        sticker_dir = resolve_sticker_dir(getattr(runtime.plugin_config, "personification_sticker_path", None))
        if sticker_dir.exists() and sticker_dir.is_dir():
            runtime_registry.register(
                build_send_sticker_tool(
                    sticker_dir,
                    runtime.plugin_config,
                    executor,
                )
            )
    except Exception as exc:
        runtime.logger.debug(f"拟人插件：注册本地表情包发送工具失败: {exc}")
    friend_ids = await get_cached_friend_ids(bot, runtime.logger)
    skill_runtime = SkillRuntime(
        plugin_config=runtime.plugin_config,
        logger=runtime.logger,
        get_now=lambda: int(time.time()),
        get_whitelisted_groups=runtime.get_whitelisted_groups,
        knowledge_store=runtime.knowledge_store,
        background_intelligence=runtime.background_intelligence,
    )
    runtime_registry.register(
        build_group_info_tool_for_runtime(
            bot=bot,
            runtime=skill_runtime,
        )
    )
    runtime_registry.register(
        build_friend_request_tool_for_runtime(
            bot=bot,
            runtime=skill_runtime,
            get_user_data=persona.get_user_data,
            get_friend_ids=lambda: set(friend_ids),
            session_interaction_count=interaction_count,
            is_group_scene=hasattr(event, "group_id") and not str(getattr(event, "group_id", "")).startswith("private_"),
        )
    )
    if (
        bool(getattr(runtime.plugin_config, "personification_plugin_invoker_enabled", False))
        and runtime.knowledge_store is not None
    ):
        runtime_registry.register(
            build_invoke_plugin_tool_for_runtime(
                bot=bot,
                event=event,
                runtime=skill_runtime,
            )
        )
    ack_phrase = ""
    if is_direct_mention:
        group_id = str(getattr(event, "group_id", "") or "").strip() or None
        ack_phrase = pick_ack_phrase(
            runtime.plugin_config,
            persona.get_group_config,
            runtime.logger,
            group_id=group_id,
        )
    ack_sender = None
    if ack_phrase:
        async def _ack_sender(text: str, *, _phrase: str = ack_phrase) -> None:
            await bot.send(event, str(text or "").strip() or _phrase)
        ack_sender = _ack_sender
    candidate_memories = await _recall_agent_candidate_memories(
        runtime=runtime,
        event=event,
        messages=messages,
        turn_plan=turn_plan,
    )
    try:
        profile_service = getattr(runtime, "profile_service", None)
        if profile_service is not None:
            _gid = str(getattr(event, "group_id", "") or "")
            _uid = str(getattr(event, "user_id", "") or "")
            if _uid:
                _block = profile_service.build_prompt_block(user_id=_uid, group_id=_gid)
                if _block:
                    messages.append({"role": "system", "content": _block})
    except Exception:
        pass
    # 注入群风格摘要（最近一次 group_style_autobuild 产出）
    try:
        _gid = str(getattr(event, "group_id", "") or "")
        if _gid:
            from ...core.group_style_autobuild import get_latest_style_text

            _style_text = get_latest_style_text(_gid)
            if _style_text:
                messages.append({
                    "role": "system",
                    "content": f"## 本群风格画像\n{_style_text}\n\n回复时请贴合该群的语气、节奏和用语习惯。",
                })
    except Exception:
        pass
    result = await run_agent(
        messages=messages,
        registry=runtime_registry,
        tool_caller=runtime.agent_tool_caller,
        executor=executor,
        plugin_config=runtime.plugin_config,
        logger=runtime.logger,
        max_steps=getattr(runtime.plugin_config, "personification_agent_max_steps", 10),
        current_image_urls=current_image_urls,
        direct_image_input=direct_image_input,
        query_rewrite_context=QueryRewriteContext(
            trigger_reason=trigger_reason,
            images=list(current_image_urls or []),
        ),
        repeat_clusters=list(repeat_clusters or []),
        relationship_hint=relationship_hint,
        recent_bot_replies=list(recent_bot_replies or []),
        precomputed_intent=precomputed_intent,
        turn_plan=turn_plan,
        candidate_memories=candidate_memories,
        time_budget_seconds=compute_agent_time_budget(
            started_at=started_at,
            total_timeout_seconds=response_timeout_seconds,
        ),
        ack_sender=ack_sender,
    )
    action_history_parts: list[str] = []
    for action in result.pending_actions:
        await executor.execute(action["type"], action["params"])
        history_text = qq_action_history_text(action)
        if history_text:
            action_history_parts.append(history_text)
    if action_history_parts:
        setattr(event, "_personification_pending_action_history_text", " ".join(action_history_parts))
    return result.text, True, bool(getattr(result, "bypass_length_limits", False))


def stale_reply_abort_reason(state: Dict[str, Any]) -> str:
    runtime_ref = state.get("batch_runtime_ref")
    if not isinstance(runtime_ref, dict):
        return ""
    entry = runtime_ref.get("entry")
    generation = int(runtime_ref.get("generation", 0) or 0)
    if not isinstance(entry, dict):
        return ""
    current_generation = int(entry.get("current_generation", 0) or 0)
    if int(entry.get("superseded_generation", 0) or 0) >= generation:
        return f"会话 {state.get('batch_session_key', '') or '当前会话'} 当前批次已被新的直呼消息抢占，旧回复丢弃。"
    if current_generation != generation:
        return f"会话 {state.get('batch_session_key', '') or '当前会话'} 已切换到更新批次，本轮旧回复丢弃。"
    if not bool(entry.get("newer_batch_for_current")):
        return ""
    return f"会话 {state.get('batch_session_key', '') or '当前会话'} 已出现更新批次，本轮旧回复丢弃。"


def truncate_at_punctuation(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    candidate = text[:max_chars]
    for i in range(len(candidate) - 1, max(0, len(candidate) - 60), -1):
        if candidate[i] in "。！？!?\n":
            return candidate[: i + 1]
    for i in range(len(candidate) - 1, max(0, len(candidate) - 30), -1):
        if candidate[i] in "，；,;":
            return candidate[: i + 1]
    return candidate


def build_final_visible_reply_text(
    reply_content: str,
    *,
    max_chars: int,
    sanitize_history_text: Callable[[str], str],
) -> str:
    """
    统一计算最终写回 session/history 的 assistant 文本。

    这里显式复用发送前的裁剪结果，确保「用户实际看到/听到的文本」
    与下一轮模型读到的 assistant 历史保持一致。
    """
    final_reply = normalize_visible_reply_text(reply_content)
    if max_chars and max_chars > 0 and len(final_reply) > max_chars:
        final_reply = truncate_at_punctuation(final_reply, max_chars)
    return sanitize_history_text(final_reply)


def build_base_system_prompt(
    *,
    base_prompt: str,
    user_name: str,
    level_name: str,
    combined_attitude: str,
    emotion_block: str,
    is_private_session: bool,
    prelude_chunks: List[str],
    context_chunks: List[str],
    postlude_chunks: List[str],
    plugin_summary: str = "",
    has_visual_context: bool = False,
    has_video_context: bool = False,
    photo_like: bool = False,
    primary_api_type: str = "",
    primary_model: str = "",
    native_search_enabled: bool = False,
) -> str:
    profile = load_persona_profile(base_prompt)
    parts: List[str] = [base_prompt if isinstance(base_prompt, str) else ""]
    parts.append(render_persona_snapshot(profile))
    parts.extend(chunk for chunk in prelude_chunks if chunk)
    parts.append(
        "## 当前对话环境\n"
        f"- 对方昵称：{user_name}\n"
        f"- 对方好感等级：{level_name}\n"
        f"- 你的互动倾向：{combined_attitude}"
    )
    if emotion_block:
        parts.append(emotion_block)
    if plugin_summary:
        parts.append(f"[已安装插件摘要（仅供参考）]\n{str(plugin_summary).strip()}")
    if is_private_session:
        parts.append(
            "## 私聊规则\n"
            "1. 私聊里也要像真人聊天，不要像客服或助手。\n"
            "2. 对重复、无意义或你不想接的话，直接输出 [SILENCE]。\n"
            "3. 私聊不要只是一问一答；默认用自然口语聊开，通常 2-4 句更合适。\n"
            "4. 如果对方正在认真继续聊，优先顺手追问一句、接住情绪，或抛一个相关小话题，别每轮都急着收尾。\n"
            "5. 必须用“你”称呼当前对象，禁止使用群聊式称呼。\n"
            "6. 如果最新消息只是“在吗/还在吗/有人吗”这类心跳，只回应当前问候，不要延续旧话题补答。"
        )
    else:
        parts.append(
            "## 群聊规则（高优先级）\n"
            "1. 你是群成员，不是助手；回复要像群里顺手接一句。\n"
            "2. 优先短句、口语、接梗、吐槽，不要总结、说教、安抚式展开，也不要用问句把话题丢回群里。\n"
            "3. 只有明显无关、会打断别人、刚说过类似内容，或高歧义且没人 cue 你时才输出 [SILENCE]。\n"
            "4. 如果别人明显在顺着你上一句接话，优先自然续聊 1-2 轮，不要立刻冷掉。\n"
            "5. 除非被直接问到，不要写成长篇说明，不要把一句话说成教程。\n"
            "6. 遇到梗、复读、空耳、调侃时，先顺着气氛接一句；不要把笑点解释成“这个梗是怎么构成的”。\n"
            "7. 除非对方明确在问出处或意思，否则不要用“像是把 X 玩成 Y 了”这种分析梗结构的句式。\n"
            "8. 如果最新消息只是“在吗/还在吗/有人吗”这类心跳，只回应当前问候，不要延续旧话题补答。\n"
            "9. 遇到可能是游戏/圈子黑话的词语，若上下文无法确认含义，不要按字面理解强行接话，优先沉默或等待更多上下文再参与。\n"
            "10. 回复时不要把对方说的话原样重复后加感叹（如“太真实了/太直球了”），直接接话即可。"
        )
    parts.append(
        build_reply_style_policy_prompt(
            has_visual_context=has_visual_context,
            photo_like=photo_like,
            is_group=not is_private_session,
        )
    )
    gemini_policy = build_gemini_route_policy_prompt(
        api_type=primary_api_type,
        model=primary_model,
        has_visual_context=has_visual_context,
        has_video_context=has_video_context,
        native_search_enabled=native_search_enabled,
    )
    if gemini_policy:
        parts.append(gemini_policy)
    parts.append(build_prompt_injection_guard())
    parts.extend(chunk for chunk in context_chunks if chunk)
    parts.append(
        "## 核心行动准则\n"
        "1. 保持自然口吻，拒绝模板化官腔和客服腔。\n"
        "2. 能用一句说完就别说两句。\n"
        "3. 玩梗场景先像群友接话，不要急着解释梗机制或复述笑点。\n"
        "4. 图片/GIF/表情包视觉信息是系统注入的内部语境，只帮助你理解上下文，不要复述给用户，也不作为攻击判定依据。\n"
        "5. [BLOCK] 仅作高风险标记参考，不要轻易触发。\n"
        "6. 不要为了迎合用户而确认不确定的事实；证据不足时直接说不确定或少说。\n"
        "7. 输出纯文本，禁止使用 markdown 格式（不要用 **加粗**、*斜体*、# 标题、- 列表符号、`代码块`等）。\n"
        "8. 收到贴图、表情包、GIF、截图或真实照片时，先把视觉信息当作内部语境理解；"
        "没人明确要求识别、翻译或说明时，不要主动评论、讲解、复述或总结图片/动图内容。"
        "表情包/梗图/GIF 只当作语气线索；真实照片也只用于判断情绪、关系和意图，最终回复接人和话题，不写成图片说明。"
        "如果只看到图片占位或没有视觉摘要，不要假装看懂，也不要泛泛问对方“看到什么”。"
        "当多位群友连续刷表情包时，理解为大家在用表情表达情绪/玩梗/附和，把它当成群里的情绪氛围；"
        "绝不要抱怨刷屏、说“看不过来”“怎么这么多表情”，也不要复述或统计表情数量。\n"
        "9. 有人让你“写一段/来一段/AI 一段/帮我写”对白、剧本、小作文、段子、歌词或角色扮演内容时，"
        "不要切换成写作工具去交付任务：不写前言铺垫（如“来一段…的氛围：”）、不写“角色：台词”式的多角色剧本、"
        "不写结尾点评或总结、不堆营业腔和网络黑话。可以用自己的人设口吻即兴接两三句、玩梗式轻轻带过，"
        "但绝不展开成长篇命题作文，也不要为此出戏、扮演成别的角色或换成别的说话风格。"
    )
    parts.extend(chunk for chunk in postlude_chunks if chunk)
    return "\n\n".join(part for part in parts if part)


def build_confidence_style_instruction(confidence: float, *, is_group: bool = False) -> str:
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 0.8:
        return ""
    if value >= 0.6:
        return (
            "\n[系统提示] 当前语义置信度中等。回复时用“我理解是…”“应该是…”这类留余地的口吻，"
            "不要把不确定的推断说死。"
        )
    if value >= 0.4:
        if is_group:
            return (
                "\n[系统提示] 当前语义置信度偏低。群聊里不要用短问句确认对象或上下文；"
                "能接就给一句保守短反应，不能接就输出 [NO_REPLY]。"
            )
        return (
            "\n[系统提示] 当前语义置信度偏低。优先先确认理解是否正确，例如用一句短问句确认对象或上下文；"
            "群聊里如果没人明确 cue 你，也可以 [NO_REPLY]。"
        )
    if is_group:
        return (
            "\n[系统提示] 当前语义置信度很低。群聊里默认不要硬插话，除非被直接 @ 或明确追问，否则输出 [NO_REPLY]。"
        )
    return "\n[系统提示] 当前语义置信度很低。先用一句话确认对方意思，不要硬猜。"


_SCENARIO_INSTRUCTIONS: dict[str, str] = {
    "casual_banter": (
        "\n[场景提示] 当前群友在互相调侃闲聊。"
        "不要认真科普或长篇大论；适合沉默、轻接一句、或短句吐槽。"
    ),
    "sarcasm_irony": (
        "\n[场景提示] 当前对话含有反讽/阴阳怪气语气。"
        "先识别语气再回应，不要把反讽当真回复；可以顺着接或沉默。"
    ),
    "argument": (
        "\n[场景提示] 当前群友在争吵或对立。"
        "不要站队任何一方；可以尝试降温，或者直接 [NO_REPLY] 不掺和。"
    ),
    "inside_joke": (
        "\n[场景提示] 当前对话涉及群内部梗或暗号。"
        "如果你不确定含义，不要硬解释；群聊里不要追问，优先沉默或等更多上下文。"
    ),
    "multi_thread": (
        "\n[场景提示] 群内多个话题同时进行。"
        "只回应与你被 @ 或直接相关的线程，不要把不同线程的内容混在一起总结。"
    ),
    "private_topic": (
        "\n[场景提示] 当前对话涉及个人隐私或敏感话题。"
        "不要主动记忆具体内容到长期存储；回复要克制，不追问细节。"
    ),
}


def build_scenario_instruction(scenario: str) -> str:
    normalized = str(scenario or "").strip().lower()
    return _SCENARIO_INSTRUCTIONS.get(normalized, "")


__all__ = [
    "batch_has_newer_messages",
    "build_base_system_prompt",
    "build_confidence_style_instruction",
    "build_scenario_instruction",
    "build_final_visible_reply_text",
    "build_group_session_relation_metadata",
    "build_tts_user_hint",
    "clear_image_classify_cache",
    "classify_incoming_image",
    "clone_tool_registry",
    "compute_agent_time_budget",
    "count_user_interactions",
    "extract_reply_sender_meta",
    "fold_consecutive_sticker_placeholders",
    "get_cached_friend_ids",
    "get_primary_provider_signature",
    "IncomingImageClassification",
    "looks_like_photo_message",
    "looks_like_sticker_message",
    "normalize_reply_key",
    "primary_route_supports_vision",
    "private_history_window_limit",
    "restore_current_user_message_content",
    "run_agent_if_enabled",
    "should_suppress_group_topic_loop",
    "should_use_agent_for_reply",
    "stale_reply_abort_reason",
    "stringify_message_content",
    "strip_injected_visual_summary",
    "truncate_at_punctuation",
]
