"""Render context evidence graph chunks for existing V3/V4/critic lanes."""

from __future__ import annotations

from typing import Any, Literal

from ...semantic.static_critic_evidence import render_static_critic_evidence
from .._cfg import build_cfg_index
from .._sliced_methods import render_sliced_methods_blob
from .evidence_index import (
    context_chunk_to_sliced_payload,
    context_chunk_to_static_payload,
)

ChunkRenderMode = Literal["v3", "v4", "critic"]


def render_chunk_evidence(
    chunk: dict[str, Any],
    *,
    mode: ChunkRenderMode,
) -> str:
    """Render one context evidence chunk using an existing variant renderer."""
    sliced_payload = context_chunk_to_sliced_payload(chunk)
    static_payload = context_chunk_to_static_payload(chunk)
    if mode == "v3":
        return render_sliced_methods_blob(
            sliced_payload,
            include_analysis_meta=False,
            cfg_index=None,
        )
    if mode == "v4":
        return render_sliced_methods_blob(
            sliced_payload,
            include_analysis_meta=True,
            cfg_index=build_cfg_index(static_payload),
        )
    if mode == "critic":
        return render_static_critic_evidence(
            sliced_methods_payload=sliced_payload,
            static_analysis_payload=static_payload,
            critic_label="resource-seeded evidence graph Step 2 critic",
            evidence_title="STATIC ANALYSIS EVIDENCE CHUNK",
            evidence_description=(
                "This is one chunk of the page-local static analysis payload, "
                "generated from context-slicer output and the method-CFG index."
            ),
        )
    raise ValueError(f"unsupported chunk evidence render mode: {mode}")
