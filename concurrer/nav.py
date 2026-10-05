"""Navigation helpers for reaching a single expense's edit surface.

Discovered shape (2026-09-21):

    /nui/expense/reports/<REPORT_ID>                 report list
    /nui/expense/reports/<R>/expenses/<EXPENSE_ID>   single expense form

``Edit`` is a toolbar action, not a row link. Checkbox zero is the select-all
header: selecting it opens the bulk-edit wizard. Callers selecting one expense
must use a one-based row index, while persistent expense IDs are preferred for
any operation that might write.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .safety import click_safe
from .widgets import field_inventory

BASE = "https://us2.concursolutions.com"


def open_report(page, report_id: str, settle_ms: int | None = None) -> None:
    """Open a report page.

    With the default ``settle_ms=None`` this waits for real content via
    :func:`wait_for_report_ready` -- a fixed 8s settle measurably lands on an
    unloaded grid, which reads back as an empty report. An explicit value
    keeps a fixed settle for callers that only navigate away (e.g. abandoning
    an unsaved form), where raising on a slow page would be wrong.
    """
    page.goto(
        f"{BASE}/nui/expense/reports/{report_id}",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    if settle_ms is None:
        wait_for_report_ready(page)
    else:
        page.wait_for_timeout(settle_ms)


#: What "this report page is loaded" means, measured live on 2026-09-30.
#:
#: **The obvious predicate is wrong and was tried.** Waiting for the
#: ``busyIndicator`` / ``skeleton`` elements to disappear never succeeds:
#: ``#full-page-busyIndicator__wrapper`` is a 1440x948, opacity-1 container
#: that holds the *real* page content permanently. Those names describe where a
#: spinner would be mounted, not whether one is showing.
#:
#: The reliable signal is positive. Sampled on the same report:
#:
#:     t=0..8s   gridRows=0  toolbar=False  bodyTextLen=286   (shell only)
#:     t=15s     gridRows=2  toolbar=True   bodyTextLen=535   (loaded)
#:
#: Note what that meant for the old 8s default in ``open_report``: at that point
#: the grid has **zero rows**, so a reader gets an empty result that looks
#: like an empty report rather than an unloaded one. Waiting for content is
#: not paranoia; the failure it prevents is silent.
READY_JS = """() => ({
  ready: !!document.querySelector('[data-nuiexp="entries-toolbar"]')
      && (document.querySelectorAll('.sapcnqr-data-grid-list__row').length >= 1
          || !!document.querySelector('[data-nuiexp="empty-placeholder"]')),
  gridRows: document.querySelectorAll('.sapcnqr-data-grid-list__row').length,
  toolbar: !!document.querySelector('[data-nuiexp="entries-toolbar"]'),
  bodyTextLen: (document.body.innerText || '').trim().length,
})"""


def wait_for_report_ready(page, timeout_ms: int = 45_000) -> dict:
    """Wait until the report page holds real content rather than a shell.

    **Waits for a state, not a duration.** The fixed settle in ``open_report``
    is measurably too short at report level, and the failure mode is silent:
    zero rows read back looks identical to a report that has no expenses.

    Ready means the entries toolbar exists AND the grid has at least one row
    or an explicit empty placeholder. A report that never satisfies that
    raises rather than exporting nothing, because the caller cannot tell an
    empty report from a stuck one and guessing wrong ships a plausible
    looking wrong file.
    """
    polled = 0
    last: dict = {}
    while polled < timeout_ms:
        try:
            last = page.evaluate(READY_JS)
        except Exception as exc:  # noqa: BLE001 - mid-navigation, retry
            last = {"error": f"{type(exc).__name__}: {exc}"}
        if last.get("ready"):
            return last
        page.wait_for_timeout(500)
        polled += 500
    raise RuntimeError(
        f"report page never rendered content after {timeout_ms // 1000}s "
        f"(url={page.url}); last state: {last}. Refusing to read a page that "
        "may not be loaded — an empty read here is indistinguishable from an "
        "empty report."
    )


def _expense_id_in_url(url: str, report_id: str) -> str:
    """Return a single-expense ID only from the requested Concur report route."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "us2.concursolutions.com":
        raise RuntimeError(f"did not remain on the Concur tenant: {url!r}")
    match = re.match(
        r"^/nui/expense/reports/([0-9A-Fa-f]+)/expenses/([0-9A-Fa-f]+)(?:/|$)",
        parsed.path,
        flags=re.IGNORECASE,
    )
    if not match or match.group(1).casefold() != report_id.casefold():
        raise RuntimeError(
            "did not reach a single-expense edit route for "
            f"report {report_id!r}; ended at {url!r}"
        )
    return match.group(2)


EXPENSE_READY_JS_POLL_MS = 500
LATE_FILLED_FIELDS = ("paymentType",)
LATE_FILL_GRACE_MS = 6000


def wait_for_expense_ready(page, timeout_ms: int = 30_000) -> list[dict]:
    """Wait until an expense form has readable named fields, then return them.

    A fixed settle can both waste time and miss a slow form. This waits for
    the populated state the reader actually needs. It raises rather than
    returning an empty or unnamed inventory that would look like an expense
    with no fields.
    """
    polled = 0
    late_polled = 0
    while polled < timeout_ms:
        try:
            fields = field_inventory(page)
        except Exception:  # noqa: BLE001 - mid-navigation, retry
            fields = []
        if any(field.get("name") for field in fields):
            # Payment Type renders blank and fills ~2s after the other fields
            # (probed 2026-10-05: '' at 7.0s, 'Personal Funds' at 9.2s, no
            # click). Returning at first render made every export, dry-run
            # diff, and pre-save check see a required field as empty. Grace is
            # bounded; a value still blank afterwards is reported, not hidden.
            pending = [f for f in fields
                       if f.get("name") in LATE_FILLED_FIELDS
                       and not str(f.get("value", "")).strip()]
            if not pending or late_polled >= LATE_FILL_GRACE_MS:
                return fields
            late_polled += EXPENSE_READY_JS_POLL_MS
        page.wait_for_timeout(EXPENSE_READY_JS_POLL_MS)
        polled += EXPENSE_READY_JS_POLL_MS
    raise RuntimeError(
        f"expense form never rendered named fields after {timeout_ms // 1000}s "
        f"(url={page.url})"
    )


def open_expense_by_id(
    page,
    report_id: str,
    expense_id: str,
    settle_ms: int | None = None,
) -> str:
    """Open and verify a specific expense's form by its stable Concur ID.

    With the default ``settle_ms=None``, navigation waits for readable form
    fields instead of sleeping for a fixed interval. An explicit millisecond
    value preserves the legacy fixed settle for callers that need it.
    """
    if not re.fullmatch(r"[0-9A-Fa-f]+", expense_id or ""):
        raise ValueError(f"invalid expense ID {expense_id!r}")
    page.goto(
        f"{BASE}/nui/expense/reports/{report_id}/expenses/{expense_id}",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    if settle_ms is None:
        wait_for_expense_ready(page)
    else:
        page.wait_for_timeout(settle_ms)
    got = _expense_id_in_url(page.url, report_id)
    if got.lower() != expense_id.lower():
        raise RuntimeError(
            f"opened expense {got!r}, expected exact expense ID {expense_id!r}"
        )
    return got


def expense_row_number(data_row_index: int) -> int:
    """Translate a zero-based list data-row index past the checkbox header."""
    if data_row_index < 0:
        raise ValueError("data-row index must not be negative")
    return data_row_index + 1


def select_row(page, row: int) -> None:
    """Select a one-based data row, never the select-all header."""
    if row < 1:
        raise ValueError("row must be at least 1; row 0 is the select-all header")
    boxes = page.locator(".sapcnqr-checkbox__input")
    n = boxes.count()
    if n == 0:
        raise RuntimeError("no selectable rows on this report")
    if row >= n:
        raise RuntimeError(f"row {row} out of range: {n - 1} expense rows")
    click_safe(page, boxes.nth(row), "row checkbox")
    page.wait_for_timeout(2500)


def open_expense_edit(page, report_id: str, row: int) -> str:
    """Open a one-based report row's single-expense form and return its ID.

    Raises rather than returning an empty ID if the Edit action opened a bulk
    wizard or any other unexpected route.
    """
    open_report(page, report_id)
    select_row(page, row)

    edits = page.locator("button", has_text="Edit")
    if edits.count() == 0:
        raise RuntimeError("no Edit button after selecting a row")
    if not edits.first.is_enabled():
        raise RuntimeError("Edit still disabled after selecting a row")
    click_safe(page, edits.first, "Edit")
    page.wait_for_timeout(7000)
    return _expense_id_in_url(page.url, report_id)


#: Report-wide facts on the report main screen, by stable handle (captured
#: read-only 2026-10-01). The document title carries the bare report name
#: ("Expense - <name> - SAP Concur"); the on-page title appends the total.
HEADER_JS = """() => {
  const t = (sel) => {
    const el = document.querySelector(`[data-nuiexp="${sel}"]`);
    return el ? (el.innerText || '').trim().split('\\n').map(s => s.trim())
        .filter(Boolean).join(' — ') : '';
  };
  const m = document.title.match(/^Expense - (.*) - SAP Concur$/);
  return {
    'Report Name': m ? m[1] : '',
    'Report Number': t('erp-lnk-report-header-facet'),
    'Status': t('report-status'),
    'Total': t('report-entries-table-footer'),
    'Linked Requests': t('linked-request-add-on'),
  };
}"""


def report_header(page) -> dict[str, str]:
    """Read report-wide facts from an already-loaded report page (no clicks)."""
    return page.evaluate(HEADER_JS)
