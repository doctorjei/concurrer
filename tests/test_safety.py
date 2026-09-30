"""Regression tests for Concur's structural safety boundary."""

from __future__ import annotations

import unittest

from concurrer import safety

TEST_URL = (
    "https://us2.concursolutions.com/nui/expense/reports/"
    f"{safety.TEST_REPORT_ID}/expenses/ABCDEF"
)
REAL_URL = "https://us2.concursolutions.com/nui/expense/reports/DEADBEEF/expenses/ABCDEF"


class SafetyTests(unittest.TestCase):
    def tearDown(self) -> None:
        safety.revoke_writes()

    def test_non_test_report_cannot_be_authorized(self) -> None:
        with self.assertRaises(AssertionError):
            safety.authorize_writes("DEADBEEF")
        self.assertFalse(safety.grant_active())

    def test_save_requires_test_grant_and_exact_live_url(self) -> None:
        with self.assertRaises(AssertionError):
            safety.assert_safe("Save Expense", "exp-save-expense", url=TEST_URL)

        safety.authorize_writes(safety.TEST_REPORT_ID)
        safety.assert_safe("Save Expense", "exp-save-expense", url=TEST_URL)
        with self.assertRaises(AssertionError):
            safety.assert_safe("Save Expense", "exp-save-expense", url=REAL_URL)
        with self.assertRaises(AssertionError):
            safety.assert_safe(
                "Save Expense",
                "exp-save-expense",
                url="https://attacker.example/nui/expense/reports/"
                f"{safety.TEST_REPORT_ID}",
            )

    def test_never_actions_stay_blocked_with_grant(self) -> None:
        safety.authorize_writes(safety.TEST_REPORT_ID)
        for action in safety.NEVER_WORDS:
            with self.subTest(action=action):
                with self.assertRaises(AssertionError):
                    safety.assert_safe("benign", f"exp-{action}-expense", url=TEST_URL)
                with self.assertRaises(AssertionError):
                    safety.assert_safe(f"Save and {action}", "", url=TEST_URL)

    def test_only_label_based_menu_trigger_bypasses_save_grant(self) -> None:
        safety.assert_safe("More Save Options", "", is_menu_trigger=True, url=REAL_URL)
        with self.assertRaises(AssertionError):
            safety.assert_safe(
                "More Save Options",
                "exp-save-expense",
                is_menu_trigger=True,
                url=REAL_URL,
            )

    def test_grant_context_always_revokes(self) -> None:
        with self.assertRaises(RuntimeError):
            with safety.test_report_write_grant(safety.TEST_REPORT_ID):
                self.assertTrue(safety.grant_active())
                raise RuntimeError("stop")
        self.assertFalse(safety.grant_active())

    def test_actual_target_text_cannot_be_hidden_by_caller_label(self) -> None:
        class Element:
            def get_attribute(self, _name: str) -> str:
                return ""

            def inner_text(self) -> str:
                return "Delete Expense"

            def evaluate(self, _script: str) -> str:
                return ""

        class Locator:
            first = Element()
            clicked = False

            def click(self, **_kwargs) -> None:
                self.clicked = True

        class Page:
            url = TEST_URL

        locator = Locator()
        with self.assertRaises(AssertionError):
            safety.click_safe(Page(), locator, "Edit")
        self.assertFalse(locator.clicked)

    def test_report_path_is_anchored_but_accepts_valid_query_forms(self) -> None:
        report = safety.TEST_REPORT_ID
        self.assertEqual(
            safety.report_in_url(
                f"https://us2.concursolutions.com/nui/expense/reports/{report}?modal=add-expense"
            ),
            report,
        )
        self.assertIsNone(
            safety.report_in_url(
                f"https://us2.concursolutions.com/other/expense/reports/{report}"
            )
        )

    def test_inspection_failure_never_falls_through_to_click(self) -> None:
        class Element:
            def get_attribute(self, name: str) -> str:
                return "exp-submit-expense" if name == "data-nuiexp" else ""

            def inner_text(self) -> str:
                raise RuntimeError("detached")

        class Locator:
            first = Element()
            clicked = False

            def click(self, **_kwargs) -> None:
                self.clicked = True

        class Page:
            url = TEST_URL

        locator = Locator()
        with self.assertRaisesRegex(RuntimeError, "unable to inspect"):
            safety.click_safe(Page(), locator, "Edit")
        self.assertFalse(locator.clicked)

    def test_aria_labelledby_is_guarded_as_an_accessible_name(self) -> None:
        class Element:
            def get_attribute(self, name: str) -> str:
                return "danger-label" if name == "aria-labelledby" else ""

            def inner_text(self) -> str:
                return ""

            def evaluate(self, _script: str) -> str:
                return "Delete Expense"

        class Locator:
            first = Element()
            clicked = False

            def click(self, **_kwargs) -> None:
                self.clicked = True

        class Page:
            url = TEST_URL

        locator = Locator()
        with self.assertRaises(AssertionError):
            safety.click_safe(Page(), locator, "Edit")
        self.assertFalse(locator.clicked)


if __name__ == "__main__":
    unittest.main()
