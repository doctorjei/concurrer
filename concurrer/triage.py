"""Triage a report: what is broken, per expense, without opening everything.

The report list already carries per-row error/warning/alert counts, and the
expense edit page exposes which fields are required-and-empty plus Concur's own
wording in `[data-test="validation-status"]`. Combining them gives a triage
report that says what to fix, in the order it blocks submission.

Two passes:

  1. LIST pass — read every row's identity and its error/warning/alert counts.
     Cheap: one page, no navigation per expense.
  2. DETAIL pass — open only the rows that showed errors, and read which fields
     are required-and-empty. Skips clean rows entirely.

Read-only. Opens edit forms but never saves; backs out by navigation.

Usage:
    python -m concurrer.triage --report <ID> [--detail] [--max N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from .login import authenticated_context
from .nav import expense_row_number, open_expense_edit, open_report
from .safety import click_safe
from .secrets import resolve
from .widgets import field_inventory

OUT = "/home/agent/.config/concur/discover"

# Per-row: identity from the checkbox label, counts from the row's buttons.
#
# Verified row structure (2026-09-21, live tenant):
#   div.sapcnqr-data-grid-list__row          <- the row; 1 checkbox, ~5 buttons
#     div.sapcnqr-data-grid-list__cell ...
#       label.sapcnqr-checkbox               <- innerText = "Select expense,
#         input.sapcnqr-checkbox__input          Ground Transportation, $6.75,
#                                              date, 08/12/2026"
#
# The identity string lives in the LABEL's innerText, not in an aria-label —
# the checkboxes have no aria-label at all. The header's select-all lives in a
# separate row and is excluded by iterating __row elements only.
ROWS_JS = """() => {
    const rows = [];
    for (const row of document.querySelectorAll(
            '.sapcnqr-data-grid-list__row')) {
        const lab = row.querySelector('label.sapcnqr-checkbox');
        const identity = lab ? (lab.innerText || '').trim() : '';
        if (identity.toLowerCase().startsWith('select all')) continue;
        const btnTexts = Array.from(row.querySelectorAll('button'))
            .map(e => (e.innerText || '').trim()).filter(Boolean);
        const counts = {};
        for (const t of btnTexts) {
            const m = t.match(/(Errors?|Warnings?|Alerts?)\\s*:\\s*(\\d+)/i);
            if (m) counts[m[1].toLowerCase()] = parseInt(m[2], 10);
        }
        if (!identity && !btnTexts.length) continue;
        const key = row.getAttribute('data-row-key') || row.id || '';
        rows.push({
            identity: identity,
            expense_id: /^[0-9A-Fa-f]+$/.test(key) ? key : '',
            counts: counts,
            buttons: btnTexts.slice(0, 8),
        });
    }
    return rows;
}"""


_HEX_ID = __import__('re').compile(r'[0-9A-Fa-f]+\Z')


def row_expense_id(row: dict) -> str:
    """Return a row's stable expense ID, or an empty string when it is absent."""
    key = str(row.get('expense_id') or '').strip()
    return key if _HEX_ID.fullmatch(key) else ''


def triage_rows(page) -> list[dict]:
    rows = page.evaluate(ROWS_JS)
    for r in rows:
        errs = r["counts"].get("errors", r["counts"].get("error", 0))
        warns = r["counts"].get("warnings", r["counts"].get("warning", 0))
        alerts = r["counts"].get("alerts", 0)
        r["n_errors"] = errs
        r["n_warnings"] = warns
        r["n_alerts"] = alerts
        r["needs_fix"] = errs > 0
    return rows


def broken_in(page) -> dict:
    """Which fields on the currently-open expense are required and empty."""
    fields = field_inventory(page)
    return {
        "n_fields": len(fields),
        "broken": [
            {"label": f["label"], "name": f["name"]}
            for f in fields
            if f["required"] and not f["value"] and not f["locked"]
        ],
        "locked_but_required_empty": [
            {"label": f["label"], "name": f["name"]}
            for f in fields
            if f["required"] and not f["value"] and f["locked"]
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Triage an expense report.")
    ap.add_argument("--report", required=True)
    ap.add_argument("--detail", action="store_true",
                    help="open error rows and read which fields are broken")
    ap.add_argument("--max", type=int, default=10, help="max rows to open")
    args = ap.parse_args()

    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        open_report(page, args.report)
        print(f"report: {page.title()}  url: {page.url}")

        rows = triage_rows(page)
        print(f"\n{len(rows)} expense rows")
        total_e = sum(r["n_errors"] for r in rows)
        total_w = sum(r["n_warnings"] for r in rows)
        total_a = sum(r["n_alerts"] for r in rows)
        print(f"totals: {total_e} errors, {total_w} warnings, {total_a} alerts")

        for i, r in enumerate(rows, 1):
            flag = "FIX " if r["needs_fix"] else "    "
            print(f" {flag}{i:>3}. {r['identity'][:56]:<56} "
                  f"E={r['n_errors']} W={r['n_warnings']} A={r['n_alerts']}")

        payload: dict = {"report": args.report, "rows": rows,
                        "totals": {"errors": total_e, "warnings": total_w,
                                  "alerts": total_a}}

        if args.detail:
            # The list page does NOT reliably expose per-row error counts
            # (verified 2026-09-21: 24 rows, 141 buttons, zero "Errors:" text
            # anywhere in the body). Earlier dumps that showed them were taken
            # from the expense edit page with the exception panel rendered.
            # So when the list gives no counts, every row must be opened.
            candidates = (
                [(expense_row_number(i), r) for i, r in enumerate(rows) if r["needs_fix"]]
                if any(r["needs_fix"] for r in rows)
                else [(expense_row_number(i), r) for i, r in enumerate(rows)]
            )
            print(f"\nopening {min(len(candidates), args.max)} rows for detail...")
            for n, (row, r) in enumerate(candidates[:args.max], 1):
                try:
                    eid = open_expense_edit(page, args.report, row)
                except Exception as exc:
                    print(f"  {n}. row {row}: could not open ({exc})")
                    continue
                info = broken_in(page)
                r["expense_id"] = eid
                r.update(info)
                mark = "BROKEN" if info["broken"] else "ok     "
                print(f"  {mark} {r['identity'][:44]:<44} "
                      f"missing={[b['label'] for b in info['broken']][:4]}")
                if info["locked_but_required_empty"]:
                    print(f"         locked+required+empty="
                          f"{info['locked_but_required_empty']}")
                open_report(page, args.report)

        path = f"{OUT}/triage_{args.report[:12]}.json"
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
