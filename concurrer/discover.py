"""Read-only structural discovery of the Concur UI.

**Read-only by construction.** This module navigates and observes. It never
clicks anything that mutates state — no submit, no create, no delete, no
move. The only clicks it performs are on navigation affordances it has
explicitly whitelisted as safe (menu openers, links to read-only views).

Why it exists: the Expense Report shape must be read off the live tenant rather
than guessed. URLs, selectors, editable states, and validation messages all
have to be observed before any of it can be coded against.

Uses the persistent profile so the Duo device trust already granted is reused.

Usage:
    python -m concurrer.discover                # home + nav map
    python -m concurrer.discover --path /expense-requests
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from .login import authenticated_context
from .secrets import STATE_DIR, resolve

BASE = "https://us2.concursolutions.com"

#: Click targets considered safe: they reveal navigation, they do not change data.
#: Anything resembling submit/approve/delete/create is deliberately absent, and
#: `_is_safe_click` refuses matches on those words even if whitelisted.
SAFE_CLICK_PATTERNS = (
    r"^available\s+expenses$",
    r"^expense\s+reports?$",
    r"^trips$",
    r"^approvals$",
    r"^home$",
)

#: Words that mark a destructive or state-changing control. Never clicked.
UNSAFE_WORDS = (
    "submit", "approve", "reject", "delete", "remove", "create", "new",
    "send", "split", "merge", "assign", "save", "confirm", "pay",
)

OUT = STATE_DIR / "discover"


def _is_safe_click(text: str) -> bool:
    """Whitelisted pattern AND no destructive word."""
    lowered = text.strip().lower()
    if any(word in lowered for word in UNSAFE_WORDS):
        return False
    return any(re.match(p, lowered) for p in SAFE_CLICK_PATTERNS)


def dump_controls(page, label: str) -> list[dict]:
    """Visible, actionable elements with enough identity to target later."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('a,button,input,select,[role=button],[role=link],[role=menuitem]'))
            .filter(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4
                    && s.visibility !== 'hidden' && s.display !== 'none';
            })
            .map(el => ({
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                id: el.id || '',
                cls: (el.className || '').toString().slice(0, 80),
                href: (el.getAttribute('href') || '').slice(0, 120),
                name: el.name || '',
                text: (el.innerText || el.value || '').trim().slice(0, 70),
            }))"""
    )


def record(name: str, payload: dict) -> None:
    """Persist one observation as JSON so findings survive the session."""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2))
    print(f"  wrote {path}")


def explore(page, url: str, name: str) -> dict:
    """Navigate, settle, capture structure and a screenshot."""
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6000)  # SPA settle
    controls = dump_controls(page, name)
    payload = {
        "requested": url,
        "final_url": page.url,
        "title": page.title(),
        "controls": controls,
    }
    record(name, payload)
    try:
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        print(f"  screenshot failed: {exc}", file=sys.stderr)

    print(f"\n=== {name} ===")
    print(f"  final url: {page.url}")
    print(f"  title:     {page.title()}")
    print(f"  controls:  {len(controls)}")
    navish = [c for c in controls if c["text"] and len(c["text"]) < 45]
    for c in navish[:40]:
        print(f"    [{c['tag']}] {c['text']!r} href={c['href'][:70]}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Concur UI discovery.")
    ap.add_argument("--path", default="/home", help="path under the us2 tenant")
    ap.add_argument("--name", default=None, help="output name stem")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    name = args.name or re.sub(r"[^a-z0-9]+", "_", args.path).strip("_") or "home"

    creds = resolve()
    print(creds.report(), file=sys.stderr)

    # Authenticate and explore in the SAME browser: the Concur session cookie is
    # session-scoped, so a second process reusing the on-disk profile would get
    # the Duo trust but not the session, and bounce to sign-in.
    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        explore(page, f"{BASE}{args.path}", name)

        # Surface the safe nav targets we could follow next, but do NOT follow
        # them automatically — each follow is its own deliberate run so nothing
        # unexpected happens unattended.
        controls = dump_controls(page, "nav")
        candidates = [c for c in controls if _is_safe_click(c["text"])]
        print("\nsafe navigation candidates (run each explicitly):")
        for c in candidates:
            print(f"  {c['text']!r} -> {c['href'] or '(js-driven, click only)'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
