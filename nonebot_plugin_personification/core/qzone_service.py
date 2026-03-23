from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx


def _get_g_tk(p_skey: str) -> int:
    hash_val = 5381
    for char in p_skey:
        hash_val += (hash_val << 5) + ord(char)
    return hash_val & 0x7FFFFFFF


def _get_cookie_from_config(plugin_config: Any) -> str:
    for attr in ("personification_qzone_cookie", "qzone_cookie"):
        value = str(getattr(plugin_config, attr, "") or "").strip().strip('"').strip("'")
        if value:
            return value
    return ""


def _persist_cookie_to_env(cookie: str, logger: Any) -> None:
    cookie_line = f'personification_qzone_cookie="{cookie}"\n'
    for env_path in (Path(".env.prod"), Path(".env")):
        if not env_path.exists():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = []
            found = False
            for line in lines:
                if line.strip().startswith("personification_qzone_cookie="):
                    new_lines.append(cookie_line)
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                if new_lines and not new_lines[-1].endswith(("\n", "\r\n")):
                    new_lines[-1] = new_lines[-1] + "\n"
                new_lines.append(cookie_line)
            env_path.write_text("".join(new_lines), encoding="utf-8")
            return
        except Exception as e:
            logger.error(f"拟人插件：保存 Qzone Cookie 到 {env_path} 失败: {e}")


def build_qzone_services(
    plugin_config: Any,
    logger: Any,
) -> tuple[bool, Callable[[str, str], Awaitable[tuple[bool, str]]], Callable[[Any], Awaitable[tuple[bool, str]]]]:
    async def update_qzone_cookie(bot: Any) -> tuple[bool, str]:
        """自动获取并刷新 Qzone Cookie，供定时任务或手动命令调用。"""
        try:
            cookies_resp = await bot.get_cookies(domain="qzone.qq.com")
            cookie = str(cookies_resp.get("cookies", "") or "").strip()
            if not cookie:
                return False, "自动获取 Cookie 失败，返回结果为空"
            if "p_skey" not in cookie:
                return False, "获取到的 Cookie 不完整（缺少 p_skey）"
            if "uin=" not in cookie:
                cookie = f"uin=o{bot.self_id}; {cookie}"
            plugin_config.personification_qzone_cookie = cookie
            _persist_cookie_to_env(cookie, logger)
            return True, cookie
        except Exception as e:
            return False, str(e)

    async def publish_qzone_shuo(content: str, bot_id: str) -> tuple[bool, str]:
        cookie = _get_cookie_from_config(plugin_config)
        if not cookie:
            return False, "未配置 Qzone Cookie"

        try:
            cleaned_content = re.sub(r"\[图片(?:·[^\]]+)?\]|\[表情\]|\[动画表情\]", "", str(content or "")).strip()
            if not cleaned_content:
                return False, "说说内容不能为空（已过滤图片和表情）"

            pskey_match = re.search(r"p_skey=([^; ]+)", cookie)
            if not pskey_match:
                return False, "Cookie 缺少 p_skey 字段"
            p_skey = pskey_match.group(1)

            uin_match = re.search(r"uin=[o0]*(\d+)", cookie)
            qq = uin_match.group(1) if uin_match else str(bot_id)

            formatted_cookie = f"uin=o{qq}; p_skey={p_skey};"
            if "skey=" in cookie:
                skey_match = re.search(r"skey=([^; ]+)", cookie)
                if skey_match:
                    formatted_cookie += f" skey={skey_match.group(1)};"

            g_tk = _get_g_tk(p_skey)
            url = (
                "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/"
                f"cgi-bin/emotion_cgi_publish_v6?g_tk={g_tk}"
            )
            data = {
                "syn_tweet_version": 1,
                "paramstr": 1,
                "pic_template": "",
                "richtype": "",
                "richval": "",
                "special_url": "",
                "subrichtype": "",
                "con": cleaned_content,
                "feed_tpl_id": "w_v6",
                "ugc_right": 1,
                "who": 1,
                "modifyflag": 0,
                "hostuin": qq,
                "format": "json",
                "qzreferrer": f"https://user.qzone.qq.com/{qq}",
            }
            headers = {
                "Cookie": formatted_cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                ),
                "Referer": f"https://user.qzone.qq.com/{qq}",
                "Origin": "https://user.qzone.qq.com",
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, data=data, headers=headers)
            if resp.status_code != 200:
                return False, f"请求失败，状态码：{resp.status_code}"

            resp_text = resp.text
            if '"code":0' in resp_text or '"code": 0' in resp_text:
                return True, "发布成功"

            resp_lower = resp_text.strip().lower()
            if resp_lower.startswith("<html") or resp_lower.startswith("<!doctype"):
                return False, "Qzone 返回了登录页面或验证码，请尝试重新获取空间 Cookie"

            msg_match = re.search(r'"message":"([^"]+)"', resp_text)
            err_msg = msg_match.group(1) if msg_match else resp_text[:100]
            return False, f"发布失败，返回：{err_msg}"
        except Exception as e:
            return False, f"发生异常：{e}"

    return True, publish_qzone_shuo, update_qzone_cookie
