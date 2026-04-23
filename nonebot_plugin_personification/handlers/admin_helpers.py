from typing import Any, Dict, Optional, Tuple

from ..core.ai_routes import summarize_route_state
from ..core.runtime_config import get_runtime_load_info


def build_group_fav_markdown(group_id: str, favorability: float, daily_count: float, status: str) -> str:
    title_color = "#ff69b4"
    text_color = "#d147a3"
    border_color = "#ffb6c1"

    return f"""
<div style="padding: 20px; background-color: #fff5f8; border-radius: 15px; border: 2px solid {border_color}; font-family: 'Microsoft YaHei', sans-serif;">
    <h1 style="color: {title_color}; text-align: center; margin-bottom: 20px;">🌸 群聊好感度详情 🌸</h1>
    
    <div style="background: white; padding: 15px; border-radius: 12px; border: 1px solid {border_color}; margin-bottom: 15px;">
        <p style="margin: 5px 0; color: #666;">群号: <strong style="color: {text_color};">{group_id}</strong></p>
        <p style="margin: 5px 0; color: #666;">当前等级: <strong style="color: {text_color}; font-size: 1.2em;">{status}</strong></p>
    </div>

    <div style="display: flex; gap: 10px; margin-bottom: 15px;">
        <div style="flex: 1; background: white; padding: 10px; border-radius: 10px; border: 1px solid {border_color}; text-align: center;">
            <div style="font-size: 0.8em; color: #999;">好感分值</div>
            <div style="font-size: 1.4em; font-weight: bold; color: {text_color};">{favorability:.2f}</div>
        </div>
        <div style="flex: 1; background: white; padding: 10px; border-radius: 10px; border: 1px solid {border_color}; text-align: center;">
            <div style="font-size: 0.8em; color: #999;">今日增长</div>
            <div style="font-size: 1.4em; font-weight: bold; color: {text_color};">{daily_count:.2f}/10.00</div>
        </div>
    </div>

    <div style="font-size: 0.9em; color: #888; background: rgba(255,255,255,0.5); padding: 10px; border-radius: 8px; line-height: 1.4;">
        ✨ 良好的聊天氛围会增加好感，触发拉黑行为则会扣除。群好感度越高，AI 就会表现得越热情哦~
    </div>
</div>
"""


def build_group_fav_text(group_id: str, favorability: float, daily_count: float, status: str) -> str:
    return (
        f"📊 群聊好感度详情\n"
        f"群号：{group_id}\n"
        f"当前好感：{favorability:.2f}\n"
        f"当前等级：{status}\n"
        f"今日增长：{daily_count:.2f} / 10.00\n"
        f"✨ 你的热情会让 AI 更有温度~"
    )


def parse_group_fav_update_args(arg_str: str, event_group_id: Optional[str]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    if not arg_str:
        return None, None, "用法: 设置群好感 [群号] [分值] 或在群内发送 设置群好感 [分值]"

    parts = arg_str.split()
    if len(parts) == 1:
        if not event_group_id:
            return None, None, "私聊设置请指定群号：设置群好感 [群号] [分值]"
        try:
            return event_group_id, float(parts[0]), None
        except ValueError:
            return None, None, "分值必须为数字。"

    try:
        return parts[0], float(parts[1]), None
    except (ValueError, IndexError):
        return None, None, "分值必须为数字。"


def parse_persona_update_args(raw_text: str, event_group_id: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not raw_text:
        return None, None, "请提供提示词！格式：设置人设 [群号] <提示词>"

    parts = raw_text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[0], parts[1], None

    if event_group_id:
        return event_group_id, raw_text, None

    return None, None, "私聊使用时请指定群号！格式：设置人设 <群号> <提示词>"


def build_view_config_nodes(
    *,
    bot_self_id: str,
    group_id: str,
    group_config: Dict[str, Any],
    provider_names: str,
    plugin_config: Any,
    session_history_limit: int,
    remote_skill_stats: Optional[Dict[str, int]] = None,
) -> list[Dict[str, Any]]:
    def _bool_text(value: Any) -> str:
        return "开启" if bool(value) else "关闭"

    def _pair(label: str, value: Any, description: str) -> str:
        return f"{label}: {value}\n用途: {description}"

    def _join_pairs(*items: str) -> str:
        return "\n\n".join(item for item in items if item)

    is_enabled = group_config.get("enabled", "未设置 (跟随白名单)")
    sticker_enabled = group_config.get("sticker_enabled", True)
    schedule_enabled = group_config.get("schedule_enabled", False)
    tts_enabled = group_config.get(
        "tts_enabled",
        getattr(plugin_config, "personification_tts_group_default_enabled", True),
    )
    custom_prompt_len = len(group_config.get("custom_prompt", "")) if "custom_prompt" in group_config else 0
    prompt_status = f"自定义 ({custom_prompt_len} 字符)" if custom_prompt_len > 0 else "默认全局"
    remote_skill_stats = remote_skill_stats or {"total": 0, "pending": 0, "approved": 0, "rejected": 0}
    route_state = summarize_route_state(plugin_config)
    runtime_load_info = get_runtime_load_info(plugin_config)
    runtime_skipped = ", ".join(runtime_load_info.get("skipped_runtime_keys", [])[:12]) or "无"
    runtime_path = str(runtime_load_info.get("path", "") or "未记录")

    overview_conf_str = _join_pairs(
        _pair("主模型", plugin_config.personification_model, "负责日常聊天、联网查证和拟人回复的主要模型。"),
        _pair("API 类型", plugin_config.personification_api_type, "决定走哪类接口协议，例如 OpenAI / Gemini / Codex。"),
        _pair("API 池", provider_names, "多个模型源时显示当前可轮换的 provider 列表。"),
        _pair("时区", plugin_config.personification_timezone, "影响今天、早晚、作息模拟和定时主动发话的时间判断。"),
        _pair("思考预算", plugin_config.personification_thinking_budget, "控制模型思考开销，越高通常越稳但也更慢。"),
    )

    behavior_conf_str = _join_pairs(
        _pair("回复概率", plugin_config.personification_probability, "控制 Bot 在群里随机接话的基础活跃度。"),
        _pair("续聊概率", getattr(plugin_config, "personification_group_chat_follow_probability", 0.0), "Bot 刚接过话后，继续顺着当前话题聊下去的概率。"),
        _pair("热聊保留率", getattr(plugin_config, "personification_hot_chat_min_pass_rate", 0.0), "群里很热闹时仍允许 Bot 插话的最低保留比例，防止太安静或太抢话。"),
        _pair("主动私聊", _bool_text(plugin_config.personification_proactive_enabled), "允许 Bot 在私聊里主动发起话题，而不只是被动回复。"),
        _pair("主动消息概率", getattr(plugin_config, "personification_proactive_probability", 0.0), "定时检查命中后，真正发出主动消息的概率。"),
    )

    grounding_conf_str = _join_pairs(
        _pair("工具联网", _bool_text(getattr(plugin_config, "personification_tool_web_search_enabled", plugin_config.personification_web_search)), "控制 web_search 等外部联网工具是否可用。"),
        _pair("工具联网模式", getattr(plugin_config, "personification_tool_web_search_mode", "enabled"), "工具联网的模式开关，支持 enabled/cached/live/disabled。"),
        _pair("模型内置搜索", _bool_text(getattr(plugin_config, "personification_model_builtin_search_enabled", getattr(plugin_config, "personification_builtin_search", True))), "控制主模型是否允许直接使用 provider 原生 builtin search。"),
        _pair("群话题摘要", _bool_text(getattr(plugin_config, "personification_group_summary_enabled", True)), "持续概括不同群最近在聊什么，供后续续聊和联网检索使用。"),
        _pair("60 秒新闻", _bool_text(getattr(plugin_config, "personification_60s_enabled", True)), "允许接入每日新闻摘要类能力，帮助 Bot 感知现实中的新鲜事件。"),
        _pair("会话历史上限", session_history_limit, "数据库中每个会话最多保留多少条原始消息，超出后会滚动清理。"),
        _pair("压缩触发条数", getattr(plugin_config, "personification_compress_threshold", 0), "达到这个数量后，会把更早的历史压缩成摘要，减少 token 占用。"),
        _pair("压缩保留条数", getattr(plugin_config, "personification_compress_keep_recent", 0), "压缩后仍保留的最近原始消息条数，越大越容易续接当前话题。"),
        _pair("私聊送模条数", getattr(plugin_config, "personification_private_history_turns", 0), "私聊真正送进主模型的最近消息条数上限，直接影响长对话延续性。"),
        _pair("私聊过期小时", getattr(plugin_config, "personification_message_expire_hours", 0.0), "私聊旧消息超过这个时间后不再参与上下文；0 表示不过期。"),
        _pair("群过期小时", getattr(plugin_config, "personification_group_context_expire_hours", 0.0), "群聊旧消息超过这个时间后不再参与上下文；0 表示不过期。"),
        _pair("群摘要过期小时", getattr(plugin_config, "personification_group_summary_expire_hours", 0.0), "群话题摘要过期后不再自动注入，避免长期围绕旧话题。"),
        _pair("记忆召回条数", getattr(plugin_config, "personification_memory_recall_top_k", 0), "单次长期记忆召回默认取回的条数，越大越容易记住久远细节。"),
        _pair("画像更新阈值", getattr(plugin_config, "personification_persona_history_max", 0), "同一用户累计多少条新消息后触发一次画像更新。"),
    )

    media_conf_str = _join_pairs(
        _pair("表情包概率", plugin_config.personification_sticker_probability, "控制 Bot 发图或表情包的积极程度。"),
        _pair("戳一戳概率", plugin_config.personification_poke_probability, "控制被戳后随机回应的频率。"),
        _pair("语音总开关", _bool_text(plugin_config.personification_tts_enabled), "控制整个插件是否允许使用 TTS 语音能力。"),
        _pair("语音自动回复", _bool_text(plugin_config.personification_tts_auto_enabled), "开启后，Bot 会在部分场景自动改用语音回复。"),
        _pair("主路由", route_state["primary"], "主模型优先路由，聊天、视觉、打标和后台分析都会先走这里。"),
        _pair("全局兜底", route_state["fallback"], "主路由失败后统一进入的二级兜底，不再区分 style/persona/compress 等专用模型。"),
        _pair("视频兜底例外", route_state["video_fallback"], "视频理解允许单独覆盖兜底模型；留空时继承全局兜底。"),
        _pair("弃用别名折叠", route_state["fallback_source"] or "无", "显示当前是否仍在从旧的 labeler/style/persona/compress 配置折叠全局兜底。"),
        _pair("忽略的旧来源", route_state["fallback_ignored"] or "无", "当多个旧配置同时存在且冲突时，这里显示被忽略的来源。"),
        _pair("运行时配置文件", runtime_path, "runtime_config.json 的实际读取路径；重启时只会补 env 未显式设置的字段。"),
        _pair("env 覆盖的 runtime 键", runtime_skipped, "这些运行时配置在启动时被 .env.prod 或进程环境中的显式值覆盖，没有回填。"),
    )

    remote_skill_conf_str = _join_pairs(
        _pair("远程 skill", _bool_text(getattr(plugin_config, "personification_skill_remote_enabled", False)), "允许从远程来源拉取额外技能包。"),
        _pair("外部执行", _bool_text(getattr(plugin_config, "personification_skill_allow_unsafe_external", False)), "是否允许执行外部来源的 Python 或 MCP skill，关闭时更安全。"),
        _pair("人工审批", _bool_text(getattr(plugin_config, "personification_skill_require_admin_review", True)), "远程 skill 加载前必须由管理员手动同意，避免自动执行外部代码。"),
        _pair(
            "审批统计",
            f"总计 {remote_skill_stats['total']} / 待审批 {remote_skill_stats['pending']} / 已批准 {remote_skill_stats['approved']} / 已拒绝 {remote_skill_stats['rejected']}",
            "显示当前配置里的远程 skill 审批进度，可配合“远程技能审批”命令使用。",
        ),
    )

    group_conf_str = _join_pairs(
        _pair("当前群号", group_id, "本条配置聊天记录对应的群。"),
        _pair("拟人功能", is_enabled, "控制本群是否允许 Bot 参与拟人聊天。"),
        _pair("表情包开关", _bool_text(sticker_enabled), "只影响本群发图/表情包，不影响其他群。"),
        _pair("作息模拟", _bool_text(schedule_enabled), "开启后会按设定时段模拟在线/休息状态。"),
        _pair("语音回复", _bool_text(tts_enabled), "控制本群是否允许自动或手动语音回复。"),
        _pair("人设配置", prompt_status, "显示当前群是沿用全局人格，还是使用单独的人设提示词。"),
    )

    return [
        {
            "type": "node",
            "data": {
                "name": "配置总览",
                "uin": str(bot_self_id),
                "content": overview_conf_str,
            },
        },
        {
            "type": "node",
            "data": {
                "name": "聊天活跃度",
                "uin": str(bot_self_id),
                "content": behavior_conf_str,
            },
        },
        {
            "type": "node",
            "data": {
                "name": "联网与查证",
                "uin": str(bot_self_id),
                "content": grounding_conf_str,
            },
        },
        {
            "type": "node",
            "data": {
                "name": "视觉与语音",
                "uin": str(bot_self_id),
                "content": media_conf_str,
            },
        },
        {
            "type": "node",
            "data": {
                "name": "远程技能",
                "uin": str(bot_self_id),
                "content": remote_skill_conf_str,
            },
        },
        {
            "type": "node",
            "data": {
                "name": "当前群配置",
                "uin": str(bot_self_id),
                "content": group_conf_str,
            },
        },
    ]
