import pytest

from vibe_work_bridge.browser import ModeSwitchFailed, VibeWorkBrowser
from vibe_work_bridge.modes import ResponseMode


class FakeModeButton:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, attribute):
        if attribute == "aria-pressed":
            return "true" if self.page.selected == self.name.lower() else "false"
        return None

    def click(self):
        self.page.selected = self.name.lower()


class FakeLocatorSet:
    def __init__(self, locators):
        self.locators = locators

    def count(self):
        return len(self.locators)

    def nth(self, index):
        return self.locators[index]


class FakePage:
    def __init__(self, selected="fast", controls=True):
        self.selected = selected
        self.controls = controls

    def get_by_role(self, role, name):
        if not self.controls or role != "button":
            return FakeLocatorSet([])
        labels = ("Fast", "Think")
        return FakeLocatorSet(
            [FakeModeButton(self, label) for label in labels if name.search(label)]
        )

    def wait_for_timeout(self, _milliseconds):
        pass


def test_visible_fast_think_toggle_switches_and_confirms_mode():
    browser = VibeWorkBrowser()
    browser._page = FakePage(selected="fast")
    browser._switch_mode(ResponseMode.THINK)
    assert browser._current_mode() is ResponseMode.THINK
    browser._switch_mode(ResponseMode.FAST)
    assert browser._current_mode() is ResponseMode.FAST


def test_missing_or_unverifiable_toggle_fails_loudly():
    browser = VibeWorkBrowser()
    browser._page = FakePage(controls=False)
    with pytest.raises(ModeSwitchFailed, match="Could not verify"):
        browser._switch_mode(ResponseMode.THINK)


def test_current_vibe_work_prosemirror_composer_selector_is_supported():
    assert '[contenteditable="true"][data-placeholder]' in VibeWorkBrowser._COMPOSER_SELECTORS


def test_current_vibe_model_menu_item_labels_are_supported():
    fast = VibeWorkBrowser._menu_mode_pattern(ResponseMode.FAST)
    think = VibeWorkBrowser._menu_mode_pattern(ResponseMode.THINK)
    assert fast.search("Fast Get answers fast")
    assert think.search("Think Handle complex tasks")
    assert not fast.search("Thinking about it")
