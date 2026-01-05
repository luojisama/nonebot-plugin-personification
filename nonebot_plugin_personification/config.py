from pydantic import BaseModel
from typing import List, Dict, Optional

class Config(BaseModel):
    personification_whitelist: List[str] = []  # 启用群聊白名单
    personification_probability: float = 0.5   # 默认回复概率 0-1
    
    # API 相关配置
    personification_api_url: str = "https://api.openai.com/v1"
    personification_api_key: str = ""
    personification_model: str = "gpt-3.5-turbo"
    
    # 提示词配置
    personification_system_prompt: str = "你是一个群聊成员，性格活泼，说话幽默。你可以根据当前语境决定是否回复，如果不回复请只输出 [NO_REPLY]。"
    personification_prompt_path: Optional[str] = None  # 提示词文件路径
    personification_system_path: Optional[str] = None  # 兼容性别名：提示词文件路径
    
    # 不同好感度的态度提示词
    # 格式: { "等级名称": "态度描述" }
    personification_favorability_attitudes: Dict[str, str] = {
        "初见": "保持基本的礼貌，态度温和但不过于亲热。",
        "面熟": "表现得比较客气，愿意倾听并给予简单的回应。",
        "初识": "态度随和，偶尔会分享一些有趣的小事，语气活泼。",
        "普通": "像普通朋友一样轻松交流，会主动接话。",
        "熟悉": "言谈举止比较随意，经常互相调侃，表现得很开心。",
        "信赖": "非常信任对方，说话很贴心，会表达关心。",
        "知心": "默契十足，有很多共同话题，语气变得亲近。",
        "深厚": "关系非常深厚，会主动分享心情，给予对方支持。",
        "挚友": "无话不谈，对对方充满热情和信任。",
        "亲密": "非常亲昵，语气温柔，充满了宠溺和爱护。"
    }

    # 聊天记录参考长度
    personification_history_len: int = 50

    # 表情包配置
    personification_sticker_path: Optional[str] = "data/stickers"  # 表情包文件夹路径
    personification_sticker_probability: float = 0.2              # 发送表情包概率

    # 戳一戳配置
    personification_poke_probability: float = 0.3                 # 戳一戳响应概率

    # 拉黑时长 (单位: 秒)，默认 30 分钟
    personification_blacklist_duration: int = 1800
