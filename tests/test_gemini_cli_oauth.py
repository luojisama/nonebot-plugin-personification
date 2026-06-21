from __future__ import annotations

import json
import time
from pathlib import Path

from ._loader import load_personification_module


impl = load_personification_module("plugin.personification.skills.skillpacks.tool_caller.scripts.impl")


def test_extract_access_token_from_flat_layout() -> None:
    auth = {"access_token": "abc", "refresh_token": "rrr"}
    assert impl._get_gemini_cli_access_token(auth) == "abc"
    assert impl._get_gemini_cli_refresh_token(auth) == "rrr"


def test_extract_access_token_from_nested_layouts() -> None:
    a1 = {"credentials": {"access_token": "x", "refresh_token": "y"}}
    assert impl._get_gemini_cli_access_token(a1) == "x"
    assert impl._get_gemini_cli_refresh_token(a1) == "y"
    a2 = {"tokens": {"access_token": "p", "refresh_token": "q"}}
    assert impl._get_gemini_cli_access_token(a2) == "p"
    assert impl._get_gemini_cli_refresh_token(a2) == "q"


def test_expiry_ms_parsing_supports_ms_and_seconds_and_iso() -> None:
    f = impl._get_gemini_cli_token_expiry_ms
    assert f({"expiry_date": 1_700_000_000_000}) == 1_700_000_000_000  # ms 原值
    assert f({"expires_at": 1_700_000_000}) == 1_700_000_000_000  # 秒级 → ms
    assert f({"expiry": "2024-01-01T00:00:00Z"}) > 0
    assert f({"credentials": {"expiry_date": 1_650_000_000_000}}) == 1_650_000_000_000


def test_persist_refreshed_auth_updates_token_and_expiry(tmp_path: Path) -> None:
    auth_file = tmp_path / "oauth_creds.json"
    initial = {"access_token": "OLD", "refresh_token": "RRR", "expiry_date": 0}
    auth_file.write_text(json.dumps(initial), encoding="utf-8")
    impl._persist_refreshed_gemini_cli_auth(
        auth_file,
        initial,
        access_token="NEW_TOKEN",
        expires_in=3600,
        id_token="ID_X",
    )
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "NEW_TOKEN"
    assert saved["id_token"] == "ID_X"
    assert int(saved["expiry_date"]) > int(time.time() * 1000)
    assert saved["refresh_token"] == "RRR"  # 不应被覆盖


def test_persist_refreshed_auth_keeps_nested_layout(tmp_path: Path) -> None:
    auth_file = tmp_path / "oauth_creds.json"
    initial = {"credentials": {"access_token": "OLD", "refresh_token": "RRR"}}
    auth_file.write_text(json.dumps(initial), encoding="utf-8")
    impl._persist_refreshed_gemini_cli_auth(
        auth_file,
        initial,
        access_token="NEW_TOKEN",
        expires_in=1800,
    )
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert "credentials" in saved
    assert saved["credentials"]["access_token"] == "NEW_TOKEN"
    assert saved["credentials"]["refresh_token"] == "RRR"
    assert int(saved["credentials"]["expiry_date"]) > 0
    # 旧的扁平字段不应被新增
    assert "access_token" not in saved


def test_get_access_token_refreshes_when_expired(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    auth_file = tmp_path / "oauth_creds.json"
    expired_ms = int(time.time() * 1000) - 60_000
    auth_file.write_text(
        json.dumps({"access_token": "OLD", "refresh_token": "RRR", "expiry_date": expired_ms}),
        encoding="utf-8",
    )

    async def _fake_refresh(refresh_token: str, *, timeout: float = 30.0):
        assert refresh_token == "RRR"
        return {"access_token": "FRESH", "expires_in": 3600, "id_token": ""}

    monkeypatch.setattr(impl, "_refresh_gemini_cli_access_token", _fake_refresh)
    monkeypatch.setattr(impl, "_find_gemini_cli_auth_file_with_log", lambda _path: (auth_file, []))

    caller = impl.GeminiCliToolCaller(model="auto-gemini-3", auth_path=str(auth_file))
    token, returned_file = asyncio.run(caller._get_access_token())
    assert token == "FRESH"
    assert returned_file == auth_file
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "FRESH"


def test_get_access_token_no_refresh_when_fresh(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    auth_file = tmp_path / "oauth_creds.json"
    future_ms = int(time.time() * 1000) + 30 * 60_000  # 30 分钟后过期
    auth_file.write_text(
        json.dumps({"access_token": "STILL_OK", "refresh_token": "RRR", "expiry_date": future_ms}),
        encoding="utf-8",
    )

    refresh_called = [0]

    async def _spy_refresh(refresh_token: str, *, timeout: float = 30.0):
        refresh_called[0] += 1
        return {"access_token": "X", "expires_in": 3600}

    monkeypatch.setattr(impl, "_refresh_gemini_cli_access_token", _spy_refresh)
    monkeypatch.setattr(impl, "_find_gemini_cli_auth_file_with_log", lambda _path: (auth_file, []))

    caller = impl.GeminiCliToolCaller(model="auto-gemini-3", auth_path=str(auth_file))
    token, _ = asyncio.run(caller._get_access_token())
    assert token == "STILL_OK"
    assert refresh_called[0] == 0


def test_get_access_token_force_refresh_bypasses_expiry(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    auth_file = tmp_path / "oauth_creds.json"
    future_ms = int(time.time() * 1000) + 30 * 60_000
    auth_file.write_text(
        json.dumps({"access_token": "STILL_OK", "refresh_token": "RRR", "expiry_date": future_ms}),
        encoding="utf-8",
    )

    async def _fake_refresh(refresh_token: str, *, timeout: float = 30.0):
        return {"access_token": "FORCED_NEW", "expires_in": 3600}

    monkeypatch.setattr(impl, "_refresh_gemini_cli_access_token", _fake_refresh)
    monkeypatch.setattr(impl, "_find_gemini_cli_auth_file_with_log", lambda _path: (auth_file, []))

    caller = impl.GeminiCliToolCaller(model="auto-gemini-3", auth_path=str(auth_file))
    token, _ = asyncio.run(caller._get_access_token(force_refresh=True))
    assert token == "FORCED_NEW"
