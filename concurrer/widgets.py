"""Drivers for Concur's custom expense-form widgets.

Concur Details fields are custom comboboxes rather than native controls. Stable
``data-nuiexp="field-<name>__trigger"`` handles let us operate them without
React's transient IDs. This module centralizes inventory and widget mechanics;
the editor uses only its strict write helpers, never the permissive creation
helper retained for the throwaway creation harness.
"""

from __future__ import annotations

from .safety import assert_write_authorized, click_safe

OPTIONS = "[role=dialog] [role=option], [role=listbox] [role=option]"


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def field_value(page, label: str) -> str:
    """Current displayed value of a field, looked up by visible label."""
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
    """Read complete live field state using stable canonical field names.

    Values are intentionally not truncated: the result is used for exact
    validation and post-save verification, not just human-facing summaries.
    """
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
                .map(e => e.getAttribute('data-nuiexp')).filter(Boolean);

            let name = (combo && combo.querySelector('[name]')
                        && combo.querySelector('[name]').getAttribute('name'))
                    || (input && input.getAttribute('name')) || '';
            if (!name) { for (const t of nui) {
                const m = t.match(/^field-([A-Za-z0-9_]+)(?:__|$)/);
                if (m) { name = m[1]; break; }
            } }

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
            const validation = container.querySelector('[data-test="validation-status"]');
            const trigger = nui.find(t => t.endsWith('__trigger')) || '';
            out.push({
                label, name, value,
                required: !!hd.querySelector('.sapcnqr-form-field__required')
                    || (combo ? combo.getAttribute('aria-required') === 'true'
                             : false),
                locked,
                widget: combo ? 'combobox'
                    : (input.tagName.toLowerCase() + '/' + (input.type || '')),
                nuiexp: nui,
                trigger,
                inputNuiexp: input ? (input.getAttribute('data-nuiexp') || '') : '',
                maxlength: input ? (input.getAttribute('maxlength') || '') : '',
                validation: validation ? (validation.innerText || '').trim() : '',
            });
        }
        return out;
    }"""
    )


def field_by_name(fields: list[dict], name: str) -> dict:
    """Return one canonical field or fail closed on absence/ambiguity."""
    matches = [field for field in fields if field["name"] == name]
    if len(matches) != 1:
        detail = "absent" if not matches else "ambiguous"
        raise ValueError(f"field {name!r} is {detail} in the live form")
    return matches[0]


def field_value_by_name(page, name: str) -> str:
    """Read one field's current full value by its canonical name."""
    return field_by_name(field_inventory(page), name)["value"]


def _option_texts(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll(
            '[role=dialog] [role=option], [role=listbox] [role=option]'))
            .filter(el => { const r = el.getBoundingClientRect();
                return r.width > 3 && r.height > 3; })
            .map(el => (el.innerText || '').trim())
            .filter(Boolean)"""
    )


def read_options(page) -> list[str]:
    """Option texts in the currently open list, deduplicated for display."""
    return list(dict.fromkeys(_option_texts(page)))


def option_index(page) -> dict[str, int]:
    """First-line display name to option index for legacy read/create callers."""
    out: dict[str, int] = {}
    for index, text in enumerate(_option_texts(page)):
        name = text.split("\n")[0].strip()
        if name and name not in out:
            out[name] = index
    return out


def exact_option_position(option_texts: list[str], requested: str) -> int:
    """Return the one exact normalized first-line option index or fail closed."""
    requested_norm = _norm(requested)
    matches = [
        index
        for index, text in enumerate(option_texts)
        if _norm(text.split("\n")[0]) == requested_norm
    ]
    if len(matches) != 1:
        match_desc = "no" if not matches else "multiple"
        raise ValueError(f"{match_desc} exact options for {requested!r}")
    return matches[0]


def _trigger_for(field: dict) -> str:
    trigger = field.get("trigger") or ""
    if trigger:
        return trigger
    for nuiexp in field.get("nuiexp", []):
        if nuiexp.endswith("__trigger"):
            return nuiexp
    if field.get("name"):
        return f"field-{field['name']}__trigger"
    return ""


def select_combobox_exact(page, field: dict, requested: str) -> str:
    """Commit one uniquely exact normalized option and verify its displayed value.

    This is deliberately separate from ``fill_combobox()``. It never performs a
    substring match or first-option fallback, both of which are unacceptable for
    an operator-requested edit.
    """
    if field.get("widget") != "combobox":
        raise ValueError(f"field {field.get('name')!r} is not a combobox")
    if field.get("locked"):
        raise ValueError(f"field {field.get('name')!r} is locked")
    if not requested or not requested.strip():
        raise ValueError("combobox value must not be blank")
    assert_write_authorized(page.url)

    trigger = _trigger_for(field)
    if not trigger:
        raise ValueError(f"field {field.get('name')!r} has no stable options trigger")
    locator = page.locator(f'[data-nuiexp="{trigger}"]')
    if locator.count() != 1:
        raise RuntimeError(f"expected one trigger for field {field.get('name')!r}")
    click_safe(page, locator, f"open options for {field['label']}")
    page.wait_for_timeout(1500)

    texts = _option_texts(page)
    try:
        index = exact_option_position(texts, requested)
    except ValueError as exc:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        raise ValueError(f"{exc} in {field['label']!r}") from exc

    chosen = texts[index].split("\n")[0].strip()
    # Opening a picker is not itself a mutation, but its option commits one.
    # Recheck the live Test Report route immediately before that click so a
    # redirect during the popup wait cannot turn it into a real-report edit.
    assert_write_authorized(page.url)
    options = page.locator(OPTIONS)
    click_safe(page, options.nth(index), chosen)
    page.wait_for_timeout(1200)
    after = field_value(page, field["label"])
    if _norm(after) != _norm(requested):
        raise RuntimeError(
            f"field {field['label']!r} did not take {requested!r}; now {after!r}"
        )
    return after


def fill_text_exact(page, field: dict, value: str) -> str:
    """Fill an unlocked native field after Test Report authorization and readback."""
    if field.get("locked"):
        raise ValueError(f"field {field.get('name')!r} is locked")
    if not field.get("widget", "").startswith(("input/", "textarea/")):
        raise ValueError(f"field {field.get('name')!r} is not a native text field")
    if not value or not value.strip():
        raise ValueError("text value must not be blank")
    maxlength = field.get("maxlength") or ""
    if maxlength.isdigit() and len(value) > int(maxlength):
        raise ValueError(
            f"value for {field['label']!r} exceeds maxlength {maxlength}"
        )
    assert_write_authorized(page.url)

    loc = page.get_by_role("textbox", name=field["label"])
    if loc.count() == 0 and field.get("name"):
        name = field["name"]
        loc = page.locator(f"input[name='{name}'], textarea[name='{name}']")
    if loc.count() == 0 and field.get("inputNuiexp"):
        loc = page.locator(f'[data-nuiexp="{field["inputNuiexp"]}"]')
    if loc.count() != 1:
        raise RuntimeError(f"expected one native control for field {field['label']!r}")
    loc.fill(value)
    # Read the same native control we filled. Label/container traversal is not
    # reliable for Comment and can select a decorative sibling instead.
    after = loc.input_value()
    if after != value:
        raise RuntimeError(
            f"field {field['label']!r} did not take {value!r}; now {after!r}"
        )
    return after


def fill_combobox(page, name: str, label: str, prefer: str | None = None) -> str:
    """Legacy permissive combobox filler for the Test Report creation harness.

    ``prefer`` selects the first containing option; absent a match, it selects the
    first option. The safe editor must use ``select_combobox_exact`` instead.
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

    chosen = next((nm for nm in idx if prefer and prefer.lower() in nm.lower()), None)
    if chosen is None:
        chosen = next(iter(idx))

    opts = page.locator(OPTIONS)
    click_safe(page, opts.nth(idx[chosen]), chosen)
    page.wait_for_timeout(3000)
    after = field_value(page, label)
    print(f"    {label}: committed {after!r}")
    return after


def fill_text(page, field: dict, value: str) -> bool:
    """Legacy unguarded text filler retained for existing creation code only."""
    loc = page.get_by_role("textbox", name=field["label"])
    if loc.count() == 0 and field["name"]:
        loc = page.locator(
            f"input[name='{field['name']}'], textarea[name='{field['name']}']"
        )
    if loc.count() == 0:
        return False
    loc.first.fill(value)
    return True


OPTIONS_JS = """() => {
  const all = Array.from(document.querySelectorAll(
      '[role=dialog] [role=option], [role=listbox] [role=option]'));
  const visible = all.filter(el => { const r = el.getBoundingClientRect();
      return r.width > 3 && r.height > 3; });
  const setsize = all.map(el => el.getAttribute('aria-setsize'))
      .find(Boolean) || null;
  return {texts: visible.map(el => (el.innerText || '').trim()).filter(Boolean),
          domCount: all.length, setsize};
}"""


class OptionsReadError(RuntimeError):
    """A field changed while only its options were being read."""


def read_combobox_options(page, field: dict) -> dict:
    """Open one combobox, read its options, close it, prove nothing changed.

    Never clicks an option. Safe on real reports: opening a picker is not a
    mutation, and the field value is re-read after Escape — any difference
    raises ``OptionsReadError`` so the caller abandons the form unsaved.
    """
    trigger = _trigger_for(field)
    if not trigger:
        return {"skipped": "no stable trigger"}
    locator = page.locator(f'[data-nuiexp="{trigger}"]')
    if locator.count() != 1:
        return {"skipped": f"{locator.count()} trigger matches"}
    before = field["value"]
    click_safe(page, locator, f"open options for {field['label']}")
    page.wait_for_timeout(1500)
    got = page.evaluate(OPTIONS_JS)
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    after = next(
        (f["value"] for f in field_inventory(page) if f["name"] == field["name"]),
        None,
    )
    if after != before:
        raise OptionsReadError(
            f"{field['name']} changed from {before!r} to {after!r} while only "
            "reading options; aborting without saving"
        )
    got["options"] = list(dict.fromkeys(got.pop("texts")))
    got["maybeTruncated"] = bool(
        got["setsize"] and str(len(got["options"])) != str(got["setsize"])
    ) or got["domCount"] > len(got["options"])
    return got
