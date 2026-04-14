from __future__ import annotations

from pathlib import Path
from typing import Any


def normalize_message_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            normalized = normalize_message_part(item)
            if normalized is not None:
                parts.append(normalized)
        return parts
    normalized = normalize_message_part(content)
    return [normalized] if normalized is not None else []


def normalize_message_part(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    if isinstance(item, str):
        text = item.strip()
        return {"type": "text", "text": text} if text else None
    if not isinstance(item, dict):
        text = str(item).strip()
        return {"type": "text", "text": text} if text else None

    part_type = str(item.get("type", "") or "").strip().lower()
    if part_type == "text":
        text = str(item.get("text", "") or "").strip()
        return {"type": "text", "text": text} if text else None

    if part_type == "image_url":
        image_value = item.get("image_url", {})
        if isinstance(image_value, str):
            url = image_value.strip()
            image_value = {"url": url}
        elif not isinstance(image_value, dict):
            image_value = {}
        url = str(image_value.get("url", "") or "").strip()
        if not url:
            return None
        normalized: dict[str, Any] = {
            "type": "image_url",
            "image_url": {"url": url},
        }
        mime_type = str(item.get("mime_type", "") or image_value.get("mime_type", "") or "").strip()
        alt_text = str(item.get("alt_text", "") or image_value.get("alt_text", "") or "").strip()
        if mime_type:
            normalized["mime_type"] = mime_type
        if alt_text:
            normalized["alt_text"] = alt_text
        return normalized

    if part_type == "image_file":
        file_value = item.get("image_file", {})
        if isinstance(file_value, str):
            file_value = {"path": file_value}
        elif not isinstance(file_value, dict):
            file_value = {}
        path = str(file_value.get("path", "") or item.get("path", "") or "").strip()
        if not path:
            return None
        normalized = {
            "type": "image_file",
            "image_file": {"path": path},
        }
        mime_type = str(item.get("mime_type", "") or file_value.get("mime_type", "") or "").strip()
        alt_text = str(item.get("alt_text", "") or file_value.get("alt_text", "") or "").strip()
        if mime_type:
            normalized["mime_type"] = mime_type
        if alt_text:
            normalized["alt_text"] = alt_text
        return normalized

    if "text" in item:
        text = str(item.get("text", "") or "").strip()
        return {"type": "text", "text": text} if text else None
    return None


def extract_text_from_parts(content: Any) -> str:
    texts: list[str] = []
    for part in normalize_message_parts(content):
        if part.get("type") == "text" and part.get("text"):
            texts.append(str(part["text"]))
    return " ".join(texts).strip()


def extract_image_refs_from_parts(content: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for part in normalize_message_parts(content):
        if part.get("type") == "image_url":
            image_obj = part.get("image_url", {})
            if isinstance(image_obj, dict) and image_obj.get("url"):
                refs.append(
                    {
                        "kind": "url",
                        "url": str(image_obj["url"]),
                        "mime_type": str(part.get("mime_type", "") or "").strip(),
                        "alt_text": str(part.get("alt_text", "") or "").strip(),
                    }
                )
        elif part.get("type") == "image_file":
            image_obj = part.get("image_file", {})
            if isinstance(image_obj, dict) and image_obj.get("path"):
                refs.append(
                    {
                        "kind": "file",
                        "path": str(image_obj["path"]),
                        "mime_type": str(part.get("mime_type", "") or "").strip(),
                        "alt_text": str(part.get("alt_text", "") or "").strip(),
                    }
                )
    return refs


def build_user_message_content(
    *,
    text: str,
    image_urls: list[str] | None = None,
    image_files: list[str | Path] | None = None,
    mime_type: str = "",
    alt_text: str = "",
) -> str | list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if str(text or "").strip():
        parts.append({"type": "text", "text": str(text).strip()})
    for image_url in image_urls or []:
        url = str(image_url or "").strip()
        if not url:
            continue
        part: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
        if mime_type:
            part["mime_type"] = mime_type
        if alt_text:
            part["alt_text"] = alt_text
        parts.append(part)
    for image_file in image_files or []:
        path = str(image_file or "").strip()
        if not path:
            continue
        part = {"type": "image_file", "image_file": {"path": path}}
        if mime_type:
            part["mime_type"] = mime_type
        if alt_text:
            part["alt_text"] = alt_text
        parts.append(part)
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0]["text"])
    return parts
