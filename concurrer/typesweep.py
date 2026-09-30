"""Map every expense type to the custom fields it demands.

The cascade mechanism is proven: changing `expenseType` live-renders a different
set of `customNN` fields. This builds the actual table — which of the 67 UF
expense types asks for which fields, and which of those are required.

Method: open ONE create form, then cycle the `expenseType` combobox through every
available option, dumping the field inventory after each change. One page load
instead of 67, and it exercises the cascade directly rather than the picker, so
the result also answers whether the cascade fires on the create form at all.

No writes. Nothing is saved; the form is abandoned at the end.

Also answers, as a final step: are a type's custom values recoverable by
re-selecting the original type, or gone?

Usage:
    python -m concurrer.typesweep [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys

from .login import authenticated_context
from .nav import open_report
from .safety import TEST_REPORT_ID, authorize_writes, click_safe
from .secrets import resolve
from .newexp import open_create_modal
from .widgets import field_inventory, option_index

OUT = "/home/agent/.config/concur/discover"
OPTS = '[role=dialog] [role=option], [role=listbox] [role=option]'


def pick_from_picker(page, name: str) -> bool:
    """Enter the create form by choosing `name` from the two-level picker.

    The create modal opens on the picker; the `expenseType` combobox only exists
    once a type has been chosen there. Prefer the element whose FIRST LINE equals
    `name` — category rows (e.g. "Postage/Freight & Courier") also match a
    search for "Postage" and clicking one only expands it.
    """
    picker = page.get_by_text(name, exact=False)
    n = picker.count()
    if n == 0:
        return False
    target = picker.last
    for i in range(n):
        first_line = picker.nth(i).inner_text().strip().split("\n")[0].strip()
        if first_line.lower() == name.strip().lower():
            target = picker.nth(i)
            break
    click_safe(page, target, f"type:{name}")
    page.wait_for_timeout(8000)
    return True


def select_type(page, name: str) -> bool:
    """Set expenseType to `name` via its combobox. True if it took."""
    trig = page.locator('[data-nuiexp="field-expenseType__trigger"]')
    if trig.count() == 0:
        return False
    click_safe(page, trig.first, "open options for Expense Type")
    page.wait_for_timeout(3000)
    idx = option_index(page)
    if name not in idx:
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
        return False
    click_safe(page, page.locator(OPTS).nth(idx[name]), name)
    page.wait_for_timeout(3000)
    return True


def customs(fields: list[dict]) -> list[dict]:
    """Just the customNN fields, with their required flag."""
    return [
        {"name": f["name"], "label": f["label"], "required": f["required"]}
        for f in fields if f["name"].startswith("custom")
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep expense types -> custom fields.")
    ap.add_argument("--limit", type=int, default=0, help="cap types sampled (0 = all)")
    args = ap.parse_args()

    # Grant is needed only if something goes wrong and a save is reachable; the
    # sweep itself never saves. Scoped to the throwaway report regardless.
    authorize_writes(TEST_REPORT_ID)
    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        open_report(page, TEST_REPORT_ID)
        open_create_modal(page)
        print(f"create modal: {page.url}")

        # Get to a form with a type selected so expenseType exists.
        if not pick_from_picker(page, "Ground Transportation"):
            print("could not select an initial type from the picker",
                  file=sys.stderr)
            return 3
        print(f"in create form: {page.url}")

        trig = page.locator('[data-nuiexp="field-expenseType__trigger"]')
        click_safe(page, trig.first, "open options for Expense Type")
        page.wait_for_timeout(3000)
        all_types = list(option_index(page).keys())
        page.keyboard.press("Escape")
        page.wait_for_timeout(1500)
        if args.limit:
            all_types = all_types[:args.limit]
        print(f"sweeping {len(all_types)} types\n")

        mapping: dict[str, list[dict]] = {}
        failed: list[str] = []

        for i, t in enumerate(all_types, 1):
            if not select_type(page, t):
                print(f"{i:>3}. {t[:44]:<44} SELECT FAILED")
                failed.append(t)
                continue
            got = page.evaluate(
                """() => {
                    const labs = document.querySelectorAll(
                        '.sapcnqr-form-field__label');
                    const el = Array.from(labs).find(
                        e => (e.innerText||'').trim() === 'Expense Type');
                    if (!el) return '';
                    const c = el.parentElement.parentElement;
                    const vs = c.querySelector(
                        '.sapcnqr-select-field__input, .sapcnqr-form-field__input');
                    return vs ? (vs.innerText||'').trim() : '';
                }"""
            )
            if got != t:
                print(f"{i:>3}. {t[:44]:<44} DID NOT TAKE (got {got[:30]!r})")
                failed.append(t)
                continue
            fields = field_inventory(page)
            cs = customs(fields)
            mapping[t] = cs
            req = [c for c in cs if c["required"]]
            desc = ", ".join(
                f"{c['label']}{'*' if c['required'] else ''}" for c in cs) or "—"
            print(f"{i:>3}. {t[:34]:<34} {len(cs):>2} custom  {desc[:78]}")

        path = f"{OUT}/typesweep.json"
        with open(path, "w") as fh:
            json.dump({"types": mapping, "failed": failed,
                      "count": len(mapping)}, fh, indent=2)
        print(f"\nmapped {len(mapping)} types, {len(failed)} failed")
        print(f"wrote {path}")

        # Recoverability: set a type with customs, change away, come back.
        print("\n--- recoverability test ---")
        a = "Ground Transportation"
        if a in mapping and mapping[a]:
            select_type(page, a)
            before = {c["name"]: c["label"] for c in customs(field_inventory(page))}
            # give custom20 a value so we can see if it survives
            from .widgets import fill_combobox
            fill_combobox(page, "custom20", "Type of Ground Transportation",
                         prefer="Taxi")
            v_before = page.evaluate(
                """() => {
                    const c = document.querySelector(
                        '[data-nuiexp="field-custom20__trigger"]');
                    if (!c) return '';
                    const row = c.closest('.sapcnqr-form-field__control-container')
                        || c.parentElement;
                    const vs = row.querySelector(
                        '.sapcnqr-select-field__input, .sapcnqr-form-field__input');
                    return vs ? (vs.innerText||'').trim() : '';
                }"""
            )
            print(f"  {a} custom20 set to {v_before!r}")
            select_type(page, "Books & Publications")
            mid = [c["name"] for c in customs(field_inventory(page))]
            print(f"  after switching away: customs = {mid}")
            select_type(page, a)
            back = [c["name"] for c in customs(field_inventory(page))]
            v_back = page.evaluate(
                """() => {
                    const c = document.querySelector(
                        '[data-nuiexp="field-custom20__trigger"]');
                    if (!c) return '';
                    const row = c.closest('.sapcnqr-form-field__control-container')
                        || c.parentElement;
                    const vs = row.querySelector(
                        '.sapcnqr-select-field__input, .sapcnqr-form-field__input');
                    return vs ? (vs.innerText||'').trim() : '';
                }"""
            )
            print(f"  after switching back:  customs = {back}")
            print(f"  custom20 value now {v_back!r} "
                  f"({'RECOVERED' if v_back == v_before and v_before else 'LOST'})")

        print("\nsweep complete — form abandoned, nothing saved")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
