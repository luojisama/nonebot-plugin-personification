from __future__ import annotations

import httpx

from plugin.personification.agent.tool_registry import AgentTool
from . import impl


class _SilentLogger:
    def debug(self, _msg: str) -> None:
        return None


def build_tools(runtime):
    logger = getattr(runtime, "logger", None) or _SilentLogger()
    plugin_config = getattr(runtime, "plugin_config", None)
    shared_client = getattr(runtime, "http_client", None)

    enabled = bool(getattr(plugin_config, "personification_game_info_enabled", True))

    async def _with_client(callback):
        if shared_client is not None:
            return await callback(shared_client)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_client:
            return await callback(http_client)

    async def _game_info_handler(game: str, aspect: str, query: str = "") -> str:
        async def _call(http_client: httpx.AsyncClient) -> str:
            return await impl.game_info(
                game,
                aspect,
                query,
                http_client=http_client,
                logger=logger,
                plugin_config=plugin_config,
            )

        return await _with_client(_call)

    return [
        AgentTool(
            name="game_info",
            description=(
                "查询指定游戏的更新公告 / 攻略 / 剧情设定 / 技巧，覆盖热门网游、3A 大作与小众独立游戏，"
                "数据源含 Steam 官方公告、20 款头部游戏官方源（网游如原神·星铁·鸣潮·王者·英雄联盟·"
                "三角洲；单机如黑神话悟空·怪物猎人荒野·艾尔登法环·塞尔达·博德之门3）与多个社区站点"
                "（含 TapTap/NGA/A9VG 等）。"
                "适用：用户问某款游戏怎么玩、最近更新了什么、剧情讲了啥、有什么技巧。"
                "参数 aspect 取 update/guide/story/tips。"
                "边界：纯角色/世界观/设定的百科式查询也可走 wiki_lookup；"
                "宽泛的资源合集/链接收集走 collect_resources；本工具专注'某款游戏 + 某个方面'的聚合。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "game": {
                        "type": "string",
                        "description": "游戏名称，如 原神 / 艾尔登法环 / Hades",
                    },
                    "aspect": {
                        "type": "string",
                        "enum": ["update", "guide", "story", "tips"],
                        "description": "update=更新/补丁公告, guide=攻略, story=剧情/设定, tips=技巧",
                    },
                    "query": {
                        "type": "string",
                        "description": "可选，更具体的子问题或关键词，如某个角色名、关卡名、版本号",
                    },
                },
                "required": ["game", "aspect"],
            },
            handler=_game_info_handler,
            enabled=lambda: enabled,
            metadata={
                "intent_tags": ["game", "lookup"],
                "requires_network": True,
                "evidence_kind": "web",
                "latency_class": "slow",
                "risk_level": "low",
            },
        )
    ]
