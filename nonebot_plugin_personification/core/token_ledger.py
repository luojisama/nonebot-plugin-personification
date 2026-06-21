from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from .db import connect_sync
from .llm_context import current_llm_context


def record_response_usage(
    response: Any,
    *,
    purpose: str = "",
    model_fallback: str = "",
) -> None:
    """便捷 helper：拿到 ToolCallerResponse 后调一次，自动从 llm_context 取 group/user/purpose。

    用法：
        from ..core.llm_context import set_llm_context, reset_llm_context
        from ..core.token_ledger import record_response_usage

        token = set_llm_context(purpose="user_persona", user_id=uid)
        try:
            response = await tool_caller.chat_with_tools(...)
            record_response_usage(response, model_fallback=tool_caller.model)
        finally:
            reset_llm_context(token)

    `purpose` 参数为空时从 contextvar 读，让 callers 既能 set_llm_context（推荐），
    也能直接调 record_response_usage(response, purpose="...") 一行搞定。
    """
    try:
        usage = getattr(response, "usage", None) or {}
        if not isinstance(usage, dict):
            return
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        if prompt_tokens == 0 and completion_tokens == 0:
            return
        ctx = current_llm_context()
        record_llm_call(
            model=str(getattr(response, "model_used", "") or model_fallback or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            group_id=str(ctx.get("group_id", "") or ""),
            user_id=str(ctx.get("user_id", "") or ""),
            purpose=str(purpose or ctx.get("purpose", "") or "direct_call"),
        )
    except Exception:
        pass


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _day_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _infer_provider(model: str, explicit: str = "") -> str:
    """从 explicit 参数或 model 名推导 provider 标签。"""
    if explicit:
        return str(explicit).strip().lower()
    name = str(model or "").lower()
    if "claude" in name or "anthropic" in name:
        return "anthropic"
    if "gemini" in name:
        return "gemini"
    if "gpt" in name or "openai" in name or name.startswith("o1") or name.startswith("o3"):
        return "openai"
    if "codex" in name:
        return "codex"
    return ""


def record_llm_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    group_id: str = "",
    user_id: str = "",
    purpose: str = "",
    provider: str = "",
    bucket_day: str | None = None,
) -> None:
    """记录一次 LLM 调用，按 (day, group, user, model, purpose) 桶累加。
    `provider` 显式提供时优先；否则从 model 名推导（anthropic/gemini/openai/codex）。
    purpose 内已编码 provider 信息：写入时实际 purpose=`{original}|provider={p}`，
    查询时按子串匹配（简单 schema 兼容）。
    """
    bucket = str(bucket_day or _today())
    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    if pt == 0 and ct == 0:
        return
    tt = pt + ct
    resolved_provider = _infer_provider(model, provider)
    # 把 provider 编码到 purpose 字段（向后兼容，不改 schema）
    purpose_str = str(purpose or "")
    if resolved_provider and "provider=" not in purpose_str:
        purpose_str = f"{purpose_str}|provider={resolved_provider}" if purpose_str else f"provider={resolved_provider}"
    now = time.time()
    with connect_sync() as conn:
        conn.execute(
            """
            INSERT INTO token_usage_ledger
                (bucket_day, group_id, user_id, model, purpose,
                 prompt_tokens, completion_tokens, total_tokens, call_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(bucket_day, group_id, user_id, model, purpose) DO UPDATE SET
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                call_count = call_count + 1,
                updated_at = excluded.updated_at
            """,
            (bucket, str(group_id or ""), str(user_id or ""), str(model or ""),
             purpose_str, pt, ct, tt, now),
        )
        conn.commit()


def query_provider_summary(window: str = "month") -> dict[str, Any]:
    """按 provider 维度聚合最近窗口的 token 用量。返回 {provider: totals}。
    provider 从 purpose 字段中的 `provider=xxx` 子串解析，或从 model 名兜底推导。
    """
    start_str = _day_str(_range_start(window))
    providers: dict[str, dict[str, int]] = {}
    with connect_sync() as conn:
        rows = conn.execute(
            """
            SELECT model, purpose,
                   SUM(prompt_tokens) AS pt,
                   SUM(completion_tokens) AS ct,
                   SUM(total_tokens) AS tt,
                   SUM(call_count) AS cc
            FROM token_usage_ledger
            WHERE bucket_day >= ?
            GROUP BY model, purpose
            """,
            (start_str,),
        ).fetchall()
    for row in rows:
        model = str(row["model"] or "")
        purpose = str(row["purpose"] or "")
        # 优先从 purpose 解析 provider=xxx
        provider = ""
        if "provider=" in purpose:
            for part in purpose.split("|"):
                if part.startswith("provider="):
                    provider = part[len("provider="):].strip().lower()
                    break
        if not provider:
            provider = _infer_provider(model) or "unknown"
        bucket = providers.setdefault(provider, {
            "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "call_count": 0,
        })
        bucket["prompt_tokens"] += int(row["pt"] or 0)
        bucket["completion_tokens"] += int(row["ct"] or 0)
        bucket["total_tokens"] += int(row["tt"] or 0)
        bucket["call_count"] += int(row["cc"] or 0)
    return {
        "window": window,
        "start_day": start_str,
        "providers": [
            {"provider": p, **vals}
            for p, vals in sorted(providers.items(), key=lambda kv: -kv[1]["total_tokens"])
        ],
    }


def _range_start(window: str) -> datetime:
    now = datetime.now()
    today = datetime(now.year, now.month, now.day)
    if window == "day":
        return today
    if window == "week":
        return today - timedelta(days=6)
    if window == "month":
        return today - timedelta(days=29)
    return today - timedelta(days=29)


def query_summary(window: str = "month") -> dict[str, Any]:
    """返回当前窗口的总 token 数 + 按 day/model/group 的分布。"""
    start = _range_start(window)
    start_str = _day_str(start)
    with connect_sync() as conn:
        total_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(call_count), 0) AS call_count
            FROM token_usage_ledger
            WHERE bucket_day >= ?
            """,
            (start_str,),
        ).fetchone()
        by_day = conn.execute(
            """
            SELECT bucket_day,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE bucket_day >= ?
            GROUP BY bucket_day
            ORDER BY bucket_day DESC
            """,
            (start_str,),
        ).fetchall()
        by_model = conn.execute(
            """
            SELECT model,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE bucket_day >= ? AND model != ''
            GROUP BY model
            ORDER BY total_tokens DESC
            """,
            (start_str,),
        ).fetchall()
        by_group = conn.execute(
            """
            SELECT group_id,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE bucket_day >= ? AND group_id != ''
            GROUP BY group_id
            ORDER BY total_tokens DESC
            LIMIT 50
            """,
            (start_str,),
        ).fetchall()
        # purpose 维度：从 purpose 字段抽 functional 部分（剥离 `|provider=xxx` 尾巴）
        by_purpose_rows = conn.execute(
            """
            SELECT purpose,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE bucket_day >= ? AND purpose != ''
            GROUP BY purpose
            ORDER BY total_tokens DESC
            LIMIT 50
            """,
            (start_str,),
        ).fetchall()
    by_purpose_agg: dict[str, dict[str, int]] = {}
    for row in by_purpose_rows:
        raw_purpose = str(row["purpose"] or "")
        # 提取 functional 段（如 `user_persona|provider=openai` → `user_persona`）
        functional = raw_purpose.split("|", 1)[0].strip() or "unknown"
        bucket_row = by_purpose_agg.setdefault(
            functional,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0},
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"):
            bucket_row[key] = int(bucket_row[key]) + int(row[key] or 0)
    by_purpose_list = [
        {"purpose": k, **v}
        for k, v in sorted(by_purpose_agg.items(), key=lambda kv: -kv[1]["total_tokens"])
    ]
    return {
        "window": window,
        "start_day": start_str,
        "total": _row_to_dict(total_row, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count")),
        "by_day": [_row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="bucket_day") for r in by_day],
        "by_model": [_row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="model") for r in by_model],
        "by_group": [_row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="group_id") for r in by_group],
        "by_purpose": by_purpose_list,
    }


def query_group_detail(group_id: str, window: str = "month") -> dict[str, Any]:
    """单个群在窗口内按 day/model 的明细。"""
    start_str = _day_str(_range_start(window))
    with connect_sync() as conn:
        rows = conn.execute(
            """
            SELECT bucket_day, model,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE bucket_day >= ? AND group_id = ?
            GROUP BY bucket_day, model
            ORDER BY bucket_day ASC, total_tokens DESC
            """,
            (start_str, str(group_id or "")),
        ).fetchall()
    return {
        "group_id": str(group_id or ""),
        "window": window,
        "rows": [
            {
                "bucket_day": str(r["bucket_day"]),
                "model": str(r["model"]),
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "call_count": int(r["call_count"] or 0),
            }
            for r in rows
        ],
    }


def _row_to_dict(row: Any, fields: tuple[str, ...], *, key: str | None = None) -> dict[str, Any]:
    if row is None:
        out = {f: 0 for f in fields}
        if key:
            out[key] = ""
        return out
    result = {f: int(row[f] or 0) for f in fields}
    if key:
        result[key] = str(row[key] or "")
    return result


__all__ = ["record_llm_call", "query_summary", "query_group_detail", "query_provider_summary"]
