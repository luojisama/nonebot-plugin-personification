from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core import token_ledger
from ..deps import AdminIdentity, require_admin


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


def _resolve_limit(plugin_config, provider: str) -> int:
    field = _QUOTA_FIELDS.get(provider, "")
    if not field:
        return 0
    try:
        return max(0, int(getattr(plugin_config, field, 0) or 0))
    except Exception:
        return 0


def build_quota_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/quota", tags=["quota"])

    @router.get("/summary")
    async def summary(
        window: str = Query(default="month", pattern="^(day|week|month)$"),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        data = token_ledger.query_provider_summary(window)
        plugin_config = getattr(runtime, "plugin_config", None)
        items = []
        seen_providers = {p["provider"] for p in data.get("providers", [])}
        # 已知 provider 优先（即使没用量也显示）；未识别归到 unknown
        for provider_key in ("anthropic", "openai", "gemini", "codex"):
            entry = next(
                (p for p in data.get("providers", []) if p["provider"] == provider_key),
                {"provider": provider_key, "prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "call_count": 0},
            )
            limit = _resolve_limit(plugin_config, provider_key) if plugin_config else 0
            used = int(entry["total_tokens"])
            usage_ratio = (used / limit) if limit > 0 else 0.0
            items.append({
                "provider": provider_key,
                "label": _PROVIDER_LABELS[provider_key],
                "prompt_tokens": int(entry["prompt_tokens"]),
                "completion_tokens": int(entry["completion_tokens"]),
                "total_tokens": used,
                "call_count": int(entry["call_count"]),
                "monthly_limit": limit,
                "usage_ratio": round(usage_ratio, 4),
                "unlimited": limit == 0,
            })
        # 其他未识别的 provider（如 "unknown"）也列出来
        for p in data.get("providers", []):
            if p["provider"] not in _PROVIDER_LABELS:
                items.append({
                    "provider": p["provider"],
                    "label": p["provider"],
                    "prompt_tokens": int(p["prompt_tokens"]),
                    "completion_tokens": int(p["completion_tokens"]),
                    "total_tokens": int(p["total_tokens"]),
                    "call_count": int(p["call_count"]),
                    "monthly_limit": 0,
                    "usage_ratio": 0.0,
                    "unlimited": True,
                })
        return {
            "window": data.get("window", window),
            "start_day": data.get("start_day", ""),
            "providers": items,
            "note": "本地记账：三家 provider 都未提供官方 quota API。数据从插件 LLM 调用累加。",
        }

    return router
