"""Integrity tests for the complete 67-type manual-form capture."""

from __future__ import annotations

import unittest

from concurrer.type_form_map import TYPE_FORM_FIELDS
from concurrer.type_map import TYPE_FIELDS


class TypeFormMapTests(unittest.TestCase):
    def test_has_exactly_the_custom_projection_of_the_existing_type_map(self):
        self.assertEqual(set(TYPE_FORM_FIELDS), set(TYPE_FIELDS))
        for expense_type, customs in TYPE_FIELDS.items():
            observed = [
                (field.name, field.label, field.required)
                for field in TYPE_FORM_FIELDS[expense_type]
                if field.name.startswith("custom")
            ]
            expected = [(field.name, field.label, field.required)
                        for field in customs]
            self.assertEqual(observed, expected, expense_type)

    def test_phone_fax_internet_has_date_but_not_location(self):
        names = {field.name for field in TYPE_FORM_FIELDS["Phone/Fax/Internet"]}
        self.assertIn("transactionDate", names)
        self.assertNotIn("location", names)


if __name__ == "__main__":
    unittest.main()
