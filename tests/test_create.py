"""Browser-free tests for planned expense creation."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from concurrer.create import create_token, load_plan, main, validate_plan
from concurrer.safety import grant_active

CCED = "506EC055FE79452BB943"
RAIL = {
    "transactionDate": "05/15/2026",
    "transactionCurrencyName": "Japan, Yen (JPY)",
    "transactionAmount": "490",
    "location": "Kyoto, JAPAN",
    "vendorName": "Keihan Railway",
    "paymentType": "Personal Funds",
    "custom13": "03. International",
    "custom12": "Student Life",
}


class CreatePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "r.jpg").write_bytes(b"\xff\xd8receipt")

    def plan(self, values=None, report=CCED, kind="Train/Rail", receipt="r.jpg"):
        path = self.dir / "plan.json"
        item = {"key": "rail-0515", "type": kind, "values": values or dict(RAIL)}
        if receipt:
            item["receipt"] = receipt
        path.write_text(json.dumps({"report_id": report, "expenses": [item]}))
        return path

    def test_complete_plan_validates(self):
        _, expenses = load_plan(self.plan())
        self.assertEqual(validate_plan(expenses), [])

    def test_missing_required_unknown_field_and_bad_values_are_refused(self):
        values = dict(RAIL)
        del values["location"]                     # required Destination
        values["custom20"] = "Taxi"                # Train/Rail has no custom20
        values["transactionDate"] = "2026-05-15"   # wrong format
        values["exchangeRate"] = "0.0064"          # Concur computes it
        _, expenses = load_plan(self.plan(values))
        problems = "\n".join(validate_plan(expenses))
        for needle in ("Destination", "custom20", "MM/DD/YYYY", "never set"):
            self.assertIn(needle, problems)

    def test_unknown_type_and_missing_receipt_are_refused(self):
        _, expenses = load_plan(self.plan(kind="Undefined", receipt="nope.jpg"))
        problems = "\n".join(validate_plan(expenses))
        self.assertIn("not in the 67-type map", problems)

    def test_token_covers_values_and_receipt_bytes(self):
        report, expenses = load_plan(self.plan())
        token = create_token(report, expenses)
        values = dict(RAIL, transactionAmount="491")
        self.assertNotEqual(token, create_token(*load_plan(self.plan(values))))
        (self.dir / "r.jpg").write_bytes(b"\xff\xd8other")
        self.assertNotEqual(token, create_token(*load_plan(self.plan())))

    def test_dry_run_prints_token_and_never_logs_in(self):
        out = io.StringIO()
        with (
            patch.object(sys, "argv", ["create", "--plan", str(self.plan())]),
            patch("concurrer.create.resolve") as resolve_creds,
            redirect_stdout(out),
        ):
            self.assertEqual(main(), 0)
        resolve_creds.assert_not_called()
        self.assertIn("PLAN TOKEN:", out.getvalue())

    def test_cced_commit_with_wrong_token_refuses_before_login(self):
        argv = ["create", "--plan", str(self.plan()), "--commit", "--confirm", "000000000000"]
        with (
            patch.object(sys, "argv", argv),
            patch("concurrer.create.resolve") as resolve_creds,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 2)
        resolve_creds.assert_not_called()
        self.assertFalse(grant_active())

    def test_other_real_report_is_refused_before_login(self):
        argv = ["create", "--plan", str(self.plan(report="0123456789ABCDEF0123")), "--commit"]
        with (
            patch.object(sys, "argv", argv),
            patch("concurrer.create.resolve") as resolve_creds,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 2)
        resolve_creds.assert_not_called()


class TypePickerTests(unittest.TestCase):
    def _page(self, entries):
        class Cand:
            def __init__(self, text, visible):
                self.text, self.visible = text, visible
            def is_visible(self):
                return self.visible
            def inner_text(self):
                return self.text

        class Many:
            def __init__(self, items):
                self.items = items
            def count(self):
                return len(self.items)
            def nth(self, i):
                return self.items[i]
            @property
            def last(self):
                return Scope()

        class Scope:
            def get_by_text(self, *_a, **_k):
                return Many([Cand(t, v) for t, v in entries])

        class Page:
            def locator(self, _sel):
                return Many([object()])
        return Page()

    def test_clicks_the_single_visible_exact_leaf_in_the_dialog(self):
        from concurrer.create import _choose_type
        page = self._page([("Ground Transportation", False),          # hidden list cell
                           ("Ground Transportation Services\nx", True),
                           ("Ground Transportation\nAccount code", True)])
        with patch("concurrer.create.click_safe") as click:
            _choose_type(page, "Ground Transportation")
        self.assertEqual(click.call_args.args[1].text, "Ground Transportation\nAccount code")

    def test_ambiguous_or_missing_entry_is_refused(self):
        from concurrer.create import CreatePlanError, _choose_type
        for entries in ([], [("Ground Transportation", True)] * 2):
            with self.subTest(n=len(entries)):
                with patch("concurrer.create.click_safe") as click:
                    with self.assertRaises(CreatePlanError):
                        _choose_type(self._page(entries), "Ground Transportation")
                click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
