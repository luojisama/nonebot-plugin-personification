from typing import Any, Dict, Optional, Tuple


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
) -> list[Dict[str, Any]]:
    global_conf_str = (
        f"API 类型: {plugin_config.personification_api_type}\n"
        f"模型名称: {plugin_config.personification_model}\n"
        f"API URL: {plugin_config.personification_api_url}\n"
        f"API 池: {provider_names}\n"
        f"回复概率: {plugin_config.personification_probability}\n"
        f"戳一戳概率: {plugin_config.personification_poke_probability}\n"
        f"表情包概率: {plugin_config.personification_sticker_probability}\n"
        f"联网搜索: {'开启' if plugin_config.personification_web_search else '关闭'}\n"
        f"私聊上下文长度: {session_history_limit} (固定，超出清空重记)\n"
        f"群聊上下文长度: {session_history_limit} (固定，超出清空重记)\n"
        f"思考预算: {plugin_config.personification_thinking_budget}"
    )

    is_enabled = group_config.get("enabled", "未设置 (跟随白名单)")
    sticker_enabled = group_config.get("sticker_enabled", True)
    schedule_enabled = group_config.get("schedule_enabled", False)
    custom_prompt_len = len(group_config.get("custom_prompt", "")) if "custom_prompt" in group_config else 0
    prompt_status = f"自定义 ({custom_prompt_len} 字符)" if custom_prompt_len > 0 else "默认全局"

    group_conf_str = (
        f"当前群号: {group_id}\n"
        f"拟人功能开关: {is_enabled}\n"
        f"表情包开关: {'开启' if sticker_enabled else '关闭'}\n"
        f"作息模拟开关: {'开启' if schedule_enabled else '关闭'}\n"
        f"人设配置: {prompt_status}"
    )

    return [
        {
            "type": "node",
            "data": {
                "name": "全局配置",
                "uin": str(bot_self_id),
                "content": global_conf_str,
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
