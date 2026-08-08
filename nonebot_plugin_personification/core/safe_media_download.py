from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

import httpx

from .safe_image_download import (
    REDIRECT_STATUSES,
    SafeImageDownloadError,
    _pinned_request_url,
    resolve_public_url,
)


Resolver = Callable[..., Awaitable[Any]]
ClientFactory = Callable[..., Any]


class SafeMediaDownloadError(ValueError):
    pass


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    content_type: str
    final_url: str
    size: int


async def download_public_media_to_path(
    url: str,
    destination: str | Path,
    *,
    timeout: float = 30.0,
    connect_timeout: float = 6.0,
    max_bytes: int,
    allowed_mimes: set[str],
    max_redirects: int = 4,
    resolver: Resolver | None = None,
    client_factory: ClientFactory = httpx.AsyncClient,
) -> DownloadedMedia:
    """Download untrusted public media to disk without buffering the body.

    DNS is resolved and pinned for every redirect just like image downloads.
    The destination is removed on every incomplete or rejected transfer.
    """

    destination_path = Path(destination)
    current = str(url or "").strip()
    for redirect_count in range(max_redirects + 1):
        try:
            original_url, approved_ip = await resolve_public_url(current, resolver=resolver)
        except SafeImageDownloadError as exc:
            raise SafeMediaDownloadError(str(exc)) from exc
        connection_url, host_header, sni_hostname = _pinned_request_url(original_url, approved_ip)
        client_kwargs = {
            "follow_redirects": False,
            "timeout": httpx.Timeout(float(timeout), connect=float(connect_timeout)),
            "trust_env": False,
        }
        async with client_factory(**client_kwargs) as client:
            request = client.build_request("GET", connection_url, headers={"Host": host_header})
            request.extensions["sni_hostname"] = sni_hostname
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location", "")
                    if not location:
                        raise SafeMediaDownloadError("redirect is missing Location")
                    if redirect_count >= max_redirects:
                        raise SafeMediaDownloadError("too many media redirects")
                    current = urljoin(original_url, location)
                    continue
                if response.status_code != 200:
                    raise SafeMediaDownloadError(f"media server returned HTTP {response.status_code}")
                mime = str(response.headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()
                if mime not in allowed_mimes:
                    raise SafeMediaDownloadError("response MIME is not an allowed media type")
                try:
                    length = int(response.headers.get("content-length", "0") or 0)
                except (TypeError, ValueError) as exc:
                    raise SafeMediaDownloadError("invalid media Content-Length") from exc
                if length > max_bytes:
                    raise SafeMediaDownloadError("media Content-Length is too large")
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                try:
                    with destination_path.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise SafeMediaDownloadError("media response exceeded size limit")
                            handle.write(chunk)
                except Exception:
                    destination_path.unlink(missing_ok=True)
                    raise
                if size <= 0:
                    destination_path.unlink(missing_ok=True)
                    raise SafeMediaDownloadError("media body is empty")
                return DownloadedMedia(destination_path, mime, original_url, size)
            finally:
                await response.aclose()
    raise SafeMediaDownloadError("too many media redirects")


__all__ = ["DownloadedMedia", "SafeMediaDownloadError", "download_public_media_to_path"]
