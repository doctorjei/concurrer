"""Browser-free tests for the xlsx workbook (plans/xlsx-workbook-format.md)."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from concurrer.csvio import ExpenseRecord, FieldCell
from concurrer.type_map import TYPE_FIELDS
from concurrer.xlsxio import (
    CUSTOMIZATION,
    DETAILS,
    EXPENSES,
    LISTS,
    READ_ONLY,
    WorkbookData,
    customization_rows,
    fit_width,
    plan_columns,
    verify_workbook,
    write_workbook,
)

REPORT = "506EC055FE79452BB943"


def cell(name, label, value="", locked=False, required=False):
    return FieldCell(name, label, value, locked, required)


def gt():
    return ExpenseRecord("AAAA1111", REPORT, 1, [
        cell("expenseType", "Expense Type", "Ground Transportation", required=True),
        cell("transactionDate", "Transaction Date", "09/21/2026", locked=True),
        cell("vendorName", "Vendor Name", "INTERNATIONAL TRANSACTION", locked=True),
        cell("custom12", "Business Purpose", "Student Life", required=True),
        cell("custom20", "Type of Ground Transportation", "Taxi", required=True),
        cell("transactionCurrencyName", "Currency", "US, Dollar (USD)", locked=True),
        cell("comment", "Comment", 'has, commas; and "quotes"'),
    ])


def air():
    return ExpenseRecord("BBBB2222", REPORT, 2, [
        cell("expenseType", "Expense Type", "Airfare", required=True),
        cell("custom20", "Type of Class", "", required=True),
        cell("vendor", "Vendor Name", "United Airlines"),
        cell("location", "Destination", "Houston, Texas"),
    ])


def data():
    return WorkbookData(
        REPORT, [gt(), air()], {"Report Name": "CCED Summer 2026"},
        {("custom20", "type of class"): ["01. Economy", "02. Business Class"],
         ("custom12", "business purpose"): ["Student Life", "Group Travel"]},
        {"custom21": 10},
    )


class XlsxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.xlsx"

    def tearDown(self):
        self.tmp.cleanup()

    def test_custom20_meanings_merge_into_variant(self):
        headers = [c.header for c in plan_columns([gt(), air()])]
        self.assertIn("Variant", headers)
        for gone in ("Type of Ground Transportation", "Type of Class", "custom20"):
            self.assertNotIn(gone, headers)

    def test_uf_order_and_no_report_id_or_meta(self):
        headers = [c.header for c in plan_columns([gt()])]
        self.assertEqual(
            headers,
            ["Date", "Expense Type", "TA#/UF-GO Req.ID",
             "Business Purpose", "Request", "Trip Type", "vendor", "Variant",
             "Shared/Accompanied?", "Days/Nights", "Payment Type", "Amount",
             "Currency", "Comment", "Expense ID"],
        )
        self.assertEqual(headers[-1], "Expense ID")
        for banned in ("report_id", "__row", "__locked", "__required"):
            self.assertNotIn(banned, headers)

    def test_round_trip_and_permissions(self):
        write_workbook(self.path, data())
        self.assertEqual(verify_workbook(self.path, data()), [])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_sheets_banner_and_report_id(self):
        write_workbook(self.path, data())
        wb = load_workbook(self.path)
        self.assertEqual(wb.sheetnames,
                         [EXPENSES, DETAILS, CUSTOMIZATION, LISTS, "Variants"])
        self.assertEqual(wb[LISTS].sheet_state, "hidden")
        self.assertEqual(wb["Variants"].sheet_state, "hidden")
        for name in (DETAILS, CUSTOMIZATION):
            a1 = wb[name]["A1"]
            self.assertEqual(a1.value, READ_ONLY)
            self.assertTrue(a1.font.bold and a1.font.italic)
        facts = {r[0]: r[1] for r in wb[DETAILS].iter_rows(min_row=4, values_only=True)}
        self.assertEqual(facts["Report ID"], REPORT)

    def test_locked_cells_protected_and_editable_cells_open(self):
        write_workbook(self.path, data())
        ws = load_workbook(self.path)[EXPENSES]
        self.assertTrue(ws.protection.sheet)
        headers = [c.value for c in ws[1]]
        date = ws.cell(row=2, column=headers.index("Date") + 1)
        comment = ws.cell(row=2, column=headers.index("Comment") + 1)
        self.assertTrue(date.protection.locked)
        self.assertFalse(comment.protection.locked)
        # Type-specific cells stay editable: a type change can make them apply.
        days = ws.cell(row=2, column=headers.index("Days/Nights") + 1)
        self.assertFalse(days.protection.locked)

    def test_dropdowns_and_applicability_rules(self):
        write_workbook(self.path, data())
        ws = load_workbook(self.path)[EXPENSES]
        formulas = [dv.formula1 for dv in ws.data_validations.dataValidation]
        self.assertTrue(any(f.startswith("Lists!") for f in formulas))
        rules = [r.formula[0] for rng in ws.conditional_formatting
                 for r in rng.rules]
        self.assertTrue(any("COUNTIFS(Customization!$A:$A" in r for r in rules))

    def test_customization_covers_every_type(self):
        types = {row[0] for row in customization_rows({})}
        with_customs = {t for t, fields in TYPE_FIELDS.items() if fields}
        self.assertEqual(types, with_customs)
        rows = {(r[0], r[1]): r for r in customization_rows({})}
        self.assertEqual(rows[("Airfare", "Variant")][2:5],
                         ("Type of Class", "custom20", "Yes"))

    def test_width_fits_longest_but_ignores_outlier(self):
        values = ["Taxi", "Bus", "Shuttle", "Ferry", "Taxi", "Bus", "Ferry",
                  "x" * 200]
        self.assertEqual(fit_width("Type", values), 8 + 2)  # ceil(7*1.05)+2
        self.assertEqual(fit_width("Ground Transportation", ["a"]), 15 + 2)
        self.assertEqual(fit_width("H", []), 8)

    def test_vendor_routes_locked_card_and_unlocked_directory_fields(self):
        data = WorkbookData(REPORT, [gt(), air()])
        headers = [c.header for c in plan_columns(data.records)]
        self.assertEqual(headers.count("vendor"), 1)
        self.assertNotIn("Vendor Name (list)", headers)
        write_workbook(self.path, data)
        self.assertEqual(verify_workbook(self.path, data), [])
        ws = load_workbook(self.path)[EXPENSES]
        headers = [c.value for c in ws[1]]
        vendor = headers.index("vendor") + 1
        locked = ws.cell(row=2, column=vendor)
        unlocked = ws.cell(row=3, column=vendor)
        self.assertEqual((locked.value, unlocked.value),
                         ("INTERNATIONAL TRANSACTION", "United Airlines"))
        self.assertTrue(locked.protection.locked)
        self.assertFalse(unlocked.protection.locked)

    def test_read_back_finds_columns_by_header_and_rejects_duplicate_ids(self):
        from openpyxl import load_workbook as lw
        from concurrer.xlsxio import read_workbook
        write_workbook(self.path, data())
        wb = lw(self.path)
        ws = wb[EXPENSES]
        ws.move_range("A1:A3", cols=ws.max_column)  # move Date to the end
        ws.delete_cols(1)
        wb.save(self.path)
        _, rows = read_workbook(self.path)
        self.assertEqual(rows["AAAA1111"][("transactionDate", "")], "09/21/2026")
        wb = lw(self.path)
        ws = wb[EXPENSES]
        id_col = [c.value for c in ws[1]].index("Expense ID") + 1
        ws.cell(row=3, column=id_col, value="AAAA1111")
        wb.save(self.path)
        with self.assertRaisesRegex(ValueError, "duplicate Expense ID"):
            read_workbook(self.path)

    def test_days_nights_merges_by_type_and_validates_whole_numbers(self):
        lodging = ExpenseRecord("CCCC3333", REPORT, 3, [
            cell("expenseType", "Expense Type", "Lodging - Group", required=True),
            cell("custom24", "Number of Nights", "4", required=True),
            cell("custom28", "Number of Rooms", "2", required=True),
        ])
        laundry = ExpenseRecord("DDDD4444", REPORT, 4, [
            cell("expenseType", "Expense Type", "Laundry Services", required=True),
            cell("custom28", "Number of consecutive days of travel", "6"),
        ])
        d = WorkbookData(REPORT, [gt(), lodging, laundry])
        cols = {c.header: c for c in plan_columns(d.records)}
        self.assertIn("Days/Nights", cols)
        self.assertIn("# Rooms", cols)  # not a day/night count
        write_workbook(self.path, d)
        self.assertEqual(verify_workbook(self.path, d), [])
        ws = load_workbook(self.path)[EXPENSES]
        headers = [c.value for c in ws[1]]
        col = headers.index("Days/Nights") + 1
        self.assertEqual([ws.cell(row=r, column=col).value for r in (2, 3, 4)],
                         [None, "4", "6"])
        self.assertTrue(any(dv.type == "whole"
                            for dv in ws.data_validations.dataValidation))

    def test_days_nights_column_present_even_when_unused(self):
        self.assertIn("Days/Nights", [c.header for c in plan_columns([gt()])])

    def test_customization_routes_day_night_fields_to_merged_header(self):
        rows = {(t, h, f) for t, h, _l, f, *_ in customization_rows({})}
        self.assertIn(("Lodging - Group", "Days/Nights", "custom24"), rows)
        self.assertIn(("Lodging - Group", "# Rooms", "custom28"), rows)
        opts = {(t, h): o for t, h, _l, _f, _r, o in customization_rows({})}
        self.assertIn("Van", opts[("Car Rental", "Variant")])
        # Same label, different lists: options must come per type.
        self.assertNotIn(("Travel Meals - Domestic",
                          "Departure Time (first day of travel)"), opts)

    def test_ambiguous_label_gets_no_fixed_dropdown(self):
        from concurrer.xlsxio import OPTIONS_BY_LABEL
        self.assertIsNone(OPTIONS_BY_LABEL[("custom17", "departure time (first day of travel)")])

    def test_per_diem_fields_left_off_but_row_kept(self):
        meals = ExpenseRecord("EEEE5555", REPORT, 5, [
            cell("expenseType", "Expense Type", "Travel Meals - Domestic"),
            cell("custom12", "Business Purpose", "Student Life"),
            cell("custom17", "Departure Time (first day of travel)", "5. Whole Day"),
            cell("custom24", "Number of Breakfasts", "1"),
            cell("custom26", "Number of Lunches", "1"),
        ])
        d = WorkbookData(REPORT, [gt(), meals])
        headers = [c.header for c in plan_columns(d.records)]
        for gone in ("Departure Time (first day of travel)",
                     "Number of Breakfasts", "Number of Lunches"):
            self.assertNotIn(gone, headers)
        write_workbook(self.path, d)
        self.assertEqual(verify_workbook(self.path, d), [])
        from concurrer.xlsxio import read_workbook
        _, rows = read_workbook(self.path)
        self.assertIn("EEEE5555", rows)  # the expense row itself stays
        self.assertNotIn(("custom24", "Number of Breakfasts"), rows["EEEE5555"])
        rows_c = {(t, h) for t, h, *_ in customization_rows({})}
        self.assertNotIn(("Travel Meals - Domestic", "Number of Breakfasts"), rows_c)
        self.assertNotIn(("All Inclusive Per Diem (Meals/Lodging)", "Days/Nights"), rows_c)

    def test_with_others_merges_custom29_with_yes_no_dropdown(self):
        gt_row = ExpenseRecord("FFFF6666", REPORT, 6, [
            cell("expenseType", "Expense Type", "Ground Transportation"),
            cell("custom29", "Were other travelers with you?", "No"),
        ])
        lodging = ExpenseRecord("GGGG7777", REPORT, 7, [
            cell("expenseType", "Expense Type", "Lodging"),
            cell("custom29", "Are you sharing a room?", "Yes"),
        ])
        d = WorkbookData(REPORT, [gt_row, lodging])
        headers = [c.header for c in plan_columns(d.records)]
        self.assertIn("Shared/Accompanied?", headers)
        self.assertNotIn("Were other travelers with you?", headers)
        write_workbook(self.path, d)
        self.assertEqual(verify_workbook(self.path, d), [])
        wb = load_workbook(self.path)
        ws = wb[EXPENSES]
        col = [c.value for c in ws[1]].index("Shared/Accompanied?") + 1
        self.assertEqual([ws.cell(row=r, column=col).value for r in (2, 3)],
                         ["No", "Yes"])
        letter = ws.cell(row=1, column=col).column_letter
        dv = next(dv for dv in ws.data_validations.dataValidation
                  if f"{letter}2" in str(dv.sqref))
        self.assertEqual(dv.type, "list")

    def test_attendee_and_room_counts_renamed_and_whole_number(self):
        party = ExpenseRecord("HHHH8888", REPORT, 8, [
            cell("expenseType", "Expense Type", "Entertainment Expense"),
            cell("custom28", "Total Number of Attendees", "12"),
        ])
        group = ExpenseRecord("IIII9999", REPORT, 9, [
            cell("expenseType", "Expense Type", "Lodging - Group"),
            cell("custom28", "Number of Rooms", "3"),
            cell("custom24", "Number of Nights", "2"),
        ])
        headers = [c.header for c in plan_columns([party, group])]
        self.assertIn("# Attendees", headers)
        self.assertIn("# Rooms", headers)
        rows = {(t, h) for t, h, *_ in customization_rows({})}
        self.assertIn(("Food & Beverages - Lunch", "# Attendees"), rows)
        self.assertIn(("Lodging - Group", "# Rooms"), rows)
        self.assertIn(("Fuel - State Vehicle", "Vehicle Govt #"), rows)
        labels = {(r[0], r[1]): r[2] for r in customization_rows({})}
        self.assertEqual(labels[("Fuel - State Vehicle", "Vehicle Govt #")],
                         "State Vehicle/Decal/Vessel Number")

    def test_variant_dependent_dropdown_reads_type_column(self):
        write_workbook(self.path, data())
        wb = load_workbook(self.path)
        ws = wb[EXPENSES]
        headers = [c.value for c in ws[1]]
        col = ws.cell(row=1, column=headers.index("Variant") + 1).column_letter
        type_col = ws.cell(row=1, column=headers.index("Expense Type") + 1).column_letter
        dv = next(dv for dv in ws.data_validations.dataValidation
                  if f"{col}2" in str(dv.sqref))
        self.assertTrue(dv.formula1.startswith("OFFSET(Variants!$A$2,0,MATCH("))
        self.assertIn(f"${type_col}2", dv.formula1)
        self.assertEqual(dv.formula1.count("("), dv.formula1.count(")"))
        variants = wb["Variants"]
        heads = [c.value for c in variants[1]]
        rental = heads.index("Rental of Space - External") + 1
        self.assertEqual([variants.cell(row=r, column=rental).value for r in (2, 3)],
                         ["12 months or less", "More than 12 months"])
        # GT row keeps its value, now under Variant.
        self.assertEqual(ws.cell(row=2, column=headers.index("Variant") + 1).value, "Taxi")


if __name__ == "__main__":
    unittest.main()
