from __future__ import annotations

import re

from ._loader import load_personification_module

# 复用 smoke 测试的 fixture（初始化 data_store + 运行时上下文）
from .test_webui_smoke import _build_client, _runtime_context  # noqa: F401


def _extract_code(sent: list[dict]) -> str:
    msg = str(sent[-1].get("message", ""))
    match = re.search(r"\b(\d{6})\b", msg)
    assert match, f"应包含 6 位验证码：{msg}"
    return match.group(1)


def _login(client, ctx, qq: str = "10001", label: str = "dev"):
    client.post("/personification/api/auth/login", json={"qq": qq})
    code = _extract_code(ctx.sent)
    res = client.post(
        "/personification/api/auth/verify",
        json={"qq": qq, "code": code, "device_label": label},
    )
    # 后续 POST 需带 CSRF header（前端自动从 cookie 注入，测试里手动设置）
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf
    return res


def test_admin_code_approves_every_new_device_without_second_approval(_runtime_context) -> None:
    _runtime_context.plugin_config.personification_webui_require_device_approval = True

    # 首个设备：无任何已批准设备 → 自动批准（防锁死）
    c1 = _build_client(_runtime_context)
    r1 = _login(c1, _runtime_context, label="first")
    assert r1.status_code == 200
    assert r1.json().get("pending") is False
    assert c1.get("/personification/api/auth/me").status_code == 200

    # 第二个设备：管理员验证码通过后直接批准，不再等待旧设备二次确认
    c2 = _build_client(_runtime_context)
    r2 = _login(c2, _runtime_context, label="second")
    assert r2.status_code == 200
    assert r2.json().get("pending") is False
    assert c2.get("/personification/api/auth/me").status_code == 200
    # 新流程不产生待审批记录
    assert c1.get("/personification/api/auth/pending-devices").json()["devices"] == []


def test_approval_disabled_keeps_legacy_auto_login(_runtime_context) -> None:
    _runtime_context.plugin_config.personification_webui_require_device_approval = False
    c1 = _build_client(_runtime_context)
    assert _login(c1, _runtime_context, label="a").json().get("pending") is False
    c2 = _build_client(_runtime_context)
    r2 = _login(c2, _runtime_context, label="b")
    # 审批关闭时新设备直接可用
    assert r2.json().get("pending") is False
    assert c2.get("/personification/api/auth/me").status_code == 200
