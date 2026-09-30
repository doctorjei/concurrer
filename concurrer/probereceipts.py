"""Read-only probe: what is the actual receipt-upload surface?

Answers three questions that decide how receipts get built, without mutating
anything:

1. Is there a native ``<input type=file>``? **Playwright drives one with
   ``set_input_files()`` directly on the element, without ever opening an OS
   picker** — so DOM presence alone answers whether uploads are automatable.
   That is why this probe clicks nothing.
2. Does the upload surface live at report level, per-expense level, or both?
3. Is there a native CSV export hiding behind ``Print/Share``?

Every control is reported with its stable handle (``id`` / ``data-nuiexp``)
rather than a hashed class, since hashed classes churn between deploys.

Usage::

    python -m concurrer.probereceipts --report B857BA45DBC24EDB8420 \
        --banner-action trustarc=Understood
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .banners import parse_banner_actions
from .login import authenticated_context
from .nav import open_expense_edit, open_report
from .secrets import STATE_DIR, resolve

PROBE_JS = """() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {w: Math.round(r.width), h: Math.round(r.height)};
  };
  const attrs = (el) => {
    const nui = Array.from(el.querySelectorAll('[data-nuiexp]'))
      .map(e => e.getAttribute('data-nuiexp')).filter(Boolean).slice(0, 4);
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      ariaLabel: el.getAttribute('aria-label') || null,
      name: el.getAttribute('name') || null,
      href: el.getAttribute('href') || null,
      nuiexp: nui,
      text: (el.innerText || '').trim().slice(0, 80),
      box: rect(el),
      display: getComputedStyle(el).display,
      visible: el.getClientRects().length > 0,
    };
  };

  const out = {url: location.href, fileInputs: [], namedControls: [],
               uploadReceipt: [], notes: []};

  // 1. Every native file input anywhere in the document.
  document.querySelectorAll('input[type=file]').forEach((el) => {
    const info = attrs(el);
    info.accept = el.accept || null;
    info.multiple = !!el.multiple;
    // A hidden file input is normal and still drivable: UIs routinely park it
    // offscreen and trigger it from a styled button.
    info.parkedOffscreen = (info.box.w < 2 || info.box.h < 2);
    info.parentChain = (() => {
      const chain = [];
      for (let n = el.parentElement; n && chain.length < 4; n = n.parentElement) {
        chain.push(n.tagName.toLowerCase() + (n.id ? '#' + n.id : ''));
      }
      return chain;
    })();
    out.fileInputs.push(info);
  });

  // 2. The known report-level upload control.
  document.querySelectorAll('#upload-receipt-button').forEach((el) => {
    out.uploadReceipt.push(attrs(el));
  });

  // 3. Anything whose accessible name mentions receipts, export, or print.
  const terms = ['receipt', 'upload', 'export', 'csv', 'print', 'share',
                'attach', 'attachment'];
  const seen = new Set();
  const candidates = document.querySelectorAll(
    'button, a, [role=button], [role=menuitem], [role=menuitemcheckbox], ' +
    '[aria-label], [data-nuiexp]');
  candidates.forEach((el) => {
    const hay = [
      el.getAttribute('aria-label') || '',
      (el.innerText || '').trim(),
      el.getAttribute('data-nuiexp') || '',
      el.id || '',
    ].join(' ').toLowerCase();
    if (!terms.some((t) => hay.includes(t))) return;
    const key = (el.id || '') + '|' + (el.getAttribute('aria-label') || '')
      + '|' + (el.innerText || '').trim().slice(0, 40);
    if (seen.has(key)) return;
    seen.add(key);
    out.namedControls.push(attrs(el));
  });
  return out;
}"""


def _dump(label: str, payload: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"receiptprobe_{label}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=False))
    path.chmod(0o600)
    return path


def _summarize(label: str, probe: dict) -> None:
    print(f"\n=== {label} ===  url={probe.get('url')}")
    print(f"  native <input type=file> count: {len(probe['fileInputs'])}")
    for f in probe["fileInputs"]:
        print(
            f"    - id={f['id']!r} name={f['name']!r} accept={f.get('accept')!r} "
            f"multiple={f.get('multiple')} parked={f.get('parkedOffscreen')} "
            f"box={f['box']} chain={f['parentChain'][:3]}"
        )
    print(f"  #upload-receipt-button: {len(probe['uploadReceipt'])} match(es)")
    for b in probe["uploadReceipt"]:
        print(f"    - visible={b['visible']} box={b['box']} text={b['text']!r}")
    print(f"  receipt/upload/export/print-named controls: {len(probe['namedControls'])}")
    for c in probe["namedControls"]:
        print(
            f"    - {c['tag']} id={c['id']!r} aria={c['ariaLabel']!r} "
            f"nuiexp={c['nuiexp']} text={c['text']!r} visible={c['visible']}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only probe of the Concur receipt-upload surface."
    )
    ap.add_argument("--report", required=True)
    ap.add_argument(
        "--row",
        type=int,
        default=1,
        help="one-based expense row to open for the per-expense probe",
    )
    ap.add_argument(
        "--banner-action",
        action="append",
        default=[],
        metavar="BANNER_ID=EXACT_LABEL",
    )
    ap.add_argument(
        "--skip-expense",
        action="store_true",
        help="probe only the report page; do not open an expense",
    )
    args = ap.parse_args()

    if args.row < 1:
        print("--row must be at least 1; row 0 is select-all", file=sys.stderr)
        return 2

    creds = resolve()
    out_dir = STATE_DIR / "discover"
    banner_actions = parse_banner_actions(args.banner_action)

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=banner_actions,
    ) as page:
        open_report(page, args.report)
        report_probe = page.evaluate(PROBE_JS)
        _dump("report", report_probe, out_dir)
        _summarize("REPORT PAGE", report_probe)

        if not args.skip_expense:
            open_expense_edit(page, args.report, args.row)
            expense_probe = page.evaluate(PROBE_JS)
            _dump("expense", expense_probe, out_dir)
            _summarize("EXPENSE PAGE", expense_probe)

    print(f"\nevidence written to {out_dir}")
    print("probe completed: no clicks, no uploads, no writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
