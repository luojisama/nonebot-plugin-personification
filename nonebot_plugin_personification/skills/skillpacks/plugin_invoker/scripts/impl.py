from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from plugin.personification.agent.tool_registry import AgentTool


INVOKE_PLUGIN_DESCRIPTION = """代为执行同一 bot 上其它已安装插件的命令，并返回该插件的输出。
适合场景：用户不是在问插件原理，而是想直接用某个插件功能（查天气、签到、点歌、查询等）。
使用前请先用 search_plugin_knowledge / list_plugin_features 定位目标插件以及它的命令触发方式，
再把【完整命令文本】传给本工具，例如 plugin_name="weather", command_text="/天气 北京"。
返回的是插件本来要发给用户的内容；拿到后请用你自己的人设语气转述，不要让用户自己去敲命令。
注意：本工具会以当前用户的身份在内部实际执行该命令，等同于用户亲自发送，请只在用户确有此需求时使用。
不能用本工具执行管理员/危险命令（封禁、踢人、删除、关机、全局开关、发奖、充值等），也不能调用我自己的命令。"""


# 发送类 API：这些调用要被缓冲捕获而不是真正发出
_SEND_APIS = frozenset(
    {
        "send_msg",
        "send_group_msg",
        "send_private_msg",
        "send_group_forward_msg",
        "send_private_forward_msg",
        "send",
    }
)

# 危险/管理员命令关键词（中英文）。命中即拒绝执行。
_DANGER_KEYWORDS = (
    # 中文
    "封", "禁言", "解禁", "踢", "撤回", "删除", "清空", "清除", "重置", "关机",
    "重启", "关闭", "停用", "广播", "全员", "全局", "解散", "退群", "转账", "提现",
    "改密码", "授权", "提权", "封禁", "拉黑", "格式化", "清库", "发奖", "充值",
    "扣款", "提款", "收款", "改名", "禁用", "卸载", "安装",
    # 英文
    "ban", "kick", "mute", "delete", "remove", "clear", "reset", "shutdown",
    "reboot", "restart", "broadcast", "global", "sudo", "admin", "grant",
    "revoke", "drop", "exec", "eval", "wipe", "purge", "format", "truncate",
    "destroy", "uninstall", "install",
)

# 触发方式 description 里出现这些词，视为需要管理员权限的命令
_ADMIN_HINT_KEYWORDS = ("superuser", "super user", "管理员", "群主", "admin", "permission", "owner")


def _cfg(plugin_config: Any, name: str, default: Any) -> Any:
    return getattr(plugin_config, name, default)


def _render_segments_to_text(message: Any) -> str:
    """把 OneBot v11 Message / 字符串 渲染成纯文本，富媒体段转成占位符。"""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    parts: list[str] = []
    try:
        segments = list(message)
    except TypeError:
        return str(message)
    for seg in segments:
        seg_type = str(getattr(seg, "type", "") or "").strip().lower()
        data = getattr(seg, "data", {}) or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "") or ""))
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "at":
            parts.append(f"@{data.get('qq', '')}")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type in {"forward", "node"}:
            parts.append("[合并转发]")
        elif seg_type == "file":
            parts.append("[文件]")
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def _render_forward_messages(nodes: Any) -> str:
    """渲染合并转发（send_*_forward_msg 的 messages 节点列表）为纯文本。"""
    if not nodes:
        return ""
    try:
        node_list = list(nodes)
    except TypeError:
        return ""
    parts: list[str] = []
    for node in node_list:
        data = getattr(node, "data", None)
        if data is None and isinstance(node, dict):
            data = node.get("data", {})
        data = data or {}
        content = data.get("content")
        text = _render_segments_to_text(content) if content is not None else ""
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _make_capturing_bot(real_bot: Any) -> tuple[Any, list[str]]:
    """构造一个代理 Bot：发送类 call_api 被缓冲捕获，其它 API 透传给真 bot。

    用「子类化真 Bot 类 + 拷 __dict__」而不是组合代理，因为 OneBot v11 的
    send_group_msg / send 等是动态/继承方法，最终都会落到 self.call_api，
    覆写 call_api 才能可靠拦截。
    """
    captured: list[str] = []
    bot_cls = type(real_bot)

    class _CapturingBot(bot_cls):  # type: ignore[valid-type, misc]
        async def call_api(self, api: str, **data: Any) -> Any:
            if api in _SEND_APIS:
                if "message" in data:
                    captured.append(_render_segments_to_text(data.get("message", "")))
                elif "messages" in data:  # 合并转发
                    captured.append(_render_forward_messages(data.get("messages")))
                return {"message_id": 0}
            return await bot_cls.call_api(self, api, **data)

    proxy = _CapturingBot.__new__(_CapturingBot)
    # 共享真 bot 的连接 / adapter / self_id 等内部状态
    proxy.__dict__ = dict(real_bot.__dict__)
    return proxy, captured


def _build_synthetic_event(event: Any, command_text: str) -> Any:
    """克隆当前消息事件，替换为目标命令文本，并打上防递归标记。"""
    from nonebot.adapters.onebot.v11 import Message

    new_msg = Message(command_text)
    update = {
        "message": new_msg,
        "raw_message": command_text,
        "to_me": True,
        "_personification_synthetic": True,
    }
    try:
        clone = event.model_copy(update=update, deep=True)  # pydantic v2
    except AttributeError:
        try:
            clone = event.copy(update=update)  # pydantic v1
        except Exception:
            clone = event
            try:
                clone.message = new_msg
                clone.raw_message = command_text
                clone.to_me = True
            except Exception:
                pass
    # 双保险：确保标记一定挂上（update 可能对额外字段不生效）
    try:
        object.__setattr__(clone, "_personification_synthetic", True)
    except Exception:
        try:
            clone._personification_synthetic = True  # type: ignore[attr-defined]
        except Exception:
            pass
    return clone


def _normalize_command_head(text: str) -> str:
    head = str(text or "").strip()
    head = head.lstrip("/!！.。#")
    head = head.strip()
    # 取首个 token（命令名），按空白/中文常见分隔切
    token = re.split(r"\s+", head, maxsplit=1)[0] if head else ""
    return token.strip().lower()


def _command_matches_triggers(command_text: str, triggers: list[Any]) -> bool:
    """校验 command_text 的前缀确实属于该插件的某个已索引触发方式。"""
    if not isinstance(triggers, list) or not triggers:
        return False
    raw = str(command_text or "").strip()
    if not raw:
        return False
    head = _normalize_command_head(raw)
    raw_lower = raw.lower()
    stripped_lower = raw.lstrip("/!！.。#").strip().lower()
    for trig in triggers:
        if not isinstance(trig, dict):
            continue
        ttype = str(trig.get("type", "") or "").strip().lower()
        pattern = str(trig.get("pattern", "") or "").strip()
        if not pattern:
            continue
        if ttype == "regex":
            try:
                if re.match(pattern, raw) or re.match(pattern, stripped_lower):
                    return True
            except re.error:
                continue
            continue
        pat_head = _normalize_command_head(pattern)
        pat_lower = pattern.lower()
        if ttype in {"command", ""}:
            if pat_head and pat_head == head:
                return True
            if pat_lower and stripped_lower.startswith(pat_lower):
                return True
        else:  # keyword / message / 其它：要求命令以该关键词开头，避免在合法关键词后夹带任意命令
            if pat_lower and (stripped_lower.startswith(pat_lower) or raw_lower.startswith(pat_lower)):
                return True
    return False


def _is_self_plugin(plugin_name: str) -> bool:
    """判断是否为 personification 自身，防止递归调用自己。"""
    name = str(plugin_name or "").strip().lower()
    if name in {"personification", "拟人化聊天", "拟人"}:
        return True
    try:
        import nonebot

        for plugin in list(nonebot.get_loaded_plugins() or []):
            pname = str(getattr(plugin, "name", "") or "").strip()
            if pname.lower() != name:
                continue
            module = getattr(plugin, "module", None)
            module_name = str(getattr(module, "__name__", "") or "")
            if pname == "personification" or module_name.endswith("personification"):
                return True
    except Exception:
        pass
    return False


def _is_dangerous(
    command_text: str,
    plugin_name: str,
    entry: dict[str, Any],
    plugin_config: Any,
) -> tuple[bool, str]:
    """危险/管理员命令过滤 + allow/blocklist。返回 (是否拦截, 原因)。"""
    name = str(plugin_name or "").strip()
    cmd = str(command_text or "").strip()
    head = _normalize_command_head(cmd)

    allowlist = [str(x).strip().lower() for x in (_cfg(plugin_config, "personification_plugin_invoker_allowlist", []) or []) if str(x).strip()]
    if allowlist and name.lower() not in allowlist:
        return True, f"插件 {name} 不在允许名单内"

    blocklist = [str(x).strip().lower() for x in (_cfg(plugin_config, "personification_plugin_invoker_blocklist", []) or []) if str(x).strip()]
    for item in blocklist:
        # 支持 "插件名" 或 "插件名:命令"
        if ":" in item:
            blk_plugin, blk_cmd = item.split(":", 1)
            blk_plugin = blk_plugin.strip()
            blk_cmd = blk_cmd.strip()
            # 命令部分按命令名（head）匹配，避免子串误伤合法命令的参数
            if blk_plugin == name.lower() and blk_cmd and _normalize_command_head(blk_cmd) == head:
                return True, "命中黑名单"
        elif item == name.lower():
            return True, "插件在黑名单内"

    extra = [str(x).strip().lower() for x in (_cfg(plugin_config, "personification_plugin_invoker_extra_danger_keywords", []) or []) if str(x).strip()]
    danger_words = tuple(_DANGER_KEYWORDS) + tuple(extra)

    cmd_lower = cmd.lower()
    for word in danger_words:
        if word and (word in cmd_lower or word in head):
            return True, f"命令疑似危险/管理操作（命中“{word}”）"

    # 命中的触发方式 / 入口若标注需要管理员权限，也拦截
    for collection_key in ("triggers", "entrypoints"):
        for item in entry.get(collection_key, []) or []:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern", "") or item.get("name", "") or "")
            if not pattern:
                continue
            if _normalize_command_head(pattern) != head and pattern.lower() not in cmd_lower:
                continue
            desc = str(item.get("description", "") or "").lower()
            if any(hint in desc for hint in _ADMIN_HINT_KEYWORDS):
                return True, "该命令疑似需要管理员权限"

    return False, ""


async def _execute_synthetic(
    bot: Any,
    event: Any,
    command_text: str,
    *,
    plugin_config: Any,
    logger: Any,
) -> str:
    """合成事件 + 代理 Bot 捕获 + handle_event 重新分发，返回捕获到的插件输出。"""
    from nonebot.message import handle_event

    timeout = float(_cfg(plugin_config, "personification_plugin_invoker_capture_timeout", 15.0) or 15.0)
    max_chars = int(_cfg(plugin_config, "personification_plugin_invoker_max_output_chars", 1500) or 1500)

    proxy_bot, captured = _make_capturing_bot(bot)
    synthetic = _build_synthetic_event(event, command_text)

    incomplete_note = ""
    try:
        await asyncio.wait_for(handle_event(proxy_bot, synthetic), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"[plugin_invoker] 执行命令超时: {command_text}")
        if not captured:
            return f"（插件执行 `{command_text}` 超时，没有及时返回结果。）"
        incomplete_note = "（注意：插件执行超时，以下可能是不完整的输出）\n"
    except Exception as e:
        logger.warning(f"[plugin_invoker] 执行命令失败: {command_text}: {e}")
        if not captured:
            return f"（尝试执行 `{command_text}` 时出错，没有拿到插件结果。）"
        incomplete_note = "（注意：插件执行中途出错，以下可能是不完整的输出）\n"

    outputs = [text for text in captured if str(text or "").strip()]
    if not outputs:
        return f"（执行了 `{command_text}`，但目标插件这次没有输出可见内容。）"

    combined = "\n".join(outputs).strip()
    if len(combined) > max_chars:
        combined = combined[:max_chars].rstrip() + "…（内容过长已截断）"
    return incomplete_note + "以下是插件的原始输出，请用你自己的人设语气自然地转述给用户：\n" + combined


def build_invoke_plugin_tool(
    *,
    bot: Any,
    event: Any,
    knowledge_store: Any,
    plugin_config: Any,
    logger: Any,
) -> AgentTool:
    call_counter = {"n": 0}

    async def _handler(plugin_name: str = "", command_text: str = "", **_extra: Any) -> str:
        if not bool(_cfg(plugin_config, "personification_plugin_invoker_enabled", False)):
            return "插件代调用功能未启用。"
        if knowledge_store is None:
            return "插件知识库不可用，无法定位插件。"

        plugin_name = str(plugin_name or "").strip()
        command_text = str(command_text or "").strip()
        if not plugin_name or not command_text:
            return "需要同时提供 plugin_name 和完整的 command_text。"

        max_calls = int(_cfg(plugin_config, "personification_plugin_invoker_max_calls_per_turn", 2) or 2)
        if max_calls > 0 and call_counter["n"] >= max_calls:
            return "本轮代为执行插件命令的次数已达上限。"

        # 解析真实插件名
        try:
            matched = knowledge_store.search_plugins(plugin_name, top_k=1)
        except Exception as e:
            logger.debug(f"[plugin_invoker] search_plugins failed: {e}")
            matched = []
        actual = matched[0] if matched else plugin_name

        if _is_self_plugin(actual):
            return "不能调用我自己的命令。"

        try:
            entry = knowledge_store.load_plugin_entry_sync(actual) or {}
        except Exception as e:
            logger.debug(f"[plugin_invoker] load_plugin_entry_sync failed: {e}")
            entry = {}

        triggers = entry.get("triggers", []) if isinstance(entry, dict) else []
        if not _command_matches_triggers(command_text, triggers):
            return (
                f"`{command_text}` 不是插件 {actual} 的已知触发方式，没有代为执行。"
                "请先用 search_plugin_knowledge / list_plugin_features 确认它的命令格式。"
            )

        blocked, reason = _is_dangerous(command_text, actual, entry, plugin_config)
        if blocked:
            return f"出于安全考虑没有执行该命令（{reason}）。"

        call_counter["n"] += 1
        logger.info(f"[plugin_invoker] 代为执行 plugin={actual} command={command_text}")
        return await _execute_synthetic(
            bot,
            event,
            command_text,
            plugin_config=plugin_config,
            logger=logger,
        )

    return AgentTool(
        name="invoke_plugin",
        description=INVOKE_PLUGIN_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "plugin_name": {
                    "type": "string",
                    "description": "目标插件名，需先用 search_plugin_knowledge 确认。",
                },
                "command_text": {
                    "type": "string",
                    "description": "要执行的完整命令文本，例如 /天气 北京。",
                },
            },
            "required": ["plugin_name", "command_text"],
        },
        handler=_handler,
        metadata={
            "intent_tags": ["plugin_question", "plugin_local", "plugin_action"],
            "evidence_kind": "plugin_action",
            "requires_network": False,
            "requires_image": False,
            "latency_class": "slow",
            "risk_level": "medium",
            "side_effect": "external",
            "final_behavior": "continue",
            "retryable": False,
        },
    )
