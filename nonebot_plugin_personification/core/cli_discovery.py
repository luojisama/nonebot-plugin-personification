"""Local, read-only discovery for supported model CLIs.

This module deliberately does not update packages, open a browser, or expose
credential contents.  It is safe to call from the WebUI and can be disabled
without affecting the normal provider router.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any


_CLI_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("codex", "codex_cli", ("codex", "codex.cmd", "codex.ps1")),
    ("agy", "antigravity_cli", ("agy", "agy.exe")),
    ("gemini", "gemini_cli", ("gemini", "gemini.cmd", "gemini.ps1")),
    ("claude", "claude_code", ("claude", "claude.cmd", "claude.ps1")),
    ("opencode", "opencode_cli", ("opencode", "opencode.cmd", "opencode.ps1")),
)


def _candidate_executable(names: tuple[str, ...]) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _configured_path(plugin_config: Any, field_name: str) -> Path | None:
    value = str(getattr(plugin_config, field_name, "") or "").strip() if plugin_config else ""
    return Path(value).expanduser() if value else None


def _environment_path(name: str) -> Path | None:
    value = str(os.environ.get(name, "") or "").strip()
    return Path(value).expanduser() if value else None


def _candidate_paths(provider_type: str, plugin_config: Any = None) -> tuple[Path, ...]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", str(home / "AppData/Roaming")))
    local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local")))
    if provider_type == "codex_cli":
        return tuple(path for path in (
            _configured_path(plugin_config, "personification_codex_auth_path"),
            (_environment_path("CHATGPT_LOCAL_HOME") / "auth.json") if _environment_path("CHATGPT_LOCAL_HOME") else None,
            (_environment_path("CODEX_HOME") / "auth.json") if _environment_path("CODEX_HOME") else None,
            home / ".chatgpt-local" / "auth.json",
            home / ".codex" / "auth.json",
        ) if path is not None)
    if provider_type == "antigravity_cli":
        return tuple(path for path in (
            _configured_path(plugin_config, "personification_antigravity_cli_auth_path"),
            _environment_path("ANTIGRAVITY_CLI_AUTH_PATH"),
            _environment_path("AGY_AUTH_PATH"),
            home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
            home / ".gemini" / "antigravity-cli" / "oauth_creds.json",
            home / ".gemini" / "antigravity-cli" / "auth.json",
            home / ".gemini" / "antigravity-cli" / "credentials.json",
            appdata / "gemini" / "antigravity-cli" / "oauth_creds.json",
        ) if path is not None)
    if provider_type == "gemini_cli":
        return tuple(path for path in (
            _configured_path(plugin_config, "personification_gemini_cli_auth_path"),
            home / ".gemini" / "oauth_creds.json",
            appdata / "gemini" / "oauth_creds.json",
        ) if path is not None)
    if provider_type == "claude_code":
        return tuple(path for path in (
            _configured_path(plugin_config, "personification_claude_code_auth_path"),
            home / ".claude" / ".credentials.json",
            appdata / "Claude" / ".credentials.json",
        ) if path is not None)
    if provider_type == "opencode_cli":
        return (
            appdata / "opencode" / "auth.json",
            local / "opencode" / "auth.json",
        )
    return ()


def _file_credential_state(provider_type: str, plugin_config: Any = None) -> tuple[str, str]:
    for candidate in _candidate_paths(provider_type, plugin_config):
        try:
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                continue
            # Validate only the outer JSON shape; never return or log its values.
            if candidate.suffix.lower() == ".json":
                json.loads(candidate.read_text(encoding="utf-8"))
            return "ready", "file"
        except json.JSONDecodeError:
            return "unreadable", "file"
        except (OSError, UnicodeError):
            return "unreadable", "file"
    return "missing", "none"


def _keyring_credential_state(provider_type: str) -> tuple[str, str]:
    if provider_type != "antigravity_cli":
        return "missing", "none"
    try:
        import keyring  # type: ignore

        value = keyring.get_password("gemini:antigravity", "antigravity")
        # Do not parse or persist the secret.  Presence is enough for a status hint.
        return ("ready", "keyring") if str(value or "").strip() else ("missing", "none")
    except Exception:
        return "unreadable", "keyring"


async def _run_version(executable: str) -> tuple[str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
        if process.returncode != 0:
            return "", "cli_version_failed"
        first = stdout.decode("utf-8", "replace").splitlines()[0].strip() if stdout else ""
        return first[:64], ""
    except asyncio.TimeoutError:
        return "", "cli_version_timeout"
    except (OSError, asyncio.CancelledError):
        return "", "cli_unavailable"


async def _run_codex_login_status(executable: str) -> tuple[str, str]:
    """Read the CLI's own login status without exposing its output."""

    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "login",
            "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
        text = stdout.decode("utf-8", "replace").lower() if stdout else ""
        if process.returncode == 0 and any(token in text for token in ("logged in", "authenticated", "已登录")):
            return "cli_only", "cli_status"
        return "missing", "cli_status"
    except asyncio.TimeoutError:
        return "unreadable", "cli_status_timeout"
    except (OSError, asyncio.CancelledError):
        return "unreadable", "cli_status_unavailable"


def _local_model_candidates(provider_type: str) -> list[dict[str, str]]:
    try:
        from ..skills.skillpacks.tool_caller.scripts import impl

        candidate_fn = {
            "gemini_cli": impl._gemini_cli_model_candidates,
            "antigravity_cli": impl._antigravity_cli_model_candidates,
        }.get(provider_type)
        if candidate_fn is None:
            return []
        return [
            {
                "id": str(model_id),
                "label": str(model_id),
                # Discovery never uploads a probe file, so media support remains explicit.
                "media_protocol": "unknown",
            }
            for model_id in candidate_fn("")
            if str(model_id or "").strip()
        ][:16]
    except Exception:
        return []


async def discover_clis(plugin_config: Any = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for _name, provider_type, names in _CLI_SPECS:
        models = _local_model_candidates(provider_type)
        executable = _candidate_executable(names)
        if not executable:
            file_state, file_source = _file_credential_state(provider_type, plugin_config)
            key_state, key_source = _keyring_credential_state(provider_type)
            state, source = (
                (key_state, key_source)
                if key_state == "ready"
                else (file_state, file_source)
            )
            items.append(
                {
                    "provider_type": provider_type,
                    "installed": False,
                    "version": "",
                    "credential_state": state,
                    "credential_source": source,
                    "models": models,
                    "selected_model": models[0]["id"] if models else "",
                    "diagnostic_code": "cli_not_found",
                }
            )
            continue
        version, diagnostic = await _run_version(executable)
        file_state, file_source = _file_credential_state(provider_type, plugin_config)
        key_state, key_source = _keyring_credential_state(provider_type)
        state, source = (
            (key_state, key_source)
            if key_state == "ready"
            else (file_state, file_source)
        )
        if state == "missing" and provider_type == "codex_cli":
            # The fixed status subcommand is read-only; it never starts login or update.
            cli_state, cli_source = await _run_codex_login_status(executable)
            state, source = cli_state, "cli_status"
            if cli_state == "unreadable" and not diagnostic:
                diagnostic = cli_source
        items.append(
            {
                "provider_type": provider_type,
                "installed": True,
                "version": version,
                "credential_state": state,
                "credential_source": source,
                "models": models,
                "selected_model": models[0]["id"] if models else "",
                "diagnostic_code": diagnostic,
            }
        )
    return {"schema_version": 1, "items": items, "refreshed_at": __import__("time").time()}


__all__ = ["discover_clis"]
