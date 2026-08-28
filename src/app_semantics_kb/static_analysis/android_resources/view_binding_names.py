"""Deterministic Android ViewBinding name helpers."""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[^0-9A-Za-z]+")


def _words(value: str) -> list[str]:
    return [part for part in _SEPARATORS.split(value.strip()) if part]


def to_pascal_case(value: str) -> str:
    """Convert an Android resource stem to the generated binding class stem."""
    words = _words(value)
    return "".join(word[:1].upper() + word[1:] for word in words)


def to_lower_camel(value: str) -> str:
    """Convert an Android resource id to the generated ViewBinding field name."""
    words = _words(value)
    if not words:
        return value
    first, *rest = words
    return first[:1].lower() + first[1:] + "".join(
        word[:1].upper() + word[1:] for word in rest
    )


def layout_name_to_binding_class(layout_name: str) -> str:
    return f"{to_pascal_case(layout_name)}Binding"


def resource_id_to_binding_field(resource_name: str) -> str:
    return to_lower_camel(resource_name)
