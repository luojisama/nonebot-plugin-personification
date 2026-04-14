import asyncio
import base64
import ipaddress
import json
import random
import re
import socket
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib.parse import urlparse

import httpx
from nonebot.exception import FinishedException
from PIL import Image

from ..agent.action_executor import ActionExecutor
from ..agent.loop import run_agent
from ..agent.tool_registry import ToolRegistry
from ..core.message_relations import build_event_relation_metadata, extract_send_message_id
from ..core.prompt_hooks import HookContext, get_hook_registry
from ..core.persona_profile import load_persona_profile, render_persona_snapshot
from ..core.target_inference import TARGET_OTHERS, infer_message_target
from ..core.tts_service import extract_persona_tts_config
from ..core.image_result_cache import (
    build_image_cache_key,
    get_cached_image_result,
    set_cached_image_result,
)
from ..skills.skillpacks.friend_request_tool.scripts.main import build_friend_request_tool_for_runtime
from ..skills.skillpacks.group_info_tool.scripts.main import build_group_info_tool_for_runtime
from ..skill_runtime.runtime_api import SkillRuntime
from ..skills.skillpacks.sticker_tool.scripts.impl import (
    UNDERSTAND_STICKER_PROMPT,
    reset_current_image_context,
    select_sticker,
    set_current_image_context,
)
from ..core.proactive_store import update_group_chat_active
from ..core.web_grounding import extract_forward_message_content
from ..utils import get_recent_group_msgs
from .event_rules import split_segment_if_long
from .runtime_commands import maybe_handle_superuser_natural_language_skill_install


_FRIEND_IDS_CACHE: Dict[str, tuple[float, set[str]]] = {}
_MAX_IMAGE_DOWNLOAD_BYTES = 8 * 1024 * 1024
_BLOCKED_IMAGE_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".corp")
_BLOCKED_IMAGE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
}
_FALLBACK_REPLIES = [
    "啊，我突然脑子有点空白...等一下再问我？",
    "这个问题我需要想想，稍后回你",
    "哦，刚才走神了，你刚说什么来着",
]


def _build_group_session_relation_metadata(
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


@dataclass
class SessionDeps:
    private_session_prefix: str
    looks_like_private_command: Callable[[str], bool]
    ensure_session_history: Callable[..., None]
    build_private_session_id: Callable[[str], str]
    build_group_session_id: Callable[[str], str]
    sanitize_session_messages: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]
    get_session_messages: Callable[[str], List[Dict[str, Any]]]
    append_session_message: Callable[..., None]
    sanitize_history_text: Callable[[str], str]
    build_private_anti_loop_hint: Callable[[List[Dict[str, Any]]], str]


@dataclass
class PersonaDeps:
    load_prompt: Callable[[str], Any]
    sign_in_available: bool
    get_user_data: Callable[[str], Dict[str, Any]]
    get_level_name: Callable[[float], str]
    update_user_data: Callable[..., None]
    get_group_config: Callable[[str], Dict[str, Any]]
    get_group_style: Callable[[str], str]
    favorability_attitudes: Dict[str, str]
    get_custom_title: Callable[[str], str]
    default_bot_nickname: str


@dataclass
class RuntimeDeps:
    is_msg_processed: Callable[[int], bool]
    logger: Any
    superusers: set[str]
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]]
    should_avoid_interrupting: Callable[[str, bool], bool]
    module_instance_id: int
    process_yaml_response_logic: Callable[..., Any]
    plugin_config: Any
    get_current_time: Callable[[], Any]
    format_time_context: Callable[[Any | None], str]
    schedule_disabled_override_prompt: Callable[[], str]
    get_schedule_prompt_injection: Callable[[], str]
    build_grounding_context: Callable[[str], Any]
    update_private_interaction_time: Callable[[str], None]
    call_ai_api: Callable[..., Any]
    save_plugin_runtime_config: Callable[[], None] | None
    user_blacklist: Dict[str, float]
    record_group_msg: Callable[..., None]
    split_text_into_segments: Callable[[str], List[str]]
    message_segment_cls: Any
    get_sticker_files: Callable[[], List[Path]]
    get_http_client: Callable[[], httpx.AsyncClient]
    get_whitelisted_groups: Callable[[], List[str]]
    tts_service: Any = None
    tool_registry: Any = None
    inner_state_updater: Any = None
    agent_tool_caller: Any = None
    persona_store: Any = None
    vision_caller: Any = None
    knowledge_store: Any = None
    memory_store: Any = None
    profile_service: Any = None
    memory_curator: Any = None
    background_intelligence: Any = None


@dataclass
class TypeDeps:
    poke_event_cls: Any
    message_event_cls: Any
    group_message_event_cls: Any
    private_message_event_cls: Any
    message_cls: Any


@dataclass
class ReplyProcessorDeps:
    session: SessionDeps
    persona: PersonaDeps
    runtime: RuntimeDeps
    types: TypeDeps


def _build_tts_user_hint(*, is_private: bool, group_style: str = "") -> str:
    scene = "私聊" if is_private else "群聊"
    hint = f"这是{scene}场景下的回复，请自然朗读，整体语速略快一点。"
    style = str(group_style or "").strip()
    if style:
        hint += f" 参考群聊风格：{style[:80]}"
    return hint


def _looks_like_sticker_message(text: str) -> bool:
    plain = str(text or "")
    return "[图片·表情包]" in plain or "[表情id:" in plain or "[表情包]" in plain


async def _run_agent_if_enabled(
    bot: Any,
    event: Any,
    messages: List[Dict[str, Any]],
    persona: PersonaDeps,
    runtime: RuntimeDeps,
    interaction_count: int = 0,
    current_image_urls: List[str] | None = None,
 ) -> tuple[str | None, bool, bool]:
    if not (
        getattr(runtime.plugin_config, "personification_agent_enabled", True)
        and runtime.tool_registry
        and runtime.agent_tool_caller
    ):
        return None, False, False

    executor = ActionExecutor(bot, event, runtime.plugin_config, runtime.logger)
    runtime_registry = _clone_tool_registry(runtime.tool_registry)
    friend_ids = await _get_cached_friend_ids(bot, runtime.logger)
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
    result = await run_agent(
        messages=messages,
        registry=runtime_registry,
        tool_caller=runtime.agent_tool_caller,
        executor=executor,
        plugin_config=runtime.plugin_config,
        logger=runtime.logger,
        max_steps=getattr(runtime.plugin_config, "personification_agent_max_steps", 5),
        current_image_urls=current_image_urls,
    )
    if runtime.inner_state_updater:
        user_id = str(getattr(event, "user_id", "") or "")
        asyncio.create_task(runtime.inner_state_updater(result.text, user_id))
    for action in result.pending_actions:
        await executor.execute(action["type"], action["params"])
    return result.text, True, bool(getattr(result, "bypass_length_limits", False))


def _clone_tool_registry(registry: ToolRegistry) -> ToolRegistry:
    cloned = ToolRegistry()
    for tool in registry.active():
        cloned.register(tool)
    return cloned


async def _get_cached_friend_ids(bot: Any, logger: Any, ttl_seconds: int = 300) -> set[str]:
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
        logger.debug(f"[reply_processor] get_friend_list failed: {e}")

    _FRIEND_IDS_CACHE[cache_key] = (now_ts, set(friend_ids))
    return friend_ids


def _stringify_message_content(content: Any) -> str:
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


def _count_user_interactions(messages: List[Dict[str, Any]], user_id: str) -> int:
    marker = f"({user_id})"
    count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content_text = _stringify_message_content(message.get("content", ""))
        if marker in content_text:
            count += 1
    return count


def _normalize_reply_key(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized[:80]


def _extract_reply_sender_meta(reply: Any) -> tuple[str, bool]:
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


def _should_suppress_group_topic_loop(
    reply_content: str,
    session_messages: List[Dict[str, Any]],
) -> bool:
    reply_key = _normalize_reply_key(reply_content)
    if not reply_key:
        return False

    recent_assistant = [
        _normalize_reply_key(_stringify_message_content(msg.get("content", "")))
        for msg in session_messages[-10:]
        if isinstance(msg, dict) and msg.get("role") == "assistant"
    ]
    recent_assistant = [key for key in recent_assistant if key]
    if len(recent_assistant) >= 2 and reply_key in recent_assistant[-2:]:
        return True
    if len(recent_assistant) >= 3 and recent_assistant[-1] == recent_assistant[-2] == reply_key:
        return True
    return False


def _is_disallowed_ip_address(ip: ipaddress._BaseAddress) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


async def _is_safe_remote_image_url(url: str, logger: Any) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in _BLOCKED_IMAGE_HOSTS or host.endswith(_BLOCKED_IMAGE_HOST_SUFFIXES):
        logger.warning(f"拟人插件：拒绝访问高风险图片地址 host={host}")
        return False

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _is_disallowed_ip_address(literal_ip):
            logger.warning(f"拟人插件：拒绝访问内网/本地图片地址 ip={literal_ip}")
            return False
        return True

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return True
    except Exception as e:
        logger.warning(f"拟人插件：解析图片域名失败，已拒绝 {host}: {e}")
        return False

    for info in infos:
        try:
            resolved_ip = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if _is_disallowed_ip_address(resolved_ip):
            logger.warning(f"拟人插件：拒绝访问解析到内网/本地的图片地址 host={host} ip={resolved_ip}")
            return False
    return True


async def _download_safe_image_bytes(
    *,
    url: str,
    file_name: str,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> tuple[str | None, bytes | None, bool]:
    if file_name.endswith(".gif"):
        return None, None, True
    if not await _is_safe_remote_image_url(url, logger):
        return None, None, False

    try:
        async with http_client.stream("GET", url, timeout=10, follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            mime_type = resp.headers.get("Content-Type", "image/jpeg")
            if "image/gif" in mime_type.lower():
                return None, None, True

            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > _MAX_IMAGE_DOWNLOAD_BYTES:
                        raise ValueError("image too large")
                except ValueError:
                    raise ValueError("invalid image size")

            payload = bytearray()
            async for chunk in resp.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > _MAX_IMAGE_DOWNLOAD_BYTES:
                    raise ValueError("image too large")
            return mime_type, bytes(payload), False
    except Exception as e:
        logger.warning(f"下载图片失败或被拦截，已忽略原图 URL: {e}")
        return None, None, False


async def _extract_images_from_segment(
    seg: Any,
    *,
    http_client: httpx.AsyncClient,
    message_text_ref: List[str],
    image_urls: List[str],
    logger: Any,
) -> None:
    if getattr(seg, "type", None) != "image":
        return
    data = getattr(seg, "data", {})
    url = data.get("url")
    file_name = str(data.get("file", "")).lower()
    if not url:
        return

    mime_type, payload, is_gif = await _download_safe_image_bytes(
        url=url,
        file_name=file_name,
        http_client=http_client,
        logger=logger,
    )
    if is_gif:
        logger.info("拟人插件：检测到 GIF 图片，忽略并不予回复")
        return
    if payload is None or mime_type is None:
        message_text_ref.append("[图片·照片]")
        return

    try:
        img_obj = Image.open(BytesIO(payload))
        w, h = img_obj.size
        if w <= 1280 and h <= 1280:
            message_text_ref.append("[图片·表情包]")
        else:
            message_text_ref.append("[图片·照片]")
    except Exception as e:
        logger.warning(f"识别图片尺寸失败: {e}")
        message_text_ref.append("[图片·照片]")

    base64_data = base64.b64encode(payload).decode("utf-8")
    image_urls.append(f"data:{mime_type};base64,{base64_data}")


async def _extract_reply_images(
    seg_type: str,
    data: Dict[str, Any],
    *,
    http_client: httpx.AsyncClient,
    message_text_ref: List[str],
    image_urls: List[str],
    logger: Any,
) -> None:
    if seg_type != "image":
        return
    url = data.get("url")
    file_name = str(data.get("file", "")).lower()
    if not url:
        return
    mime_type, payload, is_gif = await _download_safe_image_bytes(
        url=url,
        file_name=file_name,
        http_client=http_client,
        logger=logger,
    )
    if is_gif:
        return
    message_text_ref.append("[图片]")
    if payload is None or mime_type is None:
        return
    base64_data = base64.b64encode(payload).decode("utf-8")
    image_urls.append(f"data:{mime_type};base64,{base64_data}")


def _truncate_at_punctuation(text: str, max_chars: int) -> str:
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


def _build_final_visible_reply_text(
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
    final_reply = str(reply_content or "").strip()
    if max_chars and max_chars > 0 and len(final_reply) > max_chars:
        final_reply = _truncate_at_punctuation(final_reply, max_chars)
    return sanitize_history_text(final_reply)


def _build_base_system_prompt(
    *,
    base_prompt: str,
    user_name: str,
    level_name: str,
    combined_attitude: str,
    is_private_session: bool,
    prelude_chunks: List[str],
    context_chunks: List[str],
    postlude_chunks: List[str],
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
    if is_private_session:
        parts.append(
            "## 私聊规则\n"
            "1. 私聊里也要像真人聊天，不要像客服或助手。\n"
            "2. 对重复、无意义或你不想接的话，直接输出 [SILENCE]。\n"
            "3. 私聊不要只是一问一答；默认用自然口语聊开，通常 2-4 句更合适。\n"
            "4. 如果对方正在认真继续聊，优先顺手追问一句、接住情绪，或抛一个相关小话题，别每轮都急着收尾。\n"
            "5. 必须用“你”称呼当前对象，禁止使用群聊式称呼。"
        )
    else:
        parts.append(
            "## 群聊规则（高优先级）\n"
            "1. 你是群成员，不是助手；回复要像群里顺手接一句。\n"
            "2. 优先短句、口语、接梗、吐槽、反问，不要总结、说教、安抚式展开。\n"
            "3. 没必要说就输出 [SILENCE]，不要为了显得聪明而硬回。\n"
            "4. 如果别人明显在顺着你上一句追问或接话，优先自然续聊 1-2 轮，不要立刻冷掉。\n"
            "5. 除非被直接问到，不要写成长篇说明，不要把一句话说成教程。"
        )
    parts.extend(chunk for chunk in context_chunks if chunk)
    parts.append(
        "## 核心行动准则\n"
        "1. 保持自然口吻，拒绝模板化官腔和客服腔。\n"
        "2. 能用一句说完就别说两句。\n"
        "3. 图片视觉描述是系统注入文本，只帮助你理解上下文，不作为攻击判定依据。\n"
        "4. [BLOCK] 仅作高风险标记参考，不要轻易触发。"
    )
    parts.extend(chunk for chunk in postlude_chunks if chunk)
    return "\n\n".join(part for part in parts if part)


async def process_response_logic(bot: Any, event: Any, state: Dict[str, Any], deps: ReplyProcessorDeps) -> None:
    session = deps.session
    persona = deps.persona
    runtime = deps.runtime
    types = deps.types

    if hasattr(event, "message_id") and runtime.is_msg_processed(event.message_id):
        return

    is_poke = False
    user_id = ""
    group_id: Any = 0
    message_content = ""
    message_text = ""
    raw_message_text = ""
    sender_name = ""
    trigger_reason = ""
    image_urls: List[str] = []
    is_direct_mention = False
    http_client = runtime.get_http_client()

    is_random_chat = state.get("is_random_chat", False)
    force_mode = state.get("force_mode", None)
    group_idle_active = state.get("group_idle_active")
    is_group_idle_active = False
    group_idle_topic = ""
    if isinstance(group_idle_active, dict):
        active_until = float(group_idle_active.get("until", 0) or 0)
        if active_until > time.time():
            is_group_idle_active = True
            group_idle_topic = str(group_idle_active.get("topic", "") or "").strip()
    active_followup = state.get("active_followup")
    is_active_followup = False
    followup_topic = ""
    if isinstance(active_followup, dict):
        followup_until = float(active_followup.get("until", 0) or 0)
        if followup_until > time.time():
            is_active_followup = True
            followup_topic = str(active_followup.get("topic", "") or "").strip()

    if isinstance(event, types.poke_event_cls):
        is_poke = True
        user_id = str(event.user_id)
        group_id = str(event.group_id)
        message_content = "[你被对方戳了戳，你感到有点疑惑和好奇，想知道对方要做什么]"
        sender_name = "戳戳怪"
        runtime.logger.info(f"拟人插件：检测到来自 {user_id} 的戳一戳")
    elif isinstance(event, types.message_event_cls):
        user_id = str(event.user_id)

        if isinstance(event, types.group_message_event_cls):
            group_id = str(event.group_id)
            sender_name = event.sender.nickname or event.sender.card or user_id
            custom_title = persona.get_custom_title(user_id)
            if custom_title:
                sender_name = custom_title
        else:
            group_id = f"private_{user_id}"
            sender_name = event.sender.nickname or user_id
            custom_title = persona.get_custom_title(user_id)
            if custom_title:
                sender_name = custom_title

        message_text_parts: List[str] = []
        source_message = state.get("concatenated_message", event.message)
        bot_self_id = str(getattr(bot, "self_id", "") or "")
        if bot_self_id:
            try:
                for seg in source_message:
                    if getattr(seg, "type", None) != "at":
                        continue
                    qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
                    if qq == bot_self_id:
                        is_direct_mention = True
                        break
            except Exception:
                is_direct_mention = False
        for seg in source_message:
            if seg.type == "text":
                message_text_parts.append(seg.data.get("text", ""))
            elif seg.type == "face":
                face_id = seg.data.get("id", "")
                message_text_parts.append(f"[表情id:{face_id}]")
            elif seg.type == "mface":
                summary = seg.data.get("summary", "表情包")
                message_text_parts.append(f"[{summary}]")
            elif seg.type == "image":
                await _extract_images_from_segment(
                    seg,
                    http_client=http_client,
                    message_text_ref=message_text_parts,
                    image_urls=image_urls,
                    logger=runtime.logger,
                )
            elif seg.type == "gif":
                # OneBot 独立 gif 消息段，直接忽略，不下载，不传给视觉模型
                runtime.logger.info("拟人插件：检测到 gif 消息段，忽略并不予回复")

        if not image_urls and source_message is not event.message:
            try:
                for seg in event.message:
                    if getattr(seg, "type", None) == "image":
                        await _extract_images_from_segment(
                            seg,
                            http_client=http_client,
                            message_text_ref=message_text_parts,
                            image_urls=image_urls,
                            logger=runtime.logger,
                        )
            except Exception as e:
                runtime.logger.warning(f"回退解析原始消息图片失败: {e}")

        reply = getattr(event, "reply", None)
        if reply:
            reply_msg = getattr(reply, "message", None) or (reply.get("message") if isinstance(reply, dict) else None)
            if reply_msg:
                reply_sender_name, reply_is_bot = _extract_reply_sender_meta(reply)
                message_text_parts.append(
                    f"\n[引用内容|发送者:{reply_sender_name}|类型:{'机器人消息' if reply_is_bot else '群成员消息'}]: "
                )
                try:
                    if isinstance(reply_msg, (list, tuple, types.message_cls)):
                        for seg in reply_msg:
                            seg_type = getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)
                            data = getattr(seg, "data", None) or (seg.get("data") if isinstance(seg, dict) else {})
                            if seg_type == "text":
                                message_text_parts.append(data.get("text", ""))
                            elif seg_type == "image":
                                await _extract_reply_images(
                                    seg_type,
                                    data,
                                    http_client=http_client,
                                    message_text_ref=message_text_parts,
                                    image_urls=image_urls,
                                    logger=runtime.logger,
                                )
                except Exception as e:
                    runtime.logger.warning(f"处理引用消息失败: {e}")

        try:
            forward_content = await extract_forward_message_content(
                bot,
                event,
                logger=runtime.logger,
            )
        except Exception as e:
            runtime.logger.warning(f"处理聊天记录失败: {e}")
            forward_content = ""
        if forward_content:
            clipped_forward = forward_content[:2000]
            message_text_parts.append("\n[聊天记录]:\n")
            message_text_parts.append(clipped_forward)

        message_text = "".join(message_text_parts)
        raw_message_text = message_text
        message_content = message_text.strip()
        is_private_context = str(group_id).startswith(session.private_session_prefix)
        if isinstance(event, types.private_message_event_cls) and session.looks_like_private_command(message_content):
            runtime.logger.debug(f"拟人插件：私聊命令消息已跳过，用户 {user_id}")
            return

        base_prompt = persona.load_prompt(group_id)
        is_yaml_mode = isinstance(base_prompt, dict)

        if is_yaml_mode:
            if is_poke:
                trigger_reason = "对方戳了戳你。"
            elif is_active_followup:
                trigger_reason = (
                    f"你刚才已经和 {sender_name}({user_id}) 聊上了。"
                    f"当前是在顺着上一轮继续说话，刚才的话题是：{followup_topic or '刚才那段对话'}。"
                    "优先像真人继续接上，不要突然冷掉；只有明显跑题或没必要时才输出 [SILENCE]。"
                )
            elif is_random_chat:
                trigger_reason = (
                    f"你在群里潜水看大家聊天。"
                    f"发言者是 {sender_name}({user_id})，这句话未必是对你说的。"
                    f"只有在对方明显在 cue 你、顺着你的话题聊，或你自然能接上一句时再回复；否则输出 [SILENCE]。"
                )
            else:
                trigger_reason = f"对方（{sender_name}）正在【主动】与你搭话，请认真回复。"

            if image_urls and not message_content:
                message_content = "[发送了一张图片]"
        else:
            if is_private_context:
                if image_urls and not message_content:
                    message_content = "[发送了一张图片]"
            else:
                if image_urls and not message_content:
                    if is_active_followup:
                        message_content = (
                            f"[对方正在顺着你刚才的话题继续聊，并发来了一张图片。"
                            f"刚才的话题：{followup_topic or '上一轮对话'}。"
                            "如果图片明显是在接前文，就自然评价一句；否则保持安静]"
                        )
                    elif is_random_chat:
                        message_content = f"[群里 {sender_name} 发了一张图片，你只是路过看到。要是自然能接一句就接，不然保持安静]"
                    else:
                        message_content = "[对方发送了一张图片，是在对你说话]"
                elif is_active_followup:
                    message_content = (
                        f"[对方正在顺着你刚才的话继续聊，刚才的话题：{followup_topic or '上一轮对话'}。"
                        f"对方现在说：{message_content}]"
                    )
                elif is_random_chat:
                    message_content = f"[群员 {sender_name} 正在和别人聊天：{message_content}。如果这话和你没关系，或者你接不上，就回复 [SILENCE]；只有自然能插一句时再说话]"
                else:
                    message_content = f"[对方正在直接跟你说：{message_content}]"
    else:
        return

    if not runtime.get_configured_api_providers():
        runtime.logger.warning("拟人插件：未配置可用的 API provider，跳过回复")
        if is_direct_mention:
            try:
                await bot.send(event, "在呢")
            except Exception:
                pass
        return

    user_name = sender_name
    if not message_content and not is_poke and not image_urls:
        return

    if (
        not is_poke
        and str(user_id) in {str(item) for item in (runtime.superusers or set())}
    ):
        install_reply = await maybe_handle_superuser_natural_language_skill_install(
            text=message_content,
            plugin_config=runtime.plugin_config,
            save_plugin_runtime_config=runtime.save_plugin_runtime_config,
            logger=runtime.logger,
            operator_user_id=str(user_id),
            tool_caller=runtime.agent_tool_caller,
            tool_registry=runtime.tool_registry,
        )
        if install_reply:
            await bot.send(event, install_reply)
            return

    if (
        isinstance(event, types.group_message_event_cls)
        and (not is_direct_mention)
        and (not is_active_followup)
        and runtime.should_avoid_interrupting(str(group_id), is_random_chat)
    ):
        runtime.logger.info(f"拟人插件：群 {group_id} 讨论热度高，触发 KY 规避，本轮保持沉默。")
        return

    if not is_poke:
        runtime.logger.info(
            f"拟人插件：[Bot {bot.self_id}] [Inst {runtime.module_instance_id}] 正在处理来自 {user_name} ({user_id}) 的消息..."
        )
    else:
        runtime.logger.info(
            f"拟人插件：[Bot {bot.self_id}] [Inst {runtime.module_instance_id}] 正在处理来自 {user_name} ({user_id}) 的戳一戳..."
        )

    is_private_session = str(group_id).startswith(session.private_session_prefix)
    if isinstance(event, types.group_message_event_cls) and not state.get("message_target"):
        recent_group_msgs = get_recent_group_msgs(str(group_id), limit=8, expire_hours=0)
        state["message_target"] = infer_message_target(
            event,
            bot_self_id=str(getattr(bot, "self_id", "") or ""),
            recent_group_msgs=recent_group_msgs,
        )
    session_id = session.build_private_session_id(user_id) if is_private_session else session.build_group_session_id(str(group_id))
    legacy_session_id = None if is_private_session else str(group_id)
    session.ensure_session_history(session_id, legacy_session_id=legacy_session_id)

    attitude_desc = "态度普通，像平常一样交流。"
    level_name = "未知"
    group_attitude = ""

    if persona.sign_in_available:
        try:
            user_data = persona.get_user_data(user_id)
            favorability = user_data.get("favorability", 0.0)
            level_name = persona.get_level_name(favorability)
            attitude_desc = persona.favorability_attitudes.get(level_name, attitude_desc)

            group_key = f"group_{group_id}"
            group_data = persona.get_user_data(group_key)
            group_favorability = group_data.get("favorability", 100.0)
            group_level = persona.get_level_name(group_favorability)
            group_attitude = persona.favorability_attitudes.get(group_level, "")
        except Exception as e:
            runtime.logger.error(f"获取好感度数据失败: {e}")

    now = runtime.get_current_time()
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = (
        f"{now.year}年{now.month:02d}月{now.day:02d}日 "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) "
        f"[{runtime.format_time_context(now)}]"
    )

    safe_user_name = user_name.replace(":", "：").replace("\n", " ").strip()
    safe_user_name = f"{safe_user_name}({user_id})"
    msg_prefix = f"[{safe_user_name}]: "
    bot_self_id = str(getattr(bot, "self_id", "") or "")
    incoming_relation_metadata = (
        _build_group_session_relation_metadata(
            event,
            bot_self_id=bot_self_id,
            group_id=str(group_id),
            user_id=user_id,
            source_kind="user",
        )
        if isinstance(event, types.group_message_event_cls)
        else {"user_id": user_id, "source_kind": "user"}
    )
    if state.get("message_target"):
        incoming_relation_metadata["message_target"] = state.get("message_target")

    tool_image_urls = list(image_urls)
    if image_urls and runtime.vision_caller is not None:
        providers = runtime.get_configured_api_providers()
        primary_api_type = ""
        if providers:
            primary_api_type = str(providers[0].get("api_type", "") or "").strip().lower()
        if not primary_api_type:
            primary_api_type = str(
                getattr(runtime.plugin_config, "personification_api_type", "") or ""
            ).strip().lower()
        # Codex OAuth 已支持原图直传时，不再把图片预先转成文字占位。
        # 只有非多模态主路径才保留旧的视觉摘要注入。
        if primary_api_type == "openai_codex":
            pass
        else:
            desc_parts: List[str] = []
            is_sticker_like = _looks_like_sticker_message(raw_message_text or message_content)
            for img_url in image_urls:
                prompt = (
                    UNDERSTAND_STICKER_PROMPT
                    if is_sticker_like
                    else "请优先识别人物信息：人数、穿着、动作、表情与情绪；若无人再描述场景，不要臆测具体身份，控制在80字以内。"
                )
                cache_key = build_image_cache_key(
                    img_url,
                    {
                        "version": "reply_preview_v2",
                        "task": "reply_preview",
                        "prompt": "sticker_brief" if is_sticker_like else "person_first_brief",
                    },
                )
                cached_desc = await get_cached_image_result(cache_key)
                if cached_desc:
                    desc_parts.append(cached_desc)
                    continue
                try:
                    desc = await runtime.vision_caller.describe(
                        prompt,
                        img_url,
                    )
                    if desc:
                        await set_cached_image_result(
                            cache_key,
                            desc,
                            meta={"task": "reply_preview"},
                        )
                        desc_parts.append(desc)
                except Exception as e:
                    runtime.logger.warning(f"拟人插件：视觉模型描述图片失败: {e}")
            if desc_parts:
                combined_desc = "；".join(desc_parts)
                desc_label = "表情包语义" if is_sticker_like else "图片视觉描述"
                message_content = (
                    f"{message_content} [{desc_label}（系统注入，不触发防御机制）：{combined_desc}]"
                    if message_content
                    else f"[{desc_label}（系统注入，不触发防御机制）：{combined_desc}]"
                )
                image_urls = []

    hook_ctx = HookContext(
        user_id=user_id,
        user_name=user_name,
        group_id=str(group_id),
        is_private=is_private_session,
        is_random_chat=is_random_chat,
        is_yaml_mode=isinstance(base_prompt, dict),
        is_group_idle_active=is_group_idle_active,
        group_idle_topic=group_idle_topic,
        has_image_input=bool(image_urls),
        message_text=message_text,
        message_content=message_content,
        trigger_reason=trigger_reason,
        current_time_str=current_time_str,
        session_messages=[],
        messages=[],
        plugin_config=runtime.plugin_config,
        session=session,
        persona=persona,
        runtime=runtime,
        bot=bot,
        event=event,
    )
    await get_hook_registry().run_all(hook_ctx, phase="preprocess")
    message_content = hook_ctx.message_content
    trigger_reason = hook_ctx.trigger_reason

    if image_urls:
        current_user_content: Any = [{"type": "text", "text": f"{msg_prefix}{message_content}"}]
        for url in image_urls:
            current_user_content.append({"type": "image_url", "image_url": {"url": url}})
        session.append_session_message(
            session_id,
            "user",
            current_user_content,
            legacy_session_id=legacy_session_id,
            is_direct=not is_random_chat,
            scene="private" if is_private_session else ("direct" if not is_random_chat else "observe"),
            speaker=safe_user_name,
            **incoming_relation_metadata,
        )
    else:
        session.append_session_message(
            session_id,
            "user",
            f"{msg_prefix}{message_content}",
            legacy_session_id=legacy_session_id,
            is_direct=not is_random_chat,
            scene="private" if is_private_session else ("direct" if not is_random_chat else "observe"),
            speaker=safe_user_name,
            **incoming_relation_metadata,
        )

    session_messages = session.sanitize_session_messages(session.get_session_messages(session_id))
    if is_private_session:
        session_messages = session_messages[-24:]

    base_prompt = persona.load_prompt(str(group_id))
    if isinstance(base_prompt, dict):
        if not trigger_reason and is_poke:
            trigger_reason = "对方戳了戳你。"
        await runtime.process_yaml_response_logic(
            bot,
            event,
            str(group_id),
            user_id,
            user_name,
            level_name,
            base_prompt,
            session_messages,
            trigger_reason=trigger_reason,
            current_image_urls=tool_image_urls,
        )
        return

    attitude_desc = attitude_desc or "态度普通，像平常一样交流。"
    relation_style = "用自然平衡语气回应。"
    preferred_length = "默认回复 1-2 句。"
    if level_name in {"挚友", "亲密"}:
        relation_style = "适度使用更亲近的称呼或语气词，体现熟悉感。"
        preferred_length = "可以扩展到 2-4 句，增加情感反馈。"
    elif level_name in {"陌生", "路人"}:
        relation_style = "保持礼貌和边界感，避免过度亲昵。"
        preferred_length = "优先 1-2 句，直接回答重点。"
    if is_private_session:
        relation_style += " 私聊场景可更自然连续，不必强调围观感。"

    combined_attitude = f"你对该用户的个人态度是：{attitude_desc}\n关系表达策略：{relation_style}\n长度偏好：{preferred_length}"
    if group_attitude:
        combined_attitude += f"\n当前群聊整体氛围带给你的感受是：{group_attitude}"

    hook_ctx.session_messages = session_messages
    prelude_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_prelude")
    context_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_context")
    postlude_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_postlude")
    system_prompt = _build_base_system_prompt(
        base_prompt=base_prompt,
        user_name=user_name,
        level_name=level_name,
        combined_attitude=combined_attitude,
        is_private_session=is_private_session,
        prelude_chunks=prelude_chunks,
        context_chunks=context_chunks,
        postlude_chunks=postlude_chunks,
    )
    if state.get("message_target") == TARGET_OTHERS:
        system_prompt += (
            "\n[系统提示] 当前消息疑似群友之间的对话，不一定是对你说话。"
            "请判断是否需要回复，不确定则保持沉默（输出 [NO_REPLY]）。"
        )

    available_stickers: List[str] = []
    group_config = persona.get_group_config(str(group_id))
    if group_config.get("sticker_enabled", True):
        available_stickers = [f.stem for f in runtime.get_sticker_files()]

    messages = [
        {
            "role": "system",
            "content": (
                f"{system_prompt}\n\n当前可用表情包参考: "
                f"{', '.join(available_stickers[:15]) if available_stickers else '暂无'}"
            ),
        }
    ]
    messages.extend(session_messages)
    hook_ctx.messages = messages
    await get_hook_registry().run_all(hook_ctx, phase="message")
    friend_request_interaction_count = (
        _count_user_interactions(messages, user_id)
        if not is_private_session and not is_random_chat
        else 0
    )

    try:
        if is_private_session:
            try:
                runtime.update_private_interaction_time(user_id)
            except Exception as e:
                runtime.logger.error(f"更新最后交互时间失败: {e}")

        image_ctx_token = set_current_image_context(tool_image_urls, message_content)
        try:
            reply_content, used_agent, bypass_length_limits = await _run_agent_if_enabled(
                bot,
                event,
                messages,
                persona,
                runtime,
                interaction_count=friend_request_interaction_count,
                current_image_urls=tool_image_urls,
            )
        finally:
            reset_current_image_context(image_ctx_token)
        if used_agent and reply_content in ("[NO_REPLY]", "<NO_REPLY>"):
            if is_random_chat:
                runtime.logger.info("拟人插件：Agent 在随机插话场景选择 NO_REPLY，保持沉默。")
                return
            runtime.logger.info("拟人插件：Agent 返回 NO_REPLY，回退基础模型生成文本回复。")
            used_agent = False
            reply_content = ""
            bypass_length_limits = False
        if not used_agent:
            reply_content = await runtime.call_ai_api(messages)
            bypass_length_limits = False
            if not reply_content:
                if image_urls:
                    runtime.logger.warning("拟人插件：视觉模型调用可能失败，正在尝试降级至纯文本模式...")
                    fallback_messages = []
                    for msg in messages:
                        if isinstance(msg.get("content"), list):
                            text_content = "".join([item["text"] for item in msg["content"] if item["type"] == "text"])
                            fallback_messages.append({"role": msg["role"], "content": text_content})
                        else:
                            fallback_messages.append(msg)
                    reply_content = await runtime.call_ai_api(fallback_messages)
                if not reply_content:
                    runtime.logger.warning("拟人插件：未能获取到 AI 回复内容")
                    if is_direct_mention:
                        reply_content = random.choice(_FALLBACK_REPLIES)
                    else:
                        return

        reply_content = re.sub(r"\[表情:[^\]]*\]", "", reply_content)
        reply_content = re.sub(r"\[发送了表情包:[^\]]*\]", "", reply_content).strip()
        reply_content = re.sub(r"[A-F0-9]{16,}", "", reply_content).strip()
        reply_content = re.sub(r"^(根据你的描述|总的来说|总体来说)[，,:：\s]*", "", reply_content).strip()
        reply_content = re.sub(r"^(如果你需要|如果需要的话)[，,:：\s]*", "", reply_content).strip()
        reply_content = re.sub(r"(?:如果你需要|需要的话).*?$", "", reply_content).strip()
        if (
            not is_private_session
            and _should_suppress_group_topic_loop(reply_content, session_messages)
        ):
            runtime.logger.info(
                f"拟人插件：群 {group_id} 命中重复话题抑制，本轮不继续围绕旧内容展开。"
            )
            if not is_direct_mention and is_random_chat:
                return
            reply_content = "嗯，我知道啦"
        has_block_marker = "[BLOCK]" in reply_content or "<BLOCK>" in reply_content
        if has_block_marker:
            reply_content = reply_content.replace("[BLOCK]", "").replace("<BLOCK>", "").strip()

        has_silence_marker = "[SILENCE]" in reply_content or "<SILENCE>" in reply_content
        if has_silence_marker:
            runtime.logger.info(f"AI 决定结束与群 {group_id} 中 {user_name}({user_id}) 的对话 (SILENCE)")
            if is_direct_mention:
                reply_content = "在呢"
            else:
                return

        if used_agent and ("[NO_REPLY]" in reply_content or "<NO_REPLY>" in reply_content):
            if is_random_chat:
                return
            runtime.logger.info("拟人插件：Agent 文本含 NO_REPLY 标记，回退基础模型重试。")
            reply_content = await runtime.call_ai_api(messages)
            bypass_length_limits = False
            if not reply_content:
                runtime.logger.warning("拟人插件：Agent 回退基础模型后仍无回复内容")
                if is_direct_mention:
                    reply_content = random.choice(_FALLBACK_REPLIES)
                else:
                    return

        if has_block_marker:
            runtime.logger.warning(
                f"[BLOCK] 检测到高风险内容标记，当前仅忽略本轮回复: group={group_id} user={user_id}"
            )
            notify_superusers = getattr(runtime, "superusers", None) or set()
            if notify_superusers:
                notify_msg = (
                    "拟人插件高风险提示\n"
                    f"群：{group_id}\n"
                    f"用户：{user_name}（{user_id}）\n"
                    f"原始文字：{(raw_message_text or message_text or '')[:60]}\n"
                    f"处理后内容：{(message_content or '')[:100]}\n"
                    f"时间：{runtime.get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    "处理：已跳过本轮回复，未自动拉黑。"
                )

                async def _notify_superusers(
                    _bot: Any = bot,
                    _superusers: set[str] = notify_superusers,
                    _msg: str = notify_msg,
                ) -> None:
                    for _su in _superusers:
                        try:
                            await _bot.send_private_msg(user_id=int(_su), message=_msg)
                        except Exception as _e:
                            runtime.logger.warning(f"[BLOCK] 通知管理员 {_su} 失败: {_e}")

                asyncio.create_task(_notify_superusers())
            return

        if not used_agent and ("[NO_REPLY]" in reply_content or "<NO_REPLY>" in reply_content):
            runtime.logger.info(
                f"AI 选择不回复群 {group_id} 中 {user_name}({user_id}) 的消息 (NO_REPLY)"
            )
            return

        has_good_atmosphere = "[氛围好]" in reply_content or "<氛围好>" in reply_content
        if has_good_atmosphere:
            reply_content = reply_content.replace("[氛围好]", "").replace("<氛围好>", "").strip()
            if persona.sign_in_available:
                try:
                    is_private_context = str(group_id).startswith("private_")
                    if not is_private_context:
                        group_key = f"group_{group_id}"
                        group_data = persona.get_user_data(group_key)

                        today = time.strftime("%Y-%m-%d")
                        last_update = group_data.get("last_update", "")
                        daily_count = group_data.get("daily_fav_count", 0.0)

                        if last_update != today:
                            daily_count = 0.0

                        if daily_count < 10.0:
                            g_current_fav = float(group_data.get("favorability", 100.0))
                            g_new_fav = round(g_current_fav + 0.1, 2)
                            daily_count = round(float(daily_count) + 0.1, 2)
                            persona.update_user_data(
                                group_key,
                                favorability=g_new_fav,
                                daily_fav_count=daily_count,
                                last_update=today,
                            )
                            runtime.logger.info(
                                f"AI 觉得群 {group_id} 氛围良好，好感度 +0.10 (今日已加: {daily_count:.2f}/10.00)"
                            )
                except Exception as e:
                    runtime.logger.error(f"增加群聊好感度失败: {e}")

        has_interesting = "[有趣]" in reply_content
        if has_interesting:
            reply_content = reply_content.replace("[有趣]", "").strip()
            if persona.sign_in_available:
                try:
                    user_data = persona.get_user_data(user_id)
                    today = time.strftime("%Y-%m-%d")

                    last_fav_date = user_data.get("last_interesting_date", "")
                    daily_interesting_count = float(user_data.get("daily_interesting_count", 0.0))
                    if last_fav_date != today:
                        daily_interesting_count = 0.0

                    DAILY_LIMIT = 5.0
                    INCREMENT = 0.05

                    if daily_interesting_count < DAILY_LIMIT:
                        current_fav = float(user_data.get("favorability", 0.0))
                        new_fav = round(current_fav + INCREMENT, 2)
                        daily_interesting_count = round(daily_interesting_count + INCREMENT, 2)
                        persona.update_user_data(
                            user_id,
                            favorability=new_fav,
                            daily_interesting_count=daily_interesting_count,
                            last_interesting_date=today,
                        )
                        runtime.logger.info(
                            f"AI 觉得与 {user_name}({user_id}) 聊天有趣，"
                            f"好感度 +{INCREMENT} (今日已加: {daily_interesting_count:.2f}/{DAILY_LIMIT:.1f})"
                        )
                except Exception as e:
                    runtime.logger.error(f"增加用户好感度失败: {e}")

        sticker_segment = None
        sticker_name = ""
        should_get_sticker = False

        group_config = persona.get_group_config(str(group_id))
        is_sticker_enabled = group_config.get("sticker_enabled", True)
        if is_sticker_enabled:
            if force_mode == "mixed":
                should_get_sticker = True
            elif force_mode == "text_only":
                should_get_sticker = False
            elif random.random() < runtime.plugin_config.personification_sticker_probability:
                should_get_sticker = True

        if should_get_sticker:
            sticker_path = getattr(runtime.plugin_config, "personification_sticker_path", None)
            if sticker_path:
                selected = select_sticker(
                    Path(sticker_path),
                    mood=reply_content,
                    context=raw_message_text or message_text or message_content,
                    proactive=bool(is_random_chat or is_group_idle_active),
                    plugin_config=runtime.plugin_config,
                    allow_fallback=(force_mode == "mixed"),
                    minimum_score=2,
                )
                if selected:
                    chosen = Path(selected)
                    sticker_name = chosen.stem
                    sticker_segment = runtime.message_segment_cls.image(f"file:///{chosen.absolute()}")
                    runtime.logger.info(f"拟人插件：按语义挑选表情包 {chosen.name}")

        bot_nickname = persona.default_bot_nickname or str(bot.self_id)
        if isinstance(event, types.group_message_event_cls):
            try:
                bot_member_info = await bot.get_group_member_info(
                    group_id=event.group_id,
                    user_id=int(bot.self_id),
                )
                bot_nickname = bot_member_info.get("card") or bot_member_info.get("nickname") or bot_nickname
            except Exception:
                pass
        final_reply = reply_content.strip()
        max_chars = 0 if bypass_length_limits else getattr(runtime.plugin_config, "personification_max_output_chars", 0)
        if max_chars and max_chars > 0 and len(final_reply) > max_chars:
            final_reply = _truncate_at_punctuation(final_reply, max_chars)
        # session/history 只记录最终对用户生效的文本，避免原始长回复与实际可见内容漂移。
        final_visible_reply_text = _build_final_visible_reply_text(
            reply_content,
            max_chars=max_chars,
            sanitize_history_text=session.sanitize_history_text,
        )
        sent_message_id = ""
        sent_as_tts = False
        tts_service = getattr(runtime, "tts_service", None)
        if (
            final_reply
            and not sticker_segment
            and tts_service is not None
            and tts_service.should_auto_tts(
                is_private=is_private_session,
                group_config=group_config,
                text=final_reply,
                has_rich_content=False,
            )
        ):
            try:
                sent_as_tts = await tts_service.send_tts(
                    bot=bot,
                    event=event,
                    message_segment_cls=runtime.message_segment_cls,
                    text=final_reply,
                    user_hint=_build_tts_user_hint(
                        is_private=is_private_session,
                        group_style=persona.get_group_style(str(group_id)),
                    ),
                    is_private=is_private_session,
                    group_style=persona.get_group_style(str(group_id)),
                    persona_tts=extract_persona_tts_config(base_prompt),
                    pause_range=(1.2, 2.0),
                )
            except Exception as e:
                runtime.logger.warning(f"[tts] 自动语音发送失败，回退文字: {e}")
        if final_reply:
            if not sent_as_tts:
                segments = runtime.split_text_into_segments(final_reply)
                max_seg = getattr(runtime.plugin_config, "personification_max_segment_chars", 0)
                if max_seg and max_seg > 0:
                    expanded: List[str] = []
                    for seg in segments:
                        expanded.extend(split_segment_if_long(seg, max_seg))
                    segments = expanded
                if not segments:
                    segments = [final_reply]
                for i, seg in enumerate(segments):
                    if not seg.strip():
                        continue
                    send_result = await bot.send(event, seg)
                    if not sent_message_id:
                        sent_message_id = extract_send_message_id(send_result)
                    if i < len(segments) - 1 or sticker_segment:
                        await asyncio.sleep(random.uniform(0.8, 1.6))

        if sticker_segment:
            send_result = await bot.send(event, sticker_segment)
            if not sent_message_id:
                sent_message_id = extract_send_message_id(send_result)

        assistant_metadata = {
            "scene": "reply",
            "sticker_sent": sticker_name if sticker_name else None,
            "speaker": bot_nickname,
            "user_id": bot_self_id or None,
            "source_kind": "bot_reply",
        }
        if isinstance(event, types.group_message_event_cls):
            assistant_metadata.update(
                {
                    "group_id": str(event.group_id),
                    "message_id": sent_message_id or None,
                    "reply_to_msg_id": incoming_relation_metadata.get("message_id"),
                    "reply_to_user_id": user_id,
                    "mentioned_ids": [],
                    "is_at_bot": False,
                }
            )
        session.append_session_message(
            session_id,
            "assistant",
            final_visible_reply_text,
            legacy_session_id=legacy_session_id,
            **assistant_metadata,
        )
        if getattr(runtime, "memory_curator", None) is not None:
            runtime.memory_curator.schedule_capture(
                summary=final_visible_reply_text,
                user_id=user_id,
                group_id="" if is_private_session else str(group_id),
                topic_tags=[str(group_id)] if not is_private_session else [],
            )

        if isinstance(event, types.group_message_event_cls):
            runtime.record_group_msg(
                str(event.group_id),
                bot_nickname,
                final_visible_reply_text,
                is_bot=True,
                user_id=bot_self_id,
                message_id=sent_message_id or None,
                reply_to_msg_id=incoming_relation_metadata.get("message_id"),
                reply_to_user_id=user_id,
                source_kind="bot_reply",
            )
            try:
                update_group_chat_active(
                    str(event.group_id),
                    user_id=user_id,
                    topic=raw_message_text or message_text or final_visible_reply_text,
                    active_minutes=int(
                        getattr(runtime.plugin_config, "personification_group_chat_active_minutes", 8)
                    ),
                )
            except Exception as e:
                runtime.logger.debug(f"[reply_processor] update_group_chat_active failed: {e}")
    except FinishedException:
        raise
    except Exception as e:
        runtime.logger.error(f"拟人插件 API 调用失败: {e}")
        if is_direct_mention:
            try:
                await bot.send(event, random.choice(_FALLBACK_REPLIES))
            except Exception:
                pass
