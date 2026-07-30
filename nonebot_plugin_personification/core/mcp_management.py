from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

import httpx

from ..agent.tool_registry import AgentTool, ToolRegistry
from ..skill_runtime.mcp_compat import McpStdioClient
from .db import connect_sync
from .mcp_builtin import (
    BUILTIN_SOCIAL_MCP_ID,
    builtin_social_installation,
    builtin_social_launch,
    builtin_social_tools,
    is_builtin_social_installation,
)
from .meme_learning_store import LearningThresholds, MemeLearningStore
from .paths import get_data_dir
from .safe_image_download import download_public_image, resolve_public_url
from .slang_learning import BoundedSlangDiscoveryQueue, DiscoveryTask, SlangLearningPipeline


OFFICIAL_MCP_REGISTRY = "https://registry.modelcontextprotocol.io"
_SAFE_PREFIX = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_SAFE_NPM_PACKAGE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]{0,99}/)?[a-z0-9][a-z0-9._-]{0,99}$")
_SAFE_PYPI_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_EXACT_SEMVER = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_EXACT_PEP440 = re.compile(
    r"""
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>
        [-_.]?(?P<pre_label>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_number>[0-9]+)?
    )?
    (?P<post>
        (?:-(?P<post_number1>[0-9]+))
        |(?:[-_.]?(?P<post_label>post|rev|r)[-_.]?(?P<post_number2>[0-9]+)?)
    )?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_number>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_MAX_REGISTRY_QUERY_CHARS = 1000
_MAX_REGISTRY_CURSOR_CHARS = 16384
_MAX_REGISTRY_INPUT_CHARS = 18000
_SECRET_LOCK = threading.RLock()
_SOCIAL_MEDIA_HOSTS = {
    "bilibili": ("hdslb.com", "biliimg.com"),
    "douyin": ("douyinpic.com", "byteimg.com", "pstatp.com"),
    "tieba": ("tiebapic.baidu.com", "imgsa.baidu.com", "hiphotos.baidu.com"),
    "xiaoheihe": ("xiaoheihe.cn", "heybox.cn", "max-c.com"),
}
_SOCIAL_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}


def _social_media_url_allowed(platform: str, value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and any(
            host == suffix or host.endswith("." + suffix)
            for suffix in _SOCIAL_MEDIA_HOSTS.get(str(platform or ""), ())
        )
    )


def _social_image_data_url(content: bytes) -> str:
    try:
        from PIL import Image, ImageOps

        with Image.open(BytesIO(content)) as source:
            if int(source.width) * int(source.height) > 40_000_000:
                return ""
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1280, 1280))
            encoded = b""
            for quality in (86, 76, 66):
                output = BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                encoded = output.getvalue()
                if len(encoded) <= 900_000:
                    break
            if len(encoded) > 900_000:
                return ""
    except Exception:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except Exception:
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _package_support_error(package: dict[str, Any]) -> str:
    package_type = str(package.get("registryType") or "").strip().lower()
    transport = package.get("transport") if isinstance(package.get("transport"), dict) else {}
    if package_type not in {"npm", "pypi"} or transport.get("type") != "stdio":
        return "Only npm/PyPI stdio packages are supported."
    identifier = str(package.get("identifier") or "").strip()
    identifier_pattern = _SAFE_NPM_PACKAGE if package_type == "npm" else _SAFE_PYPI_PACKAGE
    if not identifier_pattern.fullmatch(identifier):
        return "Package identifier is invalid or could be interpreted as a launcher option."
    version = str(package.get("version") or "").strip()
    if len(version) > 255:
        return "Package version is too long."
    if package_type == "npm":
        if not _EXACT_SEMVER.fullmatch(version):
            return "npm package must use an exact three-part SemVer version."
    elif not _is_exact_pep440_version(version):
        return "PyPI package must use an exact PEP 440 version with at least three release segments."
    file_sha256 = str(package.get("fileSha256") or "").strip()
    if file_sha256:
        if not re.fullmatch(r"[a-f0-9]{64}", file_sha256):
            return "Package fileSha256 metadata is invalid."
        return "fileSha256 cannot be proven for the artifact executed by npx/uvx; quick install fails closed."
    registry_base = str(package.get("registryBaseUrl") or "").strip().rstrip("/").lower()
    allowed_bases = {
        "npm": {"", "https://registry.npmjs.org"},
        "pypi": {"", "https://pypi.org", "https://pypi.org/simple"},
    }
    if registry_base not in allowed_bases[package_type]:
        return "Custom package registry URLs are not supported by quick install."
    runtime_hint = str(package.get("runtimeHint") or "").strip().lower()
    expected_hint = "npx" if package_type == "npm" else "uvx"
    if runtime_hint and runtime_hint != expected_hint:
        return f"Package requires unsupported runtime: {runtime_hint}."
    if package.get("runtimeArguments"):
        return "Custom runtime arguments are not supported by quick install."
    input_keys: list[str] = []
    for spec in list(package.get("environmentVariables") or []):
        if not isinstance(spec, dict):
            continue
        if "{" in str(spec.get("value") or "") or spec.get("variables"):
            return "Templated environment variables are not supported by quick install."
        if spec.get("value") is None:
            input_keys.append(str(spec.get("name") or "").strip())
    for spec in list(package.get("packageArguments") or []):
        if not isinstance(spec, dict):
            continue
        if bool(spec.get("isSecret", False)):
            return "Secret package arguments cannot be passed safely."
        if bool(spec.get("isRepeated", False)):
            return "Repeated package arguments are not supported by quick install."
        if str(spec.get("type") or "positional") not in {"named", "positional"}:
            return "Package argument type is unsupported."
        if "{" in str(spec.get("value") or "") or spec.get("variables"):
            return "Templated package arguments are not supported by quick install."
        if spec.get("value") is None:
            input_keys.append(str(spec.get("name") or spec.get("valueHint") or "").strip())
    configurable_keys = [key for key in input_keys if key]
    if len(configurable_keys) != len(set(configurable_keys)):
        return "Package contains duplicate configurable input names."
    return ""


def _is_exact_pep440_version(value: str) -> bool:
    text = str(value or "")
    if not text or text != text.strip() or any(character.isspace() for character in text):
        return False
    match = _EXACT_PEP440.fullmatch(text)
    return match is not None and len(match.group("release").split(".")) >= 3


def _package_digest(package: dict[str, Any]) -> str:
    payload = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mcp_registry_sources(plugin_config: Any) -> list[dict[str, str]]:
    sources = [{"id": "official", "name": "Official MCP Registry", "url": OFFICIAL_MCP_REGISTRY, "preview": True}]
    raw = getattr(plugin_config, "personification_mcp_registry_sources", None)
    if isinstance(raw, str):
        raw = _loads(raw, [])
    if not isinstance(raw, list):
        return sources
    for item in raw:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        url = str(item.get("url") or "").strip().rstrip("/")
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
            or len(url) > 500
        ):
            continue
        source_id = "registry_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        sources.append({"id": source_id, "name": str(item.get("name") or source_id)[:80], "url": url, "preview": False})
    return sources


def resolve_registry_source(plugin_config: Any, source_id: str) -> dict[str, str]:
    for source in mcp_registry_sources(plugin_config):
        if source["id"] == source_id:
            return source
    raise ValueError("unknown MCP Registry source")


def _pinned_registry_target(url: str, approved_ip: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or parsed.port not in {None, 443}:
        raise ValueError("MCP Registry must use HTTPS on port 443")
    host = str(parsed.hostname or "")
    address = ipaddress.ip_address(approved_ip)
    connection_host = f"[{address}]" if address.version == 6 else str(address)
    connection_url = urlunsplit(("https", connection_host, parsed.path or "/", parsed.query, ""))
    return connection_url, host, host


class McpRegistryClient:
    def __init__(self, plugin_config: Any) -> None:
        self.plugin_config = plugin_config
        self.timeout = max(3, min(int(getattr(plugin_config, "personification_mcp_registry_timeout", 20) or 20), 60))
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        fresh: bool = False,
    ) -> dict[str, Any]:
        cache_key = url + "?" + json.dumps(params or {}, sort_keys=True)
        cached = self._cache.get(cache_key)
        if not fresh and cached and time.time() - cached[0] < 300:
            return cached[1]
        try:
            original_url, approved_ip = await resolve_public_url(url)
        except Exception as exc:
            raise ValueError("MCP Registry must resolve to a public HTTPS address") from exc
        connection_url, host_header, sni_hostname = _pinned_registry_target(original_url, approved_ip)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            request = client.build_request(
                "GET",
                connection_url,
                params=params,
                headers={"Accept": "application/json", "Host": host_header},
            )
            request.extensions["sni_hostname"] = sni_hostname
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise ValueError("MCP Registry response is too large")
            finally:
                await response.aclose()
        try:
            payload = json.loads(body)
        except Exception as exc:
            raise ValueError("invalid MCP Registry response") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid MCP Registry response")
        if len(self._cache) >= 128:
            oldest_key = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest_key, None)
        self._cache[cache_key] = (time.time(), payload)
        return payload

    async def search(self, source: dict[str, str], query: str, *, limit: int = 20, cursor: str = "") -> dict[str, Any]:
        query_value = str(query or "").strip()
        cursor_value = str(cursor or "")
        if len(query_value) > _MAX_REGISTRY_QUERY_CHARS:
            raise ValueError("MCP Registry search query is too large")
        if len(cursor_value) > _MAX_REGISTRY_CURSOR_CHARS:
            raise ValueError("MCP Registry cursor is too large")
        if len(query_value) + len(cursor_value) > _MAX_REGISTRY_INPUT_CHARS:
            raise ValueError("MCP Registry search input is too large")
        params: dict[str, Any] = {"version": "latest", "limit": max(1, min(int(limit or 20), 50))}
        if query_value:
            params["search"] = query_value
        if cursor_value:
            params["cursor"] = cursor_value
        payload = await self._get(source["url"] + "/v0.1/servers", params)
        results = []
        for item in list(payload.get("servers") or []):
            if not isinstance(item, dict):
                continue
            server = item.get("server") if isinstance(item.get("server"), dict) else item
            if not isinstance(server, dict):
                continue
            response_meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
            official = response_meta.get("io.modelcontextprotocol.registry/official") or {}
            if not isinstance(official, dict):
                official = {}
            results.append(
                {
                    "name": str(server.get("name") or ""),
                    "title": str(server.get("title") or server.get("name") or ""),
                    "description": str(server.get("description") or ""),
                    "version": str(server.get("version") or ""),
                    "status": str(official.get("status") or "unknown"),
                    "status_message": str(official.get("statusMessage") or ""),
                    "repository": server.get("repository") if isinstance(server.get("repository"), dict) else {},
                    "website": str(server.get("websiteUrl") or ""),
                    "website_url": str(server.get("websiteUrl") or ""),
                    "schema": str(server.get("$schema") or ""),
                    "stdio_packages": sum(
                        1 for package in list(server.get("packages") or [])
                        if isinstance(package, dict) and (package.get("transport") or {}).get("type") == "stdio" and package.get("registryType") in {"npm", "pypi"}
                    ),
                    "remote_count": len(list(server.get("remotes") or [])),
                    "source_id": source["id"],
                }
            )
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        next_cursor = metadata.get("nextCursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("MCP Registry returned an invalid cursor")
        return {"servers": results, "next_cursor": next_cursor or "", "source": source}

    async def detail(self, source: dict[str, str], server_name: str, *, fresh: bool = False) -> dict[str, Any]:
        name = str(server_name or "").strip()
        if not name or len(name) > 240:
            raise ValueError("invalid MCP server name")
        payload = await self._get(
            source["url"] + f"/v0.1/servers/{quote(name, safe='')}/versions/latest",
            fresh=fresh,
        )
        server = payload.get("server") if isinstance(payload.get("server"), dict) else payload
        if not isinstance(server, dict) or str(server.get("name") or "") != name:
            raise ValueError("MCP server detail does not match request")
        response_meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        official = response_meta.get("io.modelcontextprotocol.registry/official") or {}
        if not isinstance(official, dict):
            official = {}
        packages = []
        for index, package in enumerate(list(server.get("packages") or [])):
            if not isinstance(package, dict):
                continue
            transport = package.get("transport") if isinstance(package.get("transport"), dict) else {}
            support_error = _package_support_error(package)
            inputs = []
            input_groups = (
                ("environment", list(package.get("environmentVariables") or [])),
                ("argument", list(package.get("packageArguments") or [])),
            )
            for location, values in input_groups:
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    if value.get("value") is not None:
                        continue
                    key = str(value.get("name") or value.get("valueHint") or "").strip()
                    if not key:
                        continue
                    is_secret = bool(value.get("isSecret", False))
                    inputs.append({
                        "key": key,
                        "description": str(value.get("description") or ""),
                        "required": bool(value.get("isRequired", False)),
                        "secret": is_secret,
                        "default": str(value.get("default") or ""),
                        "choices": [str(item) for item in list(value.get("choices") or [])],
                        "format": str(value.get("format") or "string"),
                        "location": location,
                    })
            packages.append({
                "index": index,
                "digest": _package_digest(package),
                "registry_type": str(package.get("registryType") or ""),
                "identifier": str(package.get("identifier") or ""),
                "version": str(package.get("version") or server.get("version") or ""),
                "transport": str(transport.get("type") or ""),
                "fileSha256": str(package.get("fileSha256") or ""),
                "supported": not support_error,
                "unsupported_reason": support_error,
                "inputs": inputs,
            })
        return {
            "server": {
                "name": name,
                "title": str(server.get("title") or name),
                "description": str(server.get("description") or ""),
                "version": str(server.get("version") or ""),
                "status": str(official.get("status") or "unknown"),
                "status_message": str(official.get("statusMessage") or ""),
                "repository": server.get("repository") if isinstance(server.get("repository"), dict) else {},
                "website": str(server.get("websiteUrl") or ""),
                "website_url": str(server.get("websiteUrl") or ""),
                "schema": str(server.get("$schema") or ""),
            },
            "packages": packages,
            "raw": server,
            "source": source,
            "warning": "Registry namespace verification does not mean the package was security-audited.",
        }


class McpSecretStore:
    def __init__(self, plugin_config: Any) -> None:
        configured = str(getattr(plugin_config, "personification_mcp_secret_file", "") or "").strip()
        self.path = Path(configured) if configured else get_data_dir(plugin_config) / "mcp" / "secrets.json"

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.is_file():
            return {}
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("MCP Secret store is unreadable") from exc
        if not isinstance(parsed, dict) or any(not isinstance(value, dict) for value in parsed.values()):
            raise RuntimeError("MCP Secret store has an invalid structure")
        return parsed

    def _write(self, payload: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex[:8]}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        temp.replace(self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def set(self, installation_id: str, values: dict[str, str]) -> None:
        with _SECRET_LOCK:
            payload = self._read()
            payload[installation_id] = {str(key): str(value) for key, value in values.items()}
            self._write(payload)

    def get(self, installation_id: str) -> dict[str, str]:
        with _SECRET_LOCK:
            return dict(self._read().get(installation_id) or {})

    def delete(self, installation_id: str) -> None:
        with _SECRET_LOCK:
            payload = self._read()
            if installation_id in payload:
                payload.pop(installation_id, None)
                self._write(payload)


class McpStore:
    def ensure_builtin_social(self) -> dict[str, Any]:
        item = builtin_social_installation()
        tools = [_tool_policy(item["installation_id"], item["name_prefix"], tool) for tool in builtin_social_tools()]
        now = time.time()
        metadata_json = json.dumps(item["metadata"], ensure_ascii=False)
        with connect_sync() as conn:
            conn.execute(
                """INSERT INTO mcp_installations(
                    installation_id,source_id,source_url,server_name,server_title,server_version,package_type,
                    package_identifier,command,args_json,env_json,secret_names_json,name_prefix,desired_enabled,
                    observed_status,metadata_json,last_error,created_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,'stopped',?,'','system',?,?)
                ON CONFLICT(installation_id) DO UPDATE SET
                    source_id=excluded.source_id,source_url=excluded.source_url,server_name=excluded.server_name,
                    server_title=excluded.server_title,server_version=excluded.server_version,
                    package_type=excluded.package_type,package_identifier=excluded.package_identifier,
                    command=excluded.command,args_json=excluded.args_json,env_json=excluded.env_json,
                    secret_names_json=excluded.secret_names_json,name_prefix=excluded.name_prefix,
                    metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                (
                    item["installation_id"], item["source_id"], item["source_url"], item["server_name"],
                    item["server_title"], item["server_version"], item["package_type"], item["package_identifier"],
                    item["command"], json.dumps(item["args"], ensure_ascii=False), "{}", "[]", item["name_prefix"],
                    metadata_json, now, now,
                ),
            )
            for platform in ("bilibili", "douyin", "tieba", "xiaoheihe"):
                conn.execute(
                    """INSERT OR IGNORE INTO mcp_builtin_platforms(
                        installation_id,platform,desired_enabled,revision,config_json,created_at,updated_at
                    ) VALUES(?,?,0,0,'{}',?,?)""",
                    (item["installation_id"], platform, now, now),
                )
            for policy in tools:
                conn.execute(
                    """INSERT INTO mcp_tool_policies(
                        installation_id,remote_name,registered_name,title,description,parameters_json,
                        output_schema_json,annotations_json,enabled,risk_level,side_effect,
                        publisher_read_only,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,0,'low','none',1,?)
                    ON CONFLICT(installation_id,remote_name) DO UPDATE SET
                        registered_name=excluded.registered_name,title=excluded.title,
                        description=excluded.description,parameters_json=excluded.parameters_json,
                        output_schema_json=excluded.output_schema_json,annotations_json=excluded.annotations_json,
                        risk_level='low',side_effect='none',publisher_read_only=1,updated_at=excluded.updated_at""",
                    (
                        policy["installation_id"], policy["remote_name"], policy["registered_name"], policy["title"],
                        policy["description"], json.dumps(policy["parameters"], ensure_ascii=False),
                        json.dumps(policy["output_schema"], ensure_ascii=False),
                        json.dumps(policy["annotations"], ensure_ascii=False), now,
                    ),
                )
            conn.commit()
        result = self.get_installation(BUILTIN_SOCIAL_MCP_ID)
        if result is None:
            raise RuntimeError("builtin social MCP installation was not created")
        return result

    def list_installations(self) -> list[dict[str, Any]]:
        with connect_sync() as conn:
            rows = conn.execute("SELECT * FROM mcp_installations ORDER BY created_at DESC").fetchall()
        return [self._installation(row) for row in rows]

    def get_installation(self, installation_id: str) -> dict[str, Any] | None:
        with connect_sync() as conn:
            row = conn.execute("SELECT * FROM mcp_installations WHERE installation_id=?", (installation_id,)).fetchone()
        return self._installation(row) if row is not None else None

    def _installation(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        for field, default in (("args_json", []), ("env_json", {}), ("secret_names_json", []), ("metadata_json", {})):
            item[field[:-5]] = _loads(item.pop(field, ""), default)
        item["desired_enabled"] = bool(item.get("desired_enabled"))
        item["secrets_configured"] = bool(item.get("secret_names"))
        return item

    def tools(self, installation_id: str) -> list[dict[str, Any]]:
        with connect_sync() as conn:
            rows = conn.execute("SELECT * FROM mcp_tool_policies WHERE installation_id=? ORDER BY registered_name", (installation_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["parameters"] = _loads(item.pop("parameters_json", "{}"), {})
            item["output_schema"] = _loads(item.pop("output_schema_json", "{}"), {})
            item["annotations"] = _loads(item.pop("annotations_json", "{}"), {})
            item["inputSchema"] = item["parameters"]
            item["outputSchema"] = item["output_schema"]
            item["enabled"] = bool(item.get("enabled"))
            item["publisher_read_only"] = bool(item.get("publisher_read_only"))
            result.append(item)
        return result

    def save_installation(self, item: dict[str, Any], tools: list[dict[str, Any]]) -> None:
        now = time.time()
        with connect_sync() as conn:
            conn.execute(
                """INSERT INTO mcp_installations(
                    installation_id,source_id,source_url,server_name,server_title,server_version,package_type,
                    package_identifier,command,args_json,env_json,secret_names_json,name_prefix,desired_enabled,
                    observed_status,metadata_json,last_error,created_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["installation_id"], item["source_id"], item["source_url"], item["server_name"], item.get("server_title", ""),
                    item["server_version"], item["package_type"], item["package_identifier"], item["command"],
                    json.dumps(item["args"], ensure_ascii=False), json.dumps(item["env"], ensure_ascii=False),
                    json.dumps(item["secret_names"], ensure_ascii=False), item["name_prefix"], 1, "ready",
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False), "", item.get("created_by", ""), now, now,
                ),
            )
            for tool in tools:
                conn.execute(
                    """INSERT INTO mcp_tool_policies(
                        installation_id,remote_name,registered_name,title,description,parameters_json,
                        output_schema_json,annotations_json,enabled,risk_level,side_effect,publisher_read_only,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["installation_id"], tool["remote_name"], tool["registered_name"], tool.get("title", ""),
                        tool["description"], json.dumps(tool["parameters"], ensure_ascii=False),
                        json.dumps(tool.get("output_schema") or {}, ensure_ascii=False),
                        json.dumps(tool.get("annotations") or {}, ensure_ascii=False), int(tool["enabled"]),
                        tool["risk_level"], tool["side_effect"], int(tool["publisher_read_only"]), now,
                    ),
                )
            conn.commit()

    def sync_tools(
        self,
        installation_id: str,
        prefix: str,
        remote_tools: list[dict[str, Any]],
    ) -> dict[str, int]:
        incoming = [_tool_policy(installation_id, prefix, item) for item in remote_tools]
        remote_names = [item["remote_name"] for item in incoming]
        registered_names = [item["registered_name"] for item in incoming]
        if len(remote_names) != len(set(remote_names)):
            raise ValueError("MCP server exposed duplicate tool names")
        if len(registered_names) != len(set(registered_names)):
            raise ValueError("MCP tool names collide after normalization")

        now = time.time()
        with connect_sync() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_tool_policies WHERE installation_id=?",
                (installation_id,),
            ).fetchall()
            existing = {str(row["remote_name"]): dict(row) for row in rows}
            added = updated = 0
            for policy in incoming:
                old = existing.get(policy["remote_name"])
                parameters_json = json.dumps(policy["parameters"], ensure_ascii=False)
                output_schema_json = json.dumps(policy["output_schema"], ensure_ascii=False)
                annotations_json = json.dumps(policy["annotations"], ensure_ascii=False)
                if old is None:
                    conn.execute(
                        """INSERT INTO mcp_tool_policies(
                            installation_id,remote_name,registered_name,title,description,parameters_json,
                            output_schema_json,annotations_json,enabled,risk_level,side_effect,
                            publisher_read_only,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,0,'review','unknown',?,?)""",
                        (
                            installation_id, policy["remote_name"], policy["registered_name"], policy["title"],
                            policy["description"], parameters_json, output_schema_json, annotations_json,
                            int(policy["publisher_read_only"]), now,
                        ),
                    )
                    added += 1
                    continue
                changed = any(
                    (
                        str(old.get("title") or "") != policy["title"],
                        str(old.get("description") or "") != policy["description"],
                        _loads(old.get("parameters_json"), {}) != policy["parameters"],
                        _loads(old.get("output_schema_json"), {}) != policy["output_schema"],
                        _loads(old.get("annotations_json"), {}) != policy["annotations"],
                        bool(old.get("publisher_read_only")) != policy["publisher_read_only"],
                    )
                )
                conn.execute(
                    """UPDATE mcp_tool_policies SET
                        title=?,description=?,parameters_json=?,output_schema_json=?,annotations_json=?,
                        publisher_read_only=?,updated_at=?
                    WHERE installation_id=? AND remote_name=?""",
                    (
                        policy["title"], policy["description"], parameters_json, output_schema_json,
                        annotations_json, int(policy["publisher_read_only"]), now,
                        installation_id, policy["remote_name"],
                    ),
                )
                updated += int(changed)
            removed_names = set(existing) - set(remote_names)
            if removed_names:
                conn.executemany(
                    "DELETE FROM mcp_tool_policies WHERE installation_id=? AND remote_name=?",
                    [(installation_id, name) for name in removed_names],
                )
            conn.commit()
        return {"added": added, "updated": updated, "removed": len(removed_names), "total": len(incoming)}

    def set_protocol_metadata(
        self,
        installation_id: str,
        *,
        protocol_version: str,
        server_info: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> None:
        with connect_sync() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM mcp_installations WHERE installation_id=?",
                (installation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(installation_id)
            metadata = _loads(row["metadata_json"], {})
            metadata["protocol_version"] = str(protocol_version)
            metadata["server_info"] = dict(server_info)
            metadata["capabilities"] = dict(capabilities)
            conn.execute(
                "UPDATE mcp_installations SET metadata_json=?,updated_at=? WHERE installation_id=?",
                (json.dumps(metadata, ensure_ascii=False), time.time(), installation_id),
            )
            conn.commit()

    def set_installation_enabled(self, installation_id: str, enabled: bool) -> None:
        with connect_sync() as conn:
            if conn.execute(
                "UPDATE mcp_installations SET desired_enabled=?,updated_at=? WHERE installation_id=?",
                (int(enabled), time.time(), installation_id),
            ).rowcount != 1:
                raise KeyError(installation_id)
            conn.commit()

    def set_status(self, installation_id: str, status_value: str, error: str = "") -> None:
        with connect_sync() as conn:
            conn.execute(
                "UPDATE mcp_installations SET observed_status=?,last_error=?,updated_at=? WHERE installation_id=?",
                (status_value, str(error)[:300], time.time(), installation_id),
            )
            conn.commit()

    def set_tool_enabled(
        self,
        installation_id: str,
        remote_name: str,
        enabled: bool,
        *,
        approve_side_effect: bool = False,
        trusted_read_only: bool = False,
    ) -> None:
        if enabled and not approve_side_effect:
            raise ValueError("enabling an MCP tool requires explicit risk approval")
        with connect_sync() as conn:
            assignments = "enabled=?,updated_at=?"
            values: list[Any] = [int(enabled), time.time()]
            if enabled and approve_side_effect and not trusted_read_only:
                assignments += ",risk_level='high',side_effect='external'"
            elif trusted_read_only:
                assignments += ",risk_level='low',side_effect='none',publisher_read_only=1"
            values.extend((installation_id, remote_name))
            if conn.execute(
                f"UPDATE mcp_tool_policies SET {assignments} WHERE installation_id=? AND remote_name=?",
                values,
            ).rowcount != 1:
                raise KeyError(remote_name)
            conn.commit()

    def delete(self, installation_id: str) -> None:
        if is_builtin_social_installation(installation_id):
            raise ValueError("builtin MCP installation cannot be deleted")
        with connect_sync() as conn:
            if conn.execute("DELETE FROM mcp_installations WHERE installation_id=?", (installation_id,)).rowcount != 1:
                raise KeyError(installation_id)
            conn.commit()


def _minimal_process_env(extra: dict[str, str]) -> dict[str, str]:
    allowed = {
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    }
    allowed_lower = {key.lower() for key in allowed}
    env = {key: value for key, value in os.environ.items() if key.lower() in allowed_lower}
    env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _input_value(spec: dict[str, Any], inputs: dict[str, Any]) -> str:
    key = str(spec.get("name") or spec.get("valueHint") or "").strip()
    if spec.get("value") is not None:
        value = str(spec.get("value"))
    else:
        value = str(inputs.get(key, spec.get("default", "")) or "")
    if len(value) > 32768:
        raise ValueError(f"MCP input is too large: {key}")
    if bool(spec.get("isRequired", False)) and not value:
        raise ValueError(f"required MCP input is missing: {key}")
    choices = [str(item) for item in list(spec.get("choices") or [])]
    if value and choices and value not in choices:
        raise ValueError(f"invalid MCP input choice: {key}")
    return value


def _package_arguments(package: dict[str, Any], inputs: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for spec in list(package.get("packageArguments") or []):
        if not isinstance(spec, dict):
            continue
        if bool(spec.get("isSecret", False)):
            raise ValueError("Secret MCP package arguments are unsupported because process arguments are observable")
        value = _input_value(spec, inputs)
        if not value:
            continue
        if str(spec.get("type") or "positional") == "named":
            name = str(spec.get("name") or "").strip()
            if not re.fullmatch(r"(?:-{1,2})?[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
                raise ValueError("named MCP argument has an invalid flag name")
            flag = name if name.startswith("-") else "--" + name
            result.append(f"{flag}={value}")
        else:
            result.append(value)
    return result


def build_launch_plan(package: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    package_type = str(package.get("registryType") or "").strip().lower()
    transport = package.get("transport") if isinstance(package.get("transport"), dict) else {}
    identifier = str(package.get("identifier") or "").strip()
    version = str(package.get("version") or "").strip()
    support_error = _package_support_error(package)
    if support_error:
        raise ValueError(support_error)
    env: dict[str, str] = {}
    secrets: dict[str, str] = {}
    secret_names: list[str] = []
    for spec in list(package.get("environmentVariables") or []):
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            raise ValueError("invalid MCP environment variable name")
        value = _input_value(spec, inputs)
        if bool(spec.get("isSecret", False)):
            secret_names.append(name)
            if value:
                secrets[name] = value
        elif value:
            env[name] = value
    package_args = _package_arguments(package, inputs)
    if package_type == "npm":
        command = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
        args = ["--yes", f"{identifier}@{version}", *package_args]
    else:
        command = shutil.which("uvx") or "uvx"
        args = ["--from", f"{identifier}=={version}", identifier, *package_args]
    return {
        "package_type": package_type,
        "package_identifier": identifier,
        "version": version,
        "command": command,
        "args": args,
        "env": env,
        "secrets": secrets,
        "secret_names": secret_names,
    }


def _tool_policy(installation_id: str, prefix: str, item: dict[str, Any]) -> dict[str, Any]:
    remote_name = str(item.get("name") or "").strip()
    if not remote_name or len(remote_name) > 200:
        raise ValueError("MCP tool name is missing or too long")
    safe_remote = re.sub(r"[^A-Za-z0-9_-]+", "_", remote_name).strip("_")[:40] or "tool"
    registered_name = (prefix + safe_remote)[:64]
    raw_annotations = item.get("annotations")
    if raw_annotations is not None and not isinstance(raw_annotations, dict):
        raise ValueError(f"MCP tool has invalid annotations: {remote_name}")
    annotations = dict(raw_annotations or {})
    if len(json.dumps(annotations, ensure_ascii=False)) > 64 * 1024:
        raise ValueError(f"MCP tool has oversized annotations: {remote_name}")
    publisher_read_only = annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True
    parameters = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}
    if parameters.get("type") != "object" or len(json.dumps(parameters, ensure_ascii=False)) > 256 * 1024:
        raise ValueError(f"MCP tool has an invalid or oversized input schema: {remote_name}")
    raw_output_schema = item.get("outputSchema")
    if raw_output_schema is not None and not isinstance(raw_output_schema, dict):
        raise ValueError(f"MCP tool has an invalid output schema: {remote_name}")
    output_schema = dict(raw_output_schema or {})
    if len(json.dumps(output_schema, ensure_ascii=False)) > 256 * 1024:
        raise ValueError(f"MCP tool has an oversized output schema: {remote_name}")
    title = str(item.get("title") or annotations.get("title") or "").strip()
    if len(title) > 500:
        raise ValueError(f"MCP tool title is too long: {remote_name}")
    return {
        "installation_id": installation_id,
        "remote_name": remote_name,
        "registered_name": registered_name,
        "title": title,
        "description": str(item.get("description") or f"MCP tool {remote_name}")[:1200],
        "parameters": parameters,
        "output_schema": output_schema,
        "annotations": annotations,
        "enabled": False,
        "risk_level": "review",
        "side_effect": "unknown",
        "publisher_read_only": publisher_read_only,
    }


class McpRuntimeManager:
    def __init__(self, runtime: Any, registry: ToolRegistry) -> None:
        self.runtime = runtime
        self.registry = registry
        self.store = McpStore()
        self.secrets = McpSecretStore(runtime.plugin_config)
        self.registry_client = McpRegistryClient(runtime.plugin_config)
        self._clients: dict[str, McpStdioClient] = {}
        self._tool_names: dict[str, set[str]] = {}
        self._slang_discovery_queue: BoundedSlangDiscoveryQueue | None = None
        self._lock = asyncio.Lock()
        self.store.ensure_builtin_social()

    def _slang_tool_caller(self) -> Any:
        bundle = getattr(self.runtime, "runtime_bundle", None)
        deps = getattr(bundle, "reply_processor_deps", None)
        inner = getattr(deps, "runtime", None)
        return (
            getattr(inner, "lite_tool_caller", None)
            or getattr(inner, "agent_tool_caller", None)
            or getattr(bundle, "lite_tool_caller", None)
            or getattr(bundle, "agent_tool_caller", None)
        )

    def _slang_thresholds(self) -> LearningThresholds:
        cfg = self.runtime.plugin_config
        return LearningThresholds(
            auto_understand_min_sources=getattr(cfg, "personification_auto_understand_min_sources", 2),
            auto_use_min_sources=getattr(cfg, "personification_auto_use_min_sources", 3),
            auto_use_min_platforms=getattr(cfg, "personification_auto_use_min_platforms", 2),
            claim_min_confidence=getattr(cfg, "personification_claim_min_confidence", 0.72),
            semantic_equivalence_min_confidence=getattr(
                cfg, "personification_semantic_equivalence_min_confidence", 0.80
            ),
            reverify_after_days=getattr(cfg, "personification_reverify_after_days", 30),
            stale_after_days=getattr(cfg, "personification_stale_after_days", 90),
        ).normalized()

    async def _ingest_discovery_task(self, task: DiscoveryTask) -> None:
        caller = self._slang_tool_caller()
        if caller is None:
            return
        pipeline = SlangLearningPipeline(
            tool_caller=caller,
            max_claims=max(
                1,
                min(
                    50,
                    int(
                        getattr(
                            self.runtime.plugin_config,
                            "personification_slang_max_claims",
                            20,
                        )
                        or 20
                    ),
                ),
            ),
        )
        await MemeLearningStore(self._slang_thresholds()).ingest_claims(
            [task.claim],
            semantic_pipeline=pipeline,
            model_route="builtin_social_background",
        )

    def _discovery_queue(self) -> BoundedSlangDiscoveryQueue:
        if self._slang_discovery_queue is None:
            self._slang_discovery_queue = BoundedSlangDiscoveryQueue(
                self._ingest_discovery_task,
                max_global=100,
                max_per_content=5,
                max_per_platform=30,
                concurrency=2,
            )
        return self._slang_discovery_queue

    async def _postprocess_builtin_social_result(
        self,
        remote_name: str,
        arguments: dict[str, Any],
        raw_result: str,
    ) -> str:
        caller = self._slang_tool_caller()
        if caller is None:
            return raw_result
        try:
            packet = json.loads(raw_result)
            if not isinstance(packet, dict):
                return raw_result
            max_claims = max(
                1,
                min(50, int(getattr(self.runtime.plugin_config, "personification_slang_max_claims", 20) or 20)),
            )
            target_term = str(arguments.get("term") or arguments.get("query") or "").strip()[:80]
            pipeline = SlangLearningPipeline(tool_caller=caller, max_claims=max_claims)
            claims = await pipeline.extract_claims(packet, target_term=target_term)
            normalized_target = target_term.casefold()
            target_claims = [
                claim
                for claim in claims
                if normalized_target
                and normalized_target
                in {
                    str(claim.get("term") or "").strip().casefold(),
                    *{str(alias or "").strip().casefold() for alias in list(claim.get("aliases") or [])},
                }
            ]
            target_senses: list[dict[str, Any]] = []
            if target_claims:
                target_senses = await MemeLearningStore(self._slang_thresholds()).ingest_claims(
                    target_claims,
                    semantic_pipeline=pipeline,
                    model_route=f"builtin_social_{remote_name}",
                )
            target_claim_ids = {id(claim) for claim in target_claims}
            extra_claims = [claim for claim in claims if id(claim) not in target_claim_ids]
            queued = self._discovery_queue().schedule_claims(extra_claims, target_term=target_term)
            return json.dumps(
                {
                    **packet,
                    "slang_claims": claims,
                    "target_senses": target_senses,
                    "background_claims_queued": queued,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            logger = getattr(self.runtime, "logger", None)
            if logger is not None:
                try:
                    logger.debug(f"builtin social slang learning skipped: {type(exc).__name__}")
                except Exception:
                    pass
            return raw_result

    def _secret_environment(self, item: dict[str, Any]) -> dict[str, str]:
        required = [str(name) for name in item.get("secret_names") or [] if str(name)]
        stored = self.secrets.get(str(item.get("installation_id") or ""))
        if any(not str(stored.get(name) or "") for name in required):
            raise RuntimeError("required MCP Secret is unavailable")
        return {name: str(stored[name]) for name in required}

    def _client(self, item: dict[str, Any]) -> McpStdioClient:
        if is_builtin_social_installation(item):
            command, args, env_values, cwd_value = builtin_social_launch(self.runtime.plugin_config)
            cwd = Path(cwd_value)
        else:
            command = str(item["command"])
            args = [str(value) for value in item.get("args") or []]
            env_values = dict(item.get("env") or {})
            env_values.update(self._secret_environment(item))
            cwd = get_data_dir(self.runtime.plugin_config) / "mcp" / "runtime" / item["installation_id"][:16]
            cwd.mkdir(parents=True, exist_ok=True)
        return McpStdioClient(
            command=command,
            args=args,
            env=_minimal_process_env(env_values),
            cwd=str(cwd),
            timeout=max(3, int(getattr(self.runtime.plugin_config, "personification_skill_mcp_timeout", 20) or 20)),
        )

    async def _stop_unlocked(self, installation_id: str) -> None:
        names = self._tool_names.pop(installation_id, set())
        self.registry.remove_names(names)
        client = self._clients.pop(installation_id, None)
        if client is not None:
            await client.__aexit__(None, None, None)

    async def _mark_process_exited_unlocked(self, installation_id: str) -> None:
        await self._stop_unlocked(installation_id)
        self.store.set_status(installation_id, "error", "MCP process exited unexpectedly")

    async def refresh_process_states(self) -> int:
        async with self._lock:
            secret_invalid: list[str] = []
            for installation_id in self._clients:
                item = self.store.get_installation(installation_id)
                try:
                    if item is None:
                        raise RuntimeError("managed MCP installation is unavailable")
                    self._secret_environment(item)
                except Exception:
                    secret_invalid.append(installation_id)
            for installation_id in secret_invalid:
                await self._stop_unlocked(installation_id)
                self.store.set_status(installation_id, "error", "Required MCP Secret is unavailable")
            exited = [
                installation_id
                for installation_id, client in self._clients.items()
                if not client.is_running
            ]
            for installation_id in exited:
                await self._mark_process_exited_unlocked(installation_id)
            detached = [
                item["installation_id"]
                for item in self.store.list_installations()
                if item.get("observed_status") == "running" and item["installation_id"] not in self._clients
            ]
            for installation_id in detached:
                self.registry.remove_names(self._tool_names.pop(installation_id, set()))
                self.store.set_status(installation_id, "error", "MCP process is not attached to current runtime")
            return len(secret_invalid) + len(exited) + len(detached)

    async def _call_managed_tool(
        self,
        installation_id: str,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> str:
        async with self._lock:
            item = self.store.get_installation(installation_id)
            try:
                if item is None:
                    raise RuntimeError("managed MCP installation is unavailable")
                self._secret_environment(item)
            except Exception as exc:
                await self._stop_unlocked(installation_id)
                self.store.set_status(installation_id, "error", "Required MCP Secret is unavailable")
                raise RuntimeError("managed MCP Secret is unavailable") from exc
            client = self._clients.get(installation_id)
            if client is None or not client.is_running:
                await self._mark_process_exited_unlocked(installation_id)
                raise RuntimeError("managed MCP process is unavailable")
        try:
            if installation_id == BUILTIN_SOCIAL_MCP_ID:
                status = await client.request("personification/builtin/status", {})
                platforms = status.get("platforms") if isinstance(status, dict) else {}
                raw_requested_platforms = arguments.get("platforms")
                requested_platforms = {
                    str(value or "").strip()
                    for value in raw_requested_platforms
                    if str(value or "").strip()
                } if isinstance(raw_requested_platforms, list) else set()
                single_platform = str(arguments.get("platform") or "").strip()
                if single_platform:
                    requested_platforms.add(single_platform)
                candidate_statuses = (
                    [platforms.get(name) for name in requested_platforms]
                    if isinstance(platforms, dict) and requested_platforms
                    else list(platforms.values())
                    if isinstance(platforms, dict)
                    else []
                )
                if not any(
                    isinstance(item, dict)
                    and item.get("enabled") is True
                    and item.get("state") == "ready"
                    for item in candidate_statuses
                ):
                    raise RuntimeError("builtin MCP has no ready platform")
            result = await client.call_tool(remote_name, arguments)
            if installation_id == BUILTIN_SOCIAL_MCP_ID:
                return await self._postprocess_builtin_social_result(remote_name, arguments, result)
            return result
        except BaseException:
            async with self._lock:
                if self._clients.get(installation_id) is client and not client.is_running:
                    await self._mark_process_exited_unlocked(installation_id)
            raise

    async def _activate_unlocked(self, installation_id: str) -> dict[str, int]:
        item = self.store.get_installation(installation_id)
        if item is None:
            raise KeyError(installation_id)
        await self._stop_unlocked(installation_id)
        if not item["desired_enabled"]:
            self.store.set_status(installation_id, "stopped")
            return {"added": 0, "updated": 0, "removed": 0, "total": len(self.store.tools(installation_id))}
        try:
            client = self._client(item)
        except BaseException as exc:
            self.store.set_status(installation_id, "error", type(exc).__name__)
            raise
        try:
            await client.__aenter__()
            remote_tool_list = [tool for tool in await client.list_tools() if isinstance(tool, dict)]
            if is_builtin_social_installation(item):
                exposed = {str(tool.get("name") or "") for tool in remote_tool_list}
                trusted = builtin_social_tools()
                expected = {str(tool.get("name") or "") for tool in trusted}
                if exposed != expected:
                    raise ValueError("builtin MCP tool catalog does not match the trusted manifest")
                remote_tool_list = trusted
                from .mcp_builtin_platform_store import BuiltinPlatformStore

                for platform in BuiltinPlatformStore().list():
                    await client.request(
                        "personification/builtin/configure",
                        {
                            "platform": platform["platform"],
                            "enabled": platform["enabled"],
                            "config": platform["config"],
                        },
                    )
            catalog = self.store.sync_tools(installation_id, item["name_prefix"], remote_tool_list)
            self.store.set_protocol_metadata(
                installation_id,
                protocol_version=client.protocol_version,
                server_info=client.server_info,
                capabilities=client.capabilities,
            )
            policies = self.store.tools(installation_id)
            enabled_policies = [policy for policy in policies if policy["enabled"]]
            if not enabled_policies:
                if is_builtin_social_installation(item):
                    self._clients[installation_id] = client
                    self._tool_names[installation_id] = set()
                    self.store.set_status(installation_id, "running")
                else:
                    await client.__aexit__(None, None, None)
                    self.store.set_status(installation_id, "ready")
                return catalog
            names: set[str] = set()
            registry_version, registry_snapshot = self.registry.snapshot()
            staged_tools: list[AgentTool] = []
            for policy in enabled_policies:
                registered_name = policy["registered_name"]
                existing = registry_snapshot.get(registered_name)
                if existing is not None and str((existing.metadata or {}).get("mcp_installation_id") or "") != installation_id:
                    raise ValueError(f"MCP tool name conflicts with existing tool: {registered_name}")

                async def _handler(_remote_name=policy["remote_name"], **kwargs: Any) -> str:
                    return await self._call_managed_tool(installation_id, _remote_name, kwargs)

                staged_tools.append(
                    AgentTool(
                        name=registered_name,
                        description=policy["description"],
                        parameters=policy["parameters"],
                        handler=_handler,
                        # 托管进程由当前 manager 持有，必须调用上面绑定的持久 client handler。
                        # local=False 仅保留给 legacy Skill MCP 的按次 McpBridge 路径。
                        local=True,
                        metadata={
                            "category": "mcp",
                            "source_kind": "mcp_builtin" if is_builtin_social_installation(item) else "mcp_managed",
                            "mcp_installation_id": installation_id,
                            "remote_name": policy["remote_name"],
                            "title": policy["title"],
                            "output_schema": policy["output_schema"],
                            "annotations": policy["annotations"],
                            "transport": "stdio",
                            "risk_level": policy["risk_level"],
                            "side_effect": policy["side_effect"],
                            "retryable": policy["side_effect"] == "none",
                            **(
                                {
                                    "intent_tags": ["lookup", "game_slang", "social_research"],
                                    "evidence_kind": "social_platform",
                                    "requires_network": True,
                                }
                                if is_builtin_social_installation(item)
                                else {}
                            ),
                        },
                        result_media_resolver=(
                            self._resolve_builtin_social_result_media
                            if is_builtin_social_installation(item)
                            else None
                        ),
                    )
                )
                names.add(registered_name)
            retained_tools = [tool for name, tool in registry_snapshot.items() if name not in names]
            self.registry.replace_all([*retained_tools, *staged_tools], expected_version=registry_version)
            self._clients[installation_id] = client
            self._tool_names[installation_id] = names
            self.store.set_status(installation_id, "running")
            return catalog
        except BaseException as exc:
            try:
                await client.__aexit__(None, None, None)
            finally:
                self.store.set_status(installation_id, "error", type(exc).__name__)
            raise

    async def activate(self, installation_id: str) -> None:
        async with self._lock:
            await self._activate_unlocked(installation_id)

    async def builtin_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not str(method or "").startswith("personification/builtin/"):
            raise ValueError("unsupported builtin MCP control method")
        async with self._lock:
            item = self.store.get_installation(BUILTIN_SOCIAL_MCP_ID)
            if item is None or not item.get("desired_enabled"):
                raise RuntimeError("builtin MCP is disabled")
            client = self._clients.get(BUILTIN_SOCIAL_MCP_ID)
            if client is None or not client.is_running:
                await self._activate_unlocked(BUILTIN_SOCIAL_MCP_ID)
                client = self._clients.get(BUILTIN_SOCIAL_MCP_ID)
            if client is None or not client.is_running:
                raise RuntimeError("builtin MCP process is unavailable")
        return await client.request(method, dict(params or {}))

    async def _resolve_builtin_social_result_media(self, result: str) -> list[str]:
        try:
            packet = json.loads(str(result or ""))
        except (TypeError, ValueError):
            return []
        if not isinstance(packet, dict) or str(packet.get("trust") or "") != "untrusted_data_only":
            return []
        refs: list[str] = []
        for item in list(packet.get("items") or []):
            if not isinstance(item, dict) or str(item.get("platform") or "") != "xiaoheihe":
                continue
            for ref in list(item.get("image_refs") or []):
                token = str(ref or "").strip()
                if re.fullmatch(r"cover_[0-9a-f]{40}", token) and token not in refs:
                    refs.append(token)
                if len(refs) >= 4:
                    break
            if len(refs) >= 4:
                break
        resolved: list[tuple[str, str]] = []
        for ref in refs:
            try:
                value = await self.builtin_request(
                    "personification/builtin/cover/resolve",
                    {"cover_ref": ref},
                )
            except Exception:
                continue
            platform = str(value.get("platform") or "") if isinstance(value, dict) else ""
            url = str(value.get("url") or "") if isinstance(value, dict) else ""
            if platform == "xiaoheihe" and _social_media_url_allowed(platform, url):
                resolved.append((platform, url))

        async def materialize(platform: str, url: str) -> str:
            try:
                downloaded = await download_public_image(
                    url,
                    headers={"Accept": "image/jpeg,image/png,image/webp"},
                    timeout=8,
                    connect_timeout=4,
                    max_bytes=4 * 1024 * 1024,
                    allowed_mimes=_SOCIAL_IMAGE_MIMES,
                    max_redirects=3,
                    url_validator=lambda candidate: _social_media_url_allowed(platform, candidate),
                )
            except Exception:
                return ""
            if not _social_media_url_allowed(platform, downloaded.final_url):
                return ""
            return await asyncio.to_thread(_social_image_data_url, downloaded.content)

        media = await asyncio.gather(*(materialize(platform, url) for platform, url in resolved))
        return [value for value in media if value][:4]

    async def builtin_call_tool(self, remote_name: str, arguments: dict[str, Any] | None = None) -> str:
        allowed = {str(tool.get("name") or "") for tool in builtin_social_tools()}
        if remote_name not in allowed:
            raise ValueError("unsupported builtin MCP tool")
        async with self._lock:
            item = self.store.get_installation(BUILTIN_SOCIAL_MCP_ID)
            if item is None or not item.get("desired_enabled"):
                raise RuntimeError("builtin MCP is disabled")
            client = self._clients.get(BUILTIN_SOCIAL_MCP_ID)
            if client is None or not client.is_running:
                await self._activate_unlocked(BUILTIN_SOCIAL_MCP_ID)
                client = self._clients.get(BUILTIN_SOCIAL_MCP_ID)
            if client is None or not client.is_running:
                raise RuntimeError("builtin MCP process is unavailable")
        return await client.call_tool(remote_name, dict(arguments or {}))

    async def install(
        self,
        *,
        source: dict[str, str],
        detail: dict[str, Any],
        package_index: int,
        package_digest: str,
        inputs: dict[str, Any],
        prefix: str,
        creator: str,
    ) -> dict[str, Any]:
        async with self._lock:
            raw = detail["raw"]
            packages = list(raw.get("packages") or [])
            if package_index < 0 or package_index >= len(packages) or not isinstance(packages[package_index], dict):
                raise ValueError("invalid MCP package selection")
            package = packages[package_index]
            if not package_digest or _package_digest(package) != str(package_digest):
                raise ValueError("MCP package metadata changed; refresh details and confirm again")
            plan = build_launch_plan(package, inputs)
            installation_id = uuid.uuid4().hex
            normalized_prefix = str(prefix or "").strip()
            if not normalized_prefix:
                normalized_prefix = "mcp_" + hashlib.sha256(detail["server"]["name"].encode("utf-8")).hexdigest()[:8] + "_"
            if not _SAFE_PREFIX.fullmatch(normalized_prefix):
                raise ValueError("MCP tool prefix must be 2-32 lowercase letters, numbers or underscores")
            self.secrets.set(installation_id, plan["secrets"])
            item = {
                "installation_id": installation_id,
                "source_id": source["id"],
                "source_url": source["url"],
                "server_name": detail["server"]["name"],
                "server_title": detail["server"]["title"],
                "server_version": plan["version"],
                "package_type": plan["package_type"],
                "package_identifier": plan["package_identifier"],
                "command": plan["command"],
                "args": plan["args"],
                "env": plan["env"],
                "secret_names": plan["secret_names"],
                "name_prefix": normalized_prefix,
                "metadata": {
                    "repository": detail["server"].get("repository", {}),
                    "website": detail["server"].get("website", ""),
                    "schema": detail["server"].get("schema", ""),
                    "status": detail["server"].get("status", "unknown"),
                    "fileSha256": str(package.get("fileSha256") or ""),
                    "package_digest": package_digest,
                    "warning": detail.get("warning", ""),
                },
                "created_by": creator,
            }
            try:
                client = self._client({**item, "desired_enabled": True})
                try:
                    await client.__aenter__()
                    remote_tools = await client.list_tools()
                    protocol_metadata = {
                        "protocol_version": client.protocol_version,
                        "server_info": client.server_info,
                        "capabilities": client.capabilities,
                    }
                finally:
                    await client.__aexit__(None, None, None)
                tools = [_tool_policy(installation_id, normalized_prefix, tool) for tool in remote_tools]
                if not tools:
                    raise ValueError("MCP server exposed no tools")
                if len({tool["registered_name"] for tool in tools}) != len(tools):
                    raise ValueError("MCP tool names collide after normalization")
                item["metadata"].update(protocol_metadata)
                self.store.save_installation(item, tools)
            except Exception:
                self.secrets.delete(installation_id)
                raise
            return self.public_installation(installation_id) or {}

    def public_installation(self, installation_id: str) -> dict[str, Any] | None:
        item = self.store.get_installation(installation_id)
        if item is None:
            return None
        item["tools"] = self.store.tools(installation_id)
        secret_store_readable = True
        try:
            configured_secrets = self.secrets.get(installation_id)
        except Exception:
            configured_secrets = {}
            secret_store_readable = False
        secret_names = list(item.get("secret_names") or [])
        secrets_ready = all(
            bool(configured_secrets.get(name)) for name in item.get("secret_names") or []
        )
        item["secrets_required"] = bool(secret_names)
        item["secrets_configured"] = bool(secret_names) and secrets_ready
        item["secrets_ready"] = secrets_ready
        item["secret_store_readable"] = secret_store_readable
        client = self._clients.get(installation_id)
        process_running = client is not None and client.is_running
        registered_names = self._tool_names.get(installation_id, set())
        authorized_count = registered_count = effective_count = 0
        for tool in item["tools"]:
            authorized = bool(tool.get("enabled"))
            registered_tool = self.registry.get(str(tool.get("registered_name") or ""))
            registered = (
                str(tool.get("registered_name") or "") in registered_names
                and registered_tool is not None
                and str((registered_tool.metadata or {}).get("mcp_installation_id") or "") == installation_id
            )
            effective = bool(
                item["desired_enabled"]
                and secrets_ready
                and process_running
                and authorized
                and registered
            )
            tool["authorized"] = authorized
            tool["registered"] = registered
            tool["effective"] = effective
            authorized_count += int(authorized)
            registered_count += int(registered)
            effective_count += int(effective)
        observed_status = str(item.get("observed_status") or "stopped")
        item["process_state"] = "exited" if client is not None and not process_running else observed_status
        item["run_allowed"] = bool(item["desired_enabled"] and secrets_ready and authorized_count > 0)
        item["authorized_count"] = authorized_count
        item["registered_count"] = registered_count
        item["effective_count"] = effective_count
        item["tool_count"] = len(item["tools"])
        item["builtin"] = is_builtin_social_installation(item)
        item["deletable"] = not item["builtin"]
        item.pop("env", None)
        item.pop("args", None)
        return item

    def list_public(self) -> list[dict[str, Any]]:
        result = []
        for stored in self.store.list_installations():
            item = self.public_installation(stored["installation_id"])
            if item is not None:
                result.append(item)
        return result

    async def reload(self) -> dict[str, int]:
        async with self._lock:
            self.store.ensure_builtin_social()
            for installation_id in list(self._clients):
                await self._stop_unlocked(installation_id)
            running = ready = failed = 0
            catalog_added = catalog_updated = catalog_removed = 0
            for item in self.store.list_installations():
                if not item["desired_enabled"]:
                    continue
                try:
                    catalog = await self._activate_unlocked(item["installation_id"])
                    catalog_added += int(catalog.get("added") or 0)
                    catalog_updated += int(catalog.get("updated") or 0)
                    catalog_removed += int(catalog.get("removed") or 0)
                    if item["installation_id"] in self._clients:
                        running += 1
                    else:
                        ready += 1
                except Exception:
                    failed += 1
            return {
                "running": running,
                "ready": ready,
                "failed": failed,
                "catalog_added": catalog_added,
                "catalog_updated": catalog_updated,
                "catalog_removed": catalog_removed,
            }

    async def toggle_installation(self, installation_id: str, enabled: bool) -> dict[str, Any]:
        async with self._lock:
            self.store.set_installation_enabled(installation_id, enabled)
            await self._activate_unlocked(installation_id)
            return self.public_installation(installation_id) or {}

    async def toggle_tool(
        self,
        installation_id: str,
        remote_name: str,
        enabled: bool,
        *,
        approve_side_effect: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            self.store.set_tool_enabled(
                installation_id,
                remote_name,
                enabled,
                approve_side_effect=approve_side_effect,
                trusted_read_only=is_builtin_social_installation(installation_id),
            )
            await self._activate_unlocked(installation_id)
            return self.public_installation(installation_id) or {}

    async def delete(self, installation_id: str) -> None:
        async with self._lock:
            if is_builtin_social_installation(installation_id):
                raise ValueError("builtin MCP installation cannot be deleted")
            await self._stop_unlocked(installation_id)
            self.store.delete(installation_id)
            self.secrets.delete(installation_id)

    async def shutdown(self) -> None:
        if self._slang_discovery_queue is not None:
            await self._slang_discovery_queue.close()
            self._slang_discovery_queue = None
        async with self._lock:
            for installation_id in list(self._clients):
                await self._stop_unlocked(installation_id)
                self.store.set_status(installation_id, "stopped")


_MANAGERS: dict[int, McpRuntimeManager] = {}


def get_mcp_manager(runtime: Any) -> McpRuntimeManager:
    bundle = getattr(runtime, "runtime_bundle", None)
    registry = getattr(bundle, "tool_registry", None) if bundle is not None else None
    if registry is None:
        raise RuntimeError("tool registry is unavailable")
    key = id(registry)
    manager = _MANAGERS.get(key)
    if manager is None or manager.registry is not registry:
        manager = McpRuntimeManager(runtime, registry)
        _MANAGERS[key] = manager
    else:
        manager.runtime = runtime
    return manager


async def shutdown_mcp_managers() -> None:
    for manager in list(_MANAGERS.values()):
        await manager.shutdown()
    _MANAGERS.clear()


__all__ = [
    "McpRegistryClient",
    "McpRuntimeManager",
    "McpSecretStore",
    "McpStore",
    "build_launch_plan",
    "get_mcp_manager",
    "mcp_registry_sources",
    "resolve_registry_source",
    "shutdown_mcp_managers",
]
