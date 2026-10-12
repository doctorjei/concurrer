"""Versioned full expense-form schema observed on the Test Report create form.

Unlike :mod:`type_map`, which deliberately contains only type-specific custom
fields used by the edit safety model, this map records every named form field.
It lets the workbook distinguish a field known to apply to a type (for example
Date on Phone/Fax/Internet) from a field merely absent in the current report.

The capture is a manual-expense form. Card-feed provenance can change a field's
lock state and may expose a different vendor control; a live rehearsal remains
authoritative before a type-changing write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class TypeFormField:
    """One named field visible after selecting an expense type."""

    name: str
    label: str
    required: bool
    locked: bool
    widget: str


def _load() -> dict[str, tuple[TypeFormField, ...]]:
    raw = json.loads(Path(__file__).with_name("type_form_map.json").read_text())
    if len(raw) != 67:
        raise RuntimeError(
            f"full type form map is incomplete: expected 67 types, got {len(raw)}"
        )
    return {
        expense_type: tuple(TypeFormField(**field) for field in fields)
        for expense_type, fields in raw.items()
    }


TYPE_FORM_FIELDS: Mapping[str, tuple[TypeFormField, ...]] = _load()
