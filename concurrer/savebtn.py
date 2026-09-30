"""Inspect the `Save Expense` control's DOM — read-only, clicks NOTHING.

Prompted by the operator's observation that the button looks "almost like a
dropdown". That matters for two reasons:

  1. A split button may expose several save variants, one of which could be a
     submit — the irreversible action this project never takes.
  2. The click guard in `safety.py` matches whole labels. A nested item labelled
     "Save and Submit" matches neither `save expense` nor `submit`, so a
     whole-label guard can be bypassed by compound labels. This module exists to
     find out whether such labels exist, so the guard can be widened to match
     dangerous *verbs* instead.

This module performs no clicks at all. It reads markup only.

Usage:
    python -m concurrer.savebtn [--report ID] [--row N]
"""

from __future__ import annotations

import argparse
import json
import sys

from .login import authenticated_context
from .nav import open_expense_edit, open_report
from .secrets import resolve

OUT = "/home/agent/.config/concur/discover"
REPORT = "506EC055FE79452BB943"

# Every element whose text mentions a commit-ish verb, with its full markup.
PROBE_JS = """() => {
    const verbs = /save|submit|submitting|complete|finish|send/i;
    const vis = (el) => { const r = el.getBoundingClientRect();
        return {w: Math.round(r.width), h: Math.round(r.height),
                top: Math.round(r.top), left: Math.round(r.left)}; };
    const desc = (el) => {
        const attrs = {};
        for (const a of el.attributes) attrs[a.name] = a.value.slice(0, 120);
        return {
            tag: el.tagName.toLowerCase(),
            attrs: attrs,
            text: (el.innerText || '').trim().slice(0, 120),
            rect: vis(el),
            haspopup: el.getAttribute('aria-haspopup') || '',
            expanded: el.getAttribute('aria-expanded') || '',
            disabled: !!el.disabled,
            children: el.children.length,
        };
    };
    const hits = [];
    for (const el of document.querySelectorAll(
            'button, [role=button], a, [role=menuitem], [role=menuitemradio]')) {
        const t = (el.innerText || '').trim();
        if (!t || !verbs.test(t)) continue;
        const parent = el.parentElement;
        const grand = parent ? parent.parentElement : null;
        hits.push({
            self: desc(el),
            parent: parent ? desc(parent) : null,
            grandparent: grand ? desc(grand) : null,
            siblings: parent ? Array.from(parent.children)
                .filter(c => c !== el)
                .map(c => ({tag: c.tagName.toLowerCase(),
                           text: (c.innerText || '').trim().slice(0, 60),
                           haspopup: c.getAttribute('aria-haspopup') || '',
                           role: c.getAttribute('role') || ''})) : [],
            html: el.outerHTML.slice(0, 2500),
            parent_html: parent ? parent.outerHTML.slice(0, 4000) : '',
        });
    }
    // Any menu/list markup already present in the DOM but not visible — a
    // dropdown's items often exist before the menu is opened.
    const latent = Array.from(document.querySelectorAll(
        '[role=menu], [role=listbox], [role=menuitem], .sapcnqr-menu'))
        .map(el => ({
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            text: (el.innerText || '').trim().slice(0, 200),
            visible: vis(el).w > 5 && vis(el).h > 5,
            cls: (el.className || '').toString().slice(0, 80),
        }));
    return {hits: hits, latent_menus: latent.slice(0, 40)};
}"""


# What an opened save-variants menu contains.
MENU_JS = """() => {
    const vis = (el) => { const r = el.getBoundingClientRect();
        return r.width > 5 && r.height > 5; };
    const menus = Array.from(document.querySelectorAll(
        '[role=menu], [role=listbox], .sapcnqr-menu, .sapcnqr-popper'))
        .filter(vis);
    const items = [];
    for (const m of menus) {
        for (const it of m.querySelectorAll(
                '[role=menuitem], [role=menuitemradio], [role=option], li, button')) {
            if (!vis(it)) continue;
            const t = (it.innerText || '').trim();
            if (!t) continue;
            items.push({
                role: it.getAttribute('role') || it.tagName.toLowerCase(),
                text: t.slice(0, 120),
                nuiexp: it.getAttribute('data-nuiexp') || '',
                disabled: !!it.disabled,
                checked: it.getAttribute('aria-checked') || '',
            });
        }
    }
    return {
        menus: menus.map(m => ({
            role: m.getAttribute('role') || m.tagName.toLowerCase(),
            label: m.getAttribute('aria-label') || '',
            text: (m.innerText || '').trim().slice(0, 800),
        })),
        items: items.slice(0, 60),
    };
}"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect the Save control, no clicks.")
    ap.add_argument("--report", default=REPORT)
    ap.add_argument("--row", type=int, default=1)
    ap.add_argument("--menu", action="store_true",
                    help="open the 'More Save Options' menu and read its items. "
                         "Opens the dropdown only — never the action button.")
    args = ap.parse_args()

    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        expense_id = open_expense_edit(page, args.report, args.row)
        print(f"expense: {expense_id}")

        data = page.evaluate(PROBE_JS)
        path = f"{OUT}/savebtn_{expense_id[:16]}.json"
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
        print(f"wrote {path}\n")

        print(f"=== {len(data['hits'])} commit-ish elements ===")
        for h in data["hits"]:
            s = h["self"]
            print(f"\n<{s['tag']}> text={s['text']!r} rect={s['rect']}")
            print(f"  attrs: {json.dumps(s['attrs'])[:400]}")
            print(f"  haspopup={s['haspopup'] or '-'} expanded={s['expanded'] or '-'} "
                  f"disabled={s['disabled']} children={s['children']}")
            if h["siblings"]:
                print(f"  siblings: {json.dumps(h['siblings'])[:400]}")
            if h["parent"]:
                print(f"  parent: <{h['parent']['tag']}> "
                      f"cls={h['parent']['attrs'].get('class','')[:80]!r} "
                      f"children={h['parent']['children']}")

        print(f"\n=== latent menu markup ({len(data['latent_menus'])}) ===")
        for m in data["latent_menus"]:
            print(f"  [{'VIS' if m['visible'] else 'hid'}] {m['role']:<10} "
                  f"{m['text'][:90]!r}")

        if args.menu:
            # Target the trigger by its accessible name only. The action button
            # is `exp-save-expense` and is never addressed by this module.
            trig = page.get_by_role("button", name="More Save Options")
            n = trig.count()
            print(f"\n'More Save Options' triggers found: {n}")
            if n == 0:
                print("  not found — cannot open menu", file=sys.stderr)
            else:
                from .safety import click_safe
                print("opening the dropdown (menu only, not the action)...")
                click_safe(page, trig.first, "More Save Options")
                page.wait_for_timeout(4000)
                menu = page.evaluate(MENU_JS)
                data["menu"] = menu
                with open(path, "w") as fh:
                    json.dump(data, fh, indent=2)
                print(f"menus: {len(menu['menus'])}  items: {len(menu['items'])}")
                for m in menu["menus"]:
                    print(f"  <{m['role']}> label={m['label']!r}")
                    print(f"    {m['text'][:500]!r}")
                for it in menu["items"]:
                    d = " disabled" if it["disabled"] else ""
                    print(f"   [{it['role']:<12}] {it['text'][:70]!r}"
                          f"{d} nuiexp={it['nuiexp'] or '-'}")

        open_report(page, args.report, settle_ms=4000)
        print(f"\nbacked out; now at {page.url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
