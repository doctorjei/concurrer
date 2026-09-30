"""Tests for the checked-in type schema and semantic compatibility rules."""

from __future__ import annotations

import unittest

from concurrer.type_map import TYPE_FIELDS, compatible_custom_field, schema_for


class TypeMapTests(unittest.TestCase):
    def test_complete_map_and_reused_slot_semantics(self) -> None:
        self.assertEqual(len(TYPE_FIELDS), 67)
        self.assertEqual(
            next(field.label for field in schema_for("Ground Transportation") if field.name == "custom20"),
            "Type of Ground Transportation",
        )
        self.assertEqual(
            next(field.label for field in schema_for("Airfare") if field.name == "custom20"),
            "Type of Class",
        )
        self.assertEqual(
            next(field.label for field in schema_for("Car Rental") if field.name == "custom20"),
            "Rental Class",
        )

    def test_slot_requires_same_semantic_label_to_be_compatible(self) -> None:
        self.assertFalse(
            compatible_custom_field("Ground Transportation", "custom20", "Airfare")
        )
        self.assertTrue(
            compatible_custom_field("Ground Transportation", "custom12", "Airfare")
        )


if __name__ == "__main__":
    unittest.main()
