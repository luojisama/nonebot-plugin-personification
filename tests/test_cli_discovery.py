from __future__ import annotations

import asyncio
import json

from ._loader import load_personification_module


cli_discovery = load_personification_module("plugin.personification.core.cli_discovery")


def test_cli_discovery_returns_only_redacted_status(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(cli_discovery, "_candidate_executable", lambda _names: "fixed-cli")
    monkeypatch.setattr(
        cli_discovery,
        "_file_credential_state",
        lambda _provider, _config=None: ("missing", "none"),
    )
    monkeypatch.setattr(
        cli_discovery,
        "_keyring_credential_state",
        lambda provider: ("ready", "keyring")
        if provider == "antigravity_cli"
        else ("missing", "none"),
    )

    async def _version(_executable):  # noqa: ANN001
        return "cli 1.2.3", ""

    async def _codex_status(_executable):  # noqa: ANN001
        return "cli_only", "cli_status"

    monkeypatch.setattr(cli_discovery, "_run_version", _version)
    monkeypatch.setattr(cli_discovery, "_run_codex_login_status", _codex_status)
    result = asyncio.run(cli_discovery.discover_clis())
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["schema_version"] == 1
    assert len(result["items"]) == 5
    agy = next(item for item in result["items"] if item["provider_type"] == "antigravity_cli")
    assert agy["credential_state"] == "ready"
    assert agy["credential_source"] == "keyring"
    for forbidden in ("access_token", "refresh_token", "cookie", "auth.json", "fixed-cli"):
        assert forbidden not in serialized.lower()


def test_codex_cli_only_state_comes_from_fixed_status_command(monkeypatch) -> None:  # noqa: ANN001
    class _Process:
        returncode = 0

        async def communicate(self):
            return b"Logged in using system credential store", b""

    async def _spawn(*args, **_kwargs):  # noqa: ANN002,ANN003
        assert args[1:] == ("login", "status")
        return _Process()

    monkeypatch.setattr(cli_discovery.asyncio, "create_subprocess_exec", _spawn)
    state, source = asyncio.run(cli_discovery._run_codex_login_status("codex"))

    assert state == "cli_only"
    assert source == "cli_status"
