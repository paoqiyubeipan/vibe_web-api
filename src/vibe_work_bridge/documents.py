"""Ordinary document inputs (PDF, TXT, DOCX) for the bridge.

Documents arrive as base64 payloads: either a ``data:`` URL inside an
OpenAI-style ``file`` content part, or bare base64 with an explicit filename or
MIME type. Remote URLs are rejected for the same reason as images: a bridge that
can be reached from the LAN must not become a fetch proxy for whatever the
browser can reach.

Every payload is checked against its own container before anything is written to
disk or handed to the Work composer: the PDF header and EOF marker, a DOCX zip
that really contains ``word/document.xml``, or text that decodes cleanly. The
filename is reduced to a safe basename and given the extension of the detected
type, so a caller cannot smuggle a script extension or a path through it.

This module never talks to Work. It only decides whether a payload is an ordinary
document the visible UI can accept.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_DOCUMENTS = 4
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
MAX_DOCUMENT_TOTAL_BYTES = 30 * 1024 * 1024

PDF_MIME = "application/pdf"
TEXT_MIME = "text/plain"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SUPPORTED_DOCUMENT_MIME = (PDF_MIME, TEXT_MIME, DOCX_MIME)

DOCUMENT_EXTENSIONS = {
    PDF_MIME: "pdf",
    TEXT_MIME: "txt",
    DOCX_MIME: "docx",
}

_EXTENSION_MIME = {extension: mime for mime, extension in DOCUMENT_EXTENSIONS.items()}

# Callers occasionally send the registered short forms.
_MIME_ALIASES = {
    "application/x-pdf": PDF_MIME,
    "application/acrobat": PDF_MIME,
    "text/x-pdf": PDF_MIME,
    "text/plain; charset=utf-8": TEXT_MIME,
    "text/plain;charset=utf-8": TEXT_MIME,
    "application/docx": DOCX_MIME,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document; charset=binary": DOCX_MIME,
}

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[a-z0-9.+\-]+/[a-z0-9.+\-]+)(?P<params>(?:;[^,;]*)*?);base64,(?P<payload>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f\x7f"\\/:*?<>|]+')
_TEXT_MARKER = b"%PDF-"


class DocumentInputError(ValueError):
    """The caller supplied a document that cannot be used safely."""


@dataclass(frozen=True)
class DocumentAttachment:
    """One verified document ready for the Work composer."""

    name: str
    mime_type: str
    data: bytes
    text: str | None = None

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def characters(self) -> int | None:
        """Length of locally extracted text, used for diagnostics only."""
        return None if self.text is None else len(self.text)

    def describe(self) -> dict:
        """Public metadata. The document body itself is never echoed back."""
        return {
            "filename": self.name,
            "mime_type": self.mime_type,
            "bytes": self.size,
            "characters": self.characters,
        }


def normalise_mime(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    return _MIME_ALIASES.get(cleaned, cleaned)


def mime_from_filename(name: str | None) -> str | None:
    if not name:
        return None
    return _EXTENSION_MIME.get(Path(name).suffix.lower().lstrip("."))


def _looks_like_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


def _decode_text(data: bytes) -> str | None:
    if not data:
        return None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data:
        return None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _text_is_plain(text: str) -> bool:
    """Reject binary-ish blobs that happen to decode as UTF-8."""
    if not text.strip():
        return False
    control = 0
    for char in text:
        if char in "\n\r\t":
            continue
        if unicodedata.category(char) in {"Cc", "Cf", "Co", "Cs"}:
            control += 1
    return control / max(len(text), 1) < 0.02


def sniff_document(data: bytes) -> str | None:
    """Identify a supported document by its content, not its declared type."""
    if data.startswith(_TEXT_MARKER) and b"%%EOF" in data[-4096:]:
        return PDF_MIME
    if data.startswith(b"PK\x03\x04") and _looks_like_docx(data):
        return DOCX_MIME
    text = _decode_text(data)
    if text is not None and _text_is_plain(text):
        return TEXT_MIME
    return None


def _docx_text(data: bytes, limit: int = 200_000) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile, OSError, ValueError):
        return None
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )
    return text[:limit]


def _pdf_text(data: bytes, limit: int = 200_000) -> str | None:
    """Best-effort text extraction; PDF parsing is optional for this bridge."""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        chunks = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return None
    text = "\n".join(chunks).strip()
    return text[:limit] if text else None


def extract_text(data: bytes, mime_type: str) -> str | None:
    if mime_type == TEXT_MIME:
        return _decode_text(data)
    if mime_type == DOCX_MIME:
        return _docx_text(data)
    if mime_type == PDF_MIME:
        return _pdf_text(data)
    return None


def sanitize_filename(name: str | None, mime_type: str, index: int = 0) -> str:
    """Reduce a caller-supplied name to a safe basename with the real extension."""
    raw = str(name or "").replace("\\", "/").split("/")[-1]
    raw = _UNSAFE_FILENAME.sub("-", raw).strip(" .-")
    stem = Path(raw).stem if raw else ""
    stem = re.sub(r"\s+", " ", stem).strip(" .-")[:80]
    if not stem:
        stem = f"document-{index + 1}"
    return f"{stem}.{DOCUMENT_EXTENSIONS[mime_type]}"


def _decode_payload(value: str, *, mime_hint: str | None, name_hint: str | None) -> tuple[bytes, str | None]:
    """Return (bytes, declared MIME) from a data URL or bare base64 string."""
    if not isinstance(value, str) or not value.strip():
        raise DocumentInputError("document payload must be a non-empty base64 string or data: URL")
    candidate = value.strip()
    if candidate.lower().startswith(("http://", "https://")):
        raise DocumentInputError(
            "remote document URLs are not accepted; send the file itself as base64 "
            "or as a data:application/pdf;base64,... URL"
        )
    match = _DATA_URL_RE.match(candidate)
    if match:
        declared = normalise_mime(match.group("mime"))
        payload = match.group("payload")
    else:
        declared = normalise_mime(mime_hint) or mime_from_filename(name_hint)
        payload = candidate
    if not _BASE64_RE.match(payload or ""):
        raise DocumentInputError("document payload is not valid base64")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DocumentInputError("document payload is not valid base64") from exc
    if not data:
        raise DocumentInputError("document payload is empty")
    return data, declared


def decode_document(
    value: str,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    index: int = 0,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> DocumentAttachment:
    """Validate one document and return it ready for upload."""
    data, declared = _decode_payload(value, mime_hint=mime_type, name_hint=filename)
    if len(data) > max_bytes:
        raise DocumentInputError(
            f"document is {len(data)} bytes; the limit is {max_bytes} bytes per document"
        )
    sniffed = sniff_document(data)
    if sniffed is None:
        hint = ""
        if data.startswith(b"\xd0\xcf\x11\xe0"):
            hint = " Legacy .doc files are not supported; save as .docx first."
        raise DocumentInputError(
            "payload is not a supported document (PDF, UTF-8/UTF-16 text, or DOCX)." + hint
        )
    if declared and declared not in SUPPORTED_DOCUMENT_MIME:
        raise DocumentInputError(
            f"unsupported document type {declared!r}; use one of "
            f"{', '.join(SUPPORTED_DOCUMENT_MIME)}"
        )
    if declared and sniffed != declared:
        raise DocumentInputError(
            f"document payload is {sniffed}, but the request declares {declared}"
        )
    return DocumentAttachment(
        name=sanitize_filename(filename, sniffed, index=index),
        mime_type=sniffed,
        data=data,
        text=extract_text(data, sniffed),
    )


def decode_documents(
    items: list[dict] | None,
    *,
    max_documents: int = MAX_DOCUMENTS,
    max_total_bytes: int = MAX_DOCUMENT_TOTAL_BYTES,
) -> list[DocumentAttachment]:
    """Validate a batch of ``{"filename", "file_data"|"data", "mime_type"}`` items."""
    if not items:
        return []
    if len(items) > max_documents:
        raise DocumentInputError(
            f"at most {max_documents} documents are supported per request"
        )
    documents: list[DocumentAttachment] = []
    total = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise DocumentInputError("each document must be an object")
        payload = item.get("file_data") or item.get("data")
        if not payload:
            raise DocumentInputError("each document needs file_data (base64 or data: URL)")
        attachment = decode_document(
            payload,
            filename=item.get("filename"),
            mime_type=item.get("mime_type") or item.get("mime"),
            index=index,
        )
        total += attachment.size
        if total > max_total_bytes:
            raise DocumentInputError(
                f"documents total {total} bytes; the limit is {max_total_bytes}"
            )
        documents.append(attachment)
    return documents
