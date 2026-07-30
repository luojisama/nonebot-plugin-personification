from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse


def _file_uri_to_path(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return value
    path = unquote(parsed.path or "")
    if path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    return path


def is_supported_image_ref(value: str) -> bool:
    normalized, _ = normalize_image_ref(value)
    return bool(normalized)


def normalize_image_ref(value: str) -> tuple[str | None, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, "empty"
    if raw.startswith("data:"):
        return raw, None
    if raw.startswith(("http://", "https://")):
        return raw, None
    if raw.startswith("file://"):
        raw = _file_uri_to_path(raw)

    candidate = Path(raw)
    if not candidate.is_absolute():
        return None, "non_absolute_local_path"
    try:
        resolved = candidate.resolve(strict=True)
    except Exception:
        return None, "missing_local_file"
    if not resolved.is_file():
        return None, "not_a_file"
    return _path_to_data_url(resolved), None


def normalize_image_refs(values: Iterable[str], *, limit: int = 3) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    problems: list[str] = []
    for item in values:
        value, problem = normalize_image_ref(str(item or "").strip())
        if value:
            if value not in normalized:
                normalized.append(value)
            if len(normalized) >= max(1, int(limit)):
                break
            continue
        if problem:
            problems.append(problem)
    return normalized, problems


def _path_to_data_url(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    resolved_mime = mime_type or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{resolved_mime};base64,{payload}"
