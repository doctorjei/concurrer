"""Export an expense report to CSV for offline editing.

**Read-only.** This module never fills a field, never requests a write grant,
and never clicks anything that mutates. It may therefore target any report the
operator can read, including real ones — reading is not a write and the safety
guard is not involved.

The round trip is:

    python -m concurrer.expexp   --report <ID> --out expenses.csv
    # ...edit expenses.csv in a spreadsheet...
    python -m concurrer.pushcsv  --csv expenses.csv            # dry run
    python -m concurrer.pushcsv  --csv expenses.csv --commit   # Test Report only

Each expense is opened once to read its full field inventory, because the
report list page does not carry field detail (verified 2026-09-21: 24 rows,
141 buttons, no field-level data). That makes export linear in the number of
expenses — roughly 15-20s per expense for the open/read/return cycle.

**Fidelity is checked, not assumed.** After writing, the file is read back and
diffed against the live inventories captured moments earlier; a faithful
export must plan zero changes. If that check fails, the exporter is
misrepresenting live state and the file is not safe to push, so the run exits
non-zero rather than handing over a plausible-looking wrong file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .banners import parse_banner_actions
from .csvio import (
    EXPENSE_ID_COLUMN,
    ExpenseRecord,
    expense_cells,
    plan_push,
    read_expenses,
    write_expenses,
)
from .login import authenticated_context
from .nav import expense_row_number, open_expense_edit, open_report, wait_for_report_ready
from .secrets import STATE_DIR, resolve
from .triage import triage_rows


def _is_data_row(row: dict) -> bool:
    """Exclude the select-all header from export rows.

    The header's identity begins "Select all rows". It is filtered here as
    well as in the page-side JS because a header row leaking into an export
    would become a row with no expense ID, and a blank ID in a file destined
    for a push is exactly the kind of thing that should never reach disk.
    """
    identity = str(row.get("identity") or "").strip().casefold()
    if not identity:
        return False
    return not identity.startswith("select all")


def collect_report(
    page,
    report_id: str,
    *,
    max_expenses: int = 200,
    progress=lambda message: None,
) -> tuple[list[ExpenseRecord], dict[str, list[dict]]]:
    """Read every expense in a report. Returns records and live field state.

    The live inventories are returned alongside the records so the caller can
    verify the written file against the same data without a second pass.
    """
    open_report(page, report_id)
    wait_for_report_ready(page)

    rows = [row for row in triage_rows(page) if _is_data_row(row)]
    progress(f"{len(rows)} expense row(s) on report {report_id}")
    if len(rows) > max_expenses:
        progress(
            f"limiting to {max_expenses} of {len(rows)} rows "
            "(raise with --max)"
        )
        rows = rows[:max_expenses]

    records: list[ExpenseRecord] = []
    live_by_id: dict[str, list[dict]] = {}
    failures: list[str] = []

    for index, row in enumerate(rows):
        nav_row = expense_row_number(index)
        try:
            expense_id = open_expense_edit(page, report_id, nav_row)
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the export
            failures.append(f"row {nav_row}: {exc}")
            progress(f"  SKIP row {nav_row}: {exc}")
            continue
        from .widgets import field_inventory

        fields = field_inventory(page)
        records.append(
            ExpenseRecord(
                expense_id=expense_id,
                report_id=report_id,
                row_number=index + 1,
                cells=expense_cells(fields),
            )
        )
        live_by_id[expense_id] = fields
        progress(
            f"  read {expense_id[:12]}… "
            f"({len(records[-1].cells)} field(s)) {row['identity'][:44]}"
        )

    if failures:
        progress(f"{len(failures)} row(s) could not be read")
    return records, live_by_id


def verify_fidelity(
    csv_path: Path, live_by_id: dict[str, list[dict]]
) -> tuple[bool, str]:
    """Re-read the written CSV and require it to plan zero changes vs live.

    This is the exporter's acceptance criterion, checked on the real data it
    just wrote rather than only in unit tests. It catches quoting and encoding
    faults in the writer, which is where a subtly-wrong value would otherwise
    be discovered only after a push.
    """
    sheet = read_expenses(csv_path)
    push = plan_push(sheet, live_by_id)
    if push.total_changes == 0 and not push.has_errors:
        return True, "round-trip fidelity OK: file re-reads to zero changes"
    problems = []
    if push.total_changes:
        problems.append(
            f"{push.total_changes} field(s) differ from live after re-read — "
            "the writer is corrupting values"
        )
        for plan in push.actionable:
            for change in plan.changes:
                problems.append(
                    f"  {plan.expense_id} {change.name}: "
                    f"live={change.before!r} file={change.after!r}"
                )
    if push.has_errors:
        problems.append(
            f"file did not validate cleanly: unknown={push.unknown_ids} "
            f"duplicate={push.duplicate_ids} "
            f"blocked={[p.expense_id for p in push.blocked]}"
        )
    return False, "\n".join(problems)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Export a Concur expense report to CSV (read-only)."
    )
    ap.add_argument("--report", required=True)
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output CSV path (default: expenses-<report>.csv)",
    )
    ap.add_argument(
        "--max",
        type=int,
        default=200,
        help="maximum expenses to export (default 200)",
    )
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the read-back fidelity check (not recommended)",
    )
    ap.add_argument(
        "--banner-action",
        action="append",
        default=[],
        metavar="BANNER_ID=EXACT_LABEL",
    )
    ap.add_argument(
        "--quiet", action="store_true", help="suppress per-expense progress"
    )
    args = ap.parse_args()

    out_path = args.out or Path(f"expenses-{args.report[:12]}.csv")
    progress = (lambda _m: None) if args.quiet else (
        lambda m: print(m, file=sys.stderr, flush=True)
    )

    creds = resolve()
    banner_actions = parse_banner_actions(args.banner_action)

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=banner_actions,
    ) as page:
        records, live_by_id = collect_report(
            page, args.report, max_expenses=args.max, progress=progress
        )

    if not records:
        print("nothing exported: no readable expense rows", file=sys.stderr)
        return 1

    columns = write_expenses(out_path, records)
    print(
        f"\nwrote {out_path}  ({len(records)} expense(s), "
        f"{len(columns)} columns)"
    )
    print(f"  field columns: {[c for c in columns if c not in (EXPENSE_ID_COLUMN, 'report_id', '__locked', '__required', '__row')]}")

    if args.no_verify:
        print("  fidelity check: SKIPPED (--no-verify)")
        return 0

    ok, detail = verify_fidelity(out_path, live_by_id)
    print(f"  fidelity: {detail}")
    if not ok:
        print(
            "\nEXPORT NOT SAFE TO PUSH — the file does not re-read to the live "
            "values it was written from.",
            file=sys.stderr,
        )
        return 1

    # Keep the live snapshot next to the CSV so a later dry-run can be
    # reproduced without re-reading the report.
    snapshot = out_path.with_suffix(".live.json")
    snapshot.write_text(json.dumps({"report": args.report, "live": live_by_id}))
    snapshot.chmod(0o600)
    print(f"  live snapshot: {snapshot}")
    print("\nnext: python -m concurrer.pushcsv --csv " + str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
