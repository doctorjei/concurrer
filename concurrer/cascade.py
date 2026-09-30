"""Does changing `expenseType` re-render the dependent custom fields?

The create form for Ground Transportation asked for custom20 ("Type of Ground
Transportation") and custom29 ("Were other travelers with you?"). A different
expense type should ask for something else — that mapping is institutional
policy and concurrer has to know it before it can validate an edit locally
rather than discovering the miss at save time.

Run against the Test Report only. The write grant is checked against the live URL
on every click, so pointing this at a real report fails closed.

Usage:
    python -m concurrer.cascade [--report TEST] [--row 1] [--to "Train/Rail"]
"""

from __future__ import annotations

import argparse
import json
import sys

from .login import authenticated_context
from .nav import open_expense_edit
from .safety import TEST_REPORT_ID, authorize_writes, click_safe
from .secrets import resolve
from .widgets import fill_combobox, field_inventory, option_index

OUT = "/home/agent/.config/concur/discover"


def main() -> int:
    ap = argparse.ArgumentParser(description="Test expenseType cascade.")
    ap.add_argument("--report", default=TEST_REPORT_ID)
    ap.add_argument("--row", type=int, default=1)
    ap.add_argument("--to", default=None,
                    help="target expense type; default = first option that differs")
    ap.add_argument("--save", action="store_true",
                    help="save the change (test report only)")
    args = ap.parse_args()

    authorize_writes(args.report)
    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        expense_id = open_expense_edit(page, args.report, args.row)
        print(f"expense: {expense_id}")

        before = field_inventory(page)
        print(f"\nBEFORE — {len(before)} fields:")
        for f in before:
            print(f"  {f['label'][:36]:<36} name={f['name'][:20]:<20} "
                  f"req={str(f['required']):<5} val={f['value'][:22]!r}")

        cur = next((f for f in before if f["name"] == "expenseType"), None)
        if not cur:
            print("no expenseType field on this expense", file=sys.stderr)
            return 3
        print(f"\ncurrent expenseType: {cur['value']!r}")

        # Read the available types without committing anything yet.
        trig = page.locator('[data-nuiexp="field-expenseType__trigger"]')
        click_safe(page, trig.first, "open options for Expense Type")
        page.wait_for_timeout(3500)
        idx = option_index(page)
        types = list(idx.keys())
        print(f"available expense types: {len(types)} -> {types[:12]}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(1500)

        target = args.to
        if not target:
            others = [t for t in types if t.lower() != cur["value"].lower()]
            if not others:
                print("no alternative type available", file=sys.stderr)
                return 4
            target = others[0]
        print(f"\nchanging expenseType -> {target!r}")
        got = fill_combobox(page, "expenseType", "Expense Type", prefer=target)

        after = field_inventory(page)
        print(f"\nAFTER — {len(after)} fields:")
        for f in after:
            print(f"  {f['label'][:36]:<36} name={f['name'][:20]:<20} "
                  f"req={str(f['required']):<5} val={f['value'][:22]!r}")

        b = {f["label"] for f in before}
        a = {f["label"] for f in after}
        added = sorted(a - b)
        removed = sorted(b - a)
        print(f"\nCASCADE RESULT:")
        print(f"  added fields:   {added}")
        print(f"  removed fields: {removed}")
        if not added and not removed:
            print("  no field-set change — custom fields did NOT re-render")

        payload = {
            "report": args.report, "row": args.row, "expense_id": expense_id,
            "from_type": cur["value"], "to_type": got or target,
            "available_types": types,
            "before": before, "after": after,
            "added": added, "removed": removed,
        }
        path = f"{OUT}/cascade_{expense_id[:12]}.json"
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {path}")

        if args.save:
            print("\nsaving the change...")
            save = page.get_by_role("button", name="Save Expense")
            click_safe(page, save.first, "Save Expense")
            page.wait_for_timeout(8000)
            print(f"  after save: {page.url}")
            payload["saved"] = True
            with open(path, "w") as fh:
                json.dump(payload, fh, indent=2)
        else:
            print("\ndry run — not saving")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
