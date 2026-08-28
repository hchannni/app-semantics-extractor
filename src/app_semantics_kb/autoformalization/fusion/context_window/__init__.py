"""Context-window mitigation helpers for resource-seeded evidence graph chunks."""

from .evidence_index import (
    DOMAIN_ATTRIBUTION_METHOD_LOCAL,
    DOMAIN_ATTRIBUTION_POLICIES,
    DOMAIN_ATTRIBUTION_SLICE_LOCAL,
    build_context_chunk,
    build_context_evidence_index,
    context_chunk_to_sliced_payload,
    context_chunk_to_static_payload,
)
from .graph_packer import pack_evidence_graph_chunks, slim_chunks_manifest
from .prompt_chunker import (
    pack_rendered_prompt_chunks,
    slim_rendered_prompt_chunks_manifest,
)
from .rendering import render_chunk_evidence

__all__ = [
    "DOMAIN_ATTRIBUTION_METHOD_LOCAL",
    "DOMAIN_ATTRIBUTION_POLICIES",
    "DOMAIN_ATTRIBUTION_SLICE_LOCAL",
    "build_context_chunk",
    "build_context_evidence_index",
    "context_chunk_to_sliced_payload",
    "context_chunk_to_static_payload",
    "pack_evidence_graph_chunks",
    "pack_rendered_prompt_chunks",
    "render_chunk_evidence",
    "slim_rendered_prompt_chunks_manifest",
    "slim_chunks_manifest",
]
