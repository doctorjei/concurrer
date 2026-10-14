"""Plan or safely persist one expense edit.

Plan mode is read-only and works against any report the operator may read. A
persisting invocation requires ``--save`` and is structurally limited to the
throwaway Test Report; no CLI input can widen that boundary.

Examples::

    python -m concurrer.edit --report <REPORT> --expense-id <EXPENSE> \
        --set comment="Receipt attached"

    python -m concurrer.edit --report B857BA45DBC24EDB8420 \
        --expense-id <TEST-EXPENSE> --set comment="Test marker" --save

    python -m concurrer.edit --report B857BA45DBC24EDB8420 \
        --expense-id <TEST-EXPENSE> --type "Ground Transportation" \
        --expect-type "Books & Publications" --allow-type-change \
        --set custom20="Taxi" --set custom29="No" --save

Changing expense type is intentionally high-friction: Concur silently discards
its type-specific custom values, and switching back does not restore them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Mapping

from .banners import parse_banner_actions
from .login import authenticated_context
from .nav import open_expense_by_id, open_expense_edit, open_report
from .safety import TEST_REPORT_ID, click_safe, test_report_write_grant
from .secrets import resolve
from .type_form_map import TYPE_FORM_FIELDS
from .type_map import (
    conditional_errors,
    compatible_custom_field,
    known_type,
    schema_for,
    type_change_losses,
)
from .widgets import (
    field_by_name,
    field_inventory,
    fill_text_exact,
    select_combobox_exact,
    select_payment_type_exact,
    select_search_combobox_exact,
    settled_inventory,
)

_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


class PlanError(ValueError):
    """A request cannot be safely planned or persisted."""


@dataclass(frozen=True)
class EditRequest:
    report_id: str
    expense_id: str | None
    row: int | None
    assignments: Mapping[str, str]
    target_type: str | None = None
    expected_type: str | None = None
    allow_type_change: bool = False
    save: bool = False


@dataclass
class EditPlan:
    request: EditRequest
    current_type: str
    target_type: str
    changes: dict[str, dict[str, str]]
    losses: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Standard fields with a value that the target type's blank (manual) form
    #: lacks. Unverified: card-fed forms can differ, so these are reported for
    #: review, not refused.
    possible_losses: list[dict] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def parse_assignment(raw: str) -> tuple[str, str]:
    """Parse a stable field-name assignment without accepting field clearing."""
    name, separator, value = raw.partition("=")
    if not separator or not _FIELD_NAME.fullmatch(name):
        raise PlanError(f"invalid assignment {raw!r}; use stable FIELD=VALUE")
    if name == "expenseType":
        raise PlanError("use --type for expense-type changes, never --set expenseType=...")
    if not value.strip():
        raise PlanError(f"refusing to clear {name!r}; clearing is not supported")
    return name, value


def parse_assignments(raw_assignments: list[str]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for raw in raw_assignments:
        name, value = parse_assignment(raw)
        if name in assignments:
            raise PlanError(f"duplicate assignment for {name!r}")
        assignments[name] = value
    if not assignments:
        raise PlanError("pass at least one --set FIELD=VALUE or --type NAME")
    return assignments


def _type_from(fields: list[dict]) -> str:
    field = field_by_name(fields, "expenseType")
    if not field["value"].strip():
        raise PlanError("live expense has no readable Expense Type")
    return field["value"]


def _field_names_for_target(target_type: str, fields: list[dict]) -> set[str]:
    # Non-custom fields keep their live identity. Type-specific custom fields
    # may appear only after the target's cascade, so use the versioned schema.
    names = {field["name"] for field in fields if field["name"] and not field["name"].startswith("custom")}
    names.update(field.name for field in schema_for(target_type))
    return names


def build_edit_plan(request: EditRequest, fields: list[dict]) -> EditPlan:
    """Validate a request against a fresh live inventory without changing it."""
    current_type = _type_from(fields)
    target_type = request.target_type or current_type
    type_changing = target_type != current_type

    if request.target_type:
        if not request.expected_type:
            raise PlanError("--type requires --expect-type CURRENT_TYPE")
        if not request.allow_type_change:
            raise PlanError("--type requires --allow-type-change")
        if request.expected_type != current_type:
            raise PlanError(
                f"expected type {request.expected_type!r}, live type is {current_type!r}"
            )
        if not known_type(target_type):
            raise PlanError("target type must exist in the 67-type map")
        # Concur's "Undefined" placeholder is not a selectable type, so it is
        # not in the map. A source outside the map is safe only when its live
        # form has no custom fields: then nothing type-specific can be lost
        # or needs restoring.
        if not known_type(current_type) and any(
                field["name"].startswith("custom") for field in fields):
            raise PlanError(
                f"source type {current_type!r} is not in the 67-type map and "
                "carries custom fields; refusing an unmapped loss"
            )
    elif request.expected_type or request.allow_type_change:
        raise PlanError("--expect-type and --allow-type-change require --type")

    # A no-type-change plan is authoritative from the fresh live form. It must
    # keep working when Concur adds/renames a type before our reference map is
    # refreshed; the map is required only for schema-sensitive type changes.
    allowed_names = (
        _field_names_for_target(target_type, fields)
        if request.target_type
        else {field["name"] for field in fields if field["name"]}
    )
    by_name = {field["name"]: field for field in fields if field["name"]}
    changes: dict[str, dict[str, str]] = {}
    for name, value in request.assignments.items():
        # A standard field may exist only on the target type (Request appears
        # on Ground Transportation but not on Undefined). Customs are known
        # exhaustively from the schema; a standard field is accepted here and
        # must appear on the form after the type switch, before any Save.
        deferred = type_changing and not name.startswith("custom") and name not in by_name
        if name not in allowed_names and not deferred:
            raise PlanError(f"field {name!r} is absent from the live/target form")
        current = by_name.get(name)
        if current and current["locked"]:
            raise PlanError(f"field {name!r} ({current['label']}) is locked")
        if name in UNWRITABLE_FIELDS:
            raise PlanError(f"field {name!r}: {UNWRITABLE_FIELDS[name]}")
        changes[name] = {
            "label": current["label"] if current else name,
            "before": current["value"] if current else "<appears after type change>",
            "after": value,
        }

    losses = (type_change_losses(current_type, fields, target_type)
              if type_changing and known_type(current_type) else [])
    warnings: list[str] = []
    if type_changing:
        warnings.append(
            "Changing Expense Type discards its type-specific values; switching back "
            "does not recover them."
        )
        if losses:
            warnings.append(f"{len(losses)} nonblank custom value(s) cannot safely carry over.")
    possible = (_possible_standard_losses(fields, target_type, request.assignments)
                if type_changing else [])
    return EditPlan(request, current_type, target_type, changes, losses, warnings,
                    possible)


#: Standard fields whose presence follows currency, not expense type.
_CURRENCY_FIELDS = frozenset({"exchangeRate", "postedAmount"})


def _possible_standard_losses(fields: list[dict], target_type: str,
                              assignments) -> list[dict]:
    target = {f.name for f in TYPE_FORM_FIELDS.get(target_type, ())}
    return [
        {"name": f["name"], "label": f["label"], "value": f["value"]}
        for f in fields
        if f["name"] and not f["name"].startswith("custom")
        and f["name"] not in target and f["name"] not in _CURRENCY_FIELDS
        and f["name"] not in assignments
        and str(f.get("value", "")).strip()
    ]


def _live_custom_schema(fields: list[dict]) -> set[tuple[str, str, bool]]:
    return {
        (field["name"], _norm(field["label"]), bool(field["required"]))
        for field in fields
        if field["name"].startswith("custom")
    }


def _expected_custom_schema(expense_type: str) -> set[tuple[str, str, bool]]:
    return {
        (field.name, _norm(field.label), field.required)
        for field in schema_for(expense_type)
    }


def wait_for_type_cascade(page, target_type: str, *, interval_ms: int = 1000,
                          max_reads: int = 20) -> list[dict]:
    """Fields once the form has actually re-rendered for ``target_type``.

    After a type selection Concur can keep showing the old type's fields for
    a while (CCED GT -> Other Services rehearsal, 2026-10-14: the snapshot
    still had GT's custom20/custom29/Request). "The field set stopped
    changing" is not proof; the target's known custom schema appearing is.
    """
    expected = _expected_custom_schema(target_type)
    for _ in range(max_reads):
        fields = field_inventory(page)
        try:
            if _type_from(fields) == target_type and _live_custom_schema(fields) == expected:
                return settled_inventory(page)
        except (PlanError, ValueError):
            pass  # expenseType briefly absent mid re-render
        page.wait_for_timeout(interval_ms)
    raise PlanError(
        f"the form did not re-render for {target_type!r} within "
        f"{max_reads * interval_ms // 1000}s; nothing was saved")


def validate_target_schema(fields: list[dict], expense_type: str) -> None:
    """Require the fresh form's custom schema to match the captured type map."""
    actual = _live_custom_schema(fields)
    expected = _expected_custom_schema(expense_type)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise PlanError(
            f"live custom schema disagrees with map for {expense_type!r}; "
            f"missing={missing!r} unexpected={unexpected!r}"
        )


#: Fields with no exact write helper yet. Refused at plan time so a dry run
#: reports them, instead of a commit failing mid-row at fill time. (Request
#: left this list once its container-triggered picker was mapped.)
UNWRITABLE_FIELDS: dict[str, str] = {}


def validate_before_save(fields: list[dict], plan: EditPlan) -> None:
    """Fail closed on incomplete, invalid, or unverified live form state."""
    if _type_from(fields) != plan.target_type:
        raise PlanError(
            f"Expense Type is {_type_from(fields)!r}, expected {plan.target_type!r}"
        )
    validate_target_schema(fields, plan.target_type)

    values = {field["name"]: field["value"] for field in fields if field["name"]}
    problems = [
        f"required field {field['label']!r} is empty"
        for field in fields
        if field["required"] and not str(field["value"]).strip()
    ]
    problems.extend(
        f"field {field['label']!r} has Concur validation: {field['validation']!r}"
        for field in fields
        if str(field.get("validation", "")).strip()
    )
    problems.extend(conditional_errors(plan.target_type, values))

    for name, change in plan.changes.items():
        actual = values.get(name)
        if actual is None:
            problems.append(f"requested field {name!r} disappeared from the live form")
        elif _norm(actual) != _norm(change["after"]):
            problems.append(
                f"requested {name!r} is {actual!r}, expected {change['after']!r}"
            )
    if problems:
        raise PlanError("save blocked: " + "; ".join(problems))


def _apply_value(page, field: dict, value: str) -> None:
    # Location and Payment Type reads can be transient (empty before Concur
    # finishes populating them), so they are always set through their strict
    # helpers rather than skipped on apparent equality.
    if field["name"] == "location":
        select_search_combobox_exact(page, field, value)
    elif field["name"] == "paymentType":
        select_payment_type_exact(page, field, value)
    elif field["widget"] == "combobox":
        select_combobox_exact(page, field, value)
    else:
        fill_text_exact(page, field, value)


def _restore_compatible_customs(page, source_type: str, target_type: str, before: list[dict]) -> None:
    """Restore only same-slot, same-label values erased by a type cascade."""
    if not known_type(source_type):
        return  # planning proved such a source had no custom fields
    after = field_inventory(page)
    for source in before:
        if not source["name"].startswith("custom") or not source["value"].strip():
            continue
        if not compatible_custom_field(source_type, source["name"], target_type):
            continue
        target = field_by_name(after, source["name"])
        if _norm(target["label"]) != _norm(source["label"]):
            raise PlanError(
                f"refusing to restore {source['name']!r}: semantic label changed "
                f"from {source['label']!r} to {target['label']!r}"
            )
        if not target["value"].strip():
            _apply_value(page, target, source["value"])
            after = field_inventory(page)


def apply_plan(page, plan: EditPlan, before: list[dict]) -> list[dict]:
    """Apply a previously validated Test Report plan, then return fresh state."""
    if plan.current_type != plan.target_type:
        type_field = field_by_name(before, "expenseType")
        select_combobox_exact(page, type_field, plan.target_type)
        after_type = wait_for_type_cascade(page, plan.target_type)
        if _type_from(after_type) != plan.target_type:
            raise PlanError(f"Expense Type did not change to {plan.target_type!r}")
        validate_target_schema(after_type, plan.target_type)
        _restore_compatible_customs(page, plan.current_type, plan.target_type, before)

    current = field_inventory(page)  # any type cascade was already awaited above
    for name, change in plan.changes.items():
        if not any(field["name"] == name for field in current):
            raise PlanError(
                f"field {name!r} is not on the {plan.target_type!r} form after the "
                "type change; nothing was saved"
            )
        _apply_value(page, field_by_name(current, name), change["after"])
        current = field_inventory(page)
    return current


def verify_saved_expense(page, report_id: str, expense_id: str, plan: EditPlan) -> None:
    """Reopen the direct expense route and prove the requested values persisted."""
    open_expense_by_id(page, report_id, expense_id)
    fields = field_inventory(page)
    validate_before_save(fields, plan)


def persist_and_verify_plan(
    page,
    report_id: str,
    expense_id: str,
    plan: EditPlan,
    before: list[dict],
    *,
    on_save_attempt: Callable[[], None] | None = None,
) -> None:
    """Apply one validated plan, safely save it, and prove persistence.

    This is the **only** single-expense mutation sequence. ``pushcsv`` uses it
    rather than reimplementing a batch-local Save button, so strict writes,
    safety-guarded clicking, and direct-ID readback cannot diverge.

    ``on_save_attempt`` runs immediately before the guarded click. It lets a
    batch caller report the honest outcome of a later exception: after that
    point Save *may* have happened, so the current row must be inspected rather
    than described as definitely unsaved.
    """
    fields = apply_plan(page, plan, before)
    validate_before_save(fields, plan)

    save = page.locator('[data-nuiexp="exp-save-expense"]')
    if save.count() != 1 or not save.first.is_visible():
        raise RuntimeError("expected exactly one visible primary Save Expense button")
    if on_save_attempt is not None:
        on_save_attempt()
    click_safe(page, save, "Save Expense")
    page.wait_for_timeout(7000)
    verify_saved_expense(page, report_id, expense_id, plan)


def _plan_payload(plan: EditPlan) -> dict:
    return {
        "current_type": plan.current_type,
        "target_type": plan.target_type,
        "changes": plan.changes,
        "losses": plan.losses,
        "warnings": plan.warnings,
    }


def print_plan(plan: EditPlan, *, json_output: bool) -> None:
    payload = _plan_payload(plan)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("\nEDIT PLAN" + (" (will save to Test Report)" if plan.request.save else " (read-only)"))
    print(f"  Expense Type: {plan.current_type!r} -> {plan.target_type!r}")
    for name, change in plan.changes.items():
        print(f"  {name} ({change['label']}): {change['before']!r} -> {change['after']!r}")
    for loss in plan.losses:
        print(f"  LOSS: {loss['name']} ({loss['label']}) currently {loss['value']!r}")
    for warning in plan.warnings:
        print(f"  WARNING: {warning}")


def _run(
    request: EditRequest,
    *,
    json_output: bool,
    banner_actions: Mapping[str, str] | None = None,
) -> int:
    creds = resolve()
    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=banner_actions,
    ) as page:
        if request.expense_id:
            expense_id = open_expense_by_id(page, request.report_id, request.expense_id)
        else:
            assert request.row is not None
            expense_id = open_expense_edit(page, request.report_id, request.row)
        before = field_inventory(page)
        plan = build_edit_plan(request, before)
        print(f"report={request.report_id} expense={expense_id} url={page.url}")
        print_plan(plan, json_output=json_output)

        if not request.save:
            open_report(page, request.report_id, settle_ms=1000)
            return 0

        changed_form = False
        try:
            changed_form = True
            persist_and_verify_plan(page, request.report_id, expense_id, plan, before)
            print("saved and verified")
            return 0
        finally:
            # The edit form has no Cancel. Navigation abandons unsaved changes;
            # do not attempt to reverse a type cascade, which cannot restore data.
            if changed_form:
                open_report(page, request.report_id, settle_ms=1000)


def build_request(args: argparse.Namespace) -> EditRequest:
    if bool(args.expense_id) == bool(args.row is not None):
        raise PlanError("pass exactly one of --expense-id or --row")
    if args.row is not None and args.row < 1:
        raise PlanError("--row must be at least 1; row 0 is select-all")
    assignments = parse_assignments(args.assignments) if args.assignments else {}
    if not assignments and not args.expense_type:
        raise PlanError("pass at least one --set FIELD=VALUE or --type NAME")
    if args.save and not args.expense_id:
        raise PlanError("--save requires a stable --expense-id, not a row")
    return EditRequest(
        report_id=args.report,
        expense_id=args.expense_id,
        row=args.row,
        assignments=assignments,
        target_type=args.expense_type,
        expected_type=args.expect_type,
        allow_type_change=args.allow_type_change,
        save=args.save,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Plan or safely edit one Concur expense.")
    ap.add_argument("--report", required=True)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--expense-id")
    target.add_argument("--row", type=int)
    ap.add_argument("--set", dest="assignments", action="append", default=[], metavar="FIELD=VALUE")
    ap.add_argument("--type", dest="expense_type")
    ap.add_argument("--expect-type")
    ap.add_argument("--allow-type-change", action="store_true")
    ap.add_argument("--save", action="store_true", help="persist only to the Test Report")
    ap.add_argument(
        "--banner-action",
        action="append",
        default=[],
        metavar="BANNER_ID=EXACT_LABEL",
        help="one explicit response to a displayed sign-in banner; repeatable",
    )
    ap.add_argument("--json", action="store_true", help="emit the edit plan as JSON")
    args = ap.parse_args()

    try:
        request = build_request(args)
        try:
            banner_actions = parse_banner_actions(args.banner_action)
        except ValueError as exc:
            raise PlanError(str(exc)) from exc
        if request.save:
            # Deliberately before credential resolution or browser launch: a real
            # report never reaches a form-mutation path.
            with test_report_write_grant(request.report_id):
                return _run(
                    request,
                    json_output=args.json,
                    banner_actions=banner_actions,
                )
        return _run(request, json_output=args.json, banner_actions=banner_actions)
    except (PlanError, AssertionError) as exc:
        print(f"edit plan error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports safe failure
        print(f"edit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
