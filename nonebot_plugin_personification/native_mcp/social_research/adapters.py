from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .browser import BrowserPool
from .models import clean_text, normalize_url, stable_fingerprint


_VIDEO_MEDIA_HOSTS: dict[str, tuple[str, ...]] = {
    "bilibili": ("bilivideo.com", "bilibili.com"),
    "douyin": ("douyinvod.com", "douyin.com", "bytecdn.cn", "byteimg.com", "pstatp.com"),
}
_BILIBILI_SUBTITLE_SEMAPHORE = asyncio.Semaphore(2)
_BILIBILI_SUBTITLE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def normalize_platform_video_url(platform: str, value: Any) -> str:
    url = normalize_url(value)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not any(host == suffix or host.endswith("." + suffix) for suffix in _VIDEO_MEDIA_HOSTS.get(platform, ()))
    ):
        return ""
    return parsed.geturl()


def _subtitle_url_allowed(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and any(
            host == suffix or host.endswith("." + suffix)
            for suffix in ("bilibili.com", "hdslb.com", "bilivideo.com", "biliapi.net")
        )
    )


def _bilibili_subtitle_track(canonical_url: str) -> tuple[str, str]:
    try:
        import yt_dlp
    except Exception:
        return "", ""
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 8}
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(canonical_url, download=False)
    except Exception:
        return "", ""
    if not isinstance(info, dict):
        return "", ""
    collections = [info.get("subtitles"), info.get("automatic_captions")]
    languages = ("zh-CN", "zh-Hans", "zh-Hant", "zh", "ai-zh")
    for collection in collections:
        if not isinstance(collection, dict):
            continue
        for language in languages:
            tracks = collection.get(language)
            if not isinstance(tracks, list):
                continue
            for track in reversed(tracks):
                if not isinstance(track, dict):
                    continue
                inline = str(track.get("data") or "")
                if inline:
                    return inline, ""
                url = str(track.get("url") or "").strip()
                if _subtitle_url_allowed(url):
                    return "", url
    return "", ""


def _parse_subtitle_payload(raw: str, *, maximum: int = 200) -> list[dict[str, Any]]:
    try:
        payload = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        payload = None
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        candidates = payload.get("body") or payload.get("events") or []
        for item in list(candidates):
            if not isinstance(item, dict):
                continue
            text = clean_text(item.get("content") or item.get("text"), 600)
            if not text and isinstance(item.get("segs"), list):
                text = clean_text(
                    "".join(str(segment.get("utf8") or "") for segment in item["segs"] if isinstance(segment, dict)),
                    600,
                )
            if text:
                rows.append({"text": text, "offset_seconds": float(item.get("from") or 0.0)})
            if len(rows) >= maximum:
                break
        return rows
    for line in str(raw or "").splitlines():
        text = re.sub(r"<[^>]+>", "", line).strip()
        if not text or "-->" in text or text.upper() == "WEBVTT" or re.fullmatch(r"[0-9]+", text):
            continue
        if not rows or rows[-1]["text"] != text:
            rows.append({"text": clean_text(text, 600), "offset_seconds": 0.0})
        if len(rows) >= maximum:
            break
    return rows


async def read_bilibili_ai_subtitles(canonical_url: str) -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _BILIBILI_SUBTITLE_CACHE.get(canonical_url)
    if cached is not None and now - cached[0] <= 1800:
        return [dict(item) for item in cached[1]]
    async with _BILIBILI_SUBTITLE_SEMAPHORE:
        inline, url = await asyncio.to_thread(_bilibili_subtitle_track, canonical_url)
        raw = inline
        if url:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=False, trust_env=False
                ) as client:
                    response = await client.get(url, headers={"Accept": "application/json,text/vtt,text/plain"})
                    response.raise_for_status()
                    if len(response.content) > 2 * 1024 * 1024:
                        return []
                    raw = response.text
            except Exception:
                return []
        rows = _parse_subtitle_payload(raw) if raw else []
        if len(_BILIBILI_SUBTITLE_CACHE) >= 64:
            oldest = min(_BILIBILI_SUBTITLE_CACHE, key=lambda key: _BILIBILI_SUBTITLE_CACHE[key][0])
            _BILIBILI_SUBTITLE_CACHE.pop(oldest, None)
        _BILIBILI_SUBTITLE_CACHE[canonical_url] = (time.monotonic(), [dict(item) for item in rows])
        return rows


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
        discussion_selectors=(
            ('[data-e2e="comment-item"]', "comment"),
            ('[data-e2e="comment-item"] [class*="reply"]', "reply"),
            ('[class*="danmaku"] [class*="item"]', "danmaku"),
        ),
        qr_selectors=(
            'img[alt="二维码"]',
            '[id^="login-full-panel-"] img[alt*="二维码"]',
            'img[src*="qrcode"]',
            'canvas[class*="qr"]',
            '[class*="login"] canvas',
            '[class*="qrcode"] img',
        ),
        login_trigger_selectors=('div:text-is("登录")', 'button:has-text("登录")'),
        # passport_csrf_token is issued to anonymous login pages as well and
        # must never be treated as proof that QR/device verification succeeded.
        # sid_guard is a real account-session cookie and may be the first
        # durable login marker written after the mobile app confirms the QR.
        auth_cookie_names=frozenset({"sessionid", "sessionid_ss", "sid_guard"}),
        content_type="video",
        danmaku=True,
    ),
    "tieba": PlatformSpec(
        name="tieba",
        login_url="https://tieba.baidu.com/",
        search_url="https://tieba.baidu.com/f/search/res?ie=utf-8&qw={query}",
        allowed_hosts=("tieba.baidu.com", "baidu.com", "www.baidu.com"),
        content_link_selector='a[href*="/p/"]',
        discussion_selectors=(
            (".pb-comment-item", "comment"),
            (".pb-comment-item [class*=reply]", "reply"),
            (".l_post .d_post_content", "post"),
            (".lzl_content_main", "reply"),
        ),
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
        discussion_selectors=(
            (".link-comment__comment-item .comment-item__content", "comment"),
            (".link-comment__comment-item .children-item__comment-content", "reply"),
        ),
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
        if mode == "webui_interactive":
            return await self.browsers.start_interactive_auth(
                self.spec.name,
                owner,
                self.spec.login_url,
                self.spec.allowed_hosts,
                self.spec.qr_selectors,
                self.spec.login_trigger_selectors,
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

    async def _page(self, url: str, timeout_seconds: float) -> tuple[Any, bool]:
        fresh_page = getattr(self.browsers, "fresh_page", None)
        owned_page = callable(fresh_page)
        page = await fresh_page(self.spec.name) if owned_page else await self.browsers.page(self.spec.name)
        try:
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
            return page, owned_page
        except Exception:
            if owned_page:
                await page.close()
            raise

    def _search_selector(self) -> str:
        if self.spec.name == "douyin":
            return '[id^="waterfall_item_"]'
        if self.spec.name == "tieba":
            return ".virtual-list-item"
        return self.spec.content_link_selector

    def _search_script(self) -> str:
        if self.spec.name == "douyin":
            return """(cards, options) => cards.slice(0, options.maxItems * 4).map((card) => {
            const idMatch = /^waterfall_item_([0-9]+)$/.exec(card.id || '');
            const text = card.innerText || '';
            const exactText = (value) => String(value || '').trim();
            const nodes = Array.from(card.querySelectorAll('*'));
            const noteMarker = nodes.some((node) => exactText(node.textContent) === '图文');
            const durationMarker = nodes.some((node) => /^[0-9]{1,2}:[0-9]{2}$/.test(exactText(node.textContent)));
            const imageUrls = Array.from(card.querySelectorAll('img')).map((item) => ({
                url: item.currentSrc || item.src || item.getAttribute('data-src') || '',
                alt: item.getAttribute('alt') || item.getAttribute('title') || '',
            })).filter((item) => item.url);
            const signedUrls = imageUrls.map((item) => item.url).join(' ');
            const isNote = noteMarker || signedUrls.includes('card_type=303');
            const isVideo = !isNote && (durationMarker || card.querySelector('video') || signedUrls.includes('card_type=153'));
            const titleNode = card.querySelector('h1,h2,h3,[data-e2e*=title],[class*=title]');
            const authorNode = card.querySelector('[data-e2e*=author],[class*=author],[class*=nickname]');
            const metricText = Array.from(card.querySelectorAll('[data-e2e*=count],[class*=count],[class*=stats],[class*=metric]'))
                .map((item) => item.innerText || item.textContent || '').filter(Boolean);
            const related = Boolean(card.querySelector('[class*=related-search],[data-e2e*=related-search]'))
                || exactText(text).startsWith('相关搜索');
            const promoted = Boolean(card.querySelector('[class*=advert],[class*=promotion],[data-e2e*=ad]'));
            return {
                contentId: idMatch ? idMatch[1] : '',
                contentKind: isNote ? 'note' : (isVideo ? 'video' : ''),
                title: titleNode ? titleNode.innerText : '',
                author: authorNode ? authorNode.innerText : '',
                text,
                metricText,
                cover: imageUrls.length ? imageUrls[0].url : '',
                images: imageUrls,
                skip: related || promoted,
                commercialLabel: promoted,
            };
            })"""
        if self.spec.name == "tieba":
            return """(cards, options) => cards.slice(0, options.maxItems * 4).map((outer) => {
            const card = outer.querySelector('.threadcardclass,.thread-new3.index-feed-cards,.thread-content-box') || outer;
            const anchors = Array.from(card.querySelectorAll('a[href*="/p/"]'));
            const contentAnchor = anchors.find((node) => {
                try { return /^\\/p\\/[0-9]+(?:\\/|$)/.test(new URL(node.href, location.href).pathname); }
                catch (_) { return false; }
            });
            const titleNode = card.querySelector('[class*=title],h1,h2,h3') || contentAnchor;
            const authorNode = card.querySelector('[class*=author],[class*=user],[class*=name]');
            const imageUrls = Array.from(card.querySelectorAll('.thread-content-box img,[class*=content] img')).map((item) => ({
                url: item.currentSrc || item.src || item.getAttribute('data-src') || '',
                alt: item.getAttribute('alt') || item.getAttribute('title') || '',
            })).filter((item) => item.url && !item.closest('[class*=avatar]'));
            const metricText = Array.from(card.querySelectorAll('[class*=count],[class*=reply],[class*=like],[class*=stats]'))
                .map((item) => item.innerText || item.textContent || '').filter(Boolean);
            const promoted = Boolean(card.querySelector('[class*=advert],[class*=promotion],[class*=recommend-ad]'));
            return {
                href: contentAnchor ? (contentAnchor.href || contentAnchor.getAttribute('href') || '') : '',
                title: titleNode ? titleNode.innerText : '',
                author: authorNode ? authorNode.innerText : '',
                text: card.innerText || '',
                metricText,
                cover: imageUrls.length ? imageUrls[0].url : '',
                images: imageUrls,
                skip: promoted,
                commercialLabel: promoted,
            };
            })"""
        return """(nodes, options) => nodes.slice(0, options.maxItems * 4).map((node) => {
        const card = node.closest('article,li,[class*=card],[class*=item],[class*=video],[class*=result]') || node.parentElement;
        const img = card && card.querySelector('img');
        const text = (card && card.innerText) || node.innerText || '';
        const images = card
            ? Array.from(card.querySelectorAll('img')).map((item) => ({
                url: item.currentSrc || item.src || item.getAttribute('data-src') || '',
                alt: item.getAttribute('alt') || item.getAttribute('title') || '',
            })).filter((item) => item.url)
            : [];
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
            images,
        };
        })"""

    async def search(self, query: str, *, limit: int, timeout_seconds: float) -> list[dict[str, Any]]:
        url = self.spec.search_url.format(query=quote(clean_text(query, 200), safe=""))
        page, owned_page = await self._page(url, timeout_seconds)
        try:
            selector = self._search_selector()
            if self.spec.name in {"douyin", "tieba", "xiaoheihe"}:
                await page.wait_for_selector(
                    selector,
                    state="attached",
                    timeout=max(1000, int(timeout_seconds * 1000) - 1000),
                )
            rows = await page.locator(selector).evaluate_all(
                self._search_script(),
                {"maxItems": limit, "platform": self.spec.name},
            )
        finally:
            if owned_page:
                await page.close()
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows:
            if not isinstance(raw, dict) or bool(raw.get("skip")):
                continue
            content_kind = clean_text(raw.get("contentKind"), 20).lower()
            if self.spec.name == "douyin":
                raw_content_id = clean_text(raw.get("contentId"), 200)
                if not re.fullmatch(r"[0-9]+", raw_content_id) or content_kind not in {"video", "note"}:
                    continue
                href = f"https://www.douyin.com/{content_kind}/{raw_content_id}"
            else:
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
            metric_source = " ".join(str(value or "") for value in list(raw.get("metricText") or []))
            count_source = metric_source or text
            counts = [
                _compact_count(value)
                for value in re.findall(r"[0-9]+(?:\.[0-9]+)?\s*[万w千k]?", count_source, re.IGNORECASE)
            ]
            content_type = "article" if self.spec.name == "douyin" and content_kind == "note" else self.spec.content_type
            stats = {
                "play_count": counts[0] if content_type == "video" and counts else 0,
                "comment_count": counts[1] if content_type == "video" and len(counts) > 1 else 0,
                "reply_count": counts[0] if content_type != "video" and counts else 0,
            }
            image_urls = list(
                dict.fromkeys(
                    normalize_url(item.get("url"))
                    for item in list(raw.get("images") or [])
                    if isinstance(item, dict) and normalize_url(item.get("url"))
                )
            )[:6]
            image_count_match = re.search(r"共\s*([0-9]+)\s*张", text)
            result.append(
                {
                    "platform": self.spec.name,
                    "content_type": content_type,
                    "content_id": content_id,
                    "canonical_url": href,
                    "title": title,
                    "caption_or_body": text,
                    "cover_ref": normalize_url(raw.get("cover")),
                    "image_urls": image_urls,
                    "image_count": max(len(image_urls), int(image_count_match.group(1)) if image_count_match else 0),
                    "author": {"display_name": clean_text(raw.get("author"), 120), "fingerprint": ""},
                    "published_at": 0,
                    "stats": stats,
                    "discussion": [],
                    "commercial_label": bool(raw.get("commercialLabel")),
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

    def _content_type_for_url(self, url: str) -> str:
        if self.spec.name == "douyin" and urlparse(url).path.lower().startswith("/note/"):
            return "article"
        return self.spec.content_type

    def _detail_selector(self, canonical_url: str) -> str:
        if self.spec.name == "xiaoheihe":
            return (
                ".hb-bbs-link__content .hb-bbs-image-text .image-text__content,"
                ".hb-bbs-link__content .image-text__content,"
                ".hb-bbs-post .post__content .hb-article"
            )
        if self.spec.name == "douyin":
            return "main" if urlparse(canonical_url).path.lower().startswith("/note/") else "h1"
        if self.spec.name == "tieba":
            return ".pb-content-wrap"
        return ""

    def _detail_script(self, canonical_url: str) -> str:
        if self.spec.name == "xiaoheihe":
            return """() => {
            const root = document.querySelector('.hb-bbs-link__content,.hb-bbs-post');
            const title = root && root.querySelector('.link-section-title');
            const article = root && root.querySelector(
                '.hb-bbs-image-text .image-text__content,.image-text__content,.post__content .hb-article'
            );
            const cover = article && article.querySelector('img');
            const images = article ? Array.from(article.querySelectorAll('img')).map((item) => ({
                url: item.currentSrc || item.src || item.getAttribute('data-src') || '',
                alt: item.getAttribute('alt') || item.getAttribute('title') || '',
            })).filter((item) => item.url) : [];
            return {
                title: title ? title.innerText : '',
                description: article ? article.innerText : '',
                cover: cover ? (cover.currentSrc || cover.src || '') : '',
                images,
                author: '',
                body: '',
            };
            }"""
        if self.spec.name == "douyin":
            is_note = urlparse(canonical_url).path.lower().startswith("/note/")
            return f"""() => {{
            const isNote = {str(is_note).lower()};
            const main = document.querySelector('main,[role="main"]');
            const heading = document.querySelector('h1');
            const root = isNote ? main : (heading && heading.closest('main,[role="main"]')) || main || heading;
            const clone = root ? root.cloneNode(true) : null;
            if (clone) {{
                clone.querySelectorAll('[data-e2e="comment-item"],[class*="comment-list"],[class*="recommend"],[class*="related"]')
                    .forEach((node) => node.remove());
            }}
            const images = root ? Array.from(root.querySelectorAll('img')).filter((item) =>
                !item.closest('[data-e2e="comment-item"],[class*="comment"],[class*="avatar"],[class*="recommend"],[class*="related"]')
            ).map((item) => ({{
                url: item.currentSrc || item.src || item.getAttribute('data-src') || '',
                alt: item.getAttribute('alt') || item.getAttribute('title') || '',
            }})).filter((item) => item.url) : [];
            const author = root && root.querySelector('[data-e2e*=author],[class*=author],[class*=nickname]');
            const video = !isNote && root ? root.querySelector('video') : null;
            return {{
                title: heading ? heading.innerText : (document.title || ''),
                description: isNote ? ((clone && clone.innerText) || '') : ((heading && heading.innerText) || ''),
                cover: images.length ? images[0].url : '',
                images,
                author: author ? author.innerText : '',
                video: video ? (video.currentSrc || video.src || '') : '',
                body: '',
            }};
            }}"""
        if self.spec.name == "tieba":
            return """() => {
            const root = document.querySelector('.pb-content-wrap');
            const title = document.querySelector('h1,.core_title_txt,[class*=thread-title]');
            const contentNodes = root ? Array.from(root.querySelectorAll('.richtext-item,.pb-text-wrapper')) : [];
            const descriptions = [];
            const seen = new Set();
            for (const node of contentNodes) {
                if (node.closest('.pb-comment-list,.pb-comment-item')) continue;
                const text = (node.innerText || '').trim();
                if (text && !seen.has(text)) { seen.add(text); descriptions.push(text); }
            }
            const images = root ? Array.from(root.querySelectorAll('.richtext-item img,.pb-text-wrapper img')).filter((item) =>
                !item.closest('.pb-comment-list,.pb-comment-item,[class*=avatar]')
            ).map((item) => ({
                url: item.currentSrc || item.src || item.getAttribute('data-src') || '',
                alt: item.getAttribute('alt') || item.getAttribute('title') || '',
            })).filter((item) => item.url) : [];
            const author = root && root.querySelector('[class*=author],[class*=user-name],[class*=username]');
            return {
                title: title ? title.innerText : (document.title || ''),
                description: descriptions.join('\n'),
                cover: images.length ? images[0].url : '',
                images,
                author: author ? author.innerText : '',
                body: '',
            };
            }"""
        return """() => {
        const meta = (name, property=false) => {
            const selector = property ? `meta[property="${name}"]` : `meta[name="${name}"]`;
            const node = document.querySelector(selector);
            return node ? (node.content || '') : '';
        };
        const h1 = document.querySelector('h1');
        const video = document.querySelector('video');
        return {
            title: meta('og:title', true) || (h1 && h1.innerText) || document.title || '',
            description: meta('description') || meta('og:description', true) || '',
            cover: meta('og:image', true) || '',
            images: [],
            author: '',
            video: video ? (video.currentSrc || video.src || '') : '',
            body: document.body ? document.body.innerText.slice(0, 12000) : '',
        };
        }"""

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
        page, owned_page = await self._page(canonical_url, timeout_seconds)
        try:
            detail_selector = self._detail_selector(canonical_url)
            if detail_selector:
                try:
                    await page.wait_for_selector(
                        detail_selector,
                        state="visible",
                        timeout=max(1000, int(timeout_seconds * 1000)),
                    )
                except Exception as exc:
                    raise RuntimeError("detail_content_unavailable") from exc
            metadata = await page.evaluate(self._detail_script(canonical_url))
            discussions: list[dict[str, Any]] = []
            seen_discussions: set[tuple[str, str]] = set()
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
                        discussion_key = (kind, cleaned)
                        if not cleaned or discussion_key in seen_discussions:
                            continue
                        seen_discussions.add(discussion_key)
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
            if self.spec.name == "bilibili" and "subtitles" in include:
                subtitle_rows = await read_bilibili_ai_subtitles(canonical_url)
                for index, row in enumerate(subtitle_rows):
                    cleaned = clean_text(row.get("text"), 600)
                    if not cleaned:
                        continue
                    discussions.append(
                        {
                            "discussion_id": stable_fingerprint(canonical_url, "subtitle", index, cleaned)[:32],
                            "type": "subtitle",
                            "text": cleaned,
                            "author": {"display_name": "Bilibili AI 字幕", "fingerprint": ""},
                            "published_at": 0,
                            "offset_seconds": float(row.get("offset_seconds") or 0.0),
                            "stats": {},
                        }
                    )
        finally:
            if owned_page:
                await page.close()
        title = clean_text(metadata.get("title"), 300)
        description = clean_text(metadata.get("description"), 4000)
        if not description:
            description = clean_text(metadata.get("body"), 4000)
        if self.spec.name in {"douyin", "tieba", "xiaoheihe"} and not description:
            raise RuntimeError("detail_content_unavailable")
        resolved_content_id = self.content_id(canonical_url)
        if not resolved_content_id:
            raise ValueError("content URL is not a supported content route")
        image_urls = list(
            dict.fromkeys(
                normalize_url(item.get("url"))
                for item in list(metadata.get("images") or [])
                if isinstance(item, dict) and normalize_url(item.get("url"))
            )
        )[:12]
        return {
            "platform": self.spec.name,
            "content_type": self._content_type_for_url(canonical_url),
            "content_id": resolved_content_id,
            "canonical_url": canonical_url,
            "title": title,
            "caption_or_body": description,
            "cover_ref": normalize_url(metadata.get("cover")),
            "image_urls": image_urls,
            "image_count": len(image_urls),
            "video_ref": normalize_platform_video_url(self.spec.name, metadata.get("video")),
            "author": {"display_name": clean_text(metadata.get("author"), 120), "fingerprint": ""},
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


__all__ = [
    "PlatformAdapter",
    "PlatformSpec",
    "SPECS",
    "build_adapters",
    "normalize_platform_video_url",
    "read_bilibili_ai_subtitles",
]
