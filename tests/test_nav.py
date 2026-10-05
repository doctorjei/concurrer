"""Pure route and row guards for one-expense navigation."""

from __future__ import annotations

import unittest

from unittest.mock import patch

from concurrer.nav import _expense_id_in_url, expense_row_number, select_row


class NavTests(unittest.TestCase):
    def test_exact_single_expense_route_is_required(self) -> None:
        report = "ABC123"
        self.assertEqual(
            _expense_id_in_url(
                "https://us2.concursolutions.com/nui/expense/reports/ABC123/expenses/DEF456",
                report,
            ),
            "DEF456",
        )
        self.assertEqual(
            _expense_id_in_url(
                "https://us2.concursolutions.com/nui/expense/reports/abc123/expenses/def456",
                report,
            ),
            "def456",
        )
        for url in (
            "https://us2.concursolutions.com/nui/expense/reports/ABC123",
            "https://us2.concursolutions.com/nui/expense/reports/OTHER/expenses/DEF456",
            "https://us2.concursolutions.com/nui/expense/reports/ABC123/bulk-edit",
        ):
            with self.subTest(url=url):
                with self.assertRaises(RuntimeError):
                    _expense_id_in_url(url, report)

    def test_data_rows_translate_past_the_header(self) -> None:
        self.assertEqual(expense_row_number(0), 1)
        self.assertEqual(expense_row_number(4), 5)
        with self.assertRaises(ValueError):
            expense_row_number(-1)

    def test_select_row_rejects_header_and_negative_without_touching_page(self) -> None:
        for row in (0, -1):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    select_row(None, row)


if __name__ == "__main__":
    unittest.main()


class ExpenseReadyTests(unittest.TestCase):
    def test_readiness_waits_for_named_fields_not_a_fixed_delay(self):
        seen = []

        class Page:
            url = "https://example.test/expense"
            inventories = [[], [{"name": ""}, {"name": "expenseType"}]]

            def evaluate(self, _script):
                return self.inventories.pop(0)

            def wait_for_timeout(self, ms):
                seen.append(ms)

        from concurrer.nav import wait_for_expense_ready

        with patch("concurrer.nav.field_inventory",
                   side_effect=lambda page: page.evaluate("fields")):
            fields = wait_for_expense_ready(Page(), timeout_ms=5000)

        self.assertEqual(fields, [{"name": ""}, {"name": "expenseType"}])
        self.assertEqual(seen, [500])

    def test_readiness_waits_for_late_filled_payment_type(self):
        # Live 2026-10-05: Payment Type reads '' at first render, then fills.
        blank = [{"name": "expenseType", "value": "Airfare"},
                 {"name": "paymentType", "value": ""}]
        filled = [{"name": "expenseType", "value": "Airfare"},
                  {"name": "paymentType", "value": "Personal Funds"}]
        seen = []

        class Page:
            url = "https://example.test/expense"
            inventories = [blank, blank, filled]

            def evaluate(self, _script):
                return self.inventories.pop(0)

            def wait_for_timeout(self, ms):
                seen.append(ms)

        from concurrer.nav import wait_for_expense_ready

        with patch("concurrer.nav.field_inventory",
                   side_effect=lambda page: page.evaluate("fields")):
            fields = wait_for_expense_ready(Page(), timeout_ms=30_000)

        self.assertEqual(fields, filled)
        self.assertEqual(seen, [500, 500])

    def test_genuinely_blank_payment_type_returns_after_bounded_grace(self):
        blank = [{"name": "expenseType", "value": "Airfare"},
                 {"name": "paymentType", "value": ""}]
        seen = []

        class Page:
            url = "https://example.test/expense"

            def evaluate(self, _script):
                return blank

            def wait_for_timeout(self, ms):
                seen.append(ms)

        from concurrer.nav import LATE_FILL_GRACE_MS, wait_for_expense_ready

        with patch("concurrer.nav.field_inventory",
                   side_effect=lambda page: page.evaluate("fields")):
            fields = wait_for_expense_ready(Page(), timeout_ms=30_000)

        self.assertEqual(fields, blank)
        self.assertEqual(sum(seen), LATE_FILL_GRACE_MS)

    def test_unrendered_form_raises_instead_of_returning_empty(self):
        class Page:
            url = "https://example.test/expense"

            def evaluate(self, _script):
                return []

            def wait_for_timeout(self, _ms):
                return None

        from concurrer.nav import wait_for_expense_ready

        with patch("concurrer.nav.field_inventory",
                   side_effect=lambda page: page.evaluate("fields")), \
            self.assertRaises(RuntimeError):
            wait_for_expense_ready(Page(), timeout_ms=500)


class OpenReportTests(unittest.TestCase):
    class Page:
        def __init__(self):
            self.calls = []

        def goto(self, url, **_kw):
            self.calls.append(("goto", url))

        def wait_for_timeout(self, ms):
            self.calls.append(("sleep", ms))

    def test_default_waits_for_content_not_a_fixed_delay(self):
        from concurrer.nav import open_report

        page = self.Page()
        with patch("concurrer.nav.wait_for_report_ready",
                   side_effect=lambda p: p.calls.append(("ready",))) as ready:
            open_report(page, "ABC123")
        ready.assert_called_once()
        self.assertNotIn("sleep", [c[0] for c in page.calls])

    def test_explicit_settle_keeps_fixed_wait_for_navigate_away_callers(self):
        from concurrer.nav import open_report

        page = self.Page()
        with patch("concurrer.nav.wait_for_report_ready") as ready:
            open_report(page, "ABC123", settle_ms=1000)
        ready.assert_not_called()
        self.assertEqual(page.calls[-1], ("sleep", 1000))
