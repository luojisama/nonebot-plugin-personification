from __future__ import annotations

import sys

from ._loader import load_personification_module

# 预加载 data_store 让 provider_health 的相对 import 找得到
load_personification_module("plugin.personification.core.db")
ph = load_personification_module("plugin.personification.core.provider_health")


# ========== classify_error ==========

def test_classify_error_recognizes_timeout() -> None:
    assert ph.classify_error(RuntimeError("Read timeout after 30s")) == "timeout"
    assert ph.classify_error(RuntimeError("operation timed out")) == "timeout"
    assert ph.classify_error(TimeoutError()) == "timeout"


def test_classify_error_recognizes_rate_limit() -> None:
    assert ph.classify_error(RuntimeError("HTTP 429 too many requests")) == "rate_limit"
    assert ph.classify_error(RuntimeError("rate limit exceeded")) == "rate_limit"


def test_classify_error_recognizes_connect() -> None:
    assert ph.classify_error(RuntimeError("ConnectError: TLS handshake failed")) == "connect"


def test_classify_error_uses_status_code_when_present() -> None:
    class _FakeResp:
        status_code = 503

    class _FakeExc(Exception):
        response = _FakeResp()

    assert ph.classify_error(_FakeExc("server bad")) == "5xx"


def test_classify_error_other_fallback() -> None:
    assert ph.classify_error(ValueError("nothing matched")) == "other"


# ========== compute_effective_priority ==========

def test_effective_priority_returns_base_when_disabled() -> None:
    provider = {"priority": 5}
    stats = {"sample_count": 100, "success_count": 0, "avg_latency_ms": 60000}
    assert ph.compute_effective_priority(provider, stats, enabled=False) == 5.0


def test_effective_priority_returns_base_when_under_min_samples() -> None:
    provider = {"priority": 2}
    stats = {"sample_count": 1, "success_count": 0, "avg_latency_ms": 60000}
    # 即便 stats 极差，样本不足仍走 base
    assert ph.compute_effective_priority(provider, stats, min_samples=3) == 2.0


def test_effective_priority_no_penalty_for_fast_and_perfect_provider() -> None:
    provider = {"priority": 0}
    stats = {"sample_count": 10, "success_count": 10, "avg_latency_ms": 500}
    # 500ms < 1000ms：无 latency 惩罚；100% 成功：无 failure 惩罚
    assert ph.compute_effective_priority(provider, stats, min_samples=3) == 0.0


def test_effective_priority_latency_penalty_grows() -> None:
    provider = {"priority": 0}
    # 2000ms → 2 个 500ms 步长 → latency_penalty = 2.0
    stats = {"sample_count": 10, "success_count": 10, "avg_latency_ms": 2000}
    assert ph.compute_effective_priority(provider, stats, min_samples=3) == 2.0


def test_effective_priority_failure_penalty_grows() -> None:
    provider = {"priority": 0}
    # success_rate = 0.5 → failure_penalty = 5.0
    stats = {"sample_count": 10, "success_count": 5, "avg_latency_ms": 500}
    assert ph.compute_effective_priority(provider, stats, min_samples=3) == 5.0


def test_effective_priority_high_latency_low_success_combine() -> None:
    provider = {"priority": 1}
    stats = {"sample_count": 10, "success_count": 0, "avg_latency_ms": 5500}
    # base=1, latency_penalty=(5500-1000)/500=9,
    # failure_penalty=(1-0)*10 + never-success penalty 100
    assert ph.compute_effective_priority(provider, stats, min_samples=3) == 1 + 9 + 10 + 100


def test_effective_priority_ordering_swaps_when_top_dies() -> None:
    """模拟"top priority provider 挂了应该排到 healthy provider 之后"。"""
    top = {"priority": 0, "name": "primary"}
    backup = {"priority": 5, "name": "backup"}
    top_stats = {"sample_count": 20, "success_count": 0, "avg_latency_ms": 8000}
    backup_stats = {"sample_count": 20, "success_count": 20, "avg_latency_ms": 600}
    top_eff = ph.compute_effective_priority(top, top_stats, min_samples=3)
    backup_eff = ph.compute_effective_priority(backup, backup_stats, min_samples=3)
    # top: 0 + 14 + 10 + 100 = 124；backup: 5 + 0 + 0 = 5；backup 排前面
    assert top_eff > backup_eff


def test_effective_priority_never_success_fast_failure_after_slow_healthy() -> None:
    """失败很快的 provider 不应排在稳定成功但较慢的 provider 前面。"""
    failing = {"priority": 2, "name": "antigravity_cli_primary"}
    healthy = {"priority": 1, "name": "mimo"}
    failing_stats = {"sample_count": 22, "success_count": 0, "avg_latency_ms": 1584}
    healthy_stats = {"sample_count": 12, "success_count": 12, "avg_latency_ms": 10706}

    failing_eff = ph.compute_effective_priority(failing, failing_stats, min_samples=3)
    healthy_eff = ph.compute_effective_priority(healthy, healthy_stats, min_samples=3)

    assert healthy_eff < failing_eff


# ========== compute_cooldown_seconds ==========

def test_cooldown_exponential_backoff() -> None:
    # 单次失败：基础 60s，±20% jitter
    cd = ph.compute_cooldown_seconds(1, is_rate_limit=False)
    assert 48 <= cd <= 72, f"first cooldown out of range: {cd}"


def test_cooldown_grows_with_failure_count() -> None:
    # 失败 3 次：基础 240s
    cd = ph.compute_cooldown_seconds(3, is_rate_limit=False)
    assert 192 <= cd <= 288, f"third cooldown out of range: {cd}"


def test_cooldown_capped_at_30_minutes() -> None:
    # 失败 20 次：理论 2**19 * 60 远超封顶，应该被 cap 到 1800s ±20%
    cd = ph.compute_cooldown_seconds(20, is_rate_limit=False)
    assert 1440 <= cd <= 2160, f"capped cooldown out of range: {cd}"


def test_cooldown_rate_limit_uses_600s_base() -> None:
    cd = ph.compute_cooldown_seconds(1, is_rate_limit=True)
    assert 480 <= cd <= 720, f"rate-limit base cooldown out of range: {cd}"


def test_cooldown_rate_limit_respects_retry_after() -> None:
    cd = ph.compute_cooldown_seconds(1, is_rate_limit=True, retry_after=1200.0)
    # retry_after 1200 > 600 → 取 1200，±20% jitter
    assert 960 <= cd <= 1440, f"retry_after cooldown out of range: {cd}"


# ========== record_request_result + get_stats（端到端 SQLite，需 stub） ==========

def _stub_db_with_table(monkeypatch, tmp_path):
    """构造一个独立的 sqlite 文件，prep table。"""
    import sqlite3

    db_path = tmp_path / "test_provider_health.db"

    def _stub_connect_sync(_path=None):
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_health_stats (
                provider_name TEXT PRIMARY KEY,
                sample_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                avg_latency_ms REAL NOT NULL DEFAULT 0,
                last_request_at REAL,
                last_success_at REAL,
                last_failure_at REAL,
                last_error_kind TEXT NOT NULL DEFAULT '',
                last_seen_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        return conn

    monkeypatch.setattr(ph, "connect_sync", _stub_connect_sync)


def test_record_first_call_creates_row(tmp_path, monkeypatch) -> None:
    _stub_db_with_table(monkeypatch, tmp_path)
    ph.record_request_result(
        provider_name="p1", latency_ms=500, success=True, error_kind=""
    )
    stats = ph.get_stats("p1")
    assert stats is not None
    assert stats["sample_count"] == 1
    assert stats["success_count"] == 1
    assert stats["failure_count"] == 0
    assert stats["avg_latency_ms"] == 500.0


def test_record_ema_smooths_subsequent_calls(tmp_path, monkeypatch) -> None:
    _stub_db_with_table(monkeypatch, tmp_path)
    ph.record_request_result(provider_name="p2", latency_ms=1000, success=True)
    # EMA: α=0.3, 老值 1000, 新 2000 → 0.3*2000 + 0.7*1000 = 1300
    ph.record_request_result(provider_name="p2", latency_ms=2000, success=True)
    stats = ph.get_stats("p2")
    assert stats["sample_count"] == 2
    assert abs(stats["avg_latency_ms"] - 1300.0) < 0.01


def test_record_failure_increments_failure_count(tmp_path, monkeypatch) -> None:
    _stub_db_with_table(monkeypatch, tmp_path)
    ph.record_request_result(provider_name="p3", latency_ms=300, success=True)
    ph.record_request_result(provider_name="p3", latency_ms=60000, success=False, error_kind="timeout")
    stats = ph.get_stats("p3")
    assert stats["sample_count"] == 2
    assert stats["success_count"] == 1
    assert stats["failure_count"] == 1
    assert stats["last_error_kind"] == "timeout"


def test_reset_stats_removes_row(tmp_path, monkeypatch) -> None:
    _stub_db_with_table(monkeypatch, tmp_path)
    ph.record_request_result(provider_name="px", latency_ms=100, success=True)
    assert ph.get_stats("px") is not None
    assert ph.reset_stats("px") is True
    assert ph.get_stats("px") is None
