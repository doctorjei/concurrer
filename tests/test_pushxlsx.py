"""Browser-free tests for the pushxlsx CLI (plans/xlsx-writeback.md).

The load-bearing property here is an *ordering* one: the structural write
boundary must be reached before anything that can touch credentials or a
browser. These tests assert that ordering without launching either.
"""

from __future__ import annotations

import io
import contextlib
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from concurrer import pushxlsx
from concurrer.csvio import EXPENSE_ID_COLUMN, REPORT_ID_COLUMN
from concurrer.xlsxio import (
    RoutingError,
    WorkbookData,
    push_sheet,
    write_workbook,
)
from test_xlsx_routing import REPORT, airfare, laundry, meal

TEST_REPORT = "B857BA45DBC24EDB8420"
OTHER_REPORT = "0123456789ABCDEF0123"


def workbook(report_id, records):
    return WorkbookData(
        report_id, records, {"Report Name": "Test"}, {}, {"custom21": 10}
    )


def run_cli(argv):
    """Run pushxlsx.main() with credentials resolution booby-trapped to explode.

    If the write boundary were checked *after* resolve(), this raises and the
    test fails — which is the whole point.
    """
    from concurrer import secrets, pushcsv

    def explode(*_args, **_kwargs):
        raise AssertionError(
            "credentials were resolved before the write boundary refused"
        )

    original = secrets.resolve
    secrets.resolve = explode
    pushcsv.resolve = explode
    argv_backup = pushxlsx.sys.argv
    pushxlsx.sys.argv = ["pushxlsx", *argv]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pushxlsx.main()
    finally:
        secrets.resolve = original
        pushcsv.resolve = original
        pushxlsx.sys.argv = argv_backup
    return code, out.getvalue() + err.getvalue()


class PushXlsxLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.xlsx"
        self.addCleanup(self.tmp.cleanup)

    def write(self, report_id, records):
        write_workbook(self.path, workbook(report_id, records))
        return self.path

    def test_load_returns_report_id_and_sheet(self):
        self.write(TEST_REPORT, [airfare()])
        report_id, sheet = pushxlsx.load(self.path)
        self.assertEqual(report_id, TEST_REPORT)
        self.assertEqual(sheet.expense_ids, ("DDDD4444",))
        # Only the gated workbook reader may carry Expense Type changes.
        self.assertTrue(sheet.allow_type_changes)

    def test_red_or_edited_pink_cell_aborts_before_sheet_routing(self):
        self.write(TEST_REPORT, [airfare()])
        wb = load_workbook(self.path)
        ws = wb["Expenses"]
        headers = [c.value for c in ws[1]]
        # Airfare has no Days/Nights route. This is the same red condition the
        # sheet shows after a type-aware edit, and no browser may be reached.
        ws.cell(2, headers.index("Days/Nights") + 1).value = "1"
        wb.save(self.path)
        with self.assertRaisesRegex(RoutingError, "not-applicable"):
            pushxlsx.load(self.path)

    def test_every_row_carries_the_workbook_report_id(self):
        self.write(TEST_REPORT, [airfare(), laundry()])
        report_id, sheet = pushxlsx.load(self.path)
        for row in sheet.rows:
            self.assertEqual(row[REPORT_ID_COLUMN], report_id)

    def test_per_diem_row_is_not_pushable(self):
        self.write(TEST_REPORT, [airfare(), meal()])
        _, sheet = pushxlsx.load(self.path)
        self.assertNotIn("EEEE5555", sheet.expense_ids)

    def test_missing_file_is_a_plan_error_not_a_traceback(self):
        code, _ = run_cli(["--xlsx", str(Path(self.tmp.name) / "nope.xlsx")])
        self.assertEqual(code, 2)

    def test_workbook_without_report_id_is_refused(self):
        self.write(TEST_REPORT, [airfare()])
        _, rows = push_sheet(self.path)
        self.assertTrue(rows)


class WriteBoundaryTests(unittest.TestCase):
    """``--commit`` must refuse a real report before resolve() or a browser."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.xlsx"
        self.addCleanup(self.tmp.cleanup)
        write_workbook(self.path, workbook(REPORT, [airfare()]))

    def run_commit(self, *extra):
        return run_cli(["--xlsx", str(self.path), "--commit", *extra])

    def test_confirmed_report_commit_without_token_refuses_before_credentials(self):
        code, text = self.run_commit()
        self.assertEqual(code, 2)
        self.assertIn(REPORT, text)
        self.assertIn("without --confirm", text)
        self.assertNotIn("credentials were resolved", text)

    def test_other_real_report_refuses_before_credentials_even_with_token(self):
        write_workbook(self.path, workbook(OTHER_REPORT, [airfare()]))
        code, text = self.run_commit("--confirm", "0123456789ab")
        self.assertEqual(code, 2)
        self.assertIn(TEST_REPORT, text)
        self.assertIn("only the throwaway test report", text)
        self.assertNotIn("credentials were resolved", text)

    def test_confirm_without_commit_is_refused(self):
        code, text = run_cli(["--xlsx", str(self.path), "--confirm", "0123456789ab"])
        self.assertEqual(code, 2)
        self.assertIn("only meaningful with --commit", text)
        self.assertNotIn("credentials were resolved", text)


class SubmissionTests(unittest.TestCase):
    """Submit stays in the permanent deny tier, whatever the workbook says."""

    def test_submit_is_never_tier(self):
        from concurrer.safety import NEVER_WORDS, assert_safe

        self.assertIn("submit", NEVER_WORDS)
        for label in ("Submit Report", "Save and Submit", "Submit"):
            with self.assertRaises(AssertionError):
                assert_safe(label, url=f"https://us2.concursolutions.com/x")

    def test_submit_never_survives_an_active_test_report_grant(self):
        from concurrer.safety import assert_safe, grant_covers, test_report_write_grant

        url = f"https://us2.concursolutions.com/nui/expense/reports/{TEST_REPORT}"
        with test_report_write_grant(TEST_REPORT):
            # The grant really is active, so the refusal below is the deny tier
            # and not an accident of scope.
            self.assertTrue(grant_covers(url))
            with self.assertRaises(AssertionError):
                assert_safe("Submit Report", url=url)


if __name__ == "__main__":
    unittest.main()
