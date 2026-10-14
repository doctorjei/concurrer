"""Browser-free tests for the CSV round-trip rules.

The field fixtures are transcribed from the real captured inventory at
``~/.config/concur/discover/fields_CE96ABA11AB1674A.json`` rather than
invented, so these tests exercise the shapes Concur actually produces —
including the empty-``name`` field and values containing commas.
"""

from __future__ import annotations

import csv
import stat
import tempfile
import unittest
from pathlib import Path

from concurrer.csvio import (
    EXPENSE_ID_COLUMN,
    LOCKED_COLUMN,
    REPORT_ID_COLUMN,
    CsvError,
    ExpenseRecord,
    Sheet,
    expense_cells,
    plan_push,
    read_expenses,
    single_report_id,
    stable_name,
    union_columns,
    write_expenses,
)

# Real captured fields, trimmed to the cases that matter.
GROUND_TRANSPORT_FIELDS = [
    {
        "label": "Expense Type",
        "name": "expenseType",
        "value": "Ground Transportation",
        "required": True,
        "locked": False,
        "widget": "combobox",
        "nuiexp": ["field-expenseType__trigger", "field-expenseType__clear"],
    },
    {
        "label": "Trip Type",
        "name": "custom13",
        "value": "03. International",
        "required": True,
        "locked": False,
        "widget": "combobox",
        "nuiexp": ["field-custom13", "field-custom13__trigger"],
    },
    {
        "label": "Business Purpose",
        "name": "custom12",
        "value": "Student Life",
        "required": True,
        "locked": False,
        "widget": "combobox",
        "nuiexp": ["field-custom12"],
    },
    # The trap: name is empty; the stable handle exists only in nuiexp.
    {
        "label": "Transaction Date",
        "name": "",
        "value": "08/12/2026",
        "required": True,
        "locked": True,
        "widget": "input/text",
        "nuiexp": ["field-transactionDate"],
    },
    {
        "label": "Vendor Name",
        "name": "vendorName",
        "value": "UBER   *TRIP",
        "required": True,
        "locked": True,
        "widget": "input/text",
        "nuiexp": ["field-vendorName"],
    },
    {
        "label": "Type of Ground Transportation",
        "name": "custom20",
        "value": "",
        "required": True,
        "locked": False,
        "widget": "combobox",
        "nuiexp": ["field-custom20", "field-custom20__trigger"],
    },
    # A value containing a comma: quoting is load-bearing.
    {
        "label": "Currency",
        "name": "transactionCurrencyName",
        "value": "US, Dollar (USD)",
        "required": False,
        "locked": True,
        "widget": "input/text",
        "nuiexp": [],
    },
    {
        "label": "Comment",
        "name": "comment",
        "value": "",
        "required": False,
        "locked": False,
        "widget": "textarea/textarea",
        "nuiexp": ["field-comment"],
    },
]

EXPENSE_ID = "CE96ABA11AB1674AA77FBBC595109CB2"
TEST_REPORT = "B857BA45DBC24EDB8420"


def make_record(fields=None, expense_id=EXPENSE_ID, report_id=TEST_REPORT):
    return ExpenseRecord(
        expense_id=expense_id,
        report_id=report_id,
        row_number=1,
        cells=expense_cells(fields or GROUND_TRANSPORT_FIELDS),
    )


def roundtrip(record):
    """Write one record and read it back as a Sheet."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "expenses.csv"
        write_expenses(path, [record])
        return read_expenses(path)


class StableNameTests(unittest.TestCase):
    def test_prefers_reported_name(self):
        self.assertEqual(stable_name({"name": "custom12", "nuiexp": []}), "custom12")

    def test_falls_back_to_nuiexp_when_name_is_empty(self):
        # The real Transaction Date case. Keying on `name` alone loses this field.
        self.assertEqual(
            stable_name({"name": "", "nuiexp": ["field-transactionDate"]}),
            "transactionDate",
        )

    def test_strips_widget_suffix_variants(self):
        self.assertEqual(
            stable_name({"name": "", "nuiexp": ["field-custom20__trigger"]}),
            "custom20",
        )
        self.assertEqual(
            stable_name({"name": "", "nuiexp": ["field-x__clear"]}), "x"
        )
        self.assertEqual(
            stable_name({"name": "", "nuiexp": ["field-y__input"]}), "y"
        )

    def test_ignores_non_field_nuiexp(self):
        self.assertEqual(stable_name({"name": "", "nuiexp": ["exp-save-expense"]}), "")

    def test_unnamed_field_is_dropped_not_exported_blank(self):
        cells = expense_cells([{"label": "Mystery", "name": "", "nuiexp": []}])
        self.assertEqual(cells, [])


class RoundTripFidelityTests(unittest.TestCase):
    """export -> edit nothing -> push must report zero changes.

    This is the property the whole design rests on. If the exporter
    misrepresents live state, nothing downstream can be trusted.
    """

    def test_unchanged_file_produces_zero_changes(self):
        record = make_record()
        sheet = roundtrip(record)
        push = plan_push(
            sheet, {EXPENSE_ID: GROUND_TRANSPORT_FIELDS}
        )
        self.assertEqual(push.total_changes, 0)
        self.assertFalse(push.has_errors)
        self.assertEqual(push.actionable, [])

    def test_comma_bearing_value_survives_verbatim(self):
        sheet = roundtrip(make_record())
        row = sheet.rows[0]
        self.assertEqual(row["transactionCurrencyName"], "US, Dollar (USD)")

    def test_whitespace_in_value_survives_write(self):
        # "UBER   *TRIP" has internal runs of spaces; the file must keep them.
        sheet = roundtrip(make_record())
        self.assertEqual(sheet.rows[0]["vendorName"], "UBER   *TRIP")

    def test_export_csv_is_owner_read_write_only(self):
        # The CSV is the operator-facing file and carries live expense values;
        # protect it at least as tightly as the adjacent .live.json evidence.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expenses.csv"
            write_expenses(path, [make_record()])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_identity_and_meta_columns_present(self):
        sheet = roundtrip(make_record())
        self.assertEqual(sheet.rows[0][EXPENSE_ID_COLUMN], EXPENSE_ID)
        self.assertEqual(sheet.rows[0][REPORT_ID_COLUMN], TEST_REPORT)
        self.assertIn(LOCKED_COLUMN, sheet.rows[0])

    def test_meta_columns_are_not_writable_field_columns(self):
        sheet = roundtrip(make_record())
        for meta in (LOCKED_COLUMN, "__required", "__row"):
            self.assertNotIn(meta, sheet.field_columns)


class DiffTests(unittest.TestCase):
    def test_real_change_is_detected(self):
        record = make_record()
        sheet = roundtrip(record)
        row = dict(sheet.rows[0])
        row["comment"] = "Airport transfer for conference"
        push = plan_push(
            _with_rows(sheet, [row]), {EXPENSE_ID: GROUND_TRANSPORT_FIELDS}
        )
        actionable = push.actionable
        self.assertEqual(len(actionable), 1)
        self.assertEqual(len(actionable[0].changes), 1)
        change = actionable[0].changes[0]
        self.assertEqual(change.name, "comment")
        self.assertEqual(change.before, "")
        self.assertEqual(change.after, "Airport transfer for conference")

    def test_blank_cell_means_no_change_not_clear(self):
        # custom13 has a live value; a blank cell must NOT plan to erase it.
        record = make_record()
        sheet = roundtrip(record)
        row = dict(sheet.rows[0])
        row["custom13"] = ""
        push = plan_push(
            _with_rows(sheet, [row]), {EXPENSE_ID: GROUND_TRANSPORT_FIELDS}
        )
        self.assertEqual(push.total_changes, 0)
        self.assertEqual(push.actionable, [])

    def test_whitespace_only_cell_means_no_change(self):
        record = make_record()
        sheet = roundtrip(record)
        row = dict(sheet.rows[0])
        row["comment"] = "   "
        push = plan_push(
            _with_rows(sheet, [row]), {EXPENSE_ID: GROUND_TRANSPORT_FIELDS}
        )
        self.assertEqual(push.total_changes, 0)

    def test_case_only_difference_is_not_a_change(self):
        # _norm agrees with edit.py, so a case-only edit is a no-op, not a rewrite.
        record = make_record()
        sheet = roundtrip(record)
        row = dict(sheet.rows[0])
        row["custom12"] = "student life"
        push = plan_push(
            _with_rows(sheet, [row]), {EXPENSE_ID: GROUND_TRANSPORT_FIELDS}
        )
        self.assertEqual(push.total_changes, 0)


class RefusalTests(unittest.TestCase):
    def _row_with(self, **overrides):
        sheet = roundtrip(make_record())
        row = dict(sheet.rows[0])
        row.update(overrides)
        return _with_rows(sheet, [row])

    def test_locked_field_edit_is_refused(self):
        push = plan_push(
            self._row_with(vendorName="Not Uber Anymore"),
            {EXPENSE_ID: GROUND_TRANSPORT_FIELDS},
        )
        self.assertEqual(push.actionable, [])
        self.assertEqual(len(push.blocked), 1)
        self.assertIn("locked", push.blocked[0].errors[0])

    def test_bulk_expense_type_change_is_refused(self):
        push = plan_push(
            self._row_with(expenseType="Meals"),
            {EXPENSE_ID: GROUND_TRANSPORT_FIELDS},
        )
        self.assertEqual(push.actionable, [])
        self.assertTrue(push.blocked)
        self.assertIn("bulk type changes are refused", push.blocked[0].errors[0])

    def _undefined_live(self):
        return [
            {"label": "Expense Type", "name": "expenseType", "value": "Undefined",
             "required": True, "locked": False, "widget": "combobox"},
            {"label": "Comment", "name": "comment", "value": "", "required": False,
             "locked": False, "widget": "textarea/textarea"},
        ]

    def _type_sheet(self, allow=True, **values):
        columns = tuple(values)
        return Sheet(field_columns=columns,
                     rows=({EXPENSE_ID_COLUMN: EXPENSE_ID, REPORT_ID_COLUMN: TEST_REPORT,
                            **values},),
                     allow_type_changes=allow)

    def test_gated_sheet_carries_a_type_change_and_target_only_fields(self):
        # Undefined -> Ground Transportation: Variant (custom20) and Request
        # exist only on the target form, so they plan as '' -> value.
        sheet = self._type_sheet(expenseType="Ground Transportation", custom20="Taxi",
                                 travelRequestExpenseId="05/04/2026, $800.00 - CCED")
        push = plan_push(sheet, {EXPENSE_ID: self._undefined_live()})
        self.assertFalse(push.has_errors, push.blocked and push.blocked[0].errors)
        changes = {c.name: (c.before, c.after) for c in push.actionable[0].changes}
        self.assertEqual(changes["expenseType"], ("Undefined", "Ground Transportation"))
        self.assertEqual(changes["custom20"], ("", "Taxi"))
        self.assertEqual(changes["travelRequestExpenseId"][0], "")

    def test_gated_type_change_refuses_a_custom_the_target_lacks(self):
        sheet = self._type_sheet(expenseType="Phone/Fax/Internet", custom20="Taxi")
        push = plan_push(sheet, {EXPENSE_ID: self._undefined_live()})
        self.assertTrue(push.has_errors)
        self.assertIn("not a field on target type", push.blocked[0].errors[0])

    def test_ungated_sheet_still_refuses_type_changes(self):
        sheet = self._type_sheet(allow=False, expenseType="Ground Transportation")
        push = plan_push(sheet, {EXPENSE_ID: self._undefined_live()})
        self.assertIn("bulk type changes are refused", push.blocked[0].errors[0])

    def test_absent_field_without_a_type_change_is_still_an_error(self):
        sheet = self._type_sheet(expenseType="Undefined", custom20="Taxi")
        push = plan_push(sheet, {EXPENSE_ID: self._undefined_live()})
        self.assertIn("not a field on this expense's live form", push.blocked[0].errors[0])

    def test_unchanged_expense_type_is_not_an_error(self):
        # Regression: the bulk refusal once fired on a non-blank expenseType
        # cell rather than on an actual change, which made every faithfully
        # exported file unpushable and buried real errors under the noise.
        sheet = roundtrip(make_record())
        push = plan_push(sheet, {EXPENSE_ID: GROUND_TRANSPORT_FIELDS})
        self.assertFalse(push.has_errors)
        self.assertEqual(push.blocked, [])

    def test_unchanged_locked_field_is_not_an_error(self):
        # Same class of bug for locked fields: the exporter must be able to
        # carry a locked field's current value without tripping a refusal.
        sheet = roundtrip(make_record())
        push = plan_push(sheet, {EXPENSE_ID: GROUND_TRANSPORT_FIELDS})
        self.assertEqual(push.blocked, [])

    def test_one_real_change_alongside_unchanged_locked_and_type_fields(self):
        # The realistic case: a faithful export plus one genuine edit.
        sheet = roundtrip(make_record())
        row = dict(sheet.rows[0])
        row["custom20"] = "Taxi"
        push = plan_push(
            _with_rows(sheet, [row]), {EXPENSE_ID: GROUND_TRANSPORT_FIELDS}
        )
        self.assertFalse(push.has_errors)
        self.assertEqual(len(push.actionable), 1)
        self.assertEqual(
            [(c.name, c.after) for c in push.actionable[0].changes],
            [("custom20", "Taxi")],
        )

    def test_unknown_field_column_is_refused(self):
        sheet = self._row_with(nonexistentField="boom")
        # Add the column to the declared writable surface so it reaches the differ.
        sheet = _with_field_columns(sheet, [*sheet.field_columns, "nonexistentField"])
        push = plan_push(sheet, {EXPENSE_ID: GROUND_TRANSPORT_FIELDS})
        self.assertTrue(push.blocked)
        self.assertIn("not a field", push.blocked[0].errors[0])

    def test_unknown_expense_id_is_never_created(self):
        sheet = roundtrip(make_record())
        push = plan_push(sheet, {"SOME_OTHER_ID": GROUND_TRANSPORT_FIELDS})
        self.assertEqual(push.unknown_ids, [EXPENSE_ID])
        self.assertEqual(push.actionable, [])


class HeaderValidationTests(unittest.TestCase):
    def test_missing_expense_id_column_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("comment,custom12\nhello,world\n", encoding="utf-8")
            with self.assertRaises(CsvError) as ctx:
                read_expenses(path)
            self.assertIn("expense_id", str(ctx.exception))

    def test_missing_file_is_refused(self):
        with self.assertRaises(CsvError):
            read_expenses(Path("/nonexistent/nope.csv"))

    def test_plain_utf8_without_bom_reads(self):
        # Tools that drop the BOM must still work.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.csv"
            path.write_text(
                "expense_id,report_id,comment\nABC123,B857BA45DBC24EDB8420,café\n",
                encoding="utf-8",
            )
            sheet = read_expenses(path)
            self.assertEqual(sheet.rows[0]["comment"], "café")


class ReportIdentityTests(unittest.TestCase):
    def _sheet_with(self, rows):
        sheet = roundtrip(make_record())
        return _with_rows(sheet, rows)

    def test_single_report_id_canonicalizes_case(self):
        row = dict(roundtrip(make_record()).rows[0])
        row[REPORT_ID_COLUMN] = TEST_REPORT.lower()
        self.assertEqual(single_report_id(self._sheet_with([row])), TEST_REPORT)

    def test_empty_sheet_cannot_identify_a_report(self):
        sheet = self._sheet_with([])
        with self.assertRaisesRegex(CsvError, "no expense rows"):
            single_report_id(sheet)

    def test_blank_report_id_is_refused_with_row(self):
        row = dict(roundtrip(make_record()).rows[0])
        row[REPORT_ID_COLUMN] = "  "
        with self.assertRaisesRegex(CsvError, "row 2.*blank"):
            single_report_id(self._sheet_with([row]))

    def test_short_row_missing_report_id_is_refused(self):
        row = dict(roundtrip(make_record()).rows[0])
        row[REPORT_ID_COLUMN] = None
        with self.assertRaisesRegex(CsvError, "row 2.*missing"):
            single_report_id(self._sheet_with([row]))

    def test_invalid_report_id_is_refused(self):
        row = dict(roundtrip(make_record()).rows[0])
        row[REPORT_ID_COLUMN] = "not-a-concur-id"
        with self.assertRaisesRegex(CsvError, "invalid.*hexadecimal"):
            single_report_id(self._sheet_with([row]))

    def test_mixed_report_ids_are_refused(self):
        sheet = roundtrip(make_record())
        first = dict(sheet.rows[0])
        second = dict(sheet.rows[0])
        second[EXPENSE_ID_COLUMN] = "ABCDEF0123456789"
        second[REPORT_ID_COLUMN] = "DEADBEEF"
        with self.assertRaisesRegex(CsvError, "row 3.*mixed reports"):
            single_report_id(self._sheet_with([first, second]))

    def test_missing_report_id_header_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("expense_id,comment\nABC123,hello\n", encoding="utf-8")
            with self.assertRaisesRegex(CsvError, "report_id"):
                read_expenses(path)

    def test_case_only_duplicate_expense_ids_are_refused(self):
        sheet = roundtrip(make_record())
        first = dict(sheet.rows[0])
        second = dict(sheet.rows[0])
        second[EXPENSE_ID_COLUMN] = EXPENSE_ID.lower()
        push = plan_push(
            _with_rows(sheet, [first, second]),
            {EXPENSE_ID: GROUND_TRANSPORT_FIELDS},
        )
        self.assertEqual(push.duplicate_ids, [EXPENSE_ID.lower()])
        self.assertEqual(len(push.row_plans), 1)


class MultiExpenseTests(unittest.TestCase):
    def test_column_union_keeps_first_seen_order(self):
        a = ExpenseRecord(
            expense_id="A",
            report_id=TEST_REPORT,
            row_number=1,
            cells=expense_cells(
                [{"label": "Alpha", "name": "custom1", "value": "1"}]
            ),
        )
        b = ExpenseRecord(
            expense_id="B",
            report_id=TEST_REPORT,
            row_number=2,
            cells=expense_cells(
                [
                    {"label": "Beta", "name": "custom2", "value": "2"},
                    {"label": "Alpha", "name": "custom1", "value": "3"},
                ]
            ),
        )
        columns = union_columns([a, b])
        self.assertEqual(
            columns,
            [
                EXPENSE_ID_COLUMN,
                REPORT_ID_COLUMN,
                "custom1",
                "custom2",
                LOCKED_COLUMN,
                "__required",
                "__row",
            ],
        )

    def test_missing_field_in_one_expense_exports_blank(self):
        a = ExpenseRecord(
            expense_id="A",
            report_id=TEST_REPORT,
            row_number=1,
            cells=expense_cells(
                [{"label": "Alpha", "name": "custom1", "value": "1"}]
            ),
        )
        b = ExpenseRecord(
            expense_id="B",
            report_id=TEST_REPORT,
            row_number=2,
            cells=expense_cells(
                [{"label": "Beta", "name": "custom2", "value": "2"}]
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "multi.csv"
            write_expenses(path, [a, b])
            sheet = read_expenses(path)
            by_id = {r[EXPENSE_ID_COLUMN]: r for r in sheet.rows}
            self.assertEqual(by_id["A"]["custom2"], "")
            self.assertEqual(by_id["B"]["custom1"], "")

    def test_duplicate_expense_ids_are_caught(self):
        sheet = roundtrip(make_record())
        dup = dict(sheet.rows[0])
        push = plan_push(
            _with_rows(sheet, [dict(sheet.rows[0]), dup]),
            {EXPENSE_ID: GROUND_TRANSPORT_FIELDS},
        )
        self.assertEqual(push.duplicate_ids, [EXPENSE_ID])
        # Only the first occurrence is planned; the dup never writes twice.
        self.assertEqual(len(push.row_plans), 1)


def _with_rows(sheet, rows):
    from concurrer.csvio import Sheet

    return Sheet(field_columns=sheet.field_columns, rows=tuple(rows))


def _with_field_columns(sheet, columns):
    from concurrer.csvio import Sheet

    return Sheet(field_columns=tuple(columns), rows=sheet.rows)


if __name__ == "__main__":
    unittest.main()
