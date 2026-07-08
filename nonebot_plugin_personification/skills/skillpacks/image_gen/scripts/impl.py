from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from plugin.personification.core.image_refs import normalize_image_refs


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


def _first_gemini_cli_caller(tool_caller: Any) -> Any:
    candidates = [tool_caller]
    candidates.extend(list(getattr(tool_caller, "_primary_callers", []) or []))
    fallback = getattr(tool_caller, "_fallback_caller", None)
    if fallback is not None:
        candidates.append(fallback)
    for caller in candidates:
        if caller is None:
            continue
        if caller.__class__.__name__ in {"GeminiCliToolCaller", "AntigravityCliToolCaller"}:
            return caller
    return None


def _normalize_api_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"", "auto", "default"}:
        return "auto"
    if text in {"gemini", "gemini_official", "google"}:
        return "gemini"
    if text in {"openai", "openai_compatible", "compatible"}:
        return "openai"
    return text


def _configured_image_route(plugin_config: Any) -> dict[str, str]:
    explicit_type = _normalize_api_type(getattr(plugin_config, "personification_image_gen_api_type", "auto"))
    explicit_url = str(getattr(plugin_config, "personification_image_gen_api_url", "") or "").strip()
    explicit_key = str(getattr(plugin_config, "personification_image_gen_api_key", "") or "").strip()
    main_type = _normalize_api_type(getattr(plugin_config, "personification_api_type", "openai"))
    main_url = str(getattr(plugin_config, "personification_api_url", "") or "").strip()
    main_key = str(getattr(plugin_config, "personification_api_key", "") or "").strip()
    route_type = explicit_type
    return {
        "api_type": route_type,
        "main_api_type": main_type,
        "api_url": explicit_url or main_url,
        "api_key": explicit_key or main_key,
    }


def _gemini_image_model(plugin_config: Any, image_model: str) -> str:
    configured = str(image_model or "").strip()
    if configured and not configured.lower().startswith(("gpt-image", "chatgpt-image")):
        return configured
    fallback = str(
        getattr(plugin_config, "personification_image_gen_nanobanan_model", "")
        or configured
        or "gemini-3-pro-image-preview"
    ).strip()
    return fallback or "gemini-3-pro-image-preview"


def _strip_known_endpoint_suffix(url: str, suffixes: tuple[str, ...]) -> str:
    cleaned = str(url or "").strip().rstrip("/")
    lower = cleaned.lower()
    for suffix in suffixes:
        if lower.endswith(suffix):
            return cleaned[: -len(suffix)].rstrip("/")
    return cleaned


def _openai_image_endpoint(api_url: str) -> str:
    base = _strip_known_endpoint_suffix(
        api_url or "https://api.openai.com/v1",
        ("/images/generations", "/responses", "/chat/completions", "/models"),
    )
    if not re.search(r"/v\d+(?:beta)?(?:/openai)?$", base, flags=re.I):
        base = urljoin(base.rstrip("/") + "/", "v1")
    return base.rstrip("/") + "/images/generations"


def _gemini_generate_endpoint(api_url: str, model: str, api_key: str) -> tuple[str, dict[str, str], dict[str, str]]:
    cleaned = str(api_url or "").strip().rstrip("/")
    if not cleaned:
        cleaned = "https://generativelanguage.googleapis.com/v1beta"
    cleaned = _strip_known_endpoint_suffix(
        cleaned,
        ("/chat/completions", "/models", "/openai/models", "/openai"),
    )
    cleaned = re.sub(r"/models/[^/]+:generateContent$", "", cleaned, flags=re.I).rstrip("/")
    if not re.search(r"/v\d+(?:beta)?$", cleaned, flags=re.I):
        cleaned = cleaned.rstrip("/") + "/v1beta"
    endpoint = f"{cleaned}/models/{quote(str(model or '').strip(), safe='')}:generateContent"
    headers = {"Content-Type": "application/json"}
    params: dict[str, str] = {}
    if "generativelanguage.googleapis.com" in endpoint:
        if api_key:
            params["key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return endpoint, headers, params


def _split_data_url(data_url: str) -> tuple[str, str] | None:
    if not data_url.startswith("data:"):
        return None
    head, _, body = data_url.partition(",")
    if not body:
        return None
    head = head[len("data:") :]
    mime_type, _, encoding = head.partition(";")
    if encoding != "base64":
        return None
    return (mime_type or "image/png", body)


def _extract_openai_image(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    items = data.get("data")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            b64 = str(item.get("b64_json") or item.get("image_base64") or "").strip()
            if b64:
                return b64, ""
            url = item.get("url")
            if url:
                return "", str(url)
    for key in ("b64_json", "image_base64", "base64", "data_url", "image"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            if value.startswith("data:image/"):
                return value.split(",", 1)[1], ""
            return value.strip(), ""
    return "", ""


def _extract_gemini_image(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    candidates = list(data.get("candidates", []) or [])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        parts = list(content.get("parts", []) or []) if isinstance(content, dict) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict):
                payload = str(inline.get("data", "") or "").strip()
                if payload:
                    return payload, str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
    return "", ""


async def _download_image_as_b64(url: str, *, api_key: str = "", timeout: float = 180.0) -> str:
    headers = {"Accept": "image/*,*/*"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(float(timeout or 180.0), connect=15.0)) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
    payload = bytes(response.content or b"")
    return base64.b64encode(payload).decode("ascii") if payload else ""


def _error_from_http(exc: httpx.HTTPStatusError) -> str:
    body = ""
    try:
        body = exc.response.text[:360]
    except Exception:
        pass
    return f"HTTP {exc.response.status_code}: {body}"


async def _generate_image_openai_http(
    prompt_text: str,
    *,
    api_url: str,
    api_key: str,
    image_model: str,
    size: str,
    timeout: float,
) -> dict[str, str]:
    if not api_key:
        return {"error": "missing image generation api key"}
    endpoint = _openai_image_endpoint(api_url)
    payload = {
        "model": str(image_model or "gpt-image-2").strip() or "gpt-image-2",
        "prompt": prompt_text,
        "size": normalize_image_generation_size(size),
        "n": 1,
        "response_format": "b64_json",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(float(timeout or 180.0), connect=15.0)) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": _error_from_http(exc)}
    except Exception as exc:
        return {"error": f"request failed: {exc}"}
    b64, url = _extract_openai_image(data)
    if not b64 and url:
        try:
            b64 = await _download_image_as_b64(url, api_key=api_key, timeout=timeout)
        except Exception as exc:
            return {"error": f"image download failed: {exc}"}
    return {"b64_json": b64} if b64 else {"error": f"empty image response: {json.dumps(data, ensure_ascii=False)[:360]}"}


async def _generate_image_gemini_http(
    prompt_text: str,
    *,
    api_url: str,
    api_key: str,
    image_model: str,
    size: str,
    timeout: float,
    images: list[str] | None = None,
    image_urls: list[str] | None = None,
) -> dict[str, str]:
    model = str(image_model or "gemini-3-pro-image-preview").strip() or "gemini-3-pro-image-preview"
    endpoint, headers, params = _gemini_generate_endpoint(api_url, model, api_key)
    raw_refs = list(images or []) + list(image_urls or [])
    reference_images, reference_problems = normalize_image_refs(raw_refs, limit=3)
    parts: list[dict[str, Any]] = [
        {
            "text": (
                f"生成一张图片。尺寸/构图参考：{normalize_image_generation_size(size)}。\n"
                f"用户提示词：{prompt_text}"
            )
        }
    ]
    for ref in reference_images:
        parsed = _split_data_url(ref)
        if parsed:
            mime_type, b64 = parsed
            parts.append({"inline_data": {"mime_type": mime_type, "data": b64}})
        else:
            parts.append({"text": f"参考图片 URL: {ref}"})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(float(timeout or 180.0), connect=15.0)) as client:
            response = await client.post(endpoint, json=payload, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": _error_from_http(exc)}
    except Exception as exc:
        return {"error": f"request failed: {exc}"}
    b64, _mime = _extract_gemini_image(data)
    result = {"b64_json": b64} if b64 else {"error": f"empty image response: {json.dumps(data, ensure_ascii=False)[:360]}"}
    if b64 and reference_problems:
        result["warning"] = "some reference images were ignored: " + ", ".join(reference_problems[:3])
    return result


async def generate_image(
    prompt: str,
    *,
    tool_caller: Any,
    size: str = "1024x1024",
    image_model: str = "gpt-image-2",
    timeout: float | None = None,
    images: list[str] | None = None,
    image_urls: list[str] | None = None,
    reference_mode: str = "auto",
    plugin_config: Any | None = None,
) -> dict[str, str]:
    """Unified image generation entrypoint.

    Chooses the current Codex/Gemini CLI route when available, otherwise uses
    configured OpenAI-compatible or Gemini HTTP image APIs.
    """
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {"error": "empty prompt"}
    plugin_config = plugin_config or object()
    route = _configured_image_route(plugin_config)
    route_type = route.get("api_type") or "auto"

    if route_type == "openai":
        return await _generate_image_openai_http(
            prompt_text,
            api_url=route.get("api_url", ""),
            api_key=route.get("api_key", ""),
            image_model=image_model,
            size=size,
            timeout=float(timeout or 180.0),
        )
    if route_type == "gemini":
        return await _generate_image_gemini_http(
            prompt_text,
            api_url=route.get("api_url", ""),
            api_key=route.get("api_key", ""),
            image_model=_gemini_image_model(plugin_config, image_model),
            size=size,
            timeout=float(timeout or 180.0),
            images=images,
            image_urls=image_urls,
        )

    caller = _first_codex_caller(tool_caller)
    if caller is None:
        gemini_caller = _first_gemini_cli_caller(tool_caller)
        if gemini_caller is not None:
            from .nanobanan import generate_image_nanobanan

            gemini_model = str(
                getattr(plugin_config, "personification_image_gen_nanobanan_model", "")
                or image_model
                or "gemini-3-pro-image-preview"
            ).strip() or "gemini-3-pro-image-preview"
            return await generate_image_nanobanan(
                prompt_text,
                tool_caller=tool_caller,
                size=normalize_image_generation_size(size),
                image_model=gemini_model,
                timeout=timeout,
                images=images,
                image_urls=image_urls,
            )
        if route.get("api_key"):
            guessed_type = route_type if route_type in {"openai", "gemini"} else route.get("main_api_type") or "openai"
            if guessed_type == "gemini" or "generativelanguage.googleapis.com" in route.get("api_url", ""):
                return await _generate_image_gemini_http(
                    prompt_text,
                    api_url=route.get("api_url", ""),
                    api_key=route.get("api_key", ""),
                    image_model=_gemini_image_model(plugin_config, image_model),
                    size=size,
                    timeout=float(timeout or 180.0),
                    images=images,
                    image_urls=image_urls,
                )
            return await _generate_image_openai_http(
                prompt_text,
                api_url=route.get("api_url", ""),
                api_key=route.get("api_key", ""),
                image_model=image_model,
                size=size,
                timeout=float(timeout or 180.0),
            )
        return {"error": "image generation route is not configured"}
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
    raw_refs = list(images or []) + list(image_urls or [])
    reference_images, reference_problems = normalize_image_refs(raw_refs, limit=3)
    result = await caller.generate_image(
        prompt_text,
        size=normalize_image_generation_size(str(size or "1024x1024")),
        image_model=str(image_model or "gpt-image-2").strip() or "gpt-image-2",
        images=reference_images,
        reference_mode=str(reference_mode or "auto").strip() or "auto",
    )
    if not isinstance(result, dict):
        return {"error": "invalid image response"}
    if reference_problems and "warning" not in result:
        result = dict(result)
        result["warning"] = "some reference images were ignored: " + ", ".join(reference_problems[:3])
    return result


__all__ = [
    "_first_gemini_cli_caller",
    "_first_codex_caller",
    "generate_image",
    "normalize_image_generation_size",
]
