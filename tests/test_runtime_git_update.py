from __future__ import annotations

import asyncio

from ._loader import load_personification_module


runtime_commands = load_personification_module("plugin.personification.handlers.runtime_commands")


def test_runtime_git_update_prefers_configured_mirror(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    async def _fake_run_git(args, *, cwd, extra_config=None):  # noqa: ANN001, ANN003
        calls.append({"args": args, "extra_config": list(extra_config or [])})
        if args == ["fetch", "--prune"] and extra_config:
            return 0, "mirror ok", ""
        if args == ["fetch", "--prune"]:
            return -1, "", "direct should not run"
        return 1, "", "unexpected git args: " + " ".join(args)

    async def _fake_origin(_cwd):  # noqa: ANN001
        return "https://github.com/example/personification.git"

    async def _fake_probe(_mirror, _repo_url, *, timeout=4.0):  # noqa: ANN001
        return True

    monkeypatch.setattr(runtime_commands, "_git_mirror_prefixes", lambda: ["https://mirror.example"])
    monkeypatch.setattr(runtime_commands, "_get_origin_https_url", _fake_origin)
    monkeypatch.setattr(runtime_commands, "_probe_mirror", _fake_probe)
    monkeypatch.setattr(runtime_commands, "_run_git_command", _fake_run_git)

    rc, out, err, used_mirror, probes = asyncio.run(
        runtime_commands._run_git_with_mirror_fallback(
            ["fetch", "--prune"],
            cwd=str(tmp_path),
        )
    )

    assert rc == 0
    assert out == "mirror ok"
    assert err == ""
    assert used_mirror == "https://mirror.example"
    assert probes == [("https://mirror.example", True)]
    assert calls == [
        {
            "args": ["fetch", "--prune"],
            "extra_config": [
                "url.https://mirror.example/https://github.com/.insteadOf=https://github.com/"
            ],
        }
    ]


def test_runtime_git_update_treats_git_timeout_as_network_failure() -> None:
    assert runtime_commands._looks_like_network_failure("git 命令超时（60s）")
