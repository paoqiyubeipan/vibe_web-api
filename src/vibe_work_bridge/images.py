"""Image inputs and outputs for the bridge.

Reference images arrive as ``data:image/...;base64,...`` URLs. Remote URLs are
rejected on purpose: this bridge is meant to be reachable from other devices on
a LAN, so accepting arbitrary URLs would turn it into a request-forgery proxy
for whatever the browser can reach (including internal addresses).

Declared MIME types are checked against the file's magic bytes before anything
is written to disk or handed to the Work composer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024

SUPPORTED_MIME = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/avif",
    "image/bmp",
)

_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
    "image/bmp": "bmp",
}

_EXTENSIONS_MIME = {
    value: key for key, value in _MIME_EXTENSIONS.items()
}

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/[a-z0-9.+-]+)(?P<params>(?:;[^,;]*)*?);base64,(?P<payload>.+)$",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")


class ImageInputError(ValueError):
    """The caller supplied an image that cannot be used safely."""


@dataclass(frozen=True)
class ImageAttachment:
    """One verified reference image ready for the Work composer."""

    name: str
    mime_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime_type};base64,{encoded}"


@dataclass(frozen=True)
class GeneratedImage:
    """One image extracted from a Work assistant message."""

    image_id: str
    mime_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def b64_json(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def url_path(self) -> str:
        return f"/v1/images/{self.image_id}/content"


def sniff_mime(data: bytes) -> str | None:
    """Identify an image by magic bytes; returns None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"BM") and len(data) > 26:
        return "image/bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif"
    return None


def image_extension(mime_type: str) -> str:
    return _MIME_EXTENSIONS.get(mime_type, "bin")


def mime_from_extension(path: Path | str) -> str | None:
    return _EXTENSIONS_MIME.get(Path(path).suffix.lower().lstrip("."))


def _attachment_name(index: int, mime_type: str) -> str:
    return f"reference-{index + 1}.{image_extension(mime_type)}"


def decode_data_url(
    value: str,
    *,
    index: int = 0,
    max_bytes: int = MAX_IMAGE_BYTES,
    name: str | None = None,
) -> ImageAttachment:
    """Validate one data URL and return its bytes."""
    if not isinstance(value, str) or not value.strip():
        raise ImageInputError("image must be a non-empty data URL")
    candidate = value.strip()
    if candidate.lower().startswith(("http://", "https://")):
        raise ImageInputError(
            "remote image URLs are not accepted; send the image itself as a "
            "data:image/...;base64,... URL"
        )
    match = _DATA_URL_RE.match(candidate)
    if not match:
        raise ImageInputError(
            "image must be a base64 data URL such as data:image/png;base64,...."
        )
    declared = match.group("mime").lower()
    if declared == "image/jpg":
        declared = "image/jpeg"
    if declared not in SUPPORTED_MIME:
        raise ImageInputError(
            f"unsupported image type {declared!r}; use one of {', '.join(SUPPORTED_MIME)}"
        )
    try:
        data = base64.b64decode(re.sub(r"\s+", "", match.group("payload")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageInputError("image payload is not valid base64") from exc
    if not data:
        raise ImageInputError("image payload is empty")
    if len(data) > max_bytes:
        raise ImageInputError(
            f"image is {len(data)} bytes; the limit is {max_bytes} bytes per image"
        )
    sniffed = sniff_mime(data)
    if sniffed is None:
        raise ImageInputError("payload does not look like a supported image file")
    if sniffed != declared:
        raise ImageInputError(
            f"image payload is {sniffed}, but the data URL declares {declared}"
        )
    return ImageAttachment(
        name=name or _attachment_name(index, sniffed),
        mime_type=sniffed,
        data=data,
    )


def decode_reference_images(
    values: list[str] | None,
    *,
    max_images: int = MAX_IMAGES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> list[ImageAttachment]:
    """Validate a batch of reference images, enforcing count and total size."""
    if not values:
        return []
    if len(values) > max_images:
        raise ImageInputError(
            f"at most {max_images} reference images are supported per request"
        )
    attachments: list[ImageAttachment] = []
    total = 0
    for index, value in enumerate(values):
        attachment = decode_data_url(value, index=index)
        total += attachment.size
        if total > max_total_bytes:
            raise ImageInputError(
                f"reference images total {total} bytes; the limit is {max_total_bytes}"
            )
        attachments.append(attachment)
    return attachments


def output_directory() -> Path:
    path = Path(
        os.environ.get(
            "VIBE_WORK_OUTPUT_DIR",
            str(Path.home() / ".vibe-work-bridge" / "outputs"),
        )
    ).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_generated_image(data: bytes, mime_type: str) -> GeneratedImage:
    """Persist an extracted image and return its addressable record."""
    digest = hashlib.sha256(data).hexdigest()
    image_id = digest[:32]
    directory = output_directory()
    target = directory / f"{image_id}.{image_extension(mime_type)}"
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
    return GeneratedImage(image_id=image_id, mime_type=mime_type, data=data)


def find_saved_image(image_id: str) -> Path | None:
    """Resolve an image id to a stored file without leaving the output dir."""
    if not _IMAGE_ID_RE.match(image_id or ""):
        return None
    directory = output_directory()
    for suffix in _MIME_EXTENSIONS.values():
        candidate = directory / f"{image_id}.{suffix}"
        if candidate.is_file():
            return candidate
    return None
