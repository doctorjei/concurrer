"""Shared safety guard for anything that changes live Concur.

Three tiers:

  NEVER      submit / approve / reject / recall / void / delete / duplicate /
             allocate / combine / remove / discard. No grant unlocks these.
  GRANTABLE  save and field edits. Permitted only for the Test Report while the
             live browser URL is still its Concur expense route.
  FREE       everything else — Edit, Details, View Alerts, row checkboxes.

The write boundary is intentionally structural. ``authorize_writes()`` refuses
any report other than ``TEST_REPORT_ID``; no flag, environment variable, or
caller's label can widen it.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from urllib.parse import urlparse

# The only report any tooling may write to. Empty, named "Test Report".
TEST_REPORT_ID = "B857BA45DBC24EDB8420"
CONCUR_ORIGIN = "https://us2.concursolutions.com"

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

# Machine names receive the same permanent deny treatment as visible labels.
NEVER_NUIEXP = NEVER_WORDS

# The only element allowed past a save-bearing *label* without a grant: the
# trigger that merely opens the Save variants menu. Its data-nuiexp must not
# name Save itself, or it is still a grantable control.
MENU_TRIGGER_WIDGET = "menu-button-trigger"

_authorized_report: str | None = None


def authorize_writes(report_id: str) -> None:
    """Grant Test Report write access for the current process.

    The grant is checked against the live URL at every guarded write. It never
    enables actions in the permanent deny tier.
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
    """Remove the process-local write grant."""
    global _authorized_report
    _authorized_report = None


@contextmanager
def test_report_write_grant(report_id: str):
    """Authorize the Test Report for one operation and always revoke afterward."""
    authorize_writes(report_id)
    try:
        yield
    finally:
        revoke_writes()


def grant_active() -> bool:
    return _authorized_report is not None


def report_in_url(url: str) -> str | None:
    """Extract a report ID only from an anchored Concur expense-report path.

    Query parameters intentionally do not affect the report identity: the valid
    create form is the report path plus ``?modal=...``.
    """
    path = urlparse(url or "").path
    match = re.match(
        r"^/nui/expense/reports/([0-9A-Fa-f]+)(?:/|$)",
        path,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _is_concur_origin(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() == "us2.concursolutions.com"


def grant_covers(url: str) -> bool:
    """Whether the active grant covers this exact live Concur report URL."""
    report = report_in_url(url)
    return (
        _authorized_report is not None
        and _is_concur_origin(url)
        and report is not None
        and report.casefold() == _authorized_report.casefold()
    )


def assert_write_authorized(url: str) -> None:
    """Require the Test Report grant before a non-click field mutation."""
    if not grant_covers(url):
        raise AssertionError(
            f"refusing field edit: live URL {url!r} is not the authorized "
            "Test Report"
        )


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def assert_safe(
    label: str,
    nuiexp: str = "",
    is_menu_trigger: bool = False,
    url: str = "",
) -> None:
    """Raise if the described click target is not permitted right now."""
    lbl = _norm(label)
    exp = _norm(nuiexp)

    # Tier 1: never. Both human and machine identities must be safe.
    for frag in NEVER_NUIEXP:
        if frag in exp:
            raise AssertionError(
                f"refusing: data-nuiexp {nuiexp!r} names a never-allowed action"
            )
    for word in NEVER_WORDS:
        if word in lbl:
            raise AssertionError(
                f"refusing: label {label!r} contains never-allowed verb {word!r}"
            )

    # Tier 2: grantable. The More Save Options trigger is harmless only when
    # its *label* says Save; a machine name containing save still needs a grant.
    label_wants_grant = any(word in lbl for word in GRANTABLE_WORDS)
    exp_wants_grant = any(frag in exp for frag in GRANTABLE_NUIEXP)
    if label_wants_grant or exp_wants_grant:
        if label_wants_grant and not exp_wants_grant and is_menu_trigger:
            return
        assert_write_authorized(url)


def click_safe(page, locator, label: str) -> None:
    """Click ``locator`` only if its attributes and live URL permit it.

    The caller label supplies useful intent, while ``data-nuiexp`` independently
    protects controls whose visible text is missing or misleading.
    """
    el = locator.first
    try:
        nuiexp = el.get_attribute("data-nuiexp") or ""
        widgets = el.get_attribute("data-nui-widgets") or ""
        popup_raw = (el.get_attribute("aria-haspopup") or "").strip().lower()
        haspopup = popup_raw not in ("", "false")
        visible_text = el.inner_text() or ""
        accessible_name = el.get_attribute("aria-label") or ""
        labelledby_text = el.evaluate(
            """node => (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
                .filter(Boolean).map(id => document.getElementById(id)?.innerText || '')
                .join(' ')"""
        ) or ""
        title = el.get_attribute("title") or ""
        url = page.url if page is not None else ""
    except Exception as exc:
        # A guard that cannot inspect its target must never click it. In
        # particular, do not discard a partially read data-nuiexp and continue.
        raise RuntimeError("refusing click: unable to inspect target safely") from exc

    # Caller intent is useful, but a caller cannot describe a destructive target
    # as harmless: visible text and all accessible-name sources are evaluated
    # independently too.
    effective_label = " ".join(
        (label, visible_text, accessible_name, labelledby_text, title)
    )
    is_menu_trigger = MENU_TRIGGER_WIDGET in widgets and haspopup
    assert_safe(
        effective_label,
        nuiexp=nuiexp,
        is_menu_trigger=is_menu_trigger,
        url=url,
    )
    locator.click(timeout=15_000)
