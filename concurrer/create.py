"""Create new expenses (with receipts) from a reviewed plan file.

The plan is a JSON file; identity is a property of the file, never a CLI
flag::

    {"report_id": "506EC055FE79452BB943",
     "expenses": [
       {"key": "taxi-0712",
        "type": "Ground Transportation",
        "values": {"transactionDate": "07/12/2026",
                   "transactionCurrencyName": "Japan, Yen (JPY)",
                   "transactionAmount": "1900",
                   "vendorName": "Nihon Taxi",
                   "paymentType": "Personal Funds",
                   "custom13": "03. International", ...},
        "receipt": "photos/upload/20260712-Nihon-Taxi-1900JPY.jpg"}]}

Default mode is a local dry run: it validates every expense against the
67-type form map (type known, every required field supplied, no field the
type lacks) and every receipt file, then prints the plan and its token. On
the confirmed real report ``--commit --confirm <token>`` creates exactly
that plan; the Test Report needs no token::

    python -m concurrer.create --plan new.json
    python -m concurrer.create --plan new.json --commit --confirm <token>

Per expense, in order: open the report, Manually Create Expense, choose the
type, wait for its form, fill each value with the same exact-select helpers
the editor uses, require every value to read back and every required field
to be filled, click Save once, find the new expense as the one ID that
appeared on the report, reopen it by ID and verify every value, then attach
its receipt (permanent; never retried). A failure stops the batch and names
what was created. There is no rollback: delete stays in the NEVER tier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .banners import parse_banner_actions
from .edit import _apply_value
from .login import authenticated_context
from .nav import open_expense_by_id, open_report
from .newexp import open_create_modal
from .receipts import ALLOWED_SUFFIXES, ReceiptRequest, attach_receipt
from .safety import (
    CONFIRMED_REPORT_IDS,
    TEST_REPORT_ID,
    click_safe,
    plan_token,
    test_report_write_grant,
    write_grant,
)
from .secrets import resolve
from .triage import row_expense_id, triage_rows
from .type_form_map import TYPE_FORM_FIELDS
from .widgets import blocking_dialog_text, field_inventory, settled_inventory

#: Fill order: currency before amount (Concur re-renders money fields on a
#: currency change) and before location (a foreign location prompts to change
#: currency when they disagree).
FILL_ORDER = (
    "transactionDate", "transactionCurrencyName", "transactionAmount",
    "location", "vendorName", "vendor", "paymentType",
    "custom13", "custom12", "businessPurpose",
)
#: Present on the real report's form though absent from the Test Report sweep
#: (it has no linked request); verified on the form before Save.
REPORT_DEPENDENT = frozenset({"travelRequestExpenseId"})
#: Filled or computed by Concur, never by a plan.
NEVER_PLANNED = frozenset({"expenseType", "exchangeRate", "postedAmount"})
_DATE = re.compile(r"\d{2}/\d{2}/\d{4}")


class CreatePlanError(ValueError):
    """A create plan cannot be safely carried out."""


@dataclass(frozen=True)
class NewExpense:
    key: str
    expense_type: str
    values: Mapping[str, str]
    receipt: Path | None


def load_plan(path: Path) -> tuple[str, list[NewExpense]]:
    raw = json.loads(Path(path).read_text())
    report_id = str(raw.get("report_id") or "").strip().upper()
    if not re.fullmatch(r"[0-9A-F]+", report_id):
        raise CreatePlanError("plan has no valid report_id")
    expenses = []
    for item in raw.get("expenses") or []:
        receipt = item.get("receipt")
        expenses.append(NewExpense(
            key=str(item.get("key") or "").strip(),
            expense_type=str(item.get("type") or "").strip(),
            values={str(k): str(v) for k, v in (item.get("values") or {}).items()},
            receipt=(Path(path).parent / receipt) if receipt else None,
        ))
    if not expenses:
        raise CreatePlanError("plan has no expenses")
    return report_id, expenses


def validate_plan(expenses: list[NewExpense]) -> list[str]:
    """Every problem knowable without a browser; empty means plannable."""
    problems: list[str] = []
    keys = [e.key for e in expenses]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        problems.append("every expense needs a unique non-empty key")
    for e in expenses:
        form = {f.name: f for f in TYPE_FORM_FIELDS.get(e.expense_type, ())}
        if not form:
            problems.append(f"{e.key}: type {e.expense_type!r} is not in the 67-type map")
            continue
        for name, value in e.values.items():
            if name in NEVER_PLANNED:
                problems.append(f"{e.key}: {name} is never set by a plan")
            elif name not in form and name not in REPORT_DEPENDENT:
                problems.append(f"{e.key}: {e.expense_type!r} has no field {name!r}")
            elif not value.strip():
                problems.append(f"{e.key}: {name} is blank (omit it instead)")
        for f in form.values():
            if f.required and not f.locked and f.name != "expenseType" \
                    and not e.values.get(f.name, "").strip():
                problems.append(f"{e.key}: required {f.label!r} ({f.name}) is missing")
        date = e.values.get("transactionDate", "")
        if date and not _DATE.fullmatch(date):
            problems.append(f"{e.key}: transactionDate must be MM/DD/YYYY")
        amount = e.values.get("transactionAmount", "").replace(",", "")
        if amount and not re.fullmatch(r"\d+(\.\d{1,2})?", amount):
            problems.append(f"{e.key}: transactionAmount must be a plain number")
        if e.receipt is not None:
            if not e.receipt.is_file() or e.receipt.stat().st_size == 0:
                problems.append(f"{e.key}: receipt file missing or empty")
            elif e.receipt.suffix.casefold() not in ALLOWED_SUFFIXES:
                problems.append(f"{e.key}: receipt suffix {e.receipt.suffix!r} unsupported")
    return problems


def create_token(report_id: str, expenses: list[NewExpense]) -> str:
    """Fingerprint of every value and receipt byte the plan would create."""
    parts = []
    for e in expenses:
        parts.append((e.key, "expenseType", "", e.expense_type))
        parts.extend((e.key, name, "", value) for name, value in e.values.items())
        if e.receipt is not None:
            parts.append((e.key, "receipt", hashlib.sha256(e.receipt.read_bytes()).hexdigest(), ""))
    return plan_token(report_id, [("create",) + p for p in parts])


def format_plan(report_id: str, expenses: list[NewExpense]) -> str:
    lines = [f"CREATE PLAN — {len(expenses)} new expense(s) on report {report_id}"]
    for e in expenses:
        lines.append(f"\n  [{e.key}] {e.expense_type}")
        for name, value in e.values.items():
            lines.append(f"    {name}: {value!r}")
        if e.receipt is not None:
            lines.append(f"    receipt: <{e.receipt.suffix.casefold()} "
                         f"{e.receipt.stat().st_size} bytes> (permanent)")
    return "\n".join(lines)


def _report_ids(page, report_id: str) -> set[str]:
    open_report(page, report_id)
    return {row_expense_id(r).upper() for r in triage_rows(page) if row_expense_id(r)}


def _choose_type(page, expense_type: str) -> None:
    """Pick the leaf whose first line is exactly the type (never a category)."""
    # Search only inside the open create dialog: the report's expense list
    # behind it shows the same type names (CCED, 2026-10-14: a hidden list
    # cell "Ground Transportation" was matched and its click timed out).
    dialogs = page.locator("[role=dialog]")
    scope = dialogs.last if dialogs.count() else page
    picker = scope.get_by_text(expense_type, exact=False)
    matches = []
    for i in range(picker.count()):
        candidate = picker.nth(i)
        if not candidate.is_visible():
            continue
        first = candidate.inner_text().strip().split("\n")[0].strip()
        if first.casefold() == expense_type.casefold():
            matches.append(candidate)
    if len(matches) != 1:
        raise CreatePlanError(
            f"type {expense_type!r}: expected one visible exact entry in the "
            f"create picker, found {len(matches)}")
    click_safe(page, matches[0], f"type:{expense_type}")


def _values(fields: list[dict]) -> dict[str, str]:
    return {f["name"]: f["value"] for f in fields if f.get("name")}


def _norm(text: str) -> str:
    return " ".join((text or "").replace(",", "").split()).casefold()


def _check_form(fields: list[dict], e: NewExpense) -> list[str]:
    values = _values(fields)
    problems = []
    for name, wanted in e.values.items():
        got = values.get(name)
        if got is None:
            problems.append(f"{name} is not on the form")
        elif _norm(got) != _norm(wanted):
            problems.append(f"{name} is {got!r}, expected {wanted!r}")
    problems += [f"required {f['label']!r} is empty" for f in fields
                 if f.get("required") and not str(f.get("value", "")).strip()]
    problems += [f"{f['label']!r}: {f['validation']!r}" for f in fields
                 if str(f.get("validation", "")).strip()]
    return problems


def create_one(page, report_id: str, e: NewExpense, before_ids: set[str],
               *, on_save_attempt=lambda: None) -> str:
    """Create, save, identify, verify, and receipt one expense. Returns its ID."""
    open_report(page, report_id)
    open_create_modal(page)
    _choose_type(page, e.expense_type)
    fields = settled_inventory(page)
    if _values(fields).get("expenseType") != e.expense_type:
        raise CreatePlanError(f"{e.key}: create form did not open as {e.expense_type!r}")
    ordered = [n for n in FILL_ORDER if n in e.values] + \
              [n for n in e.values if n not in FILL_ORDER]
    for name in ordered:
        current = [f for f in field_inventory(page) if f.get("name") == name]
        if len(current) != 1:
            raise CreatePlanError(f"{e.key}: field {name!r} is not uniquely on the form")
        _apply_value(page, current[0], e.values[name])
        dialog = blocking_dialog_text(page)
        if dialog:
            raise CreatePlanError(f"{e.key}: unexpected dialog after {name}: {dialog!r}")
    problems = _check_form(settled_inventory(page), e)
    if problems:
        raise CreatePlanError(f"{e.key}: not saved — " + "; ".join(problems))
    save = page.locator('[data-nuiexp="exp-save-expense"]')
    if save.count() != 1:
        save = page.get_by_role("button", name="Save Expense")
    if save.count() != 1:
        raise CreatePlanError(f"{e.key}: expected exactly one Save Expense button")
    on_save_attempt()
    click_safe(page, save.first, "Save Expense")
    page.wait_for_timeout(7000)
    new_ids = _report_ids(page, report_id) - before_ids
    if len(new_ids) != 1:
        raise CreatePlanError(
            f"{e.key}: Save clicked but {len(new_ids)} new expense IDs appeared "
            f"({sorted(new_ids)}); inspect the report before retrying")
    expense_id = new_ids.pop()
    open_expense_by_id(page, report_id, expense_id)
    problems = _check_form(field_inventory(page), e)
    if problems:
        raise CreatePlanError(
            f"{e.key}: created {expense_id} but its reopened values differ: "
            + "; ".join(problems))
    if e.receipt is not None:
        result = attach_receipt(page, ReceiptRequest(report_id, expense_id, e.receipt))
        if result["outcome"] != "verified-persisted":
            raise CreatePlanError(
                f"{e.key}: created {expense_id}; receipt {result['outcome']} — "
                "inspect it manually, never retry the attachment blindly")
    return expense_id


def commit_plan(page, report_id: str, expenses: list[NewExpense]) -> int:
    created: list[tuple[str, str]] = []
    for e in expenses:
        saved = False

        def mark() -> None:
            nonlocal saved
            saved = True

        try:
            before_ids = _report_ids(page, report_id)
            expense_id = create_one(page, report_id, e, before_ids, on_save_attempt=mark)
        except Exception as exc:  # noqa: BLE001 - report the precise state
            print(f"\nCREATE STOPPED at [{e.key}]: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            if created:
                print("Created and verified before this (they remain): "
                      + ", ".join(f"[{k}] {i}" for k, i in created), file=sys.stderr)
            print(f"[{e.key}] " + ("Save WAS clicked — it may exist; inspect the "
                                    "report before any retry." if saved else
                                    "failed before Save; nothing was created for it."),
                  file=sys.stderr)
            return 1
        created.append((e.key, expense_id))
        print(f"created and verified: [{e.key}] {expense_id}"
              + (" (+ receipt)" if e.receipt is not None else ""))
    print(f"\nCREATE COMMITTED AND VERIFIED — {len(created)} expense(s)")
    return 0


def _run(report_id: str, expenses: list[NewExpense], *, banner_actions) -> int:
    creds = resolve()
    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=banner_actions,
    ) as page:
        return commit_plan(page, report_id, expenses)


def main() -> int:
    ap = argparse.ArgumentParser(description="Create expenses from a reviewed plan file.")
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--commit", action="store_true",
                    help="create on the Test Report, or on the confirmed report with --confirm")
    ap.add_argument("--confirm", metavar="PLAN_TOKEN",
                    help="with --commit on the confirmed report: the dry run's PLAN TOKEN")
    ap.add_argument("--banner-action", action="append", default=[],
                    metavar="BANNER_ID=EXACT_LABEL")
    args = ap.parse_args()
    try:
        report_id, expenses = load_plan(args.plan)
        problems = validate_plan(expenses)
        if problems:
            raise CreatePlanError("plan refused before login:\n  " + "\n  ".join(problems))
        token = create_token(report_id, expenses)
        if not args.commit:
            if args.confirm:
                raise CreatePlanError("--confirm is only meaningful with --commit")
            print(format_plan(report_id, expenses))
            print(f"\n  PLAN TOKEN: {token}  (to create exactly this: --commit --confirm {token})")
            print("\nDry run only — no credentials resolved, no browser opened.")
            return 0
        banner_actions = parse_banner_actions(args.banner_action)
        if report_id in CONFIRMED_REPORT_IDS:
            with write_grant(report_id, reviewed_token=args.confirm, live_token=token):
                return _run(report_id, expenses, banner_actions=banner_actions)
        if report_id != TEST_REPORT_ID:
            raise CreatePlanError(f"refusing to create on report {report_id!r}")
        with test_report_write_grant(report_id):
            return _run(report_id, expenses, banner_actions=banner_actions)
    except (CreatePlanError, ValueError, AssertionError) as exc:
        print(f"create plan error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"create failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
