import shlex
from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg

from ..core.tts_service import extract_persona_tts_config


def _parse_tts_command_args(raw_text: str) -> tuple[str | None, str | None, str, str | None]:
    text = str(raw_text or "").strip()
    if not text:
        return None, None, "", None
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        return None, None, "", f"参数解析失败: {e}"

    voice: str | None = None
    style: str | None = None
    content_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--voice="):
            voice = token.split("=", 1)[1].strip() or None
        elif token == "--voice":
            if index + 1 >= len(tokens):
                return None, None, "", "缺少 --voice 的值。"
            voice = tokens[index + 1].strip() or None
            index += 1
        elif token.startswith("--style="):
            style = token.split("=", 1)[1].strip() or None
        elif token == "--style":
            if index + 1 >= len(tokens):
                return None, None, "", "缺少 --style 的值。"
            style = tokens[index + 1].strip() or None
            index += 1
        else:
            content_parts.append(token)
        index += 1
    return voice, style, " ".join(content_parts).strip(), None


def register_tts_matchers(
    *,
    plugin_config: Any,
    message_segment_cls: Any,
    logger: Any,
    tts_service: Any,
    load_prompt: Callable[[str | None], Any],
    get_group_style: Callable[[str], str],
    group_message_event_cls: Any,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> Dict[str, Any]:
    command_prefixes = list(getattr(plugin_config, "personification_tts_command_prefixes", None) or ["说", "朗读", "配音"])
    main_command = str(command_prefixes[0]).strip() if command_prefixes else "说"
    aliases = {str(item).strip() for item in command_prefixes[1:] if str(item).strip()}
    if track_command_keywords:
        track_command_keywords(main_command, aliases or None)

    if aliases:
        tts_cmd = on_command(main_command, aliases=aliases, priority=5, block=True)
    else:
        tts_cmd = on_command(main_command, priority=5, block=True)

    @tts_cmd.handle()
    async def _handle_tts(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        if tts_service is None or not tts_service.is_enabled():
            await tts_cmd.finish("TTS 未启用。")
        if not tts_service.is_configured():
            await tts_cmd.finish("TTS 未配置 API Key。")

        voice, style, text, error = _parse_tts_command_args(args.extract_plain_text())
        if error:
            await tts_cmd.finish(error)
        if not text:
            await tts_cmd.finish(f"用法: {main_command} [--voice 音色] [--style 风格] 文本")

        is_private = not isinstance(event, group_message_event_cls)
        group_style = ""
        base_prompt = load_prompt(None)
        if isinstance(event, GroupMessageEvent):
            try:
                group_style = get_group_style(str(event.group_id))
            except Exception as e:
                logger.debug(f"[tts] 读取群风格失败: {e}")
            try:
                base_prompt = load_prompt(str(event.group_id))
            except Exception as e:
                logger.debug(f"[tts] 读取人设配置失败: {e}")

        try:
            sent = await tts_service.send_tts(
                bot=bot,
                event=event,
                message_segment_cls=message_segment_cls,
                text=text,
                voice_hint=voice,
                style_hint=style,
                user_hint="请自然朗读下面的内容，整体语速略快一点，但保持自然清晰。",
                is_private=is_private,
                group_style=group_style,
                persona_tts=extract_persona_tts_config(base_prompt),
            )
        except Exception as e:
            logger.warning(f"[tts] 命令合成失败: {e}")
            await tts_cmd.finish(f"TTS 合成失败: {e}")

        if not sent:
            await tts_cmd.finish("没有可发送的语音内容。")

    return {"tts_cmd": tts_cmd}


__all__ = ["register_tts_matchers"]
