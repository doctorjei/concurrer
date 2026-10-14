"""Attach a receipt file to one Concur expense.

Per-expense attachment only. The direct expense page carries a native
``<input type=file>`` (``id='upload-file'``), so Playwright assigns the file
with ``set_input_files()`` — no OS picker, no clicks on Upload/Save/Submit.

Default mode is a dry run that validates the local file and IDs without
touching credentials, a browser, or any grant::

    python -m concurrer.receipts --report <REPORT> --expense-id <EXPENSE> \\
        --file receipt.png

``--commit`` is structurally limited to the throwaway Test Report. The grant
is entered before credential resolution or browser launch, exactly as
``pushcsv`` does; a real report fails before login::

    python -m concurrer.receipts --report B857BA45DBC24EDB8420 \\
        --expense-id <TEST-EXPENSE> --file receipt.png --commit

Two facts shape this module:

1. ``set_input_files()`` is a server-side mutation that bypasses
   ``safety.click_safe()`` (which guards only clicks). The live URL is
   therefore re-authorized with ``assert_write_authorized()`` immediately
   before the single setter call, with nothing in between.

2. A returned setter call proves assignment, not persistence. Until a live
   discovery upload establishes the durable post-reopen receipt identity,
   outcomes are reported honestly (attempted / observed / verified) and the
   command never retries — a retry could duplicate an attachment it cannot
   see, and ``remove`` is permanently denied so cleanup is manual.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .banners import parse_banner_actions
from .login import authenticated_context
from .nav import open_expense_by_id
from .safety import (
    CONFIRMED_REPORT_IDS,
    assert_write_authorized,
    plan_token,
    test_report_write_grant,
    write_grant,
)
from .secrets import STATE_DIR, resolve

#: The one authorized upload control. The expense page carries *two* native
#: file inputs; the anonymous one has no stable identity and an unestablished
#: purpose. A generic ``input[type=file]`` locator could attach to the wrong
#: element, so only this exact ID is ever used.
UPLOAD_INPUT_SELECTOR = "#upload-file"

#: Observed ``accept`` contract on the live control (2026-09-30 probe).
#: A local guard, not server validation — Concur decides what it keeps.
ALLOWED_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf", ".tif", ".tiff")

#: First cut attaches exactly one file. The control accepts ``multiple``, but
#: multi-file server semantics (partial failure, ordering, duplicates) are
#: unproven; enabling it before one-file persistence is understood risks an
#: ambiguous partly-attached state with no undo.
MAX_FILES = 1


class ReceiptError(ValueError):
    """A receipt request cannot be safely planned or attached."""


@dataclass(frozen=True)
class ReceiptRequest:
    report_id: str
    expense_id: str
    source: Path


def validate_request(report_id: str, expense_id: str, source: Path) -> ReceiptRequest:
    """Validate everything knowable without a browser.

    IDs must already look like Concur route components; the file must exist,
    be a regular non-empty file, and carry an observed receipt suffix. The
    returned request keeps the full source path for the setter only — console
    and evidence output must use the privacy-safe summary instead.
    """
    import re

    if not re.fullmatch(r"[0-9A-Fa-f]+", report_id or ""):
        raise ReceiptError(f"invalid report ID {report_id!r}")
    if not re.fullmatch(r"[0-9A-Fa-f]+", expense_id or ""):
        raise ReceiptError(f"invalid expense ID {expense_id!r}")
    candidate = Path(source)
    if not candidate.exists():
        raise ReceiptError(f"no such file: {candidate}")
    if not candidate.is_file():
        raise ReceiptError(f"not a regular file: {candidate}")
    if candidate.stat().st_size == 0:
        raise ReceiptError(f"refusing empty file: {candidate}")
    if candidate.suffix.casefold() not in ALLOWED_SUFFIXES:
        raise ReceiptError(
            f"unsupported suffix {candidate.suffix!r}; expected one of "
            f"{', '.join(ALLOWED_SUFFIXES)}"
        )
    return ReceiptRequest(
        report_id=report_id, expense_id=expense_id, source=candidate
    )


def describe_request(request: ReceiptRequest) -> str:
    """Privacy-safe one-line summary: extension and size, never path or name."""
    size = request.source.stat().st_size
    return (
        f"report={request.report_id} expense={request.expense_id} "
        f"file=<receipt{request.source.suffix.casefold()} {size} bytes>"
    )


_READ_INPUT_JS = """(selector) => {
  const found = Array.from(document.querySelectorAll(selector));
  return {
    count: found.length,
    inputs: found.map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      multiple: !!el.multiple,
      accept: el.accept || null,
    })),
  };
}"""


def read_upload_input(page) -> dict:
    """Describe the live ``#upload-file`` control without touching it."""
    return page.evaluate(_READ_INPUT_JS, UPLOAD_INPUT_SELECTOR)


def validate_upload_input(info: dict) -> None:
    """Require exactly the known-good control; fail closed on any drift."""
    if info.get("count") != 1:
        raise ReceiptError(
            f"expected exactly one {UPLOAD_INPUT_SELECTOR}, found "
            f"{info.get('count')}; refusing to guess which input attaches"
        )
    (candidate,) = info.get("inputs", [])
    if candidate.get("tag") != "input" or candidate.get("type") != "file":
        raise ReceiptError(
            f"{UPLOAD_INPUT_SELECTOR} is no longer a file input: {candidate!r}"
        )
    if not candidate.get("multiple"):
        raise ReceiptError(
            f"{UPLOAD_INPUT_SELECTOR} lost its multiple capability; surface changed"
        )
    observed = {
        part.strip().casefold()
        for part in str(candidate.get("accept") or "").split(",")
        if part.strip()
    }
    expected = {suffix for suffix in ALLOWED_SUFFIXES}
    if observed != expected:
        raise ReceiptError(
            f"{UPLOAD_INPUT_SELECTOR} accept contract changed: {sorted(observed)!r}"
        )


def wait_for_upload_ready(page, timeout_ms: int = 20_000) -> dict:
    """Wait for the exact upload control plus receipt panel (positive wait).

    The expense page settles asynchronously; assigning files to a control
    that has not rendered yet raises or, worse, targets a stale element.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            last = read_upload_input(page)
            panel = page.evaluate(
                "() => !!document.querySelector('[data-nuiexp*=\"entry-receipt-panel\"]')"
            )
        except Exception:  # noqa: BLE001 - mid-navigation, retry
            page.wait_for_timeout(500)
            continue
        if last.get("count") == 1 and panel:
            validate_upload_input(last)
            return last
        page.wait_for_timeout(500)
    raise ReceiptError(
        f"upload surface not ready after {timeout_ms // 1000}s; "
        f"last input state: {last!r}"
    )


_RECEIPT_CHECK_JS = """(basename) => {
  const panel = document.querySelector('[data-nuiexp*="entry-receipt-panel"]');
  if (!panel) return {panel: false, nameVisible: false, itemCount: 0};
  const needles = basename.toLowerCase();
  let itemCount = 0;
  let nameVisible = false;
  const seen = new Set();
  for (const el of panel.querySelectorAll('a, img, [role="listitem"], li, [role="link"]')) {
    if (seen.has(el)) continue;
    seen.add(el);
    itemCount += 1;
    const hay = [
      el.innerText || '', el.getAttribute('alt') || '',
      el.getAttribute('title') || '', el.getAttribute('aria-label') || '',
    ].join(' ').toLowerCase();
    if (hay.includes(needles)) nameVisible = true;
  }
  const panelText = (panel.innerText || '').toLowerCase();
  if (panelText.includes(needles)) nameVisible = true;
  return {panel: true, nameVisible, itemCount};
}"""


def check_receipt_visible(page, basename: str) -> dict:
    """Whether the receipt filename is server-rendered in the receipt panel.

    The basename is passed into the page and never written to disk or logs;
    only booleans and counts come back. A match means Concur itself rendered
    the name — far stronger than ``input.files`` length, which proves only
    local assignment.
    """
    return page.evaluate(_RECEIPT_CHECK_JS, basename)


def snapshot_receipt_surface(page) -> dict:
    """Structural, filename-free snapshot of the receipt surface.

    Stores element counts, stable IDs, ``data-nuiexp`` tokens, and fingerprints
    of text — never raw text, paths, or basenames — so a before/after/reopen
    delta is detectable without persisting anything sensitive.
    """
    return page.evaluate(
        """() => {
      const fp = (s) => {
        let h1 = 0x811c9dc5, h2 = 0x811c9dc5;
        for (let i = 0; i < s.length; i++) {
          const c = s.charCodeAt(i);
          h1 = Math.imul(h1 ^ c, 2654435761);
          h2 = Math.imul(h2 ^ c, 1597334677);
        }
        return (h1 >>> 0).toString(16) + (h2 >>> 0).toString(16);
      };
      const panel = document.querySelector('[data-nuiexp*="entry-receipt-panel"]');
      const inputs = Array.from(document.querySelectorAll('#upload-file')).map((el) => ({
        type: el.type || null, multiple: !!el.multiple,
      }));
      if (!panel) return {panel: false, inputs, items: []};
      const items = [];
      for (const el of panel.querySelectorAll(
             'a, img, [role="listitem"], li, [role="link"], button')) {
        const nuis = Array.from(el.querySelectorAll('[data-nuiexp]'))
          .map((e) => e.getAttribute('data-nuiexp')).filter(Boolean);
        items.push({
          tag: el.tagName.toLowerCase(),
          id: el.id || null,
          nuiexp: nuis.slice(0, 4),
          textFp: fp((el.innerText || '').trim().toLowerCase()),
        });
      }
      return {panel: true, inputs, items};
    }"""
    )


def write_evidence(payload: dict) -> Path:
    """Write owner-only observation evidence under a non-semantic name."""
    out_dir = STATE_DIR / "receipts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    name = f"receipt-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.json"
    path = out_dir / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    path.chmod(0o600)
    return path


def _sanitize(exc: BaseException, source: Path) -> str:
    """Render an error without leaking the local file path it may echo."""
    text = str(exc).replace(str(source), "<receipt-file>")
    return f"{type(exc).__name__}: {text}"


def attach_receipt(page, request: ReceiptRequest) -> dict:
    """Perform the one guarded upload; exactly one setter call, no retry.

    Sequence: direct-ID open → readiness wait → pre snapshot → authorization
    immediately before the setter → bounded observation → reopen → post
    snapshot. Nothing is clicked: no Upload button, no Save, no Submit, no
    cleanup. Any exception after the setter marks persistence unknown rather
    than retrying, because a retry could duplicate an attachment ``remove``
    can never undo.
    """
    expense_id = open_expense_by_id(page, request.report_id, request.expense_id)
    wait_for_upload_ready(page)
    pre = snapshot_receipt_surface(page)
    pre_check = check_receipt_visible(page, request.source.name)
    if pre_check.get("nameVisible"):
        # Attachments are permanent and remove is denied: never risk a
        # duplicate of a file the expense already shows.
        raise ReceiptError(
            "this receipt filename is already shown on the expense; refusing "
            "a duplicate attachment (nothing assigned)")

    upload = page.locator(UPLOAD_INPUT_SELECTOR)
    if upload.count() != 1:
        raise ReceiptError(
            f"expected exactly one {UPLOAD_INPUT_SELECTOR} at attach time"
        )
    # The authorization MUST directly precede the setter: set_input_files is
    # not a click, so click_safe never sees it. No lookup, log, or wait may
    # intervene — a redirect between check and mutation would aim elsewhere.
    assert_write_authorized(page.url)
    try:
        upload.set_input_files(str(request.source))
    except Exception as exc:  # noqa: BLE001 - report, never retry
        raise ReceiptError(
            "upload failed during file assignment; persistence unknown — "
            f"inspect the expense before retrying ({_sanitize(exc, request.source)})"
        ) from exc

    assigned = page.evaluate(
        "(sel) => (document.querySelector(sel)?.files || []).length",
        UPLOAD_INPUT_SELECTOR,
    )

    # Bounded observation: server upload/processing is asynchronous, and its
    # signal is still being discovered. Poll for the rendered name, but treat
    # even a match here as unverified until the reopen confirms it.
    observed: dict = {"nameVisible": False, "itemCount": 0}
    deadline = time.monotonic() + 30_000 / 1000
    while time.monotonic() < deadline:
        observed = check_receipt_visible(page, request.source.name)
        if observed.get("nameVisible"):
            break
        page.wait_for_timeout(1000)

    reopened_id = open_expense_by_id(
        page, request.report_id, request.expense_id
    )
    # The receipt viewer loads after the form on reopen (CCED JR Central,
    # 2026-10-14: an immediate check missed a receipt that had persisted).
    post_check = check_receipt_visible(page, request.source.name)
    for _ in range(30):  # x 1 s
        if post_check.get("nameVisible"):
            break
        page.wait_for_timeout(1000)
        post_check = check_receipt_visible(page, request.source.name)
    post = snapshot_receipt_surface(page)

    if post_check.get("nameVisible"):
        outcome = "verified-persisted"
        detail = (
            "filename is server-rendered after direct-ID reopen; "
            "attachment persisted"
        )
    elif observed.get("nameVisible"):
        outcome = "observed-unverified"
        detail = (
            "filename appeared after assignment but is absent after reopen; "
            "persistence unverified — inspect manually, do not retry blindly"
        )
    else:
        outcome = "attempted-unverified"
        detail = (
            "file was assigned but the filename never rendered; persistence "
            "unverified — inspect manually, do not retry blindly"
        )
    return {
        "outcome": outcome,
        "detail": detail,
        "expense_id": reopened_id,
        "assignedCount": assigned,
        "preHadName": bool(pre_check.get("nameVisible")),
        "observedImmediate": bool(observed.get("nameVisible")),
        "postReopen": bool(post_check.get("nameVisible")),
        "pre": pre,
        "post": post,
    }


def receipt_token(request: ReceiptRequest) -> str:
    """Fingerprint of exactly this attachment: report, expense, file bytes."""
    digest = hashlib.sha256(request.source.read_bytes()).hexdigest()
    return plan_token(request.report_id,
                      [(request.expense_id.upper(), "receipt", digest, "")])


def _run(
    request: ReceiptRequest,
    *,
    commit: bool,
    banner_actions: Mapping[str, str],
) -> int:
    if not commit:
        print(f"RECEIPT PLAN (dry run) — {describe_request(request)}")
        if request.report_id.upper() in CONFIRMED_REPORT_IDS:
            token = receipt_token(request)
            print(f"  PLAN TOKEN: {token}  (real report; permanent attachment — "
                  f"to attach exactly this file: --commit --confirm {token})")
        print(
            "Dry run only — no credentials resolved, no browser opened, "
            "no write grant requested, no file assigned."
        )
        return 0
    creds = resolve()
    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=banner_actions,
    ) as page:
        result = attach_receipt(page, request)
        evidence = write_evidence(
            {
                "report_id": request.report_id,
                "expense_id": result["expense_id"],
                "outcome": result["outcome"],
                "assignedCount": result["assignedCount"],
                "preHadName": result["preHadName"],
                "observedImmediate": result["observedImmediate"],
                "postReopen": result["postReopen"],
                "pre": result["pre"],
                "post": result["post"],
                # Deliberately absent: source path, basename, raw filenames.
                "source": {
                    "suffix": request.source.suffix.casefold(),
                    "bytes": request.source.stat().st_size,
                    "sha256": hashlib.sha256(
                        request.source.read_bytes()
                    ).hexdigest(),
                },
            }
        )
        print(f"\nRECEIPT {result['outcome']}: {result['detail']}")
        print(f"evidence: {evidence} (owner-only; no filenames stored)")
        if result["outcome"] != "verified-persisted":
            print(
                "Persistence is NOT proven. Inspect the Test Report expense "
                "manually before any further attempt.",
                file=sys.stderr,
            )
            return 1
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Attach one receipt file to one Concur expense."
    )
    ap.add_argument("--report", required=True)
    ap.add_argument("--expense-id", required=True)
    ap.add_argument(
        "--file",
        dest="files",
        action="append",
        required=True,
        metavar="PATH",
        help="exactly one receipt file (single-file first cut)",
    )
    ap.add_argument(
        "--commit",
        action="store_true",
        help="attach on the Test Report, or on the confirmed real report "
        "with --confirm",
    )
    ap.add_argument(
        "--confirm",
        metavar="PLAN_TOKEN",
        help="with --commit on the confirmed real report: the dry run's PLAN TOKEN",
    )
    ap.add_argument(
        "--banner-action",
        action="append",
        default=[],
        metavar="BANNER_ID=EXACT_LABEL",
        help="one explicit response to a displayed sign-in banner; repeatable",
    )
    args = ap.parse_args()

    try:
        if len(args.files) != 1:
            raise ReceiptError(
                f"single-file first cut: got {len(args.files)} --file values; "
                "multi-file semantics are unproven and remove is denied, so "
                "one attachment per run"
            )
        request = validate_request(args.report, args.expense_id, Path(args.files[0]))
        banner_actions = parse_banner_actions(args.banner_action)
        if args.confirm and not args.commit:
            raise ReceiptError("--confirm is only meaningful with --commit")
        if args.commit:
            # Structural boundary before *everything* browser- or
            # credential-touching. No CLI value can widen it.
            if request.report_id.upper() in CONFIRMED_REPORT_IDS:
                with write_grant(request.report_id.upper(), reviewed_token=args.confirm,
                                 live_token=receipt_token(request)):
                    return _run(request, commit=True, banner_actions=banner_actions)
            with test_report_write_grant(request.report_id):
                return _run(request, commit=True, banner_actions=banner_actions)
        return _run(request, commit=False, banner_actions=banner_actions)
    except (ReceiptError, ValueError, AssertionError) as exc:
        print(f"receipts plan error: {_sanitize(exc, Path(args.files[0]) if args.files else Path())}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports safe failure
        source = Path(args.files[0]) if args.files else Path()
        print(f"receipts failed: {_sanitize(exc, source)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
