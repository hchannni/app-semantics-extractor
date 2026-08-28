"""Shared static-semantics input resolution helpers for autoformalization CLIs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CONTEXT_SLICER_DIRNAME = "context-slicer-output"
METHOD_CFG_INDEX_FILENAME = "method-cfg-index.json"
STATIC_SEMANTIC_BUNDLE_FILENAME = "static_semantic_bundle.json"


@dataclass(frozen=True)
class StaticSemanticsPaths:
    run_dir: Path | None
    context_slicer_dir: Path | None
    method_cfg_index_path: Path | None


def is_valid_static_semantics_run_dir(
    path: Path,
    *,
    require_bundle: bool = False,
) -> bool:
    required = [
        path.is_dir(),
        (path / CONTEXT_SLICER_DIRNAME).is_dir(),
        (path / METHOD_CFG_INDEX_FILENAME).is_file(),
    ]
    if require_bundle:
        required.append((path / STATIC_SEMANTIC_BUNDLE_FILENAME).is_file())
    return all(required)


def find_latest_static_semantics_run_dir(
    runs_root: Path,
    *,
    require_bundle: bool = False,
) -> Path | None:
    if not runs_root.is_dir():
        return None
    candidates = [
        path
        for path in runs_root.iterdir()
        if is_valid_static_semantics_run_dir(path, require_bundle=require_bundle)
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def static_semantics_paths_from_run(
    *,
    run_dir: Path | None,
    context_slicer_dir: Path | None,
    method_cfg_index_path: Path | None,
) -> StaticSemanticsPaths:
    resolved_context_dir = context_slicer_dir
    if resolved_context_dir is None and run_dir is not None:
        resolved_context_dir = run_dir / CONTEXT_SLICER_DIRNAME

    resolved_cfg_path = method_cfg_index_path
    if resolved_cfg_path is None and run_dir is not None:
        resolved_cfg_path = run_dir / METHOD_CFG_INDEX_FILENAME

    return StaticSemanticsPaths(
        run_dir=run_dir,
        context_slicer_dir=resolved_context_dir,
        method_cfg_index_path=resolved_cfg_path,
    )


def infer_run_dir_from_context_slicer_dir(
    context_slicer_dir: Path | None,
    *,
    require_bundle: bool = True,
) -> Path | None:
    if context_slicer_dir is None:
        return None
    if context_slicer_dir.name != CONTEXT_SLICER_DIRNAME:
        return None
    candidate = context_slicer_dir.parent
    if require_bundle and not (candidate / STATIC_SEMANTIC_BUNDLE_FILENAME).is_file():
        return None
    return candidate


def validate_and_infer_run_dir_from_variant4_paths(
    context_slicer_dir: Path,
    method_cfg_index_path: Path,
    *,
    require_bundle: bool = True,
) -> Path | None:
    if context_slicer_dir.parent.resolve() != method_cfg_index_path.parent.resolve():
        raise ValueError(
            "variant 4 explicit static-semantics overrides must come from the same run root"
        )
    if (
        context_slicer_dir.name != CONTEXT_SLICER_DIRNAME
        or method_cfg_index_path.name != METHOD_CFG_INDEX_FILENAME
    ):
        return None
    candidate = context_slicer_dir.parent
    if require_bundle and not (candidate / STATIC_SEMANTIC_BUNDLE_FILENAME).is_file():
        return None
    return candidate


def read_static_semantic_ref_from_run_dir(run_dir: Path | None) -> str | None:
    if run_dir is None:
        return None

    bundle_path = run_dir / STATIC_SEMANTIC_BUNDLE_FILENAME
    if not bundle_path.is_file():
        return None

    try:
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    header = payload.get("header") if isinstance(payload, dict) else None
    if not isinstance(header, dict):
        return None
    run_id = header.get("run_id")
    return str(run_id) if isinstance(run_id, str) and run_id.strip() else None
