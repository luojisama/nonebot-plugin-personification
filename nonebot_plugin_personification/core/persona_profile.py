from __future__ import annotations

from typing import Any

import yaml


DEFAULT_PERSONA_PROFILE = {
    "identity_rules": [
        "保持角色一致性，不提及自己是 AI、模型或程序。",
        "优先使用自然口语表达，避免客服腔和公文腔。",
    ],
    "style_rules": [
        "默认简短回复，常规场景控制在 1-3 句。",
        "内容优先回应对方情绪与意图，再补充事实。",
    ],
    "boundary_rules": [
        "群聊里先像群友顺手接一句，只有明显会打断别人时再收住。",
        "不确定时优先短句接住；只有明显无关、重复或高歧义时才输出 [SILENCE]。",
    ],
}


def load_persona_profile(base_prompt: Any) -> dict[str, Any]:
    if isinstance(base_prompt, dict):
        profile = base_prompt.get("persona_profile")
        if isinstance(profile, dict):
            return profile
        return DEFAULT_PERSONA_PROFILE
    if isinstance(base_prompt, str):
        text = base_prompt.strip()
        if text.startswith("---"):
            try:
                parsed = yaml.safe_load(text)
                if isinstance(parsed, dict):
                    profile = parsed.get("persona_profile")
                    if isinstance(profile, dict):
                        return profile
            except Exception:
                return DEFAULT_PERSONA_PROFILE
    return DEFAULT_PERSONA_PROFILE


def render_persona_snapshot(profile: dict[str, Any]) -> str:
    identity = profile.get("identity_rules", []) if isinstance(profile, dict) else []
    style = profile.get("style_rules", []) if isinstance(profile, dict) else []
    boundary = profile.get("boundary_rules", []) if isinstance(profile, dict) else []
    identity_lines = "\n".join(f"- {str(item)}" for item in identity[:3])
    style_lines = "\n".join(f"- {str(item)}" for item in style[:3])
    boundary_lines = "\n".join(f"- {str(item)}" for item in boundary[:3])
    return (
        "## 人格快照\n"
        f"[身份一致性]\n{identity_lines or '- 保持自然互动'}\n"
        f"[语气风格]\n{style_lines or '- 口语化简短表达'}\n"
        f"[边界策略]\n{boundary_lines or '- 默认友好且克制'}"
    )
