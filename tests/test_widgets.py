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


class SearchDialog:
    def __init__(self, page):
        self.page = page
        self.first = self

    def count(self):
        return 1 if self.page.currency_prompt else 0

    def inner_text(self):
        return ("Currency Change\nThe location you have chosen uses a different "
                "currency from the one currently specified.")

    def get_by_role(self, _role, name=None):
        assert name == "Cancel"
        return SearchLocator(self.page, "cancel")


class SearchLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        if self.selector == '[data-nuiexp="field-location__input"]':
            return 1 if self.page.search_visible else 0
        return 1

    def get_by_role(self, _role, name=None):
        return SearchLocator(self.page, "cancel")

    def fill(self, value):
        self.page.filled.append((self.selector, value))

    def type(self, value, delay=0):
        self.page.typed.append((self.selector, value, delay))

    def nth(self, index):
        self.page.clicked.append((self.selector, index))
        return self


class SearchPage:
    def __init__(self, suggestions):
        self.url = "https://us2.concursolutions.com/nui/expense/reports/TEST/expenses/EXPENSE"
        self.suggestions = suggestions
        self.search_visible = False
        self.currency_prompt = True
        self.filled = []
        self.typed = []
        self.clicked = []
        self.keyboard = type("Keyboard", (), {"press": lambda *_args: None})()

    def locator(self, selector):
        if selector == "[role=alertdialog]":
            return SearchDialog(self)
        return SearchLocator(self, selector)

    def evaluate(self, _script):
        return {"texts": list(self.suggestions), "domCount": len(self.suggestions), "setsize": None}

    def wait_for_timeout(self, _ms):
        return None


LOCATION_FIELD = {
    "name": "location",
    "label": "City of Purchase",
    "widget": "combobox",
    "locked": False,
    "trigger": "field-location__trigger",
    "inputNuiexp": "field-location__input",
}


class SearchComboboxTests(unittest.TestCase):
    def test_search_select_types_query_then_clicks_exact_match(self):
        page = SearchPage(["Kyoto, JAPAN"])
        def click(page_arg, locator, _label):
            if locator.selector == '[data-nuiexp="field-location__trigger"]':
                page_arg.search_visible = True
            elif locator.selector == "cancel":
                page_arg.currency_prompt = False

        with (
            patch("concurrer.widgets.assert_write_authorized") as authorized,
            patch("concurrer.widgets.click_safe", side_effect=click) as clicked,
            patch("concurrer.widgets.field_value", return_value="Kyoto, JAPAN"),
        ):
            self.assertEqual(
                widgets.select_search_combobox_exact(page, LOCATION_FIELD, "Kyoto, JAPAN"),
                "Kyoto, JAPAN",
            )
        self.assertIn(('[data-nuiexp="field-location__input"]', ""), page.filled)
        self.assertIn(('[data-nuiexp="field-location__input"]', "Kyoto, JAPAN", 50), page.typed)
        self.assertEqual(authorized.call_count, 2)
        self.assertEqual(clicked.call_count, 3)
        self.assertFalse(page.currency_prompt)

    def test_search_select_refuses_empty_suggestions_without_option_click(self):
        page = SearchPage([])
        def reveal_search(*_args):
            page.search_visible = True

        with (
            patch("concurrer.widgets.assert_write_authorized"),
            patch("concurrer.widgets.click_safe", side_effect=reveal_search) as click,
            self.assertRaisesRegex(ValueError, "no exact options"),
        ):
            widgets.select_search_combobox_exact(page, LOCATION_FIELD, "Kyoto, JAPAN")
        self.assertEqual(click.call_count, 1)


class PaymentPage:
    def __init__(self, values):
        self.url = "https://us2.concursolutions.com/nui/expense/reports/TEST/expenses/EXPENSE"
        self.values = list(values)
        self.clicks = 0

    def locator(self, selector):
        page = self

        class Locator:
            def count(self):
                return 1

            def nth(self, _index):
                return self

        return Locator()

    def evaluate(self, _script):
        return {"texts": ["Pending Transaction", "Personal Funds"], "domCount": 2, "setsize": None}

    def wait_for_timeout(self, _ms):
        return None


PAYMENT_FIELD = {
    "name": "paymentType",
    "label": "Payment Type",
    "widget": "combobox",
    "locked": False,
    "trigger": "",
}


REQUEST_FIELD = {
    "name": "travelRequestExpenseId",
    "label": "Request",
    "widget": "combobox",
    "locked": False,
    "trigger": "",
    "nuiexp": ["field-travelRequestExpenseId", "field-travelRequestExpenseId__input"],
}


class RequestPickerTests(unittest.TestCase):
    def test_request_opens_from_its_combobox_container(self):
        self.assertEqual(widgets._trigger_for(REQUEST_FIELD), "field-travelRequestExpenseId")

    def test_request_selects_the_exact_option_and_reads_it_back(self):
        page = FakePage()
        selectors = []
        page.locator = lambda selector: selectors.append(selector) or FakeLocator()
        wanted = "05/04/2026, $800.00 - CCED Summer 2026"
        with (
            patch("concurrer.widgets.assert_write_authorized"),
            patch("concurrer.widgets.click_safe") as click,
            patch("concurrer.widgets._option_texts",
                  return_value=["None Selected", "None", wanted]),
            patch("concurrer.widgets.field_value", return_value=wanted),
        ):
            self.assertEqual(widgets.select_combobox_exact(page, REQUEST_FIELD, wanted), wanted)
        self.assertIn('[data-nuiexp="field-travelRequestExpenseId"]', selectors)
        self.assertEqual(click.call_count, 2)  # open, then the one exact option

    def test_request_none_does_not_match_none_selected(self):
        self.assertEqual(
            widgets.exact_option_position(["None Selected", "None", "X"], "None"), 1)


class PaymentTypeTests(unittest.TestCase):
    def test_autopopulated_requested_value_needs_no_option_click(self):
        page = PaymentPage([])
        with (
            patch("concurrer.widgets.assert_write_authorized") as authorized,
            patch("concurrer.widgets.click_safe") as click,
            patch("concurrer.widgets.field_value", return_value="Personal Funds"),
        ):
            self.assertEqual(
                widgets.select_payment_type_exact(page, PAYMENT_FIELD, "Personal Funds"),
                "Personal Funds",
            )
        self.assertEqual(authorized.call_count, 1)
        self.assertEqual(click.call_count, 1)

    def test_other_value_clicks_exact_option(self):
        page = PaymentPage([])
        with (
            patch("concurrer.widgets.assert_write_authorized") as authorized,
            patch("concurrer.widgets.click_safe") as click,
            patch("concurrer.widgets.field_value",
                  side_effect=["", "Pending Transaction"]),
        ):
            self.assertEqual(
                widgets.select_payment_type_exact(page, PAYMENT_FIELD, "Pending Transaction"),
                "Pending Transaction",
            )
        self.assertEqual(authorized.call_count, 2)
        self.assertEqual(click.call_count, 2)
