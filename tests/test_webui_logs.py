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
    assert body["has_more"] is False
    assert body["next_cursor"] == 0
    assert body["limit"] == 200
    assert body["filters"]["q"] == "trace-webui"
    assert body["writer"]["alive"] is True

    csrf = client.cookies.get("personification_webui_csrf", "")
    client.headers["X-Personification-CSRF"] = csrf
    clear = client.delete("/personification/api/logs/clear")
    assert clear.status_code == 200, clear.text
    assert clear.json()["deleted"] >= 1
    assert logs.query_recent(limit=10) == []


def test_webui_trace_detail_returns_process_view(_runtime_context) -> None:
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    logs.clear_all()
    trace_id = traces.start_trace(session_type="group", group_id="20001", user_id="10001")
    traces.record_stage(
        trace_id=trace_id,
        key="agent_tool_result",
        label="Agent 工具结果",
        status="ok",
        detail="speech_act=source_summary tool=wiki_lookup result_len=120 elapsed_ms=42",
    )
    traces.finish_trace(trace_id=trace_id, outcome="ok", diagnosis_code="ok")
    logs.record(level="INFO", source="unit", message="trace log", trace_id=trace_id, min_level="DEBUG")

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get(f"/personification/api/logs/trace/{trace_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["trace"]["trace_id"] == trace_id
    assert body["logs"][0]["trace_id"] == trace_id
    assert body["process"]["items"][0]["category"] == "tool"
    assert body["process"]["items"][0]["signals"]["speech_act"] == "source_summary"
    assert body["process"]["items"][0]["signals"]["tool"] == "wiki_lookup"
    assert body["process"]["summary"]["stage_count"] == 1


def test_webui_traces_list_returns_message_io_summary(_runtime_context) -> None:
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    logs.clear_all()
    trace_id = traces.start_trace(session_type="group", group_id="20001", user_id="10001")
    traces.record_stage(
        trace_id=trace_id,
        key="incoming_message",
        label="收到消息",
        status="info",
        detail="限时复活 这个动画牛逼",
    )
    traces.record_stage(
        trace_id=trace_id,
        key="outgoing_message",
        label="发送消息",
        status="ok",
        detail="大鸟居明日香那段演出确实很抓人。",
    )
    traces.finish_trace(trace_id=trace_id, outcome="ok", diagnosis_code="ok")

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/logs/traces", params={"limit": 10})
    assert res.status_code == 200, res.text
    body = res.json()
    entry = next(item for item in body["entries"] if item["trace_id"] == trace_id)
    assert entry["incoming_text"] == "限时复活 这个动画牛逼"
    assert entry["outgoing_text"] == "大鸟居明日香那段演出确实很抓人。"
    assert entry["stage_count"] == 2


def test_webui_plugin_logs_clear_requires_csrf(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    client.headers.pop("X-Personification-CSRF", None)
    res = client.delete("/personification/api/logs/clear")
    assert res.status_code == 403
