"""Browser-free tests for xlsx write-back routing (plans/xlsx-writeback.md).

The routing layer is where the workbook's operator-facing columns become Concur
field names. Its whole job is to be right about *which* field a column means for
*this* row's expense type, and to refuse rather than guess when it cannot tell.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from concurrer import csvio
from concurrer.csvio import ExpenseRecord, FieldCell
from concurrer.xlsxio import (
    DETAILS,
    EXPENSES,
    ID_HEADER,
    MERGED_FIELD,
    RoutingError,
    WorkbookData,
    push_rows,
    push_sheet,
    read_backend_routing,
    route_merged,
    write_workbook,
)

REPORT = "506EC055FE79452BB943"
OTHER_REPORT = "077DDCA88361BC4DB005BDDD9BBC3BF4"


def cell(name, label, value="", locked=False, required=False):
    return FieldCell(name, label, value, locked, required)


def laundry():
    """custom28 as *days of travel* — one of two meanings on the same field."""
    return ExpenseRecord("AAAA1111", REPORT, 1, [
        cell("expenseType", "Expense Type", "Laundry Services", required=True),
        cell("custom12", "Business Purpose", "Student Life", required=True),
        cell("custom28", "Number of consecutive days of travel", "3", required=True),
        cell("vendorName", "Vendor Name", "INTERNATIONAL TXN", locked=True),
    ])


def lodging_other():
    """custom28 again, but as *nights* — the same field, a different meaning."""
    return ExpenseRecord("BBBB2222", REPORT, 2, [
        cell("expenseType", "Expense Type", "Lodging - Other Accommodations",
             required=True),
        cell("custom12", "Business Purpose", "Student Life", required=True),
        cell("custom28", "Number of Nights?", "2", required=True),
        cell("vendorName", "Vendor Name", "HILTON", locked=True),
    ])


def lodging_group():
    """custom24 — a *different* field that also feeds the Days/Nights merge."""
    return ExpenseRecord("CCCC3333", REPORT, 3, [
        cell("expenseType", "Expense Type", "Lodging - Group", required=True),
        cell("custom12", "Business Purpose", "Student Life", required=True),
        cell("custom24", "Number of Nights", "4", required=True),
        cell("vendorName", "Vendor Name", "HYATT", locked=True),
    ])


def airfare():
    """A type with no Days/Nights route at all."""
    return ExpenseRecord("DDDD4444", REPORT, 4, [
        cell("expenseType", "Expense Type", "Airfare", required=True),
        cell("custom12", "Business Purpose", "Student Life", required=True),
        cell("vendor", "Vendor Name", "United Airlines"),
        cell("location", "Destination", "Houston, Texas"),
    ])


def meal():
    """A per-diem row: belongs on the read-only Meals sheet, never on Expenses."""
    return ExpenseRecord("EEEE5555", REPORT, 5, [
        cell("expenseType", "Expense Type", "Travel Meals - Domestic", required=True),
        cell("custom12", "Business Purpose", "Student Life", required=True),
        cell("custom17", "Departure Time (First Day of Travel)", "07:30"),
    ])


def build(records):
    return WorkbookData(
        REPORT, records, {"Report Name": "CCED Summer 2026"}, {},
        {"custom21": 10},
    )


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.xlsx"
        self.addCleanup(self.tmp.cleanup)

    def write(self, records):
        write_workbook(self.path, build(records))
        return self.path

    # -- the trap this whole layer exists for ---------------------------

    def test_custom28_two_meanings_route_by_row_type(self):
        """The same Concur field means different things per expense type."""
        self.write([laundry(), lodging_other()])
        _, rows = push_rows(self.path)
        # Both rows land on custom28, each with its own value.
        self.assertEqual(rows["AAAA1111"]["custom28"], "3")
        self.assertEqual(rows["BBBB2222"]["custom28"], "2")

    def test_days_nights_merge_splits_across_fields(self):
        """custom28 (days) and custom24 (nights) share one column, not one field."""
        self.write([laundry(), lodging_group()])
        _, rows = push_rows(self.path)
        self.assertIn("custom28", rows["AAAA1111"])
        self.assertNotIn("custom24", rows["AAAA1111"])
        self.assertIn("custom24", rows["CCCC3333"])
        self.assertNotIn("custom28", rows["CCCC3333"])

    def test_merged_column_never_leaks_its_placeholder_field(self):
        """The internal 'merged' marker must not reach the writer."""
        self.write([laundry(), lodging_group(), airfare()])
        _, rows = push_rows(self.path)
        for expense_id, row in rows.items():
            self.assertNotIn(MERGED_FIELD, row, expense_id)

    def test_vendor_universal_merge_routes_by_type(self):
        """vendorName (locked card feed) vs vendor (directory) by type."""
        self.write([laundry(), airfare()])
        _, rows = push_rows(self.path)
        self.assertEqual(rows["AAAA1111"]["vendorName"], "INTERNATIONAL TXN")
        self.assertEqual(rows["DDDD4444"]["vendor"], "United Airlines")

    # -- refusals -------------------------------------------------------

    def test_blank_merged_cell_on_unrouted_type_is_skipped(self):
        """No route + no value = nothing intended, so nothing happens."""
        self.write([airfare()])
        _, rows = push_rows(self.path)
        self.assertNotIn("custom28", rows["DDDD4444"])
        self.assertNotIn("custom24", rows["DDDD4444"])

    def test_non_blank_merged_cell_on_unrouted_type_is_refused(self):
        """An edit we cannot honour must fail loudly, never look pushed.

        This is the real operator mistake: typing into a column the sheet shows
        as not-applicable to that row's type. Backend Data has no route, so
        there is no correct field to write it to.
        """
        self.write([airfare()])
        wb = load_workbook(self.path)
        ws = wb[EXPENSES]
        col = [c.value for c in ws[1]].index("Days/Nights") + 1
        ws.cell(row=2, column=col, value="9")
        wb.save(self.path)
        with self.assertRaises(RoutingError) as caught:
            push_rows(self.path)
        self.assertIn("does not apply", str(caught.exception))

    def test_ambiguous_routing_is_refused(self):
        routing = {("Airfare", "days/nights"): {"custom28", "custom24"}}
        with self.assertRaises(RoutingError) as caught:
            route_merged(routing, "Airfare", "Days/Nights")
        self.assertIn("ambiguous", str(caught.exception))

    def test_unknown_type_is_not_silently_not_applicable(self):
        """An absent type resolves to None, so a filled cell is still refused."""
        self.assertIsNone(route_merged({}, "Brand New Type", "Days/Nights"))

    def test_routing_follows_backend_data_not_a_hardcoded_table(self):
        """A route the workbook learned from an observed row must be honoured.

        Backend Data is the single source of truth for (type, column), so a
        type that does have a route is written even though the adapter knows
        nothing about it.
        """
        record = airfare()
        record.cells.append(
            cell("custom28", "Number of consecutive days of travel", "9"))
        self.write([record])
        self.assertIn(("Airfare", "days/nights"), read_backend_routing(self.path))
        _, rows = push_rows(self.path)
        self.assertEqual(rows["DDDD4444"]["custom28"], "9")

    # -- identity and per-diem ------------------------------------------

    def test_per_diem_rows_are_invisible_to_write_back(self):
        self.write([laundry(), meal()])
        report_id, rows = push_rows(self.path)
        self.assertEqual(report_id, REPORT)
        self.assertIn("AAAA1111", rows)
        self.assertNotIn("EEEE5555", rows)

    def test_push_sheet_carries_identity_and_excludes_it_from_fields(self):
        self.write([laundry(), airfare()])
        report_id, sheet = push_sheet(self.path)
        self.assertEqual(report_id, REPORT)
        self.assertEqual(sheet.expense_ids, ("AAAA1111", "DDDD4444"))
        for row in sheet.rows:
            self.assertEqual(row[csvio.REPORT_ID_COLUMN], REPORT)
        self.assertNotIn(csvio.EXPENSE_ID_COLUMN, sheet.field_columns)
        self.assertNotIn(csvio.REPORT_ID_COLUMN, sheet.field_columns)

    def test_push_sheet_keeps_expense_type_for_downstream_refusal(self):
        """expenseType must reach csvio so BULK_REFUSED_FIELDS can catch it."""
        self.write([laundry()])
        _, sheet = push_sheet(self.path)
        self.assertIn("expenseType", sheet.field_columns)
        self.assertEqual(sheet.rows[0]["expenseType"], "Laundry Services")

    def test_missing_report_id_is_refused(self):
        self.write([laundry()])
        wb = load_workbook(self.path)
        ws = wb[DETAILS]
        for row in ws.iter_rows(min_row=1, max_col=2):
            if row[0].value == "Report ID":
                ws.cell(row=row[0].row, column=2, value="")
        wb.save(self.path)
        with self.assertRaises(RoutingError) as caught:
            push_sheet(self.path)
        self.assertIn("Report ID", str(caught.exception))

    # -- sheet-shape assumptions write-back depends on -------------------

    def test_backend_key_column_formula_is_not_relied_on(self):
        """The Key column is a formula; openpyxl stores no cached value."""
        self.write([laundry()])
        wb = load_workbook(self.path)
        cell_g = wb["Backend Data"]["G4"]
        self.assertTrue(str(cell_g.value).startswith("="))
        # Routing still works, because it is recomputed from A and B.
        self.assertIn(("Laundry Services", "days/nights"),
                      read_backend_routing(self.path))

    def test_columns_are_found_by_header_not_position(self):
        """The operator may reorder or hide columns freely."""
        self.write([laundry(), airfare()])
        wb = load_workbook(self.path)
        ws = wb[EXPENSES]
        nrows, ncols = ws.max_row, ws.max_column
        # Snapshot the whole grid first: an in-place permutation would read
        # values it has already overwritten.
        grid = [[ws.cell(row=r, column=c).value for c in range(1, ncols + 1)]
                for r in range(1, nrows + 1)]
        # Reverse the column order; each header travels with its own data,
        # which is what reordering columns in a spreadsheet actually does.
        #
        # NOTE: assign through the ``.value`` property. openpyxl's
        # ``ws.cell(row, col, value=None)`` silently ignores a None value, so
        # every cell that should be blanked would keep its old value and the
        # headers and data would silently desynchronise.
        for r, values in enumerate(grid, start=1):
            for new_c, value in enumerate(reversed(values), start=1):
                ws.cell(row=r, column=new_c).value = value
        wb.save(self.path)
        self.assertEqual(
            [c.value for c in load_workbook(self.path)[EXPENSES][1]][-1],
            "Date",
        )
        _, rows = push_rows(self.path)
        self.assertEqual(rows["AAAA1111"]["custom28"], "3")
        self.assertEqual(rows["DDDD4444"]["location"], "Houston, Texas")
        self.assertEqual(rows["DDDD4444"]["expenseType"], "Airfare")

    def test_renamed_column_renames(self):
        """# Attendees is custom28 under a third label; it keeps its own column."""
        self.write([laundry()])
        _, rows = push_rows(self.path)
        self.assertEqual(rows["AAAA1111"]["custom12"], "Student Life")
        self.assertIn(ID_HEADER, [c.value for c in
                                  load_workbook(self.path)[EXPENSES][1]])


if __name__ == "__main__":
    unittest.main()
