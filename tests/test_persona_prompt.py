from __future__ import annotations

from ._loader import load_personification_module


persona_service = load_personification_module("plugin.personification.core.persona_service")


def test_new_prompt_contains_expanded_fields() -> None:
    prompt = persona_service.build_persona_prompt(
        ["你好", "今天累死了", "我喜欢二次元"], previous=None
    )
    # 旧字段保留
    for token in ("【职业推测】", "【年龄推测】", "【性别推测】", "【人物描述】"):
        assert token in prompt, f"缺少 {token}"
    # 新增字段
    for token in (
        "【作息特征】",
        "【兴趣领域】",
        "【沟通风格】",
        "【情绪基线】",
        "【社交模式】",
        "【知识结构】",
        "【互动建议】",
    ):
        assert token in prompt, f"新字段未注入：{token}"


def test_new_prompt_includes_messages() -> None:
    msgs = ["第一句话", "第二句话"]
    prompt = persona_service.build_persona_prompt(msgs, previous=None)
    for m in msgs:
        assert m in prompt
    # 不应出现旧画像段（新画像不传入 previous）
    assert "旧画像" not in prompt


def test_update_prompt_preserves_previous_persona() -> None:
    previous = "【职业推测】：大学生\n【年龄推测】：20岁\n【人物描述】：理工科男生，喜欢动漫"
    prompt = persona_service.build_persona_prompt(
        ["最近在准备考研", "天天熬夜刷题"],
        previous=previous,
    )
    assert "旧画像" in prompt
    # 旧画像内容原文一定要在新 prompt 里
    assert "大学生" in prompt
    assert "理工科男生" in prompt
    # 关键约束语句必须在 prompt 中明确告诉模型要保留
    assert "未被新记录推翻" in prompt
    assert "保留" in prompt
    # 字段 guide 必须出现
    assert "【兴趣领域】" in prompt
    assert "【互动建议】" in prompt


def test_update_prompt_explicitly_forbids_fabrication() -> None:
    prompt = persona_service.build_persona_prompt(["短消息"], previous="【职业推测】：未知")
    # 旧画像缺字段时不应造假
    assert "信息不足" in prompt
    assert "不要" in prompt and "编造" in prompt


def test_structured_parser_keeps_portrait_and_interaction_advice() -> None:
    parsed = persona_service.parse_persona_structured(
        "【人物描述】：经常半夜冒泡的 ACG 玩家，说话短促但会照顾熟人。\n"
        "【互动建议】：用熟人式短句接话，少讲大道理。"
    )
    assert parsed["portrait"].startswith("经常半夜冒泡")
    assert parsed["interaction_advice"] == "用熟人式短句接话，少讲大道理。"


def test_field_guide_singleton_constant() -> None:
    guide = persona_service._PERSONA_FIELD_GUIDE
    # 字段总数：原 4 个 + 新增 7 个 = 11 段
    field_count = guide.count("【")
    assert field_count >= 10, f"字段引导应包含至少 10 个【...】，实际 {field_count}"


def test_persona_prompt_uses_avatar_only_as_weak_evidence() -> None:
    prompt = persona_service.build_persona_prompt(
        ["最近在补番"],
        previous=None,
        avatar_context="类型：acg_character；中性摘要：蓝发动画角色；ACG 候选：角色甲",
    )
    assert "头像长期弱证据" in prompt
    assert "ACG 候选：角色甲" in prompt
    assert "只能辅助理解头像审美或 ACG 偏好候选" in prompt
    assert "不得替代聊天证据推断真实身份、性别、年龄" in prompt
