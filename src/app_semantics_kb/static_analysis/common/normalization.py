from __future__ import annotations

from typing import Any, Iterable


def normalize_text(value: object) -> str:
    return str(value or "").strip()


def normalize_string_list(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def source_file_from_location(location: object) -> str | None:
    normalized = normalize_text(location)
    if not normalized:
        return None
    return normalized.split(":", 1)[0] if ":" in normalized else normalized


def build_warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    warning: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    for key, value in extra.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        warning[key] = value
    return warning
