from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app_semantics_kb.static_analysis.common.normalization import (
    build_warning,
    normalize_string_list,
    normalize_text,
)
from app_semantics_kb.static_analysis.common.uid_rules import (
    method_uid,
    type_uid,
)


@dataclass
class SlicerCanonicalizationResult:
    method_slices: list[dict[str, Any]]
    domain_types: list[dict[str, Any]]
    handler_candidates: list[dict[str, str]]
    analysis_warnings: list[dict[str, Any]]


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_context_slicer(
    base_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    ctx_dir = base_dir / "context-slicer-output"
    slices = _read_json(ctx_dir / "slices.json")
    method_bodies = _read_json(ctx_dir / "method-bodies.json")
    type_index = _read_json(ctx_dir / "type-index.json")
    return (
        slices if isinstance(slices, list) else [],
        method_bodies if isinstance(method_bodies, dict) else {},
        type_index if isinstance(type_index, dict) else {},
    )


def _extract_backward_call_paths(call_paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for call_path in call_paths:
        if normalize_text(call_path.get("direction")).upper() != "BACKWARD":
            continue
        path_methods = normalize_string_list(call_path.get("path_methods") or [])
        if not path_methods:
            continue
        key = tuple(path_methods)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "callback_kind": normalize_text(call_path.get("callback_kind")) or "OTHER",
                "root_callback": normalize_text(call_path.get("root_callback")),
                "path_methods": path_methods,
            }
        )
    return result


def _extract_forward_call_paths(call_paths: list[dict[str, Any]]) -> list[list[str]]:
    result: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for call_path in call_paths:
        if normalize_text(call_path.get("direction")).upper() != "FORWARD":
            continue
        path_methods = normalize_string_list(call_path.get("path_methods") or [])
        if not path_methods:
            continue
        key = tuple(path_methods)
        if key in seen:
            continue
        seen.add(key)
        result.append(path_methods)
    return result


def _method_name_prefix(method_full_name: str) -> str:
    return method_full_name.split(":", 1)[0]


def _constructor_type_name(method_full_name: str) -> str:
    prefix = _method_name_prefix(method_full_name)
    marker = ".<init>"
    if marker not in prefix:
        return ""
    return prefix.split(marker, 1)[0]


def _lambda_enclosing_prefix(method_full_name: str) -> str:
    prefix = _method_name_prefix(method_full_name)
    marker = ".lambda$"
    if marker in prefix:
        owner, rest = prefix.split(marker, 1)
        method_name = rest.split("$", 1)[0]
        return f"{owner}.{method_name}" if method_name else owner
    if "<lambda>" in prefix:
        return prefix.split("<lambda>", 1)[0].rstrip(".$:")
    return ""


def _source_code_nullable_reason(method_full_name: str) -> str:
    lowered = method_full_name.lower()
    if "lambda$" in lowered or "<lambda>" in lowered or "$$lambda" in lowered or "$anon" in lowered:
        return "lambda_or_anonymous_body_omitted"
    if ".<init>" in method_full_name or method_full_name.endswith("<init>"):
        return "constructor_body_omitted"
    return "method_body_missing"


def _fallback_lambda_body(
    method_full_name: str,
    method_bodies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    prefix = _lambda_enclosing_prefix(method_full_name)
    if not prefix:
        return {}
    matches = [
        body
        for candidate, body in method_bodies.items()
        if candidate.startswith(f"{prefix}:") and isinstance(body, dict)
    ]
    return matches[0] if matches else {}


def _fallback_constructor_body(
    method_full_name: str,
    type_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    type_name = _constructor_type_name(method_full_name)
    entry = type_index.get(type_name)
    return entry if isinstance(entry, dict) else {}


def _resolve_method_source(
    method_full_name: str,
    method_bodies: dict[str, dict[str, Any]],
    type_index: dict[str, dict[str, Any]],
) -> tuple[Any, Any, str | None]:
    body = method_bodies.get(method_full_name) or {}
    source_code = body.get("body")
    source_file = body.get("file")
    if source_code:
        return source_code, source_file, None

    reason = _source_code_nullable_reason(method_full_name)
    fallback = (
        _fallback_lambda_body(method_full_name, method_bodies)
        if reason == "lambda_or_anonymous_body_omitted"
        else _fallback_constructor_body(method_full_name, type_index)
        if reason == "constructor_body_omitted"
        else {}
    )
    fallback_code = fallback.get("body")
    if fallback_code:
        return fallback_code, fallback.get("file"), None
    return source_code, source_file, reason


def build_slicer_canonicalization(base_dir: Path) -> SlicerCanonicalizationResult:
    slices, method_bodies, type_index = _load_context_slicer(base_dir)

    method_slices: list[dict[str, Any]] = []
    handler_candidates: list[dict[str, str]] = []
    domain_types: list[dict[str, Any]] = []
    analysis_warnings: list[dict[str, Any]] = []

    referenced_type_names: set[str] = set()
    seen_handlers: set[tuple[str, str, str]] = set()

    for method_slice in slices:
        if not isinstance(method_slice, dict):
            continue
        method_full_name = normalize_text(method_slice.get("primary_method"))
        if not method_full_name:
            continue
        resource_ids = normalize_string_list(
            usage.get("anchor_id")
            for usage in (method_slice.get("affecting_usages") or [])
            if isinstance(usage, dict)
        )
        source_code, source_file, nullable_reason = _resolve_method_source(
            method_full_name,
            method_bodies,
            type_index,
        )

        for ref in method_slice.get("domain_type_refs") or []:
            normalized_ref = normalize_text(ref)
            if normalized_ref:
                referenced_type_names.add(normalized_ref)

        call_paths = method_slice.get("call_paths") or []
        backward_call_paths = _extract_backward_call_paths(call_paths)
        forward_call_paths = _extract_forward_call_paths(call_paths)

        method_slice_entry = {
            "method_uid": method_uid(method_full_name),
            "method_full_name": method_full_name,
            "source_code": source_code,
            "source_file": source_file,
            "resource_ids": resource_ids,
            "backward_call_paths": backward_call_paths,
            "forward_call_paths": forward_call_paths,
        }
        if nullable_reason is not None:
            method_slice_entry["source_code_nullable_reason"] = nullable_reason
            analysis_warnings.append(
                build_warning(
                    "missing_method_source",
                    f"method source code not found for {method_full_name}",
                    method_full_name=method_full_name,
                    source_code_nullable_reason=nullable_reason,
                )
            )
        method_slices.append(method_slice_entry)

        for backward_call_path in backward_call_paths:
            root_callback = normalize_text(backward_call_path.get("root_callback"))
            callback_kind = normalize_text(backward_call_path.get("callback_kind")) or "OTHER"
            for resource_id in resource_ids or [""]:
                key = (root_callback, callback_kind, resource_id)
                if key in seen_handlers or not root_callback:
                    continue
                seen_handlers.add(key)
                handler_candidates.append(
                    {
                        "root_callback": root_callback,
                        "callback_kind": callback_kind,
                        "resource_id": resource_id,
                    }
                )

    for type_name in sorted(referenced_type_names):
        entry = type_index.get(type_name)
        if not isinstance(entry, dict):
            analysis_warnings.append(
                build_warning(
                    "missing_type_index_entry",
                    f"type-index entry not found for {type_name}",
                )
            )
            continue
        domain_types.append(
            {
                "type_uid": type_uid(type_name),
                "type_full_name": type_name,
                "short_name": normalize_text(entry.get("type_full_name") or type_name).split(".")[-1],
                "body": entry.get("body"),
                "file": entry.get("file"),
            }
        )

    return SlicerCanonicalizationResult(
        method_slices=method_slices,
        domain_types=domain_types,
        handler_candidates=handler_candidates,
        analysis_warnings=analysis_warnings,
    )
