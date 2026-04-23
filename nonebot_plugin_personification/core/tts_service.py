import asyncio
import base64
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx


STYLE_TAG_RE = re.compile(r"^\s*<style>(.*?)</style>\s*", re.IGNORECASE | re.DOTALL)
LATIN_RE = re.compile(r"[A-Za-z]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class TtsStyleDecision:
    text: str
    voice: str
    style: str | None = None


def extract_persona_tts_config(base_prompt: Any) -> dict[str, str]:
    if not isinstance(base_prompt, dict):
        return {}
    value = base_prompt.get("tts")
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("voice", "style", "user_hint"):
        raw = value.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            result[key] = text
    return result


class TtsService:
    def __init__(
        self,
        *,
        plugin_config: Any,
        logger: Any,
        get_http_client: Callable[[], httpx.AsyncClient],
        data_dir: Path,
        style_planner: Callable[[list[dict[str, Any]]], Any] | None = None,
    ) -> None:
        self.plugin_config = plugin_config
        self.logger = logger
        self.get_http_client = get_http_client
        self.cache_dir = data_dir / "tts_cache"
        self.style_planner = style_planner

    def is_enabled(self) -> bool:
        return bool(getattr(self.plugin_config, "personification_tts_enabled", False))

    def is_configured(self) -> bool:
        return bool(str(getattr(self.plugin_config, "personification_tts_api_key", "") or "").strip())

    def is_available(self) -> bool:
        return self.is_enabled() and self.is_configured()

    def is_style_planner_enabled(self) -> bool:
        return bool(
            getattr(self.plugin_config, "personification_tts_style_planner_enabled", False)
        ) and self.style_planner is not None

    def is_group_auto_enabled(self, group_config: dict[str, Any] | None = None) -> bool:
        config = group_config or {}
        return bool(
            config.get(
                "tts_enabled",
                getattr(self.plugin_config, "personification_tts_group_default_enabled", True),
            )
        )

    def should_auto_tts(
        self,
        *,
        is_private: bool,
        group_config: dict[str, Any] | None,
        text: str,
        has_rich_content: bool = False,
    ) -> bool:
        if not self.is_available():
            return False
        if not getattr(self.plugin_config, "personification_tts_auto_enabled", True):
            return False
        if has_rich_content:
            return False
        if not str(text or "").strip():
            return False
        if is_private and getattr(self.plugin_config, "personification_tts_private_force_auto", False):
            return True
        if not is_private and not self.is_group_auto_enabled(group_config):
            return False
        probability = float(getattr(self.plugin_config, "personification_tts_auto_probability", 0.2) or 0.0)
        if probability <= 0:
            return False
        return random.random() < probability

    def split_text(self, text: str) -> list[str]:
        raw = str(text or "").strip()
        if not raw:
            return []

        max_chars = int(getattr(self.plugin_config, "personification_tts_max_chars_per_segment", 120) or 120)
        if max_chars <= 0 or len(raw) <= max_chars:
            return [raw]

        chunks = re.split(r"([。！？!?；;，,\n])", raw)
        segments: list[str] = []
        buffer = ""
        for chunk in chunks:
            if not chunk:
                continue
            if len(buffer) + len(chunk) <= max_chars:
                buffer += chunk
                continue
            if buffer.strip():
                segments.append(buffer.strip())
            buffer = chunk
            while len(buffer) > max_chars:
                cut = self._safe_split_index(buffer, max_chars)
                segments.append(buffer[:cut].strip())
                buffer = buffer[cut:]
        if buffer.strip():
            segments.append(buffer.strip())
        return self._merge_punctuation_only_segments(segments)

    def infer_style_decision(
        self,
        text: str,
        *,
        voice_hint: str | None = None,
        style_hint: str | None = None,
        user_hint: str | None = None,
        is_private: bool = False,
        group_style: str | None = None,
        persona_tts: dict[str, str] | None = None,
    ) -> TtsStyleDecision:
        persona_config = persona_tts or {}
        explicit_style, clean_text = self._extract_explicit_style(text)
        persona_voice = str(persona_config.get("voice", "") or "").strip() or None
        persona_style = str(persona_config.get("style", "") or "").strip() or None
        final_style = (
            style_hint
            or explicit_style
            or persona_style
            or self._guess_style(clean_text, user_hint=user_hint, is_private=is_private, group_style=group_style)
        ).strip() or None
        final_voice = (voice_hint or persona_voice or self._guess_voice(clean_text)).strip()
        return TtsStyleDecision(
            text=clean_text.strip(),
            voice=final_voice,
            style=final_style,
        )

    async def synthesize(
        self,
        text: str,
        *,
        voice_hint: str | None = None,
        style_hint: str | None = None,
        user_hint: str | None = None,
        is_private: bool = False,
        group_style: str | None = None,
        persona_tts: dict[str, str] | None = None,
    ) -> list[Path]:
        if not self.is_available():
            raise RuntimeError("TTS 未启用或未配置 personification_tts_api_key")

        persona_config = persona_tts or {}
        planned = await self._plan_style(
            text=text,
            voice_hint=voice_hint,
            style_hint=style_hint,
            user_hint=user_hint,
            is_private=is_private,
            group_style=group_style,
            persona_tts=persona_config,
        )
        planned_text = str(planned.get("text", "") or "").strip() or text
        final_user_hint = (
            user_hint
            or str(planned.get("user_hint", "") or "").strip()
            or str(persona_config.get("user_hint", "") or "").strip()
            or None
        )
        persona_voice = str(persona_config.get("voice", "") or "").strip() or None
        persona_style = str(persona_config.get("style", "") or "").strip() or None
        decision = self.infer_style_decision(
            planned_text,
            voice_hint=voice_hint or persona_voice or str(planned.get("voice", "") or "").strip() or None,
            style_hint=style_hint or persona_style or str(planned.get("style", "") or "").strip() or None,
            user_hint=final_user_hint,
            is_private=is_private,
            group_style=group_style,
            persona_tts=persona_config,
        )
        if not decision.text:
            return []

        self._cleanup_old_files()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        output_files: list[Path] = []
        for segment in self.split_text(decision.text):
            audio_path = await self._synthesize_segment(
                segment,
                voice=decision.voice,
                style=decision.style,
                user_hint=final_user_hint,
            )
            output_files.append(audio_path)
        return output_files

    async def _plan_style(
        self,
        *,
        text: str,
        voice_hint: str | None = None,
        style_hint: str | None = None,
        user_hint: str | None = None,
        is_private: bool = False,
        group_style: str | None = None,
        persona_tts: dict[str, str] | None = None,
    ) -> dict[str, str]:
        if not self.is_style_planner_enabled():
            return {}

        explicit_style, clean_text = self._extract_explicit_style(text)
        if voice_hint or style_hint or explicit_style:
            return {}

        persona_config = persona_tts or {}
        scene = "私聊" if is_private else "群聊"
        system_prompt = (
            "你是语音合成风格规划器。"
            "你需要根据角色人设、文本内容、场景，给 TTS 选择最合适的音色、风格和朗读提示。"
            "只输出 JSON，不要解释，不要 markdown。"
            '格式固定为 {"voice":"mimo_default|default_zh|default_en","style":"风格标签","user_hint":"一句对朗读方式的中文指令","text":"可选的带细粒度音频标签文本"}。'
            "voice 只能返回 mimo_default、default_zh、default_en 之一。"
            "style 可以是多个中文风格词，用空格分隔。"
            "style 可直接使用 MiMo 支持的整体风格，例如 变快、变慢、开心、生气、悄悄话、东北话、粤语、唱歌 等。"
            "user_hint 应该强调语气、语速、情绪、停顿和角色感，简短自然。"
            "text 字段允许你在不改变原意的前提下，加入少量细粒度音频标签，例如（小声）、（语速加快）、（停顿）、（轻笑）。"
            "默认偏好：整体语速略快一点，但仍然自然，不要过快。"
        )
        user_payload = {
            "scene": scene,
            "text": clean_text.strip(),
            "persona_voice": str(persona_config.get("voice", "") or "").strip(),
            "persona_style": str(persona_config.get("style", "") or "").strip(),
            "persona_user_hint": str(persona_config.get("user_hint", "") or "").strip(),
            "group_style": str(group_style or "").strip(),
            "existing_user_hint": str(user_hint or "").strip(),
        }
        try:
            response = await self.style_planner(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ]
            )
        except Exception as e:
            self.logger.debug(f"[tts] 风格规划调用失败，回退规则推断: {e}")
            return {}

        parsed = self._extract_json_object(str(response or ""))
        result: dict[str, str] = {}
        if not isinstance(parsed, dict):
            return result

        voice = str(parsed.get("voice", "") or "").strip()
        if voice in {"mimo_default", "default_zh", "default_en"}:
            result["voice"] = voice
        style = str(parsed.get("style", "") or "").strip()
        if style:
            result["style"] = style
        planned_hint = str(parsed.get("user_hint", "") or "").strip()
        if planned_hint:
            result["user_hint"] = planned_hint
        planned_text = str(parsed.get("text", "") or "").strip()
        if planned_text:
            result["text"] = planned_text
        return result

    async def send_tts(
        self,
        *,
        bot: Any,
        event: Any,
        message_segment_cls: Any,
        text: str,
        voice_hint: str | None = None,
        style_hint: str | None = None,
        user_hint: str | None = None,
        is_private: bool = False,
        group_style: str | None = None,
        persona_tts: dict[str, str] | None = None,
        pause_range: tuple[float, float] = (0.8, 1.4),
    ) -> bool:
        audio_files = await self.synthesize(
            text,
            voice_hint=voice_hint,
            style_hint=style_hint,
            user_hint=user_hint,
            is_private=is_private,
            group_style=group_style,
            persona_tts=persona_tts,
        )
        if not audio_files:
            return False

        for index, audio_file in enumerate(audio_files):
            await bot.send(event, message_segment_cls.record(audio_file.absolute().as_uri()))
            if index < len(audio_files) - 1:
                await asyncio.sleep(random.uniform(*pause_range))
        return True

    async def _synthesize_segment(
        self,
        text: str,
        *,
        voice: str,
        style: str | None,
        user_hint: str | None = None,
    ) -> Path:
        payload = {
            "model": str(getattr(self.plugin_config, "personification_tts_model", "mimo-v2-tts") or "mimo-v2-tts"),
            "messages": self._build_messages(text, style=style, user_hint=user_hint),
            "audio": {
                "format": str(getattr(self.plugin_config, "personification_tts_default_format", "wav") or "wav"),
                "voice": voice,
            },
        }
        api_url = str(getattr(self.plugin_config, "personification_tts_api_url", "https://api.xiaomimimo.com/v1") or "https://api.xiaomimimo.com/v1").rstrip("/")
        api_key = str(getattr(self.plugin_config, "personification_tts_api_key", "") or "").strip()
        client = self.get_http_client()

        try:
            response = await client.post(
                f"{api_url}/chat/completions",
                headers={
                    "api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=float(getattr(self.plugin_config, "personification_tts_timeout", 60) or 60),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            self.logger.warning(f"[tts] MiMo 请求失败: {e}")
            raise

        try:
            audio_data = data["choices"][0]["message"]["audio"]["data"]
            audio_bytes = base64.b64decode(audio_data)
        except Exception as e:
            raise RuntimeError(f"MiMo 返回音频数据解析失败: {e}") from e

        file_ext = str(getattr(self.plugin_config, "personification_tts_default_format", "wav") or "wav").lower()
        target = self.cache_dir / f"tts_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.{file_ext}"
        target.write_bytes(audio_bytes)
        return target

    def _build_messages(self, text: str, *, style: str | None, user_hint: str | None) -> list[dict[str, str]]:
        content = text.strip()
        style_value = str(style or "").strip()
        if style_value:
            normalized_style = style_value
            if "变快" not in normalized_style and "变慢" not in normalized_style:
                normalized_style = f"{normalized_style} 变快".strip()
            if not STYLE_TAG_RE.match(content):
                content = f"<style>{normalized_style}</style>{content}"
        elif not STYLE_TAG_RE.match(content):
            content = f"<style>变快</style>{content}"

        messages: list[dict[str, str]] = []
        hint = str(user_hint or "").strip()
        if hint:
            messages.append({"role": "user", "content": hint})
        else:
            messages.append({"role": "user", "content": "请自然朗读，整体语速略快一点，但保持清晰自然。"})
        messages.append({"role": "assistant", "content": content})
        return messages

    def _extract_explicit_style(self, text: str) -> tuple[str | None, str]:
        raw = str(text or "")
        match = STYLE_TAG_RE.match(raw)
        if not match:
            return None, raw
        return match.group(1).strip(), raw[match.end():]

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _guess_voice(self, text: str) -> str:
        preferred = str(getattr(self.plugin_config, "personification_tts_default_voice", "mimo_default") or "mimo_default").strip()
        latin_count = len(LATIN_RE.findall(text))
        cjk_count = len(CJK_RE.findall(text))
        if latin_count >= 8 and latin_count > cjk_count * 1.5:
            return "default_en"
        return preferred or "mimo_default"

    def _guess_style(
        self,
        text: str,
        *,
        user_hint: str | None = None,
        is_private: bool = False,
        group_style: str | None = None,
    ) -> str | None:
        _ = text, user_hint, is_private, group_style
        if is_private:
            return "自然"
        return "自然"

    def _safe_split_index(self, text: str, max_chars: int) -> int:
        punctuations = "。！？!?；;，,\n "
        for index in range(max_chars, max(1, max_chars // 2), -1):
            if text[index - 1] in punctuations:
                return index
        return max_chars

    def _cleanup_old_files(self, *, max_age_seconds: int = 86400) -> None:
        if not self.cache_dir.exists():
            return
        now_ts = time.time()
        for file_path in self.cache_dir.iterdir():
            try:
                if not file_path.is_file():
                    continue
                if now_ts - file_path.stat().st_mtime > max_age_seconds:
                    file_path.unlink(missing_ok=True)
            except Exception:
                continue

    def _merge_punctuation_only_segments(self, segments: Sequence[str]) -> list[str]:
        merged: list[str] = []
        for segment in segments:
            value = str(segment or "").strip()
            if not value:
                continue
            if all(ch in "。！？!?；;，,\n " for ch in value) and merged:
                merged[-1] = f"{merged[-1]}{value}"
                continue
            merged.append(value)
        return merged

__all__ = ["TtsService", "TtsStyleDecision", "extract_persona_tts_config"]
