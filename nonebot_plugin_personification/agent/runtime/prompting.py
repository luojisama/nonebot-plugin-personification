from __future__ import annotations

from typing import Any

from ...core.meme_reply_policy import format_meme_turn_prompt
from ...core.context_policy import (
    PROMPT_INJECTION_GUARD_MARKER,
    build_prompt_injection_guard,
)
from ...core.reply_style_policy import (
    build_directed_exchange_policy_prompt,
    build_domain_evidence_policy_prompt,
    build_empty_evidence_output_policy_prompt,
    build_emotional_support_policy_prompt,
    build_media_understanding_output_policy_prompt,
    build_speech_act_policy_prompt,
)
from ...core.turn_media import render_turn_media_grounding
from .tool_selection import _semantic_tool_guidance


def append_agent_system_prompts(
    *,
    messages: list[dict],
    runtime_chat_intent: str,
    plugin_query_intent: str,
    intent_decision: Any,
    rewritten_query: Any,
    turn_plan: Any,
    user_images: list[str],
    direct_image_input: bool,
    is_group: bool | None = None,
    is_direct_mention: bool = False,
    reply_required: bool = False,
    surface: str = "",
    turn_media_context: list[Any] | None = None,
) -> None:
    if not any(
        isinstance(message, dict)
        and message.get("role") == "system"
        and PROMPT_INJECTION_GUARD_MARKER in str(message.get("content", "") or "")
        for message in messages
    ):
        messages.append({"role": "system", "content": build_prompt_injection_guard()})
    group_context = bool(is_group) if is_group is not None else any(
        isinstance(message, dict)
        and message.get("role") == "system"
        and any(marker in str(message.get("content", "") or "") for marker in ("群聊", "群里", "群友", "群成员"))
        for message in list(messages or [])
    )
    messages.append(
        {
            "role": "system",
            "content": _semantic_tool_guidance(),
        }
    )
    if reply_required:
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前是结构上必须回应的强交互轮次（私聊、明确 @/回复 bot 或其它直接指向）。"
                    "除非发送型工具已经成功排队、安全边界要求拒绝，或空证据且无法形成具体补充请求，"
                    "否则禁止输出 [NO_REPLY] 或 [SILENCE]。"
                    "如果只缺一个对方能提供的必要条件，就具体索取这一项；"
                    "不要用无法确认、没有理解或查证无结果的状态播报顶替回答。"
                ),
            }
        )
    if surface:
        messages.append(
            {
                "role": "system",
                "content": (
                    f"当前任务属于非聊天生成面：{surface}。"
                    "严格遵守最新用户消息要求的输出格式；不要套用群聊接话、追问、@、引用、"
                    "[NO_REPLY] 或聊天短句质量规则。需要事实查证时可以使用工具，"
                    "但不得把工具过程写进最终结果。"
                ),
            }
        )
        if rewritten_query.primary_query:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"当前任务主查询：{rewritten_query.primary_query}\n"
                        + (
                            f"候选查询：{'；'.join(rewritten_query.query_candidates[:4])}\n"
                            if rewritten_query.query_candidates else ""
                        )
                        + "仅在任务确实需要外部事实时使用这些查询；创作与审阅任务不要为了调用工具而调用。"
                    ),
                }
            )
        return
    messages.append(
        {
            "role": "system",
            "content": (
                "最终对用户的回复必须自然、像群聊里的活人接话。"
                "默认以讨论和闲聊为主基调：给一点具体态度、接住一个点，或顺着话题往前聊半步；"
                "不要只附和、感叹，也不要把群友刚说过的内容换一种说法转述。"
                "如果你只想说“先看看情况/等会再说/先围观一下”这类观察或等待宣告，直接输出 [NO_REPLY]。"
                "不要暴露工具、检索、看图、回忆、审查清单或 Step 1/Step 2 这类中间步骤。"
                "遇到不确定或有歧义时，如果有可用查证工具必须先查；没有可用证据时不要硬猜，也不要把失败状态包装成回复。"
                "遇到不认识的专有名词、外号、梗、游戏/动漫/卡牌术语或圈内说法，不要直接问群友那是什么，先用可用工具查证。"
                "如果用户说“这个动画/这段动画/这个角色/这张图”是在接最近上下文里的 ACG 角色、抽卡、卡面、图片或视频，"
                "先查清角色、作品、剧情/动画出处，再用一句具体评价参与讨论；不要泛泛附和“确实挺强”。"
                "群聊里的可见回复不要用追问、澄清问句或征询式结尾把信息缺口丢回给群友；"
                "轻松调侃时允许一句不索要信息的反击式反问。信息不足但能给具体态度时再短答，否则按空证据纪律收口。"
                "涉及本地天气、出行、城市或附近状态时，如果用户没明说地点，先看已注入的用户档案；仍不确定可调用记忆工具确认，不能猜城市。"
                "最终只输出纯文本，不要 markdown、标题、项目符号列表、编号列表、URL 列表，也不要说“我需要确认一下”“根据搜索结果”。"
            ),
        }
    )
    if turn_plan is not None:
        messages.append(
            {
                "role": "system",
                "content": build_speech_act_policy_prompt(
                    speech_act=str(getattr(turn_plan, "speech_act", "") or ""),
                    output_mode=str(getattr(turn_plan, "output_mode", "") or ""),
                    session_goal=str(getattr(turn_plan, "session_goal", "") or ""),
                    is_group=group_context,
                ),
            }
        )
        domain_prompt = build_domain_evidence_policy_prompt(
            domain_focus=str(getattr(turn_plan, "domain_focus", "general") or "general"),
            evidence_policy=str(getattr(turn_plan, "evidence_policy", "none") or "none"),
        )
        if domain_prompt:
            messages.append({"role": "system", "content": domain_prompt})
        support_prompt = build_emotional_support_policy_prompt(getattr(turn_plan, "emotional_support", None))
        if support_prompt:
            messages.append({"role": "system", "content": support_prompt})
        meme_prompt = format_meme_turn_prompt(getattr(turn_plan, "meme_turn_context", None))
        if meme_prompt:
            messages.append({"role": "system", "content": meme_prompt})
    directed_exchange_prompt = build_directed_exchange_policy_prompt(
        is_direct_mention=is_direct_mention,
        is_group=group_context,
        speech_act=str(getattr(turn_plan, "speech_act", "") or ""),
        output_mode=str(getattr(turn_plan, "output_mode", "") or ""),
    )
    if directed_exchange_prompt:
        messages.append({"role": "system", "content": directed_exchange_prompt})
    messages.append(
        {
            "role": "system",
            "content": (
                "群聊里通常多个话题并行：A 群友讨论地震、B 群友讨论自己的近况、C 群友在闲扯，"
                "时间相近不代表语义相关。\n"
                "硬性规则：\n"
                "1. 你回复的是上下文中标记为「当前消息」的那一条；其它发言只是背景，不要把它们的内容拿来回答当前问题。\n"
                "2. 不要把不同人说的关键词（地名、人名、状态）跨话题拼接。"
                "比如 A 在说地震位置是「广西柳州」，同时 B 在说「我家在浙江」，"
                "当 C 问「这次地震严重吗」时，你只能基于 A 的位置信息回答，绝不能说「浙江有震感」。\n"
                "3. 引用某人状态前先问自己：这个状态是不是当前消息的语境？如果不是，就不要写进去。\n"
                "4. 拿不准时不要把无关上下文糊上去；没有具体内容可说就按空证据纪律收口。"
            ),
        }
    )
    if runtime_chat_intent == "banter":
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前更像接梗、吐槽、复读或顺嘴接话场景，优先短句自然接话。"
                    "但如果群友分享了你看不懂的内容、梗、专有名词、节目名或外号（比如配图配文、视频/链接分享），"
                    "且可用工具里有 web_search、search_web、wiki_lookup 或 resolve_acg_entity，必须先快速查清楚那是什么，再用自己的口吻接住——"
                    "尤其是“这个动画牛逼/这个角色好帅/这图太强”这类指代最近 ACG 上下文的评价，要查角色/作品/剧情锚点，"
                    "不要直接在群里问『这是什么梗/哪个游戏/什么意思』，也不要凭记忆猜。"
                    "查证只为听懂梗，别变成解释、定义、考据或百科腔，查完一句话接住即可。"
                ),
            }
        )
    elif runtime_chat_intent == "image_generation":
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前用户是在要求生成图片。必须调用 generate_image 工具，"
                    "不要只回复提示词、描述或制作步骤。"
                ),
            }
        )
    elif runtime_chat_intent == "expression":
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前用户是在要求你发送 QQ 表情，或这轮最适合只用 QQ 表情回应。"
                    "必须从可用的 send_qq_face、send_qq_favorite_expression、send_qq_recommended_expression 中选择合适工具；"
                    "工具成功后最终只输出 [SILENCE]，不要再说“已发送”、不要解释工具。"
                    "如果用户明确说小黄脸/系统表情，优先 send_qq_face；"
                    "明确说收藏表情时用 send_qq_favorite_expression；"
                    "需要按情绪或场景匹配图片表情时用 send_qq_recommended_expression。"
                ),
            }
        )
    elif runtime_chat_intent == "plugin_question":
        if plugin_query_intent == "runtime_capability":
            plugin_hint = (
                "当前是在问你是否具备读取或理解当前发送者头像可观察画面事实的运行时能力。"
                "必须调用本轮可用的第一方只读 runtime capability 工具核实，不要调用插件清单、插件知识或源码工具猜测。"
                "inspect_current_user_avatar 只提供受约束的头像画面事实，不能声称看到了 raw 头像；"
                "available=false 时如实说明当前没有可用的头像画面事实。"
            )
        else:
            plugin_hint = (
                "当前更像在问插件静态能力、命令、实现或配置。"
                "如果需要工具，优先使用本地插件知识和源码工具，不要先联网。"
                "优先考虑：search_plugin_source、search_plugin_knowledge、list_plugin_features、get_feature_detail、list_plugins。"
            )
        if plugin_query_intent == "latest":
            plugin_hint += (
                "如果对方明确问官网、仓库、最新文档或版本，再考虑 web_search、search_official_site、search_github_repos。"
            )
        if plugin_query_intent != "runtime_capability":
            plugin_hint += (
                "如果对方不是在问插件原理，而是想直接用某个插件功能（查天气、签到、点歌、查询等），"
                "先用 search_plugin_knowledge / list_plugin_features 定位插件和它的命令触发方式，"
                "确认后用 invoke_plugin 传入完整命令文本（如 /天气 北京）代为执行，再用你自己的语气转述结果，"
                "不要让用户自己去发命令。"
            )
        messages.append(
            {
                "role": "system",
                "content": plugin_hint,
            }
        )
    messages.append(
        {
            "role": "system",
            "content": build_media_understanding_output_policy_prompt(),
        }
    )
    messages.append(
        {
            "role": "system",
            "content": build_empty_evidence_output_policy_prompt(),
        }
    )
    media_grounding = render_turn_media_grounding(turn_media_context)
    if media_grounding:
        messages.append({"role": "system", "content": media_grounding})
    if getattr(intent_decision, "ambiguity_level", "") == "high":
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前这句里有高歧义名词/对象，容易误解。"
                    "如果有可用查证工具，先查证再说；上下文和工具证据仍不足时，"
                    "非强交互直接 [NO_REPLY]，强交互只索取一个明确且对方能提供的必要条件。"
                ),
            }
        )
    if rewritten_query.primary_query:
        messages.append(
            {
                "role": "system",
                "content": (
                    f"当前检索意图主查询：{rewritten_query.primary_query}\n"
                    + (
                        f"候选查询：{'；'.join(rewritten_query.query_candidates[:4])}\n"
                        if rewritten_query.query_candidates else ""
                    )
                    + (
                        f"上下文线索：{'；'.join(rewritten_query.context_clues[:4])}\n"
                        if rewritten_query.context_clues else ""
                    )
                    + (
                        f"检索计划：{'；'.join(rewritten_query.search_plan[:3])}\n"
                        if rewritten_query.search_plan else ""
                    )
                    + "如果需要调用 web_search/wiki_lookup/resolve_acg_entity/vision_analyze，优先使用这些检索词，"
                    + "不要直接拿用户最后一句口语补充当 query。"
                    + "工具优先级由你结合这份计划和当前证据自主判断。"
                ),
            }
        )
    if user_images:
        if direct_image_input:
            image_prompt = (
                "如果当前消息包含图片输入，请直接结合图片和文字理解用户意图。"
                "可以内部判断它是表情包、截图还是真实照片，但不要把分类、判断过程或视觉摘要说出来。"
                "如果你只看到图片占位或视觉摘要，不要声称自己直接看到了原图。"
                "必要时可以调用视觉分析工具进一步分析图片。"
                "除非用户明确要求识别、翻译或说明图片，不要在最终回复里描述图片/GIF/表情包内容；"
                "如果没有可见摘要、文字 cue 或明确提问，不要泛泛评价图片/表情，也不要追问“看到什么了”；"
                "群聊没人 cue 你时可以输出 [NO_REPLY]。"
            )
        else:
            image_prompt = (
                "当前轮包含图片相关上下文，但你不一定直接收到了原图。"
                "可以内部判断它是表情包、截图还是真实照片，但不要把分类、判断过程或视觉摘要说出来。"
                "如果你看到的是图片占位或视觉摘要，请把它当作摘要，不要声称自己直接看到了原图。"
                "必要时可以调用视觉分析工具进一步分析图片。"
                "除非用户明确要求识别、翻译或说明图片，不要在最终回复里描述图片/GIF/表情包内容；"
                "如果没有可见摘要、文字 cue 或明确提问，不要泛泛评价图片/表情，也不要追问“看到什么了”；"
                "群聊没人 cue 你时可以输出 [NO_REPLY]。"
            )
        messages.append(
            {
                "role": "system",
                "content": image_prompt,
            }
        )


__all__ = ["append_agent_system_prompts"]
