"""Map a TAB of the Concur expense edit form — read-only, cancel-only.

`mapedit.py` opened the edit form but never left its default tab, so the
classification fields (expense type, account coding, business purpose) were never
seen. This module goes one level deeper: it opens the edit form, clicks a named
tab (default `Details`), and dumps what that tab actually contains.

Safety posture — deliberately paranoid, because this runs against a real report
with real dollar figures:

  1. Every click goes through `click_safe()`, which refuses any label in BLOCKED.
     `Save Expense` / `Submit Report` are unreachable from this module by
     construction, not by convention.
  2. Exactly one expense row, one tab per run. Nothing typed into any field.
  3. Back-out is navigation, not a button: the expense edit surface is a full
     page and **has no Cancel/Close control** (verified 2026-09-21). Nothing
     persists until `Save Expense`, so leaving the page discards the view.
  4. The baseline (default-tab) field set is captured first so the tab's fields
     can be reported as a DIFF. Note `Details` IS the default tab — the earlier
     session's "not yet opened" meant "not yet captured", not "not yet visited".

Usage:
    python -m concurrer.maptab [--report ID] [--row N] [--tab Details]
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
            label: lbl.slice(0, 80),
            required: !!(el.required || el.getAttribute('aria-required') === 'true'),
            disabled: !!el.disabled,
            readonly: !!el.readOnly,
            value: (el.value || '').slice(0, 80),
        };
    })"""

BUTTON_JS = """() => Array.from(document.querySelectorAll('button'))
    .filter(el => { const r = el.getBoundingClientRect();
        return r.width > 3 && r.height > 3; })
    .map(el => (el.innerText || '').trim()).filter(Boolean)"""

# Tab-shaped elements: role=tab, or the known tab labels rendered as buttons/links.
TABS_JS = """() => Array.from(document.querySelectorAll(
        '[role=tab], [role=tablist] *, a[href*="expense"], button'))
    .filter(el => { const r = el.getBoundingClientRect();
        return r.width > 3 && r.height > 3; })
    .map(el => ({
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role') || '',
        text: (el.innerText || '').trim().slice(0, 40),
        href: el.getAttribute('href') || '',
        selected: el.getAttribute('aria-selected') || '',
        disabled: !!el.disabled,
    }))
    .filter(t => t.text)"""

# Reading-order text of the active tab panel — carries section headings and
# read-only display values that are not form controls.
PANEL_TEXT_JS = """() => {
    const panels = Array.from(document.querySelectorAll(
        '[role=tabpanel]:not([hidden]), .sapcnqr-tab__panel'));
    const el = panels.find(p => { const r = p.getBoundingClientRect();
        return r.width > 10 && r.height > 10; })
        || document.querySelector('main') || document.body;
    return el.innerText.replace(/\\n{3,}/g, '\\n\\n').slice(0, 8000);
}"""

PANEL_EL_JS = """() => {
    const panels = Array.from(document.querySelectorAll(
        '[role=tabpanel]:not([hidden]), .sapcnqr-tab__panel'));
    const el = panels.find(p => { const r = p.getBoundingClientRect();
        return r.width > 10 && r.height > 10; })
        || document.querySelector('main') || document.body;
    return el.outerHTML.slice(0, 200000);
}"""


def field_rows_js(labels):
    """For each known label, describe the control that answers it.

    Concur's Details fields are not native inputs — the earlier pass found only
    the locked money fields and 24 row checkboxes. This keys off the label text
    and reports the widget shape of its sibling, which is what we need to drive
    edits later.
    """
    return """(labels) => {
        const desc = (el) => {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return {
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
                cls: (el.className || '').toString().slice(0, 90),
                editable: el.isContentEditable === true,
                tabindex: el.getAttribute('tabindex') || '',
                readonly: el.getAttribute('aria-readonly') || '',
                expanded: el.getAttribute('aria-expanded') || '',
                hasPopup: el.getAttribute('aria-haspopup') || '',
                text: (el.innerText || '').trim().slice(0, 60),
                w: Math.round(r.width), h: Math.round(r.height),
                cursor: s.cursor,
            };
        };
        const out = [];
        const leaves = Array.from(document.querySelectorAll('*')).filter(
            el => el.children.length === 0);
        for (const lab of labels) {
            const hit = leaves.find(el => (el.textContent || '').trim() === lab);
            if (!hit) { out.push({label: lab, found: false}); continue; }
            // Walk up to a container that also holds an answer element.
            let row = hit.parentElement, answer = null, hops = 0;
            for (; row && hops < 6; row = row.parentElement, hops++) {
                const kids = Array.from(row.querySelectorAll('*')).filter(
                    k => k !== hit && !(hit.contains(k))
                       && (k.innerText || '').trim()
                       && (k.innerText || '').trim() !== lab);
                if (kids.length) { answer = kids[kids.length - 1]; break; }
            }
            out.push({
                label: lab, found: true, hops: hops,
                label_el: desc(hit),
                row: desc(row),
                answer: desc(answer),
                row_html: row ? row.outerHTML.slice(0, 1200) : '',
            });
        }
        return out;
    }"""


def dump(page, label: str) -> dict:
    return {
        "url": page.url,
        "title": page.title(),
        "fields": page.evaluate(FIELD_JS),
        "buttons": page.evaluate(BUTTON_JS),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Map one tab of the expense edit form.")
    ap.add_argument("--report", default=REPORT)
    ap.add_argument("--row", type=int, default=1, help="expense row (0 = select-all header)")
    ap.add_argument("--tab", default="Details", help="tab label to open")
    args = ap.parse_args()

    if " ".join(args.tab.split()).lower() in BLOCKED:
        print(f"refusing: --tab {args.tab!r} is a blocked action label", file=sys.stderr)
        return 2

    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        try:
            expense_id = open_expense_edit(page, args.report, args.row)
        except RuntimeError as exc:
            print(f"cannot open expense: {exc}", file=sys.stderr)
            return 2
        print(f"expense: {expense_id}  url: {page.url}")

        baseline = dump(page, "default tab")
        base_labels = {f["label"] for f in baseline["fields"]}
        print(f"baseline: {len(baseline['fields'])} fields at {baseline['url']}")

        tabs = page.evaluate(TABS_JS)
        tab_info = [t for t in tabs if t["text"].lower().startswith(args.tab.lower().split()[0])]
        print(f"\ntab-shaped elements matching {args.tab!r}: {len(tab_info)}")
        for t in tab_info:
            print(f"  <{t['tag']} role={t['role'] or '-'} selected={t['selected'] or '-'} "
                  f"href={t['href'] or '-'}> {t['text']!r}")

        if not tab_info:
            print(f"\nNo {args.tab!r} tab found. All tabs seen:", file=sys.stderr)
            for t in tabs:
                print(f"  {t['text']!r}", file=sys.stderr)
            return 3

        target = page.get_by_role("tab", name=args.tab)
        if target.count() == 0:
            target = page.locator("button, a", has_text=args.tab)
        print(f"\nclicking {args.tab!r}...")
        click_safe(page, target.first, args.tab)
        page.wait_for_timeout(6000)

        after = dump(page, args.tab)
        panel_text = page.evaluate(PANEL_TEXT_JS)
        panel_html = page.evaluate(PANEL_EL_JS)

        # Labels seen in the panel text but absent from the native-control dump.
        # These are the classification fields; describe their widget shape.
        probe_labels = [
            "Expense Type",
            "Trip Type",
            "Business Purpose",
            "Type of Ground Transportation",
            "Were other travelers with you?",
            "Traveler's myUFL TA/UF GO Request ID",
            "Request",
        ]
        rows = page.evaluate(field_rows_js(probe_labels), probe_labels)

        new_fields = [f for f in after["fields"] if f["label"] not in base_labels]
        payload = {
            "report": args.report,
            "row": args.row,
            "tab": args.tab,
            "tab_candidates": tab_info,
            "before": baseline,
            "after": after,
            "new_fields": new_fields,
            "panel_text": panel_text,
            "panel_html": panel_html,
            "field_rows": rows,
        }
        path = f"{OUT}/tab_{args.tab.lower().replace(' ', '_')}.json"
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"wrote {path}")

        print(f"\nurl after tab: {after['url']}")
        print(f"fields on tab: {len(after['fields'])}  (new vs default tab: {len(new_fields)})")
        for f in after["fields"]:
            req = "REQUIRED" if f["required"] else "optional"
            lock = " LOCKED" if f["readonly"] or f["disabled"] else ""
            marker = "*" if f["label"] not in base_labels else " "
            print(
                f" {marker} {f['label'][:34]:<34} [{f['tag']}/{f['type']:<9}] "
                f"{req}{lock} val={f['value'][:30]!r}"
            )

        print(f"\n--- panel text ({len(panel_text)} chars) ---")
        print(panel_text[:3000])

        print(f"\n--- field widget shapes ---")
        for r in rows:
            if not r["found"]:
                print(f"  {r['label'][:38]:<38} NOT FOUND as a leaf label")
                continue
            a = r["answer"] or {}
            print(
                f"  {r['label'][:38]:<38} answer=<{a.get('tag','?')} "
                f"role={a.get('role') or '-'} cls={str(a.get('cls',''))[:44]} "
                f"editable={a.get('editable')} popup={a.get('hasPopup') or '-'}> "
                f"text={str(a.get('text',''))[:34]!r}"
            )

        cancel_after = [b for b in after["buttons"] if b.lower() in ("cancel", "close")]
        if cancel_after:
            print(f"\nclicking {cancel_after[0]!r} — NOT Save")
            click_safe(page, page.get_by_role("button", name=cancel_after[0]).first,
                      cancel_after[0])
            page.wait_for_timeout(2500)
            print(f"dismissed; now at {page.url}")
        else:
            # There is no Cancel/Close on this page. The edit surface is a full
            # page, not a modal: nothing persists until `Save Expense`, so the
            # back-out is navigating away. Do that explicitly rather than
            # relying on the browser closing.
            print("\nno Cancel/Close exists on this page — navigating away to back out")
            open_report(page, args.report, settle_ms=4000)
            print(f"backed out; now at {page.url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
