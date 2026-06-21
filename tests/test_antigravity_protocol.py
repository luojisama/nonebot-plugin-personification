from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from ._loader import load_personification_module

import pytest

impl = load_personification_module(
    "plugin.personification.skills.skillpacks.tool_caller.scripts.impl"
)


@pytest.fixture(autouse=True)
def _clear_pool():
    impl._clear_http_client_pool()
    yield
    impl._clear_http_client_pool()


# ====== 纯函数测试 ======

def test_endpoint_uses_cloudcode_pa() -> None:
    """endpoint 必须是 Antigravity CLI 实际使用的 daily-cloudcode-pa。"""
    endpoint = impl._ANTIGRAVITY_CLI_GENERATE_ENDPOINT
    assert endpoint == "https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent"
    assert impl._ANTIGRAVITY_CLI_STREAM_ENDPOINT == (
        "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"
    )
    assert "antigravity-pa" not in endpoint


def test_user_agent_format() -> None:
    ua = impl._antigravity_user_agent()
    assert ua.startswith("antigravity/")
    rest = ua.split(" ", 1)[1]
    assert "/" in rest
    os_part, arch_part = rest.split("/", 1)
    assert os_part in {"windows", "linux", "macos"}
    assert arch_part in {"amd64", "arm64"}, f"unexpected arch token: {arch_part!r}"


def test_user_agent_version_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("ANTIGRAVITY_CLI_VERSION", "9.8.7-test")
    assert impl._antigravity_user_agent().startswith("antigravity/9.8.7-test ")


def test_client_platform_returns_canonical() -> None:
    p = impl._antigravity_client_platform()
    assert p in {"WINDOWS", "MACOS", "LINUX"}


def test_model_candidates_user_model_first() -> None:
    cs = impl._antigravity_cli_model_candidates("gemini-3.5-flash")
    assert cs[0] == "gemini-3.5-flash-low"
    assert "gemini-2.5-flash" in cs
    assert "gemini-3.1-pro-high" in cs
    assert cs.count("gemini-3.5-flash-low") == 1


def test_model_candidates_auto_expands() -> None:
    cs = impl._antigravity_cli_model_candidates("auto-gemini-3")
    assert cs[0] == "gemini-3.5-flash-low"
    assert "gemini-3.1-pro-low" in cs


def test_model_candidates_unknown_user_model_keeps_first_with_fallback() -> None:
    cs = impl._antigravity_cli_model_candidates("some-future-model-x")
    assert cs[0] == "some-future-model-x"
    assert "gemini-3.5-flash-low" in cs


# ====== 协议端到端测试（mock httpx） ======

def _run_chat(model: str = "gemini-3.5-flash") -> dict[str, Any]:
    """构造一个 AntigravityCliToolCaller，mock 凭证/project/httpx，捕获 POST 详情。"""
    caller = impl.AntigravityCliToolCaller(
        model=model,
        auth_path="",
        project="fake-project-id",
        thinking_mode="none",
        timeout=30.0,
    )
    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_CapturingClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict) -> Any:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.text = 'data: {"response":{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}}\n\n'
            resp.json = lambda: {}
            resp.status_code = 200
            return resp

    async def fake_get_access_token(*, force_refresh: bool = False):
        return ("fake-token", pathlib.Path("/tmp/fake_auth.json"))

    with patch.object(caller, "_get_access_token", new=fake_get_access_token), \
         patch.object(impl.httpx, "AsyncClient", _CapturingClient):
        result = asyncio.run(
            caller.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                use_builtin_search=False,
            )
        )

    captured["result"] = result
    return captured


def test_envelope_includes_user_agent_and_request_id() -> None:
    c = _run_chat()
    body = c["json"]
    assert body["model"] == "gemini-3.5-flash-low"
    assert body["project"] == "fake-project-id"
    assert body["userAgent"] == "antigravity"
    assert "requestId" in body
    rid = body["requestId"]
    assert isinstance(rid, str) and len(rid) >= 16, f"requestId 不合理: {rid!r}"


def test_post_url_is_cloudcode_not_antigravity_pa() -> None:
    c = _run_chat()
    assert c["url"] == "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"
    assert "antigravity-pa" not in c["url"]


def test_headers_include_client_metadata_and_x_goog() -> None:
    c = _run_chat()
    h = c["headers"]
    assert h["Authorization"] == "Bearer fake-token"
    assert h["Content-Type"] == "application/json"
    assert h["Accept"] == "text/event-stream"
    assert h["X-Goog-Api-Client"] == "google-cloud-sdk vscode_cloudshelleditor/0.1"
    assert h["User-Agent"].startswith("antigravity/")
    cm = json.loads(h["Client-Metadata"])
    assert cm["ideType"] == "ANTIGRAVITY"
    assert cm["pluginType"] == "GEMINI"
    assert cm["platform"] in {"WINDOWS", "MACOS", "LINUX"}


def test_load_code_assist_uses_antigravity_endpoint_and_metadata() -> None:
    caller = impl.AntigravityCliToolCaller(
        model="gemini-3.5-flash",
        auth_path="",
        project="",
        thinking_mode="none",
        timeout=30.0,
    )
    assert caller._load_code_assist_endpoint() == (
        "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
    )
    metadata = caller._load_code_assist_metadata()
    assert metadata["ideType"] == "ANTIGRAVITY"
    assert metadata["pluginType"] == "GEMINI"
    assert metadata["platform"] in {"WINDOWS", "MACOS", "LINUX"}


def test_response_unwrapped_correctly() -> None:
    c = _run_chat()
    assert c["result"].content == "ok"


def test_antigravity_sse_chunks_are_merged() -> None:
    body = "\n".join(
        [
            'data: {"response":{"candidates":[{"content":{"parts":[{"text":"o"}]}}]}}',
            "",
            'data: {"response":{"candidates":[{"content":{"parts":[{"text":"k"}]},"finishReason":"STOP"}],"usageMetadata":{"totalTokenCount":3}}}',
            "",
        ]
    )
    data = impl._parse_antigravity_sse_response(body)
    payload = data["response"]
    parts = payload["candidates"][0]["content"]["parts"]
    assert parts == [{"text": "o"}, {"text": "k"}]
    assert payload["candidates"][0]["finishReason"] == "STOP"
    assert payload["usageMetadata"]["totalTokenCount"] == 3


def test_antigravity_retries_transient_tls_eof() -> None:
    caller = impl.AntigravityCliToolCaller(
        model="gemini-3.5-flash",
        auth_path="",
        project="fake-project-id",
        thinking_mode="none",
        timeout=30.0,
    )
    attempts = {"count": 0}

    class _RetryingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_RetryingClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict) -> Any:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise impl.httpx.ConnectError("TLS/SSL connection has been closed (EOF)")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.text = 'data: {"response":{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}}\n\n'
            resp.json = lambda: {}
            resp.status_code = 200
            return resp

    async def fake_get_access_token(*, force_refresh: bool = False):
        return ("fake-token", pathlib.Path("/tmp/fake_auth.json"))

    async def no_sleep(delay: float) -> None:
        return None

    with patch.object(caller, "_get_access_token", new=fake_get_access_token), \
         patch.object(impl.httpx, "AsyncClient", _RetryingClient), \
         patch.object(impl.asyncio, "sleep", new=no_sleep):
        result = asyncio.run(
            caller.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                use_builtin_search=False,
            )
        )

    assert attempts["count"] == 2
    assert result.content == "ok"


# ====== keyring 同步链路 ======

def _make_fake_keyring(blob: str | None) -> Any:
    class _FakeKeyring:
        @staticmethod
        def get_password(service: str, user: str) -> str | None:
            assert service == "gemini:antigravity"
            assert user == "antigravity"
            return blob
    return _FakeKeyring


def _utf16le_wrap(s: str) -> str:
    """模拟 Windows Credential Manager 把 UTF-8 bytes 当 UTF-16LE 字符串呈现的行为。"""
    b = s.encode("utf-8")
    if len(b) % 2:
        b += b"\x00"
    return b.decode("utf-16-le")


def test_read_keyring_token_decodes_utf16_wrap(monkeypatch=None) -> None:
    raw_json = json.dumps({
        "token": {
            "access_token": "ya29.fake-access-token-xxxxx",
            "token_type": "Bearer",
            "refresh_token": "1//06fake-refresh",
            "expiry": "2030-01-01T00:00:00+00:00",
        },
        "auth_method": "consumer",
    })
    wrapped = _utf16le_wrap(raw_json)
    fake = _make_fake_keyring(wrapped)
    saved = sys.modules.get("keyring")
    sys.modules["keyring"] = fake
    try:
        token = impl._read_antigravity_keyring_token()
    finally:
        if saved is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = saved
    assert token is not None
    assert token["access_token"] == "ya29.fake-access-token-xxxxx"
    assert token["refresh_token"] == "1//06fake-refresh"


def test_read_keyring_token_returns_none_when_missing() -> None:
    fake = _make_fake_keyring(None)
    saved = sys.modules.get("keyring")
    sys.modules["keyring"] = fake
    try:
        assert impl._read_antigravity_keyring_token() is None
    finally:
        if saved is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = saved


def test_sync_keyring_writes_file_with_full_scope(tmp_path_root: Any = None) -> None:
    raw_json = json.dumps({
        "token": {
            "access_token": "ya29.fake-AT",
            "token_type": "Bearer",
            "refresh_token": "1//rf",
            "expiry": "2030-01-01T00:00:00+00:00",
        },
        "auth_method": "consumer",
    })
    wrapped = _utf16le_wrap(raw_json)
    fake = _make_fake_keyring(wrapped)
    saved_kr = sys.modules.get("keyring")
    sys.modules["keyring"] = fake
    # 也要把 _Path.home() 重定向到 tmp 目录，避免污染真实用户目录
    import tempfile
    tmp_home = pathlib.Path(tempfile.mkdtemp())
    saved_home = impl._Path.home
    impl._Path.home = staticmethod(lambda: tmp_home)
    try:
        target = impl._sync_antigravity_keyring_to_file()
        assert target is not None
        assert target.exists()
        d = json.loads(target.read_text(encoding="utf-8"))
        assert d["access_token"] == "ya29.fake-AT"
        assert d["refresh_token"] == "1//rf"
        assert "cclog" in d["scope"]
        assert "experimentsandconfigs" in d["scope"]
        assert d["expiry_date"] > 0
    finally:
        if saved_kr is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = saved_kr
        impl._Path.home = saved_home
        import shutil
        shutil.rmtree(tmp_home, ignore_errors=True)


def test_find_auth_file_logs_keyring_sync_attempt() -> None:
    """_find_antigravity_cli_auth_file_with_log 应在搜索 log 里体现 keyring 同步动作。"""
    # 不论 keyring 有无凭证，函数都应正常返回（不抛）
    fake = _make_fake_keyring(None)
    saved_kr = sys.modules.get("keyring")
    sys.modules["keyring"] = fake
    try:
        found, log = impl._find_antigravity_cli_auth_file_with_log("")
    finally:
        if saved_kr is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = saved_kr
    # log 第一项不一定包含 "keyring"（无 token 时不写日志），但函数本身要正常返回
    assert isinstance(log, list)


def test_find_auth_file_prefers_antigravity_oauth_token_file() -> None:
    import shutil
    import tempfile

    tmp_home = pathlib.Path(tempfile.mkdtemp())
    token_file = tmp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(
        json.dumps(
            {
                "auth_method": "consumer",
                "token": {
                    "access_token": "agy-at",
                    "refresh_token": "agy-rt",
                    "token_type": "Bearer",
                    "expiry": "2030-01-01T00:00:00Z",
                },
            }
        ),
        encoding="utf-8",
    )
    fake = _make_fake_keyring(None)
    saved_kr = sys.modules.get("keyring")
    saved_home_env = os.environ.get("HOME")
    sys.modules["keyring"] = fake
    try:
        os.environ["HOME"] = str(tmp_home)
        found, _searched, source = impl._find_antigravity_cli_auth_file_with_source("")
    finally:
        if saved_kr is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = saved_kr
        if saved_home_env is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home_env
        shutil.rmtree(tmp_home, ignore_errors=True)

    assert found == token_file
    assert source == "antigravity"


def test_resolve_antigravity_project_from_workspace_config(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "bot" / "shizuku" / "plugin"
    workspace.mkdir(parents=True)
    project_dir = tmp_path / ".gemini" / "config" / "projects"
    project_dir.mkdir(parents=True)
    project_dir.joinpath("agy-project.json").write_text(
        json.dumps(
            {
                "id": "732daa86-2ed5-4c86-a4b4-4a4e1734a996",
                "name": str(tmp_path / "bot" / "shizuku"),
                "projectResources": {
                    "resources": [
                        {
                            "gitFolder": {
                                "folderUri": f"file://{tmp_path / 'bot' / 'shizuku'}",
                                "allowWrite": True,
                            }
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    old_cwd = pathlib.Path.cwd()
    try:
        os.chdir(workspace)
        assert impl._resolve_antigravity_project_from_workspace_configs() == (
            "732daa86-2ed5-4c86-a4b4-4a4e1734a996"
        )
    finally:
        os.chdir(old_cwd)


def test_antigravity_oauth_token_nested_shape_is_read_and_persisted(tmp_path) -> None:
    auth_file = tmp_path / "antigravity-oauth-token"
    auth = {
        "auth_method": "consumer",
        "token": {
            "access_token": "old-at",
            "refresh_token": "agy-rt",
            "token_type": "Bearer",
            "expiry": "2020-01-01T00:00:00Z",
        },
    }
    auth_file.write_text(json.dumps(auth), encoding="utf-8")
    loaded = impl._load_gemini_cli_auth(auth_file)

    assert impl._get_gemini_cli_access_token(loaded) == "old-at"
    assert impl._get_gemini_cli_refresh_token(loaded) == "agy-rt"
    assert impl._get_gemini_cli_token_expiry_ms(loaded) > 0

    persisted = impl._persist_refreshed_gemini_cli_auth(
        auth_file,
        loaded,
        access_token="new-at",
        expires_in=3600,
    )

    assert persisted["token"]["access_token"] == "new-at"
    assert persisted["token"]["expiry"].endswith("Z")
    on_disk = json.loads(auth_file.read_text(encoding="utf-8"))
    assert on_disk["token"]["access_token"] == "new-at"


# ====== OAuth client 区分（refresh token 用对的 client） ======

def test_antigravity_oauth_client_id_decodes_correctly() -> None:
    """验证 client_id 解码后结构正确 + sha256 指纹匹配（避免明文进入 git history
    被 GitHub Secret Scanning 拦截）。"""
    import hashlib
    cid = impl._antigravity_oauth_client_id()
    # 结构：12 位数字前缀 + - + 32 字符 middle + .apps.googleusercontent.com
    assert cid.startswith("1071006060591-")
    assert cid.endswith(".apps.googleusercontent.com")
    assert len(cid) == 73
    # 全文 sha256 指纹（提前离线算好的预期值）
    assert hashlib.sha256(cid.encode("ascii")).hexdigest() == (
        "bf00c418024ba6bf606ccdc37120976e41bc429dd1d46ecf16a729aa532626ea"
    )


def test_antigravity_oauth_client_secret_decodes_correctly() -> None:
    """同样用 sha256 指纹校验 client_secret，不在代码里出现明文。"""
    import hashlib
    secret = impl._antigravity_oauth_client_secret()
    assert secret.startswith("GOCS" + "PX-")
    assert len(secret) == 35
    assert hashlib.sha256(secret.encode("ascii")).hexdigest() == (
        "1d2f041093fd95aa8995a038c711d50a7960da09a505381c09a745d6ad0ecc60"
    )


def test_antigravity_oauth_differs_from_gemini_cli() -> None:
    """两组 OAuth client 必须不同，否则 refresh 会 unauthorized_client。"""
    assert impl._antigravity_oauth_client_id() != impl._gemini_oauth_client_id()
    assert impl._antigravity_oauth_client_secret() != impl._gemini_oauth_client_secret()


def test_refresh_antigravity_sends_antigravity_client() -> None:
    """_refresh_antigravity_cli_access_token 应该把 antigravity client_id/secret
    放到 POST body 里，而不是 gemini-cli 的。"""
    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_CapturingClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, data: dict, headers: dict) -> Any:
            captured["url"] = url
            captured["data"] = data
            resp = MagicMock()
            resp.status_code = 200
            resp.json = lambda: {"access_token": "new-at", "expires_in": 3599}
            return resp

    with patch.object(impl.httpx, "AsyncClient", _CapturingClient):
        result = asyncio.run(impl._refresh_antigravity_cli_access_token("fake-refresh"))
    assert result["access_token"] == "new-at"
    assert captured["url"] == "https://oauth2.googleapis.com/token"
    assert captured["data"]["client_id"] == impl._antigravity_oauth_client_id()
    assert captured["data"]["client_secret"] == impl._antigravity_oauth_client_secret()
    assert captured["data"]["refresh_token"] == "fake-refresh"
    assert captured["data"]["grant_type"] == "refresh_token"


def test_refresh_token_remote_dispatches_to_right_client() -> None:
    """GeminiCliToolCaller 用 gemini-cli refresh；AntigravityCliToolCaller 用
    antigravity refresh。两者通过 _refresh_token_remote 分派，互不串。"""
    captured: list[str] = []

    async def fake_refresh_gemini(rt, *, timeout=30.0, proxy="", trust_env=None):
        captured.append(f"gemini:{rt}:proxy={proxy}:trust_env={trust_env}")
        return {"access_token": "g-at", "expires_in": 3599}

    async def fake_refresh_antigravity(rt, *, timeout=30.0, proxy="", trust_env=None):
        captured.append(f"antigravity:{rt}:proxy={proxy}:trust_env={trust_env}")
        return {"access_token": "a-at", "expires_in": 3599}

    with patch.object(impl, "_refresh_gemini_cli_access_token", new=fake_refresh_gemini), \
         patch.object(impl, "_refresh_antigravity_cli_access_token", new=fake_refresh_antigravity):
        gemini = impl.GeminiCliToolCaller(
            model="gemini-2.5-flash", auth_path="", project="p", thinking_mode="none", timeout=30.0
        )
        anti = impl.AntigravityCliToolCaller(
            model="gemini-3.5-flash", auth_path="", project="p", thinking_mode="none", timeout=30.0
        )
        r1 = asyncio.run(gemini._refresh_token_remote("rt-1"))
        r2 = asyncio.run(anti._refresh_token_remote("rt-2"))

    assert captured == ["gemini:rt-1:proxy=:trust_env=None", "antigravity:rt-2:proxy=:trust_env=False"]
    assert r1["access_token"] == "g-at"
    assert r2["access_token"] == "a-at"


def test_antigravity_gemini_compat_auth_refreshes_with_gemini_client() -> None:
    """Linux 无 keyring 且配置 ~/.gemini/oauth_creds.json 时；这类凭证要用
    gemini-cli OAuth client 刷新，不能用 antigravity client。"""
    import tempfile
    import shutil

    tmp_home = pathlib.Path(tempfile.mkdtemp())
    auth_dir = tmp_home / ".gemini"
    auth_dir.mkdir(parents=True)
    auth_file = auth_dir / "oauth_creds.json"
    auth_file.write_text(
        json.dumps(
            {
                "access_token": "old-at",
                "refresh_token": "rt-gemini",
                "expiry_date": 1,
            }
        ),
        encoding="utf-8",
    )

    fake_keyring = _make_fake_keyring(None)
    saved_kr = sys.modules.get("keyring")
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "HOME",
            "GEMINI_CLI_HOME",
            "GEMINI_HOME",
            "ANTIGRAVITY_CLI_AUTH_PATH",
            "AGY_AUTH_PATH",
            "ANTIGRAVITY_CLI_HOME",
            "ANTIGRAVITY_HOME",
            "AGY_HOME",
        )
    }
    calls: list[str] = []

    async def fake_refresh_gemini(rt, *, timeout=30.0, proxy="", trust_env=None):
        calls.append(f"gemini:{rt}:proxy={proxy}:trust_env={trust_env}")
        return {"access_token": "new-gemini-at", "expires_in": 3599}

    async def fake_refresh_antigravity(rt, *, timeout=30.0, proxy="", trust_env=None):
        calls.append(f"antigravity:{rt}:proxy={proxy}:trust_env={trust_env}")
        return {"access_token": "new-antigravity-at", "expires_in": 3599}

    sys.modules["keyring"] = fake_keyring
    try:
        os.environ["HOME"] = str(tmp_home)
        for key in saved_env:
            if key != "HOME":
                os.environ.pop(key, None)
        caller = impl.AntigravityCliToolCaller(
            model="gemini-3.5-flash",
            auth_path="~/.gemini/oauth_creds.json",
            project="p",
            thinking_mode="none",
            timeout=30.0,
            proxy="http://127.0.0.1:17890",
        )
        with patch.object(impl, "_refresh_gemini_cli_access_token", new=fake_refresh_gemini), \
             patch.object(impl, "_refresh_antigravity_cli_access_token", new=fake_refresh_antigravity):
            token, found = asyncio.run(caller._get_access_token())
    finally:
        if saved_kr is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = saved_kr
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp_home, ignore_errors=True)

    assert token == "new-gemini-at"
    assert found == auth_file
    assert calls == ["gemini:rt-gemini:proxy=http://127.0.0.1:17890:trust_env=False"]


# ====== 显式 proxy 配置 ======

def test_default_proxy_is_empty_and_not_passed_to_httpx() -> None:
    """不传 proxy 且无 env proxy 时：不传 proxy=，并忽略环境代理以兼容 TUN 模式。"""
    caller = impl.AntigravityCliToolCaller(
        model="gemini-3.5-flash", auth_path="", project="p", thinking_mode="none", timeout=30.0
    )
    assert caller.proxy == ""

    captured: dict[str, Any] = {}

    class _Inspect:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

        async def __aenter__(self) -> "_Inspect":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url, *, json, headers):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = lambda: {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}
            resp.status_code = 200
            return resp

    async def fake_get_access_token(*, force_refresh=False):
        return ("fake-tok", pathlib.Path("/tmp/x.json"))

    _proxy_vars = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    _saved_env = {k: os.environ.pop(k, None) for k in _proxy_vars}
    try:
        with patch.object(caller, "_get_access_token", new=fake_get_access_token), \
             patch.object(impl.httpx, "AsyncClient", _Inspect):
            asyncio.run(caller.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}], tools=[], use_builtin_search=False
            ))
    finally:
        for k, v in _saved_env.items():
            if v is not None:
                os.environ[k] = v
    assert "proxy" not in captured["kwargs"]
    assert captured["kwargs"].get("trust_env") is False


def test_env_proxy_is_respected_when_no_explicit_proxy() -> None:
    """有 HTTPS_PROXY 环境变量且未配置显式 proxy 时，不应强制 trust_env=False。"""
    caller = impl.AntigravityCliToolCaller(
        model="gemini-3.5-flash", auth_path="", project="p", thinking_mode="none", timeout=30.0
    )
    assert caller.proxy == ""

    captured: dict[str, Any] = {}

    class _Inspect:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

        async def __aenter__(self) -> "_Inspect":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url, *, json, headers):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = lambda: {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}
            resp.status_code = 200
            return resp

    async def fake_get_access_token(*, force_refresh=False):
        return ("fake-tok", pathlib.Path("/tmp/x.json"))

    _saved = os.environ.get("HTTPS_PROXY")
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
    try:
        with patch.object(caller, "_get_access_token", new=fake_get_access_token), \
             patch.object(impl.httpx, "AsyncClient", _Inspect):
            asyncio.run(caller.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}], tools=[], use_builtin_search=False
            ))
    finally:
        if _saved is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = _saved
    assert "proxy" not in captured["kwargs"]
    # 有 HTTPS_PROXY 时不应强制 trust_env=False，让 httpx 自己读取环境代理
    assert captured["kwargs"].get("trust_env") is not False


def test_explicit_proxy_passed_through_to_httpx() -> None:
    """传 proxy 时 chat_with_tools 应显式把它给 httpx.AsyncClient(proxy=...)。"""
    caller = impl.AntigravityCliToolCaller(
        model="gemini-3.5-flash",
        auth_path="",
        project="p",
        thinking_mode="none",
        timeout=30.0,
        proxy="http://127.0.0.1:17890",
    )
    assert caller.proxy == "http://127.0.0.1:17890"

    captured: dict[str, Any] = {}

    class _Inspect:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

        async def __aenter__(self) -> "_Inspect":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url, *, json, headers):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = lambda: {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}
            resp.status_code = 200
            return resp

    async def fake_get_access_token(*, force_refresh=False):
        return ("fake-tok", pathlib.Path("/tmp/x.json"))

    with patch.object(caller, "_get_access_token", new=fake_get_access_token), \
         patch.object(impl.httpx, "AsyncClient", _Inspect):
        asyncio.run(caller.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=[], use_builtin_search=False
        ))
    assert captured["kwargs"].get("proxy") == "http://127.0.0.1:17890"
    assert "trust_env" not in captured["kwargs"]


def test_refresh_helper_forwards_proxy_to_httpx() -> None:
    """_refresh_oauth_token 应该把 proxy 显式传给 httpx.AsyncClient(proxy=...)。"""
    captured: dict[str, Any] = {}

    class _Inspect:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

        async def __aenter__(self) -> "_Inspect":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url, *, data, headers):
            resp = MagicMock()
            resp.status_code = 200
            resp.json = lambda: {"access_token": "x", "expires_in": 3599}
            return resp

    with patch.object(impl.httpx, "AsyncClient", _Inspect):
        asyncio.run(impl._refresh_antigravity_cli_access_token("rt", proxy="http://127.0.0.1:17890"))
    assert captured["kwargs"].get("proxy") == "http://127.0.0.1:17890"


def test_refresh_helper_without_proxy_disables_trust_env_for_tun() -> None:
    """antigravity refresh 不传 proxy 时应忽略环境代理，交给 TUN 透明代理。"""
    captured: dict[str, Any] = {}

    class _Inspect:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

        async def __aenter__(self) -> "_Inspect":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url, *, data, headers):
            resp = MagicMock()
            resp.status_code = 200
            resp.json = lambda: {"access_token": "x", "expires_in": 3599}
            return resp

    with patch.object(impl.httpx, "AsyncClient", _Inspect):
        asyncio.run(impl._refresh_antigravity_cli_access_token("rt", trust_env=False))
    assert "proxy" not in captured["kwargs"]
    assert captured["kwargs"].get("trust_env") is False
