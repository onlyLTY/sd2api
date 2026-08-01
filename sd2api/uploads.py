from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError
from starlette.datastructures import UploadFile

from .config import Settings
from .tiktok import TikTokUpstreamError


MediaKind = Literal["image", "video", "audio"]


@dataclass(frozen=True, slots=True)
class StagedMedia:
    kind: MediaKind
    path: str


class UploadManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.sd2api_upload_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def save_upload(self, upload: UploadFile) -> str:
        return (await self.save_media_upload(upload, expected_kind="image")).path

    async def save_media_upload(
        self,
        upload: UploadFile,
        *,
        expected_kind: MediaKind | None = None,
    ) -> StagedMedia:
        kind = self._infer_kind(
            content_type=upload.content_type,
            name=upload.filename,
            expected_kind=expected_kind,
        )
        path = self.root / ("upload_" + uuid.uuid4().hex)
        limit = self._max_bytes(kind)
        total = 0
        try:
            with path.open("wb") as output:
                while chunk := await upload.read(256 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise TikTokUpstreamError(
                            f"Uploaded {kind} exceeds the configured {kind} size limit",
                            status_code=413,
                            code=f"{kind}_too_large",
                        )
                    output.write(chunk)
            validated = self._validate_and_rename(
                path,
                kind=kind,
                source_name=upload.filename or "",
                content_type=upload.content_type or "",
            )
            return StagedMedia(kind=kind, path=validated)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

    async def save_url(self, value: str) -> str:
        return (await self.save_media_url(value, kind="image")).path

    async def save_media_url(self, value: str, *, kind: MediaKind) -> StagedMedia:
        if value.startswith("data:"):
            if kind == "video":
                raise TikTokUpstreamError(
                    "Seedance reference videos must use an HTTP(S) URL or multipart upload",
                    status_code=422,
                    code="video_data_url_unsupported",
                )
            return self._save_data_url(value, kind=kind)

        current = value
        for _ in range(4):
            await self._validate_public_url(current, kind=kind)
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                async with client.stream(
                    "GET",
                    current,
                    headers={"accept": f"{kind}/*,*/*;q=0.1"},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise TikTokUpstreamError(
                                f"{kind.title()} URL redirected without Location"
                            )
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        raise TikTokUpstreamError(
                            f"{kind.title()} URL returned HTTP {response.status_code}",
                            status_code=422,
                            code=f"{kind}_download_failed",
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if (
                        content_type
                        and not content_type.startswith(f"{kind}/")
                        and not content_type.startswith("application/octet-stream")
                    ):
                        raise TikTokUpstreamError(
                            f"{kind.title()} URL returned incompatible content type {content_type!r}",
                            status_code=422,
                            code=f"invalid_{kind}_type",
                        )
                    path = self.root / ("url_" + uuid.uuid4().hex)
                    total = 0
                    limit = self._max_bytes(kind)
                    try:
                        with path.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                total += len(chunk)
                                if total > limit:
                                    raise TikTokUpstreamError(
                                        f"Remote {kind} exceeds the configured {kind} size limit",
                                        status_code=413,
                                        code=f"{kind}_too_large",
                                    )
                                output.write(chunk)
                        validated = self._validate_and_rename(
                            path,
                            kind=kind,
                            source_name=urlparse(current).path,
                            content_type=content_type,
                        )
                        return StagedMedia(kind=kind, path=validated)
                    except Exception:
                        path.unlink(missing_ok=True)
                        raise
        raise TikTokUpstreamError(
            f"{kind.title()} URL redirected too many times",
            status_code=422,
            code="too_many_redirects",
        )

    def cleanup(self, media: list[StagedMedia] | tuple[StagedMedia, ...]) -> None:
        for item in media:
            path = Path(item.path).resolve()
            if path.is_relative_to(self.root):
                path.unlink(missing_ok=True)

    def _save_data_url(self, value: str, *, kind: MediaKind) -> StagedMedia:
        header, separator, encoded = value.partition(",")
        expected_prefix = f"data:{kind}/"
        if (
            not separator
            or ";base64" not in header.lower()
            or not header.lower().startswith(expected_prefix)
        ):
            raise TikTokUpstreamError(
                f"Only base64 {kind} data URLs are supported for this field",
                status_code=422,
                code="invalid_data_url",
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TikTokUpstreamError(
                f"{kind.title()} data URL contains invalid base64",
                status_code=422,
                code="invalid_data_url",
            ) from exc
        if len(data) > self._max_bytes(kind):
            raise TikTokUpstreamError(
                f"{kind.title()} data URL exceeds the configured {kind} size limit",
                status_code=413,
                code=f"{kind}_too_large",
            )
        path = self.root / ("data_" + uuid.uuid4().hex)
        path.write_bytes(data)
        content_type = header[5:].split(";", 1)[0].lower()
        try:
            validated = self._validate_and_rename(
                path,
                kind=kind,
                source_name="",
                content_type=content_type,
            )
            return StagedMedia(kind=kind, path=validated)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    async def _validate_public_url(self, value: str, *, kind: MediaKind) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise TikTokUpstreamError(
                f"{kind}_url must be an HTTP(S) URL"
                + (" or base64 data URL" if kind != "video" else ""),
                status_code=422,
                code=f"invalid_{kind}_url",
            )
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise TikTokUpstreamError(
                f"{kind}_url hostname could not be resolved",
                status_code=422,
                code=f"{kind}_dns_error",
            ) from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise TikTokUpstreamError(
                    f"{kind}_url may not target a private or local network address",
                    status_code=422,
                    code=f"{kind}_url_blocked",
                )

    def _max_bytes(self, kind: MediaKind) -> int:
        per_kind = {
            "image": self.settings.sd2api_upload_image_max_bytes,
            "video": self.settings.sd2api_upload_video_max_bytes,
            "audio": self.settings.sd2api_upload_audio_max_bytes,
        }[kind]
        return min(self.settings.sd2api_upload_max_bytes, per_kind)

    @staticmethod
    def _infer_kind(
        *,
        content_type: str | None,
        name: str | None,
        expected_kind: MediaKind | None,
    ) -> MediaKind:
        content_type = (content_type or "").lower()
        suffix = Path(name or "").suffix.lower()
        inferred: MediaKind | None = None
        for candidate in ("image", "video", "audio"):
            if content_type.startswith(candidate + "/"):
                inferred = candidate  # type: ignore[assignment]
                break
        if inferred is None:
            if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}:
                inferred = "image"
            elif suffix in {".mp4", ".mov"}:
                inferred = "video"
            elif suffix in {".wav", ".mp3"}:
                inferred = "audio"
        if expected_kind and inferred and inferred != expected_kind:
            raise TikTokUpstreamError(
                f"Expected {expected_kind}, received {inferred}",
                status_code=422,
                code=f"invalid_{expected_kind}_type",
            )
        kind = expected_kind or inferred
        if kind is None:
            raise TikTokUpstreamError(
                "Could not determine whether the uploaded reference is an image, video, or audio file",
                status_code=422,
                code="unsupported_media_type",
            )
        return kind

    def _validate_and_rename(
        self,
        path: Path,
        *,
        kind: MediaKind,
        source_name: str,
        content_type: str,
    ) -> str:
        if kind == "image":
            return self._validate_image(path)
        with path.open("rb") as source:
            header = source.read(16)
        if kind == "video":
            if len(header) < 12 or header[4:8] != b"ftyp":
                raise TikTokUpstreamError(
                    "Uploaded data is not a valid MP4 or MOV video",
                    status_code=422,
                    code="invalid_video",
                )
            suffix = (
                ".mov"
                if Path(source_name).suffix.lower() == ".mov"
                or content_type.startswith("video/quicktime")
                else ".mp4"
            )
        else:
            if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
                suffix = ".wav"
            elif header.startswith(b"ID3") or (
                len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
            ):
                suffix = ".mp3"
            else:
                raise TikTokUpstreamError(
                    "Uploaded data is not a valid WAV or MP3 audio file",
                    status_code=422,
                    code="invalid_audio",
                )
        destination = path.with_suffix(suffix)
        path.replace(destination)
        return str(destination)

    def _validate_image(self, path: Path) -> str:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = (image.format or "").lower()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise TikTokUpstreamError(
                "Uploaded data is not a valid image",
                status_code=422,
                code="invalid_image",
            ) from exc
        if width <= 0 or height <= 0 or width * height > self.settings.sd2api_upload_max_pixels:
            raise TikTokUpstreamError(
                "Image dimensions exceed SD2API_UPLOAD_MAX_PIXELS",
                status_code=422,
                code="image_dimensions_too_large",
            )
        suffix = {
            "jpeg": ".jpg",
            "jpg": ".jpg",
            "png": ".png",
            "webp": ".webp",
            "bmp": ".bmp",
            "tiff": ".tiff",
            "gif": ".gif",
        }.get(image_format)
        if suffix is None:
            raise TikTokUpstreamError(
                "Supported image formats are JPEG, PNG, WebP, BMP, TIFF, and GIF",
                status_code=422,
                code="unsupported_image_format",
            )
        destination = path.with_suffix(suffix)
        path.replace(destination)
        return str(destination)
