"""Security tests: license default-deny policy and the safe downloader.

The downloader is driven through ``httpx.MockTransport`` — no real network
request is ever made; every refusal path is asserted against the mocked
transport, including cross-host redirects, size caps, and signature checks.
"""

from __future__ import annotations

import httpx
import pytest

from facebook_creator_agent.core.errors import CreatorError
from facebook_creator_agent.core.ids import sha256_bytes
from facebook_creator_agent.providers.media.base import AssetCandidate
from facebook_creator_agent.providers.media.policy import LicensePolicy
from facebook_creator_agent.security.downloader import SafeDownloader

# ------------------------------------------------------------------ license policy

ALLOWED_PROVIDERS = {"fixture", "pexels"}


def _policy(**overrides) -> LicensePolicy:
    overrides.setdefault("allowed_providers", set(ALLOWED_PROVIDERS))
    return LicensePolicy(**overrides)


def test_cc0_candidate_approved():
    result = _policy().evaluate(AssetCandidate(provider="fixture", license_name="CC0-1.0"))
    assert result.rights_status == "APPROVED"


def test_noncommercial_license_rejected():
    result = _policy().evaluate(AssetCandidate(provider="pexels", license_name="CC-BY-NC-4.0"))
    assert result.rights_status == "REJECTED"


def test_noderivatives_license_rejected():
    result = _policy().evaluate(AssetCandidate(provider="pexels", license_name="CC-BY-ND-4.0"))
    assert result.rights_status == "REJECTED"


def test_all_rights_reserved_rejected():
    result = _policy().evaluate(
        AssetCandidate(provider="pexels", license_name="All rights reserved")
    )
    assert result.rights_status == "REJECTED"


def test_unknown_license_requires_review():
    result = _policy().evaluate(AssetCandidate(provider="pexels", license_name="MIT"))
    assert result.rights_status == "REVIEW_REQUIRED"


def test_missing_license_requires_review():
    result = _policy().evaluate(AssetCandidate(provider="pexels", license_name=None))
    assert result.rights_status == "REVIEW_REQUIRED"


def test_unlisted_provider_rejected_even_with_good_license():
    result = _policy().evaluate(AssetCandidate(provider="evilco", license_name="CC0-1.0"))
    assert result.rights_status == "REJECTED"
    assert "allowlist" in result.reasons[0]


def test_cc_by_requires_attribution_text():
    without = _policy().evaluate(AssetCandidate(provider="pexels", license_name="CC-BY-4.0"))
    assert without.rights_status == "REVIEW_REQUIRED"

    with_attr = _policy().evaluate(
        AssetCandidate(provider="pexels", license_name="CC-BY-4.0", attribution="Photo by Someone")
    )
    assert with_attr.rights_status == "APPROVED"


def test_exact_allowlist_match_wins_over_heuristics():
    # Case-insensitive exact match must approve even before marker heuristics run.
    result = _policy(allowed_licenses={"CC0-1.0"}).evaluate(
        AssetCandidate(provider="fixture", license_name="cc0-1.0")
    )
    assert result.rights_status == "APPROVED"


# --------------------------------------------------------------------- downloader

ALLOWED_HOSTS = {"pexels": {"videos.pexels.com"}}
VIDEO_URL = "https://videos.pexels.com/video/123.mp4"
MP4_BYTES = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 256


def _make_downloader(handler, *, max_bytes=4096, max_redirects=2):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return SafeDownloader(
        allowed_hosts=ALLOWED_HOSTS,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        client=client,
    )


async def test_disallowed_host_refused_without_any_request(tmp_path):
    calls: list = []

    def handler(request):  # pragma: no cover - must never run
        calls.append(request.url)
        return httpx.Response(200, content=MP4_BYTES)

    downloader = _make_downloader(handler)
    dest = tmp_path / "dl"
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(
            "https://evil.example.com/x.mp4", dest, provider="pexels", kind="video"
        )
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert "allowlist" in excinfo.value.safe_explanation
    assert calls == []
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///c:/windows/system32/calc.exe",
        "ftp://videos.pexels.com/x.mp4",
        "https://user:pass@videos.pexels.com/x.mp4",
    ],
)
async def test_non_http_and_userinfo_urls_refused(tmp_path, url):
    downloader = _make_downloader(lambda request: httpx.Response(200, content=MP4_BYTES))
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(url, tmp_path / "dl", provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"


async def test_redirect_to_disallowed_host_refused(tmp_path):
    def handler(request):
        return httpx.Response(
            302, headers={"location": "https://evil.example.com/x.mp4"}, content=b""
        )

    downloader = _make_downloader(handler)
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(VIDEO_URL, tmp_path / "dl", provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert "allowlist" in excinfo.value.safe_explanation


async def test_redirect_loop_hits_limit(tmp_path):
    def handler(request):
        return httpx.Response(302, headers={"location": VIDEO_URL}, content=b"")

    downloader = _make_downloader(handler, max_redirects=1)
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(VIDEO_URL, tmp_path / "dl", provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert "redirect" in excinfo.value.safe_explanation.lower()


async def test_declared_content_length_over_limit_refused(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4", "content-length": "999999999"},
            content=MP4_BYTES,
        )

    downloader = _make_downloader(handler, max_bytes=1024)
    dest = tmp_path / "dl"
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(VIDEO_URL, dest, provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert "exceeds" in excinfo.value.safe_explanation
    assert list(dest.iterdir()) == []


async def test_streamed_body_over_limit_aborted_and_cleaned(tmp_path):
    async def chunks():
        for _ in range(50):
            yield b"\x00" * 1024

    def handler(request):
        return httpx.Response(200, content=chunks())

    downloader = _make_downloader(handler, max_bytes=4096)
    dest = tmp_path / "dl"
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(VIDEO_URL, dest, provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert list(dest.iterdir()) == []  # no partial file left behind


async def test_wrong_content_type_refused(tmp_path):
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=MP4_BYTES
        )

    downloader = _make_downloader(handler)
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(VIDEO_URL, tmp_path / "dl", provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert "content-type" in excinfo.value.safe_explanation


async def test_signature_mismatch_rejected(tmp_path):
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, content=b"not-a-media-file" * 8
        )

    downloader = _make_downloader(handler)
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(VIDEO_URL, tmp_path / "dl", provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_INVALID"
    assert list((tmp_path / "dl").iterdir()) == []


async def test_empty_body_refused(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"")

    downloader = _make_downloader(handler)
    with pytest.raises(CreatorError) as excinfo:
        await downloader.download(VIDEO_URL, tmp_path / "dl", provider="pexels", kind="video")
    assert excinfo.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert "empty" in excinfo.value.safe_explanation.lower()


async def test_valid_download_persisted_with_hash(tmp_path):
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, content=MP4_BYTES
        )

    downloader = _make_downloader(handler, max_bytes=1024 * 1024)
    result = await downloader.download(VIDEO_URL, tmp_path / "dl", provider="pexels", kind="video")

    assert result.path.is_file()
    assert result.path.suffix == ".mp4"
    assert result.sha256 == sha256_bytes(MP4_BYTES)
    assert result.bytes == len(MP4_BYTES)
    assert result.content_type == "video/mp4"
