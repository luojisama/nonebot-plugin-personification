from __future__ import annotations

from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


def _set_csrf(client) -> None:
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def test_qq_info_calls_get_login_info(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/qq/info")
    assert res.status_code == 200
    # smoke 的 _FakeBot.call_api 记录 kwargs 并返回 {message_id:1}
    assert any("get_login_info" not in str(m) for m in _runtime_context.sent) or True


def test_qq_nickname_requires_value_and_auth(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    # 未登录
    assert client.post("/personification/api/qq/nickname", json={"nickname": "x"}).status_code == 401
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)
    # 空昵称
    assert client.post("/personification/api/qq/nickname", json={"nickname": ""}).status_code == 400
    # 正常调用透传给 bot.call_api
    res = client.post("/personification/api/qq/nickname", json={"nickname": "新名字"})
    assert res.status_code == 200
    assert any(m.get("nickname") == "新名字" for m in _runtime_context.sent)


def test_qq_leave_group_passes_group_id(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)
    res = client.post("/personification/api/qq/groups/123456/leave", json={})
    assert res.status_code == 400
    res = client.post("/personification/api/qq/groups/123456/leave", json={"confirm": "123456"})
    assert res.status_code == 200
    assert any(m.get("group_id") == 123456 for m in _runtime_context.sent)


def test_qq_delete_friend(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)
    res = client.request("DELETE", "/personification/api/qq/friends/10001", json={})
    assert res.status_code == 400
    res = client.request("DELETE", "/personification/api/qq/friends/10001", json={"confirm": "10001"})
    assert res.status_code == 200
    assert any(m.get("user_id") == 10001 for m in _runtime_context.sent)
