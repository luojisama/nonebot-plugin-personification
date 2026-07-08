from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


plugin_update_manager = load_personification_module("plugin.personification.core.plugin_update_manager")


def test_plugin_update_manager_discovers_git_source_without_hardcoded_remote(tmp_path, monkeypatch) -> None:
    remote_url = "git@example.com:team/custom-personification.git"
    local_hash = "a" * 40
    remote_hash = "b" * 40

    async def _fake_run_git(args, *, cwd, extra_config=None, timeout=60.0):  # noqa: ANN001, ANN003
        if args == ["rev-parse", "--show-toplevel"]:
            return 0, str(tmp_path), ""
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return 0, "main", ""
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return 0, "origin/main", ""
        if args == ["remote", "get-url", "origin"]:
            return 0, remote_url, ""
        if args == ["fetch", "--prune"]:
            return 0, "", ""
        if args == ["rev-parse", "HEAD"]:
            return 0, local_hash, ""
        if args == ["rev-parse", "@{u}"]:
            return 0, remote_hash, ""
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return 0, "0\t2", ""
        if args == ["status", "--porcelain"]:
            return 0, "", ""
        if args and args[0] == "log":
            if "HEAD..@{u}" in args:
                return 0, f"{remote_hash}\x1fbbbbbbb\x1f1770000000\x1fTester\x1f远端更新内容", ""
            return 0, f"{local_hash}\x1faaaaaaa\x1f1760000000\x1fTester\x1f本地历史内容", ""
        return 1, "", "unexpected git args: " + " ".join(args)

    monkeypatch.setattr(plugin_update_manager, "_run_git_command", _fake_run_git)

    status = asyncio.run(
        plugin_update_manager.get_plugin_update_status(
            plugin_root=tmp_path,
            plugin_config=SimpleNamespace(personification_git_mirror_prefixes=[]),
            refresh=True,
        )
    )

    assert status["source_type"] == "git"
    assert status["source"]["remote_url"] == "https://example.com/team/custom-personification.git"
    assert "luojisama" not in status["source"]["remote_url"].lower()
    assert status["update_available"] is True
    assert status["behind"] == 2
    assert status["pending_history"][0]["subject"] == "远端更新内容"
    assert status["history"][0]["subject"] == "本地历史内容"


def test_plugin_update_manager_prefers_configured_mirror(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    async def _fake_run_git(args, *, cwd, extra_config=None, timeout=60.0):  # noqa: ANN001, ANN003
        calls.append({"args": args, "extra_config": list(extra_config or [])})
        if args == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/example/personification.git", ""
        if args == ["fetch", "--prune"] and extra_config:
            return 0, "mirror ok", ""
        if args == ["fetch", "--prune"]:
            return -1, "", "direct should not run"
        return 1, "", "unexpected git args: " + " ".join(args)

    async def _fake_probe(_mirror, _repo_url, *, timeout=4.0):  # noqa: ANN001
        return True

    monkeypatch.setattr(plugin_update_manager, "_run_git_command", _fake_run_git)
    monkeypatch.setattr(plugin_update_manager, "_probe_mirror", _fake_probe)

    rc, out, err, used_mirror, probes = asyncio.run(
        plugin_update_manager._run_git_with_mirror_fallback(
            ["fetch", "--prune"],
            cwd=str(tmp_path),
            plugin_config=SimpleNamespace(personification_git_mirror_prefixes=["https://mirror.example"]),
        )
    )

    fetch_calls = [call for call in calls if call["args"] == ["fetch", "--prune"]]
    assert rc == 0
    assert out == "mirror ok"
    assert err == ""
    assert used_mirror == "https://mirror.example"
    assert probes == [{"mirror": "https://mirror.example", "ok": True}]
    assert fetch_calls == [
        {
            "args": ["fetch", "--prune"],
            "extra_config": [
                "url.https://mirror.example/https://github.com/.insteadOf=https://github.com/"
            ],
        }
    ]


def test_plugin_update_manager_treats_git_timeout_as_network_failure() -> None:
    assert plugin_update_manager._looks_like_network_failure("git 命令超时（60s）")


def test_plugin_manager_routes_check_update_and_audit(_runtime_context, monkeypatch) -> None:
    manager = load_personification_module("plugin.personification.core.plugin_update_manager")
    audit_mod = load_personification_module("plugin.personification.core.webui_audit_log")
    calls = {"status_refresh": [], "history": 0, "update": 0}

    async def _fake_status(*, plugin_config=None, refresh=False, history_limit=12, plugin_root=None):  # noqa: ANN001
        calls["status_refresh"].append(refresh)
        return {
            "available": True,
            "source_type": "git",
            "update_supported": True,
            "repo_root": "D:/dynamic/repo",
            "plugin_root": "D:/dynamic/repo/plugin/personification",
            "plugin_subdir": "plugin/personification",
            "source": {
                "type": "git",
                "remote_name": "origin",
                "remote_url": "https://example.com/team/custom.git",
                "branch": "main",
                "upstream": "origin/main",
            },
            "local": {"hash": "a" * 40, "short_hash": "aaaaaaa", "branch": "main"},
            "remote": {"hash": "b" * 40, "short_hash": "bbbbbbb", "upstream": "origin/main", "error": ""},
            "ahead": 0,
            "behind": 1,
            "update_available": True,
            "dirty": False,
            "dirty_count": 0,
            "fetch": {"attempted": refresh, "ok": True, "error": "", "used_mirror": "", "probes": []},
            "history": [{"short_hash": "aaaaaaa", "subject": "本地历史", "author": "A", "timestamp": 1760000000}],
            "pending_history": [{"short_hash": "bbbbbbb", "subject": "远端更新", "author": "B", "timestamp": 1770000000}],
            "message": "发现 1 个待更新提交",
        }

    async def _fake_history(*, plugin_config=None, limit=30, refresh=False, plugin_root=None):  # noqa: ANN001
        calls["history"] += 1
        status = await _fake_status(plugin_config=plugin_config, refresh=refresh, history_limit=limit)
        return {
            "available": True,
            "source_type": "git",
            "source": status["source"],
            "history": status["history"],
            "pending_history": status["pending_history"],
            "fetch": status["fetch"],
            "message": status["message"],
        }

    async def _fake_update(*, plugin_config=None, plugin_root=None):  # noqa: ANN001
        calls["update"] += 1
        status = await _fake_status(plugin_config=plugin_config, refresh=False)
        status["update_available"] = False
        status["behind"] = 0
        status["message"] = "已更新"
        return {"ok": True, "updated": True, "status": status, "message": "已更新"}

    monkeypatch.setattr(manager, "get_plugin_update_status", _fake_status)
    monkeypatch.setattr(manager, "get_plugin_update_history", _fake_history)
    monkeypatch.setattr(manager, "perform_plugin_update", _fake_update)

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    status = client.get("/personification/api/plugin-manager/status")
    assert status.status_code == 200, status.text
    assert status.json()["source"]["remote_url"] == "https://example.com/team/custom.git"

    history = client.get("/personification/api/plugin-manager/history?limit=5")
    assert history.status_code == 200, history.text
    assert history.json()["pending_history"][0]["subject"] == "远端更新"

    checked = client.post("/personification/api/plugin-manager/check", json={})
    assert checked.status_code == 200, checked.text
    assert checked.json()["update_available"] is True

    missing_confirm = client.post("/personification/api/plugin-manager/update", json={})
    assert missing_confirm.status_code == 400

    updated = client.post("/personification/api/plugin-manager/update", json={"confirm": "update"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["ok"] is True
    assert calls["update"] == 1
    assert True in calls["status_refresh"]

    check_rows = audit_mod.query_recent(action="plugin_update_check", limit=3)
    apply_rows = audit_mod.query_recent(action="plugin_update_apply", limit=3)
    assert check_rows and check_rows[0]["target"] == "origin/main"
    assert apply_rows and apply_rows[0]["outcome"] == "ok"
