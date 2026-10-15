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
    apply_plan,
    wait_for_type_cascade,
    TypeChangeConfirmation,
    is_attendee_removal,
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

    def _undefined_fields(self) -> list[dict]:
        return [
            form_field("expenseType", "Expense Type", "Undefined", required=True),
            form_field("location", "City of Purchase", "Osaka, JAPAN"),
            form_field("comment", "Comment", "", widget="textarea/text"),
        ]

    def _undefined_to_gt(self, assignments) -> EditRequest:
        return EditRequest("TEST", "ABCDEF", None, assignments,
                           target_type="Ground Transportation",
                           expected_type="Undefined", allow_type_change=True)

    def test_custom_free_unmapped_source_can_change_type(self) -> None:
        # "Undefined" is not a selectable type, so it is not in the map; with
        # no custom fields there is nothing type-specific to lose.
        plan = build_edit_plan(self._undefined_to_gt({
            "custom20": "Taxi",
            "travelRequestExpenseId": "05/04/2026, $800.00 - CCED",
        }), self._undefined_fields())
        self.assertEqual(plan.losses, [])
        self.assertEqual(plan.changes["travelRequestExpenseId"]["before"],
                         "<appears after type change>")

    def test_standard_values_missing_from_target_form_are_reported(self) -> None:
        plan = build_edit_plan(self._undefined_to_gt({"custom20": "Taxi"}),
                               self._undefined_fields())
        # The blank Ground Transportation form has no Location.
        self.assertEqual([(l["name"], l["value"]) for l in plan.possible_losses],
                         [("location", "Osaka, JAPAN")])

    def test_unmapped_source_with_custom_fields_is_refused(self) -> None:
        fields = self._undefined_fields() + [form_field("custom7", "Mystery", "x")]
        with self.assertRaisesRegex(PlanError, "not in the 67-type map"):
            build_edit_plan(self._undefined_to_gt({"custom20": "Taxi"}), fields)

    def test_deferred_field_missing_after_type_switch_stops_before_save(self) -> None:
        plan = build_edit_plan(self._undefined_to_gt({
            "travelRequestExpenseId": "05/04/2026, $800.00 - CCED"}), self._undefined_fields())
        after_switch = ground_transportation_fields()  # no Request on this form
        with (
            patch("concurrer.edit.select_combobox_exact"),
            patch("concurrer.edit.wait_for_type_cascade", return_value=after_switch),
            patch("concurrer.edit.field_inventory", return_value=after_switch),
            patch("concurrer.edit.validate_target_schema"),
            patch("concurrer.edit._apply_value") as apply_value,
        ):
            with self.assertRaisesRegex(PlanError, "nothing was saved"):
                apply_plan(object(), plan, self._undefined_fields())
        apply_value.assert_not_called()

    def test_cascade_wait_ignores_a_stale_old_type_form(self) -> None:
        # The old GT customs still showing under the new type name is stale.
        stale = ground_transportation_fields()
        stale[0] = form_field("expenseType", "Expense Type", "Books & Publications")
        rendered = [form_field("expenseType", "Expense Type", "Books & Publications"),
                    *[form_field(f.name, f.label, "", required=f.required)
                      for f in __import__("concurrer.type_map", fromlist=["x"]).schema_for(
                          "Books & Publications")]]
        page = type("P", (), {"waits": [], "wait_for_timeout": lambda self, ms: self.waits.append(ms)})()
        with (
            patch("concurrer.edit.blocking_dialog_text", return_value=None),
            patch("concurrer.edit.field_inventory", side_effect=[stale, stale, rendered]),
            patch("concurrer.edit.settled_inventory", return_value=rendered),
        ):
            self.assertEqual(wait_for_type_cascade(page, "Books & Publications"), rendered)
        self.assertEqual(len(page.waits), 2)

    def test_cascade_wait_fails_loudly_if_the_form_never_rerenders(self) -> None:
        stale = ground_transportation_fields()
        stale[0] = form_field("expenseType", "Expense Type", "Books & Publications")
        page = type("P", (), {"wait_for_timeout": lambda self, ms: None})()
        with (
            patch("concurrer.edit.blocking_dialog_text", return_value=None),
            patch("concurrer.edit.field_inventory", return_value=stale),
        ):
            with self.assertRaisesRegex(PlanError, "did not re-render"):
                wait_for_type_cascade(page, "Books & Publications", max_reads=3)

    def test_cascade_wait_never_answers_a_confirmation(self) -> None:
        page = type("P", (), {"wait_for_timeout": lambda self, ms: None})()
        text = "Confirm\nThe following attendee types ... will be removed.\nEmployee"
        with (
            patch("concurrer.edit.blocking_dialog_text", return_value=text),
            patch("concurrer.edit.field_inventory") as inventory,
        ):
            with self.assertRaises(TypeChangeConfirmation) as caught:
                wait_for_type_cascade(page, "Books & Publications")
        self.assertIn("attendee types", caught.exception.text)
        self.assertIn("nothing was saved", str(caught.exception))
        inventory.assert_not_called()

    ATTENDEE_PROMPT = ("Confirm\nThe following attendee types are not valid for this "
                       "expense and will be removed.\nEmployee\nContinue\nCancel")

    def test_only_the_exact_attendee_prompt_is_recognised(self) -> None:
        self.assertTrue(is_attendee_removal(self.ATTENDEE_PROMPT))
        for other in (
            "Confirm\nDelete this expense?\nContinue\nCancel",
            self.ATTENDEE_PROMPT.replace("removed.", "removed. Also receipts."),
            self.ATTENDEE_PROMPT + "\n---\nSecond dialog",
            "Confirm\nThe following attendee types are not valid for this expense "
            "and will be removed.\nContinue\nCancel",   # no attendee listed
        ):
            with self.subTest(other=other):
                self.assertFalse(is_attendee_removal(other))

    def _gt_to_books(self, accept: bool):
        request = EditRequest("TEST", "ABCDEF", None, {},
                              target_type="Books & Publications",
                              expected_type="Ground Transportation",
                              allow_type_change=True, accept_attendee_removal=accept)
        return build_edit_plan(request, ground_transportation_fields())

    def test_attendee_prompt_is_continued_only_when_accepted(self) -> None:
        rendered = [form_field("expenseType", "Expense Type", "Books & Publications")]
        for accept in (True, False):
            with self.subTest(accept=accept):
                plan = self._gt_to_books(accept)
                with (
                    patch("concurrer.edit.select_combobox_exact"),
                    patch("concurrer.edit.wait_for_type_cascade",
                          side_effect=[TypeChangeConfirmation(self.ATTENDEE_PROMPT), rendered]),
                    patch("concurrer.edit.continue_blocking_dialog") as cont,
                    patch("concurrer.edit.validate_target_schema"),
                    patch("concurrer.edit._restore_compatible_customs"),
                    patch("concurrer.edit.field_inventory", return_value=rendered),
                ):
                    if accept:
                        apply_plan(object(), plan, ground_transportation_fields())
                        cont.assert_called_once()
                    else:
                        with self.assertRaises(TypeChangeConfirmation):
                            apply_plan(object(), plan, ground_transportation_fields())
                        cont.assert_not_called()

    def test_acceptance_never_continues_a_different_prompt(self) -> None:
        plan = self._gt_to_books(True)
        with (
            patch("concurrer.edit.select_combobox_exact"),
            patch("concurrer.edit.wait_for_type_cascade",
                  side_effect=TypeChangeConfirmation("Confirm\nDelete receipts?\nContinue\nCancel")),
            patch("concurrer.edit.continue_blocking_dialog") as cont,
        ):
            with self.assertRaises(TypeChangeConfirmation):
                apply_plan(object(), plan, ground_transportation_fields())
        cont.assert_not_called()

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

    def test_amount_readback_ignores_thousands_separators(self) -> None:
        from concurrer.edit import _same_value
        self.assertTrue(_same_value("transactionAmount", "10,000", "10000"))
        self.assertFalse(_same_value("transactionAmount", "10,001", "10000"))
        self.assertFalse(_same_value("comment", "10,000", "10000"))

    def test_waits_for_concur_to_recompute_exchange_fields(self) -> None:
        from concurrer.edit import _await_currency_recalc
        empty = [form_field("exchangeRate", "Exchange Rate", ""),
                 form_field("postedAmount", "Amount in USD", "")]
        filled = [form_field("exchangeRate", "Exchange Rate", "0.0064"),
                  form_field("postedAmount", "Amount in USD", "64.10")]
        page = type("P", (), {"waits": [], "wait_for_timeout": lambda self, ms: self.waits.append(ms)})()
        with patch("concurrer.edit.field_inventory", side_effect=[empty, filled]):
            self.assertEqual(_await_currency_recalc(page, empty), filled)
        self.assertEqual(len(page.waits), 2)

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
