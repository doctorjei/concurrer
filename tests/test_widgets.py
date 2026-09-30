"""Option-resolution and strict combobox-write safeguards."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from concurrer import widgets
from concurrer.widgets import exact_option_position


class FakeLocator:
    def count(self) -> int:
        return 1

    def nth(self, _index: int):
        return self


class FakePage:
    def __init__(self, *, redirect_during_popup: bool = False) -> None:
        self.url = "https://us2.concursolutions.com/nui/expense/reports/TEST/expenses/EXPENSE"
        self.redirect_during_popup = redirect_during_popup
        self.keyboard = type("Keyboard", (), {"press": lambda *_args: None})()

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator()

    def wait_for_timeout(self, milliseconds: int) -> None:
        if milliseconds == 1500 and self.redirect_during_popup:
            self.url = "https://us2.concursolutions.com/nui/expense/reports/REAL/expenses/EXPENSE"


FIELD = {
    "name": "custom20",
    "label": "Type of Ground Transportation",
    "widget": "combobox",
    "locked": False,
    "trigger": "field-custom20__trigger",
}


class WidgetTests(unittest.TestCase):
    def test_exact_option_matches_first_line_and_normalized_space(self) -> None:
        self.assertEqual(
            exact_option_position(["Taxi\nAccount code", "Bus\nAccount code"], " taxi "),
            0,
        )

    def test_exact_option_rejects_absent_and_ambiguous_values(self) -> None:
        with self.assertRaises(ValueError):
            exact_option_position(["Taxi"], "Bus")
        with self.assertRaises(ValueError):
            exact_option_position(["Taxi\nOne", "taxi\nTwo"], "Taxi")

    def test_strict_selection_rechecks_authorization_and_returns_readback(self) -> None:
        page = FakePage()
        with (
            patch("concurrer.widgets.assert_write_authorized") as authorized,
            patch("concurrer.widgets.click_safe") as click,
            patch("concurrer.widgets._option_texts", return_value=["Taxi"]),
            patch("concurrer.widgets.field_value", return_value="Taxi"),
        ):
            self.assertEqual(widgets.select_combobox_exact(page, FIELD, "Taxi"), "Taxi")
        self.assertEqual(authorized.call_count, 2)
        self.assertEqual(click.call_count, 2)

    def test_redirect_during_popup_blocks_option_commit(self) -> None:
        page = FakePage(redirect_during_popup=True)

        def authorize(url: str) -> None:
            if "/REAL/" in url:
                raise AssertionError("real report")

        with (
            patch("concurrer.widgets.assert_write_authorized", side_effect=authorize),
            patch("concurrer.widgets.click_safe") as click,
            patch("concurrer.widgets._option_texts", return_value=["Taxi"]),
        ):
            with self.assertRaisesRegex(AssertionError, "real report"):
                widgets.select_combobox_exact(page, FIELD, "Taxi")
        self.assertEqual(click.call_count, 1)  # trigger only; no option commit

    def test_native_text_readback_uses_the_filled_control(self) -> None:
        class TextLocator:
            def __init__(self) -> None:
                self.value = "before"

            def count(self) -> int:
                return 1

            def fill(self, value: str) -> None:
                self.value = value

            def input_value(self) -> str:
                return self.value

        class TextPage:
            url = "https://us2.concursolutions.com/nui/expense/reports/TEST/expenses/EXPENSE"

            def __init__(self) -> None:
                self.control = TextLocator()

            def get_by_role(self, _role: str, **_kwargs) -> TextLocator:
                return self.control

            def locator(self, _selector: str) -> TextLocator:
                return self.control

        field = {
            "name": "comment",
            "label": "Comment",
            "widget": "textarea/textarea",
            "locked": False,
            "maxlength": "",
        }
        page = TextPage()
        with patch("concurrer.widgets.assert_write_authorized"):
            self.assertEqual(widgets.fill_text_exact(page, field, "after"), "after")


if __name__ == "__main__":
    unittest.main()
