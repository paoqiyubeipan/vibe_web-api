"""Validation rules for ordinary document attachments."""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from vibe_work_bridge.documents import (
    DOCX_MIME,
    PDF_MIME,
    TEXT_MIME,
    DocumentInputError,
    decode_document,
    decode_documents,
    sanitize_filename,
    sniff_document,
)

TXT = b"Synthetic note for the bridge test.\nmarker: DOC-TXT-ALPHA\n"
PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (DOC-PDF-GAMMA) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)


def make_docx(marker: str = "DOC-DOCX-BETA") -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>" + marker + "</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def test_text_pdf_and_docx_are_recognised():
    assert sniff_document(TXT) == TEXT_MIME
    assert sniff_document(PDF) == PDF_MIME
    assert sniff_document(make_docx()) == DOCX_MIME


def test_text_document_gets_a_safe_default_name():
    document = decode_document(data_url(TEXT_MIME, TXT))
    assert document.name == "document-1.txt"
    assert document.mime_type == TEXT_MIME
    assert document.text is not None and "DOC-TXT-ALPHA" in document.text
    assert document.describe()["characters"] == len(document.text)


def test_mislabelled_container_is_rejected_before_upload():
    # A PDF sent as text/plain is a caller mistake, not something to guess about.
    with pytest.raises(DocumentInputError, match="declares"):
        decode_document(data_url(TEXT_MIME, PDF), filename="notes.txt")


def test_extension_follows_the_detected_type():
    document = decode_document(data_url(PDF_MIME, PDF), filename="quarterly-report.txt")
    assert document.mime_type == PDF_MIME
    assert document.name == "quarterly-report.pdf"


def test_declared_binary_type_mismatch_is_rejected():
    with pytest.raises(DocumentInputError, match="declares"):
        decode_document(data_url(DOCX_MIME, TXT))


def test_docx_without_word_document_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("docProps/core.xml", "<core/>")
    with pytest.raises(DocumentInputError, match="not a supported document"):
        decode_document(data_url(DOCX_MIME, buffer.getvalue()))


def test_legacy_doc_binary_is_rejected_with_a_hint():
    legacy = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    with pytest.raises(DocumentInputError, match="save as .docx"):
        decode_document(data_url("application/msword", legacy), filename="old.doc")


def test_remote_url_is_refused():
    with pytest.raises(DocumentInputError, match="remote document URLs"):
        decode_document("https://example.com/private.pdf")


def test_size_limit_is_enforced_per_document():
    with pytest.raises(DocumentInputError, match="limit is 32 bytes"):
        decode_document(data_url(TEXT_MIME, TXT), max_bytes=32)


def test_filename_cannot_escape_or_change_extension():
    document = decode_document(data_url(TEXT_MIME, TXT), filename="../../etc/passwd.cmd")
    assert document.name == "passwd.txt"


def test_filename_strips_control_characters():
    assert sanitize_filename('bad"name\x00\n.txt', TEXT_MIME) == "bad-name.txt"


def test_raw_base64_is_accepted_with_a_filename_hint():
    document = decode_document(
        base64.b64encode(make_docx()).decode("ascii"), filename="brief.docx"
    )
    assert document.mime_type == DOCX_MIME
    assert document.name == "brief.docx"


def test_batch_limits_and_empty_payload_are_enforced():
    items = [{"filename": f"{index}.txt", "file_data": data_url(TEXT_MIME, TXT)} for index in range(5)]
    with pytest.raises(DocumentInputError, match="at most 4"):
        decode_documents(items)
    with pytest.raises(DocumentInputError, match="file_data"):
        decode_documents([{"filename": "empty.txt"}])


def test_batch_total_size_limit_is_enforced():
    items = [
        {"filename": "a.txt", "file_data": data_url(TEXT_MIME, TXT)},
        {"filename": "b.txt", "file_data": data_url(TEXT_MIME, TXT)},
    ]
    with pytest.raises(DocumentInputError, match="documents total"):
        decode_documents(items, max_total_bytes=20)


def test_binary_blob_disguised_as_text_is_rejected():
    with pytest.raises(DocumentInputError, match="not a supported document"):
        decode_document(
            data_url(TEXT_MIME, bytes(range(0, 32)) * 4), filename="blob.txt"
        )
