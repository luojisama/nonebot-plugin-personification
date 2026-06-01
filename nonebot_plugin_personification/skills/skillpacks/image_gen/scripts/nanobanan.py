from __future__ import annotations

from typing import Any

import httpx

from plugin.personification.core.image_refs import normalize_image_refs

from .impl import normalize_image_generation_size


def _first_gemini_cli_caller(tool_caller: Any) -> Any:
    candidates = [tool_caller]
    candidates.extend(list(getattr(tool_caller, "_primary_callers", []) or []))
    fallback = getattr(tool_caller, "_fallback_caller", None)
    if fallback is not None:
        candidates.append(fallback)
    for caller in candidates:
        if caller is None:
            continue
        if caller.__class__.__name__ not in {"GeminiCliToolCaller", "AntigravityCliToolCaller"}:
            continue
        return caller
    return None


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


def _extract_inline_image(response_payload: dict) -> tuple[str, str]:
    """Return (b64, mime_type) from the first inline image part in a Gemini response."""
    if not isinstance(response_payload, dict):
        return "", ""
    inner = response_payload.get("response") if isinstance(response_payload, dict) else None
    payload = inner if isinstance(inner, dict) else response_payload
    candidates = list(payload.get("candidates", []) or [])
    for candidate in candidates:
        content = candidate.get("content") or {}
        parts = list(content.get("parts", []) or [])
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict):
                data = str(inline.get("data", "") or "").strip()
                mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
                if data:
                    return data, mime
    return "", ""


async def generate_image_nanobanan(
    prompt: str,
    *,
    tool_caller: Any,
    size: str = "1024x1024",
    image_model: str = "gemini-3-pro-image-preview",
    timeout: float | None = None,
    images: list[str] | None = None,
    image_urls: list[str] | None = None,
) -> dict[str, str]:
    """Call Nano Banana (Gemini 3 image) via Gemini/Antigravity CLI OAuth credentials."""
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {"error": "empty prompt"}
    caller = _first_gemini_cli_caller(tool_caller)
    if caller is None:
        return {"error": "nanobanan only available on gemini_cli or antigravity_cli model route"}

    access_token, _auth_file = await caller._get_access_token()
    project = await caller._resolve_project(access_token)

    raw_refs = list(images or []) + list(image_urls or [])
    reference_images, reference_problems = normalize_image_refs(raw_refs, limit=3)

    parts: list[dict[str, Any]] = [{"text": prompt_text}]
    for ref in reference_images:
        parsed = _split_data_url(ref)
        if parsed:
            mime_type, b64 = parsed
            parts.append({"inline_data": {"mime_type": mime_type, "data": b64}})
        else:
            # http(s) url 留给 Gemini 自己拉取（部分租户支持 file_data；这里走 inline 优先）
            parts.append({"text": f"参考图片 URL: {ref}"})

    request_obj = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    envelope = {
        "model": str(image_model or "gemini-3-pro-image-preview").strip() or "gemini-3-pro-image-preview",
        "project": project,
        "request": request_obj,
    }
    timeout_value = float(timeout if timeout is not None else getattr(caller, "timeout", 180.0) or 180.0)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_value, connect=15.0)) as client:
            response = await client.post(
                "https://cloudcode-pa.googleapis.com/v1internal:generateContent",
                json=envelope,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text[:240]
        except Exception:
            pass
        return {"error": f"nanobanan HTTP {exc.response.status_code}: {body}"}
    except Exception as exc:
        return {"error": f"nanobanan request failed: {exc}"}

    b64, _mime = _extract_inline_image(data)
    if not b64:
        return {"error": "empty image response"}
    result: dict[str, str] = {"b64_json": b64}
    if reference_problems:
        result["warning"] = "some reference images were ignored: " + ", ".join(reference_problems[:3])
    return result


__all__ = [
    "_first_gemini_cli_caller",
    "generate_image_nanobanan",
    "normalize_image_generation_size",
]
