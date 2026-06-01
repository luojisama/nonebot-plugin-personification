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
    judge_sticker_against_library,
    list_local_sticker_files,
    load_sticker_metadata,
    normalize_sticker_entry,
    recall_similar_stickers,
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
_COLLECT_COOLDOWN_STATE: Dict[str, float] = {}
_COLLECT_COOLDOWN_TTL_SECONDS = 3600


def _cleanup_collect_cooldown(now_ts: float) -> None:
    expired = [
        key
        for key, ts in _COLLECT_COOLDOWN_STATE.items()
        if now_ts - float(ts or 0.0) >= _COLLECT_COOLDOWN_TTL_SECONDS
    ]
    for key in expired:
        _COLLECT_COOLDOWN_STATE.pop(key, None)


def _collect_cooldown_key(group_id: str, user_id: str) -> str:
    return f"{str(group_id or '').strip()}::{str(user_id or '').strip()}"


def _can_collect_after_cooldown(group_id: str, user_id: str, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0:
        return True
    now_ts = time.time()
    _cleanup_collect_cooldown(now_ts)
    key = _collect_cooldown_key(group_id, user_id)
    last_ts = float(_COLLECT_COOLDOWN_STATE.get(key, 0.0) or 0.0)
    return (now_ts - last_ts) >= cooldown_seconds


def _mark_collect_cooldown(group_id: str, user_id: str) -> None:
    _COLLECT_COOLDOWN_STATE[_collect_cooldown_key(group_id, user_id)] = time.time()
_MAX_IMAGE_DOWNLOAD_BYTES = 8 * 1024 * 1024
_BLOCKED_IMAGE_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".corp")
_BLOCKED_IMAGE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
}
# QQ 协议常见图片下载域名。NTQQ 客户端会把图片下载请求重定向到
# 198.18.0.0/15 这种 RFC2544 测试网段（实际是客户端内部代理），按通用 SSRF
# 规则会被当作"内网/保留 IP"拒绝。这里把腾讯系图床显式信任，跳过 IP 解析检查。
_TRUSTED_IMAGE_HOST_SUFFIXES: tuple[str, ...] = (
    ".qq.com",
    ".qq.com.cn",
    ".qpic.cn",
    ".gtimg.com",
    ".gtimg.cn",
    ".myqcloud.com",
    ".tencent-cloud.net",
)
# 由 set_image_host_allowlist() 在插件启动时填充，反映用户的
# personification_image_host_allowlist 配置项。
_USER_IMAGE_HOST_ALLOWLIST: tuple[str, ...] = ()
_MAX_IMAGE_REDIRECTS = 5


def set_image_host_allowlist(suffixes: list[str] | tuple[str, ...] | str | None) -> None:
    """配置由用户追加的可信图片域名后缀列表。

    入参可以是 list / tuple / 逗号分隔字符串；空值会清空覆盖。
    每项会被自动 lowercase + 加前导 "."（如果缺失）。
    """
    global _USER_IMAGE_HOST_ALLOWLIST
    items: list[str] = []
    raw_iter: list[str]
    if suffixes is None:
        raw_iter = []
    elif isinstance(suffixes, str):
        raw_iter = [part.strip() for part in suffixes.split(",")]
    else:
        raw_iter = [str(part or "") for part in suffixes]
    for raw in raw_iter:
        cleaned = raw.strip().lower()
        if not cleaned:
            continue
        if not cleaned.startswith("."):
            cleaned = "." + cleaned
        if cleaned not in items:
            items.append(cleaned)
    _USER_IMAGE_HOST_ALLOWLIST = tuple(items)


def _is_image_host_trusted(host: str) -> bool:
    if not host:
        return False
    target = host.lower()
    for suffix in _TRUSTED_IMAGE_HOST_SUFFIXES + _USER_IMAGE_HOST_ALLOWLIST:
        if target.endswith(suffix):
            return True
    return False


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
    hard_limit = int(getattr(runtime.plugin_config, "personification_sticker_library_hard_limit", 1200) or 1200)
    soft_limit = int(getattr(runtime.plugin_config, "personification_sticker_library_soft_limit", 800) or 800)
    per_mood_limit = int(getattr(runtime.plugin_config, "personification_sticker_per_mood_limit", 50) or 50)
    meme_policy = str(getattr(runtime.plugin_config, "personification_sticker_collect_meme_policy", "reject") or "reject").strip().lower()
    cooldown_seconds = int(getattr(runtime.plugin_config, "personification_sticker_collect_cooldown_seconds", 60) or 0)
    sample_rate = float(getattr(runtime.plugin_config, "personification_sticker_collect_sample_rate", 0.5) or 0.0)
    sample_rate = max(0.0, min(1.0, sample_rate))
    min_confidence = float(getattr(runtime.plugin_config, "personification_sticker_collect_min_confidence", 0.7) or 0.0)
    min_confidence = max(0.0, min(1.0, min_confidence))
    second_judge_enabled = bool(getattr(runtime.plugin_config, "personification_sticker_second_judge_enabled", False))
    for candidate in candidates:
        try:
            current_files = list_local_sticker_files(sticker_dir)
            file_count = len(current_files)
            if file_count >= hard_limit:
                record_counter("sticker.collect_library_full", reason="hard_limit")
                runtime.logger.warning(f"拟人插件：表情包库已达硬上限 {hard_limit} 张，停止收集。")
                return
            if not _can_collect_after_cooldown(group_id, user_id, cooldown_seconds):
                record_counter("sticker.collect_skipped", reason="cooldown")
                continue
            if sample_rate < 1.0 and random.random() >= sample_rate:
                record_counter("sticker.collect_skipped", reason="sample_rate")
                continue
            result = await analyze_sticker_image(
                runtime=runtime,
                image_refs=[candidate.data_url],
                fallback_vision_caller=runtime.vision_caller,
            )
            record_counter("sticker.collect_attempt", style=result.style)
            if not result.is_sticker or not result.should_collect:
                if result.style == "meme" and meme_policy == "reject":
                    record_counter("sticker.collect_meme_rejected")
                continue
            if min_confidence > 0.0 and result.collect_confidence > 0.0 and result.collect_confidence < min_confidence:
                record_counter("sticker.collect_skipped", reason="low_confidence")
                continue
            if file_count >= soft_limit:
                runtime.logger.info(f"拟人插件：表情包库已达软上限 {soft_limit} 张，仍接受高质量收集但建议触发整理。")
            metadata = load_sticker_metadata(sticker_dir)
            mood_over_limit = False
            for mood_tag in result.mood_tags:
                mood_count = sum(
                    1
                    for entry in metadata.values()
                    if isinstance(entry, dict) and mood_tag in (entry.get("mood_tags") or [])
                )
                if mood_count >= per_mood_limit:
                    record_counter("sticker.collect_dedup", reason="per_mood_limit")
                    runtime.logger.debug(f"拟人插件：表情包情绪标签 {mood_tag} 已达上限 {per_mood_limit} 张，跳过收集。")
                    mood_over_limit = True
                    break
            if mood_over_limit:
                continue

            if second_judge_enabled and result.style != "meme":
                metadata = load_sticker_metadata(sticker_dir)
                similar = recall_similar_stickers(
                    metadata,
                    mood_tags=result.mood_tags,
                    scene_tags=result.scene_tags,
                    top_k=12,
                )
                if similar:
                    judge = await judge_sticker_against_library(
                        runtime=runtime,
                        sticker_data_url=candidate.data_url,
                        sticker_summary=result.summary,
                        sticker_description=result.description,
                        sticker_mood_tags=result.mood_tags,
                        sticker_scene_tags=result.scene_tags,
                        similar_candidates=similar,
                    )
                    decision = str(judge.get("decision", "collect") or "collect")
                    record_counter(f"sticker.second_judge_{decision}")
                    if decision != "collect":
                        runtime.logger.debug(f"拟人插件：表情包二次判断 {decision}，跳过收集。原因：{judge.get('reason', '')}")
                        continue
                    tag_correction = judge.get("tag_correction", {})
                    if isinstance(tag_correction, dict):
                        corrected_mood = tag_correction.get("mood_tags", [])
                        corrected_scene = tag_correction.get("scene_tags", [])
                        if isinstance(corrected_mood, list) and corrected_mood:
                            result = result.__class__(**{**result.__dict__, "mood_tags": [str(t) for t in corrected_mood[:4]]})
                        if isinstance(corrected_scene, list) and corrected_scene:
                            result = result.__class__(**{**result.__dict__, "scene_tags": [str(t) for t in corrected_scene[:4]]})

            saved_path, created, file_hash = await save_collected_sticker(
                sticker_dir,
                payload=candidate.payload,
                mime_type=candidate.mime_type,
                file_name_hint=result.summary or result.description or "sticker",
            )
            if not created:
                record_counter("sticker.collect_dedup", reason="hash_duplicate")
                continue
            _mark_collect_cooldown(group_id, user_id)
            metadata[saved_path.name] = normalize_sticker_entry(
                {
                    "description": result.description,
                    "mood_tags": result.mood_tags,
                    "scene_tags": result.scene_tags,
                    "proactive_send": result.proactive_send,
                    "ocr_text": result.ocr_text,
                    "use_hint": result.use_hint,
                    "avoid_hint": result.avoid_hint,
                    "style": result.style,
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
    # 信任的图床域名（QQ 系 + 用户配置 allowlist）跳过 IP 解析检查，
    # 解决 NTQQ 把图片重定向到 198.18.0.0/15 客户端代理时被误判为内网的问题。
    if _is_image_host_trusted(host):
        return True

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
    sticker_image_urls: List[str] | None = None,
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
    # 非 gif 表情包 → 走 sticker_image_urls，由上层决定是否直传视觉模型理解（不打标）。
    if sticker_image_urls is not None:
        sticker_image_urls.append(data_url)
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
    sticker_image_urls: List[str] | None = None,
) -> None:
    if getattr(seg, "type", None) != "image":
        return
    data = getattr(seg, "data", {})
    url = data.get("url")
    file_name = str(data.get("file", "")).lower()
    if not url:
        return

    # OneBot v11 / NTQQ：sub_type=1 表示这是表情包（非真实照片）。
    # 直接走快速路径：不调 vision 分类、不把 data_url 注入 image_urls
    #（避免主对话 LLM 把表情包像素当成真实图片去"解释内容"）。
    # 兼容协议端字段类型差异：int、str、None 都判一遍。
    raw_sub_type = data.get("sub_type", data.get("subType", 0))
    try:
        sub_type_int = int(raw_sub_type) if raw_sub_type is not None else 0
    except (TypeError, ValueError):
        sub_type_int = 0
    is_protocol_sticker = sub_type_int == 1

    if is_protocol_sticker:
        summary = str(data.get("summary") or data.get("emoji_id") or "").strip()
        placeholder = f"[对方发送了一个表情包：{summary}]" if summary else "[对方发送了一个表情包]"
        message_text_ref.append(placeholder)
        record_counter(
            "incoming_image.classified_total",
            kind="sticker",
            source="onebot_subtype",
        )
        # 下载用于 sticker_candidates_ref（让插件能学习用户常用的表情包）；
        # 非 gif 的同时放进 sticker_image_urls，由上层决定是否直传视觉模型理解（不打标）。
        try:
            mime_type, payload, _is_gif = await download_safe_image_bytes(
                url=url,
                file_name=file_name,
                http_client=http_client,
                logger=logger,
            )
        except Exception:
            mime_type, payload, _is_gif = None, None, False
        if payload is not None and mime_type is not None:
            data_url = image_bytes_to_data_url(payload, mime_type)
            if not _is_gif and sticker_image_urls is not None:
                sticker_image_urls.append(data_url)
            sticker_candidates_ref.append(
                IncomingStickerCandidate(
                    data_url=data_url,
                    payload=payload,
                    mime_type=mime_type,
                    source_kind="onebot_sticker",
                    summary_hint=summary or "动画表情",
                )
            )
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
    # 真实照片 → image_urls（走原视觉/摘要路径）；
    # 表情包 → sticker_image_urls，由上层决定是否直传视觉模型理解（不打标）。
    if not is_sticker_like:
        image_urls.append(data_url)
    elif sticker_image_urls is not None:
        sticker_image_urls.append(data_url)
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
            # mixed 仅来自随机水群表情路径：仍尊重群级冷却，避免短时间内连甩表情刷屏，
            # 并记录 last_sent，让随后的普通回复也守同一条冷却线。
            should_get_sticker = not sticker_in_cooldown
            if should_get_sticker and not is_private_session:
                sticker_state["last_sent"] = sticker_now
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
