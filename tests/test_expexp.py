"""Collection uses each report page once and prefers stable expense IDs."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from concurrer import expexp


class CollectReportTests(unittest.TestCase):
    def test_direct_ids_avoid_repeated_report_navigation(self):
        rows = [
            {"identity": "Select expense, first", "expense_id": "AAA111"},
            {"identity": "Select expense, second", "expense_id": "BBB222"},
        ]
        fields = [
            {"label": "Expense Type", "name": "expenseType", "value": "Airfare",
             "required": True, "locked": False},
        ]
        calls = []

        with patch.object(expexp, "open_report",
                          side_effect=lambda page, report, **_: calls.append(("report", report))), \
             patch.object(expexp, "report_header", return_value={"Report Name": "R"}), \
             patch.object(expexp, "triage_rows", return_value=rows), \
             patch.object(expexp, "open_expense_by_id",
                          side_effect=lambda page, report, eid, **_: calls.append(("direct", eid)) or eid), \
             patch.object(expexp, "open_expense_edit",
                          side_effect=AssertionError("legacy row navigation used")), \
             patch("concurrer.widgets.field_inventory", return_value=fields):
            records, live, details = expexp.collect_report(
                object(), "R1", collect_header=True)

        self.assertEqual([record.expense_id for record in records], ["AAA111", "BBB222"])
        self.assertEqual(set(live), {"AAA111", "BBB222"})
        self.assertEqual(details, {"Report Name": "R"})
        self.assertEqual(calls, [("report", "R1"), ("direct", "AAA111"), ("direct", "BBB222")])

    def test_direct_open_failure_reports_expense_id_not_row_number(self):
        rows = [{"identity": "Select expense, first", "expense_id": "AAA111"}]
        with patch.object(expexp, "open_report"), \
             patch.object(expexp, "triage_rows", return_value=rows), \
             patch.object(expexp, "open_expense_by_id",
                          side_effect=RuntimeError("unreadable")), \
             patch("concurrer.widgets.field_inventory", return_value=[]):
            messages = []
            records, live, _details = expexp.collect_report(
                object(), "R1", progress=messages.append)

        self.assertEqual((records, live), ([], {}))
        self.assertTrue(any("SKIP AAA111" in message for message in messages))

    def test_missing_row_id_uses_legacy_row_navigation(self):
        rows = [{"identity": "Select expense, first"}]
        fields = [
            {"label": "Expense Type", "name": "expenseType", "value": "Airfare",
             "required": True, "locked": False},
        ]
        with patch.object(expexp, "open_report"), \
             patch.object(expexp, "triage_rows", return_value=rows), \
             patch.object(expexp, "open_expense_by_id",
                          side_effect=AssertionError("unexpected direct open")), \
             patch.object(expexp, "open_expense_edit", return_value="AAA111") as edit, \
             patch("concurrer.widgets.field_inventory", return_value=fields):
            records, live, details = expexp.collect_report(object(), "R1")

        self.assertEqual([record.expense_id for record in records], ["AAA111"])
        self.assertEqual(details, {})
        edit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
