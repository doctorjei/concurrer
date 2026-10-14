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


#: ``field_value`` sentinels meaning the label lookup itself failed.
_LABEL_MISSES = ("<no such label>", "<no container>", "<no value el>")
_READBACK_TRIES = 16  # x 500 ms


def _readback(page, field: dict) -> str:
    """Displayed value after a write: by visible label, else by stable name.

    On a card-fed CCED Airfare -> Phone/Fax/Internet switch the label lookup
    found no "Expense Type" label although the form showed it and the
    name-keyed inventory read the new value (rehearsal 2026-10-14). A label
    miss is a lookup failure, not a value, so fall back to the stable name.
    """
    # A type switch re-renders the whole form; for a moment neither the label
    # nor the named field exists (Test Report Train/Rail -> Supplies,
    # 2026-10-14). Poll briefly instead of reading one fixed instant.
    for attempt in range(_READBACK_TRIES):
        value = field_value(page, field["label"])
        if value not in _LABEL_MISSES:
            return value
        if field.get("name"):
            try:
                return field_value_by_name(page, field["name"])
            except ValueError:
                pass
        if attempt + 1 < _READBACK_TRIES:
            page.wait_for_timeout(500)
    return value


def settled_inventory(page, *, interval_ms: int = 1000, max_reads: int = 10) -> list[dict]:
    """Field inventory once the form stops changing shape.

    A type switch re-renders the form progressively; one fixed-delay read can
    miss fields still being added. Read until two consecutive reads show the
    same field names (and Expense Type is present), up to ``max_reads``.
    """
    previous = None
    fields: list[dict] = []
    for _ in range(max_reads):
        fields = field_inventory(page)
        names = tuple(sorted(f["name"] for f in fields if f.get("name")))
        if names == previous and "expenseType" in names:
            return fields
        previous = names
        page.wait_for_timeout(interval_ms)
    return fields


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


#: Pickers with no ``__trigger`` element: the ``field-<name>`` container is
#: itself the ``role=combobox`` that opens an ordinary option list (read-only
#: probe on CCED, 2026-10-12: "None Selected" / "None" / linked requests).
CONTAINER_TRIGGERED = frozenset({"travelRequestExpenseId"})


def _trigger_for(field: dict) -> str:
    trigger = field.get("trigger") or ""
    if trigger:
        return trigger
    if field.get("name") in CONTAINER_TRIGGERED:
        return f"field-{field['name']}"
    for nuiexp in field.get("nuiexp", []):
        if nuiexp.endswith("__trigger"):
            return nuiexp
    if field.get("name"):
        return f"field-{field['name']}__trigger"
    return ""


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


#: Search-backed combobox inputs whose options appear only after typing.
SEARCH_INPUT_NUIEXP = {
    "location": "field-location__input",
}


def select_search_combobox_exact(page, field: dict, requested: str,
                                 timeout_ms: int = 10_000) -> str:
    """Select the one exact suggestion from a type-to-search combobox.

    Location is the current example: its suggestion list is empty until the
    requested text is typed. This never clicks a partial or ambiguous match.
    """
    name = field.get("name") or ""
    if field.get("widget") != "combobox":
        raise ValueError(f"field {name!r} is not a combobox")
    if field.get("locked"):
        raise ValueError(f"field {name!r} is locked")
    if not requested or not requested.strip():
        raise ValueError("combobox value must not be blank")
    assert_write_authorized(page.url)

    trigger = _trigger_for(field)
    if not trigger:
        raise ValueError(f"field {name!r} has no stable options trigger")
    input_nuiexp = field.get("inputNuiexp") or SEARCH_INPUT_NUIEXP.get(name, "")
    if not input_nuiexp:
        raise ValueError(f"field {name!r} has no stable search input")
    trigger_locator = page.locator(f'[data-nuiexp="{trigger}"]')
    if trigger_locator.count() != 1:
        raise RuntimeError(f"expected one trigger for field {name!r}")
    click_safe(page, trigger_locator, f"open options for {field['label']}")
    page.wait_for_timeout(1200)
    # The search input is rendered only after its picker opens.
    search = page.locator(f'[data-nuiexp="{input_nuiexp}"]')
    if search.count() != 1:
        raise RuntimeError(f"expected one search box for field {name!r}")
    search.first.fill("")
    search.first.type(requested, delay=50)

    texts: list[str] = []
    waited = 0
    while waited < timeout_ms:
        got = page.evaluate(OPTIONS_JS)
        texts = list(dict.fromkeys(got.get("texts", [])))
        if texts:
            break
        page.wait_for_timeout(500)
        waited += 500
    try:
        index = exact_option_position(texts, requested)
    except ValueError as exc:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        raise ValueError(f"{exc} in {field['label']!r}") from exc

    chosen = texts[index].split("\n")[0].strip()
    assert_write_authorized(page.url)
    options = page.locator(OPTIONS)
    click_safe(page, options.nth(index), chosen)
    page.wait_for_timeout(1200)
    after = _readback(page, field)
    if _norm(after) != _norm(requested):
        raise RuntimeError(
            f"field {field['label']!r} did not take {requested!r}; now {after!r}"
        )
    _dismiss_location_currency_prompt(page)
    after = _readback(page, field)
    if _norm(after) != _norm(requested):
        raise RuntimeError(
            f"field {field['label']!r} did not retain {requested!r}; now {after!r}"
        )
    return after


def _dismiss_location_currency_prompt(page) -> bool:
    """Cancel Concur's Location currency-change prompt without changing currency.

    Choosing a location in another currency opens a modal that blocks Save.
    This cancels that prompt only: it preserves both the selected location
    and the existing currency. Any other dialog raises instead of being
    dismissed blindly.
    """
    dialogs = page.locator("[role=alertdialog]")
    if dialogs.count() == 0:
        return False
    if dialogs.count() != 1:
        raise RuntimeError("refusing to dismiss an ambiguous dialog state")
    text = dialogs.first.inner_text() or ""
    if "Currency Change" not in text or "different currency" not in text:
        raise RuntimeError(f"unexpected dialog blocking save: {text[:200]!r}")
    cancel = dialogs.first.get_by_role("button", name="Cancel")
    if cancel.count() != 1:
        raise RuntimeError("location currency prompt has no single Cancel control")
    click_safe(page, cancel, "Cancel currency change")
    page.wait_for_timeout(1000)
    if dialogs.count() != 0:
        raise RuntimeError("location currency prompt did not close after Cancel")
    return True


#: Selector for Concur's blocking confirmation modals (role varies by build).
CONFIRM_DIALOGS = "[role=alertdialog], [role=dialog][aria-modal=true]"


def blocking_dialog_text(page) -> str | None:
    """Text of the one visible blocking modal, if any (read-only)."""
    dialogs = page.locator(CONFIRM_DIALOGS)
    texts = [dialogs.nth(i).inner_text() or "" for i in range(dialogs.count())
             if dialogs.nth(i).is_visible()]
    texts = [t.strip() for t in texts if t.strip()]
    if not texts:
        return None
    return "\n---\n".join(texts)


def cancel_blocking_dialog(page) -> None:
    """Click the single Cancel of the visible blocking modal; never Continue."""
    dialogs = page.locator(CONFIRM_DIALOGS)
    visible = [dialogs.nth(i) for i in range(dialogs.count()) if dialogs.nth(i).is_visible()]
    if len(visible) != 1:
        raise RuntimeError(f"refusing to cancel an ambiguous dialog state ({len(visible)})")
    cancel = visible[0].get_by_role("button", name="Cancel")
    if cancel.count() != 1:
        raise RuntimeError("blocking dialog has no single Cancel control")
    click_safe(page, cancel, "Cancel")
    page.wait_for_timeout(1000)


def continue_blocking_dialog(page) -> None:
    """Click the single Continue of the visible blocking modal.

    Only callers holding an explicit, token-covered operator acceptance for
    this exact prompt may use it (``edit.is_attendee_removal``).
    """
    dialogs = page.locator(CONFIRM_DIALOGS)
    visible = [dialogs.nth(i) for i in range(dialogs.count()) if dialogs.nth(i).is_visible()]
    if len(visible) != 1:
        raise RuntimeError(f"refusing to confirm an ambiguous dialog state ({len(visible)})")
    button = visible[0].get_by_role("button", name="Continue")
    if button.count() != 1:
        raise RuntimeError("blocking dialog has no single Continue control")
    click_safe(page, button, "Continue")
    page.wait_for_timeout(1000)


def select_payment_type_exact(page, field: dict, requested: str) -> str:
    """Select Payment Type from its container-opened option list.

    This control has no stable ``__trigger`` handle; opening its container
    reveals exactly its valid options. Opening it can also populate a default,
    so the requested value is accepted only when it is also the one exact
    visible option and the control reads back exactly that value.
    """
    name = field.get("name") or ""
    if name != "paymentType":
        raise ValueError(f"field {name!r} is not Payment Type")
    if field.get("locked"):
        raise ValueError(f"field {name!r} is locked")
    if not requested or not requested.strip():
        raise ValueError("combobox value must not be blank")
    assert_write_authorized(page.url)

    container = page.locator('[data-nuiexp="field-paymentType"]')
    if container.count() != 1:
        raise RuntimeError("expected one Payment Type control")
    click_safe(page, container, f"open options for {field['label']}")
    page.wait_for_timeout(1200)
    got = page.evaluate(OPTIONS_JS)
    texts = list(dict.fromkeys(got.get("texts", [])))
    index = exact_option_position(texts, requested)
    chosen = texts[index].split("\n")[0].strip()
    if _norm(_readback(page, field)) != _norm(requested):
        assert_write_authorized(page.url)
        options = page.locator(OPTIONS)
        click_safe(page, options.nth(index), chosen)
        page.wait_for_timeout(1200)
    after = _readback(page, field)
    if _norm(after) != _norm(requested):
        raise RuntimeError(
            f"field {field['label']!r} did not take {requested!r}; now {after!r}"
        )
    return after


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
    after = _readback(page, field)
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

    # Exact label: a substring match finds both "Amount" and "Amount in USD"
    # once a foreign currency is chosen (CCED create proof, 2026-10-14).
    loc = page.get_by_role("textbox", name=field["label"], exact=True)
    if loc.count() != 1 and field.get("name"):
        name = field["name"]
        loc = page.locator(f"input[name='{name}'], textarea[name='{name}']")
    if loc.count() != 1 and field.get("inputNuiexp"):
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
