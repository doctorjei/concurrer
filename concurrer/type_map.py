"""Versioned UF expense-type custom-field schema.

The mapping was captured from the live Test Report create form on 2026-09-21:
67 types, zero failures. It is checked into source rather than loaded from the
operator's private discovery directory so edit planning has deployable,
reviewable reference data. Live form state remains authoritative at execution.

A ``customNN`` slot has no standalone meaning. Helpers here always compare the
slot together with its normalized label before treating values as compatible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class TypeField:
    """A custom field observed for one expense type."""

    name: str
    label: str
    required: bool


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _load() -> dict[str, tuple[TypeField, ...]]:
    raw = json.loads(Path(__file__).with_name("type_map.json").read_text())
    if len(raw) != 67:
        raise RuntimeError(f"type map is incomplete: expected 67 types, got {len(raw)}")
    return {
        expense_type: tuple(TypeField(**field) for field in fields)
        for expense_type, fields in raw.items()
    }


TYPE_FIELDS: Mapping[str, tuple[TypeField, ...]] = _load()


def schema_for(expense_type: str) -> tuple[TypeField, ...]:
    """Return the known custom-field schema or fail closed for an unknown type."""
    try:
        return TYPE_FIELDS[expense_type]
    except KeyError as exc:
        raise ValueError(f"expense type {expense_type!r} is not in the 67-type map") from exc


def known_type(expense_type: str) -> bool:
    return expense_type in TYPE_FIELDS


def field_for(expense_type: str, name: str) -> TypeField | None:
    return next((field for field in schema_for(expense_type) if field.name == name), None)


def compatible_custom_field(
    source_type: str,
    source_name: str,
    target_type: str,
) -> bool:
    """Whether a custom field keeps the same slot *and* semantic label."""
    source = field_for(source_type, source_name)
    target = field_for(target_type, source_name)
    return source is not None and target is not None and _norm(source.label) == _norm(target.label)


def type_change_losses(
    source_type: str,
    current_fields: list[dict],
    target_type: str,
) -> list[dict]:
    """Nonblank custom values that cannot safely carry into a target type."""
    schema_for(source_type)
    schema_for(target_type)
    return [
        {
            "name": field["name"],
            "label": field["label"],
            "value": field["value"],
        }
        for field in current_fields
        if field["name"].startswith("custom")
        and bool(str(field.get("value", "")).strip())
        and not compatible_custom_field(source_type, field["name"], target_type)
    ]


def target_required_fields(expense_type: str) -> tuple[TypeField, ...]:
    return tuple(field for field in schema_for(expense_type) if field.required)


def conditional_errors(expense_type: str, values: Mapping[str, str]) -> list[str]:
    """Known option-level Concur requirements not represented by required flags."""
    if (
        expense_type == "Ground Transportation"
        and _norm(values.get("custom20", "")) == _norm("Other (Comment Required)")
        and not str(values.get("comment", "")).strip()
    ):
        return ["Ground Transportation 'Other (Comment Required)' requires comment"]
    return []
