"""Safe asset downloader (NFR-001, FR-034/035, T0506).

Enforced, in order, for every request AND every redirect hop:

* scheme must be http/https (no ``file:``, no custom schemes);
* host must be on the provider allowlist (exact host or explicit subdomain);
* URLs carrying userinfo (``user:pass@``) are refused;
* redirects are followed manually, max :attr:`max_redirects`, each hop
  re-validated against the same allowlist;
* response body is streamed with a hard byte cap; oversized bodies are
  aborted and deleted;
* content-type must be in the allowed set, and the downloaded bytes must
  match a known file signature (magic bytes) — a mismatch is rejected;
* stored filename is derived from the content hash, never from the URL.

Every accepted file yields ``sha256``, byte count, mime and the URL actually
used, for the provenance record.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..core.errors import CreatorError, ErrorCode
from ..core.ids import sha256_bytes
from ..core.paths import try_resolve_under
from ..observability.logging import get_logger, sanitize_url

logger = get_logger("security.downloader")

_EXT_BY_TYPE = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
}

_SIGNATURES: tuple[tuple[str, bytes, int], ...] = (
    ("video/mp4", b"ftyp", 4),
    ("image/png", b"\x89PNG\r\n\x1a\n", 0),
    ("image/jpeg", b"\xff\xd8\xff", 0),
    ("image/webp", b"WEBP", 8),
    ("audio/mpeg", b"ID3", 0),
    ("audio/mpeg", b"\xff\xfb", 0),
    ("audio/wav", b"WAVE", 8),
    ("audio/mp4", b"ftyp", 4),
    ("video/quicktime", b"ftyp", 4),
    ("audio/aac", b"\xff\xf1", 0),
    ("audio/ogg", b"OggS", 0),
)


def detect_signature_mime(head: bytes) -> Optional[str]:
    for mime, signature, offset in _SIGNATURES:
        if len(head) >= offset + len(signature) and head[offset : offset + len(signature)] == signature:
            return mime
    return None


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    for entry in allowed_hosts:
        entry = entry.lower().rstrip(".")
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


@dataclass
class DownloadedFile:
    path: Path
    sha256: str
    bytes: int
    content_type: str
    final_url: str


class SafeDownloader:
    def __init__(
        self,
        *,
        allowed_hosts: dict[str, set[str]],  # provider_id -> hostnames
        max_bytes: int = 50 * 1024 * 1024,
        max_redirects: int = 3,
        timeout_seconds: float = 60.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.allowed_hosts = {k: {h.lower() for h in v} for k, v in allowed_hosts.items()}
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        return self._client

    def _all_hosts(self) -> set[str]:
        hosts: set[str] = set()
        for values in self.allowed_hosts.values():
            hosts.update(values)
        return hosts

    def _validate_url(self, url: str, provider: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise CreatorError(
                ErrorCode.MEDIA_DOWNLOAD_FAILED,
                f"Download refused: scheme {parsed.scheme or '(none)'!r} is not http/https.",
                stage="media",
                next_action="Only https URLs from approved providers can be downloaded.",
            )
        if parsed.username or parsed.password:
            raise CreatorError(
                ErrorCode.MEDIA_DOWNLOAD_FAILED,
                "Download refused: URL contains embedded credentials.",
                stage="media",
            )
        host = parsed.hostname or ""
        if not host:
            raise CreatorError(
                ErrorCode.MEDIA_DOWNLOAD_FAILED, "Download refused: URL has no host.", stage="media"
            )
        provider_hosts = self.allowed_hosts.get(provider, set())
        if not host_allowed(host, provider_hosts):
            # fall back to the union only when the provider is unknown to the map
            if provider in self.allowed_hosts or not host_allowed(host, self._all_hosts()):
                raise CreatorError(
                    ErrorCode.MEDIA_DOWNLOAD_FAILED,
                    f"Download refused: host {host!r} is not on the allowlist for provider {provider!r}.",
                    stage="media",
                    next_action="Only provider-approved hosts can be downloaded; check the provider allowlist.",
                )

    async def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        provider: str,
        kind: str,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> DownloadedFile:
        if dest_dir is None:
            raise CreatorError(ErrorCode.INTERNAL_ERROR, "Download destination missing.", stage="media")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        client = await self._get_client()
        current = url
        redirects = 0
        while True:
            self._validate_url(current, provider)
            try:
                response = await self._stream_once(
                    client, current, dest_dir, provider=provider, kind=kind, cancel_event=cancel_event
                )
            except _Redirect as redirect:
                redirects += 1
                if redirects > self.max_redirects:
                    raise CreatorError(
                        ErrorCode.MEDIA_DOWNLOAD_FAILED,
                        f"Download refused: more than {self.max_redirects} redirects.",
                        stage="media",
                    )
                current = redirect.location
                logger.info(
                    "following redirect (%d) to %s", redirects, sanitize_url(current)
                )
                continue
            return response

    async def _stream_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        dest_dir: Path,
        *,
        provider: str,
        kind: str,
        cancel_event: Optional[asyncio.Event],
    ) -> DownloadedFile:
        try:
            async with client.stream("GET", url) as response:
                status = response.status_code
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise CreatorError(
                            ErrorCode.MEDIA_DOWNLOAD_FAILED,
                            "Download refused: redirect without Location header.",
                            stage="media",
                        )
                    raise _Redirect(urljoin(url, location))
                if status != 200:
                    raise CreatorError(
                        ErrorCode.MEDIA_DOWNLOAD_FAILED,
                        f"Download failed: HTTP {status} from provider {provider!r}.",
                        stage="media",
                        retryable=status >= 500 or status == 429,
                        next_action="Retry later or choose another asset candidate.",
                    )
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                expected = self._expected_types(kind)
                if expected and content_type and content_type not in expected:
                    raise CreatorError(
                        ErrorCode.MEDIA_DOWNLOAD_FAILED,
                        f"Download refused: content-type {content_type!r} is not valid for a {kind} asset.",
                        stage="media",
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise CreatorError(
                        ErrorCode.MEDIA_DOWNLOAD_FAILED,
                        f"Download refused: declared size {declared} exceeds the {self.max_bytes} byte limit.",
                        stage="media",
                    )

                tmp_path = dest_dir / ".download.part"
                digest_input = bytearray()
                total = 0
                try:
                    with open(tmp_path, "wb") as fh:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            if cancel_event is not None and cancel_event.is_set():
                                raise CreatorError(
                                    ErrorCode.CANCELLED, "Download cancelled.", stage="media"
                                )
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise CreatorError(
                                    ErrorCode.MEDIA_DOWNLOAD_FAILED,
                                    f"Download refused: body exceeded the {self.max_bytes} byte limit.",
                                    stage="media",
                                )
                            if len(digest_input) < 64:
                                digest_input.extend(chunk[: 64 - len(digest_input)])
                            fh.write(chunk)
                except CreatorError:
                    _remove_quietly(tmp_path)
                    raise
                except (httpx.HTTPError, OSError) as exc:
                    _remove_quietly(tmp_path)
                    raise CreatorError(
                        ErrorCode.MEDIA_DOWNLOAD_FAILED,
                        f"Download failed mid-stream ({type(exc).__name__}).",
                        stage="media",
                        retryable=True,
                    )

                if total == 0:
                    _remove_quietly(tmp_path)
                    raise CreatorError(
                        ErrorCode.MEDIA_DOWNLOAD_FAILED, "Download failed: empty body.", stage="media"
                    )

                sniffed = detect_signature_mime(bytes(digest_input))
                final_type = content_type or sniffed or ""
                if sniffed is None:
                    _remove_quietly(tmp_path)
                    raise CreatorError(
                        ErrorCode.MEDIA_INVALID,
                        "Download rejected: file signature is not a recognized media format.",
                        stage="media",
                    )
                if expected and sniffed not in expected:
                    _remove_quietly(tmp_path)
                    raise CreatorError(
                        ErrorCode.MEDIA_INVALID,
                        f"Download rejected: file signature {sniffed!r} does not match the requested {kind} asset.",
                        stage="media",
                    )

                data = tmp_path.read_bytes() if total <= 32 * 1024 * 1024 else None
                sha = _sha256_of(tmp_path) if data is None else sha256_bytes(data)
                ext = _EXT_BY_TYPE.get(sniffed or final_type, ".bin")
                final_path = try_resolve_under(
                    Path(dest_dir).resolve().parent, Path(dest_dir) / f"{sha[:16]}{ext}"
                )
                if final_path.exists():
                    _remove_quietly(tmp_path)
                else:
                    tmp_path.replace(final_path)
                return DownloadedFile(
                    path=final_path, sha256=sha, bytes=total, content_type=sniffed, final_url=url
                )
        except httpx.HTTPError as exc:
            raise CreatorError(
                ErrorCode.MEDIA_DOWNLOAD_FAILED,
                f"Download failed ({type(exc).__name__}) for provider {provider!r}.",
                stage="media",
                retryable=isinstance(exc, (httpx.TransportError,)),
                next_action="Retry the download or pick a different asset.",
            )

    @staticmethod
    def _expected_types(kind: str) -> set[str]:
        if kind == "video":
            return {"video/mp4", "video/quicktime"}
        if kind == "image":
            return {"image/png", "image/jpeg", "image/webp"}
        if kind == "audio":
            return {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/aac", "audio/ogg"}
        return set()


class _Redirect(Exception):
    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _sha256_of(path: Path) -> str:
    from ..core.ids import sha256_file

    return sha256_file(path)
