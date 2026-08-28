"""Shared static code-context payload builders for autoformalization CLIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...extractors.code_context_loader import (
    build_cfg_context_from_method_cfg_index,
    build_sliced_methods_payload_from_context_slicer,
)


@dataclass(frozen=True)
class StaticCodeContextPayloads:
    sliced_methods_payload: dict[str, Any] | None
    static_analysis_payload: dict[str, Any] | None


def build_static_code_context_payloads(
    *,
    a11y_path: Path,
    context_slicer_dir: Path | None,
    method_cfg_index_path: Path | None,
    include_sliced_methods: bool,
    include_static_analysis: bool,
    sliced_output_path: Path | None = None,
    context_required_message: str = "variant 3/4 requires context_slicer_dir",
    cfg_required_message: str = "variant 4 requires method_cfg_index_path and context_slicer_dir",
) -> StaticCodeContextPayloads:
    sliced_payload: dict[str, Any] | None = None
    cfg_payload: dict[str, Any] | None = None

    if include_sliced_methods or include_static_analysis:
        if context_slicer_dir is None:
            raise ValueError(context_required_message)
        sliced_payload = build_sliced_methods_payload_from_context_slicer(
            a11y_path=a11y_path,
            context_slicer_dir=context_slicer_dir,
            output_path=sliced_output_path,
        )

    if include_static_analysis:
        if method_cfg_index_path is None or context_slicer_dir is None:
            raise ValueError(cfg_required_message)
        cfg_payload = build_cfg_context_from_method_cfg_index(
            a11y_path=a11y_path,
            context_slicer_dir=context_slicer_dir,
            method_cfg_index_path=method_cfg_index_path,
        )

    return StaticCodeContextPayloads(
        sliced_methods_payload=sliced_payload,
        static_analysis_payload=cfg_payload,
    )
