from __future__ import annotations

import base64
import binascii
import io
import json
import re
import struct
import zlib
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from plugin.personification.core.gemini_transport import (
    is_google_gemini_endpoint,
    raise_for_gemini_status,
    request_with_gemini_auth,
)
from plugin.personification.core.image_refs import normalize_image_refs
from plugin.personification.core.llm_context import use_single_attempt_retry_policy
from plugin.personification.core.safe_image_download import download_public_image


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
_DEDICATED_CODEX_CALLERS: dict[tuple[type, str, str, float, str], Any] = {}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


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
    auth_mode = str(getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto").strip()
    main_type = _normalize_api_type(getattr(plugin_config, "personification_api_type", "openai"))
    main_url = str(getattr(plugin_config, "personification_api_url", "") or "").strip()
    main_key = str(getattr(plugin_config, "personification_api_key", "") or "").strip()
    dedicated = any((explicit_type != "auto", explicit_url, explicit_key))
    return {
        "api_type": explicit_type,
        "main_api_type": main_type,
        "api_url": explicit_url if dedicated else main_url,
        "api_key": explicit_key if dedicated else main_key,
        "gemini_auth_mode": auth_mode,
        "dedicated": "1" if dedicated else "",
    }


def _pool_image_routes(plugin_config: Any) -> list[dict[str, Any]]:
    raw = getattr(plugin_config, "personification_api_pools", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(raw, list):
        return []
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        api_type = _normalize_api_type(item.get("api_type"))
        api_url = str(item.get("api_url", "") or "").strip()
        api_key = str(item.get("api_key", "") or "").strip()
        if api_type not in {"openai", "gemini"} or not api_url or not api_key:
            continue
        try:
            priority = int(item.get("priority", index) or 0)
        except (TypeError, ValueError):
            priority = index
        candidates.append({
            "name": str(item.get("name") or f"pool_{index + 1}"),
            "api_type": api_type,
            "api_url": api_url,
            "api_key": api_key,
            "model": str(item.get("image_model") or item.get("model") or "").strip(),
            "priority": priority,
            "proxy": str(item.get("proxy", "") or "").strip(),
            "gemini_auth_mode": str(item.get("gemini_auth_mode", "auto") or "auto").strip(),
        })
    if not candidates:
        return []
    dynamic = bool(getattr(plugin_config, "personification_provider_dynamic_priority_enabled", False))
    if dynamic:
        try:
            from plugin.personification.core import provider_health

            stats = provider_health.get_all_stats()
            min_samples = int(getattr(plugin_config, "personification_provider_health_min_samples", 3) or 3)
            candidates.sort(key=lambda provider: (
                provider_health.compute_effective_priority(
                    provider,
                    stats.get(provider["name"]),
                    min_samples=min_samples,
                    enabled=True,
                ),
                provider["name"],
            ))
        except Exception:
            candidates.sort(key=lambda provider: (provider["priority"], provider["name"]))
    else:
        candidates.sort(key=lambda provider: (provider["priority"], provider["name"]))
    return candidates


def _pool_image_route(plugin_config: Any) -> dict[str, Any] | None:
    routes = _pool_image_routes(plugin_config)
    return routes[0] if routes else None


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
    return endpoint, {"Content-Type": "application/json"}, {}


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


def _validated_b64(value: Any, mime_type: str = "") -> tuple[str, str] | None:
    text = str(value or "").strip()
    try:
        payload = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not payload or len(payload) > _MAX_IMAGE_BYTES:
        return None
    claimed = str(mime_type or "").split(";", 1)[0].strip().lower()
    if claimed and claimed not in _ALLOWED_IMAGE_MIMES:
        return None
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        detected = "image/png" if _validate_png_without_pillow(payload) else ""
    else:
        try:
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                detected = str(Image.MIME.get(image.format, "") or "").lower()
        except (UnidentifiedImageError, OSError, ValueError):
            return None
    if detected not in _ALLOWED_IMAGE_MIMES or (claimed and claimed != detected):
        return None
    return text, detected


def _validate_png_without_pillow(payload: bytes) -> bool:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    saw_iend = False
    try:
        while offset + 12 <= len(payload):
            length = struct.unpack(">I", payload[offset:offset + 4])[0]
            chunk_type = payload[offset + 4:offset + 8]
            end = offset + 12 + length
            if length > _MAX_IMAGE_BYTES or end > len(payload):
                return False
            data = payload[offset + 8:offset + 8 + length]
            crc = struct.unpack(">I", payload[offset + 8 + length:end])[0]
            if zlib.crc32(chunk_type + data) & 0xFFFFFFFF != crc:
                return False
            if chunk_type == b"IHDR":
                if length != 13 or width or height:
                    return False
                width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
                if not width or not height or compression or filtering or interlace:
                    return False
            elif chunk_type == b"IDAT":
                idat.extend(data)
            elif chunk_type == b"IEND":
                saw_iend = length == 0 and end == len(payload)
                break
            offset = end
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 0)
        if not saw_iend or not idat or bit_depth not in {1, 2, 4, 8, 16} or not channels:
            return False
        row_bytes = (width * channels * bit_depth + 7) // 8
        expected = height * (row_bytes + 1)
        if expected > _MAX_IMAGE_BYTES * 8:
            return False
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(idat), expected + 1)
        decoded += decompressor.flush()
        return decompressor.eof and not decompressor.unused_data and len(decoded) == expected
    except (ValueError, struct.error, zlib.error):
        return False


def _ok_image(value: Any, mime_type: str = "") -> dict[str, str]:
    validated = _validated_b64(value, mime_type)
    if validated is None:
        return {"error": "provider returned invalid image data"}
    b64, detected = validated
    return {"b64_json": b64, "mime_type": detected}


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


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), parsed.port or (443 if parsed.scheme == "https" else 80)


async def _download_image_as_b64(
    url: str,
    *,
    api_key: str = "",
    auth_origin: str = "",
    timeout: float = 180.0,
    proxy: str = "",
) -> str:
    headers = {"Accept": "image/*,*/*"}
    if api_key and auth_origin and _origin(url) == _origin(auth_origin):
        headers["Authorization"] = f"Bearer {api_key}"
    result = await download_public_image(
        url,
        headers=headers,
        timeout=float(timeout or 180.0),
        connect_timeout=15.0,
        max_bytes=_MAX_IMAGE_BYTES,
        allowed_mimes=_ALLOWED_IMAGE_MIMES,
        max_redirects=5,
        proxy=proxy,
        sensitive_headers_origin=auth_origin,
    )
    encoded = base64.b64encode(result.content).decode("ascii")
    return encoded if _validated_b64(encoded, result.content_type) else ""


def _error_from_http(exc: httpx.HTTPStatusError) -> str:
    return f"image provider HTTP {exc.response.status_code}"


async def _generate_image_openai_http(
    prompt_text: str,
    *,
    api_url: str,
    api_key: str,
    image_model: str,
    size: str,
    timeout: float,
    proxy: str = "",
    reference_mode: str = "auto",
    has_references: bool = False,
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
        client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(float(timeout or 180.0), connect=15.0)}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": _error_from_http(exc)}
    except Exception:
        return {"error": "image provider request failed"}
    b64, url = _extract_openai_image(data)
    if not b64 and url:
        try:
            b64 = await _download_image_as_b64(
                url, api_key=api_key, auth_origin=endpoint, timeout=timeout, proxy=proxy
            )
        except Exception:
            return {"error": "image download failed"}
    result = _ok_image(b64)
    if "error" not in result and has_references and reference_mode not in {"off", "vision_prompt"}:
        result["warning"] = "reference images are not supported by this provider"
    return result


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
    proxy: str = "",
    reference_mode: str = "auto",
    auth_mode: str = "auto",
) -> dict[str, str]:
    model = str(image_model or "gemini-3-pro-image-preview").strip() or "gemini-3-pro-image-preview"
    endpoint, headers, params = _gemini_generate_endpoint(api_url, model, api_key)
    raw_refs = [] if reference_mode in {"off", "vision_prompt"} else list(images or []) + list(image_urls or [])
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
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(float(timeout or 180.0), connect=15.0),
            "follow_redirects": False,
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            async def _send(auth):  # noqa: ANN001, ANN202
                return await client.post(
                    endpoint,
                    json=payload,
                    headers={**headers, **auth.headers},
                    params={**params, **auth.params},
                )

            auth_result = await request_with_gemini_auth(
                endpoint=endpoint.rsplit("/models/", 1)[0],
                api_key=api_key,
                auth_mode=auth_mode,
                send=_send,
                allow_negotiation=not use_single_attempt_retry_policy(),
            )
            response = auth_result.response
            raise_for_gemini_status(
                response,
                auth_mode=auth_result.mode,
                request_count=auth_result.request_count,
            )
            data = response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": _error_from_http(exc)}
    except Exception:
        return {"error": "image provider request failed"}
    b64, _mime = _extract_gemini_image(data)
    result = _ok_image(b64, _mime)
    if "error" not in result and reference_problems:
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
    reference_mode = str(reference_mode or "auto").strip().lower() or "auto"
    has_references = bool(list(images or []) + list(image_urls or []))

    if route.get("dedicated") and (
        route_type not in {"openai", "gemini"} or not route.get("api_url") or not route.get("api_key")
    ):
        return {"error": "dedicated image route requires api_type, api_url and api_key"}

    if route_type == "openai":
        return await _generate_image_openai_http(
            prompt_text,
            api_url=route.get("api_url", ""),
            api_key=route.get("api_key", ""),
            image_model=image_model,
            size=size,
            timeout=float(timeout or 180.0),
            reference_mode=reference_mode,
            has_references=has_references,
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
            reference_mode=reference_mode,
            auth_mode=str(route.get("gemini_auth_mode", "auto") or "auto"),
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
                reference_mode=reference_mode,
            )
        pool_routes = _pool_image_routes(plugin_config) if not route.get("dedicated") else []
        routes = pool_routes or ([route] if route.get("api_key") else [])
        last_result: dict[str, str] = {"error": "image generation route is not configured"}
        for route in routes:
            guessed_type = route.get("api_type") if route.get("api_type") in {"openai", "gemini"} else route.get("main_api_type") or "openai"
            candidate_model = str(route.get("model") or image_model).strip() or image_model
            if guessed_type == "gemini" or is_google_gemini_endpoint(str(route.get("api_url", "") or "")):
                last_result = await _generate_image_gemini_http(
                    prompt_text,
                    api_url=route.get("api_url", ""),
                    api_key=route.get("api_key", ""),
                    image_model=_gemini_image_model(plugin_config, candidate_model),
                    size=size,
                    timeout=float(timeout or 180.0),
                    images=images,
                    image_urls=image_urls,
                    proxy=str(route.get("proxy", "") or ""),
                    reference_mode=reference_mode,
                    auth_mode=str(route.get("gemini_auth_mode", "auto") or "auto"),
                )
            else:
                last_result = await _generate_image_openai_http(
                    prompt_text,
                    api_url=route.get("api_url", ""),
                    api_key=route.get("api_key", ""),
                    image_model=candidate_model,
                    size=size,
                    timeout=float(timeout or 180.0),
                    proxy=str(route.get("proxy", "") or ""),
                    reference_mode=reference_mode,
                    has_references=has_references,
                )
            if "error" not in last_result:
                return last_result
        return last_result
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
                str(getattr(caller, "proxy", "") or ""),
            )
            dedicated = _DEDICATED_CODEX_CALLERS.get(key)
            if dedicated is None:
                dedicated = caller.__class__(
                    model=key[1],
                    auth_path=key[2],
                    timeout=timeout_value,
                    proxy=key[4],
                )
                _DEDICATED_CODEX_CALLERS[key] = dedicated
            caller = dedicated
    raw_refs = [] if reference_mode in {"off", "vision_prompt"} else list(images or []) + list(image_urls or [])
    reference_images, reference_problems = normalize_image_refs(raw_refs, limit=3)
    result = await caller.generate_image(
        prompt_text,
        size=normalize_image_generation_size(str(size or "1024x1024")),
        image_model=str(image_model or "gpt-image-2").strip() or "gpt-image-2",
        images=reference_images,
        reference_mode=reference_mode,
    )
    if not isinstance(result, dict):
        return {"error": "invalid image response"}
    normalized = _ok_image(result.get("b64_json"), result.get("mime_type", ""))
    if "error" in normalized:
        return normalized
    if reference_problems and "warning" not in result:
        result = dict(result)
        result["warning"] = "some reference images were ignored: " + ", ".join(reference_problems[:3])
    if result.get("warning"):
        normalized["warning"] = str(result["warning"])
    return normalized


__all__ = [
    "_first_gemini_cli_caller",
    "_first_codex_caller",
    "_pool_image_route",
    "generate_image",
    "normalize_image_generation_size",
]
