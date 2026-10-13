"""Operator-facing xlsx workbook: Expenses / Report Details / Backend Data.

Pure and browser-free: everything here works from data already read by
``expexp.collect_report()``. Design: ``plans/xlsx-workbook-format.md``.

Sheets
------
* **Expenses** — one row per expense, UF headers. Identity is the first
  column (``Expense ID``). No ``report_id`` (it lives on Report Details) and no
  ``__row``/``__locked``/``__required`` packing: lock and required state are
  shown as cell styling and sheet protection instead.
* **Report Details** — READ ONLY report-wide facts, including the report ID.
* **Backend Data** — READ ONLY (expense type, column) map for **all** types
  in the type map, so highlighting keeps working after a type change.
* **Lists** (hidden) — dropdown sources and the header→field column map that
  read-back (and later write-back) keys on.

**Each custom-slot column carries exactly one meaning.** ``custom20`` is
"Type of Ground Transportation" on one type and "Type of Class" on another, so
it becomes two columns. A column's dropdown is therefore a constant list, and
whether it applies to a row is a lookup of (row's current type, column header)
in Customization — done live by conditional formatting, so it follows a type
change without INDIRECT.

Values are kept as the exact text Concur displayed, so the workbook round-trips
to zero changes. Typed dates/amounts can come later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from statistics import mean, pstdev
from pathlib import Path
from typing import Mapping, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.datavalidation import DataValidation

from .csvio import ExpenseRecord
from .type_form_map import TYPE_FORM_FIELDS
from .type_map import TYPE_FIELDS

EXPENSES = "Expenses"
DETAILS = "Report Details"
CUSTOMIZATION = "Backend Data"
LISTS = "Lists"
READ_ONLY = "READ ONLY"
ID_HEADER = "Expense ID"

#: The UF column mapping (operator-specified 2026-10-01), in order, then the
#: other always-relevant columns. Header -> Concur field.
LEADING: tuple[tuple[str, str], ...] = (
    ("Date", "transactionDate"),
    ("vendor", "vendor"),
    ("Business Purpose", "custom12"),
    ("Payment Type", "paymentType"),
    ("Amount", "transactionAmount"),
    ("Currency", "transactionCurrencyName"),
    ("Request", "travelRequestExpenseId"),
    ("Comment", "comment"),
    ("Expense Type", "expenseType"),
    ("Trip Type", "custom13"),
    ("TA#/UF-GO Req.ID", "custom21"),
)
GLOBAL_CUSTOMS = {f: h for h, f in LEADING if f.startswith("custom")}

#: Headers for non-custom fields whose UI label varies or collides.
DISPLAY = {
    "location": "Location",
    "businessPurpose": "Desc./Justification",
    "exchangeRate": "Exchange Rate",
    "postedAmount": "Amount in USD",
}

#: Search-backed comboboxes: their "options" are search results, not a list.
SEARCH_FIELDS = frozenset({"location", "vendor", "travelRequestExpenseId"})

FILL_BLOCKED = PatternFill("solid", fgColor="000000")
FONT_BLOCKED = Font(color="FFFFFF", bold=True)
FILL_REQUIRED = PatternFill("solid", fgColor="F8D7DA")
FILL_GOOD = PatternFill("solid", fgColor="C6EFCE")
FILL_OPTIONAL = PatternFill("solid", fgColor="F2F2F2")
FILL_NA = PatternFill("solid", fgColor="D9D9D9")
FILL_WRONG = PatternFill("solid", fgColor="FF9999")
FILL_HEADER = PatternFill("solid", fgColor="1F3864")
ORIG = "Orig"
MEALS = "Meals & Per Diem"
MEALS_BANNER = "READ-ONLY (Do not edit)"

#: Expense types whose rows live on the read-only Meals sheet instead of
#: Expenses (operator 2026-10-02). Their per-diem fields are display-only:
#: write-back never reads this sheet.
PER_DIEM_TYPES = frozenset({
    "Travel Meals - International",
    "Travel Meals - Domestic",
    "All Inclusive Per Diem (Meals/Lodging)",
    "Per Diem/Meal Adjustment",
})


def is_meal_record(record: ExpenseRecord) -> bool:
    """Whether this record belongs on the read-only Meals sheet."""
    return next(
        (c.value for c in record.cells if c.name == "expenseType"),
        "") in PER_DIEM_TYPES


def split_meal_records(
    records: Sequence[ExpenseRecord],
) -> tuple[list[ExpenseRecord], list[ExpenseRecord]]:
    """Partition records into (expenses, meals) preserving order."""
    expenses, meals = [], []
    for record in records:
        (meals if is_meal_record(record) else expenses).append(record)
    return expenses, meals


def meal_extra_columns(
    meals: Sequence[ExpenseRecord],
) -> list[tuple[str, str, str]]:
    """(header, field, normalized label) for per-diem fields, first-seen order."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in meals:
        for c in record.cells:
            if not is_per_diem(c.name, c.label):
                continue
            key = (c.name, _norm(c.label))
            if key in seen:
                continue
            seen.add(key)
            out.append((" ".join(c.label.split()), c.name, key[1]))
    return out


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def first_line(option: str) -> str:
    """Option display name; Concur appends descriptions on later lines."""
    return (option or "").split("\n")[0].strip()


#: Final Expenses column order (operator, 2026-10-02). Unlisted columns that
#: appear on other reports sort alphabetically immediately before Expense ID.
COLUMN_ORDER = (
    "Date",
    "Expense Type",
    "Location",
    "TA#/UF-GO Req.ID",
    "Business Purpose",
    "Desc./Justification",
    "Request",
    "Trip Type",
    "vendor",
    "Variant",
    "Reservation Source",
    "Shared/Accompanied?",
    "Days/Nights",
    "Payment Type",
    "Amount",
    "Currency",
    "Exchange Rate",
    "Amount in USD",
    "Comment",
    "Expense ID",
)
_COLUMN_RANK = {_norm(header): rank for rank, header in enumerate(COLUMN_ORDER)}


#: Operator-defined columns that gather several (field, label) meanings into
#: one, routed per row by which of them the row's expense type has. Each type
#: has at most one route per merged column, so routing is unambiguous.
#: Header -> {(field, normalized label)}.
MERGED: dict[str, frozenset[tuple[str, str]]] = {
    "Days/Nights": frozenset({
        ("custom28", "number of consecutive days of travel"),  # Laundry
        ("custom24", "number of nights"),                      # Lodging - Group
        ("custom28", "number of nights?"),                     # Lodging - Other
    }),
    "Shared/Accompanied?": frozenset({
        ("custom29", "were other travelers with you?"),  # Ground Transp., Mileage
        ("custom29", "are you sharing a room?"),         # Lodging, Lodging - Other
    }),
}

#: The vendor column routes to either vendorName (card merchant) or vendor
#: (editable directory field). It is not universal: a type with neither is
#: not-applicable, and a retained value must turn red rather than be dropped.
MERGED["vendor"] = frozenset({
    ("vendorName", "vendor name"),          # Locked card/merchant field.
    ("vendorName", "enter vendor name"),    # Same field, Undefined-type label.
    ("vendor", "vendor name"),              # Unlocked vendor-directory field.
})

#: Operator header names for single-meaning custom fields (field, label).
RENAMED: dict[tuple[str, str], str] = {
    ("custom28", "total number of attendees"): "# Attendees",
    ("custom28", "number of rooms"): "# Rooms",
    ("custom14", "state vehicle/decal/vessel number"): "Vehicle Govt #",
}
MERGED_FIELD = "merged"

#: Travel Allowance (per diem) fields — left OFF the workbook entirely and
#: never read back for writing (operator, 2026-10-01). Concur generates these
#: expenses from Travel Allowance; the fields are not editable on the expense
#: form. Keyed (field, normalized label); every label here is unique to the
#: per-diem types (Travel Meals - Domestic, All Inclusive Per Diem).
PER_DIEM_FIELDS = frozenset({
    ("custom17", "departure time (first day of travel)"),
    ("custom20", "return time (last day travel)"),
    ("custom20", "return time (last day of travel)"),
    ("custom24", "number of breakfasts"),
    ("custom26", "number of lunches"),
    ("custom27", "number of dinners"),
    ("custom28", "number of days"),
})


def is_per_diem(name: str, label: str) -> bool:
    return (name, _norm(label)) in PER_DIEM_FIELDS


#: "Variant" (operator, 2026-10-01): every non-per-diem ``custom20`` meaning —
#: a kind-of-thing dropdown whose list depends on the expense type (Type of
#: Class, Rental Class, Agency, Length of Rental, ...). Each type has at most
#: one custom20, so routing is unambiguous.
VARIANT = "Variant"
MERGED[VARIANT] = frozenset(
    (f.name, _norm(f.label))
    for fields in TYPE_FIELDS.values() for f in fields
    if f.name == "custom20" and not is_per_diem(f.name, f.label)
)
#: Hidden sheet: one column per expense type (row 1) holding its Variant list.
VARIANTS = "Variants"


def variant_lists() -> dict[str, list[str]]:
    """Expense type -> its Variant option list (types without one omitted)."""
    out = {}
    for expense_type, fields in sorted(TYPE_FIELDS.items()):
        for f in fields:
            if (f.name, _norm(f.label)) in MERGED[VARIANT]:
                out[expense_type] = list(OPTIONS_BY_TYPE.get((expense_type, f.name), ()))
    return out
#: Merged columns validated as whole numbers (Concur takes free text here).
WHOLE_NUMBER = frozenset({"Days/Nights", "# Attendees", "# Rooms"})


def merged_header(name: str, label: str) -> str:
    key = (name, _norm(label))
    return next((h for h, routes in MERGED.items() if key in routes), "")


def _type_options():
    """Checked-in option lists for every type (Test Report sweep 2026-10-01).

    Returns ``(by_type, by_label)``. **Labels do not identify option sets**:
    "Departure Time (first day of travel)" offers clock blocks on Per Diem but
    meal cutoffs on Travel Meals. ``by_label`` therefore maps a (field, label)
    to its list only when every type agrees, else to ``None`` (ambiguous).
    """
    raw = json.loads(Path(__file__).with_name("type_options.json").read_text())
    by_type: dict[tuple[str, str], list[str]] = {}
    by_label: dict[tuple[str, str], list[str] | None] = {}
    for expense_type, fields in raw.items():
        for name, f in fields.items():
            by_type[(expense_type, name)] = f["options"]
            key = (name, _norm(f["label"]))
            if key in by_label and by_label[key] != f["options"]:
                by_label[key] = None
            else:
                by_label.setdefault(key, f["options"])
    return by_type, by_label


OPTIONS_BY_TYPE, OPTIONS_BY_LABEL = _type_options()


def _standard_options() -> dict[str, list[str]]:
    """Fixed lists captured read-only from the editable Test Report form."""
    raw = json.loads(Path(__file__).with_name("standard_options.json").read_text())
    return {field: list(values) for field, values in raw.items()}


STANDARD_OPTIONS = _standard_options()


@dataclass(frozen=True)
class Column:
    header: str
    field: str
    #: Set only for type-specific custom slots: the UI label this column means.
    meaning: str = ""
    #: Set only for merged columns: the (field, normalized label) it gathers.
    routes: frozenset = frozenset()

    @property
    def type_specific(self) -> bool:
        """Applicability depends on the row's expense type."""
        return bool(self.routes) or (
            self.field.startswith("custom") and self.field not in GLOBAL_CUSTOMS)


@dataclass
class WorkbookData:
    report_id: str
    records: Sequence[ExpenseRecord]
    #: Report-wide facts in display order (label -> value).
    details: Mapping[str, str] = field(default_factory=dict)
    #: (field, normalized label) -> option display names.
    options: Mapping[tuple[str, str], Sequence[str]] = field(default_factory=dict)
    #: Field -> max input length observed on the live form.
    maxlengths: Mapping[str, int] = field(default_factory=dict)


def custom_header(name: str, label: str) -> str:
    return (GLOBAL_CUSTOMS.get(name) or merged_header(name, label)
            or RENAMED.get((name, _norm(label)))
            or " ".join(label.split()))


def plan_columns(records: Sequence[ExpenseRecord]) -> list[Column]:
    """UF leading columns, per-meaning customs, the rest, then identity last.

    ``Expense ID`` is never edited, so it sits at the end out of the way; it is
    still what read-back and write-back key every row on.
    """
    # Leading vendor entries use MERGED routes rather than the literal field.
    columns = [Column(h, MERGED_FIELD if h in MERGED else f, meaning=h if h in MERGED else "",
                    routes=MERGED[h] if h in MERGED else frozenset())
               for h, f in LEADING]
    taken = {_norm(c.header) for c in columns}
    meanings: dict[tuple[str, str], Column] = {}
    rest: dict[str, Column] = {}
    for record in records:
        for cell in record.cells:
            if any(cell.name == f for _, f in LEADING):
                continue
            if is_per_diem(cell.name, cell.label):
                continue
            if merged_header(cell.name, cell.label):
                continue
            if cell.name.startswith("custom"):
                key = (cell.name, _norm(cell.label))
                if key not in meanings:
                    header = custom_header(cell.name, cell.label)
                    if _norm(header) in taken:
                        header = f"{header} ({cell.name})"
                    taken.add(_norm(header))
                    meanings[key] = Column(header, cell.name, cell.label)
            elif cell.name not in rest:
                header = DISPLAY.get(cell.name) or cell.label
                if _norm(header) in taken:
                    header = f"{header} ({cell.name})"
                taken.add(_norm(header))
                rest[cell.name] = Column(header, cell.name)

    # The current report cannot teach us fields from types it does not use.
    # Add every non-per-diem field from the 67-type sweep so a type change has
    # a column in which to satisfy its new schema. Values remain empty unless
    # this particular record exposes that field.
    for fields in TYPE_FORM_FIELDS.values():
        for form in fields:
            if is_per_diem(form.name, form.label):
                continue
            if any(form.name == f for _, f in LEADING):
                continue
            if merged_header(form.name, form.label):
                continue
            if form.name.startswith("custom"):
                key = (form.name, _norm(form.label))
                if key in meanings:
                    continue
                header = custom_header(form.name, form.label)
                if _norm(header) in taken:
                    header = f"{header} ({form.name})"
                taken.add(_norm(header))
                meanings[key] = Column(header, form.name, form.label)
            elif form.name not in rest:
                header = DISPLAY.get(form.name) or " ".join(form.label.split())
                if _norm(header) in taken:
                    header = f"{header} ({form.name})"
                taken.add(_norm(header))
                rest[form.name] = Column(header, form.name)
    # Merged columns always appear: a type change may route a value into them
    # even when no row on the report uses them yet.
    present = {_norm(c.header) for c in columns}
    merged = [Column(h, MERGED_FIELD, h, routes) for h, routes in MERGED.items()
              if _norm(h) not in present]
    candidates = (columns + list(meanings.values()) + merged
                  + list(rest.values()) + [Column(ID_HEADER, "")])
    return sorted(candidates, key=lambda column: (
        len(COLUMN_ORDER) if column.header == ID_HEADER
        else _COLUMN_RANK.get(_norm(column.header), len(COLUMN_ORDER) - 1),
        _norm(column.header)))


def cell_for(record: ExpenseRecord, column: Column):
    """The record's cell that belongs in this column, or None (n/a)."""
    if column.routes:
        return next((c for c in record.cells
                     if (c.name, _norm(c.label)) in column.routes), None)
    for cell in record.cells:
        if cell.name != column.field:
            continue
        if column.meaning and _norm(cell.label) != _norm(column.meaning):
            return None
        return cell
    return None


def customization_rows(
    records: Sequence[ExpenseRecord],
    options: Mapping[tuple[str, str], Sequence[str]],
) -> list[tuple[str, str, str, str, str, str]]:
    """(type, column header, Concur label, Concur field, required, options).

    Column B (our header) is what the Expenses conditional formatting looks
    up; the Concur label is kept beside it because many headers are renamed.

    Custom slots cover every type in the type map. Standard (non-custom)
    fields are only known where observed, so they are recorded per observed
    (type, column): a type with no rows at all is "unknown", never
    not-applicable, and the Expenses lookup treats it as such.
    """
    rows = []
    columns = plan_columns(records)
    seen: set[tuple[str, str]] = set()
    for record in records:
        expense_type = next(
            (c.value for c in record.cells if c.name == "expenseType"), "")
        for col in columns:
            if col.header == ID_HEADER or col.field.startswith("custom"):
                continue
            cell = cell_for(record, col)
            if cell is None or (expense_type, col.header) in seen:
                continue
            seen.add((expense_type, col.header))
            # Live provenance owns the field route/lock state, while the
            # checked-in type sweep supplies a custom field's stable options
            # when this particular export did not open that picker.
            opts = (OPTIONS_BY_TYPE.get((expense_type, cell.name))
                    or options.get((cell.name, _norm(cell.label))) or ())
            rows.append((
                expense_type,
                col.header,
                " ".join(cell.label.split()),
                cell.name,
                "Yes" if cell.required else "No",
                " | ".join(opts),
                "Live",
            ))
    # Add known-positive fields from the complete manual-form sweep. An
    # observed report row wins above, because card-feed provenance may use a
    # different vendor control or locking. A manual absence is deliberately
    # not recorded as a negative: only a live rehearsal can settle a target
    # field that depends on card provenance or report linkage (Request).
    headers = {_norm(column.header) for column in columns}
    for expense_type, fields in TYPE_FORM_FIELDS.items():
        for f in fields:
            if is_per_diem(f.name, f.label):
                continue
            header = merged_header(f.name, f.label)
            if not header:
                if f.name.startswith("custom"):
                    header = custom_header(f.name, f.label)
                else:
                    header = next((h for h, name in LEADING if name == f.name),
                                  DISPLAY.get(f.name) or " ".join(f.label.split()))
            if _norm(header) not in headers or (expense_type, header) in seen:
                continue
            seen.add((expense_type, header))
            opts = (OPTIONS_BY_TYPE.get((expense_type, f.name))
                    or options.get((f.name, _norm(f.label))) or ())
            rows.append((
                expense_type,
                header,
                " ".join(f.label.split()),
                f.name,
                "Yes" if f.required else "No",
                " | ".join(opts),
                "Manual",
            ))
    return rows


def fit_width(header: str, values: Sequence[str], *, floor: int = 8,
              ceiling: int = 60) -> int:
    """Column width fitted to the longest value, ignoring outliers.

    An outlier is longer than mean + 2 standard deviations of the column's
    non-empty value lengths; it is clipped at the edge rather than widening
    the whole column. Headers wrap, so they only need their longest word.
    """
    lengths = [max(len(line) for line in str(v).split("\n"))
               for v in values if v not in (None, "")]
    fit = 0
    if lengths:
        cutoff = mean(lengths) + 2 * pstdev(lengths)
        fit = max(n for n in lengths if n <= cutoff)
    word = max((len(w) for w in header.split()), default=0)
    # ~5% padding: widths are in "0" character units and proportional text
    # runs slightly wider than its character count.
    return max(floor, min(ceiling, math.ceil(max(fit, word) * 1.05) + 2))


def _fit_columns(ws, first_row: int, *, ceiling: int = 60) -> None:
    for col in ws.iter_cols(min_row=first_row):
        header = str(col[0].value or "")
        ws.column_dimensions[col[0].column_letter].width = fit_width(
            header, [c.value for c in col[1:]], ceiling=ceiling)


def _read_only_banner(ws) -> None:
    ws["A1"] = READ_ONLY
    ws["A1"].font = Font(bold=True, italic=True)


def _header_row(ws, row: int, headers: Sequence[str]) -> None:
    for col, text in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = FILL_HEADER
        c.alignment = Alignment(wrap_text=True, vertical="top")


def _protect(ws) -> None:
    """Protect without a password; keep sorting/filtering/resizing usable."""
    ws.protection.sheet = True
    ws.protection.sort = False
    ws.protection.autoFilter = False
    ws.protection.formatColumns = False
    ws.protection.formatRows = False


def build_workbook(data: WorkbookData) -> Workbook:
    columns = plan_columns(data.records)
    exp_recs, meal_recs = split_meal_records(data.records)
    meal_extras = meal_extra_columns(meal_recs)
    wb = Workbook()
    ws = wb.active
    ws.title = EXPENSES
    meals = wb.create_sheet(MEALS)
    details = wb.create_sheet(DETAILS)
    custom = wb.create_sheet(CUSTOMIZATION)
    lists = wb.create_sheet(LISTS)
    lists.sheet_state = "hidden"
    variants = wb.create_sheet(VARIANTS)
    variants.sheet_state = "hidden"
    orig = wb.create_sheet(ORIG)
    orig.sheet_state = "hidden"
    vlists = variant_lists()
    tallest = max((len(v) for v in vlists.values()), default=1)
    for ci, (expense_type, options) in enumerate(vlists.items(), start=1):
        variants.cell(row=1, column=ci, value=expense_type)
        for ri, option in enumerate(options, start=2):
            variants.cell(row=ri, column=ci, value=option)

    # --- Lists: column map first (read-back keys on it), then dropdowns.
    lists["A1"], lists["B1"], lists["C1"] = "Header", "Field", "Meaning"
    for i, col in enumerate(columns, start=2):
        lists.cell(row=i, column=1, value=col.header)
        lists.cell(row=i, column=2, value=col.field)
        lists.cell(row=i, column=3, value=col.meaning)
    next_list_col = 5
    list_ranges: dict[str, str] = {}

    def list_range(values: Sequence[str], title: str) -> str:
        nonlocal next_list_col
        letter = get_column_letter(next_list_col)
        lists.cell(row=1, column=next_list_col, value=title)
        for r, v in enumerate(values, start=2):
            lists.cell(row=r, column=next_list_col, value=v)
        next_list_col += 1
        return f"{LISTS}!${letter}$2:${letter}${len(values) + 1}"

    # --- Meals & Per Diem (read-only display; never an input to write-back).
    meals["A1"] = MEALS_BANNER
    meals["A1"].font = Font(bold=True, italic=True)
    meal_exp_cols = [c for c in columns if c.header != ID_HEADER]
    meal_headers = ([c.header for c in meal_exp_cols]
                    + [h for h, _, _ in meal_extras] + [ID_HEADER])
    _header_row(meals, 3, meal_headers)
    for r, record in enumerate(meal_recs, start=4):
        for ci, col in enumerate(meal_exp_cols, start=1):
            cell = cell_for(record, col)
            meals.cell(row=r, column=ci, value=cell.value if cell else None)
        by_key = {(c.name, _norm(c.label)): c.value for c in record.cells}
        for ei, (_, name, label) in enumerate(meal_extras):
            meals.cell(row=r, column=len(meal_exp_cols) + 1 + ei,
                       value=by_key.get((name, label), ""))
        meals.cell(row=r, column=len(meal_headers), value=record.expense_id)
        for ci in range(1, len(meal_headers) + 1):
            target = meals.cell(row=r, column=ci)
            target.protection = target.protection.copy(locked=True)
    _fit_columns(meals, 3)
    meals.freeze_panes = "A4"
    _protect(meals)

    # --- Expenses.
    _header_row(ws, 1, [c.header for c in columns])
    n = len(exp_recs)
    last = n + 1
    type_letter = get_column_letter(
        next(i for i, c in enumerate(columns, 1) if c.field == "expenseType"))
    for r, record in enumerate(exp_recs, start=2):
        for ci, col in enumerate(columns, start=1):
            if col.header == ID_HEADER:
                idcell = ws.cell(row=r, column=ci, value=record.expense_id)
                idcell.fill = FILL_BLOCKED
                idcell.font = FONT_BLOCKED
                continue
            cell = cell_for(record, col)
            target = ws.cell(row=r, column=ci, value=cell.value if cell else None)
            # Only a value Concur actually marked locked stays locked. A blank
            # source cell must be editable: a type change can make Request,
            # vendor, a standard field, or a custom meaning newly applicable.
            # If the column truly does not apply, its live red conditional rule
            # and the preflight gate reject a nonblank value before any Save.
            # Pink must never mean "required but impossible to fill".
            unlocked = cell is None or not cell.locked
            target.protection = target.protection.copy(locked=not unlocked)
            if cell is not None and cell.locked:
                target.fill = FILL_BLOCKED
                target.font = FONT_BLOCKED

    for ci, col in enumerate(columns, start=1):
        letter = get_column_letter(ci)
        if n == 0 or not col.field:
            continue
        rng = f"{letter}2:{letter}{last}"
        labels = {_norm(col.meaning)} if col.meaning else None
        opts: list[str] = []
        for (fname, flabel), values in data.options.items():
            if fname == col.field and (labels is None or flabel in labels):
                opts = [first_line(v) for v in values]
                break
        if col.routes:
            option_sets = {tuple(OPTIONS_BY_LABEL.get(r) or ()) for r in col.routes}
            opts = list(option_sets.pop()) if len(option_sets) == 1 else []
        elif col.meaning:
            known = OPTIONS_BY_LABEL.get((col.field, _norm(col.meaning)), [])
            if known is None:
                opts = []  # different lists on different types: no fixed dropdown
            elif not opts:
                opts = list(known)
        if col.header == VARIANT and vlists:
            # Dependent dropdown: the list is the Variants column headed by
            # this row's Expense Type, sized to its non-empty entries.
            last_col = get_column_letter(len(vlists))
            pick = (f"OFFSET({VARIANTS}!$A$2,0,MATCH(${type_letter}2,"
                    f"{VARIANTS}!$A$1:${last_col}$1,0)-1")
            dv = DataValidation(
                type="list",
                formula1=f"{pick},COUNTA({pick},{tallest},1)),1)",
                allow_blank=True, showErrorMessage=True)
            dv.error = "Choose a Variant offered for this row's Expense Type."
            dv.errorTitle = VARIANT
            ws.add_data_validation(dv)
            dv.add(rng)
        elif col.header in WHOLE_NUMBER:
            dv = DataValidation(type="whole", operator="greaterThanOrEqual",
                                formula1="0", allow_blank=True, showErrorMessage=True)
            dv.error = f"{col.header}: a whole number (0 or more)."
            dv.errorTitle = col.header
            ws.add_data_validation(dv)
            dv.add(rng)
        elif col.field in SEARCH_FIELDS:
            seen = sorted({c.value for rec in data.records
                           for c in rec.cells if c.name == col.field and c.value})
            if col.field == "location" and seen:
                dv = DataValidation(type="list", formula1=list_range(seen, col.header),
                                    allow_blank=True, errorStyle="warning",
                                    showErrorMessage=True)
                dv.error = ("Not a location already on this report. Allowed, but it "
                            "must exactly match one Concur suggestion or write-back "
                            "will refuse the row.")
                dv.errorTitle = "New location"
                ws.add_data_validation(dv)
                dv.add(rng)
            elif col.field == "travelRequestExpenseId" and seen:
                # A Request is a Concur picker, not arbitrary prose. Existing
                # exact values are a conservative report-local list; live
                # preflight will re-read its current picker before a write.
                list_ranges[col.header] = list_range(seen, col.header)
                dv = DataValidation(type="list", formula1=list_ranges[col.header],
                                    allow_blank=True, showErrorMessage=True)
                dv.error = "Choose a Request listed for this report."
                dv.errorTitle = "Request"
                ws.add_data_validation(dv)
                dv.add(rng)
        elif opts or col.field in STANDARD_OPTIONS:
            opts = opts or STANDARD_OPTIONS[col.field]
            list_ranges[col.header] = list_range(opts, col.header)
            dv = DataValidation(type="list", formula1=list_ranges[col.header],
                                allow_blank=True, showErrorMessage=True)
            dv.error = f"Choose a value from the {col.header} list."
            dv.errorTitle = col.header
            ws.add_data_validation(dv)
            dv.add(rng)
        else:
            maxlen = data.maxlengths.get(col.field, 0)
            if maxlen:
                dv = DataValidation(type="textLength", operator="lessThanOrEqual",
                                    formula1=str(maxlen), allow_blank=True,
                                    showErrorMessage=True)
                dv.error = f"{col.header}: at most {maxlen} characters."
                ws.add_data_validation(dv)
                dv.add(rng)
        if col.header != ID_HEADER:
            # Cell-state system (operator 2026-10-02), evaluated live so it
            # follows edits and Expense Type changes. Precedence runs top to
            # bottom; the reds stop further rules.
            cu = _q(CUSTOMIZATION)
            hasrow = (f'COUNTIFS({cu}!$A:$A,${type_letter}2,'
                       f'{cu}!$B:$B,{letter}$1)>0')
            live = (f'COUNTIFS({cu}!$A:$A,${type_letter}2,'
                    f'{cu}!$H:$H,"Live")>0')
            manual = (f'COUNTIFS({cu}!$A:$A,${type_letter}2,'
                      f'{cu}!$H:$H,"Manual")>0')
            # Live observation is definitive for this report/provenance. The
            # manual sweep is exhaustive for custom fields, but a missing
            # ordinary field can depend on card provenance or report linkage.
            # Merged vendor is still an ordinary provenance-sensitive field:
            # the manual form lacking it does not prove a card-fed target lacks
            # vendorName. Only custom meanings have exhaustive manual absence.
            custom_applicability = (col.field.startswith("custom")
                                    or (col.routes and col.header != "vendor"))
            definite = f'OR({live},{manual})' if custom_applicability else live
            na = f'AND({definite},NOT({hasrow}))'
            req = (f'IFERROR(INDEX({cu}!$E:$E,MATCH(${type_letter}2&"|"&'
                   f'{letter}$1,{cu}!$G:$G,0))="Yes",FALSE)')
            ok = f'NOT({na})'
            # Keep a live locked source value black/white-text. Type changes may
            # make it invalid (the N/A red above still wins), but otherwise a
            # Date/Currency/Payment Type card fact must not turn green merely
            # because the target type also requires it.
            editable = f'CELL("protect",{letter}2)=0'
            ws.conditional_formatting.add(
                rng, FormulaRule(
                    formula=[f'AND({editable},{letter}2<>"",{ok},'
                             f'COUNTIF({list_ranges[col.header]},{letter}2)=0)']
                    if col.header in list_ranges else ['FALSE'],
                    fill=FILL_WRONG, stopIfTrue=True))
            ws.conditional_formatting.add(
                rng, FormulaRule(
                    formula=[f'AND({editable},{letter}2<>"",'
                             f'IFERROR(OR(INT({letter}2)<>{letter}2,'
                             f'{letter}2<0),TRUE))']
                    if col.header in WHOLE_NUMBER else ['FALSE'],
                    fill=FILL_WRONG, stopIfTrue=True))
            ws.conditional_formatting.add(
                rng, FormulaRule(formula=[f'AND({na},{letter}2<>"")'],
                                 fill=FILL_WRONG, stopIfTrue=True))
            ws.conditional_formatting.add(
                rng, FormulaRule(formula=[f'AND({editable},{ok},{req},{letter}2="")'],
                                 fill=FILL_REQUIRED))
            ws.conditional_formatting.add(
                rng, FormulaRule(formula=[f'AND({editable},{ok},{req},{letter}2<>"")'],
                                 fill=FILL_GOOD))
            ws.conditional_formatting.add(
                rng, FormulaRule(formula=[f'AND({editable},{na},{letter}2="")'],
                                 fill=FILL_NA))
            ws.conditional_formatting.add(
                rng, FormulaRule(formula=[f'AND({editable},{ok},NOT({req}))'],
                                 fill=FILL_OPTIONAL))

    _fit_columns(ws, 1)
    for ci, col in enumerate(columns, start=1):
        orig.cell(row=1, column=ci, value=col.header)
    for r, record in enumerate(exp_recs, start=2):
        for ci, col in enumerate(columns, start=1):
            if col.header == ID_HEADER:
                orig.cell(row=r, column=ci, value=record.expense_id)
                continue
            cell = cell_for(record, col)
            orig.cell(row=r, column=ci,
                      value=cell.value if cell else None)
    # Blocked-cell deviation: a locked cell whose value stops matching the
    # export-time Orig value flips bright red. Single-cell rules, because only
    # blocked cells may never legitimately change.
    id_letter = get_column_letter(
        next(i for i, c in enumerate(columns, 1) if c.header == ID_HEADER))
    for r, record in enumerate(exp_recs, start=2):
        for ci, col in enumerate(columns, start=1):
            if col.header == ID_HEADER:
                continue
            cell = cell_for(record, col)
            if cell is None or not cell.locked:
                continue
            letter = get_column_letter(ci)
            # Orig mirrors the Expenses column layout, so the ID lives in
            # the same column there; it is not column A.
            orig_val = (f'IFERROR(INDEX({ORIG}!$A:$ZZ,'
                        f'MATCH(${id_letter}{r},'
                        f'{ORIG}!${id_letter}:${id_letter},0),'
                        f'MATCH({letter}$1,{ORIG}!$1:$1,0)),"")')
            ws.conditional_formatting.add(
                f"{letter}{r}",
                FormulaRule(formula=[f'AND({letter}{r}<>"",'
                                    f'{letter}{r}<>{orig_val})'],
                            fill=FILL_WRONG, stopIfTrue=True))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{last}"
    _protect(ws)

    # --- Report Details.
    _read_only_banner(details)
    _header_row(details, 3, ["Field", "Value"])
    facts = {"Report ID": data.report_id, **dict(data.details)}
    for r, (k, v) in enumerate(facts.items(), start=4):
        details.cell(row=r, column=1, value=k).font = Font(bold=True)
        details.cell(row=r, column=2, value=v)
    _fit_columns(details, 3)
    _protect(details)

    # --- Backend Data (read-only reference).
    _read_only_banner(custom)
    _header_row(custom, 3, ["Expense Type", "Column", "Concur Label",
                            "Concur Field", "Required", "Options", "Key",
                            "Evidence"])
    for r, row in enumerate(customization_rows(data.records, data.options),
                               start=4):
        for c, v in enumerate(row[:6], start=1):
            custom.cell(row=r, column=c, value=v)
        custom.cell(row=r, column=7, value=f"=A{r}&\"|\"&B{r}")
        custom.cell(row=r, column=8, value=row[6])
    custom.column_dimensions["G"].hidden = True
    custom.column_dimensions["H"].hidden = True
    _fit_columns(custom, 3, ceiling=100)
    custom.freeze_panes = "A4"
    _protect(custom)
    return wb


def _q(sheet: str) -> str:
    return f"'{sheet}'" if " " in sheet else sheet


def maxlengths_from_live(live: Mapping[str, Sequence[Mapping]]) -> dict[str, int]:
    """Smallest observed ``maxlength`` per field from live inventories."""
    out: dict[str, int] = {}
    for fields in live.values():
        for f in fields:
            raw = str(f.get("maxlength") or "")
            if raw.isdigit() and f.get("name"):
                out[f["name"]] = min(int(raw), out.get(f["name"], int(raw)))
    return out


def write_workbook(path: Path, data: WorkbookData) -> list[Column]:
    wb = build_workbook(data)
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    wb.save(tmp)
    tmp.chmod(0o600)
    tmp.replace(path)
    return plan_columns(data.records)


def read_workbook(path: Path) -> tuple[str, dict[str, dict[tuple[str, str], str]]]:
    """Return (report_id, {expense_id: {(field, meaning): value}}).

    Only the Expenses sheet is read: the read-only Meals sheet is never an
    input to write-back.
    """
    wb = load_workbook(path)
    lists = wb[LISTS]
    colmap = {}
    for row in lists.iter_rows(min_row=2, max_col=3, values_only=True):
        if row[0] is None:
            break
        colmap[row[0]] = (row[1] or "", row[2] or "")
    report_id = ""
    for k, v in wb[DETAILS].iter_rows(min_row=4, max_col=2, values_only=True):
        if k == "Report ID":
            report_id = str(v or "")
    ws = wb[EXPENSES]
    # Columns are found by header, not position, so the operator may reorder
    # (or hide) them freely.
    headers = [c.value for c in ws[1]]
    if headers.count(ID_HEADER) != 1:
        raise ValueError(f"Expenses must have exactly one {ID_HEADER!r} column")
    if len(set(h for h in headers if h)) != len([h for h in headers if h]):
        raise ValueError("Expenses has duplicate column headers")
    id_at = headers.index(ID_HEADER)
    out: dict[str, dict[tuple[str, str], str]] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not any(v not in (None, "") for v in row):
            continue
        expense_id = str(row[id_at] or "").strip()
        if not expense_id:
            raise ValueError("a non-empty Expenses row has no Expense ID")
        if expense_id in out:
            raise ValueError(f"duplicate Expense ID {expense_id!r}")
        values = {}
        for i, (header, value) in enumerate(zip(headers, row)):
            if i == id_at or header is None:
                continue
            if header not in colmap:
                raise ValueError(f"unknown Expenses column {header!r}")
            values[colmap[header]] = "" if value is None else str(value)
        out[expense_id] = values
    return report_id, out


# --------------------------------------------------------------------------
# Write-back routing (pure). Design: plans/xlsx-writeback.md
# --------------------------------------------------------------------------

#: The (field, meaning) key ``read_workbook`` reports for the Expense Type
#: column: it is a plain single-meaning column, so its meaning is empty.
TYPE_KEY = ("expenseType", "")


class RoutingError(ValueError):
    """A workbook row cannot be routed to Concur fields without guessing."""


@dataclass(frozen=True)
class WorkbookIssue:
    """One cell whose Python state matches a red/pink workbook condition."""

    expense_id: str
    row: int
    header: str
    kind: str
    detail: str

    def describe(self) -> str:
        return (f"expense {self.expense_id} row {self.row} {self.header!r}: "
                f"{self.kind}: {self.detail}")


def _validation_options(wb, ws, headers: Sequence[str]) -> dict[str, set[str]]:
    """Exact list-validation choices by visible header (not formulas)."""
    out: dict[str, set[str]] = {}
    for dv in ws.data_validations.dataValidation:
        formula = str(dv.formula1 or "")
        if "!" not in formula:
            continue  # Variant's OFFSET is represented in Backend Data instead.
        sheet, ref = formula.rsplit("!", 1)
        sheet = sheet.strip("'")
        if sheet not in wb.sheetnames:
            continue
        try:
            min_col, min_row, max_col, max_row = range_boundaries(ref)
        except ValueError:
            continue
        choices = {str(wb[sheet].cell(r, c).value) for c in range(min_col, max_col + 1)
                   for r in range(min_row, max_row + 1)
                   if wb[sheet].cell(r, c).value not in (None, "")}
        for rng in dv.sqref.ranges:
            for col in range(rng.min_col, rng.max_col + 1):
                if col <= len(headers):
                    out[headers[col - 1]] = choices
    return out


def workbook_issues(path: Path) -> list[WorkbookIssue]:
    """Mirror bright-red and edited-row-pink rules without Excel calculation.

    This is the pre-browser batch gate. It reads the same Backend Data, Lists,
    Orig, and cell protections that the workbook's conditional formatting uses;
    it never infers an unknown standard field to be not-applicable. Unknown
    fields are left for the live type-change rehearsal, not silently routed.
    """
    wb = load_workbook(path, data_only=False)
    try:
        ws, backend, orig, lists = (wb[EXPENSES], wb[CUSTOMIZATION],
                                    wb[ORIG], wb[LISTS])
        headers = [str(cell.value or "") for cell in ws[1]]
        if ID_HEADER not in headers:
            raise RoutingError(f"Expenses lacks {ID_HEADER!r}")
        id_at = headers.index(ID_HEADER) + 1
        original_headers = [str(cell.value or "") for cell in orig[1]]
        original_at = {header: i + 1 for i, header in enumerate(original_headers)}
        if ID_HEADER not in original_at:
            raise RoutingError(f"Orig lacks {ID_HEADER!r}")
        original_rows = {
            str(orig.cell(row, original_at[ID_HEADER]).value or "").strip(): row
            for row in range(2, orig.max_row + 1)
        }
        colmap = {str(row[0] or ""): (str(row[1] or ""), str(row[2] or ""))
                  for row in lists.iter_rows(min_row=2, max_col=3, values_only=True)
                  if row and row[0] is not None}
        facts: dict[tuple[str, str], list[tuple[str, bool, set[str], str]]] = {}
        live_types, manual_types = set(), set()
        for row in backend.iter_rows(min_row=4, values_only=True):
            if not row or row[0] is None:
                break
            expense_type, header, _label, field, required, raw_options, _key, evidence = (
                (str(v or "") for v in (list(row) + [""] * 8)[:8]))
            if not (expense_type and header and field):
                continue
            options = {value.strip() for value in raw_options.split(" | ") if value.strip()}
            facts.setdefault((expense_type, header), []).append(
                (field, required == "Yes", options, evidence))
            if evidence == "Live":
                live_types.add(expense_type)
            elif evidence == "Manual":
                manual_types.add(expense_type)
        allowed = _validation_options(wb, ws, headers)
        issues: list[WorkbookIssue] = []
        type_at = headers.index("Expense Type") + 1
        for row_number in range(2, ws.max_row + 1):
            expense_id = str(ws.cell(row_number, id_at).value or "").strip()
            if not expense_id:
                continue
            expense_type = str(ws.cell(row_number, type_at).value or "").strip()
            original_row = original_rows.get(expense_id)
            if original_row is None:
                raise RoutingError(f"Orig has no row for expense {expense_id}")
            changed = any(
                str(ws.cell(row_number, ci).value or "") !=
                str(orig.cell(original_row, original_at[header]).value or "")
                for ci, header in enumerate(headers, 1)
                if header in original_at and header != ID_HEADER
            )
            for ci, header in enumerate(headers, 1):
                if header == ID_HEADER:
                    continue
                cell = ws.cell(row_number, ci)
                value = "" if cell.value is None else str(cell.value)
                key = (expense_type, header)
                field_facts = facts.get(key, [])
                # Custom columns (including Days/Nights / Variant merges) have
                # an exhaustive manual schema; vendor and standard fields do not.
                source_field, _meaning = colmap.get(header, ("", ""))
                is_custom = (source_field.startswith("custom") or
                             (source_field == MERGED_FIELD and header != "vendor"))
                definite_na = (not field_facts and
                               (expense_type in live_types or
                                (is_custom and expense_type in manual_types)))
                if definite_na and value.strip():
                    issues.append(WorkbookIssue(expense_id, row_number, header,
                        "not-applicable", f"{expense_type!r} has no route for this value"))
                    continue
                if cell.protection.locked:
                    old = (str(orig.cell(original_row, original_at[header]).value or "")
                           if header in original_at else "")
                    if value and value != old:
                        issues.append(WorkbookIssue(expense_id, row_number, header,
                            "locked-deviation", "locked Concur value differs from export"))
                    continue
                required = any(fact[1] for fact in field_facts)
                if changed and required and not value.strip():
                    issues.append(WorkbookIssue(expense_id, row_number, header,
                        "required-empty", "required for the edited row's expense type"))
                    continue
                choices = set().union(*(fact[2] for fact in field_facts)) if field_facts else set()
                choices |= allowed.get(header, set())
                if value.strip() and choices and value not in choices:
                    issues.append(WorkbookIssue(expense_id, row_number, header,
                        "off-list", f"{value!r} is not an allowed option"))
                    continue
                if header in WHOLE_NUMBER and value.strip():
                    try:
                        number = Decimal(value)
                        invalid_number = number < 0 or number != number.to_integral_value()
                    except InvalidOperation:
                        invalid_number = True
                    if invalid_number:
                        issues.append(WorkbookIssue(expense_id, row_number, header,
                            "not-whole", "must be a whole number zero or greater"))
        return issues
    finally:
        wb.close()


def read_backend_routing(path: Path) -> dict[tuple[str, str], set[str]]:
    """``(expense type, column header) -> {Concur field}`` from Backend Data.

    This is the same (type, column) table the sheet's conditional formatting
    looks up, so the cell colours and write-back can never disagree about
    which field a column means.

    The hidden ``Key`` column (``=A&"|"&B``) is deliberately **not** read: it is
    a formula, and openpyxl never evaluates one, so a freshly exported workbook
    has no cached value there. The key is recomputed from columns A and B.
    """
    wb = load_workbook(path, read_only=True)
    try:
        ws = wb[CUSTOMIZATION]
        out: dict[tuple[str, str], set[str]] = {}
        for row in ws.iter_rows(min_row=4, max_col=4, values_only=True):
            if not row or row[0] is None:
                break
            expense_type = str(row[0] or "").strip()
            header = str(row[1] or "").strip()
            field_name = str(row[3] or "").strip()
            if not (expense_type and header and field_name):
                continue
            out.setdefault((expense_type, _norm(header)), set()).add(field_name)
        return out
    finally:
        wb.close()


def route_merged(
    routing: Mapping[tuple[str, str], set[str]],
    expense_type: str,
    header: str,
) -> str | None:
    """The Concur field a merged column means *for this expense type*.

    ``None`` means the column does not apply to this type. A type absent from
    Backend Data is unknown, which is handled the same way here: it is never
    silently assumed not-applicable, and a non-blank cell in that state is
    refused by the caller instead.
    """
    if not expense_type:
        return None
    fields = routing.get((expense_type, _norm(header)))
    if not fields:
        return None
    if len(fields) > 1:
        raise RoutingError(
            f"column {header!r} is ambiguous for expense type {expense_type!r}: "
            f"Backend Data routes it to {sorted(fields)}. Refusing to guess."
        )
    return next(iter(fields))


def push_rows(
    path: Path,
) -> tuple[str, dict[str, dict[str, str]]]:
    """``(report_id, {expense_id: {Concur field: value}})`` for write-back.

    Every column is routed **per row, by that row's own expense type**. This is
    the whole point: ``custom28`` appears under two different labels in the
    ``Days/Nights`` merge, so resolving by field name alone would write a
    Laundry day-count into a Lodging - Other night-count.

    Rules, all chosen so that a mistake is loud rather than silent:

    * A merged column that does not apply to the row's type is **skipped**. A
      blank cell there meant "no change" anyway.
    * A **non-blank** merged cell that cannot be routed is a hard error. That
      is an operator edit we cannot honour, and dropping it would look like a
      successful push.
    * Two workbook columns resolving to the same Concur field on one row is a
      hard error whenever their values disagree.
    * Per-diem rows never appear: they live on the read-only Meals sheet, which
      :func:`read_workbook` does not read.
    """
    report_id, values_by_id = read_workbook(path)
    routing = read_backend_routing(path)
    out: dict[str, dict[str, str]] = {}
    for expense_id, values in values_by_id.items():
        expense_type = str(values.get(TYPE_KEY) or "").strip()
        row: dict[str, str] = {}
        origin: dict[str, str] = {}
        for (name, meaning), value in values.items():
            blank = not str(value).strip()
            # Blank means no change. Preserve the type key so a nonblank
            # merged cell can never evade the no-target-type refusal.
            if blank and (name, meaning) != TYPE_KEY:
                continue
            if name == MERGED_FIELD:
                field_name = route_merged(routing, expense_type, meaning)
                if field_name is None:
                    if not blank:
                        raise RoutingError(
                            f"expense {expense_id}: column {meaning!r} holds "
                            f"{value!r} but does not apply to expense type "
                            f"{expense_type or '(blank)'!r}; Backend Data has no "
                            f"route for it. Re-export so the column matches."
                        )
                    continue
            else:
                field_name = name
            if field_name in row and row[field_name] != value:
                raise RoutingError(
                    f"expense {expense_id}: columns {origin[field_name]!r} and "
                    f"{meaning or field_name!r} both route to Concur field "
                    f"{field_name!r} with different values "
                    f"({row[field_name]!r} vs {value!r}). Refusing to pick one."
                )
            row[field_name] = value
            origin.setdefault(field_name, meaning or field_name)
        out[expense_id] = row
    return report_id, out


def push_sheet(path: Path):
    """A ``csvio.Sheet`` for the batch writer, plus the workbook's report ID.

    Identity comes from the workbook (Expense ID per row, Report ID on Report
    Details) exactly as it does for the CSV path, so no command-line option can
    aim a write at a report the file never named.
    """
    from . import csvio

    path = Path(path)
    if not path.is_file():
        raise RoutingError(f"no such workbook: {path}")

    report_id, rows_by_id = push_rows(path)
    report_id = str(report_id or "").strip()
    if not report_id:
        raise RoutingError(
            "Report Details has no Report ID; refusing to guess which report "
            "this workbook describes. Re-export with the export CLI."
        )
    if not rows_by_id:
        raise RoutingError("Expenses has no rows; there is nothing to push")
    field_columns: list[str] = []
    seen: set[str] = set()
    for row in rows_by_id.values():
        for name in row:
            if name not in seen:
                seen.add(name)
                field_columns.append(name)
    rows = tuple(
        {
            csvio.EXPENSE_ID_COLUMN: expense_id,
            csvio.REPORT_ID_COLUMN: report_id,
            **row,
        }
        for expense_id, row in rows_by_id.items()
    )
    return report_id, csvio.Sheet(field_columns=tuple(field_columns), rows=rows)



def verify_workbook(path: Path, data: WorkbookData) -> list[str]:
    """Differences between the written workbook and the records it came from."""
    report_id, rows = read_workbook(path)
    problems = []
    if report_id != data.report_id:
        problems.append(f"report ID {report_id!r} != {data.report_id!r}")
    columns = plan_columns(data.records)
    exp_recs, meal_recs = split_meal_records(data.records)
    if set(rows) != {r.expense_id for r in exp_recs}:
        problems.append("expense ID set differs")
    for record in exp_recs:
        got = rows.get(record.expense_id, {})
        for col in columns:
            if col.header == ID_HEADER:
                continue
            cell = cell_for(record, col)
            want = cell.value if cell else ""
            have = got.get((col.field, col.meaning), "")
            if have != want:
                problems.append(f"{record.expense_id[:12]} {col.header}: "
                                f"{have!r} != {want!r}")
    problems.extend(_verify_meals(path, exp_recs, meal_recs))
    return problems


def _verify_meals(path: Path, exp_recs: Sequence[ExpenseRecord],
                  meal_recs: Sequence[ExpenseRecord]) -> list[str]:
    """Check the read-only Meals sheet against the meal records."""
    wb = load_workbook(path)
    if MEALS not in wb.sheetnames:
        return ["Meals sheet missing"] if meal_recs else []
    ms = wb[MEALS]
    if ms["A1"].value != MEALS_BANNER:
        return ["Meals sheet is missing its read-only banner"]
    headers = [c.value for c in ms[3]]
    if headers.count(ID_HEADER) != 1:
        return ["Meals sheet must have exactly one Expense ID column"]
    id_at = headers.index(ID_HEADER)
    seen: dict[str, dict[str, str]] = {}
    for row in ms.iter_rows(min_row=4, values_only=True):
        if not row or not any(v not in (None, "") for v in row):
            continue
        eid = str(row[id_at] or "").strip()
        if not eid or eid in seen:
            return [f"Meals sheet has a bad or duplicate Expense ID {eid!r}"]
        seen[eid] = {h: ("" if v is None else str(v))
                     for h, v in zip(headers, row) if h != ID_HEADER}
    problems = []
    if set(seen) != {r.expense_id for r in meal_recs}:
        problems.append("Meals expense ID set differs")
    columns = plan_columns(exp_recs + meal_recs)
    extras = {h: (name, label) for h, name, label in
              meal_extra_columns(meal_recs)}
    by_id = {r.expense_id: r for r in meal_recs}
    for eid, values in seen.items():
        record = by_id.get(eid)
        if record is None:
            continue
        by_key = {(c.name, _norm(c.label)): c.value for c in record.cells}
        for col in columns:
            if col.header == ID_HEADER:
                continue
            cell = cell_for(record, col)
            want = cell.value if cell else ""
            if values.get(col.header, "") != want:
                problems.append(f"{eid[:12]} meals {col.header}: "
                                f"{values.get(col.header, '')!r} != {want!r}")
        for header, (name, label) in extras.items():
            if values.get(header, "") != by_key.get((name, label), ""):
                problems.append(f"{eid[:12]} meals {header}: mismatch")
    return problems


def exported_at() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
