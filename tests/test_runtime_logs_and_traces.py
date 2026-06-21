from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


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
        message="api_key=secret-value token=abc123 normal=ok",
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
    assert row["context"]["Authorization"] == "***"
    assert row["context"]["nested"]["cookie"] == "***"

    assert logs.query_recent(limit=10, level="ERROR") == []
    assert logs.clear_all() == 1
    assert logs.query_recent(limit=10) == []


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
