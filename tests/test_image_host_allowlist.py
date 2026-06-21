from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

from ._loader import load_personification_module


pipeline_sticker = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.pipeline_sticker"
)


def _silent_logger():
    return SimpleNamespace(warning=lambda *_a, **_k: None, info=lambda *_a, **_k: None)


def _fake_getaddrinfo(host_to_ip: dict[str, str]):
    def _resolver(host, *_args, **_kwargs):
        ip = host_to_ip.get(host)
        if not ip:
            raise socket.gaierror(11001, "no such host")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 0, "", (ip, 443))]

    return _resolver


def _check(url: str) -> bool:
    return asyncio.run(pipeline_sticker._is_safe_remote_image_url(url, _silent_logger()))


def test_qq_image_host_with_reserved_ip_is_allowed(monkeypatch) -> None:
    # 198.18.0.0/15 是 RFC2544 测试网，ipaddress.is_reserved == True；
    # 但 QQ 系域名应跳过 IP 检查，被允许。
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"multimedia.nt.qq.com.cn": "198.18.0.78"}),
    )
    pipeline_sticker.set_image_host_allowlist([])
    assert _check("https://multimedia.nt.qq.com.cn/img/abc.jpg") is True


def test_qq_subdomain_allowlist_match(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"gchat.qpic.cn": "198.18.0.78"}),
    )
    pipeline_sticker.set_image_host_allowlist([])
    assert _check("https://gchat.qpic.cn/some/path") is True


def test_untrusted_host_with_reserved_ip_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"evil.example.com": "198.18.0.78"}),
    )
    pipeline_sticker.set_image_host_allowlist([])
    assert _check("https://evil.example.com/img.jpg") is False


def test_localhost_always_rejected_even_in_allowlist() -> None:
    pipeline_sticker.set_image_host_allowlist(["localhost"])
    try:
        # _BLOCKED_IMAGE_HOSTS 在 trusted 检查前就会命中
        assert _check("http://localhost/a") is False
    finally:
        pipeline_sticker.set_image_host_allowlist([])


def test_user_allowlist_appends_to_trusted_set(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"img.mycompany.cn": "198.18.0.78"}),
    )
    pipeline_sticker.set_image_host_allowlist([])
    assert _check("https://img.mycompany.cn/x.png") is False
    pipeline_sticker.set_image_host_allowlist([".mycompany.cn"])
    try:
        assert _check("https://img.mycompany.cn/x.png") is True
    finally:
        pipeline_sticker.set_image_host_allowlist([])


def test_set_image_host_allowlist_normalizes_input() -> None:
    pipeline_sticker.set_image_host_allowlist("foo.cn, bar.com ,.baz.cn ,")
    try:
        # 入参既支持 list 也支持逗号分隔；自动补前导 "."
        assert pipeline_sticker._is_image_host_trusted("a.foo.cn")
        assert pipeline_sticker._is_image_host_trusted("x.y.bar.com")
        assert pipeline_sticker._is_image_host_trusted("anything.baz.cn")
        assert not pipeline_sticker._is_image_host_trusted("foo.cn-evil.com")
    finally:
        pipeline_sticker.set_image_host_allowlist([])


def test_legit_public_host_still_passes(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"i.imgur.com": "151.101.1.193"}),
    )
    pipeline_sticker.set_image_host_allowlist([])
    assert _check("https://i.imgur.com/abc.jpg") is True
