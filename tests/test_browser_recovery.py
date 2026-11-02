import base64

import pytest

from vibe_work_bridge.api import BrowserWorker, _map_bridge_error
from vibe_work_bridge.browser import (
    BrowserSettings,
    ComposerDraftConflict,
    SubmissionNotConfirmed,
    VibeWorkBrowser,
)
from vibe_work_bridge.modes import ResponseMode

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TargetClosedError(Exception):
    """Mirrors Playwright's error name so the recovery check recognises it."""


class FakeBrowser:
    def __init__(self, error=None):
        self.error = error
        self.closed = False

    def send_message(self, prompt, mode, **kwargs):
        if self.error is not None:
            raise self.error
        return {"text": "ok", "mode": mode.value, "mode_applied": True, "images": [], "attachments": 0}

    def close(self):
        self.closed = True


def test_closed_browser_is_not_replaced_or_request_retried():
    first = FakeBrowser(TargetClosedError("Target page, context or browser has been closed"))
    second = FakeBrowser()
    created = []

    def factory():
        browser = first if not created else second
        created.append(browser)
        return browser

    worker = BrowserWorker(browser_factory=factory)
    with pytest.raises(TargetClosedError):
        worker._send_sync("hello", ResponseMode.FAST, None, False, False)
    assert first.closed is True
    assert worker._browser is None
    assert len(created) == 1


def test_closed_browser_error_warns_against_blind_resubmission():
    error = TargetClosedError("Target page, context or browser has been closed")
    http_error = _map_bridge_error(error)
    assert http_error.status_code == 503
    assert http_error.detail["code"] == "browser_closed"
    assert "did not retry" in http_error.detail["message"]
    assert "Check the Work conversation" in http_error.detail["message"]


class FakeButton:
    def __init__(self, fail=False):
        self.clicks = 0
        self.fail = fail

    def click(self, **kwargs):
        self.clicks += 1
        if self.fail:
            raise RuntimeError("click outcome is unknown")


class FakePage:
    def __init__(self):
        self.keys = []

    def wait_for_timeout(self, ms):
        pass

    @property
    def keyboard(self):
        page = self

        class Keyboard:
            def press(self, key):
                page.keys.append(key)

        return Keyboard()


class FakeDraftComposer:
    def __init__(self, text=""):
        self.text = text
        self.fills = []

    def input_value(self, timeout=None):
        return self.text

    def inner_text(self, timeout=None):
        return self.text

    def fill(self, text, timeout=None):
        self.fills.append(text)
        self.text = text


def test_reset_input_refuses_to_clear_an_unknown_text_draft(monkeypatch):
    browser = VibeWorkBrowser()
    browser._page = FakePage()
    composer = FakeDraftComposer("user's unsent draft")
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: composer)

    with pytest.raises(ComposerDraftConflict, match="will not clear or replace it"):
        browser.reset_input()

    assert composer.text == "user's unsent draft"
    assert browser._page.keys == []


def test_reset_input_refuses_to_remove_unknown_attachments(monkeypatch):
    browser = VibeWorkBrowser()
    browser._page = FakePage()
    composer = FakeDraftComposer()
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: composer)
    monkeypatch.setattr(browser, "_composer_has_attachments", lambda current, text: True)

    with pytest.raises(ComposerDraftConflict, match="will not clear or replace it"):
        browser.reset_input()

    assert composer.text == ""
    assert browser._page.keys == []


def test_reset_input_is_a_noop_on_a_clean_composer(monkeypatch):
    browser = VibeWorkBrowser()
    browser._page = FakePage()
    composer = FakeDraftComposer()
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: composer)
    monkeypatch.setattr(browser, "_composer_has_attachments", lambda current, text: False)

    browser.reset_input()

    assert composer.text == ""
    assert browser._page.keys == []


def test_send_message_does_not_change_mode_or_replace_an_existing_draft(monkeypatch):
    browser = VibeWorkBrowser()
    composer = FakeDraftComposer("leave this draft alone")
    steps = []
    monkeypatch.setattr(browser, "_ensure_started", lambda: None)
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: composer)
    monkeypatch.setattr(browser, "_switch_mode", lambda mode: steps.append("mode"))
    monkeypatch.setattr(browser, "_submit", lambda current, prompt: steps.append("submit"))

    with pytest.raises(ComposerDraftConflict):
        browser.send_message("replacement", ResponseMode.FAST)

    assert steps == []
    assert composer.text == "leave this draft alone"
    assert composer.fills == []


class ReRenderingComposer:
    def __init__(self, fail_fill=False, fail_press=False):
        self.fail_fill = fail_fill
        self.fail_press = fail_press
        self.filled = []
        self.presses = 0
        self.text = ""

    def input_value(self, timeout=None):
        return self.text

    def inner_text(self, timeout=None):
        return self.text

    def fill(self, prompt, timeout=None):
        if self.fail_fill:
            raise RuntimeError("element was detached")
        self.filled.append(prompt)
        self.text = prompt

    def press(self, key, timeout=None):
        if self.fail_press:
            raise RuntimeError("element is not attached")
        self.presses += 1


def test_submit_relocates_the_editor_when_work_rerenders_it(monkeypatch):
    browser = VibeWorkBrowser()
    stale = ReRenderingComposer(fail_fill=True)
    fresh = ReRenderingComposer()
    monkeypatch.setattr(browser, "_find_composer", lambda: fresh)
    monkeypatch.setattr(browser, "_click_send_button", lambda: False)
    browser._page = FakePage()

    browser._submit(stale, "draw a cat")
    assert fresh.filled == ["draw a cat"]
    assert fresh.presses == 1


def test_submit_does_not_fall_back_after_enter_attempt_fails(monkeypatch):
    browser = VibeWorkBrowser()
    composer = ReRenderingComposer(fail_press=True)
    page = FakePage()
    monkeypatch.setattr(browser, "_find_composer", lambda: composer)
    monkeypatch.setattr(browser, "_click_send_button", lambda: False)
    browser._page = page

    browser._submit(composer, "hello")
    assert page.keys == []


def test_submit_does_not_retry_after_send_button_click_errors(monkeypatch):
    browser = VibeWorkBrowser()
    composer = ReRenderingComposer()
    button = FakeButton(fail=True)

    class SendPage(FakePage):
        def get_by_role(self, role, name=None):
            return [button]

    monkeypatch.setattr(browser, "_find_composer", lambda: composer)
    monkeypatch.setattr(browser, "_iter_visible", lambda locators: list(locators))
    browser._page = SendPage()

    browser._submit(composer, "hello")

    assert button.clicks == 1
    assert composer.presses == 0


def test_send_waits_for_submission_confirmation_before_response(monkeypatch):
    browser = VibeWorkBrowser()
    steps = []
    composer = ReRenderingComposer()
    monkeypatch.setattr(browser, "_ensure_started", lambda: None)
    monkeypatch.setattr(browser, "_ensure_work_composer", lambda: composer)
    monkeypatch.setattr(browser, "_assert_composer_clean", lambda current: None)
    monkeypatch.setattr(browser, "_switch_mode", lambda mode: None)
    monkeypatch.setattr(browser, "reset_input", lambda: None)
    monkeypatch.setattr(browser, "_assistant_count", lambda: 0)
    monkeypatch.setattr(browser, "_user_message_count", lambda: 0)
    monkeypatch.setattr(
        browser,
        "_submit",
        lambda current, prompt: steps.append("submit"),
    )
    monkeypatch.setattr(
        browser,
        "_wait_for_submission",
        lambda prompt, previous, previous_prompt_count: steps.append("confirm"),
    )
    monkeypatch.setattr(
        browser,
        "_wait_for_response",
        lambda previous, expect_images=False: (steps.append("response") or "ok", []),
    )
    monkeypatch.setattr(browser, "conversation_state", lambda: {})

    result = browser.send_message("hello", ResponseMode.FAST)

    assert steps == ["submit", "confirm", "response"]
    assert result["text"] == "ok"


class FakeUserMessages:
    def __init__(self, page):
        self.page = page

    def count(self):
        return len(self.page.messages)

    def all_inner_texts(self):
        return list(self.page.messages)


class FakeSubmissionPage:
    def __init__(self, on_wait=None):
        self.messages = []
        self.visible_texts = []
        self.on_wait = on_wait

    def locator(self, selector):
        assert selector == '[data-message-author-role="user"]'
        return FakeUserMessages(self)

    def get_by_text(self, text, exact=False):
        assert exact is True
        return FakePromptMatches(
            [FakePromptNode(item) for item in self.visible_texts if item == text]
        )

    def wait_for_timeout(self, ms):
        if self.on_wait:
            self.on_wait(self)


class FakePromptNode:
    def __init__(self, text, in_composer=False):
        self.text = text
        self.in_composer = in_composer

    def is_visible(self):
        return True

    def inner_text(self):
        return self.text

    def evaluate(self, expression):
        return self.in_composer


class FakePromptMatches:
    def __init__(self, nodes):
        self.nodes = nodes

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


def test_wait_for_submission_requires_the_new_user_message_to_appear():
    settings = BrowserSettings(submission_timeout=0.02, poll_interval=0.001)
    page = FakeSubmissionPage(
        on_wait=lambda current: current.messages.append("Make a cat\nnow")
    )
    browser = VibeWorkBrowser(settings)
    browser._page = page

    browser._wait_for_submission("Make a cat now", previous_user_count=0)


def test_wait_for_submission_accepts_visible_prompt_without_role_attribute():
    settings = BrowserSettings(submission_timeout=0.02, poll_interval=0.001)
    page = FakeSubmissionPage(
        on_wait=lambda current: current.visible_texts.append("Read the synthetic PDF")
    )
    browser = VibeWorkBrowser(settings)
    browser._page = page

    browser._wait_for_submission(
        "Read the synthetic PDF", previous_user_count=0, previous_prompt_count=0
    )


def test_visible_prompt_in_composer_is_not_submission_evidence():
    browser = VibeWorkBrowser()

    class ComposerPromptPage:
        def get_by_text(self, text, exact=False):
            return FakePromptMatches([FakePromptNode(text, in_composer=True)])

    browser._page = ComposerPromptPage()
    assert browser._visible_prompt_count("Read the synthetic PDF") == 0


def test_unconfirmed_submission_is_an_explicit_error_without_retry():
    settings = BrowserSettings(submission_timeout=0.003, poll_interval=0.001)
    browser = VibeWorkBrowser(settings)
    browser._page = FakeSubmissionPage()

    with pytest.raises(SubmissionNotConfirmed, match="did not resubmit it"):
        browser._wait_for_submission("hello", previous_user_count=0)


def test_recovery_is_not_triggered_for_ordinary_errors():
    error = ValueError("plain failure")
    assert _map_bridge_error(error).status_code == 500
