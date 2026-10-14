"""Browser-free composition tests for the CSV batch driver.

These deliberately mock the browser seam. The point is to prove orchestration:
CSV identity is resolved before login, dry runs never acquire a write grant or
invoke a mutation helper, every commit is preflighted, and a later failure
reports the truth about already-verified rows.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from concurrer.csvio import FieldChange, PushPlan, RowPlan, Sheet
from concurrer.pushcsv import (
    BatchPlanError,
    BatchPreflight,
    _run,
    batch_token,
    commit_batch,
    format_preflight,
    main,
    preflight_batch,
    request_for,
)
from concurrer.safety import TEST_REPORT_ID, grant_active


EXPENSE_A = "CE96ABA11AB1674AA77FBBC595109CB2"
EXPENSE_B = "077DDCA88361BC4DB005BDDD9BBC3BF4"
REAL_REPORT = "506EC055FE79452BB943"
OTHER_REPORT = "0123456789ABCDEF0123"


def fields(*, comment: str = "before"):
    """Enough real-form shape for normal non-type editor planning."""
    return [
        {
            "name": "expenseType",
            "label": "Expense Type",
            "value": "Ground Transportation",
            "required": True,
            "locked": False,
            "widget": "combobox",
            "validation": "",
        },
        {
            "name": "custom13",
            "label": "Trip Type",
            "value": "In-State",
            "required": True,
            "locked": False,
            "widget": "combobox",
            "validation": "",
        },
        {
            "name": "custom12",
            "label": "Business Purpose",
            "value": "Meeting",
            "required": True,
            "locked": False,
            "widget": "combobox",
            "validation": "",
        },
        {
            "name": "custom20",
            "label": "Type of Ground Transportation",
            "value": "Taxi",
            "required": True,
            "locked": False,
            "widget": "combobox",
            "validation": "",
        },
        {
            "name": "custom29",
            "label": "Were other travelers with you?",
            "value": "No",
            "required": True,
            "locked": False,
            "widget": "combobox",
            "validation": "",
        },
        {
            "name": "custom21",
            "label": "Traveler's myUFL TA/UF GO Request ID",
            "value": "",
            "required": False,
            "locked": False,
            "widget": "input/text",
            "validation": "",
        },
        {
            "name": "comment",
            "label": "Comment",
            "value": comment,
            "required": False,
            "locked": False,
            "widget": "textarea/textarea",
            "validation": "",
        },
    ]


def sheet_for(
    *rows: tuple[str, str],
    report_id: str = TEST_REPORT_ID,
) -> Sheet:
    return Sheet(
        field_columns=("comment",),
        rows=tuple(
            {"expense_id": expense_id, "report_id": report_id, "comment": comment}
            for expense_id, comment in rows
        ),
    )


@contextmanager
def context_for(page):
    yield page


class RequestTests(unittest.TestCase):
    def test_request_uses_stable_id_and_only_changed_assignments(self):
        row = RowPlan(
            EXPENSE_A,
            changes=[FieldChange("comment", "before", "after")],
        )
        request = request_for(row, TEST_REPORT_ID)
        self.assertEqual(request.report_id, TEST_REPORT_ID)
        self.assertEqual(request.expense_id, EXPENSE_A)
        self.assertIsNone(request.row)
        self.assertEqual(request.assignments, {"comment": "after"})
        self.assertTrue(request.save)

    def test_request_refuses_empty_or_blocked_row(self):
        for row in (RowPlan(EXPENSE_A), RowPlan(EXPENSE_A, errors=["locked"])):
            with self.subTest(row=row):
                with self.assertRaises(BatchPlanError):
                    request_for(row, TEST_REPORT_ID)


class PreflightTests(unittest.TestCase):
    def test_preflight_reads_direct_ids_and_builds_live_editor_plan(self):
        sheet = sheet_for((EXPENSE_A, "after"))
        page = object()
        with (
            patch("concurrer.pushcsv.open_expense_by_id", side_effect=lambda *_args: _args[-1]) as open_by_id,
            patch("concurrer.pushcsv.field_inventory", return_value=fields()),
        ):
            preflight = preflight_batch(page, sheet, TEST_REPORT_ID)
        self.assertTrue(preflight.clean)
        self.assertEqual(len(preflight.actionable), 1)
        open_by_id.assert_called_once_with(page, TEST_REPORT_ID, EXPENSE_A)
        plan = preflight.edit_plans[EXPENSE_A]
        self.assertEqual(plan.changes["comment"]["before"], "before")
        self.assertEqual(plan.changes["comment"]["after"], "after")

    def test_preflight_keeps_live_read_failure_and_does_not_hide_plan_errors(self):
        sheet = Sheet(
            field_columns=("comment",),
            rows=(
                {"expense_id": EXPENSE_A, "report_id": TEST_REPORT_ID, "comment": "after"},
                {"expense_id": EXPENSE_B, "report_id": TEST_REPORT_ID, "comment": "after"},
            ),
        )
        page = object()
        with (
            patch(
                "concurrer.pushcsv.open_expense_by_id",
                side_effect=[EXPENSE_A, RuntimeError("route unavailable")],
            ),
            patch("concurrer.pushcsv.field_inventory", return_value=fields()),
        ):
            preflight = preflight_batch(page, sheet, TEST_REPORT_ID)
        self.assertFalse(preflight.clean)
        self.assertIn(EXPENSE_B, preflight.inventory_failures)
        self.assertEqual(len(preflight.actionable), 1)


class TypeChangeBatchTests(unittest.TestCase):
    def test_type_change_row_becomes_an_acknowledged_type_request(self):
        row = RowPlan(EXPENSE_A, changes=[
            FieldChange("expenseType", "Undefined", "Ground Transportation"),
            FieldChange("custom20", "", "Taxi"),
        ])
        request = request_for(row, TEST_REPORT_ID)
        self.assertEqual(request.target_type, "Ground Transportation")
        self.assertEqual(request.expected_type, "Undefined")
        self.assertTrue(request.allow_type_change)
        self.assertEqual(dict(request.assignments), {"custom20": "Taxi"})

    def test_dry_run_lists_type_changes_and_discards_and_token_covers_them(self):
        sheet = sheet_for((EXPENSE_A, "after"))
        rows = [RowPlan(EXPENSE_A, [FieldChange("expenseType", "Ground Transportation",
                                                "Books & Publications")])]
        def preflight(losses):
            plan = SimpleNamespace(current_type="Ground Transportation",
                                   target_type="Books & Publications", losses=losses)
            return BatchPreflight(REAL_REPORT, sheet, PushPlan(row_plans=rows),
                                  edit_plans={EXPENSE_A: plan})
        loss = [{"name": "custom20", "label": "Type of Ground Transportation",
                 "value": "Taxi"}]
        text = format_preflight(preflight(loss))
        self.assertIn("'Ground Transportation' -> 'Books & Publications'", text)
        self.assertIn("DISCARDS Type of Ground Transportation (custom20) = 'Taxi'", text)
        self.assertNotEqual(batch_token(preflight(loss)), batch_token(preflight([])))


class DryRunTests(unittest.TestCase):
    def test_clean_dry_run_prints_diff_but_never_persists(self):
        sheet = sheet_for((EXPENSE_A, "after"))
        preflight = BatchPreflight(
            TEST_REPORT_ID,
            sheet,
            PushPlan(row_plans=[RowPlan(EXPENSE_A, [FieldChange("comment", "before", "after")])]),
            edit_plans={EXPENSE_A: object()},
        )
        out = io.StringIO()
        with (
            patch(
                "concurrer.pushcsv.resolve",
                return_value=SimpleNamespace(
                    concur_username="concur", ufl_username="ufl", ufl_password="password"
                ),
            ),
            patch("concurrer.pushcsv.authenticated_context", return_value=context_for(object())),
            patch("concurrer.pushcsv.preflight_batch", return_value=preflight),
            patch("concurrer.pushcsv.commit_batch") as commit,
            redirect_stdout(out),
        ):
            status = _run(sheet, TEST_REPORT_ID, commit=False, banner_actions={})
        self.assertEqual(status, 0)
        self.assertIn("comment: 'before' -> 'after'", out.getvalue())
        self.assertIn("Dry run only", out.getvalue())
        commit.assert_not_called()
        self.assertFalse(grant_active())

    def _real_preflight(self, after="after"):
        sheet = sheet_for((EXPENSE_A, after))
        return sheet, BatchPreflight(
            REAL_REPORT,
            sheet,
            PushPlan(row_plans=[RowPlan(EXPENSE_A, [FieldChange("comment", "before", after)])]),
            edit_plans={EXPENSE_A: object()},
        )

    def _run_real(self, preflight, sheet, **kwargs):
        out = io.StringIO()
        seen = []
        with (
            patch(
                "concurrer.pushcsv.resolve",
                return_value=SimpleNamespace(
                    concur_username="concur", ufl_username="ufl", ufl_password="password"
                ),
            ),
            patch("concurrer.pushcsv.authenticated_context", return_value=context_for(object())),
            patch("concurrer.pushcsv.preflight_batch", return_value=preflight),
            patch("concurrer.pushcsv.commit_batch",
                  side_effect=lambda *_a: seen.append(grant_active()) or 0) as commit,
            redirect_stdout(out),
        ):
            status = _run(sheet, REAL_REPORT, banner_actions={}, **kwargs)
        return status, out.getvalue(), commit, seen

    def test_real_report_dry_run_prints_plan_token_and_never_commits(self):
        sheet, preflight = self._real_preflight()
        status, out, commit, _ = self._run_real(preflight, sheet, commit=False)
        self.assertEqual(status, 0)
        self.assertIn(f"PLAN TOKEN: {batch_token(preflight)}", out)
        commit.assert_not_called()
        self.assertFalse(grant_active())

    def test_real_report_commit_with_matching_token_grants_then_revokes(self):
        sheet, preflight = self._real_preflight()
        status, _, commit, seen = self._run_real(
            preflight, sheet, commit=True, confirm=batch_token(preflight))
        self.assertEqual(status, 0)
        commit.assert_called_once()
        self.assertEqual(seen, [True])
        self.assertFalse(grant_active())

    def test_real_report_commit_refuses_a_token_for_a_different_plan(self):
        _, reviewed = self._real_preflight("after")
        sheet, drifted = self._real_preflight("something else")
        with self.assertRaises(AssertionError):
            self._run_real(drifted, sheet, commit=True, confirm=batch_token(reviewed))
        self.assertFalse(grant_active())

    def test_blocked_batch_never_calls_commit_even_when_other_row_is_valid(self):
        sheet = sheet_for((EXPENSE_A, "after"), (EXPENSE_B, "also after"))
        push = PushPlan(
            row_plans=[
                RowPlan(EXPENSE_A, [FieldChange("comment", "before", "after")]),
                RowPlan(EXPENSE_B, errors=["comment is locked"]),
            ]
        )
        preflight = BatchPreflight(TEST_REPORT_ID, sheet, push)
        with (
            patch(
                "concurrer.pushcsv.resolve",
                return_value=SimpleNamespace(
                    concur_username="concur", ufl_username="ufl", ufl_password="password"
                ),
            ),
            patch("concurrer.pushcsv.authenticated_context", return_value=context_for(object())),
            patch("concurrer.pushcsv.preflight_batch", return_value=preflight),
            patch("concurrer.pushcsv.commit_batch") as commit,
        ):
            self.assertEqual(_run(sheet, TEST_REPORT_ID, commit=True, banner_actions={}), 2)
        commit.assert_not_called()


class CommitTests(unittest.TestCase):
    def _preflight(self, ids=(EXPENSE_A,)):
        sheet = sheet_for(*[(expense_id, "after") for expense_id in ids])
        rows = [
            RowPlan(expense_id, [FieldChange("comment", "before", "after")])
            for expense_id in ids
        ]
        return BatchPreflight(
            TEST_REPORT_ID,
            sheet,
            PushPlan(row_plans=rows),
            edit_plans={expense_id: object() for expense_id in ids},
        )

    def test_clean_commit_replans_and_persists_in_csv_order(self):
        preflight = self._preflight((EXPENSE_A, EXPENSE_B))
        writes = []
        with (
            patch("concurrer.pushcsv._replan_for_write", side_effect=[(object(), fields()), (object(), fields())]) as replan,
            patch("concurrer.pushcsv.persist_and_verify_plan", side_effect=lambda _p, _r, eid, *_a, **_k: writes.append(eid)),
        ):
            self.assertEqual(commit_batch(object(), preflight), 0)
        self.assertEqual(writes, [EXPENSE_A, EXPENSE_B])
        self.assertEqual([call.args[2].expense_id for call in replan.call_args_list], [EXPENSE_A, EXPENSE_B])

    def test_write_time_drift_halts_before_any_save(self):
        preflight = self._preflight()
        err = io.StringIO()
        with (
            patch("concurrer.pushcsv._replan_for_write", side_effect=BatchPlanError("live drift")),
            patch("concurrer.pushcsv.persist_and_verify_plan") as persist,
            redirect_stderr(err),
        ):
            self.assertEqual(commit_batch(object(), preflight), 1)
        persist.assert_not_called()
        self.assertIn("before Save was attempted", err.getvalue())

    def test_later_failure_names_prior_verified_rows_and_stops(self):
        preflight = self._preflight((EXPENSE_A, EXPENSE_B))
        err = io.StringIO()

        attempts = 0

        def persist(*_args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                kwargs["on_save_attempt"]()
                raise RuntimeError("readback failed")

        with (
            patch("concurrer.pushcsv._replan_for_write", side_effect=[(object(), fields()), (object(), fields())]),
            patch("concurrer.pushcsv.persist_and_verify_plan", side_effect=persist) as persist_mock,
            redirect_stderr(err),
        ):
            self.assertEqual(commit_batch(object(), preflight), 1)
        self.assertEqual(persist_mock.call_count, 2)
        self.assertIn(EXPENSE_A, err.getvalue())
        self.assertIn("may have saved", err.getvalue())


class MainBoundaryTests(unittest.TestCase):
    def _csv(self, report_id: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "input.csv"
        path.write_text(
            f"expense_id,report_id,comment\n{EXPENSE_A},{report_id},after\n",
            encoding="utf-8",
        )
        return path

    def test_confirmed_report_commit_without_token_fails_before_creds_or_browser(self):
        path = self._csv(REAL_REPORT)
        err = io.StringIO()
        argv = ["pushcsv", "--csv", str(path), "--commit"]
        with (
            patch.object(sys, "argv", argv),
            patch("concurrer.pushcsv.resolve") as resolve_creds,
            patch("concurrer.pushcsv.authenticated_context") as auth,
            redirect_stderr(err),
        ):
            self.assertEqual(main(), 2)
        resolve_creds.assert_not_called()
        auth.assert_not_called()
        self.assertIn("without --confirm", err.getvalue())

    def test_confirmed_report_commit_passes_token_to_run_without_a_grant(self):
        path = self._csv(REAL_REPORT)
        argv = ["pushcsv", "--csv", str(path), "--commit", "--confirm", "abc123abc123"]
        with patch.object(sys, "argv", argv), patch("concurrer.pushcsv._run", return_value=0) as run:
            self.assertEqual(main(), 0)
        # The grant is taken inside _run only after the live token matches.
        self.assertFalse(grant_active())
        self.assertEqual(run.call_args.kwargs["confirm"], "abc123abc123")
        self.assertTrue(run.call_args.kwargs["commit"])

    def test_other_real_report_commit_fails_before_creds_or_browser(self):
        path = self._csv(OTHER_REPORT)
        err = io.StringIO()
        argv = ["pushcsv", "--csv", str(path), "--commit", "--confirm", "abc123abc123"]
        with (
            patch.object(sys, "argv", argv),
            patch("concurrer.pushcsv.resolve") as resolve_creds,
            patch("concurrer.pushcsv.authenticated_context") as auth,
            redirect_stderr(err),
        ):
            self.assertEqual(main(), 2)
        resolve_creds.assert_not_called()
        auth.assert_not_called()
        self.assertIn("only the throwaway test report", err.getvalue())

    def test_test_report_commit_enters_then_revokes_grant_before_run(self):
        path = self._csv(TEST_REPORT_ID)
        argv = ["pushcsv", "--csv", str(path), "--commit"]
        with patch.object(sys, "argv", argv), patch("concurrer.pushcsv._run", return_value=0) as run:
            self.assertEqual(main(), 0)
        self.assertFalse(grant_active())
        self.assertEqual(run.call_args.kwargs["commit"], True)
        self.assertEqual(run.call_args.args[1], TEST_REPORT_ID)


if __name__ == "__main__":
    unittest.main()
