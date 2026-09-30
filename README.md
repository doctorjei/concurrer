# concurrer

Automation for SAP Concur (UF `us2` tenant) — sign in through UF SSO + Duo, then
read and edit expense reports programmatically.

Python 3.13 · Playwright · [`duo-pass`](https://pypi.org/project/duo-pass/) (GPL-3.0-or-later)

---

## Setup

```bash
scripts/setup.sh          # reprovision after a container rebuild
```

Chromium's system libraries live in the **container rootfs, not `$HOME`** — a
rebuild wipes them and the browser will fail to launch until reinstalled.

Credentials come from `~/.config/concur/secrets.env` (mode 600):

```
UFL_USERNAME=...        # bare GatorLink id, e.g. erthworm
UFL_PASSWORD=***
CONCUR_USERNAME=...     # the Concur principal, e.g. erthworm@example.edu
```

**These are two different principals.** Concur identifies you by email; UF by the
bare id. They are not aliases and must not be conflated. `CONCUR_USERNAME` is
currently *derived* as `UFL_USERNAME@ufl.edu` with a loud warning — set it
explicitly.

Never paste a secret into chat. Place the file yourself. This box has **no
controlling terminal** (`/dev/tty` → ENXIO), so nothing can prompt for it.

## Login

Four hops, all handled by `concurrer.login`:

| # | Where | What |
|---|---|---|
| 1 | `us2.concursolutions.com` | identity form → Concur email |
| 1b | `/nui/signin/hrd` | provider picker → "University of Florida SSO" |
| 2 | UF Shibboleth | `j_username` / `j_password` |
| 3 | Duo | `duo_pass.complete_duo(page, PushApprover())` |

The tenant is **`us2`** — `www.concursolutions.com` shows a bare identity field
that never resolves to a provider. Hop 1b is not optional: the Shibboleth form
does not exist in the DOM until the provider button is clicked.

**Device trust** persists in `~/.config/concur/browser` for ~10h, so re-runs
skip Duo entirely. **The Concur session cookie is session-scoped** — it dies when
the browser closes, so all work must happen inside `authenticated_context()`,
the same browser that authenticated. A second process gets Duo trust but not the
session.

Use `PushApprover`, never `PasscodeApprover` — the latter calls `input()` and
hangs forever with no terminal.

## Safety model

This drives a live system with real money. Safety is structural, not advisory.

**Every click goes through `safety.click_safe()`.** Three tiers:

- **NEVER** — `submit` `approve` `reject` `recall` `void` `delete` `duplicate`
  `allocate` `combine` `remove` `discard`. No grant unlocks these, on any
  report.
- **GRANTABLE** — `save`, and only while the **live browser URL** is the
  authorized report, re-checked on every click.
- **FREE** — everything else (Edit, Details, View Alerts, row checkboxes).

`authorize_writes(report_id)` **refuses any report other than the throwaway Test
Report** (`B857BA45DBC24EDB8420`). The grant cannot widen into something
dangerous; it only ever unlocks `save` on one report.

Dangerous verbs match as **substrings**, and `data-nuiexp` is checked
independently of the label. A whole-label guard is bypassable — `"Save and
Submit"` matches neither `save expense` nor `submit`. Over-blocking is the
correct failure direction: a false refusal costs a retry, a false allow writes
to a real expense.

One carve-out: the `More Save Options` menu trigger legitimately says "Save" but
only opens a menu. It passes only with **both**
`data-nui-widgets="menu-button-trigger"` **and** a truthy `aria-haspopup`.

## Modules

| Module | Purpose |
|---|---|
| `secrets.py` | Credential resolution; two principals, never conflated |
| `login.py` | `sign_in_page()` + `authenticated_context()` + `login()` |
| `safety.py` | Three-tier click guard; URL-scoped write grant |
| `nav.py` | `open_expense_edit()` — row-index trap handled |
| `widgets.py` | Concur custom-field drivers: `fill_combobox`, `fill_text`, `field_inventory`, `option_index` |
| `fields.py` | Field inventory + option reader |
| `newexp.py` | Create path: `--list-types` / `--type` / `--save` |
| `cascade.py` | Tests the `expenseType` → custom-fields cascade |
| `typesweep.py` | Maps all 67 expense types to their custom fields |
| `triage.py` | Per-expense: which required fields are empty |
| `savebtn.py` | Dumps the Save split-button; `--menu` reads its variants |
| `discover.py` | Read-only explorer; destructive labels blocked |
| `mapform.py` / `mapedit.py` / `maptab.py` | Form mapping, read-only |
| `probe.py` | Page structure probe (no credentials needed) |

## Running

```bash
.venv/bin/python -m concurrer.login                       # verify sign-in
.venv/bin/python -m concurrer.fields --report <ID> --row 1
.venv/bin/python -m concurrer.triage --report <ID> --detail --max 5
.venv/bin/python -m concurrer.newexp --type "Ground Transportation" --save   # TEST REPORT ONLY
.venv/bin/python -m concurrer.typesweep
```

Evidence lands in `~/.config/concur/discover/` as JSON. **Screenshots from
`/nui/` pages render blank** (the SPA layout defeats full-page capture) — the
JSON dumps are the reliable record, not the PNGs.

## Things that will bite you

- **Concur's Details fields are not native controls.** The value is a
  `<span role="presentation" name="...">` inside a `role="combobox"`. A
  `querySelectorAll('input,select,textarea')` sweep reports them as absent.
- **`customNN` slots are reused positionally.** `custom20` means twelve
  different things depending on expense type. Key on
  `(expenseType, customSlot)`, never the slot name alone.
- **Changing `expenseType` silently drops the type-specific custom values.**
  The field comes back if you switch types again; the value does not.
- **Never click a Concur option by text.** Playwright normalises whitespace in
  `has_text` matching, so patterns with newlines or `\xa0` from option
  descriptions never resolve. Click by index (`widgets.option_index()`).
- **The expense edit page has no Cancel/Close.** Backing out is navigation.
  Nothing persists until `Save Expense`.
- **`Edit` is a toolbar action**, disabled until a row checkbox is ticked.
  Checkbox index 0 is select-all → that opens the bulk-edit wizard, not a
  single-expense form.
- **Row identity is the `label.sapcnqr-checkbox` innerText.** The checkboxes
  have no `aria-label`.

## Documentation

Design, findings, and open questions live in
`~/canon/workbook/designs/expense-reports.md`. Session history and current
status: `~/canon/workbook/devnotes.md`. Task board: `~/canon/workbook/tasks.md`.

## License

Undecided. `duo-pass` is GPL-3.0-or-later and links propagate to anything
linked against it, which constrains the options here.
