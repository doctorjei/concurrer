"""Operator-facing xlsx workbook: Expenses / Report Details / Customization.

Pure and browser-free: everything here works from data already read by
``expexp.collect_report()``. Design: ``plans/xlsx-workbook-format.md``.

Sheets
------
* **Expenses** — one row per expense, UF headers. Identity is the first
  column (``Expense ID``). No ``report_id`` (it lives on Report Details) and no
  ``__row``/``__locked``/``__required`` packing: lock and required state are
  shown as cell styling and sheet protection instead.
* **Report Details** — READ ONLY report-wide facts, including the report ID.
* **Customization** — READ ONLY (expense type, column) map for **all** types
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
from datetime import datetime
from statistics import mean, pstdev
from pathlib import Path
from typing import Mapping, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .csvio import ExpenseRecord
from .type_map import TYPE_FIELDS

EXPENSES = "Expenses"
DETAILS = "Report Details"
CUSTOMIZATION = "Customization"
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

FILL_LOCKED = PatternFill("solid", fgColor="E3E7ED")
FILL_REQUIRED = PatternFill("solid", fgColor="F8D7DA")
FILL_NA = PatternFill("solid", fgColor="D9D9D9")
FILL_WRONG = PatternFill("solid", fgColor="FF9999")
FILL_HEADER = PatternFill("solid", fgColor="1F3864")


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

#: Operator-defined columns that always apply, even though their Concur source
#: varies by expense type. Unlike custom-slot merges, every type has one of the
#: listed sources, so they are never marked not-applicable.
UNIVERSAL_MERGES = frozenset({"vendor"})
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
    options: Mapping[tuple[str, str], Sequence[str]],
) -> list[tuple[str, str, str, str, str, str]]:
    """(type, column header, Concur label, Concur field, required, options).

    Column B (our header) is what the Expenses conditional formatting looks
    up; the Concur label is kept beside it because many headers are renamed.
    """
    rows = []
    for expense_type in sorted(TYPE_FIELDS):
        for f in TYPE_FIELDS[expense_type]:
            if is_per_diem(f.name, f.label):
                continue
            opts = (OPTIONS_BY_TYPE.get((expense_type, f.name))
                    or options.get((f.name, _norm(f.label))) or ())
            rows.append((
                expense_type,
                custom_header(f.name, f.label),
                " ".join(f.label.split()),
                f.name,
                "Yes" if f.required else "No",
                " | ".join(opts),
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
    wb = Workbook()
    ws = wb.active
    ws.title = EXPENSES
    details = wb.create_sheet(DETAILS)
    custom = wb.create_sheet(CUSTOMIZATION)
    lists = wb.create_sheet(LISTS)
    lists.sheet_state = "hidden"
    variants = wb.create_sheet(VARIANTS)
    variants.sheet_state = "hidden"
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

    def list_range(values: Sequence[str], title: str) -> str:
        nonlocal next_list_col
        letter = get_column_letter(next_list_col)
        lists.cell(row=1, column=next_list_col, value=title)
        for r, v in enumerate(values, start=2):
            lists.cell(row=r, column=next_list_col, value=v)
        next_list_col += 1
        return f"{LISTS}!${letter}$2:${letter}${len(values) + 1}"

    # --- Expenses.
    _header_row(ws, 1, [c.header for c in columns])
    n = len(data.records)
    last = n + 1
    type_letter = get_column_letter(
        next(i for i, c in enumerate(columns, 1) if c.field == "expenseType"))
    for r, record in enumerate(data.records, start=2):
        for ci, col in enumerate(columns, start=1):
            if col.header == ID_HEADER:
                ws.cell(row=r, column=ci, value=record.expense_id).fill = FILL_LOCKED
                continue
            cell = cell_for(record, col)
            target = ws.cell(row=r, column=ci, value=cell.value if cell else None)
            unlocked = cell is not None and not cell.locked
            # Custom meaning columns stay editable even when n/a: a type change
            # can make them apply. Conditional formatting shows applicability.
            if (col.type_specific and col.header not in UNIVERSAL_MERGES
                    ) or col.field.startswith("custom"):
                unlocked = cell is None or not cell.locked
            target.protection = target.protection.copy(locked=not unlocked)
            if cell is not None and cell.locked:
                target.fill = FILL_LOCKED
            elif cell is not None and cell.required and not cell.value.strip():
                target.fill = FILL_REQUIRED
            elif cell is None and not (
                (col.type_specific and col.header not in UNIVERSAL_MERGES)
                or col.field.startswith("custom")):
                target.fill = FILL_NA

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
        elif opts:
            dv = DataValidation(type="list", formula1=list_range(opts, col.header),
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
        if col.type_specific and col.header not in UNIVERSAL_MERGES:
            # Live applicability against Customization (A=type, B=column).
            # Universal merges are applicable to every row by construction.
            na = (f'COUNTIFS({_q(CUSTOMIZATION)}!$A:$A,${type_letter}2,'
                  f'{_q(CUSTOMIZATION)}!$B:$B,{letter}$1)=0')
            ws.conditional_formatting.add(
                rng, FormulaRule(formula=[f'AND({na},{letter}2<>"")'],
                                 fill=FILL_WRONG, stopIfTrue=True))
            ws.conditional_formatting.add(
                rng, FormulaRule(formula=[f'AND({na},{letter}2="")'], fill=FILL_NA))

    _fit_columns(ws, 1)
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

    # --- Customization.
    _read_only_banner(custom)
    _header_row(custom, 3, ["Expense Type", "Column", "Concur Label",
                            "Concur Field", "Required", "Options"])
    for r, row in enumerate(customization_rows(data.options), start=4):
        for c, v in enumerate(row, start=1):
            custom.cell(row=r, column=c, value=v)
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
    """Return (report_id, {expense_id: {(field, meaning): value}})."""
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


def verify_workbook(path: Path, data: WorkbookData) -> list[str]:
    """Differences between the written workbook and the records it came from."""
    report_id, rows = read_workbook(path)
    problems = []
    if report_id != data.report_id:
        problems.append(f"report ID {report_id!r} != {data.report_id!r}")
    columns = plan_columns(data.records)
    if set(rows) != {r.expense_id for r in data.records}:
        problems.append("expense ID set differs")
    for record in data.records:
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
    return problems


def exported_at() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
