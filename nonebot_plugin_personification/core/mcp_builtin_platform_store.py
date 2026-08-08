from __future__ import annotations

import json
import time
from typing import Any

from .db import connect_sync
from .mcp_builtin import BUILTIN_SOCIAL_MCP_ID


PLATFORMS = ("bilibili", "douyin", "tieba", "xiaoheihe")
CONFIG_FIELDS = frozenset({
    "quality_mode", "marketing_threshold", "min_play_count", "min_comment_count",
    "min_reply_count", "max_results", "comment_limit", "danmaku_limit",
    "cache_ttl_seconds", "request_timeout_seconds",
})
_INTEGER_RANGES = {
    "min_play_count": (0, 1_000_000_000),
    "min_comment_count": (0, 1_000_000),
    "min_reply_count": (0, 1_000_000),
    "max_results": (1, 50),
    "comment_limit": (0, 200),
    "danmaku_limit": (0, 500),
    "cache_ttl_seconds": (60, 86_400),
    "request_timeout_seconds": (3, 60),
}


def _parse_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _validated_config(config: dict[str, Any]) -> dict[str, Any]:
    if set(config) - CONFIG_FIELDS:
        raise ValueError("config contains unsupported fields")
    normalized = dict(config)
    if "quality_mode" in normalized and normalized["quality_mode"] not in {"balanced", "strict", "ranking_only"}:
        raise ValueError("quality_mode is invalid")
    if "marketing_threshold" in normalized:
        value = normalized["marketing_threshold"]
        if isinstance(value, bool):
            raise ValueError("marketing_threshold must be a number")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("marketing_threshold must be a number") from exc
        if not 0.0 <= numeric <= 1.0:
            raise ValueError("marketing_threshold is out of range")
        normalized["marketing_threshold"] = numeric
    for key, (minimum, maximum) in _INTEGER_RANGES.items():
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        try:
            numeric = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if numeric < minimum or numeric > maximum:
            raise ValueError(f"{key} is out of range")
        normalized[key] = numeric
    return normalized


class BuiltinPlatformStore:
    def list(self) -> list[dict[str, Any]]:
        with connect_sync() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_builtin_platforms WHERE installation_id=? ORDER BY platform",
                (BUILTIN_SOCIAL_MCP_ID,),
            ).fetchall()
        return [{
            "platform": str(row["platform"]),
            "enabled": bool(row["desired_enabled"]),
            "revision": int(row["revision"] or 0),
            "config": _parse_config(row["config_json"]),
            "updated_at": float(row["updated_at"] or 0),
        } for row in rows]

    def update(
        self,
        *,
        platform: str,
        enabled: bool,
        config: dict[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        if platform not in PLATFORMS:
            raise ValueError("unsupported platform")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a JSON boolean")
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        config = _validated_config(config)
        now = time.time()
        with connect_sync() as conn:
            result = conn.execute(
                """UPDATE mcp_builtin_platforms
                   SET desired_enabled=?,config_json=?,revision=revision+1,updated_at=?
                   WHERE installation_id=? AND platform=? AND revision=?""",
                (
                    int(enabled),
                    json.dumps(config, ensure_ascii=False),
                    now,
                    BUILTIN_SOCIAL_MCP_ID,
                    platform,
                    int(expected_revision),
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("revision_conflict")
            row = conn.execute(
                "SELECT * FROM mcp_builtin_platforms WHERE installation_id=? AND platform=?",
                (BUILTIN_SOCIAL_MCP_ID, platform),
            ).fetchone()
            conn.commit()
        return {
            "platform": platform,
            "enabled": bool(row["desired_enabled"]),
            "revision": int(row["revision"]),
            "config": _parse_config(row["config_json"]),
            "updated_at": float(row["updated_at"]),
        }


__all__ = ["BuiltinPlatformStore", "CONFIG_FIELDS", "PLATFORMS"]
