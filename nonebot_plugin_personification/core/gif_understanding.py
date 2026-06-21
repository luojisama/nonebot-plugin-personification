from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import math
import time
from dataclasses import dataclass, replace
from typing import Any

from .media_understanding import analyze_images_with_route_or_fallback
from .visual_capabilities import VISUAL_ROUTE_REPLY_PLAIN


_DEFAULT_GIF_TIMEOUT_SECONDS = 12.0
_MAX_GIF_TIMEOUT_SECONDS = 35.0
_DEFAULT_GIF_MAX_BYTES = 8 * 1024 * 1024
_MAX_GIF_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_GIF_MAX_DECODE_FRAMES = 180
_DEFAULT_GIF_SAMPLE_FRAMES = 8
_DEFAULT_GIF_CONTACT_SHEET_LONG_EDGE = 1600
_GIF_CACHE_MAX_ITEMS = 256
_GIF_SUMMARY_MAX_CHARS = 180

_GIF_SUMMARY_CACHE: dict[str, "GifSummaryResult"] = {}
_GIF_CACHE_ORDER: list[str] = []


@dataclass
class GifSummaryResult:
    summary: str
    route: str
    frame_count: int = 0
    sampled_frames: int = 0
    duration_ms: int = 0
    error: str = ""


@dataclass
class _FrameSample:
    index: int
    timestamp_ms: int
    image: Any


@dataclass
class _ContactSheet:
    data_url: str
    frame_count: int
    sampled_frames: int
    duration_ms: int


def is_gif_understanding_enabled(runtime_or_config: Any) -> bool:
    config = getattr(runtime_or_config, "plugin_config", runtime_or_config)
    return bool(getattr(config, "personification_gif_understanding_enabled", False))


def get_gif_understanding_timeout(config: Any) -> float:
    return _get_float(
        config,
        "personification_gif_understanding_timeout",
        _DEFAULT_GIF_TIMEOUT_SECONDS,
        min_value=1.0,
        max_value=_MAX_GIF_TIMEOUT_SECONDS,
    )


def get_gif_max_bytes(config: Any) -> int:
    return _get_int(
        config,
        "personification_gif_max_bytes",
        _DEFAULT_GIF_MAX_BYTES,
        min_value=64 * 1024,
        max_value=_MAX_GIF_MAX_BYTES,
    )


def get_gif_max_per_turn(config: Any) -> int:
    return _get_int(
        config,
        "personification_gif_max_per_turn",
        1,
        min_value=1,
        max_value=3,
    )


async def summarize_gif_bytes(
    *,
    runtime: Any,
    payload: bytes,
    source_hint: str = "",
    summary_hint: str = "",
) -> GifSummaryResult:
    config = getattr(runtime, "plugin_config", None)
    if config is None:
        return GifSummaryResult(summary="", route="missing_config")
    if not is_gif_understanding_enabled(config):
        return GifSummaryResult(summary="", route="disabled")

    payload_size = len(payload or b"")
    max_bytes = get_gif_max_bytes(config)
    if payload_size <= 0:
        return GifSummaryResult(summary="", route="empty_payload")
    if payload_size > max_bytes:
        return GifSummaryResult(summary="", route="too_large", error=f"{payload_size}>{max_bytes}")

    started_at = time.monotonic()
    timeout = get_gif_understanding_timeout(config)
    try:
        result = await asyncio.wait_for(
            _summarize_gif_bytes_inner(
                runtime=runtime,
                payload=payload,
                source_hint=source_hint,
                summary_hint=summary_hint,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return GifSummaryResult(
            summary="",
            route="timeout",
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
    except Exception as exc:
        _log_warning(runtime, f"拟人插件：GIF 理解失败: {exc}")
        return GifSummaryResult(
            summary="",
            route="error",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            error=str(exc),
        )
    return replace(result, duration_ms=int((time.monotonic() - started_at) * 1000))


async def _summarize_gif_bytes_inner(
    *,
    runtime: Any,
    payload: bytes,
    source_hint: str,
    summary_hint: str,
) -> GifSummaryResult:
    config = getattr(runtime, "plugin_config", None)
    cache_enabled = bool(getattr(config, "personification_gif_summary_cache_enabled", True))
    cache_key = hashlib.sha256(payload).hexdigest() if cache_enabled else ""
    if cache_key:
        cached = _GIF_SUMMARY_CACHE.get(cache_key)
        if cached is not None:
            return replace(cached, route="cache")

    sheet = await asyncio.to_thread(_build_contact_sheet_data_url, payload, config)
    prompt = _build_gif_summary_prompt(
        source_hint=source_hint,
        summary_hint=summary_hint,
        frame_count=sheet.frame_count,
        sampled_frames=sheet.sampled_frames,
        duration_ms=sheet.duration_ms,
    )
    summary_text, route = await analyze_images_with_route_or_fallback(
        runtime=runtime,
        prompt=prompt,
        image_refs=[sheet.data_url],
        route_name=VISUAL_ROUTE_REPLY_PLAIN,
        image_detail="low",
        fallback_vision_caller=getattr(runtime, "vision_caller", None),
    )
    summary = _normalize_summary(summary_text)
    result = GifSummaryResult(
        summary=summary,
        route=route or ("ok" if summary else "vision_unavailable"),
        frame_count=sheet.frame_count,
        sampled_frames=sheet.sampled_frames,
        duration_ms=sheet.duration_ms,
    )
    if cache_key and summary:
        _cache_set(cache_key, result)
    return result


def _build_gif_summary_prompt(
    *,
    source_hint: str,
    summary_hint: str,
    frame_count: int,
    sampled_frames: int,
    duration_ms: int,
) -> str:
    hints: list[str] = []
    if summary_hint:
        hints.append(f"协议端给出的表情说明：{summary_hint}")
    if source_hint:
        hints.append(f"来源：{source_hint}")
    hint_text = "\n".join(hints)
    duration_text = f"{duration_ms / 1000:.1f}s" if duration_ms > 0 else "未知"
    return (
        "这是一张从同一个 GIF/动态表情中按时间顺序抽取关键帧后生成的拼图，"
        "编号越大表示越靠后的画面，不是多张无关图片。\n"
        f"总帧数约 {frame_count}，本次展示 {sampled_frames} 帧，估算时长 {duration_text}。\n"
        f"{hint_text}\n"
        "请按短动画理解它，用中文输出一段给聊天上下文使用的摘要。"
        "重点说明主体、动作变化、表情/情绪、画面文字，以及它作为表情包可能想表达的意思。"
        "不要提到拼图、抽帧、帧号或你在分析图片；控制在 80 字以内，直接输出摘要。"
    ).strip()


def _build_contact_sheet_data_url(payload: bytes, config: Any) -> _ContactSheet:
    Image, ImageDraw, ImageSequence = _load_pillow()
    with Image.open(io.BytesIO(payload)) as image:
        frame_count = max(1, int(getattr(image, "n_frames", 1) or 1))
        max_decode_frames = _get_int(
            config,
            "personification_gif_max_decode_frames",
            _DEFAULT_GIF_MAX_DECODE_FRAMES,
            min_value=1,
            max_value=360,
        )
        max_samples = _get_int(
            config,
            "personification_gif_sample_frames",
            _DEFAULT_GIF_SAMPLE_FRAMES,
            min_value=1,
            max_value=12,
        )
        decode_frames = min(frame_count, max_decode_frames)
        selected_indices = _select_frame_indices(decode_frames, max_samples)
        selected_set = set(selected_indices)
        samples: list[_FrameSample] = []
        elapsed_ms = 0

        for index, frame in enumerate(ImageSequence.Iterator(image)):
            if index >= decode_frames:
                break
            duration = _frame_duration_ms(frame)
            if index in selected_set:
                samples.append(
                    _FrameSample(
                        index=index,
                        timestamp_ms=elapsed_ms,
                        image=frame.convert("RGBA").copy(),
                    )
                )
            elapsed_ms += duration

    if not samples:
        raise ValueError("gif_has_no_decodable_frames")

    long_edge = _get_int(
        config,
        "personification_gif_contact_sheet_long_edge",
        _DEFAULT_GIF_CONTACT_SHEET_LONG_EDGE,
        min_value=512,
        max_value=2400,
    )
    sheet = _render_contact_sheet(samples, long_edge=long_edge, Image=Image, ImageDraw=ImageDraw)
    out = io.BytesIO()
    sheet.convert("RGB").save(out, format="JPEG", quality=82, optimize=True)
    encoded = base64.b64encode(out.getvalue()).decode("ascii")
    return _ContactSheet(
        data_url=f"data:image/jpeg;base64,{encoded}",
        frame_count=frame_count,
        sampled_frames=len(samples),
        duration_ms=elapsed_ms,
    )


def _select_frame_indices(frame_count: int, max_samples: int) -> list[int]:
    frame_count = max(0, int(frame_count or 0))
    max_samples = max(1, int(max_samples or 1))
    if frame_count <= 0:
        return []
    if frame_count <= max_samples:
        return list(range(frame_count))
    if max_samples == 1:
        return [0]

    step = 6
    for candidate in (2, 3, 4, 5, 6):
        if math.ceil(frame_count / candidate) <= max_samples:
            step = candidate
            break
    indices = list(range(0, frame_count, step))
    last_index = frame_count - 1
    if indices[-1] != last_index:
        indices.append(last_index)
    if len(indices) > max_samples:
        spread: list[int] = []
        for pos in range(max_samples):
            source_pos = round(pos * (len(indices) - 1) / (max_samples - 1))
            spread.append(indices[source_pos])
        indices = spread
    deduped: list[int] = []
    for index in indices:
        if index not in deduped:
            deduped.append(index)
    if deduped[-1] != last_index and len(deduped) < max_samples:
        deduped.append(last_index)
    return deduped


def _render_contact_sheet(samples: list[_FrameSample], *, long_edge: int, Image: Any, ImageDraw: Any) -> Any:
    count = len(samples)
    columns = min(4, count)
    rows = math.ceil(count / columns)
    gap = 10
    label_height = 24
    available_width = max(320, int(long_edge or _DEFAULT_GIF_CONTACT_SHEET_LONG_EDGE))
    cell_size = max(128, min(360, (available_width - gap * (columns + 1)) // columns))
    width = columns * cell_size + gap * (columns + 1)
    height = rows * (cell_size + label_height) + gap * (rows + 1)
    sheet = Image.new("RGBA", (width, height), (246, 247, 249, 255))
    draw = ImageDraw.Draw(sheet)
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")

    for order, sample in enumerate(samples):
        row = order // columns
        col = order % columns
        x = gap + col * (cell_size + gap)
        y = gap + row * (cell_size + label_height + gap)
        draw.rectangle(
            (x, y, x + cell_size, y + cell_size),
            fill=(255, 255, 255, 255),
            outline=(215, 219, 225, 255),
            width=1,
        )
        frame = sample.image.copy()
        frame.thumbnail((cell_size - 8, cell_size - 8), resampling)
        paste_x = x + (cell_size - frame.width) // 2
        paste_y = y + (cell_size - frame.height) // 2
        sheet.alpha_composite(frame, (paste_x, paste_y))
        label = f"#{order + 1} {sample.timestamp_ms / 1000:.1f}s"
        draw.text((x + 4, y + cell_size + 5), label, fill=(52, 58, 64, 255))
    return sheet


def _frame_duration_ms(frame: Any) -> int:
    try:
        duration = int(frame.info.get("duration", 100) or 100)
    except Exception:
        duration = 100
    return max(20, min(10_000, duration))


def _normalize_summary(text: str) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if len(normalized) > _GIF_SUMMARY_MAX_CHARS:
        return normalized[: _GIF_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return normalized


def _get_int(config: Any, field: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(getattr(config, field, default) or default)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _get_float(config: Any, field: str, default: float, *, min_value: float, max_value: float) -> float:
    try:
        value = float(getattr(config, field, default) or default)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _load_pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageSequence
    except ModuleNotFoundError as exc:
        raise RuntimeError("pillow_missing_for_gif_understanding") from exc
    return Image, ImageDraw, ImageSequence


def _cache_set(key: str, value: GifSummaryResult) -> None:
    if key not in _GIF_SUMMARY_CACHE:
        _GIF_CACHE_ORDER.append(key)
    _GIF_SUMMARY_CACHE[key] = value
    while len(_GIF_CACHE_ORDER) > _GIF_CACHE_MAX_ITEMS:
        expired = _GIF_CACHE_ORDER.pop(0)
        _GIF_SUMMARY_CACHE.pop(expired, None)


def _log_warning(runtime: Any, message: str) -> None:
    logger = getattr(runtime, "logger", None)
    if logger is None:
        return
    try:
        logger.warning(message)
    except Exception:
        pass


__all__ = [
    "GifSummaryResult",
    "get_gif_max_bytes",
    "get_gif_max_per_turn",
    "get_gif_understanding_timeout",
    "is_gif_understanding_enabled",
    "summarize_gif_bytes",
]
