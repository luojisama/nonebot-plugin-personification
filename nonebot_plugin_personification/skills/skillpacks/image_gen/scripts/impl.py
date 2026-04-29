from __future__ import annotations

import re
from typing import Any


_IMAGE_SIZE_ALIASES = {
    "1024x1024": "1024x1024",
    "1:1": "1024x1024",
    "square": "1024x1024",
    "正方形": "1024x1024",
    "方图": "1024x1024",
    "1024x1536": "1024x1536",
    "1024x1792": "1024x1536",
    "portrait": "1024x1536",
    "vertical": "1024x1536",
    "竖版": "1024x1536",
    "竖图": "1024x1536",
    "纵向": "1024x1536",
    "3:4": "1024x1536",
    "2:3": "1024x1536",
    "9:16": "1024x1536",
    "1536x1024": "1536x1024",
    "1792x1024": "1536x1024",
    "landscape": "1536x1024",
    "horizontal": "1536x1024",
    "wide": "1536x1024",
    "横版": "1536x1024",
    "横图": "1536x1024",
    "横向": "1536x1024",
    "16:9": "1536x1024",
    "4:3": "1536x1024",
    "3:2": "1536x1024",
}
_DEDICATED_CODEX_CALLERS: dict[tuple[type, str, str, float], Any] = {}


def normalize_image_generation_size(size: str) -> str:
    value = str(size or "").strip().lower()
    value = value.replace("×", "x").replace("*", "x")
    value = re.sub(r"\s+", "", value)
    if not value:
        return "1024x1024"
    return _IMAGE_SIZE_ALIASES.get(value, "1024x1024")


def _first_codex_caller(tool_caller: Any) -> Any:
    candidates = [tool_caller]
    candidates.extend(list(getattr(tool_caller, "_primary_callers", []) or []))
    fallback = getattr(tool_caller, "_fallback_caller", None)
    if fallback is not None:
        candidates.append(fallback)
    for caller in candidates:
        if caller is None:
            continue
        if caller.__class__.__name__ != "OpenAICodexToolCaller":
            continue
        model = str(getattr(caller, "model", "") or "").strip()
        if model and callable(getattr(caller, "generate_image", None)):
            return caller
    return None


async def generate_image(
    prompt: str,
    *,
    tool_caller: Any,
    size: str = "1024x1024",
    image_model: str = "gpt-image-2",
    timeout: float | None = None,
) -> dict[str, str]:
    """通过 Codex 后端调用模型独有的 image_generation 托管工具生成图片。"""
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {"error": "empty prompt"}
    caller = _first_codex_caller(tool_caller)
    if caller is None:
        return {"error": "image generation only available on Codex model route"}
    if timeout is not None:
        try:
            timeout_value = float(timeout)
            caller_timeout = float(getattr(caller, "timeout", 0.0) or 0.0)
        except (TypeError, ValueError):
            timeout_value = 0.0
            caller_timeout = 0.0
        if (
            timeout_value > caller_timeout
            and hasattr(caller, "model")
            and hasattr(caller, "timeout")
            and hasattr(caller, "auth_path_override")
        ):
            key = (
                caller.__class__,
                str(getattr(caller, "model", "") or ""),
                str(getattr(caller, "auth_path_override", "") or ""),
                timeout_value,
            )
            dedicated = _DEDICATED_CODEX_CALLERS.get(key)
            if dedicated is None:
                dedicated = caller.__class__(
                    model=key[1],
                    auth_path=key[2],
                    timeout=timeout_value,
                )
                _DEDICATED_CODEX_CALLERS[key] = dedicated
            caller = dedicated
    result = await caller.generate_image(
        prompt_text,
        size=normalize_image_generation_size(str(size or "1024x1024")),
        image_model=str(image_model or "gpt-image-2").strip() or "gpt-image-2",
    )
    return result if isinstance(result, dict) else {"error": "invalid image response"}


__all__ = [
    "_first_codex_caller",
    "generate_image",
    "normalize_image_generation_size",
]
