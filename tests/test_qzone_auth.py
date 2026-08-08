from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from ._loader import load_personification_module

qzone_auth = load_personification_module("plugin.personification.core.qzone_auth")
qzone_service = load_personification_module("plugin.personification.core.qzone_service")


@pytest.fixture(autouse=True)
def _reset_qzone_auth_states():  # noqa: ANN202
    with qzone_service._AUTH_STATE_LOCK:
        qzone_service._AUTH_STATES.clear()
    yield
    with qzone_service._AUTH_STATE_LOCK:
        qzone_service._AUTH_STATES.clear()


class _Logger:
    def error(self, _message: str) -> None:
        return None


def _callback(code: str, url: str = "", message: str = "") -> str:
    return f"ptuiCB('{code}','0','{url}','0','{message}','');"


def _client_factory(handler):  # noqa: ANN001, ANN201
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def _wait_terminal(manager, session_id: str, owner_key: str) -> dict:  # noqa: ANN001
    for _ in range(100):
        state = manager.status(session_id, owner_key=owner_key)
        if state["terminal"]:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError("login session did not reach a terminal state")


def test_ptqrtoken_and_callback_parser() -> None:
    assert qzone_auth._ptqrtoken("abc") == 108966
    assert qzone_auth._parse_ptui_callback(_callback("67", message="二维码已扫描")) == (
        "67",
        "",
        "二维码已扫描",
    )
    with pytest.raises(ValueError):
        qzone_auth._parse_ptui_callback("not-a-callback")


@pytest.mark.parametrize(
    "url",
    [
        "http://ptlogin2.qzone.qq.com/check_sig?a=1",
        "https://evil.example/check_sig?a=1",
        "https://ptlogin2.qzone.qq.com/other?a=1",
    ],
)
def test_login_callback_rejects_untrusted_url(url: str) -> None:
    with pytest.raises(ValueError):
        qzone_auth._validate_login_url(url, initial=True)


def test_qzone_login_manager_completes_login_without_exposing_credentials() -> None:
    callback_url = "https://ptlogin2.qzone.qq.com/check_sig?uin=99999&ptsigx=secret"
    poll_count = 0
    installed: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == "/ptqrshow":
            return httpx.Response(200, content=b"png-bytes", headers={"set-cookie": "qrsig=qr-secret; Path=/"})
        if request.url.path == "/ptqrlogin":
            poll_count += 1
            code = "67" if poll_count == 1 else "0"
            return httpx.Response(200, text=_callback(code, callback_url if code == "0" else ""))
        if request.url.path == "/check_sig":
            return httpx.Response(
                302,
                headers=[
                    ("location", "https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone"),
                    ("set-cookie", "uin=o99999; Domain=.qq.com; Path=/"),
                    ("set-cookie", "skey=s-key; Domain=.qq.com; Path=/"),
                    ("set-cookie", "p_skey=p-secret; Domain=.qq.com; Path=/"),
                ],
            )
        return httpx.Response(200, text="ok")

    async def install(cookie: str, bot_id: str, source: str) -> tuple[bool, str]:
        installed.append((cookie, bot_id, source))
        return True, "ok"

    async def scenario() -> tuple[dict, str]:
        manager = qzone_auth.QzoneLoginManager(
            client_factory=_client_factory(handler),
            poll_interval=0.001,
        )
        started = await manager.start(bot_id="99999", owner_key="admin:device", install_cookie=install)
        assert manager.qrcode(started["session_id"], owner_key="admin:device") == b"png-bytes"
        terminal = await _wait_terminal(manager, started["session_id"], "admin:device")
        return terminal, repr(terminal)

    terminal, rendered = asyncio.run(scenario())
    assert terminal["status"] == "success"
    assert installed and installed[0][1:] == ("99999", "ptlogin")
    assert "uin=o99999" in installed[0][0]
    assert "p_skey=p-secret" in installed[0][0]
    assert "qrsig" not in installed[0][0]
    assert "p-secret" not in rendered
    assert "ptsigx" not in rendered


def test_qzone_login_manager_handles_expiry_and_owner_isolation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ptqrshow":
            return httpx.Response(200, content=b"png", headers={"set-cookie": "qrsig=qrsig; Path=/"})
        return httpx.Response(200, text=_callback("65", message="二维码已失效"))

    async def install(_cookie: str, _bot_id: str, _source: str) -> tuple[bool, str]:
        raise AssertionError("expired login must not install a cookie")

    async def scenario() -> dict:
        manager = qzone_auth.QzoneLoginManager(
            client_factory=_client_factory(handler),
            poll_interval=0.001,
        )
        started = await manager.start(bot_id="99999", owner_key="owner-a", install_cookie=install)
        with pytest.raises(LookupError):
            manager.status(started["session_id"], owner_key="owner-b")
        return await _wait_terminal(manager, started["session_id"], "owner-a")

    terminal = asyncio.run(scenario())
    assert terminal["status"] == "expired"
    assert terminal["qr_ready"] is False


def test_qzone_login_manager_rejects_cross_admin_session_replacement() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ptqrshow":
            return httpx.Response(200, content=b"png", headers={"set-cookie": "qrsig=qrsig; Path=/"})
        return httpx.Response(200, text=_callback("66", message="二维码未失效"))

    async def install(_cookie: str, _bot_id: str, _source: str) -> tuple[bool, str]:
        return True, "ok"

    async def scenario() -> None:
        manager = qzone_auth.QzoneLoginManager(
            client_factory=_client_factory(handler),
            poll_interval=0.01,
        )
        await manager.start(bot_id="99999", owner_key="owner-a", install_cookie=install)
        with pytest.raises(RuntimeError, match="另一位管理员"):
            await manager.start(bot_id="99999", owner_key="owner-b", install_cookie=install)
        await manager.shutdown()

    asyncio.run(scenario())


def test_install_qzone_cookie_validates_identity_and_persists_only_after_probe(monkeypatch) -> None:  # noqa: ANN001
    persisted: list[str] = []
    config = SimpleNamespace(personification_qzone_cookie="old-cookie")
    monkeypatch.setattr(qzone_service, "_persist_cookie_to_env", lambda cookie, _logger: persisted.append(cookie))

    async def probe(_cookie: str, qq: str, p_skey: str) -> tuple[bool, str]:
        assert qq == "99999"
        assert p_skey == "p-secret"
        return True, "ok"

    ok, message = asyncio.run(
        qzone_service.install_qzone_cookie(
            cookie="pt_login_sig=drop; p_uin=o99999; uin=o99999; p_skey=p-secret; skey=s-key;",
            expected_bot_id="99999",
            plugin_config=config,
            logger=_Logger(),
            source="manual",
            probe=probe,
        )
    )
    assert (ok, message) == (True, "ok")
    assert persisted == ["uin=o99999; p_uin=o99999; skey=s-key; p_skey=p-secret;"]
    assert config.personification_qzone_cookie == persisted[0]
    assert qzone_service.get_qzone_auth_status("99999")["status"] == "healthy"
    assert qzone_service.get_qzone_auth_status("10000")["status"] == "unknown"
    capabilities = qzone_service.get_qzone_capability_status("99999", enabled=True)
    assert capabilities["qzone.web_read"]["state"] == "available"
    assert capabilities["qzone.web_write"]["state"] == "unknown"
    assert capabilities["read_only"] is True

    mismatch = asyncio.run(
        qzone_service.install_qzone_cookie(
            cookie="uin=o10000; p_skey=p-secret;",
            expected_bot_id="99999",
            plugin_config=config,
            logger=_Logger(),
            source="manual",
            probe=probe,
        )
    )
    assert mismatch == (False, "account_mismatch")
    assert persisted == ["uin=o99999; p_uin=o99999; skey=s-key; p_skey=p-secret;"]


def test_cookie_recovery_reopens_only_auth_degraded_write_for_manual_probe(monkeypatch) -> None:  # noqa: ANN001
    config = SimpleNamespace(personification_qzone_cookie="")
    monkeypatch.setattr(qzone_service, "_persist_cookie_to_env", lambda *_args: None)

    async def probe(_cookie: str, _qq: str, _p_skey: str) -> tuple[bool, str]:
        return True, "ok"

    qzone_service._set_qzone_auth_failure(
        "expired",
        auth_failure=True,
        bot_id="99999",
    )
    before = qzone_service.get_qzone_capability_status("99999", enabled=True)
    assert before["qzone.web_write"]["state"] == "unavailable"
    ok, _message = asyncio.run(
        qzone_service.install_qzone_cookie(
            cookie="uin=o99999; p_skey=p-secret;",
            expected_bot_id="99999",
            plugin_config=config,
            logger=_Logger(),
            source="manual",
            probe=probe,
        )
    )
    recovered = qzone_service.get_qzone_capability_status("99999", enabled=True)
    assert ok is True
    assert recovered["qzone.web_read"]["state"] == "available"
    assert recovered["qzone.web_write"]["state"] == "unknown"
    assert recovered["qzone.web_write"]["reason_code"] == "auth_recovered_write_unverified"

    qzone_service._set_qzone_capability(
        "99999",
        "qzone.web_write",
        "unavailable",
        "explicit_failure",
    )
    asyncio.run(
        qzone_service.install_qzone_cookie(
            cookie="uin=o99999; p_skey=p-secret;",
            expected_bot_id="99999",
            plugin_config=config,
            logger=_Logger(),
            source="manual",
            probe=probe,
        )
    )
    explicit = qzone_service.get_qzone_capability_status("99999", enabled=True)
    assert explicit["qzone.web_write"]["state"] == "unavailable"
    assert explicit["qzone.web_write"]["reason_code"] == "explicit_failure"
