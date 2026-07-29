from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module

completion_contract = load_personification_module(
    "plugin.personification.core.reply_completion_contract"
)


@pytest.fixture
def _db_tmp(tmp_path: Path, monkeypatch):
    paths = load_personification_module("plugin.personification.core.paths")
    data_store = load_personification_module("plugin.personification.core.data_store")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)
    return cfg


def test_plugin_runtime_logs_sanitize_filter_and_clear(_db_tmp) -> None:
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()

    logs.record(
        level="INFO",
        source="unit",
        message=(
            "api_key=secret-value token=abc123 normal=ok\n"
            "Authorization: Bearer real-bearer-secret\n"
            '{"client_secret":"json-secret","password":"pass-secret"} '
            "https://example.test/?access_token=url-secret&p_skey=qzone-secret"
        ),
        context={"Authorization": "Bearer secret", "nested": {"cookie": "qq=1"}},
        trace_id="trace-1",
        min_level="DEBUG",
    )
    logs.record(
        level="DEBUG",
        source="unit",
        message="debug hidden by level",
        min_level="INFO",
    )

    rows = logs.query_recent(limit=10, q="trace-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["trace_id"] == "trace-1"
    assert "normal=ok" in row["message"]
    assert "secret-value" not in row["message"]
    assert "abc123" not in row["message"]
    for secret in ("real-bearer-secret", "json-secret", "pass-secret", "url-secret", "qzone-secret"):
        assert secret not in row["message"]
    assert row["context"]["Authorization"] == "***"
    assert row["context"]["nested"]["cookie"] == "***"

    assert logs.query_recent(limit=10, level="ERROR") == []
    assert logs.clear_all() == 1
    assert logs.query_recent(limit=10) == []


def test_plugin_runtime_logs_cursor_page_contract(_db_tmp) -> None:
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()
    for index in range(5):
        logs.record(level="INFO", source="unit", message=f"page-item-{index}", min_level="DEBUG")

    first = logs.query_page(limit=2)
    second = logs.query_page(limit=2, cursor=first["next_cursor"])
    third = logs.query_page(limit=2, cursor=second["next_cursor"])

    assert [item["message"] for item in first["entries"]] == ["page-item-4", "page-item-3"]
    assert [item["message"] for item in second["entries"]] == ["page-item-2", "page-item-1"]
    assert [item["message"] for item in third["entries"]] == ["page-item-0"]
    assert first["has_more"] is True
    assert second["has_more"] is True
    assert third["has_more"] is False
    assert third["next_cursor"] == 0
    assert len({item["id"] for page in (first, second, third) for item in page["entries"]}) == 5
    assert logs.writer_status()["pending"] == 0


def test_plugin_runtime_logs_search_treats_wildcards_literally(_db_tmp) -> None:
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()
    logs.record(level="INFO", source="unit", message="literal 100%_done", min_level="DEBUG")
    logs.record(level="INFO", source="unit", message="literal 100XXdone", min_level="DEBUG")

    page = logs.query_page(limit=10, q="%_")

    assert [item["message"] for item in page["entries"]] == ["literal 100%_done"]
    assert page["filters"]["q"] == "%_"


def test_reply_turn_trace_records_and_finishes(_db_tmp) -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    trace_id = traces.start_trace(
        session_type="group",
        group_id="123",
        user_id="456",
        detail={"source": "unit"},
    )
    token = traces.set_current_trace_id(trace_id)
    try:
        traces.record_stage(key="ingress", label="进入", status="info", detail="hello token=abc")
        traces.finish_trace(outcome="ok", diagnosis_code="ok", detail={"reply_chars": 2})
    finally:
        traces.reset_current_trace_id(token)

    row = traces.get_trace(trace_id)
    assert row is not None
    assert row["session_type"] == "group"
    assert row["group_id"] == "123"
    assert row["user_id"] == "456"
    assert row["outcome"] == "ok"
    assert row["diagnosis_code"] == "ok"
    assert row["detail"]["reply_chars"] == 2
    assert row["stages"][0]["key"] == "ingress"
    assert "abc" not in row["stages"][0]["detail"]

    recent = traces.query_recent(session_type="group", group_id="123", user_id="456")
    assert recent and recent[0]["trace_id"] == trace_id


@pytest.mark.parametrize(
    ("state", "delivery_partial", "delivery_unknown", "outcome", "diagnosis"),
    [
        (
            {
                "agent_evidence_delivery_required": True,
                "agent_evidence_delivery_status": "met",
                "agent_social_tool_execution": "ok",
                "agent_social_coverage_status": "complete",
            },
            False,
            False,
            "ok",
            "ok",
        ),
        (
            {
                "agent_evidence_delivery_required": True,
                "agent_evidence_delivery_status": "recovered",
                "agent_evidence_recovered": True,
                "agent_social_tool_execution": "ok",
            },
            False,
            False,
            "partial",
            "visible_output_recovered",
        ),
        (
            {
                "agent_evidence_delivery_required": True,
                "agent_evidence_delivery_status": "failed",
            },
            False,
            False,
            "partial",
            "evidence_delivery_incomplete",
        ),
        ({}, False, True, "failed", "outbound_send_failed"),
    ],
)
def test_reply_completion_contract_separates_evidence_and_outbound_states(
    state, delivery_partial, delivery_unknown, outcome, diagnosis
) -> None:
    resolved = completion_contract.resolve_sent_reply_completion(
        state=state,
        visible_text="已发送",
        delivery_partial=delivery_partial,
        delivery_unknown=delivery_unknown,
    )

    assert resolved["outcome"] == outcome
    assert resolved["diagnosis_code"] == diagnosis
    assert resolved["outbound_delivery"] == ("unconfirmed" if delivery_unknown else "confirmed")


def test_reply_turn_trace_builds_safe_process_view(_db_tmp) -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()

    trace_id = traces.start_trace(session_type="group", group_id="123", user_id="456")
    token = traces.set_current_trace_id(trace_id)
    try:
        traces.record_stage(
            key="agent_model_step",
            label="Agent 模型步 1",
            status="ok",
            detail="action=reply speech_act=participate finish=tool_calls elapsed_ms=1500 token=abc123",
        )
        traces.record_stage(
            key="agent_tool_result",
            label="Agent 工具结果",
            status="warn",
            detail="tool=web_search result_len=0 elapsed_ms=80",
        )
        traces.finish_trace(outcome="no_reply", diagnosis_code="agent_no_reply")
    finally:
        traces.reset_current_trace_id(token)
    logs.record(level="WARNING", source="unit", message="slow stage", trace_id=trace_id, min_level="DEBUG")

    row = traces.get_trace(trace_id)
    view = traces.build_process_view(row, logs=logs.query_recent(trace_id=trace_id))

    assert view["summary"]["trace_id"] == trace_id
    assert view["summary"]["warn_count"] == 1
    assert view["summary"]["log_levels"]["WARNING"] == 1
    assert view["items"][0]["category"] == "agent"
    assert view["items"][0]["duration_ms"] == 1500
    assert view["items"][0]["signals"]["speech_act"] == "participate"
    assert view["items"][1]["signals"]["tool"] == "web_search"


def test_reply_turn_trace_exposes_only_completion_contract_summary(_db_tmp) -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    trace_id = traces.start_trace(session_type="group", group_id="123", user_id="456")
    token = traces.set_current_trace_id(trace_id)
    try:
        traces.finish_trace(
            outcome="partial",
            diagnosis_code="visible_output_recovered",
            detail={
                "tool_execution": "partial",
                "evidence_delivery": "recovered",
                "outbound_delivery": "confirmed",
                "social_coverage_status": "degraded",
                "evidence_recovered": True,
                "raw_tool_result": "must-not-be-exposed",
            },
        )
    finally:
        traces.reset_current_trace_id(token)

    view = traces.build_process_view(traces.get_trace(trace_id))

    assert view["summary"]["completion"] == {
        "tool_execution": "partial",
        "evidence_delivery": "recovered",
        "outbound_delivery": "confirmed",
        "social_coverage_status": "degraded",
        "evidence_recovered": "True",
    }
    assert "raw_tool_result" not in view["summary"]["completion"]


def test_reply_turn_trace_extracts_budget_signals(_db_tmp) -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()
    trace_id = traces.start_trace(session_type="group", group_id="123", user_id="456")
    token = traces.set_current_trace_id(trace_id)
    try:
        traces.record_stage(
            key="agent_budget",
            label="Agent 预算模式",
            status="info",
            detail=(
                "budget=light_chat suggested_steps=2 actual_steps=10 "
                "suggested_seconds=18 actual_seconds=150 source=shadow"
            ),
        )
        traces.finish_trace(outcome="ok", diagnosis_code="ok")
    finally:
        traces.reset_current_trace_id(token)

    row = traces.get_trace(trace_id)
    view = traces.build_process_view(row, logs=logs.query_recent(trace_id=trace_id))

    assert view["items"][0]["signals"]["budget"] == "light_chat"
    assert view["items"][0]["signals"]["suggested_steps"] == "2"
    assert view["items"][0]["signals"]["actual_seconds"] == "150"
    assert view["items"][0]["signals"]["source"] == "shadow"
    assert view["items"][0]["category"] == "agent"
    assert view["summary"]["slow_stages"] == []


def test_reply_turn_trace_extracts_reply_quality_signals(_db_tmp) -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()
    trace_id = traces.start_trace(session_type="group", group_id="123", user_id="456")
    token = traces.set_current_trace_id(trace_id)
    try:
        traces.record_stage(
            key="agent_reply_quality",
            label="Agent 回复质量",
            status="warn",
            detail=(
                "action=rewritten source=model_stop flags=formulaic_tic,style_risk "
                "revision=true elapsed_ms=120 chars=12->10"
            ),
        )
        traces.finish_trace(outcome="ok", diagnosis_code="ok")
    finally:
        traces.reset_current_trace_id(token)

    row = traces.get_trace(trace_id)
    view = traces.build_process_view(row, logs=logs.query_recent(trace_id=trace_id))

    assert view["items"][0]["category"] == "agent"
    assert view["items"][0]["signals"]["action"] == "rewritten"
    assert view["items"][0]["signals"]["flags"] == "formulaic_tic,style_risk"
    assert view["items"][0]["signals"]["revision"] == "true"
    assert view["items"][0]["signals"]["chars"] == "12->10"


def test_reply_turn_trace_extracts_topic_state_signals(_db_tmp) -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()
    trace_id = traces.start_trace(session_type="group", group_id="123", user_id="456")
    token = traces.set_current_trace_id(trace_id)
    try:
        traces.record_stage(
            key="topic_state",
            label="短期话题状态",
            status="info",
            detail="topic_thread=ta topic_speaker=u1 reply_to_bot=true bot_in_thread=true parallel_threads=2 participants=3",
        )
        traces.finish_trace(outcome="ok", diagnosis_code="ok")
    finally:
        traces.reset_current_trace_id(token)

    row = traces.get_trace(trace_id)
    view = traces.build_process_view(row, logs=logs.query_recent(trace_id=trace_id))

    assert view["items"][0]["signals"]["topic_thread"] == "ta"
    assert view["items"][0]["signals"]["reply_to_bot"] == "true"
    assert view["items"][0]["signals"]["parallel_threads"] == "2"


def test_reply_turn_trace_builds_agent_inspection_summary(_db_tmp) -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    logs.clear_all()
    trace_id = traces.start_trace(session_type="group", group_id="123", user_id="456")
    token = traces.set_current_trace_id(trace_id)
    try:
        traces.record_stage(
            key="semantic_frame",
            label="语义帧",
            status="ok",
            detail="intent=lookup ambiguity=low speech_act=source_summary output=source_summary address_mode=quote",
        )
        traces.record_stage(
            key="agent_query_rewrite",
            label="Agent 查询改写",
            status="ok",
            detail="query=大鸟居明日香_动画_剧情 elapsed_ms=20",
        )
        traces.record_stage(
            key="agent_tool_call",
            label="Agent 工具调用",
            status="ok",
            detail="tool=resolve_acg_entity elapsed_ms=120",
        )
        traces.record_stage(
            key="addressing_plan",
            label="发送指向",
            status="info",
            detail="address_mode=quote source=semantic_frame quote=true at=false target=- elapsed_ms=0",
        )
        traces.finish_trace(outcome="ok", diagnosis_code="ok")
    finally:
        traces.reset_current_trace_id(token)

    view = traces.build_process_view(traces.get_trace(trace_id), logs=logs.query_recent(trace_id=trace_id))
    inspection = view["agent_inspection"]

    assert inspection["understanding"]["intent"] == "lookup"
    assert inspection["addressing"]["address_mode"] == "quote"
    assert next(item for item in view["items"] if item["key"] == "addressing_plan")["duration_ms"] == 0
    assert inspection["tools"][0]["tool"] == "resolve_acg_entity"
    assert inspection["questions"][0] == "大鸟居明日香_动画_剧情"


def test_process_view_does_not_attribute_wait_time_to_zero_duration_markers() -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    view = traces.build_process_view(
        {
            "trace_id": "trace-duration",
            "outcome": "ok",
            "diagnosis_code": "ok",
            "stages": [
                {
                    "ts": 100.0,
                    "key": "vision_mode",
                    "label": "视觉路径",
                    "status": "info",
                    "detail": "mode=auto elapsed_ms=0",
                },
                {
                    "ts": 225.0,
                    "key": "semantic_frame_llm",
                    "label": "语义帧 LLM",
                    "status": "ok",
                    "detail": "intent=explanation elapsed_ms=7290",
                },
            ],
        }
    )

    assert view["items"][0]["duration_ms"] == 0
    assert view["items"][1]["duration_ms"] == 7290
    assert [item["key"] for item in view["summary"]["slow_stages"]] == ["semantic_frame_llm"]


def test_process_view_uses_explicit_stage_elapsed_without_changing_visible_detail() -> None:
    traces = load_personification_module("plugin.personification.core.reply_turn_trace")
    view = traces.build_process_view(
        {
            "trace_id": "trace-explicit-duration",
            "stages": [
                {
                    "ts": 100.0,
                    "key": "outgoing_message",
                    "label": "发送消息",
                    "status": "ok",
                    "detail": "实际可见回复",
                    "elapsed_ms": 0,
                },
                {
                    "ts": 102.5,
                    "key": "post_send_bookkeeping",
                    "label": "发送后状态写入",
                    "status": "ok",
                    "detail": "elapsed_ms=2500",
                },
            ],
        }
    )

    assert view["items"][0]["detail"] == "实际可见回复"
    assert view["items"][0]["duration_ms"] == 0
    assert view["items"][1]["duration_ms"] == 2500
