from __future__ import annotations

import asyncio
import io

import pytest

from ._loader import load_personification_module


avatar = load_personification_module("plugin.personification.core.avatar_candidate")
relevance = load_personification_module("plugin.personification.core.avatar_relevance")
safe_download = load_personification_module("plugin.personification.core.safe_image_download")


def _image(seed: int, size: tuple[int, int] = (256, 256)) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    output = io.BytesIO()
    image = Image.new("RGB", size, (20 + seed * 7, 30, 220 - seed * 9))
    draw = ImageDraw.Draw(image)
    draw.rectangle((seed * 11, 20, seed * 11 + 55, 235), fill=(240, 40 + seed * 12, 30))
    draw.line((0, seed * 19, 255, 255 - seed * 13), fill=(255, 255, 255), width=9)
    image.save(output, format="PNG")
    return output.getvalue()


def _public_resolver(*_args, **_kwargs):
    async def resolve():
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    return resolve()


def test_avatar_url_rejects_private_and_redirect_target() -> None:
    with pytest.raises(avatar.AvatarCandidateError):
        asyncio.run(avatar.validate_remote_image_url("http://127.0.0.1/avatar.png"))
    with pytest.raises(avatar.AvatarCandidateError):
        asyncio.run(avatar.validate_remote_image_url("http://169.254.169.254/latest/meta-data"))


def test_avatar_url_rejects_hostname_with_any_private_resolution() -> None:
    async def mixed_resolver(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

    with pytest.raises(avatar.AvatarCandidateError):
        asyncio.run(avatar.validate_remote_image_url("https://example.test/avatar.png", resolver=mixed_resolver))


def test_pinned_download_uses_ip_host_and_https_sni() -> None:
    httpx = pytest.importorskip("httpx")
    requests = []
    resolutions = []

    async def resolver(host, *_args, **_kwargs):  # noqa: ANN001
        resolutions.append(host)
        address = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
        return [(2, 1, 6, "", (address, 443))]

    async def handler(request):  # noqa: ANN001
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"payload")

    def client_factory(**kwargs):  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    result = asyncio.run(safe_download.download_public_image(
        "https://images.example.test/avatar.png?x=1",
        max_bytes=100,
        allowed_mimes={"image/png"},
        resolver=resolver,
        client_factory=client_factory,
    ))

    assert result.final_url == "https://images.example.test/avatar.png?x=1"
    assert resolutions == ["images.example.test"]
    assert str(requests[0].url) == "https://93.184.216.34/avatar.png?x=1"
    assert requests[0].headers["host"] == "images.example.test"
    assert requests[0].extensions["sni_hostname"] == "images.example.test"


def test_pinned_redirect_resolves_each_original_host_and_keeps_relative_base() -> None:
    httpx = pytest.importorskip("httpx")
    requests = []
    resolved = []
    approved = {
        "first.example.test": "93.184.216.34",
        "cdn.example.test": "93.184.216.35",
    }

    async def resolver(host, *_args, **_kwargs):  # noqa: ANN001
        resolved.append(host)
        return [(2, 1, 6, "", (approved[host], 443))]

    async def handler(request):  # noqa: ANN001
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.example.test/assets/final.png"})
        if len(requests) == 2:
            return httpx.Response(302, headers={"location": "../avatar.png?size=large"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"payload")

    def client_factory(**kwargs):  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    result = asyncio.run(safe_download.download_public_image(
        "https://first.example.test/start/image.png",
        headers={"Authorization": "Bearer secret"},
        sensitive_headers_origin="https://first.example.test/v1/images",
        max_bytes=100,
        allowed_mimes={"image/png"},
        resolver=resolver,
        client_factory=client_factory,
    ))

    assert resolved == ["first.example.test", "cdn.example.test", "cdn.example.test"]
    assert [request.url.host for request in requests] == ["93.184.216.34", "93.184.216.35", "93.184.216.35"]
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert "authorization" not in requests[1].headers
    assert str(requests[2].url) == "https://93.184.216.35/avatar.png?size=large"
    assert requests[2].extensions["sni_hostname"] == "cdn.example.test"
    assert result.final_url == "https://cdn.example.test/avatar.png?size=large"


def test_pinned_download_rejects_proxy_and_formats_ipv6() -> None:
    with pytest.raises(safe_download.SafeImageDownloadError, match="proxies"):
        asyncio.run(safe_download.download_public_image(
            "https://example.test/image.png",
            max_bytes=100,
            allowed_mimes={"image/png"},
            proxy="http://proxy.test:8080",
        ))

    connection_url, host, sni = safe_download._pinned_request_url(
        "https://example.test/image.png", "2001:4860:4860::8888"
    )
    assert connection_url == "https://[2001:4860:4860::8888]/image.png"
    assert host == "example.test"
    assert sni == "example.test"


def test_avatar_candidates_dedupe_and_persist_ten(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(avatar, "get_data_dir", lambda _config=None: tmp_path)
    sources = [
        {"source": "mock", "url": f"https://example.test/page/{index}", "image_url": f"https://example.test/{index}.png"}
        for index in range(12)
    ]

    async def fetcher(url: str):
        index = int(url.rsplit("/", 1)[-1].split(".", 1)[0])
        # Last two are exact duplicates of earlier files.
        color_index = index if index < 10 else index - 10
        return _image(color_index), "image/png", url

    candidates = asyncio.run(
        avatar.build_avatar_candidates(
            sources,
            revision="a" * 32,
            fetcher=fetcher,
            resolver=_public_resolver,
        )
    )

    assert len(candidates) == 10
    assert len({item["sha256"] for item in candidates}) == 10
    assert all(item["safety_status"] == "pass" for item in candidates)
    assert all(item["fit_score"] == 0 for item in candidates)
    assert all(item["aspect_score"] > 0 for item in candidates)
    assert all(avatar.candidate_file(item).is_file() for item in candidates)


def test_avatar_candidate_diagnostics_classify_non_image_responses(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(avatar, "get_data_dir", lambda _config=None: tmp_path)
    diagnostics = {}

    async def fetcher(url: str):
        return b"<html>not an image</html>", "text/html", url

    candidates = asyncio.run(avatar.build_avatar_candidates(
        [{"image_url": "https://example.test/not-image", "source": "mock"}],
        revision="d" * 32,
        fetcher=fetcher,
        resolver=_public_resolver,
        diagnostics=diagnostics,
    ))

    assert candidates == []
    assert diagnostics["extracted_url_count"] == 1
    assert diagnostics["failure_counts"]["not_an_image"] == 1


def test_avatar_candidate_extraction_skips_malformed_ipv6_urls() -> None:
    sources = [{
        "image_url": "https://[broken/image.png",
        "summary": "可用图片 https://example.test/avatar.png",
    }]

    extracted = avatar.extract_image_urls(sources)

    assert [item["url"] for item in extracted] == ["https://example.test/avatar.png"]


def _review_payload(*, match: str, confidence: float = 0.95, quality: float = 0.8) -> str:
    return __import__("json").dumps({
        "target_match": match,
        "recognized_identity": "目标角色" if match == "yes" else "其他角色",
        "character_confidence": confidence,
        "portrait_quality": quality,
        "single_subject": True,
        "is_cosplay_or_real_person": False,
        "is_logo_cover_or_ui": False,
        "content_safe": True,
        "contradictions": [],
        "reason": "视觉特征一致" if match == "yes" else "角色不一致",
    }, ensure_ascii=False)


def test_visual_review_filters_safe_but_wrong_characters(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(avatar, "get_data_dir", lambda _config=None: tmp_path)
    sources = [
        {
            "source": "official" if index >= 4 else "image_search",
            "title": f"测试作品 目标角色 {index}" if index >= 4 else f"其他角色 {index}",
            "image_url": f"https://example.test/{index}.png",
        }
        for index in range(14)
    ]

    async def fetcher(url: str):
        index = int(url.rsplit("/", 1)[-1].split(".", 1)[0])
        size = (256, 256) if index < 4 else (240, 300)
        return _image(index, size), "image/png", url

    candidates = asyncio.run(avatar.build_avatar_candidates(
        sources,
        revision="c" * 32,
        fetcher=fetcher,
        resolver=_public_resolver,
    ))

    async def reviewer(_prompt: str, image_ref: str):
        payload = __import__("base64").b64decode(image_ref.split(",", 1)[1])
        Image = pytest.importorskip("PIL.Image")
        with Image.open(io.BytesIO(payload)) as image:
            is_wrong_square = image.size[0] == image.size[1]
        return _review_payload(match="no" if is_wrong_square else "yes"), "mock"

    reviewed, summary = asyncio.run(relevance.review_avatar_candidates(
        runtime=object(),
        candidates=candidates,
        work_title="测试作品",
        character_name="目标角色",
        aliases={"work_aliases": ["测试作品"], "character_aliases": ["目标角色"]},
        candidate_path=lambda item: avatar.candidate_file(item),
        reviewer=reviewer,
    ))

    assert summary["verified_count"] == 10
    assert all(item["vision_status"] == "verified" for item in reviewed[:10])
    assert all(item["aspect_score"] < 1 for item in reviewed[:10])
    assert all(item["vision_status"] == "rejected" for item in reviewed[10:])


def test_visual_review_hard_filters_wrong_candidates_without_pillow(tmp_path) -> None:
    candidates = []
    paths = {}
    for index in range(14):
        candidate_id = f"{index:032x}"
        path = tmp_path / f"{candidate_id}.jpg"
        path.write_bytes((b"wrong-" if index < 4 else b"target-") + str(index).encode())
        paths[candidate_id] = path
        candidates.append({
            "candidate_id": candidate_id,
            "source": "official" if index >= 4 else "image_search",
            "title": f"测试作品 目标角色 {index}" if index >= 4 else f"其他角色 {index}",
            "page_url": "https://example.test/page",
            "mime": "image/jpeg",
            "aspect_score": 1.0 if index < 4 else 0.75,
            "safety_status": "pass",
        })

    async def reviewer(_prompt: str, image_ref: str):
        payload = __import__("base64").b64decode(image_ref.split(",", 1)[1])
        return _review_payload(match="no" if payload.startswith(b"wrong-") else "yes"), "mock"

    reviewed, summary = asyncio.run(relevance.review_avatar_candidates(
        runtime=object(),
        candidates=candidates,
        work_title="测试作品",
        character_name="目标角色",
        aliases={"work_aliases": ["测试作品"], "character_aliases": ["目标角色"]},
        candidate_path=lambda item: paths[item["candidate_id"]],
        reviewer=reviewer,
    ))

    assert summary["verified_count"] == 10
    assert [item["vision_status"] for item in reviewed[:10]] == ["verified"] * 10
    assert all(item["fit_score"] > 0 for item in reviewed[:10])
    assert all(item["vision_status"] == "rejected" for item in reviewed[10:])


def test_visual_review_stops_at_target_and_marks_total_errors_unavailable(tmp_path, monkeypatch) -> None:
    candidates = []
    paths = {}
    for index in range(20):
        candidate_id = f"{index:032x}"
        path = tmp_path / f"{candidate_id}.jpg"
        path.write_bytes(f"candidate-{index}".encode())
        paths[candidate_id] = path
        candidates.append({
            "candidate_id": candidate_id,
            "source": "official",
            "title": f"测试作品 目标角色 {index}",
            "page_url": "https://example.test/character",
            "mime": "image/jpeg",
            "aspect_score": 0.8,
            "safety_status": "pass",
        })
    calls = 0

    async def verified_reviewer(_prompt: str, _image_ref: str):
        nonlocal calls
        calls += 1
        return _review_payload(match="yes"), "mock"

    _reviewed, summary = asyncio.run(relevance.review_avatar_candidates(
        runtime=object(),
        candidates=candidates,
        work_title="测试作品",
        character_name="目标角色",
        aliases={"work_aliases": ["测试作品"], "character_aliases": ["目标角色"]},
        candidate_path=lambda item: paths[item["candidate_id"]],
        reviewer=verified_reviewer,
        target_verified=10,
    ))
    assert calls == 10
    assert summary["verified_count"] == 10

    monkeypatch.setattr(relevance, "VISUAL_REVIEW_TIMEOUT_SECONDS", 0.001)

    async def hanging_reviewer(_prompt: str, _image_ref: str):
        await asyncio.sleep(0.05)
        return _review_payload(match="yes"), "mock"

    _reviewed, failed_summary = asyncio.run(relevance.review_avatar_candidates(
        runtime=object(),
        candidates=candidates[:2],
        work_title="测试作品",
        character_name="目标角色",
        aliases={"work_aliases": ["测试作品"], "character_aliases": ["目标角色"]},
        candidate_path=lambda item: paths[item["candidate_id"]],
        reviewer=hanging_reviewer,
        target_verified=10,
    ))
    assert failed_summary["status_counts"]["error"] == 2
    assert failed_summary["vision_available"] is False


@pytest.mark.parametrize("raw,route,status", [
    ("", "vision_unavailable", "unavailable"),
    ("not-json", "mock", "invalid_response"),
    (_review_payload(match="uncertain", confidence=0.6), "mock", "uncertain"),
    (_review_payload(match="yes", confidence=0.7), "mock", "rejected"),
])
def test_visual_review_is_fail_closed(raw: str, route: str, status: str) -> None:
    assert relevance.normalize_avatar_visual_review(raw, route=route)["vision_status"] == status


def test_avatar_text_score_does_not_treat_query_as_evidence() -> None:
    score = relevance.avatar_text_relevance(
        {"title": "无标题图片", "query": "测试作品 目标角色 官方头像", "page_url": "https://cdn.test/a"},
        work_aliases=["测试作品"],
        character_aliases=["目标角色"],
    )
    assert score <= 0.1


def test_history_summary_counts_only_verified_avatars() -> None:
    history = load_personification_module("plugin.personification.core.persona_template_history")
    summary = history.summarize_persona_template_record({
        "result": {
            "avatar_candidates": [
                {"candidate_id": "1", "vision_status": "verified"},
                {"candidate_id": "2", "vision_status": "unavailable"},
            ],
            "avatar_review_summary": {"verified_count": 1, "reviewed_count": 2},
        }
    })
    assert summary["avatar_candidate_count"] == 1
    assert summary["verified_avatar_count"] == 1
    assert summary["avatar_reviewed_count"] == 2


def test_candidate_file_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    if not hasattr(__import__("os"), "symlink"):
        pytest.skip("symlinks unsupported")
    monkeypatch.setattr(avatar, "get_data_dir", lambda _config=None: tmp_path)
    revision = "a" * 32
    candidate_id = "b" * 32
    root = tmp_path / "persona_avatar_candidates" / revision
    root.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(_image(1))
    link = root / f"{candidate_id}.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(avatar.AvatarCandidateError):
        avatar.candidate_file({"revision": revision, "candidate_id": candidate_id, "suffix": ".png"})


def test_signature_contract_rejects_leaks_and_urls() -> None:
    routes = load_personification_module("plugin.personification.webui.routes.persona_template_routes")
    assert routes._validate_signature_text("今晚也慢慢来")[1] == []
    assert "url" in routes._validate_signature_text("看这里 https://example.test")[1]
    assert "ai_identity_leak" in routes._validate_signature_text("我是 AI 助手")[1]
    assert "control_character" in routes._validate_signature_text("正常\x00文本")[1]


def test_signature_generation_returns_structured_pass_candidates() -> None:
    routes = load_personification_module("plugin.personification.webui.routes.persona_template_routes")

    async def caller(_messages, **_kwargs):
        return '{"candidates":[' \
            '{"text":"今晚也慢慢来","rationale":"语气贴合","fit_score":0.9},' \
            '{"text":"风吹过就出发","rationale":"简短自然","fit_score":0.8},' \
            '{"text":"把今天收进星光里","rationale":"形象锚点","fit_score":0.7}' \
            ']}'

    candidates = asyncio.run(routes._generate_signature_candidates(
        caller=caller,
        work_title="测试作品",
        character_name="测试角色",
        source_text="可靠资料",
    ))

    assert len(candidates) == 3
    assert all(item["candidate_id"] for item in candidates)
    assert any(item["safety_status"] == "pass" for item in candidates)
