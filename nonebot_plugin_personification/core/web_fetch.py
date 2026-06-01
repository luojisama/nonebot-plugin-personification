"""通用网页抓取与正文抽取。

为 agent 提供"给一个 URL 拿回正文文本"的能力。设计要点：
- 60 秒整体超时（由调用方传入，默认 60s）
- SSRF 防御：拒绝非 http(s) scheme，DNS 解析后若任一 IP 落在
  私有/回环/链路本地段则拒绝
- 用 bs4 + lxml 抽正文（剥离 script/style/nav/header/footer/
  aside/form/iframe，优先取 article/main/body）；不依赖
  readability 或 trafilatura
- 输出按字符数截断（默认 3000），避免占满 LLM 上下文
- 单页字节上限 2 MB 防爆内存
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

_DEFAULT_TIMEOUT = 60.0
_DEFAULT_MAX_CHARS = 3000
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PersonificationBot/1.0"
)


class WebFetchError(Exception):
    """web_fetch 内部统一异常，调用方据此降级。"""


def _is_unsafe_host(host: str) -> bool:
    """通过 DNS 解析判断目标 host 是否落在内网/回环/链路本地段。

    解析失败时保守判为不安全，防止 DNS 错误被当成放行。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    if not infos:
        return True
    for info in infos:
        sockaddr = info[4] if len(info) >= 5 else None
        addr = sockaddr[0] if sockaddr else ""
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def _is_literal_private_ip(host: str) -> bool:
    """host 本身就是字面 IP 且落在内网/本地段时返回 True；非 IP 字面量返回 False。

    走代理时只用这个做防护：DNS 由代理侧解析，本地解析结果不可信，
    但若用户直接给了内网 IP 字面量仍要拦截。
    """
    h = (host or "").strip().strip("[]")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_url(url: str) -> tuple[str, str]:
    """返回 (scheme, host) 或抛 WebFetchError。"""
    parsed = urlparse(str(url or "").strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise WebFetchError(f"不允许的 URL scheme：{scheme or '(空)'}（只允许 http/https）")
    host = (parsed.hostname or "").strip()
    if not host:
        raise WebFetchError("URL 缺少主机名")
    return scheme, host


def _is_blocked_domain(host: str, blocked_domains: list[str] | None) -> bool:
    if not blocked_domains:
        return False
    host_lower = host.lower()
    for pattern in blocked_domains:
        p = str(pattern or "").strip().lower()
        if not p:
            continue
        if host_lower == p or host_lower.endswith("." + p):
            return True
    return False


def _extract_main_text(html_text: str, *, max_chars: int) -> tuple[str, str]:
    """从 HTML 抽 (title, body_text)，body_text 截断到 max_chars 字符。"""
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        soup = BeautifulSoup(html_text, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    for tag in soup(["script", "style", "noscript", "iframe", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return title, text


async def fetch_web_page(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_chars: int = _DEFAULT_MAX_CHARS,
    blocked_domains: list[str] | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """抓取指定 URL 并返回结构化结果。失败时抛 WebFetchError。

    返回字典字段：
    - url: 最终 URL（含重定向）
    - status_code: HTTP 状态码
    - title: 页面标题
    - text: 抽取后的正文（已截断）
    - content_type: 服务器返回的 Content-Type
    - char_count: text 实际字符数
    """
    scheme, host = _validate_url(url)
    if _is_blocked_domain(host, blocked_domains):
        raise WebFetchError(f"目标域名被黑名单拦截：{host}")
    proxy_url = str(proxy or "").strip() or None
    if proxy_url:
        # 走代理：DNS 由代理侧解析，本地解析结果不可信（可能被污染），
        # 只拦截字面内网 IP，避免误把公网站点（被本地 DNS 污染）当成内网拒绝。
        if _is_literal_private_ip(host):
            raise WebFetchError(f"目标主机是内网/本地 IP，拒绝访问：{host}")
    else:
        is_unsafe = await asyncio.to_thread(_is_unsafe_host, host)
        if is_unsafe:
            raise WebFetchError(
                f"目标主机解析到内网/本地地址，拒绝访问：{host}"
                "（若该域名确为公网站点，可能是本地 DNS 被污染/劫持；"
                "可在配置 personification_web_proxy 设置代理后重试）"
            )

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    safe_max_chars = max(200, int(max_chars or _DEFAULT_MAX_CHARS))
    try:
        client_kwargs: dict[str, Any] = {
            "timeout": float(timeout or _DEFAULT_TIMEOUT),
            "follow_redirects": True,
            "max_redirects": 5,
            "headers": headers,
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url)
            content_type = (resp.headers.get("content-type", "") or "").lower()
            raw = resp.content
            if len(raw) > _MAX_BYTES:
                raw = raw[:_MAX_BYTES]
            try:
                html_text = resp.text
            except Exception:
                html_text = raw.decode("utf-8", errors="replace")
            if "html" not in content_type and "xml" not in content_type:
                text = html_text[:safe_max_chars]
                if len(html_text) > safe_max_chars:
                    text = text.rstrip() + "…"
                return {
                    "url": str(resp.url),
                    "status_code": resp.status_code,
                    "title": "",
                    "text": text,
                    "content_type": content_type,
                    "char_count": len(text),
                }
            title, text = _extract_main_text(html_text, max_chars=safe_max_chars)
            return {
                "url": str(resp.url),
                "status_code": resp.status_code,
                "title": title,
                "text": text,
                "content_type": content_type,
                "char_count": len(text),
            }
    except httpx.TimeoutException as exc:
        raise WebFetchError(f"请求超时（{timeout}s）：{exc}") from exc
    except httpx.HTTPError as exc:
        raise WebFetchError(f"HTTP 错误：{exc}") from exc


__all__ = ["fetch_web_page", "WebFetchError"]
