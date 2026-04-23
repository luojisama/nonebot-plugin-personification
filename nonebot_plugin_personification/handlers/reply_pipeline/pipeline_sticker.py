from __future__ import annotations

import asyncio
import base64
import ipaddress
import random
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List
from urllib.parse import urljoin, urlparse

import httpx

from ...core.media_understanding import analyze_images_with_route_or_fallback
from ...core.metrics import record_counter
from ...core.sticker_library import (
    analyze_sticker_image,
    image_bytes_to_data_url,
    load_sticker_metadata,
    normalize_sticker_entry,
    render_sticker_semantic_summary,
    resolve_sticker_dir,
    save_collected_sticker,
    save_sticker_metadata,
)
from ...core.sticker_feedback import load_sticker_feedback
from ...core.visual_capabilities import VISUAL_ROUTE_REPLY_PLAIN
from ...skills.skillpacks.sticker_tool.scripts.impl import (
    UNDERSTAND_STICKER_PROMPT,
    choose_sticker_for_context,
)
from .pipeline_context import classify_incoming_image


_GROUP_STICKER_STATE: Dict[str, dict] = {}
_STICKER_COOLDOWN_SECONDS = 180
_STICKER_DEDUP_WINDOW = 5
_STICKER_STATE_TTL_SECONDS = 86400
_MAX_IMAGE_DOWNLOAD_BYTES = 8 * 1024 * 1024
_BLOCKED_IMAGE_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".corp")
_BLOCKED_IMAGE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
}
_MAX_IMAGE_REDIRECTS = 5


@dataclass
class IncomingStickerCandidate:
    data_url: str
    payload: bytes
    mime_type: str
    source_kind: str
    summary_hint: str = ""


async def build_image_summary_suffix(
    *,
    runtime: Any,
    image_urls: List[str],
    sticker_like: bool,
) -> str:
    if not image_urls:
        return ""
    summary_text = ""
    if sticker_like:
        try:
            result = await analyze_sticker_image(
                runtime=runtime,
                image_refs=image_urls,
                fallback_vision_caller=runtime.vision_caller,
            )
            summary_text = render_sticker_semantic_summary(
                {
                    "description": result.description,
                    "ocr_text": result.ocr_text,
                    "use_hint": result.use_hint,
                    "avoid_hint": result.avoid_hint,
                    "mood_tags": result.mood_tags,
                    "scene_tags": result.scene_tags,
                }
            ) or result.summary
        except Exception as exc:
            runtime.logger.warning(f"拟人插件：表情包视觉理解失败，改用摘要回退: {exc}")
    if not summary_text:
        prompt = (
            UNDERSTAND_STICKER_PROMPT
            if sticker_like
            else "请用一句中文概括图片的主体、动作/表情、图中文字和当前语境里最相关的线索；控制在80字内，直接输出。"
        )
        try:
            summary_text, _route = await analyze_images_with_route_or_fallback(
                runtime=runtime,
                prompt=prompt,
                image_refs=image_urls,
                route_name=VISUAL_ROUTE_REPLY_PLAIN,
                fallback_vision_caller=runtime.vision_caller,
            )
        except Exception as exc:
            runtime.logger.warning(f"拟人插件：图片摘要注入失败: {exc}")
    if not summary_text:
        return ""
    desc_label = "表情包语义" if sticker_like else "图片视觉描述"
    return f"[{desc_label}（系统注入，不触发防御机制）：{summary_text}]"


async def auto_collect_stickers(
    *,
    runtime: Any,
    group_id: str,
    user_id: str,
    candidates: List[IncomingStickerCandidate],
) -> None:
    sticker_dir = resolve_sticker_dir(getattr(runtime.plugin_config, "personification_sticker_path", None), create=True)
    if not candidates:
        return
    for candidate in candidates:
        try:
            result = await analyze_sticker_image(
                runtime=runtime,
                image_refs=[candidate.data_url],
                fallback_vision_caller=runtime.vision_caller,
            )
            if not result.is_sticker or not result.should_collect:
                continue
            saved_path, created, file_hash = await save_collected_sticker(
                sticker_dir,
                payload=candidate.payload,
                mime_type=candidate.mime_type,
                file_name_hint=result.summary or result.description or "sticker",
            )
            metadata = load_sticker_metadata(sticker_dir)
            metadata[saved_path.name] = normalize_sticker_entry(
                {
                    "description": result.description,
                    "mood_tags": result.mood_tags,
                    "scene_tags": result.scene_tags,
                    "proactive_send": result.proactive_send,
                    "ocr_text": result.ocr_text,
                    "use_hint": result.use_hint,
                    "avoid_hint": result.avoid_hint,
                },
                file_name=saved_path.stem,
                vision_route=result.vision_route,
                file_hash=file_hash,
                source_kind=candidate.source_kind,
                source_group_id=group_id,
                source_user_id=user_id,
                collected_at=time.strftime("%Y-%m-%d %H:%M"),
            )
            await save_sticker_metadata(sticker_dir, metadata)
            if created:
                runtime.logger.info(f"拟人插件：已自动收藏表情包 {saved_path.name}")
        except Exception as exc:
            runtime.logger.debug(f"[reply_processor] auto collect sticker skipped: {exc}")


def spawn_auto_collect_stickers(
    *,
    runtime: Any,
    group_id: str,
    user_id: str,
    candidates: List[IncomingStickerCandidate],
    task_exc_logger: Callable[[str, Any], Any],
) -> None:
    if not candidates:
        return
    task = asyncio.create_task(
        auto_collect_stickers(
            runtime=runtime,
            group_id=group_id,
            user_id=user_id,
            candidates=list(candidates),
        )
    )
    task.add_done_callback(task_exc_logger("auto_collect_stickers", runtime.logger))


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
        logger.warning(f"拟人插件：图片域名解析失败，已拒绝 {host}")
        return False
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


async def download_safe_image_bytes(
    *,
    url: str,
    file_name: str,
    http_client: httpx.AsyncClient,
    logger: Any,
) -> tuple[str | None, bytes | None, bool]:
    if file_name.endswith(".gif"):
        return None, None, True
    current_url = str(url or "").strip()
    if not current_url:
        return None, None, False
    if not await _is_safe_remote_image_url(current_url, logger):
        return None, None, False

    try:
        for _ in range(_MAX_IMAGE_REDIRECTS):
            async with http_client.stream("GET", current_url, timeout=10, follow_redirects=False) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = str(resp.headers.get("Location") or resp.headers.get("location") or "").strip()
                    if not location:
                        raise ValueError("redirect without location")
                    next_url = urljoin(current_url, location)
                    if not await _is_safe_remote_image_url(next_url, logger):
                        raise ValueError("redirect target rejected")
                    current_url = next_url
                    continue
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
        raise ValueError("too many redirects")
    except Exception as e:
        logger.warning(f"下载图片失败或被拦截，已忽略原图 URL: {e}")
        return None, None, False


async def extract_mface_from_segment(
    seg: Any,
    *,
    http_client: httpx.AsyncClient,
    message_text_ref: List[str],
    image_urls: List[str],
    sticker_candidates_ref: List[IncomingStickerCandidate],
    logger: Any,
    stop_reply_ref: List[bool],
) -> None:
    if getattr(seg, "type", None) != "mface":
        return
    data = getattr(seg, "data", {}) or {}
    summary = str(data.get("summary", "") or "表情包").strip() or "表情包"
    url = str(data.get("url", "") or "").strip()
    file_name = str(data.get("file", "") or "").lower()
    if not url:
        message_text_ref.append(f"[{summary}]")
        return
    mime_type, payload, is_gif = await download_safe_image_bytes(
        url=url,
        file_name=file_name,
        http_client=http_client,
        logger=logger,
    )
    if is_gif:
        logger.info("拟人插件：检测到 GIF 表情包，忽略并不予回复")
        stop_reply_ref[0] = True
        return
    if payload is None or mime_type is None:
        message_text_ref.append(f"[{summary}]")
        return
    message_text_ref.append("[图片·表情包]")
    data_url = image_bytes_to_data_url(payload, mime_type)
    image_urls.append(data_url)
    sticker_candidates_ref.append(
        IncomingStickerCandidate(
            data_url=data_url,
            payload=payload,
            mime_type=mime_type,
            source_kind="mface",
            summary_hint=summary,
        )
    )


async def extract_images_from_segment(
    seg: Any,
    *,
    runtime: Any,
    http_client: httpx.AsyncClient,
    message_text_ref: List[str],
    image_urls: List[str],
    sticker_candidates_ref: List[IncomingStickerCandidate],
    logger: Any,
    stop_reply_ref: List[bool],
) -> None:
    if getattr(seg, "type", None) != "image":
        return
    data = getattr(seg, "data", {})
    url = data.get("url")
    file_name = str(data.get("file", "")).lower()
    if not url:
        return

    mime_type, payload, is_gif = await download_safe_image_bytes(
        url=url,
        file_name=file_name,
        http_client=http_client,
        logger=logger,
    )
    if is_gif:
        logger.info("拟人插件：检测到 GIF 图片，忽略并不予回复")
        stop_reply_ref[0] = True
        return
    if payload is None or mime_type is None:
        message_text_ref.append("[图片·表情包]")
        return

    data_url = image_bytes_to_data_url(payload, mime_type)
    classification = await classify_incoming_image(
        runtime=runtime,
        image_url=data_url,
        source_kind="image",
        width=data.get("width", 0),
        height=data.get("height", 0),
        file_id=str(data.get("file") or data.get("file_id") or "").strip(),
    )
    is_sticker_like = classification.is_sticker_like
    message_text_ref.append(classification.text_label)
    image_urls.append(data_url)
    record_counter(
        "incoming_image.classified_total",
        kind=classification.kind,
        source=classification.source,
    )
    if is_sticker_like:
        sticker_candidates_ref.append(
            IncomingStickerCandidate(
                data_url=data_url,
                payload=payload,
                mime_type=mime_type,
                source_kind="image",
                summary_hint=classification.reason,
            )
        )


async def extract_reply_images(
    seg_type: str,
    data: Dict[str, Any],
    *,
    http_client: httpx.AsyncClient,
    message_text_ref: List[str],
    image_urls: List[str],
    logger: Any,
    stop_reply_ref: List[bool],
) -> None:
    if seg_type != "image":
        return
    url = data.get("url")
    file_name = str(data.get("file", "")).lower()
    if not url:
        return
    mime_type, payload, is_gif = await download_safe_image_bytes(
        url=url,
        file_name=file_name,
        http_client=http_client,
        logger=logger,
    )
    if is_gif:
        stop_reply_ref[0] = True
        return
    message_text_ref.append("[图片]")
    if payload is None or mime_type is None:
        return
    base64_data = base64.b64encode(payload).decode("utf-8")
    image_urls.append(f"data:{mime_type};base64,{base64_data}")


async def maybe_choose_reply_sticker(
    *,
    runtime: Any,
    group_id: str,
    group_config: dict[str, Any],
    semantic_frame: Any,
    reply_content: str,
    raw_message_text: str,
    message_text: str,
    message_content: str,
    image_summary_suffix: str,
    is_private_session: bool,
    is_random_chat: bool,
    is_group_idle_active: bool,
    force_mode: str | None,
    strip_injected_visual_summary: Callable[[str], str],
) -> tuple[Any | None, str]:
    sticker_segment = None
    sticker_name = ""
    should_get_sticker = False
    if not bool(getattr(semantic_frame, "sticker_appropriate", True)):
        record_counter("reply_sticker.skipped_total", reason="semantic_gate")
        return None, ""

    is_sticker_enabled = group_config.get("sticker_enabled", True)
    sticker_now = time.time()
    sticker_existing = _GROUP_STICKER_STATE.get(str(group_id))
    if sticker_existing and sticker_now - float(sticker_existing.get("last_sent", 0) or 0) > _STICKER_STATE_TTL_SECONDS:
        del _GROUP_STICKER_STATE[str(group_id)]
    sticker_state = _GROUP_STICKER_STATE.setdefault(str(group_id), {"last_sent": 0.0, "recent": []})
    sticker_in_cooldown = (
        not is_private_session
        and (sticker_now - float(sticker_state.get("last_sent", 0) or 0)) < _STICKER_COOLDOWN_SECONDS
    )
    if not is_sticker_enabled:
        record_counter("reply_sticker.skipped_total", reason="group_disabled")
    if is_sticker_enabled:
        if force_mode == "mixed":
            should_get_sticker = True
        elif force_mode == "text_only":
            should_get_sticker = False
        elif not sticker_in_cooldown and random.random() < runtime.plugin_config.personification_sticker_probability:
            should_get_sticker = True
            if not is_private_session:
                sticker_state["last_sent"] = sticker_now

    if not should_get_sticker:
        record_counter(
            "reply_sticker.skipped_total",
            reason="cooldown" if sticker_in_cooldown else "probability",
        )
        return None, ""

    sticker_dir = resolve_sticker_dir(getattr(runtime.plugin_config, "personification_sticker_path", None))
    if not (sticker_dir.exists() and sticker_dir.is_dir()):
        return None, ""

    recent_stickers: List[str] = list(sticker_state.get("recent", []) or [])
    feedback_state = await load_sticker_feedback()
    chosen = await choose_sticker_for_context(
        sticker_dir,
        mood=str(getattr(semantic_frame, "sticker_mood_hint", "") or getattr(semantic_frame, "bot_emotion", "") or reply_content),
        context=(
            f"用户刚说：{raw_message_text or message_text or message_content}\n"
            f"你准备回：{reply_content[:80]}"
        ),
        draft_reply=reply_content[:120],
        current_visual_summary=strip_injected_visual_summary(image_summary_suffix),
        proactive=bool(is_random_chat or is_group_idle_active),
        plugin_config=runtime.plugin_config,
        call_ai_api=runtime.call_ai_api,
        minimum_score=2,
        excluded_stickers=recent_stickers,
        feedback_state=feedback_state,
    )
    if chosen is None:
        record_counter("reply_sticker.skipped_total", reason="no_match")
        return None, ""

    sticker_name = chosen.stem
    sticker_segment = runtime.message_segment_cls.image(f"file:///{chosen.absolute()}")
    runtime.logger.info(f"拟人插件：按语义挑选表情包 {chosen.name}")
    record_counter("reply_sticker.selected_total", sticker=sticker_name)
    if is_private_session:
        sticker_state["last_sent"] = sticker_now
    recent_stickers.append(sticker_name)
    sticker_state["recent"] = recent_stickers[-_STICKER_DEDUP_WINDOW:]
    return sticker_segment, sticker_name


__all__ = [
    "IncomingStickerCandidate",
    "auto_collect_stickers",
    "build_image_summary_suffix",
    "download_safe_image_bytes",
    "extract_images_from_segment",
    "extract_mface_from_segment",
    "extract_reply_images",
    "maybe_choose_reply_sticker",
    "spawn_auto_collect_stickers",
]
