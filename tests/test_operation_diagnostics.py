from __future__ import annotations

import asyncio

from ._loader import load_personification_module

operation_diagnostics = load_personification_module("plugin.personification.core.operation_diagnostics")


def test_diagnostic_serializes_steps_and_redacts_secrets() -> None:
    payload = operation_diagnostics.diagnostic(
        ok=False,
        code="provider_failed",
        phase="generation",
        title="草稿生成失败",
        message="Authorization: Bearer top-secret",
        details=(
            operation_diagnostics.detail("Provider", "main", "error"),
            operation_diagnostics.detail("上下文", {"p_skey": "cookie-secret", "attempt": 5}),
        ),
        steps=(
            operation_diagnostics.step(
                "generate",
                "生成草稿",
                "error",
                "API key=provider-secret",
            ),
        ),
        suggestion="检查 Provider 配置",
        retryable=True,
        operation_id="op-1",
        trace_id="trace-1",
    )

    rendered = str(payload)
    assert payload["code"] == "provider_failed"
    assert payload["steps"][0]["status"] == "error"
    assert payload["retryable"] is True
    assert payload["error"] == payload["message"]
    assert "top-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "provider-secret" not in rendered


def test_exception_diagnostic_classifies_timeout_without_raw_exception() -> None:
    exc = asyncio.TimeoutError("https://api.example.test/?access_token=secret")
    payload = operation_diagnostics.exception_diagnostic(
        exc,
        phase="provider_call",
        operation_id="op-timeout",
    )

    assert payload["code"] == "operation_timeout"
    assert payload["retryable"] is True
    assert payload["operation_id"] == "op-timeout"
    assert payload["trace_id"]
    assert "api.example" not in str(payload)


def test_normalize_diagnostic_preserves_safe_contract() -> None:
    payload = operation_diagnostics.normalize_diagnostic({
        "ok": False,
        "code": "partial_write",
        "phase": "persist",
        "title": "只完成了一部分",
        "message": "文件已移动，但索引保存失败",
        "partial": True,
        "outcome_unknown": True,
        "details": [{"label": "文件", "value": "已移动", "status": "ok"}],
    })

    assert payload["partial"] is True
    assert payload["outcome_unknown"] is True
    assert payload["details"] == [{"label": "文件", "value": "已移动", "status": "ok"}]
