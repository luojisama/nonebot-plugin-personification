from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from .db import connect_sync
from .llm_context import current_llm_context

_WINDOW_ALIASES = {
    "24h": "day",
    "day": "day",
    "7d": "week",
    "week": "week",
    "30d": "month",
    "month": "month",
}


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


def _hour_str(dt: datetime) -> str:
    return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:00")


def _normalize_bucket_values(
    *,
    bucket_day: str | None = None,
    bucket_hour: str | None = None,
) -> tuple[str, str]:
    raw_hour = str(bucket_hour or "").strip()
    if raw_hour:
        try:
            hour = datetime.strptime(raw_hour[:13], "%Y-%m-%d %H")
            return _day_str(hour), _hour_str(hour)
        except Exception:
            pass

    raw_day = str(bucket_day or "").strip()
    if raw_day:
        day = raw_day[:10]
        return day, f"{day} 00:00"

    now = datetime.now()
    return _day_str(now), _hour_str(now)


def normalize_window(window: str) -> str:
    return _WINDOW_ALIASES.get(str(window or "").strip().lower(), "month")


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


def _provider_from_model_purpose(model: str, purpose: str) -> str:
    provider = ""
    if "provider=" in purpose:
        for part in purpose.split("|"):
            if part.startswith("provider="):
                provider = part[len("provider="):].strip().lower()
                break
    return provider or _infer_provider(model) or "unknown"


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
    bucket_hour: str | None = None,
) -> None:
    """记录一次 LLM 调用，按 (day, group, user, model, purpose) 桶累加。
    `provider` 显式提供时优先；否则从 model 名推导（anthropic/gemini/openai/codex）。
    purpose 内已编码 provider 信息：写入时实际 purpose=`{original}|provider={p}`，
    查询时按子串匹配（简单 schema 兼容）。
    """
    bucket, hour_bucket = _normalize_bucket_values(
        bucket_day=bucket_day,
        bucket_hour=bucket_hour,
    )
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
        conn.execute(
            """
            INSERT INTO token_usage_hourly_ledger
                (bucket_hour, bucket_day, group_id, user_id, model, purpose,
                 prompt_tokens, completion_tokens, total_tokens, call_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(bucket_hour, group_id, user_id, model, purpose) DO UPDATE SET
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                call_count = call_count + 1,
                updated_at = excluded.updated_at
            """,
            (hour_bucket, bucket, str(group_id or ""), str(user_id or ""), str(model or ""),
             purpose_str, pt, ct, tt, now),
        )
        conn.commit()


def query_provider_summary(window: str = "month") -> dict[str, Any]:
    """按 provider 维度聚合最近窗口的 token 用量。返回 {provider: totals}。
    provider 从 purpose 字段中的 `provider=xxx` 子串解析，或从 model 名兜底推导。
    """
    window_key = normalize_window(window)
    if window_key == "day":
        start_str = _hour_str(_hour_range_start())
        table_name = "token_usage_hourly_ledger"
        where_field = "bucket_hour"
    else:
        start_str = _day_str(_range_start(window_key))
        table_name = "token_usage_ledger"
        where_field = "bucket_day"
    providers: dict[str, dict[str, int]] = {}
    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT model, purpose,
                   SUM(prompt_tokens) AS pt,
                   SUM(completion_tokens) AS ct,
                   SUM(total_tokens) AS tt,
                   SUM(call_count) AS cc
            FROM {table_name}
            WHERE {where_field} >= ?
            GROUP BY model, purpose
            """,
            (start_str,),
        ).fetchall()
    for row in rows:
        model = str(row["model"] or "")
        purpose = str(row["purpose"] or "")
        provider = _provider_from_model_purpose(model, purpose)
        bucket = providers.setdefault(provider, {
            "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "call_count": 0,
        })
        bucket["prompt_tokens"] += int(row["pt"] or 0)
        bucket["completion_tokens"] += int(row["ct"] or 0)
        bucket["total_tokens"] += int(row["tt"] or 0)
        bucket["call_count"] += int(row["cc"] or 0)
    return {
        "window": window_key,
        "start_day": start_str,
        "providers": [
            {"provider": p, **vals}
            for p, vals in sorted(providers.items(), key=lambda kv: -kv[1]["total_tokens"])
        ],
    }


def query_total_consumption() -> dict[str, Any]:
    """返回不受窗口限制的累计 token 消耗。"""
    with connect_sync() as conn:
        total_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(call_count), 0) AS call_count,
                MIN(bucket_day) AS first_day,
                MAX(bucket_day) AS last_day
            FROM token_usage_ledger
            """
        ).fetchone()
        provider_rows = conn.execute(
            """
            SELECT model, purpose,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            GROUP BY model, purpose
            """
        ).fetchall()
        model_rows = conn.execute(
            """
            SELECT model,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE model != ''
            GROUP BY model
            ORDER BY total_tokens DESC
            LIMIT 50
            """
        ).fetchall()
        group_rows = conn.execute(
            """
            SELECT group_id,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE group_id != ''
            GROUP BY group_id
            ORDER BY total_tokens DESC
            LIMIT 50
            """
        ).fetchall()
        purpose_rows = conn.execute(
            """
            SELECT purpose,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            WHERE purpose != ''
            GROUP BY purpose
            ORDER BY total_tokens DESC
            LIMIT 80
            """
        ).fetchall()
        day_rows = conn.execute(
            """
            SELECT bucket_day,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_ledger
            GROUP BY bucket_day
            ORDER BY bucket_day ASC
            """
        ).fetchall()

    providers: dict[str, dict[str, int]] = {}
    for row in provider_rows:
        model = str(row["model"] or "")
        purpose = str(row["purpose"] or "")
        provider = _provider_from_model_purpose(model, purpose)
        bucket = providers.setdefault(provider, _empty_totals())
        for key, column in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
            ("call_count", "call_count"),
        ):
            bucket[key] += int(row[column] or 0)

    total = _row_to_dict(
        total_row,
        ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"),
    )
    return {
        "total": total,
        "first_day": str(total_row["first_day"] or "") if total_row is not None else "",
        "last_day": str(total_row["last_day"] or "") if total_row is not None else "",
        "providers": [
            {"provider": provider, **values}
            for provider, values in sorted(
                providers.items(), key=lambda item: -item[1]["total_tokens"]
            )
        ],
        "by_model": [
            _row_to_dict(row, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="model")
            for row in model_rows
        ],
        "by_group": [
            _row_to_dict(row, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="group_id")
            for row in group_rows
        ],
        "by_purpose": _aggregate_purpose_rows(purpose_rows),
        "series": _build_total_series(
            [
                _row_to_dict(
                    row,
                    ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"),
                    key="bucket_day",
                )
                for row in day_rows
            ]
        ),
    }


def _range_start(window: str) -> datetime:
    window = normalize_window(window)
    now = datetime.now()
    today = datetime(now.year, now.month, now.day)
    if window == "day":
        return today
    if window == "week":
        return today - timedelta(days=6)
    if window == "month":
        return today - timedelta(days=29)
    return today - timedelta(days=29)


def _hour_range_start() -> datetime:
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    return current_hour - timedelta(hours=23)


def _empty_totals() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0}


def _build_hourly_series(hour_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_hour = {str(row.get("bucket_hour") or ""): row for row in hour_rows}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens", "call_count")
    start_hour = _hour_range_start()
    buckets: list[dict[str, Any]] = []
    for offset in range(24):
        bucket_dt = start_hour + timedelta(hours=offset)
        key = _hour_str(bucket_dt)
        raw = by_hour.get(key) or _empty_totals()
        values = {field: int(raw.get(field, 0) or 0) for field in fields}
        buckets.append(
            {
                "bucket": key,
                "bucket_hour": key,
                "label": bucket_dt.strftime("%H:00"),
                **values,
            }
        )
    return buckets


def _build_series(window: str, day_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为 WebUI sparkline 补齐时间桶。

    week/month 窗口按自然日补齐；day/24h 窗口由小时级账本单独构造。
    """
    window = normalize_window(window)
    today = datetime.now()
    by_day = {str(row.get("bucket_day") or ""): row for row in day_rows}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens", "call_count")
    days = 7 if window == "week" else 30
    start = datetime(today.year, today.month, today.day) - timedelta(days=days - 1)
    buckets = []
    for offset in range(days):
        bucket_dt = start + timedelta(days=offset)
        key = _day_str(bucket_dt)
        raw = by_day.get(key) or _empty_totals()
        values = {f: int(raw.get(f, 0) or 0) for f in fields}
        buckets.append(
            {
                "bucket": key,
                "label": bucket_dt.strftime("%m-%d"),
                **values,
            }
        )
    return buckets


def _build_total_series(day_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按自然日返回全量累计曲线。

    账本当前按天聚合，因此总消耗图展示每日新增与累计 total_tokens。
    """
    cumulative = _empty_totals()
    series: list[dict[str, Any]] = []
    for row in day_rows:
        values = {
            key: int(row.get(key, 0) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "call_count")
        }
        for key in cumulative:
            cumulative[key] += values[key]
        bucket = str(row.get("bucket_day") or "")
        series.append(
            {
                "bucket": bucket,
                "label": bucket[5:] if len(bucket) >= 10 else bucket,
                **values,
                "cumulative_prompt_tokens": cumulative["prompt_tokens"],
                "cumulative_completion_tokens": cumulative["completion_tokens"],
                "cumulative_total_tokens": cumulative["total_tokens"],
                "cumulative_call_count": cumulative["call_count"],
            }
        )
    return series


def _query_hourly_summary() -> dict[str, Any]:
    start_str = _hour_str(_hour_range_start())
    with connect_sync() as conn:
        total_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(call_count), 0) AS call_count
            FROM token_usage_hourly_ledger
            WHERE bucket_hour >= ?
            """,
            (start_str,),
        ).fetchone()
        by_hour = conn.execute(
            """
            SELECT bucket_hour,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_hourly_ledger
            WHERE bucket_hour >= ?
            GROUP BY bucket_hour
            ORDER BY bucket_hour ASC
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
            FROM token_usage_hourly_ledger
            WHERE bucket_hour >= ? AND model != ''
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
            FROM token_usage_hourly_ledger
            WHERE bucket_hour >= ? AND group_id != ''
            GROUP BY group_id
            ORDER BY total_tokens DESC
            LIMIT 50
            """,
            (start_str,),
        ).fetchall()
        by_purpose_rows = conn.execute(
            """
            SELECT purpose,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(call_count) AS call_count
            FROM token_usage_hourly_ledger
            WHERE bucket_hour >= ? AND purpose != ''
            GROUP BY purpose
            ORDER BY total_tokens DESC
            LIMIT 50
            """,
            (start_str,),
        ).fetchall()

    by_hour_list = [
        _row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="bucket_hour")
        for r in by_hour
    ]
    by_model_list = [
        _row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="model")
        for r in by_model
    ]
    by_group_list = [
        _row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="group_id")
        for r in by_group
    ]

    total = _row_to_dict(total_row, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"))
    total_tokens = max(0, int(total.get("total_tokens", 0) or 0))
    max_model_tokens = max((int(row.get("total_tokens", 0) or 0) for row in by_model_list), default=0)
    model_distribution = []
    for row in by_model_list[:12]:
        tokens = int(row.get("total_tokens", 0) or 0)
        calls = int(row.get("call_count", 0) or 0)
        model_distribution.append(
            {
                **row,
                "token_share": round(tokens / total_tokens, 4) if total_tokens > 0 else 0.0,
                "relative_width": round(tokens / max_model_tokens, 4) if max_model_tokens > 0 else 0.0,
                "call_share": round(calls / int(total.get("call_count", 0) or 1), 4)
                if int(total.get("call_count", 0) or 0) > 0
                else 0.0,
            }
        )

    return {
        "window": "day",
        "start_day": start_str[:10],
        "start_hour": start_str,
        "total": total,
        "series": _build_hourly_series(by_hour_list),
        "by_hour": by_hour_list,
        "by_day": [],
        "by_model": by_model_list,
        "model_distribution": model_distribution,
        "by_group": by_group_list,
        "by_purpose": _aggregate_purpose_rows(by_purpose_rows),
    }


def query_summary(window: str = "month") -> dict[str, Any]:
    """返回当前窗口的总 token 数 + 按 day/model/group 的分布。"""
    window_key = normalize_window(window)
    if window_key == "day":
        return _query_hourly_summary()
    start = _range_start(window_key)
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
    by_day_list = [
        _row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="bucket_day")
        for r in by_day
    ]
    by_model_list = [
        _row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="model")
        for r in by_model
    ]
    by_group_list = [
        _row_to_dict(r, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"), key="group_id")
        for r in by_group
    ]

    total = _row_to_dict(total_row, ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"))
    total_tokens = max(0, int(total.get("total_tokens", 0) or 0))
    max_model_tokens = max((int(row.get("total_tokens", 0) or 0) for row in by_model_list), default=0)
    model_distribution = []
    for row in by_model_list[:12]:
        tokens = int(row.get("total_tokens", 0) or 0)
        calls = int(row.get("call_count", 0) or 0)
        model_distribution.append(
            {
                **row,
                "token_share": round(tokens / total_tokens, 4) if total_tokens > 0 else 0.0,
                "relative_width": round(tokens / max_model_tokens, 4) if max_model_tokens > 0 else 0.0,
                "call_share": round(calls / int(total.get("call_count", 0) or 1), 4)
                if int(total.get("call_count", 0) or 0) > 0
                else 0.0,
            }
        )

    by_purpose_list = _aggregate_purpose_rows(by_purpose_rows)
    return {
        "window": window_key,
        "start_day": start_str,
        "total": total,
        "series": _build_series(window_key, by_day_list),
        "by_day": by_day_list,
        "by_model": by_model_list,
        "model_distribution": model_distribution,
        "by_group": by_group_list,
        "by_purpose": by_purpose_list,
    }


def query_group_detail(group_id: str, window: str = "month") -> dict[str, Any]:
    """单个群在窗口内按 day/model 的明细。"""
    window_key = normalize_window(window)
    if window_key == "day":
        start_str = _hour_str(_hour_range_start())
        with connect_sync() as conn:
            rows = conn.execute(
                """
                SELECT bucket_hour, model,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(call_count) AS call_count
                FROM token_usage_hourly_ledger
                WHERE bucket_hour >= ? AND group_id = ?
                GROUP BY bucket_hour, model
                ORDER BY bucket_hour ASC, total_tokens DESC
                """,
                (start_str, str(group_id or "")),
            ).fetchall()
        return {
            "group_id": str(group_id or ""),
            "window": window_key,
            "rows": [
                {
                    "bucket_hour": str(r["bucket_hour"]),
                    "bucket_day": str(r["bucket_hour"])[:10],
                    "model": str(r["model"]),
                    "prompt_tokens": int(r["prompt_tokens"] or 0),
                    "completion_tokens": int(r["completion_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "call_count": int(r["call_count"] or 0),
                }
                for r in rows
            ],
        }
    start_str = _day_str(_range_start(window_key))
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
        "window": window_key,
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


def _aggregate_purpose_rows(rows: list[Any]) -> list[dict[str, Any]]:
    by_purpose_agg: dict[str, dict[str, int]] = {}
    for row in rows:
        raw_purpose = str(row["purpose"] or "")
        functional = raw_purpose.split("|", 1)[0].strip() or "unknown"
        bucket_row = by_purpose_agg.setdefault(functional, _empty_totals())
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"):
            bucket_row[key] = int(bucket_row[key]) + int(row[key] or 0)
    return [
        {"purpose": purpose, **values}
        for purpose, values in sorted(by_purpose_agg.items(), key=lambda item: -item[1]["total_tokens"])
    ]


__all__ = [
    "record_llm_call",
    "query_summary",
    "query_group_detail",
    "query_provider_summary",
    "query_total_consumption",
    "normalize_window",
]
