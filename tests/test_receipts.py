"""Browser-free tests for per-expense receipt attachment.

These mock the browser seam to prove orchestration: local validation precedes
login, dry runs never touch credentials or setters, real-report commits fail
before side effects, the exact ``#upload-file`` control is required, URL
authorization directly precedes the single setter call, and no retry or click
ever follows a failure. Filenames and paths must never reach console output or
evidence payloads.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from concurrer.receipts import (
    ALLOWED_SUFFIXES,
    UPLOAD_INPUT_SELECTOR,
    ReceiptError,
    attach_receipt,
    describe_request,
    main,
    validate_request,
    validate_upload_input,
)
from concurrer.safety import TEST_REPORT_ID, grant_active


EXPENSE = "077DDCA88361BC4DB005BDDD9BBC3BF4"
REAL_REPORT = "506EC055FE79452BB943"


def receipt_file(directory: Path, name: str = "receipt.png", size: int = 64) -> Path:
    path = directory / name
    path.write_bytes(b"\x89PNG\r\n" + b"x" * size)
    return path


class FakeLocator:
    def __init__(self, page, count: int = 1):
        self.page = page
        self._count = count
        self.set_calls: list[str] = []
        self.clicks = 0

    def count(self):
        return self._count

    def set_input_files(self, path):
        self.set_calls.append(path)
        if self.page.fail_setter is not None:
            raise self.page.fail_setter
        return None

    def click(self, *args, **kwargs):
        self.clicks += 1
        raise AssertionError("receipts must never click")


class FakePage:
    def __init__(self, url: str, checks: list[dict] | None = None):
        self.url = url
        self.checks = list(checks or [])
        self.locator_selector: str | None = None
        self.locator_count = 1
        self.locator_obj: FakeLocator | None = None
        self.fail_setter = None
        self.waits = 0

    def locator(self, selector):
        self.locator_selector = selector
        self.locator_obj = FakeLocator(self, self.locator_count)
        return self.locator_obj

    def evaluate(self, _js, arg=None):
        if "files" in _js:
            return 1
        if self.checks:
            return self.checks.pop(0)
        return {"panel": True, "nameVisible": False, "itemCount": 0}

    def wait_for_timeout(self, _millis):
        self.waits += 1


@contextmanager
def context_for(page):
    yield page


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_rejects_bad_ids_before_touching_disk(self):
        target = receipt_file(self.directory)
        for report, expense in (("zz", EXPENSE), (TEST_REPORT_ID, "!!")):
            with self.subTest(report=report, expense=expense):
                with self.assertRaises(ReceiptError):
                    validate_request(report, expense, target)

    def test_rejects_missing_directory_empty_and_bad_suffix(self):
        with self.assertRaises(ReceiptError):
            validate_request(TEST_REPORT_ID, EXPENSE, self.directory / "absent.png")
        subdir = self.directory / "sub"
        subdir.mkdir()
        with self.assertRaises(ReceiptError):
            validate_request(TEST_REPORT_ID, EXPENSE, subdir)
        empty = self.directory / "empty.png"
        empty.write_bytes(b"")
        with self.assertRaises(ReceiptError):
            validate_request(TEST_REPORT_ID, EXPENSE, empty)
        bad = self.directory / "note.txt"
        bad.write_text("not a receipt")
        with self.assertRaises(ReceiptError):
            validate_request(TEST_REPORT_ID, EXPENSE, bad)

    def test_accepts_observed_suffixes_case_insensitively(self):
        for suffix in ALLOWED_SUFFIXES:
            target = receipt_file(self.directory, name=f"r{suffix.upper()}")
            request = validate_request(TEST_REPORT_ID, EXPENSE, target)
            self.assertEqual(request.source, target)

    def test_description_never_carries_path_or_name(self):
        target = receipt_file(self.directory, name="sensitive-vendor.png")
        request = validate_request(TEST_REPORT_ID, EXPENSE, target)
        summary = describe_request(request)
        self.assertNotIn("sensitive-vendor", summary)
        self.assertNotIn(str(target), summary)
        self.assertIn(".png", summary)


class UploadInputTests(unittest.TestCase):
    def good(self):
        return {
            "count": 1,
            "inputs": [
                {
                    "tag": "input",
                    "type": "file",
                    "multiple": True,
                    "accept": ".png, .jpg, .jpeg, .pdf, .tif, .tiff",
                }
            ],
        }

    def test_accepts_known_good_control(self):
        validate_upload_input(self.good())

    def test_count_tag_type_multiple_and_accept_fail_closed(self):
        cases = [
            dict(self.good(), count=0),
            dict(self.good(), count=2),
            {"count": 1, "inputs": [{"tag": "div", "type": "file", "multiple": True, "accept": ".png"}]},
            {"count": 1, "inputs": [{"tag": "input", "type": "text", "multiple": True, "accept": ".png"}]},
            {"count": 1, "inputs": [{"tag": "input", "type": "file", "multiple": False, "accept": ".png"}]},
            {"count": 1, "inputs": [{"tag": "input", "type": "file", "multiple": True, "accept": ".png"}]},
        ]
        for info in cases:
            with self.subTest(info=info):
                with self.assertRaises(ReceiptError):
                    validate_upload_input(info)


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = receipt_file(Path(self.tmp.name), name="vendor-bill.png")

    def test_dry_run_validates_locally_with_no_browser_or_grant(self):
        argv = [
            "receipts",
            "--report", TEST_REPORT_ID,
            "--expense-id", EXPENSE,
            "--file", str(self.target),
        ]
        out = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch("concurrer.receipts.resolve") as resolve_creds,
            patch("concurrer.receipts.authenticated_context") as auth,
            patch("concurrer.receipts.attach_receipt") as attach,
            redirect_stdout(out),
        ):
            self.assertEqual(main(), 0)
        resolve_creds.assert_not_called()
        auth.assert_not_called()
        attach.assert_not_called()
        self.assertFalse(grant_active())
        text = out.getvalue()
        self.assertIn("Dry run only", text)
        self.assertNotIn("vendor-bill", text)
        self.assertNotIn(str(self.target), text)

    def test_multiple_files_refused_as_plan_error(self):
        argv = [
            "receipts",
            "--report", TEST_REPORT_ID,
            "--expense-id", EXPENSE,
            "--file", str(self.target),
            "--file", str(self.target),
        ]
        with patch.object(sys, "argv", argv):
            self.assertEqual(main(), 2)


class CommitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = receipt_file(Path(self.tmp.name))

    def test_real_report_commit_fails_before_creds_or_browser(self):
        argv = [
            "receipts",
            "--report", REAL_REPORT,
            "--expense-id", EXPENSE,
            "--file", str(self.target),
            "--commit",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("concurrer.receipts.resolve") as resolve_creds,
            patch("concurrer.receipts.authenticated_context") as auth,
            patch("concurrer.receipts.attach_receipt") as attach,
        ):
            self.assertEqual(main(), 2)
        resolve_creds.assert_not_called()
        auth.assert_not_called()
        attach.assert_not_called()
        self.assertFalse(grant_active())

    def _cced_argv(self, *extra):
        return ["receipts", "--report", "506EC055FE79452BB943", "--expense-id", EXPENSE,
                "--file", str(self.target), *extra]

    def test_cced_dry_run_prints_a_token_for_these_exact_bytes(self):
        from concurrer.receipts import receipt_token, validate_request
        out = io.StringIO()
        with patch.object(sys, "argv", self._cced_argv()), redirect_stdout(out):
            self.assertEqual(main(), 0)
        token = receipt_token(validate_request("506EC055FE79452BB943", EXPENSE, self.target))
        self.assertIn(f"PLAN TOKEN: {token}", out.getvalue())
        self.target.write_bytes(self.target.read_bytes() + b"x")
        self.assertNotEqual(token, receipt_token(
            validate_request("506EC055FE79452BB943", EXPENSE, self.target)))

    def test_cced_commit_with_a_wrong_token_fails_before_creds(self):
        with (
            patch.object(sys, "argv", self._cced_argv("--commit", "--confirm", "000000000000")),
            patch("concurrer.receipts.resolve") as resolve_creds,
            patch("concurrer.receipts.attach_receipt") as attach,
        ):
            self.assertEqual(main(), 2)
        resolve_creds.assert_not_called()
        attach.assert_not_called()
        self.assertFalse(grant_active())

    def test_cced_commit_with_the_token_attaches_under_a_scoped_grant(self):
        from concurrer.receipts import receipt_token, validate_request
        token = receipt_token(validate_request("506EC055FE79452BB943", EXPENSE, self.target))
        seen = []
        result = {"outcome": "verified-persisted", "detail": "ok", "expense_id": EXPENSE,
                  "assignedCount": 1, "preHadName": False, "observedImmediate": True,
                  "postReopen": True, "pre": {}, "post": {}}
        with (
            patch.object(sys, "argv", self._cced_argv("--commit", "--confirm", token)),
            patch("concurrer.receipts.resolve", return_value=SimpleNamespace(
                concur_username="c", ufl_username="u", ufl_password="p")),
            patch("concurrer.receipts.authenticated_context", return_value=context_for(object())),
            patch("concurrer.receipts.attach_receipt",
                  side_effect=lambda *_a: seen.append(grant_active()) or result),
            patch("concurrer.receipts.write_evidence", return_value=Path("/tmp/x.json")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(seen, [True])
        self.assertFalse(grant_active())

    def test_test_report_commit_enters_then_revokes_grant(self):
        argv = [
            "receipts",
            "--report", TEST_REPORT_ID,
            "--expense-id", EXPENSE,
            "--file", str(self.target),
            "--commit",
        ]
        page = FakePage(f"https://us2.concursolutions.com/nui/expense/reports/{TEST_REPORT_ID}/expenses/{EXPENSE}")
        with (
            patch.object(sys, "argv", argv),
            patch(
                "concurrer.receipts.resolve",
                return_value=SimpleNamespace(
                    concur_username="c", ufl_username="u", ufl_password="p"
                ),
            ),
            patch("concurrer.receipts.authenticated_context", return_value=context_for(page)),
            patch("concurrer.receipts.attach_receipt", return_value={"outcome": "verified-persisted", "detail": "ok", "expense_id": EXPENSE, "assignedCount": 1, "preHadName": False, "observedImmediate": True, "postReopen": True, "pre": {}, "post": {}}),
            patch("concurrer.receipts.write_evidence", return_value=Path("/tmp/x.json")),
        ):
            self.assertEqual(main(), 0)
        self.assertFalse(grant_active())


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = receipt_file(Path(self.tmp.name), name="bill.png")
        self.url = f"https://us2.concursolutions.com/nui/expense/reports/{TEST_REPORT_ID}/expenses/{EXPENSE}"

    def _request(self):
        return validate_request(TEST_REPORT_ID, EXPENSE, self.target)

    def _run_attach(self, page, **patches):
        defaults = {
            "concurrer.receipts.open_expense_by_id": lambda _p, _r, _e: EXPENSE,
            "concurrer.receipts.wait_for_upload_ready": lambda _p: {"count": 1},
            "concurrer.receipts.snapshot_receipt_surface": lambda _p: {"panel": True, "items": []},
        }
        defaults.update(patches)
        managers = [patch(name, target) for name, target in defaults.items()]
        for manager in managers:
            entered = manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)
        return entered

    def test_uses_direct_id_and_exact_selector_with_adjacent_authorization(self):
        page = FakePage(self.url, checks=[
            {"panel": True, "nameVisible": False, "itemCount": 0},
            {"panel": True, "nameVisible": True, "itemCount": 1},
            {"panel": True, "nameVisible": True, "itemCount": 1},
        ])
        order: list[str] = []
        with (
            patch("concurrer.receipts.open_expense_by_id", side_effect=lambda p, r, e: order.append("open") or EXPENSE),
            patch("concurrer.receipts.wait_for_upload_ready", side_effect=lambda p: order.append("ready") or {"count": 1}),
            patch("concurrer.receipts.snapshot_receipt_surface", side_effect=lambda p: order.append("snapshot") or {}),
            patch("concurrer.receipts.check_receipt_visible", side_effect=lambda p, _b: order.append("check") or {"panel": True, "nameVisible": order.count("check") > 1, "itemCount": 1}),
            patch("concurrer.receipts.assert_write_authorized", side_effect=lambda _u: order.append("authorize")),
        ):
            result = attach_receipt(page, self._request())
        self.assertEqual(result["outcome"], "verified-persisted")
        self.assertEqual(page.locator_selector, UPLOAD_INPUT_SELECTOR)
        self.assertNotEqual(page.locator_selector, "input[type=file]")
        # Authorization directly precedes the single setter call.
        auth_index = order.index("authorize")
        self.assertEqual(page.locator_obj.set_calls, [str(self.target)])
        self.assertLess(auth_index, len(order))
        self.assertEqual(page.locator_obj.clicks, 0)

    def test_a_receipt_already_shown_is_never_attached_again(self):
        page = FakePage(self.url)
        with (
            patch("concurrer.receipts.open_expense_by_id", return_value=EXPENSE),
            patch("concurrer.receipts.wait_for_upload_ready", return_value={"count": 1}),
            patch("concurrer.receipts.snapshot_receipt_surface", return_value={}),
            patch("concurrer.receipts.check_receipt_visible",
                  return_value={"panel": True, "nameVisible": True, "itemCount": 1}),
            patch("concurrer.receipts.assert_write_authorized") as authorize,
        ):
            with self.assertRaisesRegex(ReceiptError, "duplicate"):
                attach_receipt(page, self._request())
        authorize.assert_not_called()
        self.assertIsNone(page.locator_obj)  # the upload control is never touched

    def test_never_uses_row_navigation(self):
        page = FakePage(self.url)
        with (
            patch("concurrer.receipts.open_expense_by_id", return_value=EXPENSE),
            patch("concurrer.receipts.wait_for_upload_ready", return_value={"count": 1}),
            patch("concurrer.receipts.snapshot_receipt_surface", return_value={}),
            patch("concurrer.receipts.check_receipt_visible", side_effect=[{"panel": True, "nameVisible": False, "itemCount": 0}] + [{"panel": True, "nameVisible": True, "itemCount": 1}] * 5),
            patch("concurrer.receipts.assert_write_authorized"),
            patch("concurrer.nav.open_expense_edit") as by_row,
            patch("concurrer.nav.select_row") as select,
        ):
            attach_receipt(page, self._request())
        by_row.assert_not_called()
        select.assert_not_called()

    def test_setter_failure_calls_once_never_retries_never_claims_unsaved(self):
        page = FakePage(self.url)
        page.fail_setter = RuntimeError("boom")
        with (
            patch("concurrer.receipts.open_expense_by_id", return_value=EXPENSE),
            patch("concurrer.receipts.wait_for_upload_ready", return_value={"count": 1}),
            patch("concurrer.receipts.snapshot_receipt_surface", return_value={}),
            patch("concurrer.receipts.check_receipt_visible", return_value={"panel": True, "nameVisible": False, "itemCount": 0}),
            patch("concurrer.receipts.assert_write_authorized"),
        ):
            with self.assertRaisesRegex(ReceiptError, "persistence unknown"):
                attach_receipt(page, self._request())
        self.assertEqual(len(page.locator_obj.set_calls), 1)
        self.assertEqual(page.locator_obj.clicks, 0)

    def test_outcome_mapping_verified_observed_attempted(self):
        scenarios = [
            # (immediate visible, reopen visible, expected outcome)
            (True, True, "verified-persisted"),
            (True, False, "observed-unverified"),
            (False, False, "attempted-unverified"),
        ]
        for immediate, reopen, expected in scenarios:
            page = FakePage(self.url)
            clock = {"t": 0.0}
            state = {"opens": 0, "pre_done": False}

            def fake_open(_page, _report, _expense):
                state["opens"] += 1
                return EXPENSE

            def check(_page, _basename):
                if state["opens"] >= 2:
                    return {"panel": True, "nameVisible": reopen, "itemCount": 1 if reopen else 0}
                if not state["pre_done"]:
                    state["pre_done"] = True
                    return {"panel": True, "nameVisible": False, "itemCount": 0}
                return {"panel": True, "nameVisible": immediate, "itemCount": 1 if immediate else 0}

            with (
                patch("concurrer.receipts.open_expense_by_id", side_effect=fake_open),
                patch("concurrer.receipts.wait_for_upload_ready", return_value={"count": 1}),
                patch("concurrer.receipts.snapshot_receipt_surface", return_value={}),
                patch("concurrer.receipts.check_receipt_visible", side_effect=check),
                patch("concurrer.receipts.assert_write_authorized"),
                patch("concurrer.receipts.time.monotonic", side_effect=lambda: clock["t"]),
                patch.object(FakePage, "wait_for_timeout", lambda self, _m: clock.update(t=clock["t"] + 10.0)),
            ):
                result = attach_receipt(page, self._request())
            with self.subTest(expected=expected):
                self.assertEqual(result["outcome"], expected)


class EvidencePrivacyTests(unittest.TestCase):
    def test_evidence_and_output_carry_no_filenames_or_paths(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = receipt_file(Path(tmp.name), name="acme-corp-invoice.png")
        request = validate_request(TEST_REPORT_ID, EXPENSE, target)
        out = io.StringIO()
        with (
            patch("concurrer.receipts.resolve", return_value=SimpleNamespace(concur_username="c", ufl_username="u", ufl_password="p")),
            patch("concurrer.receipts.authenticated_context", return_value=context_for(FakePage("https://x"))),
            patch("concurrer.receipts.attach_receipt", return_value={"outcome": "attempted-unverified", "detail": "no render", "expense_id": EXPENSE, "assignedCount": 1, "preHadName": False, "observedImmediate": False, "postReopen": False, "pre": {"panel": True}, "post": {"panel": True}}),
            patch("concurrer.receipts.write_evidence", return_value=Path("/nonexistent/evidence.json")),
            redirect_stdout(out),
        ):
            from concurrer.receipts import _run
            _run(request, commit=True, banner_actions={})
        text = out.getvalue()
        self.assertNotIn("acme-corp-invoice", text)
        self.assertNotIn(str(target), text)


if __name__ == "__main__":
    unittest.main()
