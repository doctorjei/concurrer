"""Read the editable field inventory of one Concur expense.

This is the read path the edit feature is built on. It answers, per field: what is
it called, what is its stable name, what is its current value, is it required, is
it locked, and what does Concur's per-field validation say.

Why a dedicated extractor was needed: Concur's Details fields are **not native
form controls**. A `querySelectorAll('input,select,textarea')` sweep — what the
first mapping passes used — returns only the locked money fields and the row
checkboxes, and reports the classification fields as absent. They are actually
custom comboboxes:

    <div class="sapcnqr-select-field" id=":r20b:-select-field">
      <div class="sapcnqr-form-field__heading">
        <span class="sapcnqr-form-field__label">Expense Type</span>
        <span class="sapcnqr-form-field__required">*</span>
      </div>
      <div class="sapcnqr-form-field__control-container">
        <div role="combobox" aria-haspopup="dialog" aria-required="true"
             aria-labelledby=":r206:-label" class="sapcnqr-form-field__control">
          <span role="presentation" name="expenseType"
                class="sapcnqr-select-field__input">Ground Transportation</span>
          <button data-nuiexp="field-expenseType__trigger"
                  aria-label="Show options for Expense Type">
          <button data-nuiexp="field-expenseType__clear" aria-label="Clear">
        </div>
        <div data-test="validation-status" class="...live-region">
      </div>
    </div>

Selector policy, learned here and worth keeping:

  STABLE   `data-nuiexp="field-<name>__trigger"`  semantic, survives deploys
           `span[name="<name>"]`                  the field's real name
           `aria-labelledby` -> label text        human-readable join
           `.sapcnqr-form-field__label` / `__required` / `__control`
           `[data-test="validation-status"]`
  UNSTABLE `:r20b:` / `:r206:` React ids — never key off these
           `sapcnqr-*-<hex>` hashed classes — churn per deploy

Read-only: this module enumerates and may open an options dialog to read the
allowed value set. It never types, never clears, never saves.

Usage:
    python -m concurrer.fields --report ID [--row N]
    python -m concurrer.fields --report ID --row 1 --open expenseType
"""

from __future__ import annotations

import argparse
import json
import sys

from .login import authenticated_context
from .nav import open_expense_edit, open_report
from .safety import click_safe
from .secrets import resolve

OUT = "/home/agent/.config/concur/discover"
REPORT = "506EC055FE79452BB943"  # CCED Summer 2026 — read-only mapping target

FIELDS_JS = """() => {
    const out = [];
    const seen = new Set();
    for (const hd of document.querySelectorAll('.sapcnqr-form-field__heading')) {
        const labEl = hd.querySelector('.sapcnqr-form-field__label');
        if (!labEl) continue;
        const label = (labEl.innerText || '').trim();
        if (!label) continue;
        const container = hd.parentElement;
        if (!container || seen.has(container)) continue;
        seen.add(container);

        const combo = container.querySelector('[role=combobox]');
        const input = container.querySelector(
            'input:not([type=hidden]), textarea');
        const ctrl = combo || input;
        if (!ctrl) continue;

        const nui = Array.from(container.querySelectorAll('[data-nuiexp]'))
            .map(e => e.getAttribute('data-nuiexp'));

        let name = (ctrl.querySelector('[name]')
                    && ctrl.querySelector('[name]').getAttribute('name'))
                || (input && input.getAttribute('name')) || '';
        if (!name) {
            for (const t of nui) {
                const m = t.match(/^field-([A-Za-z0-9_]+)__/);
                if (m) { name = m[1]; break; }
            }
        }

        let value = '';
        if (input) {
            value = input.value || '';
        } else {
            const vs = ctrl.querySelector(
                '.sapcnqr-select-field__input, .sapcnqr-form-field__input');
            value = vs ? (vs.innerText || '').trim()
                       : (ctrl.innerText || '').trim();
        }

        const locked = input
            ? !!(input.readOnly || input.disabled)
            : (combo ? combo.getAttribute('aria-disabled') === 'true' : false);

        const vnode = container.querySelector(
            '[data-test="validation-status"]');

        out.push({
            label: label,
            name: name,
            value: value.slice(0, 140),
            required: !!hd.querySelector('.sapcnqr-form-field__required')
                || (combo ? combo.getAttribute('aria-required') === 'true' : false),
            locked: locked,
            widget: combo ? 'combobox'
                : (input.tagName.toLowerCase() + '/' + (input.type || '')),
            hasPopup: combo ? (combo.getAttribute('aria-haspopup') || '') : '',
            labelId: labEl.id || '',
            nuiexp: nui,
            validation: vnode ? (vnode.innerText || '').trim().slice(0, 160) : '',
        });
    }
    return out;
}"""

# What an opened options dialog contains. Broad on purpose: Concur renders these
# as poppers with listbox/option roles, but a plain list is possible too.
OPTIONS_JS = """() => {
    const vis = (el) => { const r = el.getBoundingClientRect();
        return r.width > 5 && r.height > 5; };
    const dialogs = Array.from(document.querySelectorAll(
        '[role=dialog], [role=listbox], .sapcnqr-popper, .sapcnqr-popup'))
        .filter(vis);
    const opts = [];
    for (const d of dialogs) {
        for (const o of d.querySelectorAll(
                '[role=option], [role=listitem], li, .sapcnqr-checkbox')) {
            if (!vis(o)) continue;
            const t = (o.innerText || '').trim();
            if (t) opts.push({
                role: o.getAttribute('role') || o.tagName.toLowerCase(),
                text: t.slice(0, 120),
                selected: o.getAttribute('aria-selected') || '',
                checked: !!(o.querySelector && o.querySelector(
                    'input:checked, .sapcnqr-checkbox--checked')),
            });
        }
    }
    return {
        dialogs: dialogs.map(d => ({
            role: d.getAttribute('role') || d.tagName.toLowerCase(),
            label: d.getAttribute('aria-label') || '',
            text: (d.innerText || '').trim().slice(0, 600),
        })),
        options: opts.slice(0, 200),
    };
}"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Read an expense's field inventory.")
    ap.add_argument("--report", default=REPORT)
    ap.add_argument("--row", type=int, default=1)
    ap.add_argument("--open", dest="open_field", default=None,
                    help="field name whose options dialog to open and read")
    args = ap.parse_args()

    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        expense_id = open_expense_edit(page, args.report, args.row)
        print(f"expense: {expense_id}  url: {page.url}")

        fields = page.evaluate(FIELDS_JS)
        payload = {
            "report": args.report,
            "row": args.row,
            "expense_id": expense_id,
            "url": page.url,
            "fields": fields,
        }

        print(f"\n{len(fields)} fields:")
        for f in fields:
            flags = []
            if f["required"]:
                flags.append("REQUIRED")
            if f["locked"]:
                flags.append("LOCKED")
            if f["hasPopup"]:
                flags.append(f"popup={f['hasPopup']}")
            print(
                f"  {f['label'][:34]:<34} name={f['name'][:20]:<20} "
                f"[{f['widget']:<14}] {' '.join(flags):<22} "
                f"val={f['value'][:32]!r}"
            )
            if f["validation"]:
                print(f"  {'':<34} validation: {f['validation'][:90]!r}")

        if args.open_field:
            target = None
            for f in fields:
                if f["name"] == args.open_field:
                    for t in f["nuiexp"]:
                        if t.endswith("__trigger"):
                            target = t
                            break
            if not target:
                print(f"\nno __trigger for field {args.open_field!r}. "
                      f"Names seen: {[f['name'] for f in fields]}", file=sys.stderr)
                return 3
            print(f"\nopening options for {args.open_field!r} via [{target}]...")
            click_safe(page, page.locator(f'[data-nuiexp="{target}"]').first,
                      f"trigger:{args.open_field}")
            page.wait_for_timeout(5000)
            opts = page.evaluate(OPTIONS_JS)
            payload["opened"] = {"field": args.open_field, **opts}
            print(f"dialogs: {len(opts['dialogs'])}  options: {len(opts['options'])}")
            for d in opts["dialogs"]:
                print(f"  dialog role={d['role']} label={d['label']!r}")
                print(f"    {d['text'][:400]!r}")
            for o in opts["options"][:60]:
                mark = "*" if o["selected"] or o["checked"] else " "
                print(f"   {mark} [{o['role']:<10}] {o['text'][:80]!r}")

        path = f"{OUT}/fields_{expense_id[:16] or 'unknown'}.json"
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {path}")

        # Back out by navigation: this page has no Cancel/Close, and nothing
        # persists until `Save Expense`.
        open_report(page, args.report, settle_ms=4000)
        print(f"backed out; now at {page.url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
