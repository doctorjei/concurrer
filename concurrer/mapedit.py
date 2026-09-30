"""Map the Concur expense EDIT form — read-only, cancel-only.

Opens `Edit` on one expense row and records the field structure, then dismisses
via Cancel. **There is no code path to Save in this module.**

Safety posture, deliberately paranoid because this runs against a real report with
real dollar figures:

  1. Before clicking Edit, confirm a Cancel/Close affordance exists. If none is
     found, abort WITHOUT clicking Edit — a form we cannot back out of is a form
     we do not open.
  2. `Save` is never clicked. The button is recorded so we know where it is, and
     never activated.
  3. Exactly one expense row is opened, not all of them.
  4. Nothing is typed into any field. This reads; it does not write.

Usage:
    python -m concurrer.mapedit --report <ID> [--row N]
"""

from __future__ import annotations

import argparse
import json
import sys

from .login import authenticated_context
from .safety import click_safe
from .secrets import resolve

OUT = "/home/agent/.config/concur/discover"

FIELD_JS = """() => Array.from(document.querySelectorAll('input,select,textarea'))
    .filter(el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 3 && r.height > 3
            && s.visibility !== 'hidden' && s.display !== 'none'
            && el.type !== 'hidden';
    })
    .map(el => {
        const lbl = (el.labels && el.labels[0])
            ? el.labels[0].innerText.trim()
            : (el.getAttribute('aria-label') || '');
        return {
            tag: el.tagName.toLowerCase(),
            type: el.type || '',
            id: el.id || '',
            name: el.name || '',
            placeholder: el.placeholder || '',
            label: lbl.slice(0, 70),
            required: !!(el.required || el.getAttribute('aria-required') === 'true'),
            disabled: !!el.disabled,
            readonly: !!el.readOnly,
            value: (el.value || '').slice(0, 70),
        };
    })"""

BUTTON_JS = """() => Array.from(document.querySelectorAll('button'))
    .filter(el => { const r = el.getBoundingClientRect();
        return r.width > 3 && r.height > 3; })
    .map(el => (el.innerText || '').trim()).filter(Boolean)"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Map the expense EDIT form, read-only.")
    ap.add_argument("--report", required=True)
    ap.add_argument("--row", type=int, default=1, help="one-based expense row to open (default first)")
    args = ap.parse_args()

    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        page.goto(
            f"https://us2.concursolutions.com/nui/expense/reports/{args.report}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(8000)
        print(f"report: {page.title()}  url: {page.url}")

        # `Edit` is a TOOLBAR action, not a per-row link: it renders disabled
        # until a row checkbox is ticked. Discovered the hard way — the first
        # attempt timed out on `element is not enabled`. Selecting a row is a
        # UI selection, not a data mutation, so this is still read-only.
        boxes = page.locator(".sapcnqr-checkbox__input")
        n_boxes = boxes.count()
        print(f"row checkboxes: {n_boxes}")
        if n_boxes <= 1:
            print("No selectable expense rows on this report.", file=sys.stderr)
            return 1
        if args.row < 1 or args.row >= n_boxes:
            print(
                f"row must be 1 through {n_boxes - 1}; row 0 is select-all",
                file=sys.stderr,
            )
            return 2
        print(f"selecting row {args.row} (checkbox tick only)...")
        click_safe(page, boxes.nth(args.row), "row checkbox")
        page.wait_for_timeout(2500)

        edits = page.locator("button", has_text="Edit")
        count = edits.count()
        print(f"'Edit' buttons visible: {count}")
        if count == 0:
            print("No Edit button after selecting.", file=sys.stderr)
            return 1

        target = edits.first
        if not target.is_enabled():
            print(
                "Edit still disabled after ticking a row — the checkbox may need a "
                "different target, or this report's rows are not editable. Aborting.",
                file=sys.stderr,
            )
            return 2
        print("Edit is enabled. Opening (read-only, will Cancel)...")
        click_safe(page, target, "Edit")
        page.wait_for_timeout(7000)

        fields = page.evaluate(FIELD_JS)
        buttons_after = page.evaluate(BUTTON_JS)

        payload = {
            "report": args.report,
            "row": args.row,
            "form_url": page.url,
            "fields": fields,
            "buttons": buttons_after,
        }
        path = f"{OUT}/edit_form.json"
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"wrote {path}")

        print(f"\n{len(fields)} fields on the edit form:")
        for f in fields:
            req = "REQUIRED" if f["required"] else "optional"
            lock = " LOCKED" if f["readonly"] or f["disabled"] else ""
            print(
                f"  {f['label'][:32]:<32} [{f['tag']}/{f['type']:<9}] "
                f"{req}{lock} val={f['value'][:28]!r}"
            )

        print(f"\nbuttons: {buttons_after}")

        cancel_after = [b for b in buttons_after if b.lower() in ("cancel", "close")]
        if cancel_after:
            print(f"clicking {cancel_after[0]!r} — NOT Save")
            click_safe(
                page,
                page.get_by_role("button", name=cancel_after[0]).first,
                cancel_after[0],
            )
            page.wait_for_timeout(2500)
            print(f"dismissed; now at {page.url}")
        else:
            print(
                "WARNING: no Cancel appeared after opening. Leaving as-is; closing "
                "the browser without saving.",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
