"""Pure route and row guards for one-expense navigation."""

from __future__ import annotations

import unittest

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
