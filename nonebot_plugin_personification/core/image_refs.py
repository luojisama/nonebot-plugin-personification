from __future__ import annotations

from typing import Iterable


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
    return None, "local_path_not_allowed"


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
