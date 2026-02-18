from datetime import datetime, timedelta, timezone

# Beijing Timezone (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

# Japanese National Holidays (Fixed Date) - Simplified list
# Note: Happy Mondays and Equinoxes are variable, but for simplicity we can hardcode common ones or use a library.
# Given the constraint of not installing new libs if possible, I'll approximate or list fixed ones.
# 2026 is the year in the user's context (d:\bot\nonebot\shirotest context says 2026-02-11)
# But better to just support month-day matching for fixed holidays.
FIXED_HOLIDAYS = {
    (1, 1): "元日 (New Year's Day)",
    (2, 11): "建国記念の日 (National Foundation Day)",
    (2, 23): "天皇誕生日 (Emperor's Birthday)",
    (3, 3): "雛祭り (Hina Matsuri - Not a national holiday but cultural)",
    (4, 29): "昭和の日 (Showa Day)",
    (5, 3): "憲法記念日 (Constitution Memorial Day)",
    (5, 4): "みどりの日 (Greenery Day)",
    (5, 5): "こどもの日 (Children's Day)",
    (7, 7): "七夕 (Tanabata - Cultural)",
    (8, 11): "山の日 (Mountain Day)",
    (11, 3): "文化の日 (Culture Day)",
    (11, 23): "勤労感謝の日 (Labor Thanksgiving Day)",
    (12, 25): "クリスマス (Christmas - Cultural)",
}

# School Events (Month, StartDay, EndDay, EventName, Type)
# Type: 'exam', 'trip', 'festival', 'ceremony'
SCHOOL_EVENTS = [
    (4, 6, 8, "入学式/始业式 (Entrance/Opening Ceremony)", "ceremony"),
    (5, 20, 22, "一学期中间考试 (Midterm Exams)", "exam"),
    (6, 15, 17, "修学旅行 (School Trip)", "trip"),
    (7, 6, 9, "一学期期末考试 (Term-end Exams)", "exam"),
    (10, 9, 10, "体育祭 (Sports Festival)", "festival"),
    (10, 20, 22, "二学期中间考试 (Midterm Exams)", "exam"),
    (11, 2, 3, "文化祭 (Culture Festival)", "festival"),
    (12, 6, 9, "二学期期末考试 (Term-end Exams)", "exam"),
    (2, 24, 26, "学年末考试 (Year-end Exams)", "exam"),
    (3, 15, 15, "毕业典礼 (Graduation Ceremony)", "ceremony"),
]

def get_beijing_time() -> datetime:
    """Get current time in Beijing Timezone"""
    return datetime.now(BEIJING_TZ)

def get_activity_status() -> str:
    """
    Get the current activity status of a Japanese middle school student (Mahiro/Shiro)
    based on Beijing Time.
    """
    now = get_beijing_time()
    month, day = now.month, now.day
    hour, minute = now.hour, now.minute
    weekday = now.weekday() # 0=Mon, 6=Sun

    # 1. Check for Holidays (Fixed)
    if (month, day) in FIXED_HOLIDAYS:
        holiday_name = FIXED_HOLIDAYS[(month, day)]
        # Holidays behavior: Free time, maybe lazy
        return _get_holiday_routine(hour, holiday_name)

    # 2. Check for School Events (Priority over Vacation/Regular)
    for (e_month, e_start, e_end, e_name, e_type) in SCHOOL_EVENTS:
        if month == e_month and e_start <= day <= e_end:
            return _get_school_event_routine(hour, e_name, e_type)

    # 3. Check for Vacations (Approximate ranges)
    # Winter: Dec 25 - Jan 7
    if (month == 12 and day >= 25) or (month == 1 and day <= 7):
        return _get_holiday_routine(hour, "冬休み (Winter Vacation)")
    
    # Spring: Mar 25 - Apr 5
    if (month == 3 and day >= 25) or (month == 4 and day <= 5):
        return _get_holiday_routine(hour, "春休み (Spring Vacation)")
        
    # Summer: July 20 - Aug 31
    if (month == 7 and day >= 20) or (month == 8):
        return _get_holiday_routine(hour, "夏休み (Summer Vacation)")

    # 4. Regular Weekdays vs Weekends
    if weekday >= 5: # Saturday, Sunday
        return _get_weekend_routine(hour)
    else: # Monday - Friday
        return _get_school_day_routine(hour)

def _get_school_event_routine(hour: int, event_name: str, event_type: str) -> str:
    prefix = f"今天是 {event_name}。"
    
    if event_type == "trip":
        if 0 <= hour < 7:
            return f"{prefix} 在旅馆睡觉/醒来 (Hotel)."
        elif 7 <= hour < 22:
            return f"{prefix} 全天都在修学旅行活动中！观光、大巴移动或集体活动 (School Trip)."
        else:
            return f"{prefix} 在旅馆房间和朋友聊天/打闹 (Hotel Night)."
            
    elif event_type == "exam":
        if 8 <= hour < 12:
            return f"{prefix} 正在紧张考试中 (Taking Exams). 绝对不能回消息。"
        elif 12 <= hour < 13:
            return f"{prefix} 考试午休，正在对答案 (Exam Lunch)."
        elif 13 <= hour < 16:
            return f"{prefix} 下午继续复习或考试 (Exam/Study)."
        elif 16 <= hour < 23:
            return f"{prefix} 放学后正在拼命复习明天的科目 (Cramming)."
        else:
            return f"{prefix} 准备睡觉 (Sleeping)."
            
    elif event_type == "festival":
        if 8 <= hour < 16:
            return f"{prefix} 正在全神贯注参加活动/看店 (Festival Activity). 非常热闹，可能没空看手机。"
        elif 16 <= hour < 19:
            return f"{prefix} 活动后的后夜祭或收拾 (After Party/Cleanup)."
        else:
            return f"{prefix} 累了一天回家了 (Tired but happy)."
            
    elif event_type == "ceremony":
        if 8 <= hour < 12:
            return f"{prefix} 正在参加典礼 (Ceremony). 氛围庄重。"
        else:
            return f"{prefix} 典礼结束，半天放学或班会 (Half-day)."
            
    return _get_school_day_routine(hour) # Fallback

def _get_holiday_routine(hour: int, holiday_name: str) -> str:
    status = f"今天是 {holiday_name}。"
    if 0 <= hour < 8:
        return f"{status} 还在睡懒觉 (Sleeping). 除非被吵醒，否则应该在睡觉。"
    elif 8 <= hour < 10:
        return f"{status} 刚起床不久，正在享受假期的早晨 (Morning relax)."
    elif 10 <= hour < 12:
        return f"{status} 自由活动时间，可能在玩游戏或看番 (Gaming/Anime)."
    elif 12 <= hour < 13:
        return f"{status} 午饭时间 (Lunch)."
    elif 13 <= hour < 18:
        return f"{status} 下午自由活动，或者出门玩了 (Free time / Out)."
    elif 18 <= hour < 19:
        return f"{status} 晚饭时间 (Dinner)."
    elif 19 <= hour < 23:
        return f"{status} 晚上的休闲时间 (Relaxing)."
    else: # 23+
        return f"{status} 准备睡觉了 (Going to bed)."

def _get_weekend_routine(hour: int) -> str:
    if 0 <= hour < 8:
        return "周末还在睡懒觉 (Sleeping)."
    elif 8 <= hour < 12:
        return "周末上午，可能在赖床或者玩游戏 (Lazy Morning)."
    elif 12 <= hour < 13:
        return "周末午饭时间 (Lunch)."
    elif 13 <= hour < 18:
        return "周末下午，可能去社团活动或者宅在家里 (Club/Home)."
    elif 18 <= hour < 23:
        return "周末晚上，放松时间 (Relaxing)."
    else:
        return "该睡觉了 (Sleeping)."

def _get_school_day_routine(hour: int) -> str:
    if 6 <= hour < 7:
        return "起床，准备上学 (Waking up for school)."
    elif 7 <= hour < 8:
        return "上学路上 (Commuting)."
    elif 8 <= hour < 12:
        return "学校上课中 (In Class). 只有下课休息时间能简短回复，上课时不能玩手机。"
    elif 12 <= hour < 13:
        return "午休时间 (Lunch Break). 正在吃便当，可以聊天。"
    elif 13 <= hour < 15:
        return "下午上课中 (In Class). 不能玩手机。"
    elif 15 <= hour < 16:
        return "放学/班会 (Homeroom/School End)."
    elif 16 <= hour < 18:
        return "社团活动 (Club Activities). 可能会比较忙。"
    elif 18 <= hour < 19:
        return "放学回家/晚饭 (Commuting/Dinner)."
    elif 19 <= hour < 21:
        return "做作业/复习 (Homework)."
    elif 21 <= hour < 23:
        return "自由时间/洗澡 (Free Time/Bath)."
    elif 23 <= hour or hour < 6:
        return "深夜睡觉时间 (Sleeping). 除非被特别的事情吵醒。"
    else:
        return "日常活动中。"

def get_schedule_prompt_injection() -> str:
    """Get the full prompt text to inject"""
    now = get_beijing_time()
    status = get_activity_status()
    
    prompt = (
        f"## 当前作息状态 (基于日本中学生设定)\n"
        f"- 当前活动：{status}\n"
        f"- **强制扮演要求**：\n"
        f"  1. 如果当前是【上课中】，你回复应该非常简短，或者表现出偷偷玩手机的紧张感，或者直到【下课/午休】才回复。\n"
        f"  2. 如果当前是【睡觉时间】，你应该表现出被吵醒的困倦，或者干脆不理（通过输出 [SILENCE]），除非对方非常紧急。\n"
        f"  3. 如果是【假期/周末】，你可以表现得更慵懒或兴奋。\n"
        f"  4. 请根据上述状态自然地融入对话，例如抱怨作业多、分享午饭、吐槽老师等。\n"
    )
    return prompt

def is_rest_time() -> bool:
    """
    判断当前是否为休息时间 (适合主动发消息)
    基于现有作息设定：
    - 工作日：午休 (12-13), 晚间自由时间 (21-23)
    - 周末/假期：白天大部分时间 (9-22)
    """
    now = get_beijing_time()
    hour = now.hour
    weekday = now.weekday()
    month, day = now.month, now.day

    # 深夜/清晨绝对不发 (23:00 - 08:00)
    if hour >= 22 or hour < 9: # 稍微保守一点，22点以后不主动发
        return False

    # 1. 检查假期/周末
    is_holiday = (month, day) in FIXED_HOLIDAYS
    is_vacation = False
    # Winter
    if (month == 12 and day >= 25) or (month == 1 and day <= 7): is_vacation = True
    # Spring
    if (month == 3 and day >= 25) or (month == 4 and day <= 5): is_vacation = True
    # Summer
    if (month == 7 and day >= 20) or (month == 8): is_vacation = True

    if is_holiday or is_vacation or weekday >= 5:
        # 周末/假期 9:00 - 22:00 都可以认为是休息时间
        return True

    # 2. 工作日
    # 午休 12:00 - 13:00
    if 12 <= hour < 13:
        return True
    # 晚间自由时间 21:00 - 22:00 (23点睡觉，所以22点前发)
    if 21 <= hour < 22:
        return True
        
    return False
