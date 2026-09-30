"""Drive the Concur sign-in: Concur -> UF Shibboleth -> Duo -> Concur.

Three hops, each with a different owner:

    1. Concur identity-first form   #username-input + #btnSubmit   (ours)
    2. UF Shibboleth               j_username / j_password         (ours)
    3. Duo second factor           duo_pass.complete_duo           (duo-pass)

Uses a **persistent browser profile** (`~/.config/concur/browser`) rather than an
ephemeral context. That is deliberate: Duo's "remember this device" trust is a
cookie, and only a profile that survives between runs can cash it. With trust
granted, later runs never reach the second factor at all, which is the difference
between an unattended job and one that needs a phone every time.

The password is read from a secrets file and never written to stdout, a log, or a
screenshot. Snapshots fire only on failure, into the state dir.

Usage:
    python -m concurrer.login [--headed] [--no-persist] [--timeout MS]
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

from duo_pass import PushApprover, complete_duo, configure, on_duo_page
from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from .secrets import STATE_DIR, resolve

#: The **tenant** URL. `www.concursolutions.com` shows a bare identity field that
#: never resolves to a provider; `us2` is the tenant that carries the UF SSO
#: configuration. Probing the wrong host was one of the two reasons the first
#: attempt failed.
CONCUR_URL = "https://us2.concursolutions.com/"

# Hop 1: Concur's identity-first form (confirmed by probe against us2).
CONCUR_USER_FIELD = "#username-input"
CONCUR_SUBMIT = "#btnSubmit"

#: Hop 1b: after the email, Concur shows a **provider picker** at
#: /nui/signin/hrd offering "University of Florida SSO" and "SAP Concur
#: Password". The first attempt missed this step entirely and then waited for
#: Shibboleth fields that only appear *after* the button is clicked.
#: Matched by accessible name, not the generated class hash
#: (`sapcnqr-button--default-xxxx`), which is rebuilt per deploy and churns.
SSO_PROVIDER_LABEL = "University of Florida SSO"

# Hop 2: UF's Shibboleth single-screen form. Same JAAS names canvasser proved
# against this IdP: `j_username`/`j_password` are the Shibboleth/JAAS standard,
# not Canvas-specific, so they carry over to a different service provider.
SHIB_USER_FIELD = "input[name='j_username']"
SHIB_PASS_FIELD = "input[name='j_password']"

#: Concur's identity-first field reads "Username, verified email address, or SSO
#: code" — and what it actually needs is something that maps the account to a
#: **company**, so it knows which IdP to bounce you to. A bare GatorLink id does
#: not carry a domain and may not resolve. UF's Shibboleth, by contrast, wants the
#: bare id and would likely choke on the email. So the two hops need different
#: spellings of the same principal, and the stored `UFL_USERNAME` is the bare one.
UF_EMAIL_DOMAIN = "ufl.edu"


def concur_identity(username: str) -> str:
    """Full email for Concur's company lookup; bare id stays for Shibboleth."""
    return username if "@" in username else f"{username}@{UF_EMAIL_DOMAIN}"


#: URL fragments that mean "Concur is still asking who you are". Note `/signin`
#: is NOT caught by a "login" substring test — that is exactly how the first
#: version of this driver reported a successful login while sitting on
#: /nui/signin. Match the real paths, not a guess at the wording.
SIGNIN_MARKERS = ("/signin", "/sign-in", "/login")


class LoginError(RuntimeError):
    """Reached the end of the flow without an authenticated session."""


def _wait_settled(page, timeout_ms: int = 15_000) -> None:
    """Let an in-flight navigation finish before touching the DOM.

    Concur chains redirects (`/goto/...` -> real target), and a DOM query issued
    mid-flight raises "Execution context was destroyed, most likely because of a
    navigation". Never idle-waiting forever: a page that keeps polling is treated
    as settled enough, and the caller works with what it has.
    """
    for state in ("domcontentloaded", "load"):
        try:
            page.wait_for_load_state(state, timeout=timeout_ms)
        except PlaywrightTimeout:
            pass


def is_signed_in(page, retries: int = 3) -> bool:
    """Whether the browser actually holds an authenticated Concur session.

    Three independent negatives, because a single check proved too weak:
    a signin path in the URL, still parked on Duo, or the identity field
    present in the DOM (meaning Concur is still asking).

    Retries on a destroyed execution context. Concur redirects asynchronously,
    and the first version of this function crashed outright when a redirect
    landed between the URL read and the DOM query.
    """
    for attempt in range(retries):
        try:
            url = page.url.lower()
            if any(marker in url for marker in SIGNIN_MARKERS):
                return False
            if on_duo_page(page):
                return False
            if page.query_selector(CONCUR_USER_FIELD):
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            if "Execution context was destroyed" not in str(exc):
                raise
            if attempt == retries - 1:
                raise
            _wait_settled(page, timeout_ms=5_000)
    return False


def _snapshot(page, label: str) -> Path:
    """Failure evidence, into our state dir — not the library default of ./snapshots."""
    out = STATE_DIR / "snapshots"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort, never fatal
        return Path(f"(screenshot failed: {exc})")
    path.chmod(0o600)
    return path


def _announce(number) -> None:
    """Verified Push number, loud and on stderr.

    Surfaced even when None: a tenant with Verified Push off still needs the
    human to go tap Approve, and a silent screen reads like a hang.
    """
    if number:
        print(
            f"\n  +{'=' * 38}+\n"
            f"  |  Duo number:  {number:^22}  |\n"
            f"  |  Tap THIS number on your phone.        |\n"
            f"  +{'=' * 38}+\n",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            "\n  >> Duo push sent (no number shown - tap Approve on your phone).\n"
            "     If you did NOT expect this, DENY it.\n",
            file=sys.stderr,
            flush=True,
        )


def sign_in_page(
    page,
    *,
    concur_username: str,
    ufl_username: str,
    ufl_password: str,
    timeout_ms: int = 45_000,
    duo_timeout_ms: int = 300_000,
) -> str:
    """Drive the hops on an **already-open** page and return the post-login URL.

    Split out from `login()` because Concur's session cookie is session-scoped:
    it dies when the browser closes. Anything that wants to *use* the session —
    discovery, the eventual expense-report work — must do so in the same browser
    instance that authenticated, against the same context. A separate process
    that reuses the on-disk profile gets the Duo device trust but not the
    session, and bounces to sign-in.
    """
    # A persistent profile may already hold a live Concur session, or a
    # stale Duo trust that lands us past the form. Check before filling.
    page.goto(CONCUR_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3000)
    if is_signed_in(page):
        print(f"already signed in -> {page.url}", file=sys.stderr)
        return page.url

    # --- Hop 1: Concur identity-first (email, not the bare UF id) ---
    print(f"hop 1: Concur identity form -> {concur_username}", file=sys.stderr)
    page.fill(CONCUR_USER_FIELD, concur_username, timeout=timeout_ms)
    page.click(CONCUR_SUBMIT, timeout=timeout_ms)
    page.wait_for_timeout(3000)

    # --- Hop 1b: provider picker at /nui/signin/hrd ---
    # Not optional. Concur will sit on this picker indefinitely; the
    # Shibboleth form does not exist until the provider is chosen.
    provider = page.get_by_role("button", name=SSO_PROVIDER_LABEL)
    try:
        provider.first.wait_for(state="visible", timeout=15_000)
        print(f"hop 1b: clicking '{SSO_PROVIDER_LABEL}'", file=sys.stderr)
        provider.first.click(timeout=15_000)
        page.wait_for_timeout(4000)
    except PlaywrightTimeout:
        raise LoginError(
            f"provider button '{SSO_PROVIDER_LABEL}' never appeared "
            f"(url={page.url}). Concur did not offer the UF SSO option for "
            f"this identity."
        )

    # --- Hop 2: UF Shibboleth (bare GatorLink id, not the email) ---
    # Concur may hand us straight to Duo if a Shibboleth session is
    # already alive, so only fill credentials if the form is actually up.
    if page.query_selector(SHIB_PASS_FIELD):
        print(f"hop 2: UF Shibboleth -> {ufl_username}", file=sys.stderr)
        page.fill(SHIB_USER_FIELD, ufl_username, timeout=timeout_ms)
        page.fill(SHIB_PASS_FIELD, ufl_password, timeout=timeout_ms)
        page.press(SHIB_PASS_FIELD, "Enter")
        page.wait_for_timeout(4000)
    else:
        print("hop 2: skipped (no Shibboleth form up)", file=sys.stderr)

    # --- Hop 3: Duo ---
    if on_duo_page(page):
        print("hop 3: Duo", file=sys.stderr)
        # Separate budget from the form fills. A push needs a human to
        # notice it, walk to their phone, and read the number; 120s is
        # tight when nobody warned them in advance, and a timeout here
        # costs a whole re-run of hops 1-2.
        complete_duo(page, PushApprover(), timeout_ms=duo_timeout_ms)
    else:
        print(f"hop 3: skipped (not on Duo; url={page.url})", file=sys.stderr)

    page.wait_for_timeout(3000)
    final = page.url

    # Verify, do not assume. Reaching the end of the hops is not the same
    # as being signed in: an unmapped identity bounces back to
    # /nui/signin without any error text, and the old code read that
    # silence as success.
    if not is_signed_in(page):
        raise LoginError(
            f"never reached an authenticated session (ended on {final}). "
            "Concur could not map the identity to a company with SSO, or a "
            "hop was skipped. See the screenshot in the state dir."
        )

    print(f"\nsigned in -> {final}", file=sys.stderr)
    return final


def login(
    *,
    concur_username: str,
    ufl_username: str,
    ufl_password: str,
    headed: bool = False,
    persist: bool = True,
    timeout_ms: int = 45_000,
    duo_timeout_ms: int = 300_000,
) -> str:
    """Sign in and return the post-login URL. Raises on any failed hop.

    Closes the browser on return, which **drops the session cookie**. Callers
    that need to keep using the session should use `authenticated_context()`
    instead.
    """
    with authenticated_context(
        concur_username=concur_username,
        ufl_username=ufl_username,
        ufl_password=ufl_password,
        headed=headed,
        persist=persist,
        timeout_ms=timeout_ms,
        duo_timeout_ms=duo_timeout_ms,
    ) as page:
        return page.url


@contextmanager
def authenticated_context(
    *,
    concur_username: str,
    ufl_username: str,
    ufl_password: str,
    headed: bool = False,
    persist: bool = True,
    timeout_ms: int = 45_000,
    duo_timeout_ms: int = 300_000,
):
    """Context manager yielding a **live, signed-in page**.

    Yields the Playwright `Page` inside an open browser so the caller can work
    in the same session that authenticated. Closes everything on exit.
    """
    configure(snapshot=_snapshot, announce=_announce)
    profile = STATE_DIR / "browser" if persist else None

    with sync_playwright() as p:
        if profile is not None:
            profile.mkdir(parents=True, exist_ok=True)
            ctx = p.chromium.launch_persistent_context(
                str(profile), headless=not headed, viewport={"width": 1440, "height": 1000}
            )
        else:
            ctx = p.chromium.launch(headless=not headed).new_context(
                viewport={"width": 1440, "height": 1000}
            )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            sign_in_page(
                page,
                concur_username=concur_username,
                ufl_username=ufl_username,
                ufl_password=ufl_password,
                timeout_ms=timeout_ms,
                duo_timeout_ms=duo_timeout_ms,
            )
            yield page
        except Exception:
            _snapshot(page, "login-failure")
            raise
        finally:
            ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sign in to Concur via UF SSO + Duo.")
    ap.add_argument(
        "--concur-username", default=None, help="default from $CONCUR_USERNAME or secrets file"
    )
    ap.add_argument(
        "--ufl-username", default=None, help="default from $UFL_USERNAME or secrets file"
    )
    ap.add_argument("--secrets-file", type=Path, default=None)
    ap.add_argument("--headed", action="store_true", help="show the browser (needs a display)")
    ap.add_argument("--no-persist", action="store_true", help="ephemeral profile, no device trust")
    ap.add_argument("--timeout", type=int, default=45_000, help="form-fill timeout (ms)")
    ap.add_argument(
        "--duo-timeout",
        type=int,
        default=300_000,
        help="how long to wait for the phone tap (ms, default 300s)",
    )
    args = ap.parse_args()

    try:
        creds = resolve(
            concur_username=args.concur_username,
            ufl_username=args.ufl_username,
            secrets_file=args.secrets_file,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Secret-free: reports which source supplied each value, never the values that
    # matter. The usernames are not secret and are shown so a wrong-principal
    # failure is visible at a glance.
    print(creds.report(), file=sys.stderr)

    try:
        login(
            concur_username=creds.concur_username,
            ufl_username=creds.ufl_username,
            ufl_password=creds.ufl_password,
            headed=args.headed,
            persist=not args.no_persist,
            timeout_ms=args.timeout,
            duo_timeout_ms=args.duo_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\nlogin failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"evidence: screenshots in {STATE_DIR / 'snapshots'}", file=sys.stderr)
        return 1
    print("login ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
