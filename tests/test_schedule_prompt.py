from __future__ import annotations

from ._loader import load_personification_module


schedule = load_personification_module("plugin.personification.schedule")


def test_schedule_prompt_defaults_to_empty_without_hardcoded_status() -> None:
    prompt = schedule.get_schedule_prompt_injection()
    assert "未配置具体作息表" in prompt
    assert "不要自动推断上课" in prompt
    assert "通常在上学、上班" not in prompt


def test_schedule_prompt_can_use_custom_editable_schedule() -> None:
    prompt = schedule.get_schedule_prompt_injection("- 晚上更活跃\n- 作息仅作背景，不限制是否回复")
    assert "当前时间与自定义作息参考" in prompt
    assert "晚上更活跃" in prompt
