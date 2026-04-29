from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable


MODEL_ROLE_INTENT = "intent"
MODEL_ROLE_REVIEW = "review"
MODEL_ROLE_AGENT = "agent"
MODEL_ROLE_STICKER = "sticker"
MODEL_ROLE_WARMUP = "warmup"

MODEL_ROLE_LABELS = {
    MODEL_ROLE_INTENT: "意图/语义帧",
    MODEL_ROLE_REVIEW: "回复审阅",
    MODEL_ROLE_AGENT: "正文回复/Agent",
    MODEL_ROLE_STICKER: "表情包视觉",
    MODEL_ROLE_WARMUP: "预热",
}

MODEL_OVERRIDE_ROLES = (
    MODEL_ROLE_INTENT,
    MODEL_ROLE_REVIEW,
    MODEL_ROLE_AGENT,
    MODEL_ROLE_STICKER,
)

_ROLE_ALIASES = {
    "intent": MODEL_ROLE_INTENT,
    "意图": MODEL_ROLE_INTENT,
    "语义": MODEL_ROLE_INTENT,
    "语义帧": MODEL_ROLE_INTENT,
    "review": MODEL_ROLE_REVIEW,
    "审阅": MODEL_ROLE_REVIEW,
    "审核": MODEL_ROLE_REVIEW,
    "agent": MODEL_ROLE_AGENT,
    "reply": MODEL_ROLE_AGENT,
    "回复": MODEL_ROLE_AGENT,
    "正文": MODEL_ROLE_AGENT,
    "sticker": MODEL_ROLE_STICKER,
    "表情": MODEL_ROLE_STICKER,
    "表情包": MODEL_ROLE_STICKER,
    "视觉": MODEL_ROLE_STICKER,
}


def normalize_model_overrides(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except Exception as exc:
            raise ValueError("模型覆盖需要 JSON 对象，如 {\"agent\":\"gpt-5.4\"}") from exc
    if not isinstance(value, dict):
        return {}
    overrides: dict[str, str] = {}
    for raw_role, raw_model in value.items():
        role = resolve_model_role(raw_role)
        model = str(raw_model or "").strip()
        if role and model:
            overrides[role] = model
    return overrides


def resolve_model_role(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _ROLE_ALIASES.get(text, text if text in MODEL_OVERRIDE_ROLES else "")


def get_model_for_role(plugin_config: Any, role: str, default_model: str = "") -> str:
    overrides = normalize_model_overrides(
        getattr(plugin_config, "personification_model_overrides", {}) or {}
    )
    normalized_role = resolve_model_role(role)
    if normalized_role and overrides.get(normalized_role):
        return overrides[normalized_role]
    return str(default_model or "").strip()


def get_model_override_for_role(plugin_config: Any, role: str) -> str:
    return get_model_for_role(plugin_config, role, "")


def _normalize_api_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"openai_codex", "codex"}:
        return "openai_codex"
    if text == "gemini_official":
        return "gemini"
    return text


def _codex_cache_dirs(plugin_config: Any, providers: list[dict[str, Any]]) -> list[Path]:
    dirs: list[Path] = []

    def _add_dir(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        path = Path(os.path.expandvars(text)).expanduser()
        if path.name.lower() == "auth.json":
            path = path.parent
        if path not in dirs:
            dirs.append(path)

    for env_name in ("CODEX_HOME", "CHATGPT_LOCAL_HOME"):
        _add_dir(os.environ.get(env_name, ""))
    _add_dir(getattr(plugin_config, "personification_codex_auth_path", ""))
    for provider in providers:
        if _normalize_api_type(provider.get("api_type")) == "openai_codex":
            _add_dir(provider.get("auth_path") or provider.get("codex_auth_path"))
    _add_dir("~/.codex")
    _add_dir("~/.chatgpt-local")
    return dirs


def _load_codex_cached_models(
    plugin_config: Any,
    providers: list[dict[str, Any]],
) -> list[dict[str, str]]:
    has_codex_provider = (
        any(_normalize_api_type(provider.get("api_type")) == "openai_codex" for provider in providers)
        or _normalize_api_type(getattr(plugin_config, "personification_api_type", "")) == "openai_codex"
    )
    if not has_codex_provider:
        return []
    for cache_dir in _codex_cache_dirs(plugin_config, providers):
        cache_path = cache_dir / "models_cache.json"
        if not cache_path.exists():
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_models = payload.get("models", []) if isinstance(payload, dict) else []
        if not isinstance(raw_models, list):
            continue
        models: list[dict[str, str]] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if str(item.get("visibility", "list") or "list").strip() != "list":
                continue
            if item.get("supported_in_api") is False:
                continue
            model = str(item.get("slug") or item.get("display_name") or "").strip()
            if not model:
                continue
            models.append({"model": model, "source": "Codex /model"})
        if models:
            return models
    return []


def collect_available_models(
    plugin_config: Any,
    *,
    get_configured_api_providers: Callable[[], list[dict[str, Any]]] | None = None,
) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []

    def _add(model: Any, source: str) -> None:
        model_text = str(model or "").strip()
        if not model_text:
            return
        key = model_text.lower()
        if key in seen:
            return
        seen.add(key)
        result.append({"model": model_text, "source": source})

    providers: list[dict[str, Any]] = []
    if callable(get_configured_api_providers):
        try:
            providers = [
                dict(item)
                for item in list(get_configured_api_providers() or [])
                if isinstance(item, dict)
            ]
        except Exception:
            providers = []
    if providers:
        for item in _load_codex_cached_models(plugin_config, providers):
            _add(item["model"], item["source"])
        for provider in providers:
            if _normalize_api_type(provider.get("api_type")) == "openai_codex":
                continue
            provider_name = str(provider.get("name") or provider.get("api_type") or "主路由")
            _add(provider.get("model"), provider_name)
    else:
        for item in _load_codex_cached_models(plugin_config, providers):
            _add(item["model"], item["source"])
        _add(getattr(plugin_config, "personification_model", ""), "主模型")

    _add(getattr(plugin_config, "personification_lite_model", ""), "轻量模型")
    _add(getattr(plugin_config, "personification_fallback_model", ""), "全局兜底")
    _add(getattr(plugin_config, "personification_vision_fallback_model", ""), "视觉兜底")
    _add(getattr(plugin_config, "personification_labeler_model", ""), "表情包视觉")

    overrides = normalize_model_overrides(
        getattr(plugin_config, "personification_model_overrides", {}) or {}
    )
    for role, model in overrides.items():
        _add(model, f"当前覆盖:{MODEL_ROLE_LABELS.get(role, role)}")
    return result


def format_model_overrides(plugin_config: Any) -> list[str]:
    overrides = normalize_model_overrides(
        getattr(plugin_config, "personification_model_overrides", {}) or {}
    )
    lines: list[str] = []
    for role in MODEL_OVERRIDE_ROLES:
        model = overrides.get(role, "")
        lines.append(f"- {role}（{MODEL_ROLE_LABELS[role]}）：{model or '未覆盖'}")
    return lines


__all__ = [
    "MODEL_OVERRIDE_ROLES",
    "MODEL_ROLE_AGENT",
    "MODEL_ROLE_INTENT",
    "MODEL_ROLE_LABELS",
    "MODEL_ROLE_REVIEW",
    "MODEL_ROLE_STICKER",
    "MODEL_ROLE_WARMUP",
    "collect_available_models",
    "format_model_overrides",
    "get_model_for_role",
    "get_model_override_for_role",
    "normalize_model_overrides",
    "resolve_model_role",
]
