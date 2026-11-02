from __future__ import annotations


import os
import re
import time
import hashlib
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit
from .documents import DocumentAttachment
from .images import ImageAttachment, sniff_mime
from .modes import ResponseMode

Attachment = ImageAttachment | DocumentAttachment


class BridgeError(RuntimeError):
    """Base class for errors that can be safely returned by the local API."""


class BrowserUnavailable(BridgeError):
    pass


class AuthenticationRequired(BridgeError):
    pass


class CloudflareChallenge(BridgeError):
    """Mistral's edge protection is showing a bot-verification page."""


class TermsConsentRequired(BridgeError):
    """The Vibe web app is waiting for the account holder to accept its terms."""


class WorkUiNotFound(BridgeError):
    pass


class ModeSwitchFailed(BridgeError):
    pass


class ResponseTimeout(BridgeError):
    pass


class SubmissionNotConfirmed(BridgeError):
    """Work did not visibly show the submitted user message."""


class ApprovalWaitTimeout(BridgeError):
    pass


class AttachmentUploadFailed(BridgeError):
    """A reference image could not be confirmed inside the Work composer."""


class NewConversationFailed(BridgeError):
    """The Work UI did not open a fresh conversation."""


class ComposerDraftConflict(BridgeError):
    """The composer contains content the bridge cannot safely replace."""


SYSTEM_CHROMIUM_CANDIDATES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
)

_CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "verifying you are human",
    "checking your browser",
)

_CHALLENGE_BODY = (
    "performing security verification",
    "verify you are not a bot",
    "verify you are human",
    "checking the site connection",
    "enable javascript and cookies to continue",
)

_TERMS_BODY = (
    "you must accept our terms",
    "accept and continue",
    "terms of service and privacy policy",
)

_LOGIN_HOSTS = ("auth.mistral.ai", "login.mistral.ai")
_LOGIN_PATHS = ("/login", "/signin", "/sign-in", "/auth/")


def resolve_chromium_executable(explicit: str | None = None) -> str | None:
    """Prefer a real Chromium/Chrome build over Playwright's bundled one.

    Mistral's edge protection keeps challenging Playwright's bundled Chromium
    (its automation fingerprint), while a normal system browser build passes the
    check. Set ``VIBE_WORK_CHROMIUM`` to point at a specific binary.
    """
    if explicit:
        return explicit
    env_override = os.environ.get("VIBE_WORK_CHROMIUM")
    if env_override:
        return env_override
    for candidate in SYSTEM_CHROMIUM_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


@dataclass(frozen=True)
class BrowserSettings:
    base_url: str = "https://chat.mistral.ai/"
    profile_dir: Path = Path.home() / ".vibe-work-bridge" / "profile"
    headless: bool = False
    response_timeout: float = 300.0
    approval_wait: float = 300.0
    upload_timeout: float = 120.0
    image_settle_timeout: float = 45.0
    reset_input: bool = True
    poll_interval: float = 0.7
    entry_timeout: float = 25.0
    chromium_executable: str | None = None
    viewport_width: int = 1440
    viewport_height: int = 950
    assistant_selector: str = (
        '[data-message-author-role="assistant"], '
        '[data-testid*="assistant-message"]'
    )
    submission_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "BrowserSettings":
        profile_dir = Path(
            os.environ.get(
                "VIBE_WORK_PROFILE_DIR",
                str(Path.home() / ".vibe-work-bridge" / "profile"),
            )
        ).expanduser()
        return cls(
            base_url=os.environ.get("VIBE_WORK_URL", "https://chat.mistral.ai/"),
            profile_dir=profile_dir,
            headless=os.environ.get("VIBE_WORK_HEADLESS", "0").strip().lower()
            in {"1", "true", "yes", "on"},
            response_timeout=float(os.environ.get("VIBE_WORK_TIMEOUT", "300")),
            submission_timeout=float(
                os.environ.get("VIBE_WORK_SUBMISSION_TIMEOUT", "15")
            ),
            approval_wait=float(os.environ.get("VIBE_WORK_APPROVAL_WAIT", "300")),
            upload_timeout=float(os.environ.get("VIBE_WORK_UPLOAD_TIMEOUT", "120")),
            image_settle_timeout=float(
                os.environ.get("VIBE_WORK_IMAGE_SETTLE_TIMEOUT", "45")
            ),
            reset_input=os.environ.get("VIBE_WORK_RESET_INPUT", "1").strip().lower()
            not in {"0", "false", "no", "off"},
            entry_timeout=float(os.environ.get("VIBE_WORK_ENTRY_TIMEOUT", "25")),
            chromium_executable=os.environ.get("VIBE_WORK_CHROMIUM") or None,
            viewport_width=int(os.environ.get("VIBE_WORK_VIEWPORT_WIDTH", "1440")),
            viewport_height=int(os.environ.get("VIBE_WORK_VIEWPORT_HEIGHT", "950")),
            assistant_selector=os.environ.get(
                "VIBE_WORK_ASSISTANT_SELECTOR",
                '[data-message-author-role="assistant"], '
                '[data-testid*="assistant-message"]',
            ),
        )


class VibeWorkBrowser:
    """Operate Vibe Work through its visible UI; no private HTTP endpoints."""

    _MODE_ROLES = ("button", "radio", "tab", "menuitemradio", "menuitemcheckbox", "menuitem")
    _COMPOSER_SELECTORS = (
        'textarea[placeholder*="message" i]',
        'textarea[placeholder*="ask" i]',
        'textarea',
        '[contenteditable="true"][data-placeholder]',
        '[contenteditable="true"]',
        '[role="textbox"][contenteditable="true"]',
        '[contenteditable="true"][role="textbox"]',
        '[role="textbox"]',
    )

    def __init__(self, settings: BrowserSettings | None = None):
        self.settings = settings or BrowserSettings.from_env()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- page state helpers -------------------------------------------------

    def _page_text(self, limit: int = 4000) -> str:
        try:
            return self._page.locator("body").inner_text(timeout=1500)
        except Exception:
            return ""

    def _page_title(self) -> str:
        try:
            return (self._page.title() or "").lower()
        except Exception:
            return ""

    def _challenge_visible(self) -> bool:
        title = self._page_title()
        if any(term in title for term in _CHALLENGE_TITLES):
            return True
        body = self._page_text().lower()
        return any(term in body for term in _CHALLENGE_BODY)

    def _terms_visible(self) -> bool:
        body = self._page_text().lower()
        if not body:
            return False
        if "you must accept our terms" in body or "accept and continue" in body:
            return True
        return (
            "terms of service" in body
            and "privacy policy" in body
            and "before you continue" in body
        )

    def _login_url_visible(self) -> bool:
        url = ""
        try:
            url = (self._page.url or "").lower()
        except Exception:
            return False
        host = urlparse(url).hostname or ""
        if any(host == login_host for login_host in _LOGIN_HOSTS):
            return True
        return any(path in url for path in _LOGIN_PATHS)

    # -- lifecycle ---------------------------------------------------------

    def _ensure_started(self) -> None:
        if self._page is not None and not self._page.is_closed():
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed. Install the project dependencies and run `playwright install chromium`."
            ) from exc

        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.settings.profile_dir.chmod(0o700)
        except OSError:
            pass

        self._playwright = sync_playwright().start()
        try:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(self.settings.profile_dir),
                "headless": self.settings.headless,
                "viewport": {
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                },
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            executable = resolve_chromium_executable(
                self.settings.chromium_executable
            )
            if executable:
                launch_kwargs["executable_path"] = executable
            self._context = self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
            self._page.set_default_timeout(5000)
            current_host = urlparse(self._page.url or "").hostname
            target_host = urlparse(self.settings.base_url).hostname
            if not current_host or current_host != target_host:
                self._page.goto(
                    self.settings.base_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            self._wait_for_entry()
        except BridgeError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserUnavailable(f"Could not start the Vibe Work browser: {exc}") from exc

    def _wait_for_entry(self) -> None:
        """Wait for Work's editor (or an actionable blocking page) to settle."""
        deadline = time.monotonic() + self.settings.entry_timeout
        while time.monotonic() < deadline:
            if self._find_composer() is not None:
                return
            if self._terms_visible() or self._login_url_visible():
                return
            if self._challenge_visible():
                self._page.wait_for_timeout(1000)
                continue
            # The Work shell can render its text while the composer is still
            # hydrating. Do not report a false missing-composer error during
            # that short cold-start window.
            self._page.wait_for_timeout(500)
        if self._challenge_visible():
            raise CloudflareChallenge(
                "Mistral's edge protection is still showing a bot-verification page. "
                "Open the browser window once, complete the check, and retry. "
                "If it keeps looping, set VIBE_WORK_CHROMIUM to a system Chrome/Chromium binary."
            )

    @staticmethod
    def _exact_mode_pattern(mode: ResponseMode) -> re.Pattern[str]:
        if mode is ResponseMode.FAST:
            return re.compile(r"^\s*Fast(?:\s+mode)?\s*$", re.IGNORECASE)
        return re.compile(r"^\s*(?:Think|Thinking)(?:\s+mode)?\s*$", re.IGNORECASE)

    @staticmethod
    def _menu_mode_pattern(mode: ResponseMode) -> re.Pattern[str]:
        # The current Vibe menu includes a short explanation after each choice.
        label = "Fast" if mode is ResponseMode.FAST else "Think"
        return re.compile(rf"^\s*{label}(?:\s+.+)?\s*$", re.IGNORECASE)

    @staticmethod
    def _selected_state(locator: Any) -> bool | None:
        for attribute in ("aria-pressed", "aria-checked", "aria-selected", "data-state"):
            try:
                value = locator.get_attribute(attribute)
            except Exception:
                value = None
            if value is None:
                continue
            normalized = value.strip().lower()
            if normalized in {"true", "on", "active", "selected", "checked"}:
                return True
            if normalized in {"false", "off", "inactive", "unselected", "unchecked"}:
                return False
        return None

    @staticmethod
    def _iter_visible(locators: Any):
        try:
            count = locators.count()
        except Exception:
            return
        for index in range(count):
            locator = locators.nth(index)
            try:
                if locator.is_visible() and locator.is_enabled():
                    yield locator
            except Exception:
                continue

    def _mode_candidates(self, mode: ResponseMode):
        for role in self._MODE_ROLES:
            pattern = (
                self._menu_mode_pattern(mode)
                if role == "menuitem"
                else self._exact_mode_pattern(mode)
            )
            try:
                locators = self._page.get_by_role(role, name=pattern)
            except Exception:
                continue
            yield from self._iter_visible(locators)

    def _visible_mode_controls(self) -> list[tuple[str, Any]]:
        found: list[tuple[str, Any]] = []
        for mode in (ResponseMode.FAST, ResponseMode.THINK):
            for locator in self._mode_candidates(mode):
                found.append((mode.value, locator))
        return found

    def _current_mode(self) -> ResponseMode | None:
        # Current Vibe Work exposes the active mode as text inside its
        # aria-labelled "Choose model" menu trigger.
        try:
            chooser = self._page.get_by_role(
                "button", name=re.compile(r"^\s*Choose model\s*$", re.IGNORECASE)
            )
            visible_choosers = list(self._iter_visible(chooser))
            if len(visible_choosers) == 1:
                label = " ".join(visible_choosers[0].inner_text().split()).lower()
                if label == "fast":
                    return ResponseMode.FAST
                if label in {"think", "thinking"}:
                    return ResponseMode.THINK
        except Exception:
            pass

        controls = self._visible_mode_controls()
        selected = [
            mode for mode, locator in controls if self._selected_state(locator) is True
        ]
        if len(selected) == 1:
            return ResponseMode.parse(selected[0])
        if selected:
            return None

        # Some versions render one button whose accessible name is the active mode.
        names = {mode for mode, _ in controls}
        if len(names) == 1:
            return ResponseMode.parse(next(iter(names)))
        return None

    def _click_visible_mode(self, mode: ResponseMode) -> bool:
        candidates = list(self._mode_candidates(mode))
        if not candidates:
            return False
        for locator in candidates:
            if self._selected_state(locator) is True:
                return True
        candidates[0].click()
        self._page.wait_for_timeout(250)
        return self._current_mode() is mode

    def _switch_mode(self, mode: ResponseMode) -> None:
        if self._current_mode() is mode:
            return
        if self._click_visible_mode(mode):
            return

        # The current Work UI uses one labelled menu trigger whose text is the
        # active mode; its menu contains Fast/Think entries with descriptions.
        try:
            chooser = self._page.get_by_role(
                "button", name=re.compile(r"^\s*Choose model\s*$", re.IGNORECASE)
            )
            visible_choosers = list(self._iter_visible(chooser))
            if len(visible_choosers) == 1:
                visible_choosers[0].click()
                self._page.wait_for_timeout(200)
                if self._click_visible_mode(mode):
                    return
        except Exception:
            pass

        # In some UI versions the visible mode button opens a menu containing the
        # other choice. Click only the visible Fast/Think control, then re-check.
        controls = self._visible_mode_controls()
        trigger = next((locator for _, locator in controls if self._selected_state(locator) is True), None)
        if trigger is None and len(controls) == 1:
            trigger = controls[0][1]
        if trigger is not None:
            trigger.click()
            self._page.wait_for_timeout(200)
            if self._click_visible_mode(mode):
                return

        raise ModeSwitchFailed(
            "Could not verify the Vibe Work Fast/Think selection. Check that the page is in Work mode and that its visible mode toggle is available."
        )

    def _find_composer(self):
        for selector in self._COMPOSER_SELECTORS:
            try:
                locators = self._page.locator(selector)
            except Exception:
                continue
            for locator in self._iter_visible(locators):
                return locator
        return None

    def _composer_text(self, composer: Any) -> str:
        """Read the editor without changing it; fail closed if its state is unknown."""
        for method_name in ("input_value", "inner_text"):
            try:
                method = getattr(composer, method_name)
                try:
                    value = method(timeout=1000)
                except TypeError:
                    value = method()
                return " ".join((value or "").split())
            except Exception:
                continue
        raise ComposerDraftConflict(
            "Could not verify the Work composer contents. The bridge left the draft "
            "untouched and will not replace it. Check the browser window and retry."
        )

    def _composer_has_attachments(self, composer: Any, editor_text: str) -> bool:
        """Detect attachment previews/chips outside the editable text area."""
        card = self._composer_card(composer)
        try:
            if card.locator("img").count() > 0:
                return True
        except Exception as exc:
            raise ComposerDraftConflict(
                "Could not verify whether the Work composer contains attachments. "
                "The bridge left the draft untouched and will not replace it."
            ) from exc

        try:
            residual = card.evaluate(
                """(root) => {
                    const clone = root.cloneNode(true);
                    clone.querySelectorAll(
                        'textarea, input, [contenteditable="true"], [role="textbox"], button, [role="button"], [role="dialog"], [hidden]'
                    ).forEach((node) => node.remove());
                    return clone.innerText || clone.textContent || '';
                }"""
            )
        except Exception:
            try:
                residual = card.inner_text(timeout=1000)
            except Exception as exc:
                raise ComposerDraftConflict(
                    "Could not verify whether the Work composer contains attachments. "
                    "The bridge left the draft untouched and will not replace it."
                ) from exc
            lines = [" ".join(line.split()) for line in (residual or "").splitlines()]
            controls = {
                "send",
                "send message",
                "submit",
                "fast",
                "think",
                "upload files",
                "open settings menu",
            }
            lines = [line for line in lines if line and line.lower() not in controls]
            residual = " ".join(lines)

        residual = " ".join((residual or "").split())
        if editor_text:
            residual = residual.replace(editor_text, " ", 1)
        return bool(" ".join(residual.split()))

    def _assert_composer_text_empty(self, composer: Any) -> None:
        if self._composer_text(composer):
            raise ComposerDraftConflict(
                "The Work composer already contains a draft. The bridge left it "
                "untouched and will not overwrite it; clear or save it manually, "
                "then retry."
            )

    def _assert_composer_clean(self, composer: Any) -> None:
        editor_text = self._composer_text(composer)
        if editor_text or self._composer_has_attachments(composer, editor_text):
            raise ComposerDraftConflict(
                "The Work composer already contains text or attachments that the "
                "bridge cannot verify as its own. The bridge left the draft "
                "untouched and will not clear or replace it; clear or save it "
                "manually, then retry."
            )

    def _ensure_work_composer(self):
        composer = self._find_composer()
        if composer is not None:
            return composer

        if self._challenge_visible():
            raise CloudflareChallenge(
                "Mistral's edge protection is showing a bot-verification page instead of the Work UI."
            )
        if self._terms_visible():
            raise TermsConsentRequired(
                "The Vibe web app is waiting for its Terms of Service to be accepted in the bridge browser. "
                "The bridge will not accept legal terms on your behalf; accept them once in that window."
            )
        if self._login_url_visible():
            raise AuthenticationRequired(
                "Sign in to Mistral in the opened browser, select Work, and retry the request."
            )
        body = self._page_text(1500).lower()
        if any(term in body for term in ("sign in to", "log in to", "continue with google")):
            raise AuthenticationRequired(
                "Sign in to Mistral in the opened browser, select Work, and retry the request."
            )
        raise WorkUiNotFound(
            "The Vibe Work composer was not found. Confirm the browser is signed in and the Work tab is selected."
        )

    def _latest_assistant_text(self) -> str:
        try:
            locator = self._page.locator(self.settings.assistant_selector)
            texts = locator.all_inner_texts()
        except Exception:
            return ""
        for text in reversed(texts):
            normalized = (text or "").strip()
            if normalized:
                return normalized
        return ""

    def _assistant_count(self) -> int:
        try:
            return self._page.locator(self.settings.assistant_selector).count()
        except Exception:
            return 0

    def _user_message_count(self) -> int:
        try:
            return self._page.locator(
                '[data-message-author-role="user"]'
            ).count()
        except Exception:
            return 0

    def _user_message_texts(self) -> list[str]:
        try:
            return self._page.locator(
                '[data-message-author-role="user"]'
            ).all_inner_texts()
        except Exception:
            return []

    def _visible_prompt_count(self, prompt: str) -> int:
        """Count exact visible prompt text outside the editable composer.

        Work has changed its message DOM more than once. The role attribute is
        the preferred signal, but this accessible-text fallback lets the bridge
        confirm a visibly rendered user turn when that attribute is absent.
        """
        normalized_prompt = " ".join(prompt.split())
        if not normalized_prompt:
            return 0
        try:
            matches = self._page.get_by_text(normalized_prompt, exact=True)
            count = matches.count()
        except Exception:
            return 0

        visible_matches = 0
        for index in range(count):
            node = matches.nth(index)
            try:
                if not node.is_visible():
                    continue
                text = " ".join((node.inner_text() or "").split())
                if text != normalized_prompt:
                    continue
                in_editor = node.evaluate(
                    """(element) => Boolean(element.closest(
                        '[contenteditable="true"], textarea, [role="textbox"]'
                    ))"""
                )
                if not in_editor:
                    visible_matches += 1
            except Exception:
                continue
        return visible_matches

    def _last_assistant_node(self):
        """The newest assistant message element, used to read images too."""
        try:
            nodes = self._page.locator(self.settings.assistant_selector)
            count = nodes.count()
        except Exception:
            return None
        for index in reversed(range(count)):
            node = nodes.nth(index)
            try:
                if node.inner_text().strip():
                    return node
            except Exception:
                continue
        return None

    def _approval_is_visible(self) -> bool:
        try:
            dialogs = self._page.get_by_role("dialog")
            for dialog in self._iter_visible(dialogs):
                text = dialog.inner_text(timeout=1000).lower()
                if "continue" in text and ("decline" in text or "always allow" in text):
                    return True
        except Exception:
            pass
        return False

    def _is_generating(self) -> bool:
        for selector in (
            'button[aria-label*="stop" i]',
            'button:has-text("Stop")',
        ):
            try:
                for _locator in self._iter_visible(self._page.locator(selector)):
                    return True
            except Exception:
                continue
        return False

    def _fill_composer(self, composer: Any, prompt: str) -> Any:
        """Type the prompt, re-locating the editor if Work re-renders it."""
        try:
            composer.fill(prompt, timeout=5000)
            return composer
        except Exception:
            composer = self._find_composer()
            if composer is None:
                raise WorkUiNotFound(
                    "The Work composer disappeared while the prompt was being entered."
                )
            try:
                composer.fill(prompt, timeout=5000)
                return composer
            except Exception as exc:
                raise WorkUiNotFound(
                    f"Could not enter the prompt in the Work composer: {exc}"
                ) from exc

    def _click_send_button(self) -> bool:
        patterns = (
            re.compile(r"^\s*(?:Send|Send message|Submit)\s*$", re.IGNORECASE),
            re.compile(r"send", re.IGNORECASE),
        )
        for pattern in patterns:
            try:
                buttons = self._page.get_by_role("button", name=pattern)
            except Exception:
                continue
            for button in self._iter_visible(buttons):
                try:
                    if button.get_attribute("aria-disabled") == "true":
                        continue
                except Exception:
                    pass
                try:
                    button.click()
                except Exception:
                    # The click may have reached Work before Playwright failed.
                    # Treat it as an attempted submission; never try another control.
                    return True
                return True
        for selector in ('button[type="submit"]:visible', 'button[aria-label*="send" i]:visible'):
            try:
                for button in self._iter_visible(self._page.locator(selector)):
                    try:
                        button.click()
                    except Exception:
                        # Its outcome is ambiguous, so confirmation—not a retry—
                        # decides whether the request was submitted.
                        return True
                    return True
            except Exception:
                continue
        return False

    def _submit(self, composer: Any, prompt: str) -> None:
        # Re-read the live editor immediately before fill(): a previous check or
        # an upload may have been followed by a user-edited draft.
        composer = self._find_composer() or composer
        self._assert_composer_text_empty(composer)
        composer = self._fill_composer(composer, prompt)
        if self._click_send_button():
            return

        # Work's editor replaces its node after typing, so re-locate before the
        # single keyboard submission attempt.
        target = self._find_composer() or composer
        try:
            target.press("Enter", timeout=4000)
        except Exception:
            # An exception may occur after the key reached the page. Do not send
            # Enter again through a different target; the caller verifies the UI.
            return

    def _wait_for_submission(
        self,
        prompt: str,
        previous_user_count: int,
        previous_prompt_count: int | None = None,
    ) -> None:
        """Require a new visible user turn before waiting for an answer."""
        normalized_prompt = " ".join(prompt.split())
        if previous_prompt_count is None:
            previous_prompt_count = self._visible_prompt_count(prompt)
        deadline = time.monotonic() + self.settings.submission_timeout
        while time.monotonic() < deadline:
            messages = self._user_message_texts()
            if len(messages) > previous_user_count:
                new_messages = messages[previous_user_count:]
                if any(
                    normalized_prompt in " ".join((text or "").split())
                    for text in new_messages
                ):
                    return
            if self._visible_prompt_count(prompt) > previous_prompt_count:
                return
            self._page.wait_for_timeout(
                int(self.settings.poll_interval * 1000)
            )
        raise SubmissionNotConfirmed(
            "Work did not visibly confirm the submitted user message. The bridge "
            "did not resubmit it. Check the conversation before sending again."
        )

    def _wait_for_response(
        self,
        previous_assistant_count: int,
        expect_images: bool = False,
    ) -> tuple[str, list[dict[str, Any]]]:
        started = time.monotonic()
        approval_started: float | None = None
        last_text = ""
        stable_polls = 0
        image_wait_started: float | None = None
        while time.monotonic() - started < self.settings.response_timeout:
            now = time.monotonic()
            if self._approval_is_visible():
                if approval_started is None:
                    approval_started = now
                if now - approval_started > self.settings.approval_wait:
                    raise ApprovalWaitTimeout(
                        "Work is waiting for a human approval in the browser. The bridge did not approve it; approve or decline in Work and submit a new request."
                    )
                self._page.wait_for_timeout(int(self.settings.poll_interval * 1000))
                continue
            approval_started = None

            current = self._latest_assistant_text()
            # A new assistant message counts even when its text repeats the
            # previous answer word for word.
            new_reply = self._assistant_count() > previous_assistant_count
            if current and new_reply:
                if current == last_text and not self._is_generating():
                    stable_polls += 1
                    if stable_polls >= 3:
                        images = self._extract_images()
                        if images or not expect_images:
                            return current, images
                        # Text can settle a moment before an image finishes
                        # rendering, so keep a short, bounded extra wait.
                        if image_wait_started is None:
                            image_wait_started = now
                        if now - image_wait_started > self.settings.image_settle_timeout:
                            return current, []
                else:
                    stable_polls = 0
                    image_wait_started = None
                last_text = current
            else:
                stable_polls = 0
            self._page.wait_for_timeout(int(self.settings.poll_interval * 1000))

        raise ResponseTimeout(
            "Timed out waiting for a stable Work response. The task may still be running in the browser."
        )

    # -- attachments, conversations and images -----------------------------

    _NEW_CHAT_PATTERN = re.compile(
        r"^\s*(?:(?:start|create)(?:\s+a)?\s+)?new\s+(?:chat|conversation)(?:\b.*)?$",
        re.IGNORECASE,
    )
    _UPLOAD_BUSY_PATTERN = re.compile(
        r"uploading|processing file|preparing file|still uploading", re.IGNORECASE
    )
    _IMAGE_NOISE = ("avatar", "favicon", "logo", "emoji", "icon", "sprite")

    def _composer_card(self, composer: Any):
        """The card wrapping the composer; attachment chips render inside it."""
        for selector in (
            "xpath=ancestor::div[contains(@class, 'click-focus-input')][1]",
            "xpath=ancestor::form[1]",
            "xpath=ancestor::div[3]",
        ):
            try:
                card = composer.locator(selector)
                if card.count():
                    return card.first
            except Exception:
                continue
        return composer

    def _file_input(self):
        """Return the first file input for session diagnostics only.

        Work's hidden input can accept programmatic files without starting its
        upload flow, so attachment uploads must use the visible menu chooser.
        """
        try:
            inputs = self._page.locator('input[type="file"]')
            if inputs.count():
                return inputs.first
        except Exception:
            pass
        return None

    def _file_input_accept(self) -> list[str]:
        """Read the accept attributes Work advertises, without uploading anything."""
        values: list[str] = []
        try:
            inputs = self._page.locator('input[type="file"]')
            for index in range(min(inputs.count(), 5)):
                accept = inputs.nth(index).get_attribute("accept")
                if accept:
                    values.append(accept.strip()[:400])
                else:
                    values.append("")
        except Exception:
            return values
        return values

    @staticmethod
    def _document_upload_hint(accept_values: list[str]) -> str | None:
        """Say whether the file input looks open to documents, images only, or unknown."""
        if not accept_values:
            return "no_file_input"
        combined = " ".join(accept_values).lower()
        if not combined.strip():
            return "any_type"
        tokens = [token for token in re.split(r"[,\s]+", combined) if token]
        document_tokens = (
            "application/pdf",
            "text/plain",
            ".pdf",
            ".txt",
            ".docx",
            "wordprocessingml",
            "application/msword",
            "text/",
            "application/",
        )
        if any(any(marker in token for marker in document_tokens) for token in tokens):
            return "documents_advertised"
        if all(token.startswith("image/") or token.startswith(".") for token in tokens):
            return "images_only"
        return "unknown"

    def _open_composer_menu(self) -> bool:
        try:
            upload_item = self._page.get_by_text(
                re.compile(r"^\s*Upload Files\s*$", re.IGNORECASE)
            )
            if next(self._iter_visible(upload_item), None) is not None:
                return True
        except Exception:
            pass
        try:
            trigger = self._page.get_by_role(
                "button", name=re.compile(r"^\s*Open settings menu\s*$", re.IGNORECASE)
            )
            for button in self._iter_visible(trigger):
                button.click()
                self._page.wait_for_timeout(300)
                return True
        except Exception:
            pass
        return False

    def _close_composer_menu(self) -> None:
        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(150)
        except Exception:
            pass

    def _attachment_evidence(self, card: Any) -> tuple[int, str]:
        try:
            count = card.locator("img").count()
        except Exception:
            count = 0
        try:
            text = card.inner_text(timeout=1000)
        except Exception:
            text = ""
        return count, text

    @staticmethod
    def _name_variants(name: str) -> list[str]:
        """Names Work might print on an attachment chip (full name or stem)."""
        lowered = name.lower()
        stem = lowered.rsplit(".", 1)[0] if "." in lowered else lowered
        variants = [lowered]
        if stem and stem != lowered:
            variants.append(stem)
        return variants

    def _wait_for_attachments(self, names: list[str], baseline_images: int) -> None:
        deadline = time.monotonic() + self.settings.upload_timeout
        variants = [self._name_variants(name) for name in names]
        single_prefix = ""
        if len(names) == 1:
            stem = names[0].lower().rsplit(".", 1)[0]
            # Work may shorten a long chip label; accept a 12-character prefix
            # when only one attachment was handed over.
            single_prefix = stem[:12] if len(stem) > 12 else ""
        while time.monotonic() < deadline:
            composer = self._find_composer()
            if composer is not None:
                images, text = self._attachment_evidence(self._composer_card(composer))
                busy = bool(self._UPLOAD_BUSY_PATTERN.search(text))
                lowered = text.lower()
                named = bool(variants) and all(
                    any(alias in lowered for alias in aliases) for aliases in variants
                )
                truncated = bool(single_prefix) and single_prefix.lower() in lowered
                thumbnails = images > baseline_images
                if not busy and (named or truncated or thumbnails) and not self._is_generating():
                    return
            self._page.wait_for_timeout(int(self.settings.poll_interval * 1000))
        raise AttachmentUploadFailed(
            "The attachment was handed to the browser but never appeared in the Work composer. "
            "Check the open browser window for an upload error or an unsupported file type, then retry."
        )

    def reset_input(self) -> None:
        """Preserve unknown drafts; verify the composer is already clean.

        The visible browser profile may contain user-authored text or uploads,
        including after a failed request. The bridge cannot prove ownership of
        that content, so this method intentionally never deletes it.
        """
        try:
            composer = self._ensure_work_composer()
        except WorkUiNotFound:
            return
        self._assert_composer_clean(composer)

    def attach_files(self, attachments: list[Attachment]) -> None:
        """Upload verified images or documents into the current Work composer."""
        if not attachments:
            return
        composer = self._ensure_work_composer()
        baseline_images, _ = self._attachment_evidence(self._composer_card(composer))
        payload = [
            {"name": item.name, "mimeType": item.mime_type, "buffer": item.data}
            for item in attachments
        ]
        if not self._open_composer_menu():
            raise AttachmentUploadFailed(
                "Could not open Work's attachment menu. Confirm the browser window is on the Work composer and retry."
            )

        try:
            try:
                with self._page.expect_file_chooser(timeout=20000) as chooser_info:
                    dialog = self._page.get_by_role("dialog").first
                    upload_item = dialog.get_by_text(
                        re.compile(r"^\s*Upload Files\s*$", re.IGNORECASE)
                    )
                    clicked = False
                    for candidate in self._iter_visible(upload_item):
                        candidate.click()
                        clicked = True
                        break
                    if not clicked:
                        raise AttachmentUploadFailed(
                            "Work's 'Upload Files' menu item is not visible."
                        )
                chooser_info.value.set_files(payload)
            except BridgeError:
                raise
            except Exception as exc:
                raise AttachmentUploadFailed(
                    "Could not open Work's 'Upload Files' file chooser. Confirm the browser window is on the Work composer and retry."
                ) from exc
        finally:
            self._close_composer_menu()

        self._wait_for_attachments([item.name for item in attachments], baseline_images)

    def _new_chat_control(self):
        # Work versions have rendered this as a text button, a compact icon
        # button, and a button whose accessible name includes a shortcut.
        attribute_selectors = (
            'button[aria-label*="new chat" i]',
            'a[aria-label*="new chat" i]',
            '[role="button"][aria-label*="new chat" i]',
            'button[aria-label*="new conversation" i]',
            'a[aria-label*="new conversation" i]',
            '[role="button"][aria-label*="new conversation" i]',
            'button[title*="new chat" i]',
            'a[title*="new chat" i]',
            '[role="button"][title*="new chat" i]',
            'button[title*="new conversation" i]',
            'a[title*="new conversation" i]',
            '[role="button"][title*="new conversation" i]',
            'button[data-testid*="new-chat" i]',
            'a[data-testid*="new-chat" i]',
            '[role="button"][data-testid*="new-chat" i]',
            'button[data-testid*="new-conversation" i]',
            'a[data-testid*="new-conversation" i]',
            '[role="button"][data-testid*="new-conversation" i]',
        )
        for selector in attribute_selectors:
            try:
                locators = self._page.locator(selector)
            except Exception:
                continue
            for item in self._iter_visible(locators):
                return item

        for role in ("link", "button"):
            try:
                locator = self._page.get_by_role(role, name=self._NEW_CHAT_PATTERN)
            except Exception:
                continue
            for item in self._iter_visible(locator):
                return item
        try:
            label = self._page.get_by_text(
                self._NEW_CHAT_PATTERN
            )
            for item in self._iter_visible(label):
                clickable = item.locator(
                    "xpath=ancestor-or-self::*[self::a or self::button or @role='button'][1]"
                )
                if clickable.count():
                    return clickable.first
                return item
        except Exception:
            pass
        return None

    def _message_count(self) -> int:
        # Count the union rather than adding overlapping selectors: assistant
        # messages commonly match both selectors and must not be double-counted.
        selector = (
            f"[data-message-author-role], {self.settings.assistant_selector}"
        )
        try:
            return self._page.locator(selector).count()
        except Exception:
            counts = []
            for fallback in (
                "[data-message-author-role]",
                self.settings.assistant_selector,
            ):
                try:
                    counts.append(self._page.locator(fallback).count())
                except Exception:
                    continue
            return max(counts, default=0)

    @staticmethod
    def _conversation_id(url: str) -> str | None:
        segments = [part for part in urlparse(url or "").path.split("/") if part]
        if not segments:
            return None
        last = segments[-1]
        if last.lower() in {"work", "chat", "chats", "new", "code"}:
            return None
        return last

    def current_conversation(self) -> dict[str, Any]:
        self._ensure_started()
        return self.conversation_state()

    def conversation_state(self) -> dict[str, Any]:
        url = ""
        try:
            url = self._page.url or ""
        except Exception:
            pass
        count = self._message_count()
        return {
            "id": self._conversation_id(url),
            "url": url,
            "empty": count == 0,
            "message_count": count,
        }

    def start_new_conversation(self, *, reuse_empty: bool = True) -> dict[str, Any]:
        """Open a distinct conversation and verify its identity and clean composer."""
        self._ensure_started()
        composer = self._ensure_work_composer()
        self._assert_composer_clean(composer)
        before = self.conversation_state()
        if (
            reuse_empty
            and before.get("id")
            and before.get("message_count") == 0
        ):
            state = before
            return {**state, "created": False, "reused": True}

        control = self._new_chat_control()
        if control is None:
            raise NewConversationFailed(
                "Could not find Work's 'New Chat' control in the sidebar. Expand the sidebar or check that the page is on Work."
            )
        try:
            control.click()
        except Exception as exc:
            raise NewConversationFailed(
                f"Could not click Work's 'New Chat' control: {exc}"
            ) from exc

        deadline = time.monotonic() + self.settings.entry_timeout
        while time.monotonic() < deadline:
            composer = self._find_composer()
            if composer is not None:
                state = self.conversation_state()
                if (
                    state.get("id")
                    and state.get("id") != before.get("id")
                    and state.get("message_count") == 0
                ):
                    self._assert_composer_clean(composer)
                    return {**state, "created": True, "reused": False}
            self._page.wait_for_timeout(500)
        raise NewConversationFailed(
            "Work did not prove that 'New Chat' opened a distinct conversation with "
            "an empty message area and clean composer (a changed conversation ID "
            "is required). The bridge will not send into an unverified chat. Check "
            "the browser window and retry."
        )

    def _images_in_node(self, node: Any) -> list[dict[str, Any]]:
        try:
            raw = node.evaluate(
                """(root) => [...root.querySelectorAll('img')].map((element) => ({
                    src: element.currentSrc || element.src || '',
                    width: element.naturalWidth || Math.round(element.getBoundingClientRect().width),
                    height: element.naturalHeight || Math.round(element.getBoundingClientRect().height),
                    alt: element.getAttribute('alt') || '',
                }))"""
            )
        except Exception:
            return []
        found: list[dict[str, Any]] = []
        for item in raw or []:
            src = (item.get("src") or "").strip()
            if not src:
                continue
            lowered = src.lower()
            if lowered.startswith("data:image/svg"):
                continue
            # Only URLs are screened for chrome/UI assets: a data: URL is the
            # image payload itself, and its base64 can legitimately contain
            # words like "icon" or "logo".
            if not lowered.startswith("data:") and any(
                token in lowered for token in self._IMAGE_NOISE
            ):
                continue
            if int(item.get("width") or 0) < 200 or int(item.get("height") or 0) < 200:
                continue
            found.append({**item, "src": src})
        return found

    def _latest_assistant_images(self) -> list[dict[str, Any]]:
        node = self._last_assistant_node()
        if node is None:
            return []
        return self._images_in_node(node)

    @staticmethod
    def _image_key(src: str) -> str:
        return hashlib.sha1(src.encode("utf-8", "ignore")).hexdigest()

    @staticmethod
    def _original_image_url(src: str) -> str | None:
        """Unwrap Cloudflare Image Resizing URLs to their original image URL."""
        outer = urlsplit(src)
        marker = "/cdn-cgi/image/"
        marker_at = outer.path.lower().find(marker)
        if marker_at < 0:
            return None

        # Cloudflare places the source URL after its image options in the path.
        # Decode the path first because some pages percent-encode the full URL.
        remainder = unquote(outer.path[marker_at + len(marker):])
        lowered = remainder.lower()
        starts = [position for scheme in ("https://", "http://")
                  if (position := lowered.find(scheme)) >= 0
                  and (position == 0 or remainder[position - 1] == "/")]
        if not starts:
            return None

        source = urlsplit(remainder[min(starts):])
        if source.scheme.lower() not in {"http", "https"} or not source.netloc:
            return None

        # In the normal Cloudflare form, the original URL's query (e.g. Azure
        # Blob SAS parameters) remains the outer URL's query string.
        query = source.query
        if outer.query:
            query = f"{query}&{outer.query}" if query else outer.query
        return urlunsplit((source.scheme, source.netloc, source.path, query, source.fragment))

    def _fetch_image_bytes(self, src: str) -> tuple[bytes, str] | None:
        """Read one rendered image; uses the page for blobs and its cookies for URLs."""
        if src.startswith("data:"):
            header, _, payload = src.partition(",")
            try:
                data = base64.b64decode(payload)
            except Exception:
                return None
            mime = sniff_mime(data) or header[5:].split(";")[0] or "image/png"
            return data, mime
        if src.startswith("blob:"):
            try:
                encoded = self._page.evaluate(
                    """async (source) => {
                        const response = await fetch(source);
                        const buffer = await response.arrayBuffer();
                        const bytes = new Uint8Array(buffer);
                        let binary = '';
                        for (let index = 0; index < bytes.length; index += 1) {
                            binary += String.fromCharCode(bytes[index]);
                        }
                        return {mime: response.headers.get('content-type') || '', b64: btoa(binary)};
                    }""",
                    src,
                )
                data = base64.b64decode(encoded["b64"])
            except Exception:
                return None
            mime = sniff_mime(data) or (encoded.get("mime") or "image/png")
            return data, mime
        if src.startswith(("http://", "https://")):
            original_url = self._original_image_url(src)
            urls = [original_url, src] if original_url and original_url != src else [src]
            response = None
            for url in urls:
                try:
                    candidate = self._context.request.get(url, timeout=60000)
                except Exception:
                    continue
                if candidate.ok:
                    response = candidate
                    break
            if response is None:
                return None
            data = response.body()
            declared = (response.headers.get("content-type") or "").split(";")[0].strip()
            mime = sniff_mime(data) or declared or "image/png"
            return data, mime
        return None

    def _extract_images(self) -> list[dict[str, Any]]:
        """Read images from the newest assistant message, de-duplicated by content."""
        extracted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self._latest_assistant_images():
            key = self._image_key(item["src"])
            if key in seen:
                continue
            fetched = self._fetch_image_bytes(item["src"])
            if fetched is None:
                continue
            data, mime_type = fetched
            if not data or not mime_type.startswith("image/"):
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            seen.add(key)
            extracted.append(
                {
                    "key": digest,
                    "source_url": item["src"],
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                    "mime_type": mime_type,
                    "data": data,
                }
            )
        return extracted

    # -- public API --------------------------------------------------------

    def inspect_session(self) -> dict[str, Any]:
        """Report the browser's state without sending anything to Work."""
        self._ensure_started()
        state: dict[str, Any] = {
            "url": "",
            "title": "",
            "signed_in": False,
            "work_composer_found": False,
            "terms_consent_required": False,
            "cloudflare_challenge": False,
            "mode": None,
            "page_hint": "",
        }
        try:
            state["url"] = self._page.url
            state["title"] = self._page.title()
        except Exception:
            pass
        state["cloudflare_challenge"] = self._challenge_visible()
        state["terms_consent_required"] = self._terms_visible()
        state["work_composer_found"] = self._find_composer() is not None
        composer_candidates: list[dict[str, Any]] = []
        for selector in self._COMPOSER_SELECTORS:
            try:
                locators = self._page.locator(selector)
                count = locators.count()
                visible = sum(
                    1 for index in range(count)
                    if locators.nth(index).is_visible()
                )
                composer_candidates.append(
                    {"selector": selector, "count": count, "visible": visible}
                )
            except Exception:
                composer_candidates.append(
                    {"selector": selector, "count": 0, "visible": 0}
                )
        state["composer_candidates"] = composer_candidates
        if not state["work_composer_found"]:
            state["page_hint"] = " ".join(self._page_text(2000).split())[:300]
        if state["work_composer_found"]:
            state["signed_in"] = True
            current = self._current_mode()
            state["mode"] = current.value if current else None
            state["conversation"] = self.conversation_state()
        state["file_input_found"] = self._file_input() is not None
        state["file_input_accept"] = self._file_input_accept()
        state["document_upload_hint"] = self._document_upload_hint(state["file_input_accept"])
        state["new_chat_control_found"] = self._new_chat_control() is not None
        return state

    def send_message(
        self,
        prompt: str,
        mode: ResponseMode,
        attachments: list[Attachment] | None = None,
        new_conversation: bool = False,
        expect_images: bool = False,
    ) -> dict[str, Any]:
        self._ensure_started()
        conversation: dict[str, Any] | None = None
        if new_conversation:
            conversation = self.start_new_conversation(reuse_empty=False)
        composer = self._ensure_work_composer()
        self._assert_composer_clean(composer)
        self._switch_mode(mode)
        if self.settings.reset_input:
            self.reset_input()
        if attachments:
            self.attach_files(attachments)
            composer = self._ensure_work_composer()
        previous_assistant_count = self._assistant_count()
        previous_user_count = self._user_message_count()
        previous_prompt_count = self._visible_prompt_count(prompt)
        self._submit(composer, prompt)
        self._wait_for_submission(
            prompt, previous_user_count, previous_prompt_count
        )
        text, images = self._wait_for_response(
            previous_assistant_count, expect_images=expect_images
        )
        return {
            "text": text,
            "mode": mode.value,
            "mode_applied": True,
            "attachments": len(attachments or []),
            "images": images,
            "conversation": conversation or self.conversation_state(),
        }

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
