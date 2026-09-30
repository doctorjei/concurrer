"""Navigation helpers for reaching a single expense's edit surface.

Discovered shape (2026-09-21):

    /nui/expense/reports/<REPORT_ID>              report list
    /nui/expense/reports/<R>/expenses/<EXPENSE_ID>  single-expense edit page

`Edit` is a TOOLBAR action, not a per-row link: it renders disabled until a row
checkbox is ticked. Checkbox index 0 is the select-all header — ticking it and
clicking Edit opens the bulk-edit wizard instead of a single-expense form.
Callers who want one expense pass row >= 1.

Ticking a checkbox is a UI selection, not a data mutation, so this whole path is
read-only.
"""

from __future__ import annotations

from .safety import click_safe

BASE = "https://us2.concursolutions.com"


def open_report(page, report_id: str, settle_ms: int = 8000) -> None:
    page.goto(f"{BASE}/nui/expense/reports/{report_id}",
              wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(settle_ms)


def select_row(page, row: int) -> None:
    boxes = page.locator(".sapcnqr-checkbox__input")
    n = boxes.count()
    if n == 0:
        raise RuntimeError("no selectable rows on this report")
    if row >= n:
        raise RuntimeError(f"row {row} out of range: only {n} checkboxes")
    click_safe(page, boxes.nth(row), "row checkbox")
    page.wait_for_timeout(2500)


def open_expense_edit(page, report_id: str, row: int) -> str:
    """Drive the report list to one expense's edit page. Returns the expense id.

    Raises if Edit never becomes enabled — never falls back to select-all, which
    would open the bulk wizard against the whole report.
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

    # /nui/expense/reports/<R>/expenses/<ID>
    url = page.url
    expense_id = ""
    if "/expenses/" in url:
        expense_id = url.split("/expenses/")[1].split("?")[0].split("/")[0]
    return expense_id
