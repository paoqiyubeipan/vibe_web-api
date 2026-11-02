#!/usr/bin/env python3
"""Offline round-trip check for ordinary document attachments.

This builds three clearly synthetic documents (TXT, PDF, DOCX), validates them
with the same code the API uses, then drives a real headless Chromium through
``tests/fixtures/fake_work.html``: upload -> composer confirmation -> submit ->
read the reply back.

It never signs in to Mistral, never touches a real Work conversation, and never
generates an image. The JSON report is written to ``test-artifacts/``.
"""

from __future__ import annotations

import base64
import datetime as dt
import functools
import hashlib
import http.server
import io
import json
import socket
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibe_work_bridge.documents import (
    DOCX_MIME,
    PDF_MIME,
    TEXT_MIME,
    decode_document,
)
from vibe_work_bridge.modes import ResponseMode

TXT = (
    "Synthetic bridge verification note.\n"
    "This file contains no real personal or business data.\n"
    "marker: DOC-TXT-ALPHA\n"
)

PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 62>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (Synthetic fixture document. DOC-PDF-GAMMA) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)


def docx_bytes() -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Synthetic fixture document. DOC-DOCX-BETA</w:t></w:r></w:p>"
        "</w:body></w:document>"
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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    from vibe_work_bridge.browser import BrowserSettings, VibeWorkBrowser

    draft_dir = Path(tempfile.mkdtemp(prefix="vibe-doc-verification-"))
    samples = {
        "text": ("synthetic-note.txt", TEXT_MIME, TXT.encode("utf-8")),
        "pdf": ("synthetic-brief.pdf", PDF_MIME, PDF),
        "docx": ("synthetic-memo.docx", DOCX_MIME, docx_bytes()),
    }

    validated = {}
    for key, (name, mime, payload) in samples.items():
        (draft_dir / name).write_bytes(payload)
        document = decode_document(data_url(mime, payload), filename=name)
        validated[key] = {
            "filename": document.name,
            "declared_mime": mime,
            "detected_mime": document.mime_type,
            "bytes": document.size,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "characters": document.characters,
            "local_path": str(draft_dir / name),
            "attachment": document,
        }

    fixture_dir = ROOT / "tests" / "fixtures"
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(fixture_dir)
    )
    port = free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    settings = BrowserSettings(
        base_url=f"http://127.0.0.1:{port}/fake_work.html",
        profile_dir=draft_dir / "profile",
        headless=True,
        response_timeout=30.0,
        upload_timeout=20.0,
        image_settle_timeout=5.0,
        poll_interval=0.2,
        entry_timeout=25.0,
    )
    browser = VibeWorkBrowser(settings)
    report = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": f"http://127.0.0.1:{port}/fake_work.html (local Work look-alike)",
        "real_work_session": False,
        "image_generation": False,
        "formats": {},
    }
    failures = []
    try:
        browser._ensure_started()
        session = browser.inspect_session()
        report["session"] = {
            "file_input_accept": session.get("file_input_accept"),
            "document_upload_hint": session.get("document_upload_hint"),
        }
        for key, entry in validated.items():
            browser._page.evaluate("document.getElementById('chips').innerHTML = ''")
            result: dict = {"format": key}
            try:
                browser.attach_files([entry["attachment"]])
                composer_text = browser._composer_card(browser._find_composer()).inner_text()
                chip_ok = entry["filename"] in composer_text
                reply = browser.send_message(
                    "Read the attached document and summarize its key points.",
                    ResponseMode.FAST,
                    attachments=[entry["attachment"]],
                )
                text = reply.get("text", "")
                readback_ok = entry["filename"] in text and str(entry["bytes"]) in text
                # The look-alike reads binary files as bytes, so only plain text
                # can prove the *content* survived; PDF/DOCX prove delivery by
                # name and byte count.
                marker_ok = None
                if key == "text":
                    marker_ok = "DOC-TXT-ALPHA" in text
                result.update(
                    {
                        "upload_confirmed_in_composer": chip_ok,
                        "submitted_attachments": reply.get("attachments"),
                        "readback_text": text,
                        "readback_matched_name_and_size": readback_ok,
                        "marker_visible_in_reply": marker_ok,
                        "marker_note": (
                            "content marker asserted for text/plain only; "
                            "binary formats are confirmed by name and byte count"
                        ),
                        "ok": bool(
                            chip_ok and readback_ok and (marker_ok is not False)
                        ),
                    }
                )
                if not result["ok"]:
                    failures.append(key)
            except Exception as exc:  # pragma: no cover - reported, not hidden
                result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                failures.append(key)
            result.update(
                {
                    "filename": entry["filename"],
                    "mime_type": entry["detected_mime"],
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                    "local_characters": entry["characters"],
                }
            )
            report["formats"][key] = result
    finally:
        browser.close()
        server.shutdown()
        report["passed"] = not failures
        report["failed_formats"] = failures
        stamp = dt.datetime.now().strftime("%Y%m%d")
        out_dir = ROOT / "test-artifacts"
        out_dir.mkdir(exist_ok=True)
        out_file = out_dir / f"document-roundtrip-{stamp}.json"
        out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nreport: {out_file}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
