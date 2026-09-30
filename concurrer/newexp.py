"""Create an expense in the throwaway Test Report — the first real write path.

Operator authorization (2026-09-21): *"you can add expenses to the test report."*
That is scoped here by `authorize_writes(TEST_REPORT_ID)`, which the guard
checks against the live URL on every click. Writing anywhere else raises. Submit
and delete remain unreachable no matter what.

The create flow is a modal on the report page:

    /nui/expense/reports/<ID>?modal=add-expense&view=create_new_expense

It is two-stage: a searchable picker of ~80 UF expense types, each carrying its
account-code routing rules, and only after choosing a type does the detail form
appear. That second stage is where the money fields live, and for a manually
created (non-card) expense they should be EDITABLE — which is the open question
about why P-card money fields are locked.

Stages, so this can be run and inspected incrementally:

    --list-types   open the picker, dump every type, stop
    --type NAME    choose a type, dump the detail form, stop
    --save         choose a type, fill required fields, Save Expense, verify

Usage:
    python -m concurrer.newexp --list-types
    python -m concurrer.newexp --type "Ground Transportation"
    python -m concurrer.newexp --type "Ground Transportation" --save
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .login import authenticated_context
from .nav import open_report
from .safety import TEST_REPORT_ID, authorize_writes, click_safe
from .secrets import resolve
from .widgets import field_inventory, fill_combobox

OUT = "/home/agent/.config/concur/discover"

# Test values for the money fields, used only on the throwaway report.
TEST_FILL = {
    "transactionDate": time.strftime("%m/%d/%Y"),
    "vendorName": "Concurrer Test Vendor",
    "transactionAmount": "1.23",
    "comment": "Created by concurrer.newexp — throwaway test expense.",
}


PICKER_JS = """() => {
    const vis = (el) => { const r = el.getBoundingClientRect();
        return r.width > 5 && r.height > 5; };
    const items = [];
    for (const el of document.querySelectorAll(
            'button, [role=button], [role=option], [role=menuitem], li')) {
        if (!vis(el)) continue;
        const t = (el.innerText || '').trim();
        if (!t || t.length < 3) continue;
        items.push({
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            text: t.slice(0, 200),
            nuiexp: el.getAttribute('data-nuiexp') || '',
        });
    }
    return items.slice(0, 300);
}"""


def open_create_modal(page) -> None:
    """Open the manual create picker.

    The entry depends on report state (verified 2026-09-21):
      - empty report: `Manually Create Expense` is on the page directly
      - populated report: `Add Expense` opens a menu of
        `Create Expense from Receipt` / `Manually Create Expense` /
        `Select from Available Expenses (N)`

    So: open `Add Expense` if it exists, then take `Manually Create Expense`.
    """
    add = page.get_by_role("button", name="Add Expense")
    if add.count():
        click_safe(page, add.first, "Add Expense")
        page.wait_for_timeout(3000)

    # In the menu this is role="menuitem", not role="button", so the accessible
    # name lookup used above does not find it. Its stable handle is the
    # data-nuiexp; fall back to the button role for the empty-report layout
    # where it sits on the page directly.
    manual = page.locator('[data-nuiexp="create-new-expense-menu-item"]')
    if manual.count() == 0:
        manual = page.get_by_role("button", name="Manually Create Expense")
    if manual.count() == 0:
        raise RuntimeError(
            "no 'Manually Create Expense' available on this report")
    click_safe(page, manual.first, "Manually Create Expense")
    page.wait_for_timeout(6000)


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a test expense.")
    ap.add_argument("--report", default=TEST_REPORT_ID)
    ap.add_argument("--list-types", action="store_true")
    ap.add_argument("--type", default=None)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    if not (args.list_types or args.type):
        print("nothing to do: pass --list-types or --type NAME", file=sys.stderr)
        return 2

    # The grant is what makes Save reachable at all, and only for this report.
    authorize_writes(args.report)

    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        open_report(page, args.report)
        print(f"report: {page.title()}  url: {page.url}")

        open_create_modal(page)
        print(f"create modal open: {page.url}")

        items = page.evaluate(PICKER_JS)
        payload: dict = {"report": args.report, "url": page.url, "picker": items}

        if args.list_types:
            print(f"\n{len(items)} picker elements:")
            for it in items:
                print(f"  [{it['role']:<10}] {it['text'][:110]!r}")
            path = f"{OUT}/create_types.json"
            with open(path, "w") as fh:
                json.dump(payload, fh, indent=2)
            print(f"\nwrote {path}")
            return 0

        # Choose the expense type from the picker.
        #
        # The picker is two-level: a category row and its leaf rows can both
        # contain the search term ("Postage/Freight & Courier" vs "Postage"),
        # and clicking the category only expands it. Prefer the element whose
        # FIRST LINE equals the requested type.
        print(f"\nchoosing type {args.type!r}...")
        picker = page.get_by_text(args.type, exact=False)
        n = picker.count()
        print(f"  matches: {n}")
        if n == 0:
            print(f"type {args.type!r} not found in picker", file=sys.stderr)
            return 3
        target = picker.last
        for i in range(n):
            first_line = picker.nth(i).inner_text().strip().split("\n")[0].strip()
            if first_line.lower() == args.type.strip().lower():
                target = picker.nth(i)
                print(f"  exact leaf match at index {i}")
                break
        click_safe(page, target, f"type:{args.type}")
        page.wait_for_timeout(8000)
        print(f"  now at {page.url}")

        fields = field_inventory(page)
        payload["chosen_type"] = args.type
        payload["form_url"] = page.url
        payload["fields"] = fields
        print(f"\n{len(fields)} fields on the create form:")
        for f in fields:
            flags = []
            if f["required"]:
                flags.append("REQUIRED")
            if f["locked"]:
                flags.append("LOCKED")
            print(f"  {f['label'][:34]:<34} name={f['name'][:22]:<22} "
                  f"[{f['widget']:<14}] {' '.join(flags)}")

        if not args.save:
            path = f"{OUT}/create_form_{args.type[:18].replace(' ', '_')}.json"
            with open(path, "w") as fh:
                json.dump(payload, fh, indent=2)
            print(f"\ndry run — wrote {path}, not saving")
            return 0

        # Fill required-and-empty fields.
        print("\nfilling required fields...")
        filled: dict[str, str] = {}
        gaps: list[str] = []
        for f in fields:
            if not f["required"]:
                continue
            label = f["label"]
            if f["locked"] or f["value"]:
                print(f"  {label[:34]:<34} already set: {f['value'][:24]!r}")
                continue
            if f["widget"] == "combobox":
                got = fill_combobox(page, f["name"], label)
                if got:
                    filled[label] = got
                else:
                    gaps.append(label)
                continue
            val = TEST_FILL.get(f["name"])
            if val is None and label == "Transaction Date":
                val = time.strftime("%m/%d/%Y")
            if val is None:
                print(f"  {label[:34]:<34} no test value (name={f['name']!r})")
                gaps.append(label)
                continue
            loc = page.get_by_role("textbox", name=label)
            if loc.count() == 0:
                loc = page.locator(
                    f"input[name='{f['name']}'], textarea[name='{f['name']}']")
            if loc.count() == 0:
                print(f"  {label[:34]:<34} no locator matched")
                gaps.append(label)
                continue
            loc.first.fill(val)
            filled[label] = val
            print(f"  {label[:34]:<34} := {val!r}")

        # Comment is optional but useful provenance on a test expense.
        cmt = page.get_by_role("textbox", name="Comment")
        if cmt.count() == 0:
            cmt = page.locator("textarea[name='comment']")
        if cmt.count():
            cmt.first.fill(TEST_FILL["comment"])

        if gaps:
            print(f"\nUNFILLED required fields: {gaps}")
            print("not saving — Concur would reject it anyway.")
            return 4

        page.wait_for_timeout(2000)

        print("\nsaving...")
        save = page.get_by_role("button", name="Save Expense")
        print(f"  Save buttons: {save.count()}")
        click_safe(page, save.first, "Save Expense")
        page.wait_for_timeout(8000)
        print(f"  after save: {page.url}")

        open_report(page, args.report, settle_ms=8000)
        after = page.evaluate("""() => document.body.innerText.slice(0, 3000)""")
        payload["after_save_text"] = after
        path = f"{OUT}/created_{args.type[:18].replace(' ', '_')}.json"
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nreport text after save:\n{after[:1500]}")
        print(f"\nwrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
