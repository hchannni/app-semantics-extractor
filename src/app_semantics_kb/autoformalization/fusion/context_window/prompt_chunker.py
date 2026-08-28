"""Naive rendered-prompt chunking baseline for context-window experiments."""

from __future__ import annotations

from typing import Any


def _normalize_budget(target_chars: int, max_chars: int) -> tuple[int, int]:
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    return min(target_chars, max_chars), max_chars


def _split_long_line(line: str, max_chars: int) -> list[str]:
    return [line[idx : idx + max_chars] for idx in range(0, len(line), max_chars)]


def _iter_prompt_units(text: str, max_chars: int) -> list[str]:
    units: list[str] = []
    for line in text.splitlines(keepends=True):
        if len(line) <= max_chars:
            units.append(line)
        else:
            units.extend(_split_long_line(line, max_chars))
    if not units and text:
        return [text]
    return units


def _chunk_record(
    *,
    chunk_id: str,
    text: str,
    start_char: int,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "start_char": start_char,
        "end_char": start_char + len(text),
        "char_count": len(text),
        "text": text,
    }


def pack_rendered_prompt_chunks(
    text: str,
    *,
    target_chars: int = 400000,
    max_chars: int = 500000,
) -> dict[str, Any]:
    """Split a rendered prompt into deterministic text chunks without loss."""
    effective_target, effective_max = _normalize_budget(target_chars, max_chars)
    units = _iter_prompt_units(text, effective_max)
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_chars = 0
    cursor = 0

    def flush() -> None:
        nonlocal current, current_chars, cursor
        if not current:
            return
        chunk_text = "".join(current)
        chunks.append(
            _chunk_record(
                chunk_id=f"chunk_{len(chunks) + 1:04d}",
                text=chunk_text,
                start_char=cursor,
            )
        )
        cursor += len(chunk_text)
        current = []
        current_chars = 0

    for unit in units:
        unit_len = len(unit)
        if current and (
            current_chars >= effective_target
            or current_chars + unit_len > effective_max
        ):
            flush()
        current.append(unit)
        current_chars += unit_len
    flush()
    return {
        "meta": {
            "source": "naive_rendered_prompt_chunker",
            "unit": "rendered_prompt_text",
            "boundary_policy": "line_boundary_then_hard_split_long_lines",
            "target_chars": target_chars,
            "max_chars": max_chars,
        },
        "chunks": chunks,
        "stats": {
            "chunk_count": len(chunks),
            "source_char_count": len(text),
            "chunked_char_count": sum(chunk["char_count"] for chunk in chunks),
            "max_chunk_chars": max(
                (chunk["char_count"] for chunk in chunks),
                default=0,
            ),
        },
    }


def slim_rendered_prompt_chunks_manifest(packed: dict[str, Any]) -> dict[str, Any]:
    """Return a prompt chunk manifest without duplicated prompt text."""
    return {
        "meta": dict(packed.get("meta") or {}),
        "chunks": [
            {key: value for key, value in chunk.items() if key != "text"}
            for chunk in packed.get("chunks", [])
            if isinstance(chunk, dict)
        ],
        "stats": dict(packed.get("stats") or {}),
    }
