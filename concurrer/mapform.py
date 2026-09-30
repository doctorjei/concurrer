"""Map the Concur expense form field structure — read-only.

Opens the expense form and records every field: label, control type, whether it
is required, its current value, and the selector needed to target it. Then
**cancels without saving**.

Why open the *create* form to learn the *edit* form: Concur renders both from the
same component, so the field set, requiredness, and control types are shared.
Opening create and cancelling touches no data, whereas opening edit requires a
report that already has a real expense row in it. This gets the same map without
that.

`Save` is never clicked. There is no code path here that reaches it. If the form
cannot be dismissed without saving, this script aborts rather than persisting.

Usage:
    python -m concurrer.mapform            # create-form on the throwaway report
    python -m concurrer.mapform --report <ID>
"""

from __future__ import annotations

import argparse
import json
import sys

# (sync_playwright is opened inside authenticated_context)

from .login import authenticated_context
from .secrets import resolve

#: The report we are allowed to poke at. Default is the operator-designated
#: throwaway ("Test Report"), not a real one.
THROWAWAY_REPORT = "B857BA45DBC24EDB8420"

#: Never clicked, under any circumstance, by any code path in this module.
FORBIDDEN = ("save", "submit", "delete", "approve", "create expense report")

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
            label: lbl.slice(0, 80),
            required: !!(el.required || el.getAttribute('aria-required') === 'true'),
            disabled: !!el.disabled,
            readonly: !!el.readOnly,
            value: (el.value || '').slice(0, 60),
            options: el.tagName === 'SELECT'
                ? Array.from(el.options).slice(0, 12).map(o => o.text.trim()).filter(Boolean)
                : undefined,
        };
    })"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Map the Concur expense form, read-only.")
    ap.add_argument("--report", default=THROWAWAY_REPORT)
    args = ap.parse_args()

    creds = resolve()
    print(creds.report(), file=sys.stderr)

    # authenticated_context opens its own sync_playwright(); nesting a second one
    # here raises "Sync API inside the asyncio loop".
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
        page.wait_for_timeout(7000)
        print(f"report: {page.title()}")

        opener = page.get_by_text("Manually Create Expense", exact=False).first
        try:
            opener.wait_for(state="visible", timeout=15_000)
        except Exception:  # noqa: BLE001
            print("'Manually Create Expense' not offered on this report.", file=sys.stderr)
            return 1

        print("opening 'Manually Create Expense' (nothing saves)...")
        opener.click(timeout=15_000)
        page.wait_for_timeout(6000)

        fields = page.evaluate(FIELD_JS)
        buttons = [
            b.strip()
            for b in page.evaluate(
                """() => Array.from(document.querySelectorAll('button'))
                    .filter(el => { const r = el.getBoundingClientRect();
                        return r.width > 3 && r.height > 3; })
                    .map(el => (el.innerText || '').trim())"""
            )
            if b
        ]

        payload = {
            "report": args.report,
            "form_url": page.url,
            "fields": fields,
            "buttons": buttons,
        }
        path = f"{OUT}/expense_form.json"
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"wrote {path}")

        print(f"\n{len(fields)} fields:")
        for f in fields:
            req = "REQUIRED" if f["required"] else "optional"
            ro = " readonly" if f["readonly"] or f["disabled"] else ""
            print(
                f"  {f['label'][:34]:<34} [{f['tag']}/{f['type']:<9}] "
                f"{req}{ro} id={f['id'][:22]!r}"
            )
            if f.get("options"):
                print(f"      options: {f['options'][:8]}")

        print(f"\nbuttons on the open form: {buttons}")

        # Guard: confirm we are not about to leave via a Save. Cancel only.
        cancel = [b for b in buttons if b.lower() in ("cancel", "close", "back")]
        print(f"\ncancel affordances available: {cancel or 'NONE'}")
        if not cancel:
            print(
                "No Cancel/Close found. NOT clicking anything further — leaving "
                "this form open rather than risk a save-on-close behaviour.",
                file=sys.stderr,
            )
        else:
            print(f"clicking {cancel[0]!r} to dismiss without saving")
            page.get_by_role("button", name=cancel[0]).first.click(timeout=10_000)
            page.wait_for_timeout(2000)
            print(f"dismissed; now at {page.url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
