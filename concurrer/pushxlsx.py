"""Review or safely apply a modified Concur expense **workbook**.

The xlsx counterpart of :mod:`concurrer.pushcsv`. Same batch driver, same safety
model, same one-expense editor underneath; only the *reader* differs. The
workbook's operator-facing columns are routed to Concur fields by
:func:`concurrer.xlsxio.push_sheet`, and the result is handed to the identical
preflight/re-plan/commit pipeline the CSV path uses.

Default mode is a fresh-live-state dry run::

    python -m concurrer.pushxlsx --xlsx CCED-Summer-2026.xlsx

``--commit`` is structurally limited to the throwaway Test Report and the one
confirmed real report (which also needs ``--confirm <plan token>``), exactly as
for CSV. The report ID is read from the workbook's Report Details sheet — never
from a command-line option — and the grant is taken *before* credentials are
resolved or a browser is opened, so a real-report workbook fails locally::

    python -m concurrer.pushxlsx --xlsx test-report.xlsx --commit

Batching is not transactional. All rows are preflighted before the first
mutation, each row is re-diffed immediately before its Save, and a failure
stops the batch while naming the rows that did save and verify. There is never
an attempted rollback.

What this path deliberately does **not** do:

* **Bulk ``expenseType`` changes.** ``csvio.BULK_REFUSED_FIELDS`` refuses them
  and points at ``concurrer.edit``. A type change irreversibly drops the
  type-specific custom values, and the per-row routing that authorised the rest
  of a row is itself keyed on that type, so it cannot share a batch with
  ordinary field writes. Design: ``plans/xlsx-writeback.md``.
* **Travel Allowance / per diem.** Those rows live on the read-only Meals sheet,
  which the reader never opens, so they are invisible here by construction.
* **Submit, or anything else in the permanent deny tier.** Untouched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .banners import parse_banner_actions
from .csvio import single_report_id
from .edit import PlanError
from .pushcsv import BatchPlanError, _run, run_commit
from .xlsxio import RoutingError, push_sheet, workbook_issues


def load(path: Path):
    """Read a workbook into ``(report_id, csvio.Sheet)``.

    Identity is a property of the file: the report ID comes from Report Details
    and the expense ID from each row, so no option can aim a write at a report
    the workbook never named.
    """
    if not path.is_file():
        raise RoutingError(f"workbook does not exist or is not a regular file: {path}")
    issues = workbook_issues(path)
    if issues:
        rendered = "\n  ".join(issue.describe() for issue in issues[:40])
        suffix = "\n  … additional issues omitted" if len(issues) > 40 else ""
        raise RoutingError(
            f"workbook has {len(issues)} red/pink cell issue(s); refusing the "
            f"whole batch before login or write:\n  {rendered}{suffix}"
        )
    report_id, sheet = push_sheet(path)
    # Reuse the CSV path's canonicalisation and validation so both readers
    # agree on what a usable report ID is (uppercase, hexadecimal, one value).
    return single_report_id(sheet), sheet


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Review or safely apply a Concur expense workbook."
    )
    ap.add_argument("--xlsx", required=True, type=Path)
    ap.add_argument(
        "--commit",
        action="store_true",
        help="persist only when the workbook identifies the Test Report or the "
        "confirmed real report",
    )
    ap.add_argument(
        "--rehearse",
        action="store_true",
        help="dry run only: switch each type-changing row's type in the form, "
        "record what the new type keeps and drops, abandon without Save, and "
        "verify the reopened expense is unchanged",
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
        report_id, sheet = load(args.xlsx)
        banner_actions = parse_banner_actions(args.banner_action)
        if args.rehearse and args.commit:
            raise ValueError("--rehearse is a dry-run option; it cannot be combined with --commit")
        if args.confirm and not args.commit:
            raise ValueError("--confirm is only meaningful with --commit")
        if args.commit:
            # The structural boundary must precede everything that can touch
            # credentials or a browser. The report ID was read from the file
            # itself, so no CLI argument can widen it.
            return run_commit(sheet, report_id, confirm=args.confirm,
                              banner_actions=banner_actions)
        return _run(sheet, report_id, commit=False, banner_actions=banner_actions,
                    rehearse=args.rehearse)
    except (BatchPlanError, PlanError, RoutingError, ValueError,
            AssertionError) as exc:
        print(f"pushxlsx plan error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports safe failure
        print(f"pushxlsx failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
