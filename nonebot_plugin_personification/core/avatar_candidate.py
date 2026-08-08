from __future__ import annotations

import asyncio
import hashlib
import io
import re
import uuid
import warnings
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .paths import get_data_dir
from .safe_image_download import SafeImageDownloadError, download_public_image, resolve_public_url


MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MIN_EDGE = 96
MAX_REDIRECTS = 4
ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_PHASH_DUPLICATE_DISTANCE = 4
Fetcher = Callable[[str], Awaitable[Any]]


class AvatarCandidateError(ValueError):
    pass


async def validate_remote_image_url(
    url: str,
    *,
    resolver: Callable[..., Awaitable[Any]] | None = None,
) -> str:
    try:
        normalized, _approved_ip = await resolve_public_url(url, resolver=resolver)
        return normalized
    except SafeImageDownloadError as exc:
        raise AvatarCandidateError(str(exc)) from exc


def _magic_mime(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    raise AvatarCandidateError("unsupported or invalid image magic")


def sanitize_image(payload: bytes, declared_mime: str = "") -> tuple[bytes, str, int, int, str]:
    if not payload or len(payload) > MAX_DOWNLOAD_BYTES:
        raise AvatarCandidateError("image body is empty or too large")
    magic_mime = _magic_mime(payload)
    declared = str(declared_mime or "").split(";", 1)[0].strip().lower()
    if declared and declared not in ALLOWED_MIMES:
        raise AvatarCandidateError("response MIME is not an allowed image type")
    if declared and declared != magic_mime:
        raise AvatarCandidateError("response MIME does not match image magic")
    try:
        from PIL import Image, UnidentifiedImageError

        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                width, height = image.size
                if min(width, height) < MIN_EDGE or width * height > MAX_PIXELS:
                    raise AvatarCandidateError("image dimensions are outside safety limits")
                output = io.BytesIO()
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    image.convert("RGBA").save(output, format="PNG", optimize=True)
                    mime, suffix = "image/png", ".png"
                else:
                    image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
                    mime, suffix = "image/jpeg", ".jpg"
    except ImportError as exc:
        raise AvatarCandidateError("Pillow is required to sanitize avatar images") from exc
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise AvatarCandidateError("image decoder rejected payload") from exc
    return output.getvalue(), mime, width, height, suffix


def perceptual_hash(payload: bytes) -> str:
    try:
        from PIL import Image
    except ImportError as exc:
        raise AvatarCandidateError("Pillow is required to hash avatar images") from exc
    with Image.open(io.BytesIO(payload)) as image:
        gray = image.convert("L").resize((8, 8))
        flattened = getattr(gray, "get_flattened_data", None)
        values = list(flattened() if callable(flattened) else gray.getdata())
    average = sum(values) / len(values)
    bits = "".join("1" if value >= average else "0" for value in values)
    return f"{int(bits, 2):016x}"


def phash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


async def _default_fetch(
    url: str,
    *,
    resolver: Callable[..., Awaitable[Any]] | None = None,
) -> tuple[bytes, str, str]:
    try:
        result = await download_public_image(
            url,
            headers={"Accept": "image/*"},
            max_bytes=MAX_DOWNLOAD_BYTES,
            allowed_mimes=ALLOWED_MIMES,
            max_redirects=MAX_REDIRECTS,
            resolver=resolver,
        )
    except SafeImageDownloadError as exc:
        raise AvatarCandidateError(str(exc)) from exc
    return result.content, result.content_type, result.final_url


def extract_image_urls(sources: list[dict[str, Any]], *, limit: int = 60) -> list[dict[str, str]]:
    fields = ("image_url", "thumbnail_url", "original_url", "avatar_url", "image", "thumbnail")
    url_re = re.compile(r"https?://[^\s<>\"']+", flags=re.I)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for source in sources:
        page_url = str(source.get("url") or "").strip()
        values = [str(source.get(field) or "").strip() for field in fields]
        for key in ("summary", "content", "raw"):
            values.extend(url_re.findall(str(source.get(key) or "")))
        for value in values:
            url = value.rstrip(".,);]}")
            if not url or url in seen:
                continue
            try:
                path = urlsplit(url).path.lower()
            except ValueError:
                continue
            if not (path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) or value in values[:len(fields)]):
                continue
            seen.add(url)
            out.append({
                "source": str(source.get("source") or source.get("kind") or "source"),
                "title": str(source.get("title") or ""),
                "query": str(source.get("query") or ""),
                "page_url": str(source.get("page_url") or page_url),
                "url": url,
            })
            if len(out) >= limit:
                return out
    return out


async def build_avatar_candidates(
    sources: list[dict[str, Any]],
    *,
    revision: str,
    plugin_config: Any = None,
    fetcher: Fetcher | None = None,
    resolver: Callable[..., Awaitable[Any]] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    extracted = extract_image_urls(sources)
    failure_counts: Counter[str] = Counter()
    output_dir = Path(get_data_dir(plugin_config)) / "persona_avatar_candidates" / revision
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[dict[str, Any]] = []
    sha_seen: set[str] = set()
    phashes: list[str] = []
    semaphore = asyncio.Semaphore(8)

    async def process(item: dict[str, str]) -> tuple[dict[str, Any] | None, bytes | None, str]:
        try:
            async with semaphore:
                if fetcher is None:
                    raw = await _default_fetch(item["url"], resolver=resolver)
                else:
                    await validate_remote_image_url(item["url"], resolver=resolver)
                    raw = await fetcher(item["url"])
            if isinstance(raw, tuple):
                payload = bytes(raw[0])
                declared = str(raw[1] if len(raw) > 1 else "")
                final_url = str(raw[2] if len(raw) > 2 else item["url"])
            else:
                payload, declared, final_url = bytes(raw), "", item["url"]
            if fetcher is not None:
                await validate_remote_image_url(final_url, resolver=resolver)
            encoded, mime, width, height, suffix = sanitize_image(payload, declared)
            sha256 = hashlib.sha256(encoded).hexdigest()
            phash = perceptual_hash(encoded)
            candidate_id = uuid.uuid4().hex
            candidate = {
                "candidate_id": candidate_id,
                "source": item["source"],
                "title": item.get("title", ""),
                "query": item.get("query", ""),
                "page_url": item["page_url"],
                "width": width,
                "height": height,
                "mime": mime,
                "sha256": sha256,
                "phash": phash,
                "phash_cluster": phash,
                "safety_status": "pass",
                "aspect_score": round(min(width, height) / max(width, height), 4),
                "fit_score": 0.0,
                "revision": revision,
                "suffix": suffix,
            }
            return candidate, encoded, suffix
        except Exception as exc:
            message = str(exc).lower()
            if "pillow" in message:
                failure_counts["dependency_missing"] += 1
            elif "resolve" in message or "address" in message:
                failure_counts["dns_or_address"] += 1
            elif "mime" in message or "magic" in message:
                failure_counts["not_an_image"] += 1
            elif "http " in message or "redirect" in message:
                failure_counts["http_error"] += 1
            elif "too large" in message or "size limit" in message:
                failure_counts["too_large"] += 1
            elif "decoder" in message or "dimensions" in message:
                failure_counts["decode_rejected"] += 1
            else:
                failure_counts["download_error"] += 1
            return None, None, ""

    results = await asyncio.gather(*(process(item) for item in extracted))
    for candidate, encoded, suffix in results:
        if candidate is None or encoded is None:
            continue
        sha256 = candidate["sha256"]
        phash = candidate["phash"]
        if sha256 in sha_seen or any(
            phash_distance(phash, old) <= MAX_PHASH_DUPLICATE_DISTANCE for old in phashes
        ):
            failure_counts["duplicate"] += 1
            continue
        sha_seen.add(sha256)
        phashes.append(phash)
        cluster = min(phashes, key=lambda old: phash_distance(phash, old))
        candidate["phash_cluster"] = cluster
        (output_dir / f"{candidate['candidate_id']}{suffix}").write_bytes(encoded)
        candidates.append(candidate)
    candidates.sort(key=lambda item: float(item["fit_score"]), reverse=True)
    if diagnostics is not None:
        diagnostics.update({
            "extracted_url_count": len(extracted),
            "safe_count": len(candidates),
            "failure_counts": dict(failure_counts),
        })
    return candidates


def candidate_file(candidate: dict[str, Any], *, plugin_config: Any = None) -> Path:
    revision = str(candidate.get("revision") or "")
    candidate_id = str(candidate.get("candidate_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", candidate_id) or not re.fullmatch(r"[0-9a-f]{32}", revision):
        raise AvatarCandidateError("invalid candidate identity")
    root = (Path(get_data_dir(plugin_config)) / "persona_avatar_candidates" / revision).resolve()
    path = root / f"{candidate_id}{candidate.get('suffix') or ''}"
    if not path.is_file():
        raise AvatarCandidateError("candidate image is unavailable")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise AvatarCandidateError("candidate image escapes storage root")
    return resolved


__all__ = [
    "AvatarCandidateError",
    "build_avatar_candidates",
    "candidate_file",
    "extract_image_urls",
    "perceptual_hash",
    "phash_distance",
    "sanitize_image",
    "validate_remote_image_url",
]
