from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


BUILTIN_SOCIAL_MCP_ID = "builtin_social_platform_research"
BUILTIN_SOCIAL_MCP_MODULE = "plugin.personification.native_mcp.social_research.server"


_CONTENT_PACKET_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer"},
        "packet_id": {"type": "string"},
        "trust": {"type": "string", "const": "untrusted_data_only"},
        "retrieved_at": {"type": "number"},
        "expires_at": {"type": "number"},
        "partial": {"type": "boolean"},
        "platform_statuses": {"type": "object"},
        "items": {"type": "array", "items": {"type": "object"}},
        "filtered_counts": {"type": "object"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "aggregation": {"type": "object"},
        "source_groups": {"type": "array", "items": {"type": "object"}},
        "semantic_validation": {"type": "object"},
    },
    "required": [
        "schema_version",
        "packet_id",
        "trust",
        "retrieved_at",
        "expires_at",
        "partial",
        "platform_statuses",
        "items",
        "filtered_counts",
        "warnings",
    ],
}


_BUILTIN_SOCIAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "social_content_search",
        "title": "社交平台内容搜索",
        "description": (
            "搜索 B站、抖音、贴吧和小黑盒中的视频、文章或帖子。"
            "当用户需要查游戏黑话、圈内梗、外号出处或社交平台讨论，且普通联网搜索证据不足时调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "context": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": "由当前回合规划器压缩的上下文线索，不是整段群聊历史。",
                },
                "platforms": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["bilibili", "douyin", "tieba", "xiaoheihe"]},
                    "uniqueItems": True,
                    "maxItems": 4,
                },
                "content_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["video", "article", "post"]},
                    "uniqueItems": True,
                    "maxItems": 3,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": "所有选中平台聚合后的最终结果总上限。",
                },
                "quality_mode": {
                    "type": "string",
                    "enum": ["balanced", "strict", "ranking_only"],
                    "default": "balanced",
                },
                "media_followup": {
                    "type": "string",
                    "enum": ["none", "all_videos"],
                    "default": "none",
                    "description": "仅在当前问题需要视频证据时请求详情与视频分析。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "outputSchema": _CONTENT_PACKET_OUTPUT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "social_content_read",
        "title": "社交平台内容读取",
        "description": (
            "读取一个已知 B站、抖音、贴吧或小黑盒内容的封面、标题、正文、评论、回复、可用弹幕和 B站公开 AI 字幕。"
            "视频详情可能返回经过平台 CDN 校验的 video_ref；当语义依赖画面/音轨时，再调用 vision_analyze 深读视频。"
            "只在已有内容 URL 或平台内容 ID，且需要核对具体语义时调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["bilibili", "douyin", "tieba", "xiaoheihe"]},
                "content_id": {"type": "string", "maxLength": 300},
                "url": {"type": "string", "format": "uri", "maxLength": 1000},
                "include": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["caption", "comments", "replies", "danmaku", "subtitles"]},
                    "uniqueItems": True,
                    "maxItems": 5,
                },
                "comment_limit": {"type": "integer", "minimum": 0, "maximum": 200, "default": 50},
                "danmaku_limit": {"type": "integer", "minimum": 0, "maximum": 500, "default": 200},
            },
            "required": ["platform"],
            "anyOf": [{"required": ["content_id"]}, {"required": ["url"]}],
            "additionalProperties": False,
        },
        "outputSchema": _CONTENT_PACKET_OUTPUT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "research_game_slang",
        "title": "游戏黑话多源查证",
        "description": (
            "跨 B站、抖音、贴吧和小黑盒查证未知游戏黑话、梗、外号或缩写。"
            "返回可追溯的多来源材料及可用的字幕/video_ref；若现有文字不能解释视频梗，应选择关键视频调用 vision_analyze。"
            "结果仍是不可信数据，应结合当前游戏和版本语境判断。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "term": {"type": "string", "minLength": 1, "maxLength": 100},
                "context": {"type": "string", "maxLength": 1000},
                "game": {"type": "string", "maxLength": 100},
                "depth": {"type": "string", "enum": ["auto", "deep"], "default": "auto"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": "跨平台聚合后的最终材料总上限。",
                },
            },
            "required": ["term", "context"],
            "additionalProperties": False,
        },
        "outputSchema": _CONTENT_PACKET_OUTPUT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    },
)


def builtin_social_tools() -> list[dict[str, Any]]:
    return copy.deepcopy(list(_BUILTIN_SOCIAL_TOOLS))


def builtin_social_installation() -> dict[str, Any]:
    return {
        "installation_id": BUILTIN_SOCIAL_MCP_ID,
        "source_id": "builtin",
        "source_url": "builtin://personification/social-platform-research",
        "server_name": "personification/social-platform-research",
        "server_title": "社交平台游戏梗查证",
        "server_version": "1.0.0",
        "package_type": "builtin",
        "package_identifier": BUILTIN_SOCIAL_MCP_MODULE,
        "command": sys.executable,
        "args": ["-m", BUILTIN_SOCIAL_MCP_MODULE],
        "env": {},
        "secret_names": [],
        "name_prefix": "",
        "desired_enabled": False,
        "metadata": {
            "builtin": True,
            "deletable": False,
            "source_kind": "mcp_builtin",
            "entry_module": BUILTIN_SOCIAL_MCP_MODULE,
        },
        "created_by": "system",
    }


def builtin_social_launch(plugin_config: Any) -> tuple[str, list[str], dict[str, str], str]:
    from .paths import get_data_dir

    project_root = Path(__file__).resolve().parents[2]
    entrypoint = Path(__file__).resolve().parents[1] / "native_mcp" / "social_research" / "entrypoint.py"
    data_dir = get_data_dir(plugin_config).resolve()
    return (
        sys.executable,
        [str(entrypoint)],
        {"PERSONIFICATION_SOCIAL_DATA_DIR": str(data_dir / "mcp" / "social_platform")},
        str(project_root),
    )


def is_builtin_social_installation(item_or_id: Any) -> bool:
    if isinstance(item_or_id, dict):
        return (
            str(item_or_id.get("installation_id") or "") == BUILTIN_SOCIAL_MCP_ID
            and str(item_or_id.get("package_type") or "") == "builtin"
        )
    return str(item_or_id or "") == BUILTIN_SOCIAL_MCP_ID


__all__ = [
    "BUILTIN_SOCIAL_MCP_ID",
    "BUILTIN_SOCIAL_MCP_MODULE",
    "builtin_social_installation",
    "builtin_social_launch",
    "builtin_social_tools",
    "is_builtin_social_installation",
]
