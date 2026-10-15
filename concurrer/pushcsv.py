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
    TYPE_FIELD,
    format_plan,
    plan_push,
    read_expenses,
    single_report_id,
)
from .edit import (
    EditPlan,
    EditRequest,
    PlanError,
    TypeChangeConfirmation,
    build_edit_plan,
    is_attendee_removal,
    persist_and_verify_plan,
    wait_for_type_cascade,
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
from .widgets import (
    field_by_name,
    field_inventory,
    cancel_blocking_dialog,
    select_combobox_exact,
    settled_inventory,
)


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
    #: Operator acceptance of Concur's attendee-removal prompt (token-covered).
    accept_attendee_removal: bool = False
    #: Expense key -> what an unsaved in-form type switch actually showed.
    rehearsals: dict[str, "Rehearsal"] = field(default_factory=dict)

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


def request_for(row_plan: RowPlan, report_id: str, *,
                accept_attendee_removal: bool = False) -> EditRequest:
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
    type_change = next(
        (change for change in row_plan.changes if change.name == TYPE_FIELD), None)
    if type_change is None:
        return EditRequest(
            report_id=report_id,
            expense_id=row_plan.expense_id,
            row=None,
            assignments=assignments,
            save=True,
        )
    # Same single-expense type-change path as ``edit --type``: the live type
    # must still be the reviewed "before", and the change is acknowledged
    # explicitly. Type first, then the target type's fields.
    del assignments[TYPE_FIELD]
    return EditRequest(
        report_id=report_id,
        expense_id=row_plan.expense_id,
        row=None,
        assignments=assignments,
        target_type=type_change.after,
        expected_type=type_change.before,
        allow_type_change=True,
        save=True,
        accept_attendee_removal=accept_attendee_removal,
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
    single = Sheet(field_columns=sheet.field_columns, rows=(row,),
                   allow_type_changes=sheet.allow_type_changes)
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


def preflight_batch(page, sheet: Sheet, report_id: str, *,
                    accept_attendee_removal: bool = False) -> BatchPreflight:
    """Read fresh state and build every reviewable plan without writing."""
    # A row with no nonblank field value carries no edit (blank means no
    # change; the workbook reader already drops cells equal to the export),
    # so it needs no live read. Skipping it turns a 144-row read into one per
    # edited row; such rows could never contribute a change or a token part.
    edited = tuple(row for row in sheet.rows
                   if any(str(row.get(c) or "").strip() for c in sheet.field_columns))
    sheet = Sheet(field_columns=sheet.field_columns, rows=edited,
                  allow_type_changes=sheet.allow_type_changes)
    live_by_id, inventory_failures = _live_inventories(page, sheet, report_id)
    push = plan_push(sheet, live_by_id)
    preflight = BatchPreflight(
        report_id=report_id,
        sheet=sheet,
        push=push,
        inventory_failures=inventory_failures,
        accept_attendee_removal=accept_attendee_removal,
    )
    for row_plan in push.actionable:
        try:
            row = _row_for_id(sheet, row_plan.expense_id)
            fields = live_by_id[_id_key(row_plan.expense_id)]
            preflight.edit_plans[_id_key(row_plan.expense_id)] = build_edit_plan(
                request_for(row_plan, report_id,
                            accept_attendee_removal=accept_attendee_removal), fields
            )
        except Exception as exc:  # noqa: BLE001 - planner failure belongs in review
            preflight.edit_failures[row_plan.expense_id] = f"{type(exc).__name__}: {exc}"
    return preflight


@dataclass
class Rehearsal:
    """Facts from switching one expense's type in the form without saving."""

    drops: list[dict]
    missing: list[str]
    #: Text of a confirmation Concur demanded before re-rendering; blocking.
    confirmation: str | None = None


def _rehearsal_grant(report_id: str, expense_id: str, current: str, target: str):
    """Grant only the in-form type selection of one rehearsal.

    A confirmed real report's token is derived from this exact rehearsal; the
    operator opted in with ``--rehearse`` for the run. The Test Report uses its
    ordinary grant. Every other report is refused by ``authorize_writes``.
    """
    if report_id in CONFIRMED_REPORT_IDS:
        token = plan_token(report_id, [(expense_id, "rehearsal:expenseType", current, target)])
        return write_grant(report_id, reviewed_token=token, live_token=token)
    return test_report_write_grant(report_id)


def _values(fields: list[dict]) -> dict[str, str]:
    return {f["name"]: f["value"] for f in fields if f.get("name")}


def rehearse_one(page, report_id: str, expense_id: str, plan: EditPlan) -> Rehearsal:
    """Switch type in-form, record the target form, abandon, prove no change.

    Never clicks Save. Leaving is a direct-ID reopen, the same abandonment the
    67-type sweep relied on; the reopen must match the opening state exactly.
    """
    open_expense_by_id(page, report_id, expense_id)
    before = field_inventory(page)
    live_type = field_by_name(before, "expenseType")["value"]
    if live_type != plan.current_type:
        raise BatchPlanError(
            f"live type is {live_type!r}, reviewed plan expects {plan.current_type!r}")
    confirmation = None
    after: list[dict] = []
    with _rehearsal_grant(report_id, expense_id, plan.current_type, plan.target_type):
        select_combobox_exact(page, field_by_name(before, "expenseType"), plan.target_type)
        try:
            after = wait_for_type_cascade(page, plan.target_type)
        except TypeChangeConfirmation as exc:
            confirmation = exc.text
            cancel_blocking_dialog(page)
    open_expense_by_id(page, report_id, expense_id)
    reopened = field_inventory(page)
    if _values(reopened) != _values(before):
        raise BatchPlanError(
            "REHEARSAL LEFT A CHANGE — inspect this expense manually: "
            f"before={_values(before)!r} reopened={_values(reopened)!r}")
    names_after = set(_values(after))
    drops = [
        {"name": f["name"], "label": f["label"], "value": f["value"]}
        for f in before
        if f.get("name") and f["name"] not in names_after and str(f["value"]).strip()
    ]
    if confirmation is not None:
        return Rehearsal([], [], confirmation)
    missing = [name for name in plan.changes if name not in names_after]
    return Rehearsal(drops, missing)


def rehearse_type_changes(page, preflight: BatchPreflight) -> None:
    """Rehearse every type-changing row; a problem blocks the whole batch."""
    for key, plan in preflight.edit_plans.items():
        if plan.current_type == plan.target_type:
            continue
        try:
            rehearsal = rehearse_one(page, preflight.report_id, key, plan)
        except Exception as exc:  # noqa: BLE001 - a failed rehearsal is a review fact
            preflight.edit_failures[key] = f"rehearsal failed: {type(exc).__name__}: {exc}"
            continue
        preflight.rehearsals[key] = rehearsal
        if rehearsal.confirmation and not (
                preflight.accept_attendee_removal and is_attendee_removal(rehearsal.confirmation)):
            preflight.edit_failures[key] = (
                f"rehearsal: Concur demands confirmation for {plan.target_type!r} "
                f"and would remove data: {rehearsal.confirmation!r}")
        elif rehearsal.missing:
            preflight.edit_failures[key] = (
                f"rehearsal: the {plan.target_type!r} form has no "
                f"{', '.join(rehearsal.missing)} for this expense")


def format_preflight(preflight: BatchPreflight) -> str:
    """Append browser/editor diagnostics to csvio's complete plan output."""
    parts = [format_plan(preflight.push)]
    type_lines = []
    for expense_id, plan in preflight.edit_plans.items():
        if getattr(plan, "current_type", None) == getattr(plan, "target_type", None):
            continue
        type_lines.append(
            f"    expense {expense_id}: {plan.current_type!r} -> {plan.target_type!r}")
        for loss in plan.losses:
            type_lines.append(
                f"      DISCARDS {loss['label']} ({loss['name']}) = {loss['value']!r}")
        if not plan.losses:
            type_lines.append("      discards no nonblank custom value")
        rehearsal = preflight.rehearsals.get(expense_id)
        if rehearsal is not None and rehearsal.confirmation:
            accepted = (preflight.accept_attendee_removal
                        and is_attendee_removal(rehearsal.confirmation))
            type_lines.append(
                "      WILL REMOVE (Concur confirmation, rehearsed; "
                + ("ACCEPTED by --accept-attendee-removal): " if accepted else "batch blocked): ")
                + " / ".join(rehearsal.confirmation.split("\n")))
            continue
        if rehearsal is not None:
            custom = {loss["name"] for loss in plan.losses}
            for drop in rehearsal.drops:
                if drop["name"] not in custom:
                    type_lines.append(
                        f"      WILL DISCARD {drop['label']} ({drop['name']}) = "
                        f"{drop['value']!r}  [rehearsed]")
            if not rehearsal.drops:
                type_lines.append("      rehearsed: the target form keeps every nonblank value")
            continue
        for loss in getattr(plan, "possible_losses", ()):
            type_lines.append(
                f"      MAY DISCARD {loss['label']} ({loss['name']}) = {loss['value']!r}"
                f"  [not on the blank {plan.target_type!r} form; unverified]")
    if type_lines:
        parts.append("\n  TYPE CHANGES (type is set first; Concur then drops "
                     "fields the new type lacks):\n" + "\n".join(type_lines))
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
    return build_edit_plan(request_for(
        fresh, preflight.report_id,
        accept_attendee_removal=preflight.accept_attendee_removal), fields), fields


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
    changes = [
        (_id_key(row_plan.expense_id), change.name, change.before, change.after)
        for row_plan in preflight.actionable
        for change in row_plan.changes
    ]
    # Discarded values are part of what the operator approves: a token for a
    # type change must not survive a live value that would now be lost.
    losses = [
        (expense_id, f"{kind}:{loss['name']}", loss["value"], "")
        for expense_id, plan in preflight.edit_plans.items()
        for kind, attr in (("discard", "losses"), ("maybe", "possible_losses"))
        for loss in getattr(plan, attr, ())
    ]
    accepted = ([("*", "accept", "attendee-removal", "")]
                if preflight.accept_attendee_removal else [])
    return plan_token(preflight.report_id, changes + losses + accepted)


def _run(
    sheet: Sheet,
    report_id: str,
    *,
    commit: bool,
    banner_actions: Mapping[str, str],
    confirm: str | None = None,
    rehearse: bool = False,
    accept_attendee_removal: bool = False,
) -> int:
    """Authenticate once, preflight all rows, then optionally commit."""
    if rehearse and commit:
        raise ValueError("--rehearse is a dry-run option; it cannot be combined with --commit")
    creds = resolve()
    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=banner_actions,
    ) as page:
        preflight = preflight_batch(page, sheet, report_id,
                                    accept_attendee_removal=accept_attendee_removal)
        if accept_attendee_removal:
            print("\n  ACCEPTING Concur's attendee-removal prompt on type changes "
                  "(--accept-attendee-removal; part of the plan token)")
        if rehearse:
            rehearse_type_changes(page, preflight)
        print(format_preflight(preflight))
        if not preflight.clean:
            return 2
        confirmed = report_id in CONFIRMED_REPORT_IDS
        if confirmed:
            token = batch_token(preflight)
            print(f"\n  PLAN TOKEN: {token}  (real report {report_id}; to write "
                  f"exactly this plan: --commit --confirm {token})")
        if not commit:
            if rehearse:
                print(f"\nDry run with rehearsal — {len(preflight.rehearsals)} type "
                      "change(s) switched in-form and abandoned; each reopen matched "
                      "its original values. No Save clicked.")
            else:
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
    accept_attendee_removal: bool = False,
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
                    banner_actions=banner_actions,
                    accept_attendee_removal=accept_attendee_removal)
    # The grant refuses any other real report first, so its boundary message
    # wins over the --confirm misuse below.
    with test_report_write_grant(report_id):
        if confirm:
            raise ValueError("--confirm applies only to a confirmed real report")
        return _run(sheet, report_id, commit=True, banner_actions=banner_actions,
                    accept_attendee_removal=accept_attendee_removal)


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
