from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


_PINYIN_CHAR_MAP: dict[str, str] = {
    "安": "an",
    "案": "an",
    "包": "bao",
    "保": "bao",
    "本": "ben",
    "表": "biao",
    "并": "bing",
    "播": "bo",
    "采": "cai",
    "查": "cha",
    "长": "chang",
    "场": "chang",
    "超": "chao",
    "称": "cheng",
    "持": "chi",
    "迟": "chi",
    "重": "chong",
    "出": "chu",
    "储": "chu",
    "触": "chu",
    "窗": "chuang",
    "错": "cuo",
    "存": "cun",
    "打": "da",
    "单": "dan",
    "档": "dang",
    "到": "dao",
    "等": "deng",
    "低": "di",
    "地": "di",
    "递": "di",
    "点": "dian",
    "调": "diao",
    "定": "ding",
    "动": "dong",
    "度": "du",
    "短": "duan",
    "队": "dui",
    "对": "dui",
    "额": "e",
    "发": "fa",
    "返": "fan",
    "防": "fang",
    "访": "fang",
    "分": "fen",
    "风": "feng",
    "封": "feng",
    "复": "fu",
    "感": "gan",
    "高": "gao",
    "更": "geng",
    "工": "gong",
    "功": "gong",
    "关": "guan",
    "管": "guan",
    "广": "guang",
    "规": "gui",
    "国": "guo",
    "过": "guo",
    "好": "hao",
    "后": "hou",
    "化": "hua",
    "话": "hua",
    "回": "hui",
    "绘": "hui",
    "活": "huo",
    "获": "huo",
    "机": "ji",
    "级": "ji",
    "记": "ji",
    "间": "jian",
    "检": "jian",
    "键": "jian",
    "降": "jiang",
    "交": "jiao",
    "角": "jiao",
    "接": "jie",
    "节": "jie",
    "解": "jie",
    "禁": "jin",
    "进": "jin",
    "静": "jing",
    "境": "jing",
    "径": "jing",
    "旧": "jiu",
    "具": "ju",
    "据": "ju",
    "距": "ju",
    "绝": "jue",
    "开": "kai",
    "控": "kong",
    "口": "kou",
    "库": "ku",
    "宽": "kuan",
    "馈": "kui",
    "扩": "kuo",
    "拉": "la",
    "览": "lan",
    "老": "lao",
    "类": "lei",
    "冷": "leng",
    "离": "li",
    "理": "li",
    "历": "li",
    "连": "lian",
    "聊": "liao",
    "列": "lie",
    "流": "liu",
    "路": "lu",
    "录": "lu",
    "率": "lv",
    "码": "ma",
    "每": "mei",
    "密": "mi",
    "免": "mian",
    "描": "miao",
    "名": "ming",
    "模": "mo",
    "默": "mo",
    "目": "mu",
    "内": "nei",
    "拟": "ni",
    "年": "nian",
    "片": "pian",
    "配": "pei",
    "频": "pin",
    "平": "ping",
    "评": "ping",
    "启": "qi",
    "器": "qi",
    "签": "qian",
    "亲": "qin",
    "强": "qiang",
    "清": "qing",
    "情": "qing",
    "求": "qiu",
    "群": "qun",
    "权": "quan",
    "全": "quan",
    "热": "re",
    "人": "ren",
    "认": "ren",
    "日": "ri",
    "容": "rong",
    "入": "ru",
    "扫": "sao",
    "色": "se",
    "删": "shan",
    "上": "shang",
    "少": "shao",
    "设": "she",
    "身": "shen",
    "审": "shen",
    "生": "sheng",
    "声": "sheng",
    "时": "shi",
    "识": "shi",
    "始": "shi",
    "使": "shi",
    "式": "shi",
    "视": "shi",
    "手": "shou",
    "首": "shou",
    "输": "shu",
    "数": "shu",
    "双": "shuang",
    "说": "shuo",
    "私": "si",
    "搜": "sou",
    "速": "su",
    "随": "sui",
    "索": "suo",
    "态": "tai",
    "体": "ti",
    "题": "ti",
    "天": "tian",
    "贴": "tie",
    "停": "ting",
    "通": "tong",
    "同": "tong",
    "图": "tu",
    "推": "tui",
    "外": "wai",
    "网": "wang",
    "文": "wen",
    "问": "wen",
    "务": "wu",
    "息": "xi",
    "系": "xi",
    "细": "xi",
    "下": "xia",
    "限": "xian",
    "响": "xiang",
    "像": "xiang",
    "项": "xiang",
    "消": "xiao",
    "小": "xiao",
    "效": "xiao",
    "新": "xin",
    "信": "xin",
    "心": "xin",
    "行": "xing",
    "型": "xing",
    "性": "xing",
    "休": "xiu",
    "序": "xu",
    "需": "xu",
    "学": "xue",
    "询": "xun",
    "压": "ya",
    "延": "yan",
    "验": "yan",
    "钥": "yao",
    "页": "ye",
    "音": "yin",
    "隐": "yin",
    "应": "ying",
    "用": "yong",
    "友": "you",
    "优": "you",
    "语": "yu",
    "域": "yu",
    "源": "yuan",
    "远": "yuan",
    "阅": "yue",
    "月": "yue",
    "运": "yun",
    "载": "zai",
    "暂": "zan",
    "增": "zeng",
    "摘": "zhai",
    "展": "zhan",
    "张": "zhang",
    "账": "zhang",
    "找": "zhao",
    "照": "zhao",
    "者": "zhe",
    "针": "zhen",
    "整": "zheng",
    "证": "zheng",
    "值": "zhi",
    "智": "zhi",
    "制": "zhi",
    "置": "zhi",
    "中": "zhong",
    "终": "zhong",
    "主": "zhu",
    "注": "zhu",
    "转": "zhuan",
    "装": "zhuang",
    "追": "zhui",
    "字": "zi",
    "自": "zi",
    "总": "zong",
    "组": "zu",
    "最": "zui",
}


def normalize_search_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def compact_search_text(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalize_search_text(value))


def _fallback_lazy_pinyin(text: str) -> list[str]:
    parts: list[str] = []
    for char in str(text or ""):
        if char in _PINYIN_CHAR_MAP:
            parts.append(_PINYIN_CHAR_MAP[char])
        elif char.isascii() and char.isalnum():
            parts.append(char.lower())
    return parts


def _lazy_pinyin(text: str, *, initials: bool = False) -> list[str]:
    try:
        from pypinyin import Style, lazy_pinyin  # type: ignore

        style = Style.FIRST_LETTER if initials else Style.NORMAL
        return [str(item).lower() for item in lazy_pinyin(str(text or ""), style=style, errors="ignore") if str(item).strip()]
    except Exception:
        parts = _fallback_lazy_pinyin(text)
        if initials:
            return [part[:1] for part in parts if part]
        return parts


def _iter_values(values: Iterable[Any]) -> Iterable[str]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                text = normalize_search_text(item)
                if text:
                    yield text
            continue
        text = normalize_search_text(value)
        if text:
            yield text


def build_config_search_index(*values: Any) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = normalize_search_text(value)
        if not text:
            return
        for item in (text, compact_search_text(text)):
            if item and item not in seen:
                seen.add(item)
                tokens.append(item)

    for text in _iter_values(values):
        add(text)
        pinyin = _lazy_pinyin(text)
        if pinyin:
            add(" ".join(pinyin))
            add("".join(pinyin))
        initials = _lazy_pinyin(text, initials=True)
        if initials:
            add("".join(initials))
    return tokens


__all__ = [
    "build_config_search_index",
    "compact_search_text",
    "normalize_search_text",
]
