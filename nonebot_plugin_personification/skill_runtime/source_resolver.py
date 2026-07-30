from __future__ import annotations

import asyncio
import hashlib
import html
import json
import ipaddress
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import yaml
import socket


_SKILL_PAGE_HOSTS = {"clawhub.ai", "skillhub.tencent.com", "skillhub.cn"}
_MAX_REMOTE_ZIP_BYTES = 50 * 1024 * 1024
_MAX_REMOTE_REDIRECTS = 5
_BLOCKED_REMOTE_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".corp")
_BLOCKED_REMOTE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
}


def _is_disallowed_ip_address(ip: ipaddress._BaseAddress) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


async def _is_safe_remote_url(url: str, logger: Any) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in _BLOCKED_REMOTE_HOSTS or host.endswith(_BLOCKED_REMOTE_HOST_SUFFIXES):
        logger.warning(f"[skill_source]拒绝访问高风险地址 host={host}")
        return False

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        return not _is_disallowed_ip_address(literal_ip)

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return True
    except Exception as e:
        logger.warning(f"[skill_source]解析地址失败，已拒绝 {host}: {e}")
        return False

    for info in infos:
        try:
            resolved_ip = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if _is_disallowed_ip_address(resolved_ip):
            logger.warning(f"[skill_source]拒绝解析到内网/本地的地址 host={host} ip={resolved_ip}")
            return False
    return True


def parse_skill_sources(raw: Any, logger: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []

    parsed: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        candidate = Path(text)
        if candidate.exists() and candidate.is_file():
            try:
                loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[skill_source] load config failed {candidate}: {e}")
                return []
            parsed = loaded
        else:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = [line.strip() for line in text.splitlines() if line.strip()]

    if isinstance(parsed, dict):
        parsed = parsed.get("sources", [])
    if not isinstance(parsed, list):
        return []

    results: list[dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if isinstance(item, str):
            item = {"source": item}
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("url") or item.get("path") or "").strip()
        if not source:
            continue
        results.append(
            {
                "name": str(item.get("name") or f"source_{index + 1}").strip() or f"source_{index + 1}",
                "source": source,
                "ref": str(item.get("ref") or "").strip(),
                "subdir": str(item.get("subdir") or "").strip(),
                "kind": str(item.get("kind") or item.get("type") or "auto").strip().lower(),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return results


def get_skill_cache_dir(plugin_config: Any, data_dir: Path) -> Path:
    custom = str(getattr(plugin_config, "personification_skill_cache_dir", "") or "").strip()
    if custom:
        return Path(custom)
    return data_dir / "skill_cache"


async def resolve_skill_source_dirs(
    *,
    plugin_config: Any,
    logger: Any,
    cache_dir: Path,
) -> list[Path]:
    sources = parse_skill_sources(
        getattr(plugin_config, "personification_skill_sources", None),
        logger,
    )
    if not sources:
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for source in sources:
        if not source.get("enabled", True):
            continue
        resolved = await _prepare_source_dir(
            source=source,
            cache_dir=cache_dir,
            logger=logger,
            update_interval=max(0, int(getattr(plugin_config, "personification_skill_update_interval", 3600) or 0)),
        )
        if resolved is not None:
            results.append(resolved)
    return results


def discover_skill_dirs(root: Path, *, max_depth: int = 5) -> list[Path]:
    if not root.exists():
        return []

    markers = ("skill.yaml", "SKILL.md")
    found: dict[str, Path] = {}
    if any((root / marker).exists() for marker in markers):
        found[str(root.resolve()).lower()] = root

    for marker in markers:
        for path in root.rglob(marker):
            try:
                relative = path.relative_to(root)
            except Exception:
                continue
            if len(relative.parts) > max_depth:
                continue
            skill_dir = path.parent
            key = str(skill_dir.resolve()).lower()
            found[key] = skill_dir
    return sorted(found.values(), key=lambda item: str(item).lower())


async def _prepare_source_dir(
    *,
    source: dict[str, Any],
    cache_dir: Path,
    logger: Any,
    update_interval: int,
) -> Path | None:
    raw_source = str(source.get("source") or "").strip()
    if not raw_source:
        return None

    local_path = Path(raw_source)
    kind = str(source.get("kind") or "auto").strip().lower()
    if local_path.exists() and local_path.is_dir() and kind in {"auto", "dir", "path"}:
        return _apply_subdir(local_path, str(source.get("subdir") or "").strip(), logger)
    if local_path.exists() and local_path.is_file() and local_path.suffix.lower() == ".zip":
        extracted = cache_dir / _source_cache_name(source)
        _extract_zip_file(local_path, extracted, logger, force=True)
        return _apply_subdir(extracted, str(source.get("subdir") or "").strip(), logger)

    remote_url = _normalize_remote_url(raw_source, source)
    if not remote_url:
        logger.warning(f"[skill_source] unsupported source: {raw_source}")
        return None

    source_cache = cache_dir / _source_cache_name(source)
    archive_path = source_cache / "package.zip"
    extracted_path = source_cache / "extracted"
    manifest_path = source_cache / "manifest.json"
    source_cache.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest(manifest_path)
    should_refresh = not archive_path.exists() or not extracted_path.exists()
    if not should_refresh and update_interval > 0:
        fetched_at = int(manifest.get("fetched_at", 0))
        should_refresh = (int(time.time()) - fetched_at) >= update_interval
    elif not should_refresh and update_interval == 0:
        should_refresh = True

    if should_refresh:
        try:
            download_url = await _resolve_remote_download_url(remote_url)
            await _download_zip(download_url, archive_path, logger)
            _extract_zip_file(archive_path, extracted_path, logger, force=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "source": raw_source,
                        "remote_url": remote_url,
                        "download_url": download_url,
                        "fetched_at": int(time.time()),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[skill_source] fetch failed {raw_source}: {e}")
            if not extracted_path.exists():
                return None

    return _apply_subdir(extracted_path, str(source.get("subdir") or "").strip(), logger)


def _source_cache_name(source: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "name": source.get("name"),
            "source": source.get("source"),
            "ref": source.get("ref"),
            "subdir": source.get("subdir"),
            "kind": source.get("kind"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    base = str(source.get("name") or "source").strip().replace(" ", "_")
    safe = "".join(ch for ch in base if ch.isalnum() or ch in {"_", "-"}) or "source"
    return f"{safe}_{digest}"


def _normalize_remote_url(raw_source: str, source: dict[str, Any]) -> str | None:
    parsed = urlparse(raw_source)
    if parsed.scheme not in {"http", "https"}:
        return None
    if raw_source.lower().endswith(".zip"):
        return raw_source

    host = (parsed.netloc or "").lower()
    if any(domain == host or host.endswith(f".{domain}") for domain in _SKILL_PAGE_HOSTS):
        return raw_source
    if "github.com" not in host:
        return None

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    ref = str(source.get("ref") or "").strip()
    subdir = str(source.get("subdir") or "").strip()
    if len(parts) >= 4 and parts[2] == "tree":
        if not ref:
            ref = parts[3]
        if not subdir and len(parts) > 4:
            subdir = "/".join(parts[4:])
            source["subdir"] = subdir
    ref = ref or "HEAD"
    return f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"


async def _resolve_remote_download_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if not any(domain == host or host.endswith(f".{domain}") for domain in _SKILL_PAGE_HOSTS):
        return url

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        html_text = response.text

    anchor_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for href, text in anchor_pattern.findall(html_text):
        anchor_text = re.sub(r"<[^>]+>", " ", text)
        anchor_text = html.unescape(re.sub(r"\s+", " ", anchor_text)).strip().lower()
        candidate = urljoin(url, html.unescape(href).strip())
        if not candidate:
            continue
        if "download zip" in anchor_text or "下载 zip" in anchor_text or "下载zip" in anchor_text:
            return candidate

    href_pattern = re.compile(r'https?://[^"\'\s<>]+', flags=re.IGNORECASE)
    for candidate in href_pattern.findall(html_text):
        normalized = html.unescape(candidate)
        lower = normalized.lower()
        if lower.endswith(".zip") or "download" in lower or "convex.site" in lower:
            return normalized

    raise RuntimeError(f"failed to resolve zip download url from skill page: {url}")


async def _download_zip(url: str, target: Path, logger: Any) -> None:
    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as client:
        current_url = url
        for _ in range(_MAX_REMOTE_REDIRECTS):
            if not await _is_safe_remote_url(current_url, logger):
                raise RuntimeError(f"unsafe remote zip url: {current_url}")
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("location") or "").strip()
                    if not location:
                        raise RuntimeError("redirect without location")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with open(target, "wb") as fh:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_REMOTE_ZIP_BYTES:
                            raise RuntimeError("remote zip too large")
                        fh.write(chunk)
                return
        raise RuntimeError("too many redirects while downloading remote zip")


def _extract_zip_file(archive_path: Path, dest_dir: Path, logger: Any, *, force: bool = False) -> None:
    if force and dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    with zipfile.ZipFile(archive_path) as zf:
        for member in zf.infolist():
            member_name = member.filename.replace("\\", "/")
            target_path = (dest_dir / member_name).resolve()
            try:
                if os.path.commonpath([str(root), str(target_path)]) != str(root):
                    logger.warning(f"[skill_source] skip unsafe archive member: {member.filename}")
                    continue
            except Exception:
                logger.warning(f"[skill_source] skip unsafe archive member: {member.filename}")
                continue
            zf.extract(member, dest_dir)


def _apply_subdir(root: Path, subdir: str, logger: Any) -> Path | None:
    normalized = _collapse_single_wrapper_dir(root)
    if not subdir:
        return normalized
    candidate = normalized / subdir
    if candidate.exists() and candidate.is_dir():
        return candidate
    logger.warning(f"[skill_source] subdir not found: {subdir} in {normalized}")
    return None


def _collapse_single_wrapper_dir(root: Path) -> Path:
    current = root
    while True:
        children = [path for path in current.iterdir() if path.is_dir()]
        files = [path for path in current.iterdir() if path.is_file()]
        if len(children) != 1 or files:
            return current
        current = children[0]


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "discover_skill_dirs",
    "get_skill_cache_dir",
    "parse_skill_sources",
    "resolve_skill_source_dirs",
]
