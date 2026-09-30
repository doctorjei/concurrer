"""List expenses of a given type with the detail a human needs to classify them.

Built for the "Undefined" pile: the report shows ~11 expenses typed Undefined,
and until they carry a real expense type their required fields are unknown, so
they cannot be triaged or auto-filled. The row text only gives type/amount/date
— not the vendor, which is the thing you actually classify from.

This opens each matching expense and reads the money fields, then emits a
markdown table with a blank column to fill in.

Read-only. Never calls authorize_writes, so `save` is unreachable for the whole
run regardless of which report it points at.

Usage:
    python -m concurrer.classify --report <ID> [--match Undefined]
"""

from __future__ import annotations

import argparse
import json
import sys

from .login import authenticated_context
from .nav import expense_row_number, open_expense_edit, open_report
from .secrets import resolve
from .triage import triage_rows
from .widgets import field_inventory

OUT = "/home/agent/.config/concur/discover"

# Field labels read off the expense edit page, in the order that helps classify.
WANT = [
    "Transaction Date",
    "Vendor Name",
    "Payment Type",
    "Amount",
    "Currency",
    "Comment",
]


def read_detail(page) -> dict:
    fields = field_inventory(page)
    by_label = {f["label"]: f for f in fields}
    out = {}
    for label in WANT:
        f = by_label.get(label)
        out[label] = (f["value"] if f else "")
    # Some layouts truncate the currency label; grab by prefix too.
    if not out.get("Currency"):
        for f in fields:
            if f["label"].startswith("Currency"):
                out["Currency"] = f["value"]
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="List expenses for classification.")
    ap.add_argument("--report", required=True)
    ap.add_argument("--match", default="Undefined",
                    help="only rows whose identity contains this")
    ap.add_argument("--max", type=int, default=40)
    args = ap.parse_args()

    creds = resolve()

    with authenticated_context(
        concur_username=creds.concur_username,
        ufl_username=creds.ufl_username,
        ufl_password=creds.ufl_password,
    ) as page:
        open_report(page, args.report)
        print(f"report: {page.title()}")

        rows = triage_rows(page)
        hits = [(expense_row_number(i), r) for i, r in enumerate(rows)
                if args.match.lower() in r["identity"].lower()]
        print(f"{len(rows)} rows total, {len(hits)} matching {args.match!r}\n")

        if not hits:
            print("nothing to list", file=sys.stderr)
            return 1

        records = []
        for n, (i, r) in enumerate(hits[:args.max], 1):
            try:
                eid = open_expense_edit(page, args.report, i)
            except Exception as exc:
                print(f"  {n}. row {i}: could not open ({exc})", file=sys.stderr)
                records.append({"row": i, "identity": r["identity"],
                               "error": str(exc)})
                continue
            d = read_detail(page)
            d["row"] = i
            d["expense_id"] = eid
            records.append(d)
            print(f"  {n:>2}. row {i}  {d.get('Vendor Name','')[:34]:<34} "
                  f"{d.get('Amount',''):>10} {d.get('Currency','')[:10]:<10} "
                  f"{d.get('Transaction Date','')}")
            open_report(page, args.report, settle_ms=4000)

        path = f"{OUT}/classify_{args.match.lower()}_{args.report[:12]}.json"
        with open(path, "w") as fh:
            json.dump({"report": args.report, "match": args.match,
                       "records": records}, fh, indent=2)
        print(f"\nwrote {path}")

        # Markdown table with a blank column to fill in.
        md = ["| # | Date | Vendor | Amount | Currency | Payment | Comment | "
              "→ Expense type (fill in) |",
              "|---|---|---|---|---|---|---|---|"]
        for n, d in enumerate(records, 1):
            if "error" in d:
                md.append(f"| {n} | — | (could not open) | | | | | |")
                continue
            md.append(
                f"| {n} | {d.get('Transaction Date','')} | "
                f"{d.get('Vendor Name','')} | {d.get('Amount','')} | "
                f"{d.get('Currency','')} | {d.get('Payment Type','')} | "
                f"{d.get('Comment','') or '—'} |  |"
            )
        table = "\n".join(md)
        mdpath = f"{OUT}/classify_{args.match.lower()}.md"
        with open(mdpath, "w") as fh:
            fh.write(table + "\n")
        print(f"wrote {mdpath}\n")
        print(table)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
