"""Shared safety guard for anything that clicks inside live Concur.

Every click in this codebase goes through `click_safe()`. Mutating actions are
unreachable by construction, not by convention.

Three tiers, because "never" and "not yet" are different requirements:

  NEVER      submit / approve / reject / recall / void / delete / duplicate /
             allocate / combine / remove / discard. No grant unlocks these.
             Submission is irreversible and moves a real person's money into
             an approval queue; the others destroy or redistribute.

  GRANTABLE  save. Permitted ONLY while a write grant is active AND the live
             browser URL belongs to the authorized report. Checked at click
             time, so a stray navigation revokes the grant.

  FREE       everything else — Edit, Details, View Alerts, row checkboxes.

Why the guard is this way. `Save Expense` is a **split button** (verified
2026-09-21):

    <div class="sapcnqr-split-button" role="group" data-toolbar-region="end">
      <button data-nuiexp="exp-save-expense" class="...split-button__action">
        <span class="sapcnqr-button__text">Save Expense</span></button>
      <button data-nui-widgets="menu-button-trigger" aria-haspopup="true"
              aria-label="More Save Options" .../></div>

Its menu holds one item, "Save and Add Another". There is also a live off-screen
twin, `exp-save-expense-hidden`, 1x1px at (-10001,-9949), not disabled.

The original guard matched whole labels, which is bypassable: "Save and Submit"
matches neither `save expense` nor `submit`. So:

  1. Dangerous verbs match as SUBSTRINGS. Over-blocking is the correct failure
     direction — a false refusal costs a retry, a false allow writes to a real
     expense.
  2. `data-nuiexp` is checked independently of the label, because it is the
     stable machine name and labels are presentation.
  3. One narrow carve-out: the `More Save Options` trigger legitimately says
     "Save" but only opens a menu. Allowed past the label check only with BOTH
     `data-nui-widgets="menu-button-trigger"` AND a truthy `aria-haspopup`.
"""

from __future__ import annotations

import re

# The only report any tooling may write to. Empty, named "Test Report".
TEST_REPORT_ID = "B857BA45DBC24EDB8420"

# Never permitted, under any grant.
NEVER_WORDS = (
    "submit",
    "approve",
    "reject",
    "recall",
    "void",
    "delete",
    "duplicate",
    "allocate",
    "combine",
    "remove",
    "discard",
)

# Permitted only under an active, URL-scoped write grant.
GRANTABLE_WORDS = ("save",)
GRANTABLE_NUIEXP = ("save",)

# `data-nuiexp` fragments that are never permitted regardless of label.
NEVER_NUIEXP = ("submit", "approve", "reject", "delete", "duplicate",
                "allocate", "combine")

# The only element allowed past a dangerous *label* without a grant: the trigger
# that opens the save-variants menu.
MENU_TRIGGER_WIDGET = "menu-button-trigger"

_authorized_report: str | None = None


def authorize_writes(report_id: str) -> None:
    """Grant write access, scoped to one throwaway report.

    The grant is checked against the live URL on every guarded click, so it does
    not survive navigating to another report. Writes are still limited to
    GRANTABLE words — this does not make submit reachable.
    """
    if report_id != TEST_REPORT_ID:
        raise AssertionError(
            f"refusing to authorize writes against {report_id!r}; only the "
            f"throwaway test report {TEST_REPORT_ID!r} may be written to"
        )
    global _authorized_report
    _authorized_report = report_id
    print(f"[safety] write grant ACTIVE — scoped to report {report_id}")


def revoke_writes() -> None:
    global _authorized_report
    _authorized_report = None


def grant_active() -> bool:
    return _authorized_report is not None


def report_in_url(url: str) -> str | None:
    m = re.search(r"/expense/reports/([0-9A-Fa-f]+)", url or "")
    return m.group(1) if m else None


def grant_covers(url: str) -> bool:
    return (
        _authorized_report is not None
        and report_in_url(url) == _authorized_report
    )


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def assert_safe(label: str, nuiexp: str = "", is_menu_trigger: bool = False,
                url: str = "") -> None:
    """Raise if the described target is not permitted right now."""
    lbl = _norm(label)
    exp = _norm(nuiexp)

    # Tier 1: never.
    for frag in NEVER_NUIEXP:
        if frag in exp:
            raise AssertionError(
                f"refusing: data-nuiexp {nuiexp!r} names a never-allowed action"
            )
    for w in NEVER_WORDS:
        if w in lbl:
            raise AssertionError(
                f"refusing: label {label!r} contains never-allowed verb {w!r}"
            )

    # Tier 2: grantable.
    wants_grant = any(w in lbl for w in GRANTABLE_WORDS) or any(
        f in exp for f in GRANTABLE_NUIEXP)
    if wants_grant:
        if is_menu_trigger:
            return  # opens the menu; does not act
        if grant_covers(url):
            return
        raise AssertionError(
            f"refusing: {label!r} writes, and the live URL {url!r} is not the "
            f"authorized test report"
        )


def click_safe(page, locator, label: str) -> None:
    """Click `locator` only if its label, attributes, and the live URL permit it."""
    el = locator.first
    try:
        nuiexp = el.get_attribute("data-nuiexp") or ""
        widgets = el.get_attribute("data-nui-widgets") or ""
        popup_raw = (el.get_attribute("aria-haspopup") or "").strip().lower()
        haspopup = popup_raw not in ("", "false")
        url = page.url if page is not None else ""
    except Exception:
        # Attribute read failed — fall back to label-only. Still a guard, just a
        # weaker one, and with no URL a write cannot be authorized.
        nuiexp = widgets = ""
        haspopup = False
        url = ""

    is_menu_trigger = MENU_TRIGGER_WIDGET in widgets and haspopup
    assert_safe(label, nuiexp=nuiexp, is_menu_trigger=is_menu_trigger, url=url)

    locator.click(timeout=15_000)
