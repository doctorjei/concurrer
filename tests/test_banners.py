"""Browser-free tests for explicit banner inspection and responses."""

from __future__ import annotations

import unittest

from concurrer.banners import BannerBlocked, handle_banners, parse_banner_actions


RAW_BANNER = {
    "text": "This site uses cookies for operation, analytics, or advertising.",
    "actions": [
        {"label": "Understood", "selector": "#truste-consent-button", "kind": "button"},
        {"label": "Manage Settings", "selector": "#truste-show-consent", "kind": "button"},
    ],
}


class FakeLocator:
    def __init__(self, text: str, *, visible: bool = True) -> None:
        self.text = text
        self.visible = visible
        self.clicked = False

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        return self.visible

    def inner_text(self) -> str:
        return self.text

    def click(self, **_kwargs) -> None:
        self.clicked = True


class FakePage:
    def __init__(self, snapshots: list[dict | None], locators: dict[str, FakeLocator]) -> None:
        self.snapshots = iter(snapshots)
        self.locators = locators

    def evaluate(self, _script: str, _args: dict) -> dict | None:
        return next(self.snapshots)

    def locator(self, selector: str) -> FakeLocator:
        return self.locators[selector]

    def wait_for_timeout(self, _milliseconds: int) -> None:
        pass


class BannerTests(unittest.TestCase):
    def test_parser_requires_one_unambiguous_response_per_banner(self) -> None:
        self.assertEqual(
            parse_banner_actions(["trustarc=Understood"]),
            {"trustarc": "Understood"},
        )
        for raw in (["trustarc"], ["=Understood"], ["trustarc="], ["trustarc=Understood", "trustarc=Manage Settings"]):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_banner_actions(raw)

    def test_no_response_reports_text_and_does_not_click(self) -> None:
        locator = FakeLocator("Understood")
        page = FakePage([RAW_BANNER], {"#truste-consent-button": locator})
        with self.assertRaises(BannerBlocked) as caught:
            handle_banners(page)
        self.assertIn("This site uses cookies", str(caught.exception))
        self.assertFalse(locator.clicked)

    def test_exact_explicit_response_clicks_and_requires_banner_to_clear(self) -> None:
        locator = FakeLocator("Understood")
        page = FakePage(
            [RAW_BANNER, None],
            {"#truste-consent-button": locator},
        )
        handle_banners(page, {"trustarc": "Understood"})
        self.assertTrue(locator.clicked)

    def test_changed_button_text_fails_closed(self) -> None:
        locator = FakeLocator("Accept all cookies")
        page = FakePage(
            [RAW_BANNER, None],
            {"#truste-consent-button": locator},
        )
        with self.assertRaisesRegex(RuntimeError, "text changed"):
            handle_banners(page, {"trustarc": "Understood"})
        self.assertFalse(locator.clicked)


if __name__ == "__main__":
    unittest.main()
