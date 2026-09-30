"""Browser-free edit-plan validation tests."""

from __future__ import annotations

import unittest

from concurrer.edit import EditRequest, PlanError, build_edit_plan, parse_assignment, validate_before_save


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


if __name__ == "__main__":
    unittest.main()
