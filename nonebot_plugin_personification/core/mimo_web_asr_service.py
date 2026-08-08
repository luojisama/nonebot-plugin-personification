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


_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".amr"}
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
            "PERSONIFICATION_MIMO_WEB_ASR_ROOT": str(root),
        }
    )
    return env


class MiMoWebAsrService:
    def __init__(self, data_dir: Path) -> None:
        data_root = Path(data_dir).resolve()
        self.root = (data_root / "mimo_web_asr").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root = (self.root / "staging").resolve()
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._client: McpStdioClient | None = None
        self._client_lock = asyncio.Lock()
        self._last_status: dict[str, Any] = {}
        consumer_web_coordinator.register(
            "mimo_asr",
            close=self._close_client_for_switch,
            protected=self._switch_protected,
        )

    @staticmethod
    def enabled(config: Any) -> bool:
        return bool(getattr(config, "personification_mimo_web_asr_enabled", False))

    @staticmethod
    def risk_acknowledged(config: Any) -> bool:
        return bool(getattr(config, "personification_mimo_web_asr_risk_acknowledged", False))

    def _profile_present(self) -> bool:
        profile = self.root / "profiles" / "mimo_asr_web"
        try:
            return profile.exists() and any(profile.iterdir())
        except OSError:
            return False

    def _switch_protected(self) -> bool:
        snapshot = consumer_web_coordinator.snapshot("mimo_asr")
        return bool(snapshot.get("active") or self._last_status.get("interactive_session"))

    async def _close_client_for_switch(self) -> None:
        async with self._client_lock:
            client = self._client
            self._client = None
            if client is not None:
                with suppress(Exception):
                    await client.__aexit__(None, None, None)

    async def _ensure_client(self, config: Any) -> McpStdioClient:
        async with self._client_lock:
            client = self._client
            if client is not None and client.is_running:
                return client
            if client is not None:
                with suppress(Exception):
                    await client.__aexit__(None, None, None)
            entrypoint = Path(__file__).resolve().with_name("mimo_web_asr_entrypoint.py")
            project_root = Path(__file__).resolve().parents[2]
            client = McpStdioClient(
                command=sys.executable,
                args=[str(entrypoint)],
                env=_minimal_helper_env(self.root),
                cwd=str(project_root),
                timeout=600,
            )
            await client.__aenter__()
            self._client = client
            await client.request(
                "personification/mimo-web-asr/configure",
                {
                    "idle_timeout_seconds": _bounded_float(
                        getattr(config, "personification_mimo_web_asr_idle_timeout", 300),
                        300,
                        60,
                        1800,
                    )
                },
            )
            return client

    @asynccontextmanager
    async def _admit(self):
        try:
            async with consumer_web_coordinator.admit("mimo_asr"):
                yield
        except RuntimeError as exc:
            if str(exc or "") == "consumer_web_busy":
                raise RuntimeError("mimo_web_asr_busy") from exc
            raise

    def local_status(self, config: Any) -> dict[str, Any]:
        enabled = self.enabled(config)
        acknowledged = self.risk_acknowledged(config)
        coordinator = consumer_web_coordinator.snapshot("mimo_asr")
        if not enabled:
            state, code = "disabled", "mimo_web_asr_disabled"
        elif not acknowledged:
            state, code = "disabled", "mimo_web_asr_risk_ack_required"
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
            "page_contract_version": "mimo_studio_asr_v1",
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

    async def _activate(self) -> None:
        try:
            await consumer_web_coordinator.activate("mimo_asr")
        except RuntimeError as exc:
            if str(exc or "") == "consumer_web_busy":
                raise RuntimeError("mimo_web_asr_busy") from exc
            raise

    async def status(self, config: Any, *, refresh: bool = False) -> dict[str, Any]:
        coordinator = consumer_web_coordinator.snapshot("mimo_asr")
        if not refresh or coordinator.get("active"):
            return self.local_status(config)
        if not self.enabled(config) or not self.risk_acknowledged(config):
            return self.local_status(config)
        try:
            await self._activate()
            client = await self._ensure_client(config)
            self._last_status = await client.request("personification/mimo-web-asr/status", {})
        except Exception:
            self._last_status = {"state": "unavailable", "last_diagnostic_code": "mimo_web_asr_process_failed"}
        return self.local_status(config)

    async def _control(
        self,
        config: Any,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        require_enabled: bool = True,
        require_acknowledgement: bool = True,
    ) -> dict[str, Any]:
        if require_enabled and not self.enabled(config):
            raise RuntimeError("mimo_web_asr_disabled")
        if require_acknowledgement and not self.risk_acknowledged(config):
            raise RuntimeError("mimo_web_asr_risk_ack_required")
        await self._activate()
        client = await self._ensure_client(config)
        result = await client.request(f"personification/mimo-web-asr/{action}", dict(params or {}))
        if action in {"status", "probe", "logout", "configure"}:
            self._last_status = dict(result)
        return result

    async def probe(self, config: Any) -> dict[str, Any]:
        await self._control(config, "probe")
        return self.local_status(config)

    async def auth_start(self, config: Any, owner: str) -> dict[str, Any]:
        result = await self._control(config, "auth/start", {"owner": owner})
        self._last_status = {
            **self._last_status,
            "interactive_session": result if result.get("session_id") else None,
            "last_diagnostic_code": str(result.get("error_code") or ""),
        }
        return result

    async def auth_status(self, config: Any, session_id: str, owner: str) -> dict[str, Any]:
        result = await self._control(config, "auth/status", {"session_id": session_id, "owner": owner})
        self._last_status = {**self._last_status, "interactive_session": result}
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
            "auth/frame",
            {"session_id": session_id, "owner": owner, "after_revision": after_revision},
        )

    async def auth_input(self, config: Any, session_id: str, owner: str, action: dict[str, Any]) -> dict[str, Any]:
        return await self._control(
            config,
            "auth/input",
            {"session_id": session_id, "owner": owner, "action": dict(action)},
        )

    async def auth_finish(self, config: Any, session_id: str, owner: str) -> dict[str, Any]:
        result = await self._control(config, "auth/finish", {"session_id": session_id, "owner": owner})
        self._last_status = {
            **self._last_status,
            "state": "ready" if result.get("status") == "success" else str(result.get("status") or "login_required"),
            "interactive_session": None if result.get("status") == "success" else result,
            "last_diagnostic_code": str(result.get("error_code") or ""),
        }
        return result

    async def auth_cancel(self, config: Any, session_id: str, owner: str) -> dict[str, Any]:
        result = await self._control(
            config,
            "auth/cancel",
            {"session_id": session_id, "owner": owner},
            require_enabled=False,
            require_acknowledgement=False,
        )
        self._last_status = {**self._last_status, "interactive_session": None}
        return result

    async def logout(self, config: Any) -> dict[str, Any]:
        result = await self._control(
            config,
            "logout",
            require_enabled=False,
            require_acknowledgement=False,
        )
        self._last_status = dict(result)
        return self.local_status(config)

    async def _stage_audio(self, config: Any, media_ref: str) -> tuple[str, Path]:
        max_bytes = _bounded_int(
            getattr(config, "personification_mimo_web_asr_audio_max_bytes", 64 * 1024 * 1024),
            64 * 1024 * 1024,
            64 * 1024,
            512 * 1024 * 1024,
        )
        token = f"job_{uuid.uuid4().hex}"
        directory = (self.staging_root / token).resolve()
        if not directory.is_relative_to(self.staging_root):
            raise RuntimeError("mimo_web_asr_media_token_invalid")
        directory.mkdir(parents=True, exist_ok=False)
        raw = str(media_ref or "").strip()
        try:
            if raw.startswith(("http://", "https://")):
                parsed = urlsplit(raw)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("mimo_web_asr_media_ref_invalid")
                suffix = Path(parsed.path).suffix.lower()
                if suffix not in _AUDIO_EXTENSIONS:
                    suffix = ".m4a"
                try:
                    await download_public_media_to_path(
                        raw,
                        directory / f"audio{suffix}",
                        max_bytes=max_bytes,
                        allowed_mimes=_AUDIO_MIMES,
                    )
                except SafeMediaDownloadError as exc:
                    raise ValueError("mimo_web_asr_media_download_failed") from exc
                return token, directory
            if raw.startswith("file://"):
                raw = raw[7:]
            source = Path(raw)
            if not source.is_absolute():
                raise ValueError("mimo_web_asr_media_ref_invalid")
            source = source.resolve(strict=True)
            if not source.is_file() or source.suffix.lower() not in _AUDIO_EXTENSIONS:
                raise ValueError("mimo_web_asr_media_ref_invalid")
            if source.stat().st_size > max_bytes:
                raise ValueError("mimo_web_asr_media_too_large")
            await asyncio.to_thread(shutil.copyfile, source, directory / f"audio{source.suffix.lower()}")
            return token, directory
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    async def transcribe(
        self,
        *,
        config: Any,
        media_ref: str,
        prompt: str,
    ) -> tuple[str, dict[str, Any]]:
        if not self.enabled(config):
            return "", {"status": "skipped", "diagnostic_code": "mimo_web_asr_disabled", "elapsed_ms": 0}
        if not self.risk_acknowledged(config):
            return "", {"status": "skipped", "diagnostic_code": "mimo_web_asr_risk_ack_required", "elapsed_ms": 0}
        directory: Path | None = None
        try:
            async with self._admit():
                token, directory = await self._stage_audio(config, media_ref)
                client = await self._ensure_client(config)
                result = await client.request(
                    "personification/mimo-web-asr/analyze",
                    {
                        "media_token": token,
                        "prompt": str(prompt or "")[:4000],
                        "timeout_seconds": _bounded_float(
                            getattr(config, "personification_mimo_web_asr_job_timeout", 300),
                            300,
                            20,
                            600,
                        ),
                        "output_max_chars": _bounded_int(
                            getattr(config, "personification_mimo_web_asr_output_max_chars", 20000),
                            20000,
                            1000,
                            50000,
                        ),
                    },
                )
                code = str(result.get("diagnostic_code") or "")
                text = str(result.get("text") or "").strip()
                if result.get("status") != "ok" or not text:
                    self._last_status = {**self._last_status, "last_diagnostic_code": code}
                    return "", dict(result)
                wrapped = (
                    "[UNTRUSTED_DATA_ONLY: MIMO_WEB_ASR_TRANSCRIPT]\n"
                    f"{text}\n"
                    "[/UNTRUSTED_DATA_ONLY]"
                )
                self._last_status = {**self._last_status, "state": "ready", "last_diagnostic_code": ""}
                return wrapped, dict(result)
        except RuntimeError as exc:
            raw = str(exc or "")
            code = raw if raw.startswith("mimo_web_asr_") else "mimo_web_asr_process_failed"
            return "", {"status": "failed", "diagnostic_code": code, "elapsed_ms": 0}
        except (McpProtocolError, ValueError) as exc:
            raw = str(exc or "")
            code = raw if raw.startswith("mimo_web_asr_") else "mimo_web_asr_process_failed"
            return "", {"status": "failed", "diagnostic_code": code, "elapsed_ms": 0}
        except Exception:
            return "", {"status": "failed", "diagnostic_code": "mimo_web_asr_process_failed", "elapsed_ms": 0}
        finally:
            if directory is not None:
                await asyncio.to_thread(shutil.rmtree, directory, True)

    async def shutdown(self) -> None:
        await self._close_client_for_switch()


_SERVICES: dict[str, MiMoWebAsrService] = {}


def get_mimo_web_asr_service(runtime_or_config: Any) -> MiMoWebAsrService:
    config = getattr(runtime_or_config, "plugin_config", runtime_or_config)
    data_dir = Path(get_data_dir(config)).resolve()
    key = str(data_dir)
    service = _SERVICES.get(key)
    if service is None:
        service = MiMoWebAsrService(data_dir)
        _SERVICES[key] = service
    return service


async def shutdown_mimo_web_asr_services() -> None:
    services = list(_SERVICES.values())
    _SERVICES.clear()
    if services:
        await asyncio.gather(*(service.shutdown() for service in services), return_exceptions=True)


__all__ = ["MiMoWebAsrService", "get_mimo_web_asr_service", "shutdown_mimo_web_asr_services"]
