from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..skill_runtime.mcp_compat import McpProtocolError, McpStdioClient
from .consumer_web_coordinator import consumer_web_coordinator
from .paths import get_data_dir
from .safe_media_download import SafeMediaDownloadError, download_public_media_to_path


_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".amr"}
_VIDEO_MIMES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
    "video/x-msvideo",
    "application/octet-stream",
}
_AUDIO_MIMES = {
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/opus",
    "audio/flac",
    "audio/x-flac",
    "audio/amr",
    "application/octet-stream",
}
_VIDEO_DEFAULT_MAX_BYTES = 512 * 1024 * 1024
_AUDIO_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_UNTRUSTED_LABELS = {
    "video": "GEMINI_WEB_VIDEO_OBSERVATION",
    "audio": "GEMINI_WEB_AUDIO_OBSERVATION",
}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(maximum, max(minimum, number))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(maximum, max(minimum, number))


def _minimal_helper_env(root: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "PLAYWRIGHT_BROWSERS_PATH",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PERSONIFICATION_GEMINI_WEB_ROOT": str(root),
        }
    )
    return env


class GeminiWebService:
    def __init__(self, data_dir: Path) -> None:
        data_root = Path(data_dir).resolve()
        self._legacy_qwen_removed = self._remove_legacy_qwen_web_data(data_root)
        self.root = (data_root / "gemini_web").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root = (self.root / "staging").resolve()
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._client: McpStdioClient | None = None
        self._client_lock = asyncio.Lock()
        self._last_status: dict[str, Any] = {}
        consumer_web_coordinator.register(
            "gemini",
            close=self._close_client_for_switch,
            protected=self._switch_protected,
        )

    @staticmethod
    def _remove_legacy_qwen_web_data(data_root: Path) -> bool:
        marker = (data_root / ".qwen_web_removed").resolve()
        if marker.parent != data_root or marker.exists():
            return False
        legacy = (data_root / "qwen_web").resolve()
        if legacy.parent != data_root or legacy.name != "qwen_web":
            return False
        removed = False
        if legacy.exists() and legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)
            removed = not legacy.exists()
            if not removed:
                return False
        try:
            marker.write_text("qwen_web_removed\n", encoding="utf-8")
        except OSError:
            pass
        return removed

    @staticmethod
    def enabled(config: Any) -> bool:
        return bool(getattr(config, "personification_gemini_web_enabled", False))

    @staticmethod
    def risk_acknowledged(config: Any) -> bool:
        return bool(getattr(config, "personification_gemini_web_risk_acknowledged", False))

    def _profile_present(self) -> bool:
        profile = self.root / "profiles" / "gemini_web"
        if not profile.exists():
            return False
        try:
            return any(profile.iterdir())
        except OSError:
            return False

    async def _ensure_client(self, config: Any) -> McpStdioClient:
        async with self._client_lock:
            client = self._client
            if client is not None and client.is_running:
                return client
            if client is not None:
                with suppress(Exception):
                    await client.__aexit__(None, None, None)
            entrypoint = Path(__file__).resolve().with_name("gemini_web_entrypoint.py")
            project_root = Path(__file__).resolve().parents[2]
            client = McpStdioClient(
                command=sys.executable,
                args=[str(entrypoint)],
                env=_minimal_helper_env(self.root),
                cwd=str(project_root),
                timeout=900,
            )
            await client.__aenter__()
            self._client = client
            await client.request(
                "personification/gemini-web/configure",
                {
                    "idle_timeout_seconds": _bounded_float(
                        getattr(config, "personification_gemini_web_idle_timeout", 300.0),
                        300.0,
                        60.0,
                        1800.0,
                    )
                },
            )
            return client

    def _switch_protected(self) -> bool:
        snapshot = consumer_web_coordinator.snapshot("gemini")
        interactive = self._last_status.get("interactive_session")
        return bool(snapshot.get("active") or interactive)

    async def _close_client_for_switch(self) -> None:
        async with self._client_lock:
            client = self._client
            self._client = None
            if client is not None:
                with suppress(Exception):
                    await client.__aexit__(None, None, None)

    @asynccontextmanager
    async def _admit(self):
        try:
            async with consumer_web_coordinator.admit("gemini"):
                yield
        except RuntimeError as exc:
            if str(exc or "") == "consumer_web_busy":
                raise RuntimeError("gemini_web_busy") from exc
            raise

    def local_status(self, config: Any) -> dict[str, Any]:
        coordinator = consumer_web_coordinator.snapshot("gemini")
        enabled = self.enabled(config)
        acknowledged = self.risk_acknowledged(config)
        if not enabled:
            state = "disabled"
            code = "gemini_web_disabled"
        elif not acknowledged:
            state = "disabled"
            code = "gemini_web_risk_ack_required"
        else:
            state = str(self._last_status.get("state") or "login_required")
            code = str(self._last_status.get("last_diagnostic_code") or "")
        result = {
            "schema_version": 1,
            "enabled": enabled,
            "risk_acknowledged": acknowledged,
            "state": "busy" if coordinator.get("active") else state,
            "profile_present": self._profile_present(),
            "browser_running": bool(self._client is not None and self._client.is_running),
            "active_job": bool(coordinator.get("active")),
            "waiting_jobs": int(coordinator.get("waiting") or 0),
            "interactive_session": None,
            "last_diagnostic_code": code,
            "last_probe_at": 0.0,
            "page_contract_version": "gemini_web_v1",
            "migration_diagnostic": "qwen_web_removed" if self._legacy_qwen_removed else "",
        }
        result.update(
            {
                key: value
                for key, value in self._last_status.items()
                if key
                in {
                    "interactive_session",
                    "last_probe_at",
                    "page_contract_version",
                    "risk_cooldown_seconds",
                    "diagnostics",
                }
            }
        )
        return result

    async def status(self, config: Any, *, refresh: bool = False) -> dict[str, Any]:
        coordinator = consumer_web_coordinator.snapshot("gemini")
        if not refresh or coordinator.get("active"):
            return self.local_status(config)
        if not self.enabled(config) or not self.risk_acknowledged(config):
            return self.local_status(config)
        try:
            await consumer_web_coordinator.activate("gemini")
            client = await self._ensure_client(config)
            self._last_status = await client.request("personification/gemini-web/status", {})
        except Exception:
            self._last_status = {
                "state": "unavailable",
                "last_diagnostic_code": "gemini_web_process_failed",
            }
        return self.local_status(config)

    async def _control(
        self,
        config: Any,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        require_enabled: bool = True,
        require_acknowledgement: bool = True,
    ) -> dict[str, Any]:
        if require_enabled and not self.enabled(config):
            raise RuntimeError("gemini_web_disabled")
        if require_acknowledgement and not self.risk_acknowledged(config):
            raise RuntimeError("gemini_web_risk_ack_required")
        try:
            await consumer_web_coordinator.activate("gemini")
        except RuntimeError as exc:
            if str(exc or "") == "consumer_web_busy":
                raise RuntimeError("gemini_web_busy") from exc
            raise
        client = await self._ensure_client(config)
        result = await client.request(method, dict(params or {}))
        if method.endswith(("/status", "/probe", "/logout", "/configure")):
            self._last_status = dict(result)
        return result

    async def probe(self, config: Any) -> dict[str, Any]:
        await self._control(config, "personification/gemini-web/probe")
        return self.local_status(config)

    async def auth_start(self, config: Any, owner: str) -> dict[str, Any]:
        result = await self._control(
            config,
            "personification/gemini-web/auth/start",
            {"owner": str(owner or "")},
        )
        if result.get("session_id"):
            self._last_status = {**self._last_status, "interactive_session": result}
        else:
            self._last_status = {
                **self._last_status,
                "state": "manual_verification_required",
                "last_diagnostic_code": str(result.get("error_code") or "gemini_web_process_failed"),
                "risk_cooldown_seconds": max(0, int(result.get("remaining_seconds") or 0)),
                "interactive_session": None,
            }
        return result

    async def auth_status(self, config: Any, session_id: str, owner: str) -> dict[str, Any]:
        result = await self._control(
            config,
            "personification/gemini-web/auth/status",
            {"session_id": session_id, "owner": owner},
        )
        self._last_status = {
            **self._last_status,
            "interactive_session": None
            if result.get("status") in {"success", "expired", "cancelled", "error"}
            else result,
        }
        return result

    async def auth_frame(
        self,
        config: Any,
        session_id: str,
        owner: str,
        *,
        after_revision: int = 0,
    ) -> dict[str, Any]:
        return await self._control(
            config,
            "personification/gemini-web/auth/frame",
            {"session_id": session_id, "owner": owner, "after_revision": after_revision},
        )

    async def auth_input(
        self,
        config: Any,
        session_id: str,
        owner: str,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._control(
            config,
            "personification/gemini-web/auth/input",
            {"session_id": session_id, "owner": owner, "action": dict(action)},
        )

    async def auth_finish(self, config: Any, session_id: str, owner: str) -> dict[str, Any]:
        result = await self._control(
            config,
            "personification/gemini-web/auth/finish",
            {"session_id": session_id, "owner": owner},
        )
        self._last_status = {
            **self._last_status,
            "state": "ready" if result.get("status") == "success" else result.get("status", "login_required"),
            "last_diagnostic_code": str(result.get("error_code") or ""),
            "interactive_session": None if result.get("status") == "success" else result,
        }
        return result

    async def auth_cancel(self, config: Any, session_id: str, owner: str) -> dict[str, Any]:
        result = await self._control(
            config,
            "personification/gemini-web/auth/cancel",
            {"session_id": session_id, "owner": owner},
            require_enabled=False,
            require_acknowledgement=False,
        )
        self._last_status = {**self._last_status, "interactive_session": None}
        return result

    async def logout(self, config: Any) -> dict[str, Any]:
        result = await self._control(
            config,
            "personification/gemini-web/logout",
            require_enabled=False,
            require_acknowledgement=False,
        )
        self._last_status = dict(result)
        return self.local_status(config)

    def _limits(self, config: Any, kind: str) -> tuple[int, set[str], set[str]]:
        if kind == "video":
            return (
                _bounded_int(
                    getattr(config, "personification_gemini_web_video_max_bytes", _VIDEO_DEFAULT_MAX_BYTES),
                    _VIDEO_DEFAULT_MAX_BYTES,
                    8 * 1024 * 1024,
                    2 * 1024 * 1024 * 1024,
                ),
                _VIDEO_EXTENSIONS,
                _VIDEO_MIMES,
            )
        return (
            _bounded_int(
                getattr(config, "personification_gemini_web_audio_max_bytes", _AUDIO_DEFAULT_MAX_BYTES),
                _AUDIO_DEFAULT_MAX_BYTES,
                64 * 1024,
                512 * 1024 * 1024,
            ),
            _AUDIO_EXTENSIONS,
            _AUDIO_MIMES,
        )

    async def _stage_media(self, config: Any, kind: str, media_ref: str) -> tuple[str, Path]:
        max_bytes, extensions, mimes = self._limits(config, kind)
        token = f"job_{uuid.uuid4().hex}"
        directory = (self.staging_root / token).resolve()
        if not directory.is_relative_to(self.staging_root):
            raise RuntimeError("gemini_web_media_token_invalid")
        directory.mkdir(parents=True, exist_ok=False)
        raw = str(media_ref or "").strip()
        try:
            if raw.startswith(("http://", "https://")):
                parsed = urlsplit(raw)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("gemini_web_media_ref_invalid")
                suffix = Path(parsed.path).suffix.lower()
                if suffix not in extensions:
                    suffix = ".mp4" if kind == "video" else ".m4a"
                target = directory / f"media{suffix}"
                try:
                    await download_public_media_to_path(
                        raw,
                        target,
                        timeout=_bounded_float(
                            getattr(config, "personification_video_download_timeout", 90.0),
                            90.0,
                            8.0,
                            180.0,
                        ),
                        max_bytes=max_bytes,
                        allowed_mimes=mimes,
                    )
                except SafeMediaDownloadError as exc:
                    raise ValueError("gemini_web_media_download_failed") from exc
                return token, directory
            if raw.startswith("file://"):
                raw = raw[7:]
            source = Path(raw)
            if not source.is_absolute():
                raise ValueError("gemini_web_media_ref_invalid")
            source = source.resolve(strict=True)
            if not source.is_file() or source.suffix.lower() not in extensions:
                raise ValueError("gemini_web_media_ref_invalid")
            if source.stat().st_size > max_bytes:
                raise ValueError("gemini_web_media_too_large")
            target = directory / f"media{source.suffix.lower()}"
            await asyncio.to_thread(shutil.copyfile, source, target)
            return token, directory
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    async def analyze(
        self,
        *,
        config: Any,
        kind: str,
        media_ref: str,
        prompt: str,
    ) -> tuple[str, dict[str, Any]]:
        if not self.enabled(config):
            return "", {"status": "skipped", "diagnostic_code": "gemini_web_disabled", "elapsed_ms": 0}
        if not self.risk_acknowledged(config):
            return "", {
                "status": "skipped",
                "diagnostic_code": "gemini_web_risk_ack_required",
                "elapsed_ms": 0,
            }
        token = ""
        directory: Path | None = None
        try:
            async with self._admit():
                token, directory = await self._stage_media(config, kind, media_ref)
                client = await self._ensure_client(config)
                result = await client.request(
                    "personification/gemini-web/analyze",
                    {
                        "media_token": token,
                        "kind": kind,
                        "prompt": str(prompt or "")[:4000],
                        "timeout_seconds": _bounded_float(
                            getattr(config, "personification_gemini_web_job_timeout", 600.0),
                            600.0,
                            20.0,
                            900.0,
                        ),
                        "output_max_chars": _bounded_int(
                            getattr(config, "personification_gemini_web_output_max_chars", 20000),
                            20000,
                            1000,
                            50000,
                        ),
                    },
                )
                code = str(result.get("diagnostic_code") or "")
                text = str(result.get("text") or "").strip()
                if result.get("status") != "ok" or not text:
                    self._last_status = {
                        **self._last_status,
                        "state": (
                            "manual_verification_required"
                            if code
                            in {
                                "gemini_web_manual_verification_required",
                                "gemini_web_network_risk_detected",
                                "gemini_web_network_risk_cooldown",
                            }
                            else "login_required"
                            if code == "gemini_web_login_required"
                            else "dom_changed"
                            if code == "gemini_web_dom_changed"
                            else self._last_status.get("state", "unavailable")
                        ),
                        "last_diagnostic_code": code,
                    }
                    return "", dict(result)
                label = _UNTRUSTED_LABELS[kind]
                wrapped = f"[UNTRUSTED_DATA_ONLY: {label}]\n{text}\n[/UNTRUSTED_DATA_ONLY]"
                self._last_status = {**self._last_status, "state": "ready", "last_diagnostic_code": ""}
                return wrapped, dict(result)
        except RuntimeError as exc:
            code = str(exc or "")
            if code != "gemini_web_busy":
                code = "gemini_web_process_failed"
            return "", {"status": "failed", "diagnostic_code": code, "elapsed_ms": 0}
        except (McpProtocolError, ValueError) as exc:
            raw = str(exc or "")
            code = raw if raw.startswith("gemini_web_") else "gemini_web_process_failed"
            return "", {"status": "failed", "diagnostic_code": code, "elapsed_ms": 0}
        except Exception:
            return "", {"status": "failed", "diagnostic_code": "gemini_web_process_failed", "elapsed_ms": 0}
        finally:
            if directory is not None:
                await asyncio.to_thread(shutil.rmtree, directory, True)

    async def shutdown(self) -> None:
        await self._close_client_for_switch()


_SERVICES: dict[str, GeminiWebService] = {}


def get_gemini_web_service(runtime_or_config: Any) -> GeminiWebService:
    config = getattr(runtime_or_config, "plugin_config", runtime_or_config)
    data_dir = Path(get_data_dir(config)).resolve()
    key = str(data_dir)
    service = _SERVICES.get(key)
    if service is None:
        service = GeminiWebService(data_dir)
        _SERVICES[key] = service
    return service


async def shutdown_gemini_web_services() -> None:
    services = list(_SERVICES.values())
    _SERVICES.clear()
    if services:
        await asyncio.gather(*(service.shutdown() for service in services), return_exceptions=True)


__all__ = ["GeminiWebService", "get_gemini_web_service", "shutdown_gemini_web_services"]
