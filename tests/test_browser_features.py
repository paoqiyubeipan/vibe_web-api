import base64

import pytest

from vibe_work_bridge.api import build_image_prompt
from vibe_work_bridge.browser import (
    AttachmentUploadFailed,
    NewConversationFailed,
    VibeWorkBrowser,
)
from vibe_work_bridge.images import ImageAttachment

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeUploadItem:
    def __init__(self):
        self.clicked = False

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.clicked = True


class FakeLocatorList:
    def __init__(self, item):
        self.item = item

    def count(self):
        return 1

    def nth(self, index):
        assert index == 0
        return self.item


class FakeDialog:
    first = None

    def __init__(self, item):
        self.item = item
        self.first = self

    def get_by_text(self, pattern):
        assert pattern.search("Upload Files")
        return FakeLocatorList(self.item)


class FakeFileChooser:
    def __init__(self):
        self.payloads = []

    def set_files(self, payload):
        self.payloads.append(payload)


class FakeChooserContext:
    def __init__(self, chooser):
        self.value = chooser

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeUploadPage:
    def __init__(self, dialog, chooser):
        self.dialog = dialog
        self.chooser = chooser
        self.chooser_timeout = None

    def get_by_role(self, role):
        assert role == "dialog"
        return self.dialog

    def expect_file_chooser(self, timeout):
        self.chooser_timeout = timeout
        return FakeChooserContext(self.chooser)


class FakeComposer:
    def locator(self, selector):
        return self

    def count(self):
        return 1

    def first(self):
        return self

    def inner_text(self, timeout=None):
        return "reference-1.png"


def test_conversation_id_uses_the_last_path_segment():
    assert VibeWorkBrowser._conversation_id("https://chat.mistral.ai/work/abc123") == "abc123"
    assert VibeWorkBrowser._conversation_id("https://chat.mistral.ai/work") is None
    assert VibeWorkBrowser._conversation_id("https://chat.mistral.ai/work/") is None
    assert VibeWorkBrowser._conversation_id("") is None


def test_image_prompt_carries_the_request_and_optional_size():
    prompt = build_image_prompt("  水墨猫  ", "1024x576")
    assert "水墨猫" in prompt
    assert "1024x576" in prompt
    assert prompt.startswith("Create an image for this request.")


def test_attach_files_sends_verified_buffers_and_confirms_upload(monkeypatch):
    browser = VibeWorkBrowser()
    upload_item = FakeUploadItem()
    chooser = FakeFileChooser()
    page = FakeUploadPage(FakeDialog(upload_item), chooser)
    browser._page = page
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: FakeComposer())
    monkeypatch.setattr(browser, "_composer_card", lambda composer: FakeComposer())
    monkeypatch.setattr(browser, "_attachment_evidence", lambda card: (0, ""))
    monkeypatch.setattr(browser, "_open_composer_menu", lambda: True)
    closed = []
    monkeypatch.setattr(browser, "_close_composer_menu", lambda: closed.append(True))
    checked = {}

    def fake_wait(names, baseline):
        checked["names"] = names
        checked["baseline"] = baseline

    monkeypatch.setattr(browser, "_wait_for_attachments", fake_wait)
    attachment = ImageAttachment(name="reference-1.png", mime_type="image/png", data=PNG)
    browser.attach_files([attachment])

    payload = chooser.payloads[0][0]
    assert payload["name"] == "reference-1.png"
    assert payload["mimeType"] == "image/png"
    assert payload["buffer"] == PNG
    assert upload_item.clicked
    assert page.chooser_timeout == 20000
    assert closed == [True]
    assert checked["names"] == ["reference-1.png"]


def test_attach_files_fails_when_no_file_picker_is_available(monkeypatch):
    browser = VibeWorkBrowser()
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: FakeComposer())
    monkeypatch.setattr(browser, "_composer_card", lambda composer: FakeComposer())
    monkeypatch.setattr(browser, "_attachment_evidence", lambda card: (0, ""))
    monkeypatch.setattr(browser, "_open_composer_menu", lambda: False)
    with pytest.raises(AttachmentUploadFailed, match="attachment menu"):
        browser.attach_files(
            [ImageAttachment(name="a.png", mime_type="image/png", data=PNG)]
        )


def test_attachment_wait_requires_evidence_in_the_composer(monkeypatch):
    browser = VibeWorkBrowser()
    browser.settings = type(browser.settings)(upload_timeout=1.0, poll_interval=0.01)
    monkeypatch.setattr(browser, "_find_composer", lambda: FakeComposer())
    monkeypatch.setattr(browser, "_composer_card", lambda composer: FakeComposer())
    monkeypatch.setattr(browser, "_is_generating", lambda: False)
    monkeypatch.setattr(browser._page, "wait_for_timeout", lambda ms: None) if browser._page else None
    browser._page = type("Page", (), {"wait_for_timeout": staticmethod(lambda ms: None)})()
    monkeypatch.setattr(browser, "_attachment_evidence", lambda card: (0, ""))
    with pytest.raises(AttachmentUploadFailed, match="never appeared"):
        browser._wait_for_attachments(["reference-1.png"], baseline_images=0)

    monkeypatch.setattr(browser, "_attachment_evidence", lambda card: (1, "reference-1.png"))
    browser._wait_for_attachments(["reference-1.png"], baseline_images=0)


class FakeNewChatControl:
    def __init__(self):
        self.clicks = 0

    def click(self):
        self.clicks += 1


def test_start_new_conversation_clicks_and_verifies_empty_state(monkeypatch):
    browser = VibeWorkBrowser()
    control = FakeNewChatControl()
    states = iter(
        [
            {"id": "old", "url": "https://chat.mistral.ai/work/old", "empty": False, "message_count": 3},
            {"id": "new", "url": "https://chat.mistral.ai/work/new", "empty": True, "message_count": 0},
        ]
    )
    monkeypatch.setattr(browser, "_ensure_started", lambda: None)
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: object())
    monkeypatch.setattr(browser, "_assert_composer_clean", lambda composer: None)
    monkeypatch.setattr(browser, "_find_composer", lambda: object())
    monkeypatch.setattr(browser, "_new_chat_control", lambda: control)
    monkeypatch.setattr(browser, "_page", type("Page", (), {"wait_for_timeout": staticmethod(lambda ms: None)})())
    monkeypatch.setattr(browser, "conversation_state", lambda: next(states))
    state = browser.start_new_conversation(reuse_empty=False)
    assert control.clicks == 1
    assert state["created"] is True
    assert state["id"] == "new"


def test_start_new_conversation_reuses_an_already_empty_chat(monkeypatch):
    browser = VibeWorkBrowser()
    control = FakeNewChatControl()
    monkeypatch.setattr(browser, "_ensure_started", lambda: None)
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: object())
    monkeypatch.setattr(browser, "_assert_composer_clean", lambda composer: None)
    monkeypatch.setattr(browser, "_new_chat_control", lambda: control)
    monkeypatch.setattr(
        browser,
        "conversation_state",
        lambda: {"id": "same", "url": "https://chat.mistral.ai/work/same", "empty": True, "message_count": 0},
    )
    state = browser.start_new_conversation(reuse_empty=True)
    assert control.clicks == 0
    assert state["reused"] is True


def test_start_new_conversation_fails_without_the_sidebar_control(monkeypatch):
    browser = VibeWorkBrowser()
    monkeypatch.setattr(browser, "_ensure_started", lambda: None)
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: object())
    monkeypatch.setattr(browser, "_assert_composer_clean", lambda composer: None)
    monkeypatch.setattr(
        browser,
        "conversation_state",
        lambda: {"id": "old", "url": "https://chat.mistral.ai/work/old", "empty": False, "message_count": 2},
    )
    monkeypatch.setattr(browser, "_new_chat_control", lambda: None)
    with pytest.raises(NewConversationFailed, match="New Chat"):
        browser.start_new_conversation()


def test_new_conversation_rejects_unchanged_work_url_even_when_empty(monkeypatch):
    from vibe_work_bridge.browser import BrowserSettings

    browser = VibeWorkBrowser(BrowserSettings(entry_timeout=0.01, poll_interval=0.001))
    control = FakeNewChatControl()
    state = {"id": None, "url": "https://chat.mistral.ai/work", "empty": True, "message_count": 0}
    monkeypatch.setattr(browser, "_ensure_started", lambda: None)
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: object())
    monkeypatch.setattr(browser, "_assert_composer_clean", lambda composer: None)
    monkeypatch.setattr(browser, "_find_composer", lambda: object())
    monkeypatch.setattr(browser, "_new_chat_control", lambda: control)
    monkeypatch.setattr(browser, "conversation_state", lambda: state)
    monkeypatch.setattr(browser, "_page", type("Page", (), {"wait_for_timeout": staticmethod(lambda ms: None)})())

    with pytest.raises(NewConversationFailed, match="distinct conversation"):
        browser.start_new_conversation(reuse_empty=False)

    assert control.clicks == 1


def test_message_count_counts_overlapping_assistant_selectors_once(monkeypatch):
    browser = VibeWorkBrowser()

    class Messages:
        def count(self):
            return 3

    class Page:
        def locator(self, selector):
            assert selector == (
                '[data-message-author-role], '
                '[data-message-author-role="assistant"], '
                '[data-testid*="assistant-message"]'
            )
            return Messages()

    browser._page = Page()
    assert browser._message_count() == 3


class FakeImageNode:
    def __init__(self, images):
        self.images = images

    def evaluate(self, script):
        return self.images


def test_only_large_real_images_are_treated_as_generated():
    browser = VibeWorkBrowser()
    node = FakeImageNode(
        [
            {"src": "https://chat.mistral.ai/static/logo.svg", "width": 900, "height": 900},
            {"src": "https://avatar.example/u.png", "width": 512, "height": 512},
            {"src": "data:image/svg+xml;base64,AAAA", "width": 800, "height": 800},
            {"src": "blob:https://chat.mistral.ai/123", "width": 64, "height": 64},
            {"src": "https://files.mistral.ai/generated/abc.png", "width": 1024, "height": 1024},
        ]
    )
    found = browser._images_in_node(node)
    assert [item["src"] for item in found] == [
        "https://files.mistral.ai/generated/abc.png"
    ]


def test_data_url_images_are_decoded_without_network_access():
    browser = VibeWorkBrowser()
    src = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    data, mime = browser._fetch_image_bytes(src)
    assert data == PNG
    assert mime == "image/png"


def test_cloudflare_image_proxy_fetches_original_blob_sas_url_first():
    original = (
        "https://storage.example.blob.core.windows.net/images/generated.png"
        "?sv=2025-01-05&se=2026-11-02T02%3A32%3A20Z&sig=a%2Fb"
    )
    proxy = (
        "https://chat.mistral.ai/cdn-cgi/image/width=1024,quality=85/"
        "https://storage.example.blob.core.windows.net/images/generated.png"
        "?sv=2025-01-05&se=2026-11-02T02%3A32%3A20Z&sig=a%2Fb"
    )

    class Response:
        ok = True

        @property
        def headers(self):
            return {"content-type": "image/png"}

        def body(self):
            return PNG

    class Request:
        def __init__(self):
            self.urls = []

        def get(self, url, timeout):
            self.urls.append(url)
            return Response()

    class Context:
        def __init__(self):
            self.request = Request()

    browser = VibeWorkBrowser()
    browser._context = Context()
    data, mime = browser._fetch_image_bytes(proxy)

    assert data == PNG
    assert mime == "image/png"
    assert browser._context.request.urls == [original]


def test_cloudflare_image_proxy_unwraps_percent_encoded_source_url():
    proxy = (
        "https://chat.mistral.ai/cdn-cgi/image/format=auto/"
        "https%3A%2F%2Fstorage.example%2Fimage.png%3Ftoken%3Dabc"
    )
    assert VibeWorkBrowser._original_image_url(proxy) == (
        "https://storage.example/image.png?token=abc"
    )
