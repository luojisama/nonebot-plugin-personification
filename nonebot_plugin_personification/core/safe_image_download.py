from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


Resolver = Callable[..., Awaitable[Any]]
ClientFactory = Callable[..., Any]
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class SafeImageDownloadError(ValueError):
    pass


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes
    content_type: str
    final_url: str


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


async def resolve_public_url(url: str, *, resolver: Resolver | None = None) -> tuple[str, str]:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SafeImageDownloadError("image URL must be an unauthenticated HTTP(S) URL")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SafeImageDownloadError("image URL has an invalid port") from exc
    if port not in {80, 443}:
        raise SafeImageDownloadError("image URL uses a disallowed port")
    host = parsed.hostname.rstrip(".")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        lookup = resolver or asyncio.get_running_loop().getaddrinfo
        try:
            records = await lookup(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            raise SafeImageDownloadError("image host could not be resolved") from exc
        addresses = list(dict.fromkeys(str(item[4][0]) for item in records if item and len(item) > 4 and item[4]))
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise SafeImageDownloadError("image URL resolves to a non-public address")
    return parsed.geturl(), addresses[0].split("%", 1)[0]


def _pinned_request_url(original_url: str, approved_ip: str) -> tuple[str, str, str]:
    parsed = urlsplit(original_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ip = ipaddress.ip_address(approved_ip)
    ip_host = f"[{ip}]" if ip.version == 6 else str(ip)
    default_port = 443 if parsed.scheme == "https" else 80
    connection_netloc = ip_host if port == default_port else f"{ip_host}:{port}"
    try:
        original_ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        original_host = host
    else:
        original_host = f"[{original_ip}]" if original_ip.version == 6 else str(original_ip)
    host_header = original_host if port == default_port else f"{original_host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme, connection_netloc, path, parsed.query, "")), host_header, host


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), parsed.port or (443 if parsed.scheme == "https" else 80)


async def download_public_image(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
    connect_timeout: float = 4.0,
    max_bytes: int,
    allowed_mimes: set[str],
    max_redirects: int = 4,
    resolver: Resolver | None = None,
    proxy: str = "",
    sensitive_headers_origin: str = "",
    client_factory: ClientFactory = httpx.AsyncClient,
) -> DownloadedImage:
    if proxy:
        raise SafeImageDownloadError("pinned image downloads do not support explicit proxies")
    current = str(url or "").strip()
    for redirect_count in range(max_redirects + 1):
        original_url, approved_ip = await resolve_public_url(current, resolver=resolver)
        connection_url, host_header, sni_hostname = _pinned_request_url(original_url, approved_ip)
        request_headers = dict(headers or {})
        if sensitive_headers_origin and _origin(original_url) != _origin(sensitive_headers_origin):
            request_headers = {key: value for key, value in request_headers.items() if key.lower() != "authorization"}
        request_headers["Host"] = host_header
        client_kwargs = {
            "follow_redirects": False,
            "timeout": httpx.Timeout(float(timeout), connect=float(connect_timeout)),
            "trust_env": False,
        }
        async with client_factory(**client_kwargs) as client:
            request = client.build_request("GET", connection_url, headers=request_headers)
            request.extensions["sni_hostname"] = sni_hostname
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location", "")
                    if not location:
                        raise SafeImageDownloadError("redirect is missing Location")
                    if redirect_count >= max_redirects:
                        raise SafeImageDownloadError("too many image redirects")
                    current = urljoin(original_url, location)
                    continue
                if response.status_code != 200:
                    raise SafeImageDownloadError(f"image server returned HTTP {response.status_code}")
                mime = str(response.headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()
                if mime not in allowed_mimes:
                    raise SafeImageDownloadError("response MIME is not an allowed image type")
                try:
                    length = int(response.headers.get("content-length", "0") or 0)
                except (TypeError, ValueError) as exc:
                    raise SafeImageDownloadError("invalid image Content-Length") from exc
                if length > max_bytes:
                    raise SafeImageDownloadError("image Content-Length is too large")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SafeImageDownloadError("image response exceeded size limit")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise SafeImageDownloadError("image body is empty")
                return DownloadedImage(content, mime, original_url)
            finally:
                await response.aclose()
    raise SafeImageDownloadError("too many image redirects")


__all__ = [
    "DownloadedImage",
    "SafeImageDownloadError",
    "download_public_image",
    "resolve_public_url",
]
