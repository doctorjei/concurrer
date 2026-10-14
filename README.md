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

**If the repository or `.venv` has been moved:** recreate the virtualenv before
running setup. Virtualenv console scripts embed an absolute Python path. As of
2026-09-30, the checked-in workspace's `.venv/bin/pip` still points to the old
`~/workspace/concurrer/.venv/bin/python3` location and cannot run; this is
tracked as immediate repair work in `~/canon/workbook/tasks.md`.

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

Use `PushApprover` by default. This box has no controlling terminal, so a
plain `PasscodeApprover` cannot be used here — it calls `input()`. The rebuilt
environment currently has `duo-pass` 0.0.2, whose injectable passcode source is
available but has not been integrated or behaviorally tested by concurrer.

### Blocking banners

Login inspects supported blocking banners and stops with their visible text and
choices by default; it never silently accepts consent or privacy preferences.
After reviewing a notice, provide one explicit response for that invocation:

```bash
.venv/bin/python -m concurrer.edit ... --banner-action trustarc=Understood
```

Responses must match both a known banner ID and its current visible action text.
For the observed TrustArc notice, the choices are `Understood`, `Manage
Settings`, and `Cookie Statement`. Choosing `Manage Settings` exposes the next
state for review rather than making any further preference selection.

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
| `safety.py` | Three-tier click guard; Test-Report + Concur-origin-scoped write grant |
| `nav.py` | Open one expense by one-based row or stable ID; bulk-wizard rejected |
| `widgets.py` | Field inventory + strict write helpers (`select_combobox_exact`, `select_search_combobox_exact` for Location, `select_payment_type_exact`, `fill_text_exact`) |
| `type_map.py` / `type_map.json` | Versioned 67-type custom-field schema + loss/compat/conditional helpers |
| `type_form_map.py` / `type_form_map.json` | Full 67-type manual-form schema (standard + custom field presence) for the type-aware workbook |
| `standard_options.json` | Exact editable-form lists for Payment Type and Currency |
| `banners.py` | Blocking-banner inspection; presents by default, acts only on an explicit `--banner-action` |
| `fields.py` | Field inventory + option reader |
| `newexp.py` | Create path: `--list-types` / `--type` / `--save` |
| `cascade.py` | Tests the `expenseType` → custom-fields cascade |
| `typesweep.py` | Maps all 67 expense types to their custom fields |
| `triage.py` | Per-expense: which required fields are empty |
| `classify.py` | Read-only vendor/amount/detail table for expenses of a given type (especially `Undefined`) |
| `edit.py` | Single-expense edit planner; persistence is structurally Test Report-only |
| `csvio.py` | Browser-free CSV schema, identity validation, and fresh-live diff engine |
| `xlsxio.py` | Browser-free operator workbook: Expenses / Meals / Report Details / Backend Data, validation, cell-state colors |
| `type_options.json` | Captured dropdown option lists per expense type (Test Report sweep 2026-10-02) |
| `expexp.py` | Read-only full-report export (CSV **or** `.xlsx`) with re-read fidelity verification |
| `probereport.py` | Read-only report-header + per-type option probe |
| `pushcsv.py` | CSV dry-run / Test-Report-only batch driver over `edit.py`'s save path |
| `pushxlsx.py` | Workbook dry-run / Test-Report-only batch driver; routes columns to fields by each row's expense type |
| `probereceipts.py` | Read-only receipt/upload surface probe |
| `receipts.py` | Per-expense receipt attach; dry-run default, Test-Report-only `--commit` |
| `savebtn.py` | Dumps the Save split-button; `--menu` reads its variants |
| `discover.py` | Read-only explorer; destructive labels blocked |
| `mapform.py` / `mapedit.py` / `maptab.py` | Form mapping, read-only |
| `probe.py` | Page structure probe (no credentials needed) |

## Running

```bash
.venv/bin/python -m concurrer.login                       # verify sign-in
.venv/bin/python -m concurrer.fields --report <ID> --row 1
.venv/bin/python -m concurrer.triage --report <ID> --detail --max 5
.venv/bin/python -m concurrer.classify --report <ID> --match Undefined
.venv/bin/python -m concurrer.edit --report <ID> --expense-id <EXPENSE_ID> --set custom20="Taxi"
.venv/bin/python -m concurrer.edit --report B857BA45DBC24EDB8420 --expense-id <TEST_EXPENSE_ID> --set comment="Test marker" --save
.venv/bin/python -m concurrer.newexp --type "Ground Transportation" --save   # TEST REPORT ONLY

The `classify` command emits a Markdown table with vendor, amount, payment
method, and a blank "Expense type" column. It is read-only; use it to give the
operator enough context to resolve `Undefined` rows before building an edit.

`edit` is **plan-only by default**: it reads a fresh form, validates every
requested stable field name/value pair, and prints the proposed changes without
filling a field or granting write access. `--save` requires a stable
`--expense-id` and is structurally limited to the throwaway Test Report
`B857BA45DBC24EDB8420`; it cannot save a real report.

An expense-type change requires `--type`, `--expect-type`, and
`--allow-type-change`. The plan reports values Concur would discard. It only
restores customs whose slot **and label** remain identical—never merely matching
`customNN` slots—and refuses to save if Concur's live schema differs from the
versioned 67-type map.

### CSV round trip

Export is read-only and may target any readable report. It reads each expense's
live form, writes one CSV row per stable expense ID, then re-reads the written
file and requires a zero-change live diff before calling the export faithful:

```bash
.venv/bin/python -m concurrer.expexp --report <REPORT_ID> --out expenses.csv
.venv/bin/python -m concurrer.pushcsv --csv expenses.csv        # fresh-live dry run
.venv/bin/python -m concurrer.pushcsv --csv test-report.csv --commit
```

`expense_id` and `report_id` are mandatory **identity columns**, not editable
fields. Every CSV row must name one same hexadecimal `report_id`; the command
never accepts a target report on the CLI or falls back to a row number. Blank
editable cells mean **no change**, never clear. A changed locked/off-form field
or an `expenseType` change blocks the batch; type changes stay one-at-a-time in
`edit.py` because Concur can silently discard type-specific values.

`pushcsv` reopens every stable expense ID and plans against **fresh live state**,
not the export-time `.live.json` evidence. Its default is read-only and prints
every before/after change plus every refusal. `--commit` is structurally
Test-Report-only (`B857BA45DBC24EDB8420`) and fails before credentials or a
browser for any other report. Exported CSVs and their `.live.json` evidence are
written at mode **600**: they can contain vendor, location, amount, request,
and comment data.

A commit needs a completely clean preflight: one invalid row means no rows are
silently skipped or written. Each row is re-read and must still match the
reviewed plan immediately before Save. Concur has no batch transaction: if a
later row fails, the command stops and names earlier rows that are already
**saved and verified**; it never tries a fictional rollback.

### xlsx workbook export (operator-facing)

`expexp` writes an `.xlsx` workbook instead of a CSV when the output path ends
in `.xlsx`. It is still **read-only** and still verifies by re-reading what it
wrote against the live values it was written from.

```bash
.venv/bin/python -m concurrer.expexp --report <REPORT_ID> --out report.xlsx
```

Sheets, in order:

| Sheet | Contents |
|---|---|
| `Expenses` | One row per expense, UF column order. Identity is the last column (`Expense ID`). |
| `Meals & Per Diem` | **READ-ONLY (Do not edit).** Per-diem rows (Travel Meals, All Inclusive Per Diem, Per Diem/Meal Adjustment) live here, with their Departure/Return Time and meal-count fields shown for the first time. Never read by write-back. |
| `Report Details` | **READ ONLY.** Report-wide facts including `Report ID`, which write-back should take from here. |
| `Backend Data` | **READ ONLY.** (expense type, column) map with 67-type manual-form positives plus live-report facts, required flags, option lists, and evidence source. |
| `Lists`, `Variants`, `Orig` | Hidden. Dropdown sources, per-type Variant lists, and export-time values backing the blocked-cell check. |

Columns are type-routed merges: `custom20` becomes one `Variant` column, the
two `custom29` questions become `Shared/Accompanied?`, day/night counts become
`Days/Nights`. `vendor` merges Concur's locked `vendorName` with the unlocked
Airfare vendor-directory field. Per-diem fields are excluded from `Expenses`.

Cell states are live conditional formatting, so they follow edits and Expense
Type changes:

| State | Appearance |
|---|---|
| Required, filled appropriately | light green |
| Required, empty | pink |
| Invalid — value where prohibited, off-list, non-whole, or a blocked cell that changed | bright red |
| Optional, filled or not | very light grey |
| Unverified — the type's blank form lacks this field; an existing value may be dropped, a new one is refused | yellow |
| Field absent from this row's type | grey (red if it holds a value) |
| Blocked by Concur | black background, bold white type |

**Known limitations:** Travel Allowance / per diem is out of scope (the Meals
sheet displays generated rows; it never creates, regenerates or edits them).
All fields from the 67-type manual-form sweep are present for type-aware
editing, but the **batch type-change writer is still under construction**. A
field whose target applicability depends on card provenance or report linkage
is deliberately neutral until the live rehearsal resolves it. A column whose
options differ per type (e.g. Departure Time) gets no fixed dropdown.
LibreOffice cannot resize or format on a protected sheet; Excel can.

#### Writing back from the workbook

`pushxlsx` applies workbook edits to Concur. It is the same batch driver as
`pushcsv` — same preflight, same re-diff before Save, same honest per-row
outcomes, no rollback — with a different reader:

```bash
.venv/bin/python -m concurrer.pushxlsx --xlsx report.xlsx           # fresh-live dry run
.venv/bin/python -m concurrer.pushxlsx --xlsx test-report.xlsx --commit
```

The report ID is read from **Report Details**, never from a command-line
option, so no argument can aim a write at a report the workbook never named.
`--commit` is Test-Report-only and fails before credentials or a browser for
any other report.

The workbook's columns are operator-facing headers, but Concur's are field
names, and some columns are **merged**: `Days/Nights` is `custom28` on some
expense types and `custom24` on others, and `custom28` itself carries two
different meanings. So every column is routed **per row, by that row's own
expense type**, using the Backend Data sheet — the same (type, column) table
the cell colours look up, which keeps the colours and the writer from drifting.

Before `pushxlsx` routes any rows, requests credentials, or opens a browser,
it mirrors the workbook's state rules. Any bright-red invalid cell, or a
required-empty pink cell on an edited row, refuses the **whole batch** and
names each expense/row/column locally. Pink means "must fill this in"; red
means correct or clear it before the batch can start.

Three situations are refused rather than guessed:

- a non-blank merged cell with no route for that row's type (re-export so the
  column matches);
- a column Backend Data maps to two different fields for one type;
- two columns that collapse onto one Concur field with disagreeing values.

A *blank* merged cell on a type it does not apply to is simply skipped — no
change was intended. Per-diem rows are invisible here by construction: they
live on the read-only Meals sheet, which the reader never opens.

**Not supported:** bulk `expenseType` changes. `csvio` refuses them and points
at `edit.py`, because a type change irreversibly drops type-specific custom
values and the per-row routing is itself keyed on that type. Submit, and
everything else in the permanent deny tier, is untouched.

### Receipts

Attaches one local receipt file to one expense, by stable expense ID. The
expense page's native `#upload-file` input is driven directly — no OS picker,
no clicks on Upload/Save/Submit:

```bash
.venv/bin/python -m concurrer.receipts --report <REPORT_ID> --expense-id <EXPENSE_ID> --file receipt.png
.venv/bin/python -m concurrer.receipts --report B857BA45DBC24EDB8420 --expense-id <TEST_EXPENSE_ID> --file receipt.png --commit
```

The default is a dry run that validates the file and IDs locally with **no
browser and no grant**. `--commit` is structurally Test-Report-only and fails
before credentials or a browser for any other report. Accepted suffixes are
`.png .jpg .jpeg .pdf .tif .tiff` (the live control's contract); exactly one
`--file` per run — multi-file semantics are unproven and `remove` is denied, so
there is no undo for a partly-attached batch.

File assignment is a mutation that bypasses the click guard, so the live URL is
re-authorized immediately before the single `set_input_files()` call. A
returned call proves assignment, not persistence: the command polls for the
filename Concur itself renders, reopens the expense by stable ID, and reports
`verified-persisted`, `observed-unverified`, or `attempted-unverified`
honestly. It never retries — a blind retry could duplicate an attachment — and
never attempts cleanup. Observation evidence lands owner-only under
`~/.config/concur/receipts/` with no file paths, basenames, or raw page text;
console output likewise reports only suffix and byte size.

.venv/bin/python -m concurrer.typesweep
```

Evidence lands in `~/.config/concur/discover/` as JSON. **Screenshots from
`/nui/` pages render blank** (the SPA layout defeats full-page capture) — the
JSON dumps are the reliable record, not the PNGs.

## Tests

Browser-free, standard library only — no `pytest`, no network, no browser:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Expect `OK`. The run **prints `[safety] write grant ACTIVE` and `BLOCKED` lines
to stdout as part of normal operation** — those are the guard's own output
being exercised, not failures. The result line goes to **stderr**:

```bash
.venv/bin/python -m unittest discover -s tests -q 2>&1 >/dev/null | grep -E '^(OK|FAILED|Ran )'
```

## Things that will bite you

- **`ws.cell(row, col, value=None)` is a silent no-op in openpyxl.** It only
  assigns when the value is not `None`, so any code that rewrites cells can
  leave stale values behind and desynchronise headers from data. Assign
  through the `.value` property instead.
- **Chromium's system libraries live in the container rootfs and are wiped by a
  rebuild.** Re-run `sudo .venv/bin/playwright install-deps chromium`; the venv
  and browsers under `$HOME` survive.
- **Duo: do not block the foreground on a full run.** When device trust has
  lapsed, a run waits minutes for the phone tap. Run the command in the
  background and tail its log for `Duo number: NNN`, then surface the number
  immediately — the number appears early, long before the run finishes.
- **A blocking banner is an operator choice, not a nuisance.** Login presents
  the visible text/choices and stops (`BannerBlocked`) rather than dismissing.
  Pass `--banner-action trustarc=Understood` only after the operator has read
  the notice; the live label is re-checked at click time.
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

Undecided. `duo-pass` is GPL-3.0-or-later; determine the resulting licensing
and distribution obligations before redistributing concurrer.
