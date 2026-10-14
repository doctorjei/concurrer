"""Browser-free edit-plan validation tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from concurrer.edit import (
    EditRequest,
    PlanError,
    build_edit_plan,
    parse_assignment,
    persist_and_verify_plan,
    validate_before_save,
    _apply_value,
)


def form_field(
    name: str,
    label: str,
    value: str,
    *,
    required: bool = False,
    locked: bool = False,
    widget: str = "combobox",
    validation: str = "",
) -> dict:
    return {
        "name": name,
        "label": label,
        "value": value,
        "required": required,
        "locked": locked,
        "widget": widget,
        "validation": validation,
    }


def ground_transportation_fields(*, comment: str = "note", custom20: str = "Taxi") -> list[dict]:
    return [
        form_field("expenseType", "Expense Type", "Ground Transportation", required=True),
        form_field("custom13", "Trip Type", "In-State", required=True),
        form_field("custom12", "Business Purpose", "Meeting", required=True),
        form_field("custom20", "Type of Ground Transportation", custom20, required=True),
        form_field("custom29", "Were other travelers with you?", "No", required=True),
        form_field("custom21", "Traveler's myUFL TA/UF GO Request ID", ""),
        form_field("comment", "Comment", comment, widget="textarea/text"),
    ]


class EditPlanTests(unittest.TestCase):
    def test_assignment_syntax_rejects_clearing_and_type_bypass(self) -> None:
        self.assertEqual(parse_assignment("comment=ready"), ("comment", "ready"))
        for raw in ("comment=", "bad-name=value", "expenseType=Airfare", "comment"):
            with self.subTest(raw=raw):
                with self.assertRaises(PlanError):
                    parse_assignment(raw)

    def test_type_change_identifies_noncompatible_custom_value_losses(self) -> None:
        request = EditRequest(
            report_id="TEST",
            expense_id="ABCDEF",
            row=None,
            assignments={"custom12": "Conference"},
            target_type="Books & Publications",
            expected_type="Ground Transportation",
            allow_type_change=True,
        )
        plan = build_edit_plan(request, ground_transportation_fields())
        self.assertEqual(plan.target_type, "Books & Publications")
        self.assertEqual({loss["name"] for loss in plan.losses}, {"custom20", "custom29"})

    def test_type_change_requires_matching_expectation_and_acknowledgement(self) -> None:
        fields = ground_transportation_fields()
        for expected, acknowledged in ((None, True), ("Airfare", True), ("Ground Transportation", False)):
            with self.subTest(expected=expected, acknowledged=acknowledged):
                request = EditRequest(
                    report_id="TEST",
                    expense_id="ABCDEF",
                    row=None,
                    assignments={},
                    target_type="Books & Publications",
                    expected_type=expected,
                    allow_type_change=acknowledged,
                )
                with self.assertRaises(PlanError):
                    build_edit_plan(request, fields)

    def test_locked_and_unknown_fields_are_rejected(self) -> None:
        fields = ground_transportation_fields()
        fields[-1]["locked"] = True
        for assignment in ({"comment": "new"}, {"doesNotExist": "new"}):
            with self.subTest(assignment=assignment):
                request = EditRequest("TEST", "ABCDEF", None, assignment)
                with self.assertRaises(PlanError):
                    build_edit_plan(request, fields)

    def test_request_picker_is_plannable(self) -> None:
        fields = ground_transportation_fields()
        fields.append(form_field("travelRequestExpenseId", "Request", "None"))
        request = EditRequest("TEST", "ABCDEF", None,
                              {"travelRequestExpenseId": "05/04/2026, $800.00 - CCED"})
        plan = build_edit_plan(request, fields)
        self.assertEqual(plan.changes["travelRequestExpenseId"]["before"], "None")

    def test_unwritable_fields_are_refused_at_plan_time(self) -> None:
        fields = ground_transportation_fields()
        request = EditRequest("TEST", "ABCDEF", None, {"comment": "x"})
        with patch.dict("concurrer.edit.UNWRITABLE_FIELDS", {"comment": "no helper"}):
            with self.assertRaisesRegex(PlanError, "no helper"):
                build_edit_plan(request, fields)

    def test_known_conditional_requirement_blocks_save(self) -> None:
        fields = ground_transportation_fields(comment="", custom20="Other (Comment Required)")
        request = EditRequest("TEST", "ABCDEF", None, {"custom20": "Other (Comment Required)"})
        plan = build_edit_plan(request, fields)
        with self.assertRaisesRegex(PlanError, "requires comment"):
            validate_before_save(fields, plan)

    def test_live_only_plan_allows_an_unknown_current_type(self) -> None:
        fields = [
            form_field("expenseType", "Expense Type", "New Concur Type", required=True),
            form_field("comment", "Comment", "before", widget="textarea/text"),
        ]
        plan = build_edit_plan(
            EditRequest("TEST", "ABCDEF", None, {"comment": "after"}),
            fields,
        )
        self.assertEqual(plan.current_type, "New Concur Type")
        self.assertEqual(plan.changes["comment"]["after"], "after")


class PersistAndVerifyTests(unittest.TestCase):
    def _page(self):
        class First:
            def is_visible(self):
                return True

        class Save:
            first = First()

            def count(self):
                return 1

        class Page:
            def __init__(self):
                self.events = []

            def locator(self, selector):
                self.events.append(("locator", selector))
                return Save()

            def wait_for_timeout(self, millis):
                self.events.append(("wait", millis))

        return Page()

    def test_shared_persist_sequence_is_apply_validate_save_verify(self):
        page = self._page()
        events = page.events
        before = ground_transportation_fields()
        plan = object()
        with (
            patch("concurrer.edit.apply_plan", side_effect=lambda *_: events.append("apply") or before),
            patch("concurrer.edit.validate_before_save", side_effect=lambda *_: events.append("validate")),
            patch("concurrer.edit.click_safe", side_effect=lambda *_: events.append("click")),
            patch("concurrer.edit.verify_saved_expense", side_effect=lambda *_: events.append("verify")),
        ):
            persist_and_verify_plan(page, "REPORT", "EXPENSE", plan, before)
        self.assertEqual(
            events,
            [
                "apply",
                "validate",
                ("locator", '[data-nuiexp="exp-save-expense"]'),
                "click",
                ("wait", 7000),
                "verify",
            ],
        )

    def test_validation_failure_never_reaches_save(self):
        page = self._page()
        before = ground_transportation_fields()
        with (
            patch("concurrer.edit.apply_plan", return_value=before),
            patch("concurrer.edit.validate_before_save", side_effect=PlanError("bad form")),
            patch("concurrer.edit.click_safe") as click,
        ):
            with self.assertRaisesRegex(PlanError, "bad form"):
                persist_and_verify_plan(page, "REPORT", "EXPENSE", object(), before)
        click.assert_not_called()
        self.assertEqual(page.events, [])


if __name__ == "__main__":
    unittest.main()


class ApplyValueTests(unittest.TestCase):
    def test_location_uses_exact_search_selection(self):
        field = form_field("location", "City of Purchase", "")
        with (
            patch("concurrer.edit.select_search_combobox_exact", return_value="Kyoto, JAPAN") as search,
            patch("concurrer.edit.select_combobox_exact") as fixed,
            patch("concurrer.edit.fill_text_exact") as text,
        ):
            _apply_value(None, field, "Kyoto, JAPAN")
        search.assert_called_once_with(None, field, "Kyoto, JAPAN")
        fixed.assert_not_called()
        text.assert_not_called()

    def test_unstable_location_read_is_set_not_skipped(self):
        field = form_field("location", "City of Purchase", "Kyoto, JAPAN")
        with (
            patch("concurrer.edit.select_search_combobox_exact") as search,
            patch("concurrer.edit.select_combobox_exact") as fixed,
            patch("concurrer.edit.fill_text_exact") as text,
        ):
            _apply_value(None, field, "Kyoto, JAPAN")
        search.assert_called_once_with(None, field, "Kyoto, JAPAN")
        fixed.assert_not_called()
        text.assert_not_called()

    def test_unstable_payment_read_is_set_not_skipped(self):
        field = form_field("paymentType", "Payment Type", "Personal Funds")
        with (
            patch("concurrer.edit.select_payment_type_exact",
                  return_value="Personal Funds") as payment,
            patch("concurrer.edit.select_search_combobox_exact") as search,
            patch("concurrer.edit.select_combobox_exact") as fixed,
            patch("concurrer.edit.fill_text_exact") as text,
        ):
            _apply_value(None, field, "Personal Funds")
        payment.assert_called_once_with(None, field, "Personal Funds")
        search.assert_not_called()
        fixed.assert_not_called()
        text.assert_not_called()

    def test_payment_type_uses_its_container_helper(self):
        field = form_field("paymentType", "Payment Type", "")
        with (
            patch("concurrer.edit.select_search_combobox_exact") as search,
            patch("concurrer.edit.select_payment_type_exact",
                  return_value="Personal Funds") as payment,
            patch("concurrer.edit.select_combobox_exact") as fixed,
            patch("concurrer.edit.fill_text_exact") as text,
        ):
            _apply_value(None, field, "Personal Funds")
        payment.assert_called_once_with(None, field, "Personal Funds")
        search.assert_not_called()
        fixed.assert_not_called()
        text.assert_not_called()
