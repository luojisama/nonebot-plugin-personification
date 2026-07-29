from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from .browser import BrowserPool
from .models import clean_text, normalize_url, stable_fingerprint


def _compact_count(value: str) -> int:
    text = clean_text(value, 80).lower().replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([万w千k]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {"万": 10000, "w": 10000, "千": 1000, "k": 1000}.get(match.group(2), 1)
    return int(number * multiplier)


@dataclass(frozen=True)
class PlatformSpec:
    name: str
    login_url: str
    search_url: str
    allowed_hosts: tuple[str, ...]
    content_link_selector: str
    discussion_selectors: tuple[tuple[str, str], ...]
    qr_selectors: tuple[str, ...]
    login_trigger_selectors: tuple[str, ...]
    auth_cookie_names: frozenset[str]
    content_type: str
    danmaku: bool


SPECS: dict[str, PlatformSpec] = {
    "bilibili": PlatformSpec(
        name="bilibili",
        login_url="https://passport.bilibili.com/login",
        search_url="https://search.bilibili.com/all?keyword={query}",
        allowed_hosts=("bilibili.com", "www.bilibili.com", "search.bilibili.com"),
        content_link_selector='a[href*="bilibili.com/video/"],a[href^="//www.bilibili.com/video/"]',
        discussion_selectors=((".reply-item .reply-content", "comment"), (".sub-reply-item .reply-content", "reply"), (".bili-dm", "danmaku")),
        qr_selectors=(
            'img[alt="Scan me!"]',
            '[title*="scan-web"] img',
            'img[src*="qrcode"]',
            'canvas[class*="qr"]',
            '[class*="qrcode"] img',
            '[class*="qr-code"] canvas',
        ),
        login_trigger_selectors=(),
        auth_cookie_names=frozenset({"SESSDATA", "DedeUserID"}),
        content_type="video",
        danmaku=True,
    ),
    "douyin": PlatformSpec(
        name="douyin",
        login_url="https://www.douyin.com/",
        search_url="https://www.douyin.com/search/{query}",
        allowed_hosts=("douyin.com", "www.douyin.com"),
        content_link_selector='a[href*="/video/"],a[href*="/note/"]',
        discussion_selectors=(("[data-e2e=comment-item]", "comment"), ('[class*="comment-item"]', "comment"), ('[class*="reply"]', "reply")),
        qr_selectors=(
            'img[alt="二维码"]',
            '[id^="login-full-panel-"] img[alt]',
            'img[src*="qrcode"]',
            'canvas[class*="qr"]',
            '[class*="login"] canvas',
            '[class*="qrcode"] img',
        ),
        login_trigger_selectors=('div:text-is("登录")', 'button:has-text("登录")'),
        # passport_csrf_token is issued to anonymous login pages as well and
        # must never be treated as proof that QR/device verification succeeded.
        auth_cookie_names=frozenset({"sessionid", "sessionid_ss"}),
        content_type="video",
        danmaku=True,
    ),
    "tieba": PlatformSpec(
        name="tieba",
        login_url="https://tieba.baidu.com/",
        search_url="https://tieba.baidu.com/f/search/res?ie=utf-8&qw={query}",
        allowed_hosts=("tieba.baidu.com", "baidu.com", "www.baidu.com"),
        content_link_selector='a[href*="/p/"]',
        discussion_selectors=((".l_post .d_post_content", "post"), (".j_l_post .d_post_content", "reply"), (".lzl_content_main", "reply")),
        qr_selectors=('img[src*="qrcode"]', 'img[src*="qr"]', '[class*="qrcode"] img', '[class*="tang-pass-qrcode"] img'),
        login_trigger_selectors=('a:has-text("登录")', 'button:has-text("登录")'),
        auth_cookie_names=frozenset({"BDUSS", "STOKEN", "PTOKEN"}),
        content_type="post",
        danmaku=False,
    ),
    "xiaoheihe": PlatformSpec(
        name="xiaoheihe",
        login_url="https://xiaoheihe.cn/app/bbs/home",
        search_url="https://xiaoheihe.cn/app/search/list?q={query}",
        allowed_hosts=("xiaoheihe.cn", "www.xiaoheihe.cn"),
        content_link_selector='a[href*="/app/bbs/link/"]',
        discussion_selectors=(("[class*=comment] [class*=content]", "comment"), ("[class*=reply] [class*=content]", "reply")),
        qr_selectors=(
            'canvas.website-login__qr-canvas',
            'canvas[class*="qr"]',
            'img[src*="qrcode"]',
            'img[src*="qr"]',
            '[class*="qrcode"] img',
            '[class*="login"] canvas',
        ),
        login_trigger_selectors=('button:has-text("登录")', '[class*="login"]:has-text("登录")'),
        auth_cookie_names=frozenset({"x_xhh_token", "heybox_id", "pkey", "account_token"}),
        content_type="article",
        danmaku=False,
    ),
}


class PlatformAdapter:
    def __init__(self, spec: PlatformSpec, browsers: BrowserPool) -> None:
        self.spec = spec
        self.browsers = browsers

    def capabilities(self) -> dict[str, bool]:
        return {
            "search": True,
            "detail": True,
            "cover": True,
            "caption": True,
            "comments": True,
            "replies": True,
            "danmaku": self.spec.danmaku,
            "authenticated": True,
        }

    async def authenticated(self, *, interactive: bool | None = None) -> bool:
        return await self.browsers.authenticated(
            self.spec.name,
            set(self.spec.auth_cookie_names),
            headless=None if interactive is None else not interactive,
        )

    def validate_url(self, value: str) -> str:
        url = normalize_url(value)
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise ValueError("invalid content URL") from exc
        host = (parsed.hostname or "").lower()
        allowed = any(host == suffix or host.endswith("." + suffix) for suffix in self.spec.allowed_hosts)
        if parsed.scheme != "https" or not allowed or parsed.username is not None or parsed.password is not None:
            raise ValueError("content URL is outside the selected platform")
        if self.spec.name == "xiaoheihe" and host == "www.xiaoheihe.cn":
            return parsed._replace(netloc="xiaoheihe.cn").geturl()
        return url

    async def start_auth(self, owner: str, *, mode: str = "embedded_qr") -> dict[str, Any]:
        if mode == "manual_browser":
            return await self.browsers.start_manual_auth(
                self.spec.name,
                owner,
                self.spec.login_url,
            )
        if mode != "embedded_qr":
            raise ValueError("unsupported auth mode")
        if self.spec.name == "bilibili":
            return await self.browsers.start_bilibili_qr_auth(owner)
        return await self.browsers.start_auth(
            self.spec.name,
            owner,
            self.spec.login_url,
            self.spec.qr_selectors,
            self.spec.login_trigger_selectors,
            prefer_headless=self.spec.name == "xiaoheihe",
        )

    async def _page(self, url: str, timeout_seconds: float) -> Any:
        page = await self.browsers.page(self.spec.name)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=max(3000, int(timeout_seconds * 1000)))
        await page.wait_for_timeout(800)
        state = await page.evaluate(
            """() => ({
                title: document.title || '',
                body: document.body ? document.body.innerText.slice(0, 2500) : '',
            })"""
        )
        state_text = clean_text(f"{state.get('title', '')} {state.get('body', '')}", 3000).lower()
        verification_markers = ("验证码", "安全验证", "人机验证", "完成下列验证", "拖动完成", "captcha")
        risk_markers = ("访问过于频繁", "操作频繁", "请求频繁", "risk control", "risk_control")
        if any(marker in state_text for marker in verification_markers):
            raise RuntimeError("manual_verification_required")
        status = int(getattr(response, "status", 0) or 0)
        if status in {401, 403, 429} or any(marker in state_text for marker in risk_markers):
            raise RuntimeError("risk_controlled")
        return page

    async def search(self, query: str, *, limit: int, timeout_seconds: float) -> list[dict[str, Any]]:
        url = self.spec.search_url.format(query=quote(clean_text(query, 200), safe=""))
        page = await self._page(url, timeout_seconds)
        rows = await page.locator(self.spec.content_link_selector).evaluate_all(
            """(nodes, options) => nodes.slice(0, options.maxItems * 4).map((node) => {
                const card = node.closest('article,li,[class*=card],[class*=item],[class*=video],[class*=result]') || node.parentElement;
                const img = card && card.querySelector('img');
                const text = (card && card.innerText) || node.innerText || '';
                const metricText = options.platform === 'xiaoheihe' && card
                    ? Array.from(card.querySelectorAll('button,[class*=stat],[class*=count],[class*=like],[class*=comment]'))
                        .map((item) => item.innerText || item.textContent || '')
                        .filter(Boolean)
                    : [];
                return {
                    href: node.href || node.getAttribute('href') || '',
                    title: node.getAttribute('title') || node.getAttribute('aria-label') || node.innerText || '',
                    text,
                    metricText,
                    cover: img ? (img.currentSrc || img.src || img.getAttribute('data-src') || '') : '',
                };
            })""",
            {"maxItems": limit, "platform": self.spec.name},
        )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            href = normalize_url(raw.get("href"))
            if not href:
                continue
            try:
                href = self.validate_url(href)
            except ValueError:
                continue
            content_id = self.content_id(href)
            if not content_id or content_id in seen:
                continue
            seen.add(content_id)
            text = clean_text(raw.get("text"), 1200)
            title = clean_text(raw.get("title"), 300) or text[:200]
            count_source = (
                " ".join(str(value or "") for value in list(raw.get("metricText") or []))
                if self.spec.name == "xiaoheihe"
                else text
            )
            counts = [
                _compact_count(value)
                for value in re.findall(r"[0-9]+(?:\.[0-9]+)?\s*[万w千k]?", count_source, re.IGNORECASE)
            ]
            stats = {
                "play_count": counts[0] if self.spec.content_type == "video" and counts else 0,
                "comment_count": counts[1] if self.spec.content_type == "video" and len(counts) > 1 else 0,
                "reply_count": counts[0] if self.spec.content_type != "video" and counts else 0,
            }
            result.append(
                {
                    "platform": self.spec.name,
                    "content_type": self.spec.content_type,
                    "content_id": content_id,
                    "canonical_url": href,
                    "title": title,
                    "caption_or_body": text,
                    "cover_ref": normalize_url(raw.get("cover")),
                    "author": {"display_name": "", "fingerprint": ""},
                    "published_at": 0,
                    "stats": stats,
                    "discussion": [],
                    "content_fingerprint": stable_fingerprint(title, text),
                }
            )
            if len(result) >= limit:
                break
        return result

    def content_id(self, url: str) -> str:
        parsed = urlparse(url)
        patterns = {
            "bilibili": r"/video/((?:BV|av)[A-Za-z0-9_-]+)(?:/|$)",
            "douyin": r"/(?:video|note)/([0-9]+)(?:/|$)",
            "tieba": r"/p/([0-9]+)(?:/|$)",
            "xiaoheihe": r"/app/bbs/link/([0-9]+)(?:/|$)",
        }
        match = re.search(patterns[self.spec.name], parsed.path, re.IGNORECASE)
        if match:
            return match.group(1)[:200]
        return ""

    async def read(
        self,
        *,
        content_id: str,
        url: str,
        include: list[str],
        comment_limit: int,
        danmaku_limit: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if url:
            canonical_url = self.validate_url(url)
        else:
            canonical_url = self.url_for_id(content_id)
        page = await self._page(canonical_url, timeout_seconds)
        if self.spec.name == "xiaoheihe":
            try:
                await page.wait_for_selector(
                    ".hb-bbs-post .post__content .hb-article",
                    state="visible",
                    timeout=max(1000, int(timeout_seconds * 1000)),
                )
            except Exception as exc:
                raise RuntimeError("detail_content_unavailable") from exc
            metadata_script = """() => {
                const root = document.querySelector('.hb-bbs-post');
                const title = root && root.querySelector('.link-section-title');
                const article = root && root.querySelector('.post__content .hb-article');
                const cover = article && article.querySelector('img');
                return {
                    title: title ? title.innerText : '',
                    description: article ? article.innerText : '',
                    cover: cover ? (cover.currentSrc || cover.src || '') : '',
                    body: '',
                };
            }"""
        else:
            metadata_script = """() => {
                const meta = (name, property=false) => {
                    const selector = property ? `meta[property="${name}"]` : `meta[name="${name}"]`;
                    const node = document.querySelector(selector);
                    return node ? (node.content || '') : '';
                };
                const h1 = document.querySelector('h1');
                return {
                    title: meta('og:title', true) || (h1 && h1.innerText) || document.title || '',
                    description: meta('description') || meta('og:description', true) || '',
                    cover: meta('og:image', true) || '',
                    body: document.body ? document.body.innerText.slice(0, 12000) : '',
                };
            }"""
        metadata = await page.evaluate(metadata_script)
        discussions: list[dict[str, Any]] = []
        if any(name in include for name in ("comments", "replies", "danmaku")):
            for selector, kind in self.spec.discussion_selectors:
                if kind == "danmaku" and "danmaku" not in include:
                    continue
                if kind == "comment" and "comments" not in include:
                    continue
                if kind in {"reply", "post"} and "replies" not in include and "comments" not in include:
                    continue
                maximum = danmaku_limit if kind == "danmaku" else comment_limit
                try:
                    texts = await page.locator(selector).all_inner_texts()
                except Exception:
                    texts = []
                for index, text in enumerate(texts[:maximum]):
                    cleaned = clean_text(text, 600)
                    if not cleaned:
                        continue
                    discussions.append(
                        {
                            "discussion_id": stable_fingerprint(canonical_url, kind, index, cleaned)[:32],
                            "type": kind,
                            "text": cleaned,
                            "author": {"display_name": "", "fingerprint": ""},
                            "published_at": 0,
                            "offset_seconds": 0,
                            "stats": {},
                        }
                    )
        title = clean_text(metadata.get("title"), 300)
        description = clean_text(metadata.get("description"), 4000)
        if not description:
            description = clean_text(metadata.get("body"), 4000)
        resolved_content_id = self.content_id(canonical_url)
        if not resolved_content_id:
            raise ValueError("content URL is not a supported content route")
        return {
            "platform": self.spec.name,
            "content_type": self.spec.content_type,
            "content_id": resolved_content_id,
            "canonical_url": canonical_url,
            "title": title,
            "caption_or_body": description,
            "cover_ref": normalize_url(metadata.get("cover")),
            "author": {"display_name": "", "fingerprint": ""},
            "published_at": 0,
            "stats": {
                "play_count": 0,
                "comment_count": sum(1 for item in discussions if item["type"] == "comment"),
                "reply_count": sum(1 for item in discussions if item["type"] in {"reply", "post"}),
                "danmaku_count": sum(1 for item in discussions if item["type"] == "danmaku"),
            },
            "discussion": discussions,
            "content_fingerprint": stable_fingerprint(title, description),
        }

    def url_for_id(self, content_id: str) -> str:
        safe = clean_text(content_id, 200)
        if not safe or not re.fullmatch(r"[A-Za-z0-9_-]+", safe):
            raise ValueError("content_id is invalid")
        templates = {
            "bilibili": "https://www.bilibili.com/video/{id}/",
            "douyin": "https://www.douyin.com/video/{id}",
            "tieba": "https://tieba.baidu.com/p/{id}",
            "xiaoheihe": "https://xiaoheihe.cn/app/bbs/link/{id}",
        }
        return templates[self.spec.name].format(id=safe)


def build_adapters(browsers: BrowserPool) -> dict[str, PlatformAdapter]:
    return {name: PlatformAdapter(spec, browsers) for name, spec in SPECS.items()}


__all__ = ["PlatformAdapter", "PlatformSpec", "SPECS", "build_adapters"]
