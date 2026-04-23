from __future__ import annotations

from typing import Any, Sequence

from .image_result_cache import build_image_cache_key, get_cached_image_result, set_cached_image_result
from .visual_capabilities import (
    error_indicates_vision_unavailable,
    provider_supports_vision as resolve_provider_supports_vision,
)

_IMAGE_INPUT_MODES = {"auto", "direct", "summary", "disabled"}
_IMAGE_DETAIL_MODES = {"auto", "low", "high"}


def normalize_image_input_mode(value: Any) -> str:
    mode = str(value or "auto").strip().lower()
    return mode if mode in _IMAGE_INPUT_MODES else "auto"


def normalize_image_detail(value: Any) -> str:
    detail = str(value or "auto").strip().lower()
    return detail if detail in _IMAGE_DETAIL_MODES else "auto"


def provider_supports_vision(
    api_type: str,
    model: str | None = None,
    *,
    route_name: str = "",
) -> bool:
    return resolve_provider_supports_vision(api_type, model, route_name=route_name)


def is_image_input_unsupported_error(error: Any) -> bool:
    return error_indicates_vision_unavailable(error)


async def summarize_images_with_vision(
    *,
    vision_caller: Any,
    image_urls: Sequence[str],
    sticker_like: bool,
    sticker_prompt: str,
    person_prompt: str,
    cache_namespace: str,
    logger: Any,
) -> str:
    if vision_caller is None or not image_urls:
        return ""

    prompt = sticker_prompt if sticker_like else person_prompt
    prompt_key = "sticker_brief" if sticker_like else "person_first_brief"
    desc_parts: list[str] = []
    for img_url in image_urls:
        url = str(img_url or "").strip()
        if not url:
            continue
        cache_key = build_image_cache_key(
            url,
            {
                "version": cache_namespace,
                "task": "reply_preview",
                "prompt": prompt_key,
            },
        )
        cached_desc = await get_cached_image_result(cache_key)
        if cached_desc:
            desc_parts.append(str(cached_desc))
            continue
        try:
            desc = await vision_caller.describe(prompt, url)
        except Exception as exc:
            logger.warning(f"拟人插件：视觉模型描述图片失败: {exc}")
            continue
        if desc:
            text = str(desc).strip()
            if text:
                await set_cached_image_result(
                    cache_key,
                    text,
                    meta={"task": "reply_preview"},
                )
                desc_parts.append(text)
    return "；".join(desc_parts).strip()
