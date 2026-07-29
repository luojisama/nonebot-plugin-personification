from __future__ import annotations

from ._loader import load_personification_module

reply_style_policy = load_personification_module("plugin.personification.core.reply_style_policy")


def test_reply_style_policy_keeps_visual_context_internal() -> None:
    prompt = reply_style_policy.build_reply_style_policy_prompt(
        has_visual_context=True,
        photo_like=False,
    )

    assert "内部上下文" in prompt
    assert "媒体占位纪律" in prompt
    assert "不要假装知道画面内容" in prompt
    assert "最近上下文已经说明原因" in prompt
    assert "不要主动讲图里是什么" in prompt
    assert "讨论与闲聊基调" in prompt
    assert "不要只附和、感叹" in prompt
    assert "把群友刚说的话换一种说法转述" in prompt
    assert "不要堆砌互联网热词" in prompt
    assert "等下/等一下/你这也/这图也" in prompt
    assert "固定起手与口癖复用纪律" in prompt
    assert "这也太" in prompt
    assert "最近 bot 发言已经用过相似开头" in prompt
    assert "观望与旁白式发言纪律" in prompt
    assert "不要说明自己接下来要怎么观察" in prompt
    assert "。。。/……/..." in prompt
    assert "仅供理解" in prompt
    assert "不要把判断过程说给用户" in prompt
    assert "不要讲解、复述、总结或分析画面内容" in prompt
    assert "空证据可见输出纪律" in prompt
    assert "内部状态换成人设口吻" in prompt
    assert "不得猜测出处、群内约定" in prompt


def test_context_continuity_policy_covers_media_only_and_direct_cues() -> None:
    prompt = reply_style_policy.build_context_continuity_policy_prompt()

    assert "低信息跟帖或媒体占位" in prompt
    assert "优先保持沉默" in prompt
    assert "相邻图片、表情或截图不能覆盖直接 cue 的文字问题" in prompt
    assert "优先回应这个问题本身" in prompt
    assert "安全底线不是危险词触发器" in prompt
    assert "没有 @/引用/直呼你的群友玩笑" in prompt


def test_plugin_episode_policy_keeps_external_output_in_context() -> None:
    prompt = reply_style_policy.build_plugin_interaction_policy_prompt(
        is_direct_mention=True,
    )

    assert "不是你说过的话" in prompt
    assert "不要因为结果里出现专业名词" in prompt
    assert "不得声称插件结果是你抽到、查到或刚说的" in prompt
    assert "直呼本身不等于要求" in prompt


def test_observer_posture_policy_prefers_silence_over_status_announcement() -> None:
    prompt = reply_style_policy.build_observer_posture_policy_prompt()

    assert "潜水" in prompt
    assert "等会再说" in prompt
    assert "[NO_REPLY]" in prompt
    assert "具体反应" in prompt


def test_conversational_baseline_discourages_echo_and_empty_affirmation() -> None:
    prompt = reply_style_policy.build_conversational_baseline_policy_prompt()

    assert "参与讨论的群友" in prompt
    assert "新角度" in prompt
    assert "宁可少说或沉默" in prompt


def test_group_reply_style_discourages_visible_questions() -> None:
    prompt = reply_style_policy.build_reply_style_policy_prompt(is_group=True)

    assert "群聊不追问纪律" in prompt
    assert "不要用问句、反问句、澄清问句" in prompt
    assert "不要追着群友补材料" in prompt
    assert "不索要信息的反问/反击句" in prompt


def test_directed_exchange_policy_separates_answers_from_banter() -> None:
    prompt = reply_style_policy.build_directed_exchange_policy_prompt(
        is_direct_mention=True,
        is_group=True,
        speech_act="tease",
        output_mode="chat_short",
    )

    assert "不表示所有内容都要写成正式问答" in prompt
    assert "先给结论或选择" in prompt
    assert "否认、反击、自辩" in prompt
    assert "2-4 条短消息" in prompt
    assert "即时反应" in prompt
    assert "不要每次被 @ 都固定连发四条" in prompt


def test_speech_act_policy_guides_discussion_action() -> None:
    prompt = reply_style_policy.build_speech_act_policy_prompt(
        speech_act="participate",
        output_mode="chat_short",
        session_goal="接住当前话题",
    )

    assert "本轮说话动作" in prompt
    assert "speech_act=participate" in prompt
    assert "参与讨论" in prompt
    assert "不要只附和、感叹、复述" in prompt
    assert "output_mode=chat_short" in prompt
    assert "接住当前话题" in prompt


def test_speech_act_policy_converts_group_clarify_to_participate() -> None:
    prompt = reply_style_policy.build_speech_act_policy_prompt(
        speech_act="clarify",
        is_group=True,
    )

    assert "speech_act=participate" in prompt
    assert "不要把 ask_followup/clarify 写成可见问句" in prompt


def test_speech_act_policy_for_action_prefers_silence_after_send() -> None:
    prompt = reply_style_policy.build_speech_act_policy_prompt(speech_act="execute_action")

    assert "发送图片/表情" in prompt
    assert "[SILENCE]" in prompt
    assert "不要再补一段解释" in prompt


def test_direct_visual_guard_does_not_ask_to_describe_image() -> None:
    prompt = reply_style_policy.build_direct_visual_identity_guard()

    assert "不要主动讲解、复述或分析画面" in prompt
    assert "不要把分类或判断过程说出来" in prompt
    assert "描述或评论图片内容" not in prompt


def test_media_understanding_policy_allows_internal_distinction_only() -> None:
    prompt = reply_style_policy.build_media_understanding_output_policy_prompt()

    assert "内部语境证据" in prompt
    assert "表情包、梗图、截图还是真实照片" in prompt
    assert "不要把判断过程说给用户" in prompt
    assert "没有证据时按空证据纪律保持沉默或只索取一个必要条件" in prompt


def test_empty_evidence_policy_prefers_silence_or_one_concrete_condition() -> None:
    prompt = reply_style_policy.build_empty_evidence_output_policy_prompt()

    assert "不算一条回复" in prompt
    assert "没人明确需要你回应时直接 [NO_REPLY]" in prompt
    assert "索取一个具体条件" in prompt
    assert "无法形成具体补充请求时输出 [SILENCE]" in prompt
