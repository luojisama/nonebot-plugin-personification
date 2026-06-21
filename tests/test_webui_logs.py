from __future__ import annotations

from ._loader import load_personification_module
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


def test_webui_plugin_logs_query_and_clear(_runtime_context) -> None:
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()
    logs.record(
        level="WARNING",
        source="unit",
        message="trace-visible message",
        trace_id="trace-webui",
        min_level="DEBUG",
    )

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/logs/recent", params={"q": "trace-webui"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["entries"]
    assert body["entries"][0]["trace_id"] == "trace-webui"

    csrf = client.cookies.get("personification_webui_csrf", "")
    client.headers["X-Personification-CSRF"] = csrf
    clear = client.delete("/personification/api/logs/clear")
    assert clear.status_code == 200, clear.text
    assert clear.json()["deleted"] >= 1
    assert logs.query_recent(limit=10) == []


def test_webui_plugin_logs_clear_requires_csrf(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    client.headers.pop("X-Personification-CSRF", None)
    res = client.delete("/personification/api/logs/clear")
    assert res.status_code == 403
