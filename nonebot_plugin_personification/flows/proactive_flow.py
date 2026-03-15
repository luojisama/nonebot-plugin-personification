import json
import random
import re
import time
from typing import Any, Awaitable, Callable, Dict, Optional


async def run_proactive_messaging(
    target_user_id: Optional[str],
    bypass_checks: bool,
    *,
    plugin_config: Any,
    sign_in_available: bool,
    is_rest_time: Callable[..., bool],
    get_bots: Callable[[], Dict[str, Any]],
    load_data: Callable[[], Dict[str, Any]],
    load_proactive_state: Callable[[], Dict[str, Any]],
    save_proactive_state: Callable[[Dict[str, Any]], None],
    get_user_data: Callable[[str], Dict[str, Any]],
    get_level_name: Callable[[float], str],
    get_now: Callable[[], Any],
    get_activity_status: Callable[[], str],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    parse_yaml_response: Callable[[str], Dict[str, Any]],
    logger: Any,
) -> str:
    if not bypass_checks and not plugin_config.personification_proactive_enabled:
        return "主动消息功能未开启"

    if not sign_in_available:
        return "签到插件未加载，无法获取好感度数据"

    if not bypass_checks and plugin_config.personification_schedule_global:
        if not is_rest_time(allow_unsuitable_prob=0.2):
            return "当前不是休息时间"

    try:
        bots = get_bots()
        if not bots:
            return "未找到 Bot 实例"
        bot = list(bots.values())[0]
    except Exception as e:
        return f"获取 Bot 实例失败: {e}"

    all_data = load_data()
    if not all_data:
        return "未找到用户数据"

    proactive_state = load_proactive_state()
    today_str = get_now().strftime("%Y-%m-%d")
    current_ts = time.time()
    interval_seconds = plugin_config.personification_proactive_interval * 60
    proactive_idle_seconds = max(0.0, float(getattr(plugin_config, "personification_proactive_idle_hours", 24.0))) * 3600
    must_send_due_to_idle = False

    if not target_user_id:
        normal_candidates = []
        overdue_candidates = []

        for user_id, user_data in all_data.items():
            if not user_data or user_id.startswith("group_"):
                continue

            try:
                fav = float(user_data.get("favorability", 0.0))
            except (ValueError, TypeError):
                fav = 0.0

            if not bypass_checks and fav < plugin_config.personification_proactive_threshold:
                continue
            if user_data.get("is_perm_blacklisted", False):
                continue

            user_state = proactive_state.get(user_id, {})
            last_interaction = float(user_state.get("last_interaction", 0) or 0)
            last_proactive_at = float(user_state.get("last_proactive_at", 0) or 0)
            last_date = user_state.get("last_date", "")
            daily_count = int(user_state.get("count", 0) or 0)
            if last_date != today_str:
                daily_count = 0

            if not bypass_checks and current_ts - last_interaction < interval_seconds:
                continue

            is_overdue = proactive_idle_seconds > 0 and (
                last_proactive_at <= 0 or current_ts - last_proactive_at >= proactive_idle_seconds
            )
            if is_overdue:
                overdue_candidates.append(user_id)
                continue

            if (
                not bypass_checks
                and plugin_config.personification_proactive_daily_limit > 0
                and daily_count >= plugin_config.personification_proactive_daily_limit
            ):
                continue

            normal_candidates.append(user_id)

        if overdue_candidates:
            must_send_due_to_idle = True
            target_user_id = random.choice(overdue_candidates)
        else:
            if not normal_candidates:
                return "没有符合条件的目标用户"
            if not bypass_checks and random.random() > plugin_config.personification_proactive_probability:
                return "主动私聊本轮未命中概率门槛"
            target_user_id = random.choice(normal_candidates)

    try:
        user_data = get_user_data(target_user_id)
        if not user_data:
            return f"无法获取用户 {target_user_id} 的数据"

        fav = user_data.get("favorability", 0.0)
        level_name = get_level_name(fav)
        now = get_now()
        activity_status = get_activity_status()
        absolute_time = now.strftime("%Y-%m-%d %H:%M (JST/UTC+9)")
        activity_line = f"Current absolute time: {absolute_time}; current status: {activity_status}\n"
        activity_context_line = f"Current status: {activity_status} (absolute time: {absolute_time})\n"
        schedule_considerations = (
            "1. 如果是深夜或上课时间，应尽量避免打扰。\n"
            "2. 如果是休息时间，可以尝试发起话题。\n"
            "3. 请根据你的人设自然决定。\n\n"
        )

        raw_prompt_data = load_prompt()
        if isinstance(raw_prompt_data, dict):
            base_persona = raw_prompt_data.get("system", "")
            bot_name = raw_prompt_data.get("name", "")
            if bot_name:
                base_persona = f"你的角色名是{bot_name}。\n\n" + base_persona
        else:
            base_persona = raw_prompt_data or ""

        decision_prompt = (
            f"{base_persona}\n\n"
            f"## 决策任务\n"
            f"{activity_line}"
            f"目标对象：你的好友（好感度：{fav}，关系：{level_name}）\n"
            f"任务：请判断现在是否适合主动向对方发起私聊对话？\n"
            f"考虑因素：\n"
            f"{schedule_considerations}"
            f"请只输出 JSON：{{\"should_send\": true/false, \"reason\": \"原因\"}}"
        )

        should_send = must_send_due_to_idle
        reason = "超过 24 小时未主动私聊，触发保底发送" if must_send_due_to_idle else "未决策"
        if not must_send_due_to_idle:
            decision_messages = [{"role": "system", "content": decision_prompt}]
            decision_reply = await call_ai_api(decision_messages, temperature=0.5)
            try:
                if decision_reply:
                    json_match = re.search(r"\{.*\}", decision_reply, re.DOTALL)
                    if json_match:
                        decision_data = json.loads(json_match.group(0))
                        should_send = decision_data.get("should_send", False)
                        reason = decision_data.get("reason", "未知")
            except Exception as e:
                logger.error(f"拟人插件：主动私聊决策解析失败: {e}")

        if not should_send and not bypass_checks:
            return f"AI 决定不发送消息（原因: {reason}）"

        web_search_hint = ""
        if plugin_config.personification_web_search:
            web_search_hint = "（你可以利用联网能力获取当前热门话题或新闻来开启对话）"

        system_prompt = (
            f"{base_persona}\n\n"
            f"## 当前情境\n"
            f"{activity_context_line}"
            f"你的一位好友（好感度：{fav}，关系：{level_name}）现在可能在线。\n"
            f"你决定主动找对方聊两句。\n"
            f"{web_search_hint}\n\n"
            f"## 生成要求\n"
            f"1. 必须严格符合人设，语气自然、口语化。\n"
            f"2. 内容尽量短，像即时消息。\n"
            f"3. 不要写成正式信件。\n"
            f"4. 严禁输出 [SILENCE] 或 [NO_REPLY]。\n"
        )

        prompt_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "（看着手机，想给对方发个消息……）"},
        ]

        reply = await call_ai_api(prompt_messages, temperature=0.9)
        if not reply:
            return "AI 未生成回复"

        try:
            parsed = parse_yaml_response(reply)
            if parsed["messages"]:
                reply_content = parsed["messages"][0]["text"]
            else:
                raise ValueError("No messages found")
        except Exception:
            reply_content = reply.strip().strip('"').strip("'")
            reply_content = re.sub(r"<status.*?>.*?</\\s*status\\s*>", "", reply_content, flags=re.DOTALL | re.IGNORECASE)
            reply_content = re.sub(r"<think.*?>.*?</\\s*think\\s*>", "", reply_content, flags=re.DOTALL | re.IGNORECASE)
            reply_content = re.sub(r"</?\\s*output.*?>", "", reply_content, flags=re.IGNORECASE)
            reply_content = re.sub(r"</?\\s*message.*?>", "", reply_content, flags=re.IGNORECASE).strip()

        if not reply_content:
            return "AI 生成内容为空"
        if "[SILENCE]" in reply_content or "[NO_REPLY]" in reply_content:
            if must_send_due_to_idle:
                reply_content = "在吗"
            else:
                return "AI 决定不回复"

        await bot.send_private_msg(user_id=int(target_user_id), message=reply_content)
        logger.info(f"拟人插件：主动向用户 {target_user_id} 发送消息: {reply_content}")

        user_state = proactive_state.get(target_user_id, {})
        current_count = 0
        last_date = user_state.get("last_date", "")
        if last_date == today_str:
            current_count = int(user_state.get("count", 0) or 0)

        proactive_state[target_user_id] = {
            "last_date": today_str,
            "count": current_count + 1,
            "last_proactive_at": time.time(),
            "last_interaction": time.time(),
        }
        save_proactive_state(proactive_state)
        return f"成功向用户 {target_user_id} 发送消息: {reply_content}"

    except Exception as e:
        logger.error(f"拟人插件：主动发送消息任务异常: {e}")
        return f"发送消息过程中发生异常: {e}"


def build_proactive_checker(
    *,
    plugin_config: Any,
    sign_in_available: bool,
    is_rest_time: Callable[..., bool],
    get_bots: Callable[[], Dict[str, Any]],
    load_data: Callable[[], Dict[str, Any]],
    load_proactive_state: Callable[[], Dict[str, Any]],
    save_proactive_state: Callable[[Dict[str, Any]], None],
    get_user_data: Callable[[str], Dict[str, Any]],
    get_level_name: Callable[[float], str],
    get_now: Callable[[], Any],
    get_activity_status: Callable[[], str],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    parse_yaml_response: Callable[[str], Dict[str, Any]],
    logger: Any,
) -> Callable[[Optional[str], bool], Awaitable[str]]:
    async def _checker(target_user_id: Optional[str] = None, bypass_checks: bool = False) -> str:
        return await run_proactive_messaging(
            target_user_id,
            bypass_checks,
            plugin_config=plugin_config,
            sign_in_available=sign_in_available,
            is_rest_time=is_rest_time,
            get_bots=get_bots,
            load_data=load_data,
            load_proactive_state=load_proactive_state,
            save_proactive_state=save_proactive_state,
            get_user_data=get_user_data,
            get_level_name=get_level_name,
            get_now=get_now,
            get_activity_status=get_activity_status,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            parse_yaml_response=parse_yaml_response,
            logger=logger,
        )

    return _checker
