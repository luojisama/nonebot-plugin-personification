from typing import Any, Dict, List


def collect_perm_blacklist_items(data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从签到数据中提取永久黑名单用户。"""
    items: List[Dict[str, Any]] = []
    for uid, udata in data.items():
        if uid.startswith("group_"):
            continue
        if not udata.get("is_perm_blacklisted", False):
            continue

        items.append(
            {
                "id": uid,
                "count": int(udata.get("blacklist_count", 0) or 0),
                "fav": float(udata.get("favorability", 0.0) or 0.0),
            }
        )
    return items


def build_perm_blacklist_card_markdown(items: List[Dict[str, Any]]) -> str:
    """构建永久黑名单卡片的 HTML/Markdown 文本。"""
    title_color = "#ff69b4"
    text_color = "#d147a3"
    border_color = "#ffb6c1"
    bg_color = "#fff5f8"

    item_blocks = []
    for item in items:
        item_blocks.append(
            f"""
        <div style="background: white; padding: 12px; border-radius: 10px; border: 1px solid {border_color}; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <div style="font-weight: bold; color: {text_color}; font-size: 1.1em;">{item['id']}</div>
                <div style="font-size: 0.85em; color: #999;">好感度: {item['fav']:.2f}</div>
            </div>
            <div style="text-align: right;">
                <div style="color: #ff4d4f; font-weight: bold;">{item['count']} 次拉黑</div>
                <div style="font-size: 0.8em; color: #ff9999;">⚠️ 永久封禁</div>
            </div>
        </div>
            """
        )

    items_html = "\n".join(item_blocks)
    return f"""
<div style="padding: 20px; background-color: {bg_color}; border-radius: 15px; border: 2px solid {border_color}; font-family: 'Microsoft YaHei', sans-serif;">
    <h1 style="color: {title_color}; text-align: center; margin-bottom: 20px;">🚫 永久黑名单列表 🚫</h1>
    
    <div style="margin-bottom: 15px;">
        {items_html}
    </div>

    <div style="font-size: 0.9em; color: #888; background: rgba(255,255,255,0.5); padding: 10px; border-radius: 8px; line-height: 1.4; text-align: center;">
        此列表中的用户已被永久禁止与 AI 进行交互。<br>使用「取消永久拉黑」指令可恢复权限。
    </div>
</div>
"""


def build_perm_blacklist_text(items: List[Dict[str, Any]]) -> str:
    """构建永久黑名单纯文本退化输出。"""
    lines = ["🚫 永久黑名单列表 🚫"]
    for item in items:
        lines.append(f"- {item['id']} ({item['count']}次拉黑 / 好感:{item['fav']:.2f})")
    return "\n".join(lines)
