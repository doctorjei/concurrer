"""Probe the Concur sign-in page: where it goes and what it asks for.

No credentials required. This exists because concursolutions.com is a SPA with
no server-side redirect chain to read (verified: `curl -L` stays on the root
URL), so the SSO hop is only observable in a real browser.

Reports: the URL chain, every visible input on the final page, and any
identity-provider markers. Writes a screenshot and the HTML to the state dir
for inspection.

Usage:
    python -m concurrer.probe [--url URL] [--wait MS]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from .secrets import STATE_DIR

DEFAULT_URL = "https://www.concursolutions.com/"

#: Host fragments that mean "we have left Concur and reached an IdP".
IDP_MARKERS = {
    "duosecurity.com": "Duo (second factor)",
    "onelogin.com": "OneLogin / UF Shibboleth",
    "shibboleth": "Shibboleth",
    "login.uf.edu": "UF login",
    "login.microsoftonline.com": "Entra ID",
    "auth0.com": "Auth0",
    "okta": "Okta",
}


def describe_inputs(page) -> list[str]:
    """Every input a human could actually see and type into."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('input,select,button'))
            .filter(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4
                    && s.visibility !== 'hidden' && s.display !== 'none'
                    && el.type !== 'hidden';
            })
            .map(el => [
                el.tagName.toLowerCase(),
                el.type || '',
                `name=${el.name || '-'}`,
                `id=${el.id || '-'}`,
                `placeholder=${JSON.stringify(el.placeholder || '')}`,
                `label=${JSON.stringify(
                    (el.labels && el.labels[0] && el.labels[0].innerText || '').trim().slice(0, 60)
                )}`,
                `text=${JSON.stringify((el.innerText || '').trim().slice(0, 60))}`,
            ].join(' '))"""
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe the Concur sign-in page.")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--wait", type=int, default=8000, help="ms to let the SPA settle")
    ap.add_argument("--headed", action="store_true", help="show the browser (needs a display)")
    args = ap.parse_args()

    out = STATE_DIR / "probe"
    out.mkdir(parents=True, exist_ok=True)

    chain: list[str] = []

    with sync_playwright() as p:
        # Ephemeral context on purpose: a probe should not inherit or leave
        # behind session state. The real driver uses a persistent profile.
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # Registered before goto: a listener added afterwards misses every
        # navigation that already happened, which is the whole point of the probe.
        page.on("framenavigated", lambda f: chain.append(f.url) if f is page.main_frame else None)

        print(f"goto {args.url}")
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:  # noqa: BLE001 - probe reports, does not fail hard
            print(f"  goto raised: {type(exc).__name__}: {exc}", file=sys.stderr)

        page.wait_for_timeout(args.wait)


        final = page.url
        print(f"\nfinal url: {final}")
        print(f"title:     {page.title()}")

        print("\nnavigation chain:")
        for i, url in enumerate(dict.fromkeys(chain)):  # de-dupe, keep order
            print(f"  {i}. {url}")

        hits = [
            f"  {frag:<32} {name}"
            for frag, name in IDP_MARKERS.items()
            if frag in final.lower()
        ]
        print("\nIdP markers on final URL:")
        print("\n".join(hits) if hits else "  (none — still on Concur)")

        inputs = describe_inputs(page)
        print(f"\nvisible controls ({len(inputs)}):")
        for line in inputs:
            print(f"  {line}")

        shot = out / "landing.png"
        page.screenshot(path=str(shot), full_page=True)
        (out / "landing.html").write_text(page.content())
        print(f"\nwrote {shot}")
        print(f"wrote {out / 'landing.html'}  ({len(page.content())} bytes)")

        browser.close()

    print(
        "\nNext: identify the Concur->UF SSO entry point from the controls above, "
        "then build the credential flow."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
