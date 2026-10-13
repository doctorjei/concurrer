"""Review or safely apply a modified Concur expense CSV.

This is a **batch driver, not a bulk writer**. It reuses the one-expense editor's
strict plan, fill, validation, safety-guarded Save, and direct-ID persistence
verification. No CSV field gets a separate mutation primitive here.

Default mode is a fresh-live-state dry run::

    python -m concurrer.pushcsv --csv expenses.csv

``--commit`` is structurally limited to the throwaway Test Report, plus the
one confirmed real report (``safety.CONFIRMED_REPORT_IDS``). The report ID
comes from the CSV's required identity column; any other real-report CSV fails
before credentials are resolved or a browser is opened::

    python -m concurrer.pushcsv --csv test-report.csv --commit

A confirmed real report additionally needs the plan token its dry run printed.
The commit re-reads live state, rebuilds the plan, and refuses unless the token
matches, so only the exact reviewed plan can be written::

    python -m concurrer.pushcsv --csv cced.csv                    # prints PLAN TOKEN
    python -m concurrer.pushcsv --csv cced.csv --commit --confirm <token>

A batch is not transactional — Concur saves each expense individually. To make
that limitation honest rather than surprising, *all* rows are preflighted
before the first mutation, each row is revalidated immediately before it saves,
and any execution failure stops the batch and names prior rows that did save and
verify. There is never an attempted rollback: especially for type-sensitive
expenses, a reverse fill cannot honestly restore lost state.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .banners import parse_banner_actions
from .csvio import (
    EXPENSE_ID_COLUMN,
    FieldChange,
    PushPlan,
    RowPlan,
    Sheet,
    format_plan,
    plan_push,
    read_expenses,
    single_report_id,
)
from .edit import (
    EditPlan,
    EditRequest,
    PlanError,
    build_edit_plan,
    persist_and_verify_plan,
)
from .login import authenticated_context
from .nav import open_expense_by_id
from .safety import (
    CONFIRMED_REPORT_IDS,
    plan_token,
    test_report_write_grant,
    write_grant,
)
from .secrets import resolve
from .widgets import field_inventory


class BatchPlanError(ValueError):
    """A CSV batch is not clean enough to mutate."""


@dataclass
class BatchPreflight:
    """Full review state captured before a batch may mutate anything."""

    report_id: str
    sheet: Sheet
    push: PushPlan
    edit_plans: dict[str, EditPlan] = field(default_factory=dict)
    inventory_failures: dict[str, str] = field(default_factory=dict)
    edit_failures: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        expected = {_id_key(row_plan.expense_id) for row_plan in self.push.actionable}
        return not (
            self.push.has_errors
            or self.inventory_failures
            or self.edit_failures
            or set(self.edit_plans) != expected
        )

    @property
    def actionable(self) -> list[RowPlan]:
        return self.push.actionable


def _id_key(expense_id: str) -> str:
    """Case-insensitive Concur ID key, matching csvio's route semantics."""
    return str(expense_id).strip().upper()


def _row_for_id(sheet: Sheet, expense_id: str) -> Mapping[str, str]:
    """Return the one CSV row matching an actionable plan's stable ID."""
    wanted = _id_key(expense_id)
    rows = [
        row
        for row in sheet.rows
        if _id_key(str(row.get(EXPENSE_ID_COLUMN) or "")) == wanted
    ]
    if len(rows) != 1:
        # This cannot occur for a clean plan: csvio catches duplicate IDs, and
        # a RowPlan only exists for a row in this Sheet. Keep it a hard failure
        # rather than allowing an orchestration bug to choose one silently.
        raise BatchPlanError(
            f"expected exactly one CSV row for expense {expense_id!r}, found {len(rows)}"
        )
    return rows[0]


def request_for(row_plan: RowPlan, report_id: str) -> EditRequest:
    """Turn a CSV field diff into the existing single-expense request object."""
    if row_plan.errors or not row_plan.changes:
        raise BatchPlanError(
            f"expense {row_plan.expense_id}: cannot build an edit request from "
            "a blocked or empty row plan"
        )
    assignments = {change.name: change.after for change in row_plan.changes}
    if len(assignments) != len(row_plan.changes):
        raise BatchPlanError(
            f"expense {row_plan.expense_id}: duplicate field changes in row plan"
        )
    return EditRequest(
        report_id=report_id,
        expense_id=row_plan.expense_id,
        row=None,
        assignments=assignments,
        save=True,
    )


def _change_signature(changes: Sequence[FieldChange]) -> tuple[tuple[str, str, str], ...]:
    """The exact reviewed field contract for one row, in CSV/form order."""
    return tuple((change.name, change.before, change.after) for change in changes)


def _one_row_plan(
    sheet: Sheet,
    row: Mapping[str, str],
    expense_id: str,
    fields: list[dict],
) -> RowPlan:
    """Freshly diff one source row against the just-opened live form."""
    single = Sheet(field_columns=sheet.field_columns, rows=(row,))
    refreshed = plan_push(single, {expense_id: fields})
    if refreshed.unknown_ids or refreshed.duplicate_ids:
        raise BatchPlanError(
            f"expense {expense_id}: direct route opened but one-row diff lost identity "
            f"(unknown={refreshed.unknown_ids}, duplicate={refreshed.duplicate_ids})"
        )
    if len(refreshed.row_plans) != 1:
        raise BatchPlanError(
            f"expense {expense_id}: fresh one-row diff produced "
            f"{len(refreshed.row_plans)} row plans"
        )
    return refreshed.row_plans[0]


def _live_inventories(page, sheet: Sheet, report_id: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Read each unique, nonblank expense ID directly; preserve all failures."""
    inventories: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    seen: set[str] = set()
    for raw in sheet.expense_ids:
        expense_id = str(raw or "").strip()
        if not expense_id:
            # csvio.plan_push reports this as a standard unknown-id error. Do
            # not send a blank route to the browser just to produce duplicate
            # diagnostics.
            continue
        key = _id_key(expense_id)
        if key in seen:
            # csvio reports duplicates. One live read is sufficient for the
            # complete review surface and avoids opening an expense twice.
            continue
        seen.add(key)
        try:
            opened = open_expense_by_id(page, report_id, expense_id)
            inventories[_id_key(opened)] = field_inventory(page)
        except Exception as exc:  # noqa: BLE001 - report every readable failure
            failures[expense_id] = f"{type(exc).__name__}: {exc}"
    return inventories, failures


def preflight_batch(page, sheet: Sheet, report_id: str) -> BatchPreflight:
    """Read fresh state and build every reviewable plan without writing."""
    live_by_id, inventory_failures = _live_inventories(page, sheet, report_id)
    push = plan_push(sheet, live_by_id)
    preflight = BatchPreflight(
        report_id=report_id,
        sheet=sheet,
        push=push,
        inventory_failures=inventory_failures,
    )
    for row_plan in push.actionable:
        try:
            row = _row_for_id(sheet, row_plan.expense_id)
            fields = live_by_id[_id_key(row_plan.expense_id)]
            preflight.edit_plans[_id_key(row_plan.expense_id)] = build_edit_plan(
                request_for(row_plan, report_id), fields
            )
        except Exception as exc:  # noqa: BLE001 - planner failure belongs in review
            preflight.edit_failures[row_plan.expense_id] = f"{type(exc).__name__}: {exc}"
    return preflight


def format_preflight(preflight: BatchPreflight) -> str:
    """Append browser/editor diagnostics to csvio's complete plan output."""
    parts = [format_plan(preflight.push)]
    if preflight.inventory_failures:
        lines = ["\n  LIVE READ FAILURES — no mutation is allowed:"]
        for expense_id, why in preflight.inventory_failures.items():
            lines.append(f"    expense {expense_id}: {why}")
        parts.append("\n".join(lines))
    if preflight.edit_failures:
        lines = ["\n  EDIT-PLAN FAILURES — no mutation is allowed:"]
        for expense_id, why in preflight.edit_failures.items():
            lines.append(f"    expense {expense_id}: {why}")
        parts.append("\n".join(lines))
    if not preflight.clean:
        parts.append(
            "\n  BATCH BLOCKED — --commit requires every row to pass preflight; "
            "no rows will be silently skipped."
        )
    return "\n".join(parts)


def _replan_for_write(page, preflight: BatchPreflight, original: RowPlan) -> tuple[EditPlan, list[dict]]:
    """Require write-time state to match the operator-reviewed row exactly."""
    expense_id = original.expense_id
    row = _row_for_id(preflight.sheet, expense_id)
    opened = open_expense_by_id(page, preflight.report_id, expense_id)
    fields = field_inventory(page)
    fresh = _one_row_plan(preflight.sheet, row, opened, fields)
    if fresh.errors:
        raise BatchPlanError(
            f"expense {expense_id}: live state now blocks the reviewed change: "
            + "; ".join(fresh.errors)
        )
    if _change_signature(fresh.changes) != _change_signature(original.changes):
        raise BatchPlanError(
            f"expense {expense_id}: live state drifted since preflight; refusing "
            f"to replace reviewed changes {_change_signature(original.changes)!r} "
            f"with {_change_signature(fresh.changes)!r}"
        )
    return build_edit_plan(request_for(fresh, preflight.report_id), fields), fields


def commit_batch(page, preflight: BatchPreflight) -> int:
    """Persist a clean batch in CSV order, stopping honestly on first failure."""
    if not preflight.clean:
        raise BatchPlanError("refusing commit: batch preflight is not clean")

    committed: list[str] = []
    for original in preflight.actionable:
        expense_id = original.expense_id
        save_attempted = False
        phase = "write-time revalidation"
        try:
            plan, before = _replan_for_write(page, preflight, original)
            phase = "apply, Save, and direct-ID verification"

            def mark_save_attempt() -> None:
                nonlocal save_attempted
                save_attempted = True

            persist_and_verify_plan(
                page,
                preflight.report_id,
                expense_id,
                plan,
                before,
                on_save_attempt=mark_save_attempt,
            )
        except Exception as exc:  # noqa: BLE001 - report precise operational state
            print(
                f"\nBATCH STOPPED at expense {expense_id} during {phase}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if committed:
                print(
                    "Previously saved and verified (they remain saved; Concur has "
                    f"no batch rollback): {', '.join(committed)}",
                    file=sys.stderr,
                )
            if save_attempted:
                print(
                    f"Current expense {expense_id} may have saved; its final state "
                    "is unknown because Save was attempted but verification failed. "
                    "Inspect it manually before retrying.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Current expense {expense_id} failed before Save was attempted; "
                    "it was not persisted by this command.",
                    file=sys.stderr,
                )
            return 1
        committed.append(expense_id)
        print(f"saved and verified: {expense_id}")

    if committed:
        print(f"\nBATCH COMMITTED AND VERIFIED — {len(committed)} expense(s)")
    else:
        print("\nNo changes. The file already matches live state; nothing saved.")
    return 0


def batch_token(preflight: BatchPreflight) -> str:
    """The reviewable fingerprint of exactly what this batch would write."""
    return plan_token(preflight.report_id, [
        (_id_key(row_plan.expense_id), change.name, change.before, change.after)
        for row_plan in preflight.actionable
        for change in row_plan.changes
    ])


def _run(
    sheet: Sheet,
    report_id: str,
    *,
    commit: bool,
    banner_actions: Mapping[str, str],
    confirm: str | None = None,
) -> int:
    """Authenticate once, preflight all rows, then optionally commit."""
    creds = resolve()
    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=banner_actions,
    ) as page:
        preflight = preflight_batch(page, sheet, report_id)
        print(format_preflight(preflight))
        if not preflight.clean:
            return 2
        confirmed = report_id in CONFIRMED_REPORT_IDS
        if confirmed:
            token = batch_token(preflight)
            print(f"\n  PLAN TOKEN: {token}  (real report {report_id}; to write "
                  f"exactly this plan: --commit --confirm {token})")
        if not commit:
            print("\nDry run only — no fields filled, no write grant requested, no Save clicked.")
            return 0
        if confirmed:
            # Raises before any fill unless the reviewed token equals the plan
            # just rebuilt from live state.
            with write_grant(report_id, reviewed_token=confirm, live_token=token):
                return commit_batch(page, preflight)
        return commit_batch(page, preflight)


def run_commit(
    sheet: Sheet,
    report_id: str,
    *,
    confirm: str | None,
    banner_actions: Mapping[str, str],
) -> int:
    """CLI ``--commit`` entry shared by the CSV and xlsx drivers.

    Every refusal here happens before credentials or a browser. The Test Report
    keeps its token-free grant; a confirmed real report must carry a token,
    which ``_run`` then checks against the live plan.
    """
    if report_id in CONFIRMED_REPORT_IDS:
        if not confirm:
            raise AssertionError(
                f"refusing --commit on real report {report_id!r} without "
                "--confirm <plan token>; run the dry run first and review its plan"
            )
        return _run(sheet, report_id, commit=True, confirm=confirm,
                    banner_actions=banner_actions)
    # The grant refuses any other real report first, so its boundary message
    # wins over the --confirm misuse below.
    with test_report_write_grant(report_id):
        if confirm:
            raise ValueError("--confirm applies only to a confirmed real report")
        return _run(sheet, report_id, commit=True, banner_actions=banner_actions)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Review or safely apply a Concur expense CSV."
    )
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument(
        "--commit",
        action="store_true",
        help="persist only when the CSV identifies the Test Report or the "
        "confirmed real report",
    )
    ap.add_argument(
        "--confirm",
        metavar="PLAN_TOKEN",
        help="with --commit on a confirmed real report: the PLAN TOKEN its dry run printed",
    )
    ap.add_argument(
        "--banner-action",
        action="append",
        default=[],
        metavar="BANNER_ID=EXACT_LABEL",
        help="one explicit response to a displayed sign-in banner; repeatable",
    )
    args = ap.parse_args()

    try:
        sheet = read_expenses(args.csv)
        report_id = single_report_id(sheet)
        banner_actions = parse_banner_actions(args.banner_action)
        if args.confirm and not args.commit:
            raise ValueError("--confirm is only meaningful with --commit")
        if args.commit:
            # The structural boundary must precede *everything* that can touch
            # credentials or a browser. No CLI argument can widen it: report ID
            # is required inside the CSV and checked here before resolve().
            return run_commit(sheet, report_id, confirm=args.confirm,
                              banner_actions=banner_actions)
        return _run(sheet, report_id, commit=False, banner_actions=banner_actions)
    except (BatchPlanError, PlanError, ValueError, AssertionError) as exc:
        print(f"pushcsv plan error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports safe failure
        print(f"pushcsv failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
