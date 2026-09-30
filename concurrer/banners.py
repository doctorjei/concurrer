"""Inspect and explicitly walk through blocking browser banners.

A banner is not dismissed just because it blocks automation. The default is to
report its visible text and choices through ``BannerBlocked`` before credentials
or application actions proceed. A caller can supply a one-invocation response as
``BANNER_ID=EXACT VISIBLE LABEL`` after the operator has reviewed the notice.

The first supported banner is Concur's TrustArc cookie notice. Its selectors and
visible actions were observed on 2026-09-30; every click rechecks the action's
live text, so a changed notice fails closed instead of silently accepting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

TRUSTARC_ID = "trustarc"
TRUSTARC_OVERLAY = "#trustarc-banner-overlay"
TRUSTARC_ROOT = "#truste-consent-track"


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


@dataclass(frozen=True)
class BannerAction:
    label: str
    selector: str
    kind: str


@dataclass(frozen=True)
class Banner:
    identifier: str
    text: str
    actions: tuple[BannerAction, ...]

    def summary(self) -> str:
        choices = ", ".join(repr(action.label) for action in self.actions)
        return f"{self.identifier}: {self.text[:500]!r}; choices: {choices}"


class BannerBlocked(RuntimeError):
    """A visible banner needs an explicit response before login can continue."""

    def __init__(self, banners: list[Banner]):
        self.banners = tuple(banners)
        detail = "\n".join(f"  - {banner.summary()}" for banner in banners)
        super().__init__(
            "blocking banner requires an explicit response; inspect its text and "
            "rerun with --banner-action BANNER_ID=EXACT_LABEL:\n" + detail
        )


def _trustarc_banner(page) -> Banner | None:
    """Return the visible TrustArc notice in its current live form."""
    raw = page.evaluate(
        """({overlay, root}) => {
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 0 && r.height > 0
                    && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const banner = document.querySelector(overlay);
            if (!visible(banner)) return null;
            const container = document.querySelector(root) || banner.parentElement;
            const action = (label, selector, kind) => {
                const el = document.querySelector(selector);
                return visible(el) ? {label, selector, kind} : null;
            };
            return {
                text: (container?.innerText || document.body.innerText || '').trim(),
                actions: [
                    action('Understood', '#truste-consent-button', 'button'),
                    action('Manage Settings', '#truste-show-consent', 'button'),
                    action('Cookie Statement', '#truste-cookie-button', 'link'),
                ].filter(Boolean),
            };
        }""",
        {"overlay": TRUSTARC_OVERLAY, "root": TRUSTARC_ROOT},
    )
    if raw is None:
        return None
    return Banner(
        TRUSTARC_ID,
        raw["text"],
        tuple(BannerAction(**action) for action in raw["actions"]),
    )


def visible_banners(page) -> list[Banner]:
    """Return supported banners currently blocking the supplied page."""
    banners: list[Banner] = []
    trustarc = _trustarc_banner(page)
    if trustarc is not None:
        banners.append(trustarc)
    return banners


def parse_banner_actions(raw_actions: list[str]) -> dict[str, str]:
    """Parse repeatable ``BANNER_ID=EXACT_LABEL`` CLI values."""
    actions: dict[str, str] = {}
    for raw in raw_actions:
        identifier, separator, label = raw.partition("=")
        if not separator or not identifier.strip() or not label.strip():
            raise ValueError(
                f"invalid banner action {raw!r}; use BANNER_ID=EXACT_LABEL"
            )
        identifier = identifier.strip().casefold()
        if identifier in actions:
            raise ValueError(f"duplicate banner action for {identifier!r}")
        actions[identifier] = label.strip()
    return actions


def _click_response(page, banner: Banner, requested_label: str) -> None:
    action = next(
        (action for action in banner.actions if _norm(action.label) == _norm(requested_label)),
        None,
    )
    if action is None:
        raise ValueError(
            f"banner {banner.identifier!r} does not offer exact action {requested_label!r}; "
            f"offers {[action.label for action in banner.actions]!r}"
        )
    locator = page.locator(action.selector)
    if locator.count() != 1 or not locator.first.is_visible():
        raise RuntimeError(
            f"banner action {action.label!r} is no longer uniquely visible; refusing click"
        )
    live_label = locator.first.inner_text().strip()
    if _norm(live_label) != _norm(action.label):
        raise RuntimeError(
            f"banner action text changed from {action.label!r} to {live_label!r}; refusing click"
        )
    locator.click(timeout=15_000)
    page.wait_for_timeout(500)


def handle_banners(page, responses: Mapping[str, str] | None = None) -> None:
    """Apply explicit responses or raise a report of the still-visible banners.

    Settings pages deliberately do not receive implied follow-up choices. If a
    response exposes another banner state, this function raises ``BannerBlocked``
    again with the new text/actions for operator review.
    """
    banners = visible_banners(page)
    if not banners:
        return
    responses = {key.casefold(): value for key, value in (responses or {}).items()}
    for banner in banners:
        response = responses.get(banner.identifier.casefold())
        if response is None:
            raise BannerBlocked(banners)
        _click_response(page, banner, response)
    remaining = visible_banners(page)
    if remaining:
        raise BannerBlocked(remaining)
