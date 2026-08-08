from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .image_refs import normalize_image_refs


_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".mkv",
    ".avi",
}
_AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
    ".amr",
}


def is_supported_video_filename(value: str) -> bool:
    """Return whether a path/URL/name has a supported video suffix."""

    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        path = unquote(urlsplit(raw).path) if "://" in raw else raw
    except Exception:
        path = raw
    return Path(path).suffix.lower() in _VIDEO_EXTENSIONS


def normalize_video_ref(value: str) -> tuple[str | None, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, "empty"
    if raw.startswith(("http://", "https://")):
        return raw, None
    if raw.startswith("file://"):
        raw = raw[7:]
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None, "non_absolute_local_path"
    try:
        resolved = candidate.resolve(strict=True)
    except Exception:
        return None, "missing_local_file"
    if not resolved.is_file():
        return None, "not_a_file"
    if resolved.suffix.lower() not in _VIDEO_EXTENSIONS:
        return None, "unsupported_video_extension"
    return str(resolved), None


def normalize_video_refs(values: Iterable[str], *, limit: int = 1) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    problems: list[str] = []
    for item in values:
        value, problem = normalize_video_ref(str(item or "").strip())
        if value:
            if value not in normalized:
                normalized.append(value)
            if len(normalized) >= max(1, int(limit)):
                break
            continue
        if problem:
            problems.append(problem)
    return normalized, problems


def normalize_audio_ref(value: str) -> tuple[str | None, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, "empty"
    if raw.startswith(("http://", "https://")):
        return raw, None
    if raw.startswith("file://"):
        raw = raw[7:]
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None, "non_absolute_local_path"
    try:
        resolved = candidate.resolve(strict=True)
    except Exception:
        return None, "missing_local_file"
    if not resolved.is_file():
        return None, "not_a_file"
    if resolved.suffix.lower() not in _AUDIO_EXTENSIONS:
        return None, "unsupported_audio_extension"
    return str(resolved), None


def normalize_audio_refs(values: Iterable[str], *, limit: int = 1) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    problems: list[str] = []
    for item in values:
        value, problem = normalize_audio_ref(str(item or "").strip())
        if value:
            if value not in normalized:
                normalized.append(value)
            if len(normalized) >= max(1, int(limit)):
                break
            continue
        if problem:
            problems.append(problem)
    return normalized, problems


def normalize_media_refs(
    *,
    images: Iterable[str] | None = None,
    videos: Iterable[str] | None = None,
    audios: Iterable[str] | None = None,
    image_limit: int = 3,
    video_limit: int = 1,
    audio_limit: int = 1,
) -> dict[str, Any]:
    normalized_images, image_problems = normalize_image_refs(images or [], limit=image_limit)
    normalized_videos, video_problems = normalize_video_refs(videos or [], limit=video_limit)
    normalized_audios, audio_problems = normalize_audio_refs(audios or [], limit=audio_limit)
    return {
        "images": normalized_images,
        "videos": normalized_videos,
        "audios": normalized_audios,
        "image_problems": image_problems,
        "video_problems": video_problems,
        "audio_problems": audio_problems,
    }


__all__ = [
    "is_supported_video_filename",
    "normalize_media_refs",
    "normalize_audio_ref",
    "normalize_audio_refs",
    "normalize_video_ref",
    "normalize_video_refs",
]
