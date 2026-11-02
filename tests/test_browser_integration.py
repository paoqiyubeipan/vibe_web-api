"""End-to-end checks of the browser automation against a local Work look-alike.

These run a real Chromium (headless) against ``tests/fixtures/fake_work.html``,
so composer interaction, the upload confirmation loop, new-chat verification and
image extraction are exercised without touching a signed-in Mistral session.
"""

from __future__ import annotations

import base64
import functools
import http.server
import socket
import threading
from dataclasses import replace

import pytest

from vibe_work_bridge.browser import (
    ComposerDraftConflict,
    NewConversationFailed,
    VibeWorkBrowser,
)
from vibe_work_bridge.documents import TEXT_MIME, DocumentAttachment
from vibe_work_bridge.images import ImageAttachment

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

TXT = b"Synthetic bridge note.\nmarker: DOC-TXT-ALPHA\n"


def clear_chips(browser):
    browser._page.evaluate("document.getElementById('chips').innerHTML = ''")

pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def fixture_url():
    from pathlib import Path

    directory = Path(__file__).parent / "fixtures"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/fake_work.html"
    server.shutdown()


@pytest.fixture(scope="function")
def browser(fixture_url, tmp_path_factory):
    from vibe_work_bridge.browser import BrowserSettings

    settings = BrowserSettings(
        base_url=fixture_url,
        profile_dir=tmp_path_factory.mktemp("fixture-profile"),
        headless=True,
        response_timeout=30.0,
        upload_timeout=15.0,
        image_settle_timeout=10.0,
        poll_interval=0.2,
        entry_timeout=20.0,
    )
    instance = VibeWorkBrowser(settings)
    try:
        instance._ensure_started()
    except Exception as exc:  # pragma: no cover - environment without a browser
        pytest.skip(f"Chromium unavailable for integration test: {exc}")
    yield instance
    instance.close()


def test_mode_switching_works_on_the_fixture(browser):
    from vibe_work_bridge.modes import ResponseMode

    browser._switch_mode(ResponseMode.THINK)
    assert browser._current_mode() is ResponseMode.THINK
    browser._switch_mode(ResponseMode.FAST)
    assert browser._current_mode() is ResponseMode.FAST


def test_new_chat_control_finds_an_icon_button_by_accessible_name(browser):
    control = browser._new_chat_control()
    assert control is not None
    assert control.get_attribute("id") == "new-chat"


def test_new_conversation_creates_and_verifies_an_empty_chat(browser):
    from vibe_work_bridge.modes import ResponseMode

    browser.send_message("Keep this message around.", ResponseMode.FAST)
    assert browser.conversation_state()["empty"] is False
    state = browser.start_new_conversation(reuse_empty=False)
    assert state["created"] is True
    assert state["empty"] is True
    assert browser.conversation_state()["message_count"] == 0


def test_text_turn_returns_the_latest_reply(browser):
    from vibe_work_bridge.modes import ResponseMode

    result = browser.send_message("Hello there", ResponseMode.FAST)
    assert "Hello there" in result["text"]
    assert result["images"] == []
    assert result["mode_applied"] is True


def test_reference_image_upload_is_confirmed_before_sending(browser):
    attachment = ImageAttachment(name="reference-1.png", mime_type="image/png", data=PNG)
    browser.attach_files([attachment])
    text = browser._composer_card(browser._find_composer()).inner_text()
    assert "reference-1.png" in text


def test_image_generation_is_extracted_from_the_assistant_message(browser):
    from vibe_work_bridge.modes import ResponseMode

    result = browser.send_message(
        "draw a gradient square", ResponseMode.FAST, expect_images=True
    )
    assert result["images"], "expected one extracted image"
    image = result["images"][0]
    assert image["mime_type"] == "image/png"
    assert image["data"][:8] == b"\x89PNG\r\n\x1a\n"
    assert image["width"] >= 200


def test_reference_image_and_prompt_are_sent_together(browser):
    from vibe_work_bridge.modes import ResponseMode

    attachment = ImageAttachment(name="reference-1.png", mime_type="image/png", data=PNG)
    result = browser.send_message(
        "make this an image again",
        ResponseMode.FAST,
        attachments=[attachment],
        expect_images=True,
    )
    assert result["attachments"] == 1
    assert result["images"]


def test_session_reports_whether_documents_are_advertised(browser):
    state = browser.inspect_session()
    assert state["document_upload_hint"] == "documents_advertised"
    assert any(".pdf" in value for value in state["file_input_accept"])


def test_document_upload_is_confirmed_in_the_composer(browser):
    clear_chips(browser)
    attachment = DocumentAttachment(
        name="synthetic-note.txt", mime_type=TEXT_MIME, data=TXT, text=TXT.decode()
    )
    browser.attach_files([attachment])
    text = browser._composer_card(browser._find_composer()).inner_text()
    assert "synthetic-note.txt" in text


def test_document_text_can_be_read_back_from_the_reply(browser):
    from vibe_work_bridge.modes import ResponseMode

    clear_chips(browser)
    attachment = DocumentAttachment(
        name="synthetic-brief.txt", mime_type=TEXT_MIME, data=TXT, text=TXT.decode()
    )
    result = browser.send_message(
        "Read the attached document and summarize its key points.",
        ResponseMode.FAST,
        attachments=[attachment],
    )
    assert result["attachments"] == 1
    assert "DOC-TXT-ALPHA" in result["text"]


def test_send_preserves_and_rejects_an_existing_text_draft(browser):
    from vibe_work_bridge.modes import ResponseMode

    composer = browser._find_composer()
    composer.fill("an unsent user draft")

    with pytest.raises(ComposerDraftConflict):
        browser.send_message("do not replace this", ResponseMode.FAST)

    assert composer.inner_text() == "an unsent user draft"


def test_send_preserves_and_rejects_an_existing_attachment(browser):
    from vibe_work_bridge.modes import ResponseMode

    browser.attach_files(
        [ImageAttachment(name="unknown-draft.png", mime_type="image/png", data=PNG)]
    )
    with pytest.raises(ComposerDraftConflict):
        browser.send_message("do not send with the unknown file", ResponseMode.FAST)

    card_text = browser._composer_card(browser._find_composer()).inner_text()
    assert "unknown-draft.png" in card_text


def test_unverifiable_new_chat_route_prevents_the_message_send(browser):
    from vibe_work_bridge.modes import ResponseMode

    browser.settings = replace(browser.settings, entry_timeout=0.02, poll_interval=0.001)
    browser._page.evaluate("history.pushState = () => {}")

    with pytest.raises(NewConversationFailed, match="distinct conversation"):
        browser.send_message(
            "must not be sent to an unverified chat",
            ResponseMode.FAST,
            new_conversation=True,
        )

    assert browser._user_message_count() == 0
