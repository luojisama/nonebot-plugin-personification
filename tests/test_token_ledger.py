from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _ledger(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))
    return load_personification_module("plugin.personification.core.token_ledger")


def test_record_aggregates_same_bucket(_ledger) -> None:
    ledger = _ledger
    ledger.record_llm_call(model="gpt-x", prompt_tokens=100, completion_tokens=50, group_id="g1", user_id="u1", purpose="chat")
    ledger.record_llm_call(model="gpt-x", prompt_tokens=200, completion_tokens=80, group_id="g1", user_id="u1", purpose="chat")
    summary = ledger.query_summary("month")
    assert summary["total"]["prompt_tokens"] == 300
    assert summary["total"]["completion_tokens"] == 130
    assert summary["total"]["total_tokens"] == 430
    assert summary["total"]["call_count"] == 2


def test_record_separates_different_models(_ledger) -> None:
    ledger = _ledger
    ledger.record_llm_call(model="gpt-a", prompt_tokens=100, completion_tokens=50)
    ledger.record_llm_call(model="gpt-b", prompt_tokens=200, completion_tokens=80)
    summary = ledger.query_summary("month")
    models = {row["model"]: row for row in summary["by_model"]}
    assert models["gpt-a"]["total_tokens"] == 150
    assert models["gpt-b"]["total_tokens"] == 280


def test_record_zero_tokens_skipped(_ledger) -> None:
    ledger = _ledger
    ledger.record_llm_call(model="x", prompt_tokens=0, completion_tokens=0)
    summary = ledger.query_summary("month")
    assert summary["total"]["call_count"] == 0


def test_group_detail_filters_by_group(_ledger) -> None:
    ledger = _ledger
    ledger.record_llm_call(model="m", prompt_tokens=10, completion_tokens=5, group_id="g1")
    ledger.record_llm_call(model="m", prompt_tokens=20, completion_tokens=8, group_id="g2")
    detail = ledger.query_group_detail("g1", "month")
    assert len(detail["rows"]) == 1
    assert detail["rows"][0]["total_tokens"] == 15


def test_summary_by_group_excludes_empty(_ledger) -> None:
    ledger = _ledger
    ledger.record_llm_call(model="m", prompt_tokens=10, completion_tokens=5)  # no group
    ledger.record_llm_call(model="m", prompt_tokens=20, completion_tokens=8, group_id="g1")
    summary = ledger.query_summary("month")
    group_ids = [row["group_id"] for row in summary["by_group"]]
    assert "g1" in group_ids
    assert "" not in group_ids


def test_day_summary_uses_full_24_hour_buckets(_ledger) -> None:
    ledger = _ledger
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    first_hour = current_hour - timedelta(hours=23)
    middle_hour = current_hour - timedelta(hours=5)

    ledger.record_llm_call(
        model="gpt-early",
        prompt_tokens=10,
        completion_tokens=5,
        group_id="g1",
        bucket_hour=first_hour.strftime("%Y-%m-%d %H:00"),
    )
    ledger.record_llm_call(
        model="gpt-middle",
        prompt_tokens=20,
        completion_tokens=8,
        group_id="g2",
        bucket_hour=middle_hour.strftime("%Y-%m-%d %H:00"),
    )
    ledger.record_llm_call(
        model="gpt-now",
        prompt_tokens=30,
        completion_tokens=12,
        group_id="g1",
        bucket_hour=current_hour.strftime("%Y-%m-%d %H:00"),
    )

    summary = ledger.query_summary("24h")

    assert summary["window"] == "day"
    assert summary["start_hour"] == first_hour.strftime("%Y-%m-%d %H:00")
    assert len(summary["series"]) == 24
    assert summary["series"][0]["bucket"] == first_hour.strftime("%Y-%m-%d %H:00")
    assert summary["series"][-1]["bucket"] == current_hour.strftime("%Y-%m-%d %H:00")
    series_by_bucket = {row["bucket"]: row for row in summary["series"]}
    assert series_by_bucket[first_hour.strftime("%Y-%m-%d %H:00")]["total_tokens"] == 15
    assert series_by_bucket[middle_hour.strftime("%Y-%m-%d %H:00")]["total_tokens"] == 28
    assert series_by_bucket[current_hour.strftime("%Y-%m-%d %H:00")]["total_tokens"] == 42
    assert summary["total"]["total_tokens"] == 15 + 28 + 42
    assert {row["model"] for row in summary["by_model"]} == {"gpt-early", "gpt-middle", "gpt-now"}

    detail = ledger.query_group_detail("g1", "day")
    assert {row["bucket_hour"] for row in detail["rows"]} == {
        first_hour.strftime("%Y-%m-%d %H:00"),
        current_hour.strftime("%Y-%m-%d %H:00"),
    }
