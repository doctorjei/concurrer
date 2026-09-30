"""Drivers for Concur's custom form widgets.

Concur's Details/create fields are custom comboboxes, not native controls — see
`designs/expense-reports.md`. This module is the only place that knows how to
operate them, so the edit path and the create path share one implementation.

Every click here goes through `click_safe`. Opening a field's option list is not
a write (its `data-nuiexp` is `field-<name>__trigger`, no save/submit in it);
committing an option passes the option's own text to the guard, so an option
named e.g. "Delete ..." would be refused rather than clicked.

Verified commit behaviour (2026-09-21, Test Report): a combobox commits on a
**single click** on the option. No confirm step, no Save-and-Add-Another needed.
"""

from __future__ import annotations

from .safety import click_safe


def field_value(page, label: str) -> str:
    """Current displayed value of a field, looked up by its label."""
    return page.evaluate(
        """(label) => {
            const labs = Array.from(document.querySelectorAll(
                '.sapcnqr-form-field__label'));
            const lab = labs.find(e => (e.innerText||'').trim() === label);
            if (!lab) return '<no such label>';
            const c = lab.parentElement ? lab.parentElement.parentElement : null;
            if (!c) return '<no container>';
            const vs = c.querySelector(
                '.sapcnqr-select-field__input, .sapcnqr-form-field__input');
            if (vs) return (vs.innerText || '').trim();
            const inp = c.querySelector('input, textarea');
            return inp ? (inp.value || '') : '<no value el>';
        }""",
        label,
    )


def field_inventory(page) -> list[dict]:
    """Every field on the current page: label, name, value, required, locked."""
    return page.evaluate(
        """() => {
        const out = [];
        const seen = new Set();
        for (const hd of document.querySelectorAll(
                '.sapcnqr-form-field__heading')) {
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
            if (!name) { for (const t of nui) {
                const m = t.match(/^field-([A-Za-z0-9_]+)__/);
                if (m) { name = m[1]; break; } } }
            let value = '';
            if (input) value = input.value || '';
            else {
                const vs = ctrl.querySelector(
                    '.sapcnqr-select-field__input, .sapcnqr-form-field__input');
                value = vs ? (vs.innerText || '').trim()
                          : (ctrl.innerText || '').trim();
            }
            const locked = input ? !!(input.readOnly || input.disabled)
                : (combo ? combo.getAttribute('aria-disabled') === 'true' : false);
            out.push({
                label, name, value: value.slice(0, 120),
                required: !!hd.querySelector('.sapcnqr-form-field__required')
                    || (combo ? combo.getAttribute('aria-required') === 'true'
                             : false),
                locked,
                widget: combo ? 'combobox'
                    : (input.tagName.toLowerCase() + '/' + (input.type || '')),
                nuiexp: nui,
            });
        }
        return out;
    }"""
    )


def read_options(page) -> list[str]:
    """Option texts in whatever option list is currently open, deduped.

    Concur option rows carry a long account-code description after the display
    name on a second line. The display name is the first line.
    """
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll(
            '[role=dialog] [role=option], [role=listbox] [role=option]'))
            .filter(el => { const r = el.getBoundingClientRect();
                return r.width > 3 && r.height > 3; })
            .map(el => (el.innerText || '').trim())
            .filter(Boolean)"""
    )
    return list(dict.fromkeys(raw))


def option_index(page) -> dict[str, int]:
    """Display name -> option index, for clicking by position.

    Clicking by index rather than by text is deliberate. Playwright normalises
    whitespace when matching `has_text`, so a pattern containing a newline or a
    non-breaking space from a Concur description will not match the normalised
    DOM text — a real failure seen 2026-09-21. Index is unambiguous.
    """
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll(
            '[role=dialog] [role=option], [role=listbox] [role=option]'))
            .filter(el => { const r = el.getBoundingClientRect();
                return r.width > 3 && r.height > 3; })
            .map(el => (el.innerText || '').trim())
            .filter(Boolean)"""
    )
    out: dict[str, int] = {}
    for i, text in enumerate(raw):
        name = text.split("\n")[0].strip()
        if name and name not in out:
            out[name] = i
    return out


def fill_combobox(page, name: str, label: str,
                 prefer: str | None = None) -> str:
    """Open a combobox and commit one option. Returns the committed value.

    `prefer` picks the first option whose display name contains that substring;
    otherwise the first option is taken.
    """
    trig = page.locator(f'[data-nuiexp="field-{name}__trigger"]')
    if trig.count() == 0:
        print(f"    {label}: no trigger for {name!r}")
        return ""
    click_safe(page, trig.first, f"open options for {label}")
    page.wait_for_timeout(3500)

    idx = option_index(page)
    if not idx:
        print(f"    {label}: dialog opened but no [role=option]")
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
        return ""
    print(f"    {label}: {len(idx)} options -> {list(idx)[:8]}")

    chosen = next((nm for nm in idx if prefer and prefer.lower() in nm.lower()),
                 None)
    if chosen is None:
        chosen = next(iter(idx))

    opts = page.locator(
        '[role=dialog] [role=option], [role=listbox] [role=option]')
    # Pass the display name, not the full description, so the guard sees a
    # clean label.
    click_safe(page, opts.nth(idx[chosen]), chosen)
    page.wait_for_timeout(3000)
    after = field_value(page, label)
    print(f"    {label}: committed {after!r}")
    return after


def fill_text(page, field: dict, value: str) -> bool:
    """Fill a native input/textarea by name, falling back to its label."""
    loc = page.get_by_role("textbox", name=field["label"])
    if loc.count() == 0 and field["name"]:
        loc = page.locator(
            f"input[name='{field['name']}'], textarea[name='{field['name']}']")
    if loc.count() == 0:
        return False
    loc.first.fill(value)
    return True
