import shlex
from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg

from ..core.tts_service import extract_persona_tts_config


def _parse_tts_command_args(raw_text: str) -> tuple[dict[str, str | None], str | None]:
    text = str(raw_text or "").strip()
    empty = {
        "mode": None,
        "voice": None,
        "style": None,
        "voice_prompt": None,
        "voice_clone": None,
        "voice_clone_path": None,
        "model": None,
        "text": "",
    }
    if not text:
        return empty, None
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        return empty, f"参数解析失败: {e}"

    options = dict(empty)
    content_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
            token = f"--{key.strip()}"
            tokens.insert(index + 1, value)
        if token in {"--mode", "--voice", "--style", "--voice-prompt", "--design", "--clone-voice", "--voice-clone", "--clone-path", "--clone-file", "--model"}:
            if index + 1 >= len(tokens):
                return empty, f"缺少 {token} 的值。"
            value = tokens[index + 1].strip() or None
            if token == "--mode":
                options["mode"] = value
            elif token == "--voice":
                options["voice"] = value
            elif token == "--style":
                options["style"] = value
            elif token in {"--voice-prompt", "--design"}:
                options["voice_prompt"] = value
                options["mode"] = options["mode"] or "design"
            elif token in {"--clone-voice", "--voice-clone"}:
                options["voice_clone"] = value
                options["mode"] = options["mode"] or "clone"
            elif token in {"--clone-path", "--clone-file"}:
                options["voice_clone_path"] = value
                options["mode"] = options["mode"] or "clone"
            elif token == "--model":
                options["model"] = value
            index += 1
        else:
            content_parts.append(token)
        index += 1
    options["text"] = " ".join(content_parts).strip()
    return options, None


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

        parsed, error = _parse_tts_command_args(args.extract_plain_text())
        if error:
            await tts_cmd.finish(error)
        text = str(parsed.get("text") or "").strip()
        if not text:
            await tts_cmd.finish(
                f"用法: {main_command} [--mode preset|design|clone] "
                "[--voice 音色] [--style 风格] [--voice-prompt 音色描述] [--clone-voice data:...] [--clone-path 样本路径] 文本"
            )

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

        persona_tts = extract_persona_tts_config(base_prompt)
        tts_decision = None
        try:
            tts_decision = await tts_service.decide_tts_delivery(
                text=text,
                is_private=is_private,
                command_triggered=True,
                raw_message_text=text,
                group_style=group_style,
                fallback_style_hint=str(parsed.get("style") or ""),
                command_options=parsed,
                persona_tts=persona_tts,
            )
        except Exception as e:
            logger.warning(f"[tts] 命令语音审查失败: {e}")
            await tts_cmd.finish("语音审查暂时失败，先不合成语音。")
        if tts_decision is None:
            await tts_cmd.finish("语音审查暂时失败，先不合成语音。")
        if tts_decision.action != "voice":
            fallback_message = (
                tts_decision.visible_message
                or ("这段内容不适合合成语音。" if tts_decision.action == "block" else "这段我先不发语音。")
            )
            await tts_cmd.finish(fallback_message)

        try:
            sent = await tts_service.send_tts(
                bot=bot,
                event=event,
                message_segment_cls=message_segment_cls,
                text=text,
                mode_hint=parsed.get("mode"),
                voice_hint=parsed.get("voice"),
                style_hint=tts_decision.style_hint or parsed.get("style"),
                voice_prompt_hint=parsed.get("voice_prompt"),
                voice_clone_hint=parsed.get("voice_clone"),
                voice_clone_path_hint=parsed.get("voice_clone_path"),
                model_hint=parsed.get("model"),
                user_hint="请自然朗读下面的内容，整体语速略快一点，但保持自然清晰。",
                is_private=is_private,
                group_style=group_style,
                persona_tts=persona_tts,
            )
        except Exception as e:
            logger.warning(f"[tts] 命令合成失败: {e}")
            await tts_cmd.finish(f"TTS 合成失败: {e}")

        if not sent:
            await tts_cmd.finish("没有可发送的语音内容。")

    return {"tts_cmd": tts_cmd}


__all__ = ["register_tts_matchers"]
