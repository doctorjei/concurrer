#!/usr/bin/env bash
# Reprovision concurrer's runtime from nothing but $HOME.
#
# WHY THIS EXISTS: Chromium's system libraries install into the container rootfs,
# NOT $HOME. A container rebuild keeps $HOME and loses them, which shows up as
# chrome-headless-shell dying on "libnspr4.so: cannot open shared object file".
# Re-run this after any rebuild.
#
# Usage:  scripts/setup.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> venv + Python deps"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip -q install --upgrade pip
# duo-pass: Duo second-factor handling (GPL-3.0-or-later), spun out of canvasser.
# playwright: browser automation. Pin loosely; both are early-versioned.
.venv/bin/pip install 'duo-pass>=0.0.1' 'playwright>=1.40'

echo "==> Chromium browser binaries (into ~/.cache/ms-playwright, survives rebuild)"
.venv/bin/playwright install chromium

echo "==> System libraries (into the container rootfs, LOST on rebuild)"
# 16 shared libs the headless shell needs: libnss3/libnspr4/libnssutil3,
# libatk*/libatspi, libX11/libXcomposite/libXdamage/libXext/libXfixes/libXrandr,
# libgbm/libxcb/libxkbcommon, libasound, plus fonts.
if sudo -n true 2>/dev/null; then
    sudo -n .venv/bin/playwright install-deps chromium
else
    echo "   No passwordless sudo. Run manually as root:"
    echo "     .venv/bin/playwright install-deps chromium"
fi

echo "==> State dirs"
mkdir -p ~/.config/concur/browser ~/.config/concur/snapshots
chmod 700 ~/.config/concur ~/.config/concur/browser

echo "==> Smoke test: chromium launches"
.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    pg.goto("about:blank")
    print("   chromium ok:", b.version)
    b.close()
PY

echo
echo "Done. Credentials are NOT handled here by design - place them yourself:"
echo "  ~/.config/concur/secrets.env   (chmod 600)"
echo "    CONCUR_USERNAME=erthworm@ufl.edu"
echo "    CONCUR_PASSWORD=..."
