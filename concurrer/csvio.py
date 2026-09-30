"""Pure-stdlib CSV round-trip for Concur expenses: schema, diff, validation.

Deliberately **browser-free**. Every rule that decides *what would be written*
lives here so it can be unit-tested without a live session, a browser, or a
credential. The CLI modules that touch Concur are thin; the reasoning is here,
where the cheap tests can reach it.

Three rules carry most of the safety in this module:

1. **Blank means "no change", never "clear".** The editor refuses to clear a
   field, and an empty CSV cell is far more likely to mean "I did not touch
   this" than "erase it". Treating blank as clear would turn a careless
   save-as into a mass wipe, so it is not supported at all.

2. **Diff against live state, never against the CSV's own origin.** A push
   compares each row to what Concur says *right now*. If the report changed
   since export, the operator sees the real current value in the diff rather
   than silently clobbering it.

3. **Locked fields are refused, not skipped.** A locked field the operator
   edited is an error worth surfacing, not something to quietly drop.

Column naming: identity columns are fixed, meta columns are prefixed ``__`` so
they cannot collide with a Concur field name, and every other column is a
stable field name as reported by ``field_inventory()``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EXPENSE_ID_COLUMN = "expense_id"
REPORT_ID_COLUMN = "report_id"

#: Informational columns. Prefixed so no Concur field name can collide, and
#: ignored entirely on push.
LOCKED_COLUMN = "__locked"
REQUIRED_COLUMN = "__required"
ROW_COLUMN = "__row"
META_COLUMNS = (LOCKED_COLUMN, REQUIRED_COLUMN, ROW_COLUMN)
IDENTITY_COLUMNS = (EXPENSE_ID_COLUMN, REPORT_ID_COLUMN)

#: Field names that are never writable through the CSV path even when the live
#: form reports them unlocked. ``expenseType`` changes silently discard
#: type-specific custom values, which is far too destructive to do in bulk on
#: the strength of a spreadsheet cell; it stays a one-at-a-time ``edit.py``
#: operation where the loss report is read directly.
BULK_REFUSED_FIELDS = ("expenseType",)


class CsvError(ValueError):
    """A CSV file or a row in it cannot be safely interpreted."""


def _norm(text: Any) -> str:
    """Whitespace-and-case-insensitive comparison key.

    Matches ``edit.py``'s ``_norm`` deliberately: the exporter, the differ,
    and the editor must agree on what "same value" means, or a push rewrites
    fields that never changed.
    """
    return " ".join(str(text if text is not None else "").split()).casefold()


def stable_name(field: Mapping[str, Any]) -> str:
    """The canonical, writable name for an inventoried field.

    **``name`` alone is not sufficient and this is not hypothetical.** On the
    live edit form, Transaction Date reports ``name == ""`` while its stable
    handle is present only in ``nuiexp`` as ``field-transactionDate``. A
    keying scheme built on ``name`` silently loses such fields, and a lost
    field in an export is a field the operator cannot see or fix.

    Resolution order: the reported ``name``, then a name derived from a
    ``field-<name>`` entry in ``nuiexp``. Suffix variants (``__trigger``,
    ``__clear``, ``__input``) are decorations on the same field and are
    stripped.
    """
    name = str(field.get("name") or "").strip()
    if name:
        return name
    for nuiexp in field.get("nuiexp") or ():
        text = str(nuiexp)
        if not text.startswith("field-"):
            continue
        candidate = text[len("field-"):]
        for suffix in ("__trigger", "__clear", "__input"):
            if candidate.endswith(suffix):
                candidate = candidate[: -len(suffix)]
                break
        if candidate:
            return candidate
    return ""


@dataclass(frozen=True)
class FieldCell:
    """One field's exported value plus the context the operator needs."""

    name: str
    label: str
    value: str
    locked: bool
    required: bool


def expense_cells(fields: Sequence[Mapping[str, Any]]) -> list[FieldCell]:
    """Flatten a live ``field_inventory()`` into exportable cells.

    Fields with no resolvable stable name are dropped rather than exported
    under a blank column header, which would be an unaddressable column that
    push could never key on.
    """
    cells: list[FieldCell] = []
    seen: set[str] = set()
    for item in fields:
        name = stable_name(item)
        if not name or name in seen:
            continue
        seen.add(name)
        cells.append(
            FieldCell(
                name=name,
                label=str(item.get("label") or name),
                value=str(item.get("value") or ""),
                locked=bool(item.get("locked")),
                required=bool(item.get("required")),
            )
        )
    return cells


@dataclass
class ExpenseRecord:
    """One expense, as it will appear as one CSV row."""

    expense_id: str
    report_id: str
    row_number: int | None
    cells: list[FieldCell]

    def to_csv_row(self, columns: Sequence[str]) -> dict[str, str]:
        row = {
            EXPENSE_ID_COLUMN: self.expense_id,
            REPORT_ID_COLUMN: self.report_id,
            ROW_COLUMN: "" if self.row_number is None else str(self.row_number),
        }
        by_name = {cell.name: cell for cell in self.cells}
        for column in columns:
            if column in IDENTITY_COLUMNS or column in META_COLUMNS:
                continue
            cell = by_name.get(column)
            row[column] = cell.value if cell else ""
        row[LOCKED_COLUMN] = ";".join(
            sorted(cell.name for cell in self.cells if cell.locked)
        )
        row[REQUIRED_COLUMN] = ";".join(
            sorted(cell.name for cell in self.cells if cell.required)
        )
        return {column: row.get(column, "") for column in columns}


def union_columns(records: Sequence[ExpenseRecord]) -> list[str]:
    """Column order: identity, then fields in first-seen order, then meta.

    First-seen rather than alphabetical so the columns read in the order the
    form presents them, which is the order the operator is thinking in.
    """
    seen: dict[str, None] = {}
    for record in records:
        for cell in record.cells:
            seen.setdefault(cell.name, None)
    return [*IDENTITY_COLUMNS, *seen.keys(), *META_COLUMNS]


def write_expenses(path: Path, records: Sequence[ExpenseRecord]) -> list[str]:
    """Write records as CSV and return the column order used."""
    columns = union_columns(records)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so a spreadsheet reads UTF-8 correctly on open; Concur data
    # carries typographic characters in vendor names and labels.
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for record in records:
            writer.writerow(record.to_csv_row(columns))
    return columns


@dataclass
class Sheet:
    """A parsed CSV: which columns are fields, and the rows.

    ``field_columns`` excludes identity and meta columns, so it is exactly the
    writable surface as declared by the file's own header.
    """

    field_columns: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]

    @property
    def expense_ids(self) -> tuple[str, ...]:
        return tuple(row.get(EXPENSE_ID_COLUMN, "") for row in self.rows)


def read_expenses(path: Path) -> Sheet:
    """Parse a CSV written by :func:`write_expenses`.

    Validates the header rather than guessing at it. A file missing
    ``expense_id`` cannot be keyed on anything safe, so it is refused outright
    instead of falling back to row order, which is exactly the identity
    confusion this design exists to prevent.
    """
    source = Path(path)
    if not source.is_file():
        raise CsvError(f"no such file: {source}")
    # utf-8-sig transparently handles the BOM we write and plain UTF-8 files
    # edited by tools that drop it.
    with source.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        if EXPENSE_ID_COLUMN not in header:
            raise CsvError(
                f"{source.name} has no {EXPENSE_ID_COLUMN!r} column; refusing to "
                "guess row identity. Re-export with the export CLI."
            )
        rows = tuple(reader)
    field_columns = tuple(
        column
        for column in header
        if column not in IDENTITY_COLUMNS and column not in META_COLUMNS
    )
    return Sheet(field_columns=field_columns, rows=rows)


@dataclass
class FieldChange:
    """One field on one expense that push would write."""

    name: str
    before: str
    after: str


@dataclass
class RowPlan:
    """What push would do to one expense."""

    expense_id: str
    changes: list[FieldChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return bool(self.changes) and not self.errors


@dataclass
class PushPlan:
    """The whole-file decision: what changes, what is refused, what is unchanged."""

    row_plans: list[RowPlan] = field(default_factory=list)
    unknown_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> list[RowPlan]:
        return [plan for plan in self.row_plans if plan.actionable]

    @property
    def blocked(self) -> list[RowPlan]:
        return [plan for plan in self.row_plans if plan.errors]

    @property
    def total_changes(self) -> int:
        return sum(len(plan.changes) for plan in self.actionable)

    @property
    def has_errors(self) -> bool:
        return bool(
            self.unknown_ids
            or self.duplicate_ids
            or any(plan.errors for plan in self.row_plans)
        )


def plan_row(
    row: Mapping[str, str],
    live_fields: Sequence[Mapping[str, Any]] | None,
    field_columns: Sequence[str],
) -> RowPlan:
    """Diff one CSV row against one expense's fresh live field inventory.

    ``live_fields`` is ``None`` when the expense does not exist live; the
    caller decides whether that is an unknown-id error, so this function does
    not have to know how the lookup was done.
    """
    expense_id = str(row.get(EXPENSE_ID_COLUMN) or "").strip()
    plan = RowPlan(expense_id=expense_id)
    if live_fields is None:
        return plan

    cells = {cell.name: cell for cell in expense_cells(live_fields)}

    for column in field_columns:
        raw = row.get(column)
        if raw is None:
            continue
        # Blank means untouched. See module docstring rule 1.
        if not str(raw).strip():
            continue
        if column not in cells:
            plan.errors.append(
                f"{column}: not a field on this expense's live form"
            )
            continue
        before = cells[column].value
        after = str(raw)
        # Unchanged values are skipped BEFORE any refusal is considered. An
        # exported file carries every field's current value, including
        # ``expenseType`` and locked fields; refusing on mere presence would
        # make a faithful round-trip unpushable and would bury the operator's
        # real errors under a wall of noise. Only a genuine difference here
        # is worth refusing.
        if _norm(before) == _norm(after):
            continue
        if column in BULK_REFUSED_FIELDS:
            plan.errors.append(
                f"{column}: bulk type changes are refused; change Expense Type "
                "one at a time with `python -m concurrer.edit` so the value-loss "
                "report is read directly"
            )
            continue
        if cells[column].locked:
            plan.errors.append(
                f"{column} ({cells[column].label}) is locked by Concur and "
                "cannot be written"
            )
            continue
        plan.changes.append(FieldChange(name=column, before=before, after=after))
    return plan


def plan_push(
    sheet: Sheet,
    live_by_id: Mapping[str, Sequence[Mapping[str, Any]]],
) -> PushPlan:
    """Diff a whole sheet against live state keyed by stable expense ID."""
    push = PushPlan()
    seen: set[str] = set()
    for row in sheet.rows:
        expense_id = str(row.get(EXPENSE_ID_COLUMN) or "").strip()
        if not expense_id:
            push.unknown_ids.append("<blank expense_id>")
            continue
        if expense_id in seen:
            push.duplicate_ids.append(expense_id)
            continue
        seen.add(expense_id)
        if expense_id not in live_by_id:
            push.unknown_ids.append(expense_id)
            continue
        push.row_plans.append(
            plan_row(row, live_by_id[expense_id], sheet.field_columns)
        )
    return push


def format_plan(push: PushPlan, *, verbose: bool = True) -> str:
    """Human-readable dry-run report.

    The dry run is the operator's review surface, so it prints every field
    before/after rather than a count. A count is not reviewable.
    """
    lines: list[str] = []
    actionable = push.actionable
    lines.append(
        f"\nPUSH PLAN — {len(actionable)} expense(s) would change, "
        f"{push.total_changes} field value(s) total"
    )
    for plan in actionable:
        lines.append(f"\n  expense {plan.expense_id}")
        for change in plan.changes:
            lines.append(f"    {change.name}: {change.before!r} -> {change.after!r}")
    if push.blocked:
        lines.append(f"\n  BLOCKED — {len(push.blocked)} expense(s) refused:")
        for plan in push.blocked:
            lines.append(f"    expense {plan.expense_id}")
            for problem in plan.errors:
                lines.append(f"      - {problem}")
    if push.unknown_ids:
        lines.append(
            f"\n  UNKNOWN expense_id(s) — {len(push.unknown_ids)}: "
            + ", ".join(push.unknown_ids)
            + " (never created; push writes only to expenses that already exist)"
        )
    if push.duplicate_ids:
        lines.append(
            f"\n  DUPLICATE expense_id(s): {', '.join(push.duplicate_ids)}"
        )
    if not actionable and not push.has_errors:
        lines.append("\n  No changes. The CSV matches live state exactly.")
    return "\n".join(lines)


def records_from_inventories(
    inventories: Iterable[tuple[str, str, int | None, Sequence[Mapping[str, Any]]]],
) -> list[ExpenseRecord]:
    """Adapt ``(expense_id, report_id, row_number, fields)`` tuples to records.

    Exists so the export CLI can stream what it reads without building
    intermediate dicts, and so this module stays the only place that knows the
    row shape.
    """
    return [
        ExpenseRecord(
            expense_id=expense_id,
            report_id=report_id,
            row_number=row_number,
            cells=expense_cells(fields),
        )
        for expense_id, report_id, row_number, fields in inventories
    ]
