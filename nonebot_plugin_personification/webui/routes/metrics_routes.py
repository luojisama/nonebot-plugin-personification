from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ...core import metrics, token_ledger
from ...core.onebot_cache import get_group_name_map
from ..deps import AdminIdentity, require_admin

_WINDOW_PATTERN = "^(day|week|month|24h|7d|30d)$"

_PROVIDER_LABELS = {
    "anthropic": "Anthropic / Claude",
    "openai": "OpenAI",
    "gemini": "Gemini/Antigravity CLI",
    "codex": "Codex / ChatGPT",
}

_QUOTA_FIELDS = {
    "anthropic": "personification_quota_anthropic_monthly_tokens",
    "openai": "personification_quota_openai_monthly_tokens",
    "gemini": "personification_quota_gemini_cli_monthly_tokens",
    "codex": "personification_quota_codex_monthly_tokens",
}

_PURPOSE_LABELS = {
    "chat": "群聊回复",
    "ai_route": "未标注主模型调用",
    "direct_call": "直接调用",
    "user_persona": "用户画像",
    "group_style": "群风格学习",
    "group_knowledge": "群知识构建",
    "memory_summarizer": "记忆摘要",
    "memory_summarizer_daily": "每日记忆摘要",
    "inner_state_chat": "聊天内心状态",
    "inner_state_diary": "日记内心状态",
    "qzone_diary": "QQ 空间日记",
    "proactive_group_idle": "群主动消息",
    "proactive_private": "私聊主动消息",
    "persona_template_alias_planning": "人设构建：别名规划",
    "persona_template_research": "人设构建：资料研究",
    "persona_template_synthesis": "人设构建：模板生成",
    "persona_template_repair": "人设构建：模板修复",
}


def _resolve_limit(plugin_config, provider: str) -> int:
    field = _QUOTA_FIELDS.get(provider, "")
    if not field:
        return 0
    try:
        return max(0, int(getattr(plugin_config, field, 0) or 0))
    except Exception:
        return 0


def _provider_usage(*, plugin_config, window: str) -> list[dict]:
    data = token_ledger.query_provider_summary(window)
    rows = list(data.get("providers", []) or [])
    items = []
    for provider_key in ("anthropic", "openai", "gemini", "codex"):
        entry = next(
            (p for p in rows if p.get("provider") == provider_key),
            {
                "provider": provider_key,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "call_count": 0,
            },
        )
        limit = _resolve_limit(plugin_config, provider_key) if plugin_config else 0
        used = int(entry.get("total_tokens", 0) or 0)
        items.append(
            {
                "provider": provider_key,
                "label": _PROVIDER_LABELS[provider_key],
                "prompt_tokens": int(entry.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(entry.get("completion_tokens", 0) or 0),
                "total_tokens": used,
                "call_count": int(entry.get("call_count", 0) or 0),
                "monthly_limit": limit,
                "usage_ratio": round(used / limit, 4) if limit > 0 else 0.0,
                "unlimited": limit == 0,
            }
        )
    for entry in rows:
        provider = str(entry.get("provider", "") or "unknown")
        if provider in _PROVIDER_LABELS:
            continue
        items.append(
            {
                "provider": provider,
                "label": provider,
                "prompt_tokens": int(entry.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(entry.get("completion_tokens", 0) or 0),
                "total_tokens": int(entry.get("total_tokens", 0) or 0),
                "call_count": int(entry.get("call_count", 0) or 0),
                "monthly_limit": 0,
                "usage_ratio": 0.0,
                "unlimited": True,
            }
        )
    return items


def _billing_summary(summary: dict, provider_usage: list[dict]) -> dict:
    series = []
    for point in summary.get("series", []) or []:
        series.append(
            {
                "bucket": point.get("bucket", ""),
                "label": point.get("label", ""),
                "request_cost": 0.0,
                "credit_deduction": 0.0,
                "requests": int(point.get("call_count", 0) or 0),
                "tokens": int(point.get("total_tokens", 0) or 0),
            }
        )
    limited = [p for p in provider_usage if int(p.get("monthly_limit", 0) or 0) > 0]
    used_tokens = sum(int(p.get("total_tokens", 0) or 0) for p in provider_usage)
    limited_used_tokens = sum(int(p.get("total_tokens", 0) or 0) for p in limited)
    limit_tokens = sum(int(p.get("monthly_limit", 0) or 0) for p in limited)
    return {
        "request_cost": 0.0,
        "credit_deduction": 0.0,
        "cost_configured": False,
        "currency": "USD",
        "series": series,
        "quota": {
            "used_tokens": used_tokens,
            "limited_used_tokens": limited_used_tokens,
            "limit_tokens": limit_tokens,
            "limited_provider_count": len(limited),
            "unlimited": len(limited) == 0,
        },
        "note": "本地令牌账本当前没有模型单价配置，费用字段显示为 $0.00；额度进度按 provider 月度令牌额度统计。",
    }


def _get_first_bot(runtime) -> Any | None:
    for holder in (getattr(runtime, "runtime_bundle", None), runtime):
        if holder is None:
            continue
        get_bots = getattr(holder, "get_bots", None)
        if not callable(get_bots):
            continue
        try:
            bots = get_bots() or {}
        except Exception:
            continue
        bot = next(iter(bots.values()), None) if bots else None
        if bot is not None:
            return bot
    return None


def _fallback_group_label(group_id: str) -> str:
    return f"群 {group_id}" if group_id else "群名获取失败"


async def _annotate_group_rows(runtime, rows: list[dict]) -> list[dict]:
    bot = _get_first_bot(runtime)
    group_ids = [str(row.get("group_id", "") or "").strip() for row in rows or []]
    name_map = await get_group_name_map(bot, group_ids) if group_ids else {}
    out: list[dict] = []
    for row in rows or []:
        item = dict(row)
        group_id = str(item.get("group_id", "") or "").strip()
        existing_name = str(item.get("group_name", "") or "").strip()
        group_name = (str(name_map.get(group_id, "") or "").strip() if group_id else "") or existing_name
        item["group_name"] = group_name
        item["group_label"] = group_name or _fallback_group_label(group_id)
        item["group_name_missing"] = bool(group_id and not group_name)
        item["group_lookup_status"] = "ok" if group_name else ("no_bot" if bot is None else "missing")
        out.append(item)
    return out


def _add_distribution_fields(rows: list[dict], *, total_tokens: int) -> list[dict]:
    max_tokens = max((int(row.get("total_tokens", 0) or 0) for row in rows), default=0)
    out = []
    for row in rows:
        item = dict(row)
        tokens = int(item.get("total_tokens", 0) or 0)
        item["token_share"] = round(tokens / total_tokens, 4) if total_tokens > 0 else 0.0
        item["percent"] = round(tokens / total_tokens * 100, 2) if total_tokens > 0 else 0.0
        item["relative_width"] = round(tokens / max_tokens, 4) if max_tokens > 0 else 0.0
        out.append(item)
    return out


def _annotate_purpose_rows(rows: list[dict], *, total_tokens: int) -> list[dict]:
    annotated = []
    for row in rows:
        item = dict(row)
        purpose = str(item.get("purpose", "") or "unknown")
        item["purpose"] = purpose
        item["purpose_label"] = _PURPOSE_LABELS.get(purpose, purpose)
        annotated.append(item)
    return _add_distribution_fields(annotated, total_tokens=total_tokens)


def _chart_from_summary(key: str, label: str, summary: dict) -> dict:
    total = dict(summary.get("total") or {})
    return {
        "key": key,
        "label": label,
        "value_key": "total_tokens",
        "total": total,
        "series": list(summary.get("series") or []),
    }


async def _dashboard_overview(runtime, total_consumption: dict) -> dict:
    summaries = {
        "day": token_ledger.query_summary("day"),
        "week": token_ledger.query_summary("week"),
        "month": token_ledger.query_summary("month"),
    }
    total = dict(total_consumption.get("total") or {})
    total_tokens = int(total.get("total_tokens", 0) or 0)
    total_groups = await _annotate_group_rows(runtime, list(total_consumption.get("by_group") or []))
    return {
        "charts": [
            _chart_from_summary("day", "24小时", summaries["day"]),
            _chart_from_summary("week", "7天", summaries["week"]),
            _chart_from_summary("month", "30天", summaries["month"]),
            {
                "key": "total",
                "label": "总消耗",
                "value_key": "cumulative_total_tokens",
                "total": total,
                "series": list(total_consumption.get("series") or []),
            },
        ],
        "model_usage": _add_distribution_fields(
            list(total_consumption.get("by_model") or []),
            total_tokens=total_tokens,
        ),
        "purpose_usage": _annotate_purpose_rows(
            list(total_consumption.get("by_purpose") or []),
            total_tokens=total_tokens,
        ),
        "group_usage": _add_distribution_fields(total_groups, total_tokens=total_tokens),
    }


def build_metrics_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/metrics", tags=["metrics"])

    @router.get("/summary")
    async def summary(
        window: str = Query(default="month", pattern=_WINDOW_PATTERN),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        window_key = token_ledger.normalize_window(window)
        data = token_ledger.query_summary(window_key)
        plugin_config = getattr(runtime, "plugin_config", None)
        provider_usage = _provider_usage(plugin_config=plugin_config, window=window_key)
        data["by_group"] = await _annotate_group_rows(runtime, list(data.get("by_group") or []))
        data["by_purpose"] = _annotate_purpose_rows(
            list(data.get("by_purpose") or []),
            total_tokens=int((data.get("total") or {}).get("total_tokens", 0) or 0),
        )
        data["provider_usage"] = provider_usage
        data["billing"] = _billing_summary(data, provider_usage)
        total_consumption = token_ledger.query_total_consumption()
        total_consumption["by_group"] = await _annotate_group_rows(
            runtime,
            list(total_consumption.get("by_group") or []),
        )
        total_consumption["by_purpose"] = _annotate_purpose_rows(
            list(total_consumption.get("by_purpose") or []),
            total_tokens=int((total_consumption.get("total") or {}).get("total_tokens", 0) or 0),
        )
        data["total_consumption"] = total_consumption
        data["dashboard_overview"] = await _dashboard_overview(runtime, total_consumption)
        return data

    @router.get("/group/{group_id}")
    async def group_detail(
        group_id: str,
        window: str = Query(default="month", pattern=_WINDOW_PATTERN),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        return token_ledger.query_group_detail(group_id, token_ledger.normalize_window(window))

    @router.get("/runtime")
    async def runtime_snapshot(_: AdminIdentity = Depends(require_admin)) -> dict:
        snap = metrics.snapshot_metrics()
        return {
            "counters": list(snap.get("counters", []))[:30],
            "timings": list(snap.get("timings", []))[:30],
        }

    return router
