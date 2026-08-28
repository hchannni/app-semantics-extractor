"""Shared static-evidence rendering for Step 2 critic prompts."""

from __future__ import annotations

from typing import Any

from ..fusion._cfg import build_cfg_index
from ..fusion._sliced_methods import render_sliced_methods_blob


def render_static_critic_evidence(
    *,
    sliced_methods_payload: dict[str, Any],
    static_analysis_payload: dict[str, Any],
    critic_label: str,
    evidence_title: str = "STATIC ANALYSIS EVIDENCE",
    evidence_description: str | None = None,
) -> str:
    """Render page-local Joern/context-slicer evidence for a drop-only critic."""
    cfg_index = build_cfg_index(static_analysis_payload)
    evidence_blob = render_sliced_methods_blob(
        sliced_methods_payload,
        include_analysis_meta=True,
        cfg_index=cfg_index,
    ).strip()
    if not evidence_blob:
        evidence_blob = "(No page-local sliced methods or CFG evidence was available.)"
    description = evidence_description or (
        "Generated from context-slicer output and the method-CFG index "
        f"for the {critic_label}."
    )
    return (
        f"{evidence_title}\n"
        f"{description}\n\n"
        "--- SLICED METHODS AND CFG EVIDENCE ---\n"
        f"{evidence_blob}\n"
    )
