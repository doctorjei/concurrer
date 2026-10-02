"""Read-only probe: report-wide details and per-type dropdown option lists.

Feeds the xlsx workbook format (``Report Details`` sheet and dropdown
validation lists). Two passes, one login:

1. **Report main screen** — zero clicks. Dumps page text, every element with a
   stable ``data-nuiexp`` handle, and headings, so the operator can choose which
   report-wide facts belong on the Report Details sheet.
2. **Option lists** — one expense per expense type (picked from an export's
   ``.live.json``), opened by stable ID. Each unlocked combobox is opened, its
   options read, and the picker closed with Escape. **No option is ever
   clicked.** The field's value is re-read after closing; any change aborts the
   run at once. Nothing is saved: the form is abandoned by navigation, and the
   edit page has no autosave (see ``designs/expense-reports.md``).

Usage::

    python -m concurrer.probereport --report 506EC055FE79452BB943 \\
        --live ~/vault/rw/expenses-CCED-Summer-2026-20261001.live.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .banners import parse_banner_actions
from .login import authenticated_context
from .nav import open_expense_by_id, open_report, wait_for_report_ready
from .secrets import STATE_DIR, resolve
from .widgets import field_inventory, read_combobox_options

REPORT_JS = """() => {
  const vis = (el) => el.getClientRects().length > 0;
  const text = (el) => (el.innerText || '').trim();
  const nuiexp = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('[data-nuiexp]')) {
    // Expense grid rows are already exported; keep the dump about the report.
    if (el.closest('.sapcnqr-data-grid-list__row')) continue;
    const key = el.getAttribute('data-nuiexp');
    const t = text(el).slice(0, 200);
    if (seen.has(key + '|' + t)) continue;
    seen.add(key + '|' + t);
    nuiexp.push({nuiexp: key, tag: el.tagName.toLowerCase(), text: t,
                 ariaLabel: el.getAttribute('aria-label') || null,
                 visible: vis(el)});
  }
  const headings = Array.from(document.querySelectorAll(
      'h1,h2,h3,h4,[role=heading]')).map(e => text(e)).filter(Boolean);
  return {url: location.href, title: document.title, headings, nuiexp,
          bodyText: (document.body.innerText || '').slice(0, 30000)};
}"""

def _dump(name: str, payload: dict) -> Path:
    out_dir = STATE_DIR / "discover"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)
    return path


def pick_one_per_type(live: dict[str, list[dict]]) -> dict[str, str]:
    """Map expense type -> first expense ID of that type, in export order."""
    picked: dict[str, str] = {}
    for expense_id, fields in live.items():
        etype = next((f["value"] for f in fields if f["name"] == "expenseType"), "")
        picked.setdefault(etype, expense_id)
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only probe of report details and dropdown options."
    )
    ap.add_argument("--report", required=True)
    ap.add_argument("--live", required=True, type=Path,
                    help="an expexp .live.json for the same report")
    ap.add_argument("--skip-options", action="store_true")
    ap.add_argument("--banner-action", action="append", default=[],
                    metavar="BANNER_ID=EXACT_LABEL")
    args = ap.parse_args()

    live_doc = json.loads(args.live.expanduser().read_text())
    if live_doc.get("report") != args.report:
        print(f"--live is for report {live_doc.get('report')!r}, "
              f"not {args.report!r}", file=sys.stderr)
        return 2
    picked = pick_one_per_type(live_doc["live"])

    creds = resolve()
    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
        banner_actions=parse_banner_actions(args.banner_action),
    ) as page:
        open_report(page, args.report)
        wait_for_report_ready(page)
        report = page.evaluate(REPORT_JS)
        path = _dump(f"reportprobe_{args.report}.json", report)
        print(f"report page: {len(report['nuiexp'])} handles, "
              f"{len(report['headings'])} headings -> {path}", flush=True)

        if args.skip_options:
            return 0
        options: dict[str, dict] = {}
        try:
            for etype, expense_id in picked.items():
                open_expense_by_id(page, args.report, expense_id)
                per_field: dict[str, dict] = {}
                for field in field_inventory(page):
                    if field["widget"] != "combobox" or field["locked"]:
                        continue
                    per_field[field["name"]] = {
                        "label": field["label"], **read_combobox_options(page, field)}
                    opts = per_field[field["name"]].get("options", [])
                    print(f"  {etype[:28]:28} {field['name']:24} "
                          f"{len(opts):3} option(s)"
                          f"{'  (maybe truncated)' if per_field[field['name']].get('maybeTruncated') else ''}",
                          flush=True)
                options[etype] = {"expense_id": expense_id, "fields": per_field}
        finally:
            path = _dump(f"optionsprobe_{args.report}.json",
                         {"report": args.report, "types": options})
            print(f"options for {len(options)} type(s) -> {path}", flush=True)

    print("probe completed: no option committed, nothing saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
