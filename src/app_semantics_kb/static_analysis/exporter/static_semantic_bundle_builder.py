"""
static_semantic_bundle_builder.py — copied legacy Joern outputs -> StaticSemanticBundle bridge

target-home `legacy_joern/` snapshot 또는 지정된 legacy Joern output directory를 읽어
canonical `StaticSemanticBundle` JSON을 생성한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app_semantics_kb.static_analysis.common.normalization import (
    build_warning,
    normalize_text,
    source_file_from_location,
)
from app_semantics_kb.static_analysis.common.uid_rules import (
    anchor_uid,
    evidence_uid,
    handler_uid,
)
from app_semantics_kb.static_analysis.slicer.legacy_context_slicer_bridge import (
    build_slicer_canonicalization,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read_json_list_if_exists(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = _read_json(path)
    return data if isinstance(data, list) else []


def _read_required_json_list(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"expected JSON array at {path}")
    return data


def _load_view_anchors(base_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    v2_path = base_dir / "view-anchors-v2.json"
    if v2_path.exists():
        return _read_required_json_list(v2_path), True
    return _read_json_list_if_exists(base_dir / "view-anchors.json"), False


def _load_anchor_usages(base_dir: Path) -> list[dict[str, Any]]:
    path = base_dir / "anchor-usages.json"
    data = _read_json(path)
    return data if isinstance(data, list) else []


def _json_id(value: Any) -> str:
    return normalize_text(value)


def _v2_usage_type(anchor: dict[str, Any]) -> str | None:
    usage_type = normalize_text(anchor.get("usage_type"))
    if usage_type:
        return usage_type

    occurrence_role = normalize_text(anchor.get("occurrence_role"))
    if not occurrence_role:
        return None
    if occurrence_role == "USAGE":
        return "DIRECT_USAGE"

    occurrence_node_id = _json_id(anchor.get("cpg_node_id"))
    owner_node_id = _json_id(anchor.get("handle_owner_node_id"))
    if owner_node_id and owner_node_id != occurrence_node_id:
        return "ASSIGNMENT"
    return "CHAINING"


def _anchor_name(anchor: dict[str, Any]) -> str:
    for key in ("anchor_name", "handle_name", "binding_field"):
        value = normalize_text(anchor.get(key))
        if value:
            return value
    return ""


def _is_dynamic_resource_id(resource_id: str) -> bool:
    return resource_id in {"UNKNOWN_DYNAMIC_RESOURCE", "DYNAMIC_RESOURCE_ID"}


def _anchor_entry(anchor: dict[str, Any]) -> dict[str, Any] | None:
    resource_id = normalize_text(anchor.get("resource_id"))
    if not resource_id or _is_dynamic_resource_id(resource_id):
        return None

    anchor_name = _anchor_name(anchor)
    location = _anchor_location(anchor)
    anchor_ref = anchor_uid(resource_id, anchor_name, location)
    source_file = source_file_from_location(location)
    return {
        "anchor_uid": anchor_ref,
        "resource_id": resource_id,
        "widget_type": anchor.get("view_type"),
        "source_file": source_file or None,
        "anchor_location": location or None,
        "anchor_name": anchor_name or None,
        "usage_type": _v2_usage_type(anchor),
    }


def _anchor_location(anchor: dict[str, Any]) -> str:
    for key in ("location", "anchor_location", "occurrence_location"):
        location = normalize_text(anchor.get(key))
        if location:
            return location
    return ""


def _anchor_node_ids(anchor: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for key in ("cpg_node_id", "occurrence_node_id", "handle_owner_node_id"):
        node_id = _json_id(anchor.get(key))
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        result.append(node_id)
    return result


def _append_unique(mapping: dict[Any, list[str]], key: Any, value: str) -> None:
    values = mapping.setdefault(key, [])
    if value not in values:
        values.append(value)


def _warning_key(warning: dict[str, Any]) -> tuple[Any, ...]:
    return (
        warning.get("code"),
        warning.get("message"),
        warning.get("resource_id"),
        warning.get("anchor_name"),
        warning.get("location"),
        warning.get("cpg_node_id"),
    )


def _append_warning_once(
    warnings: list[dict[str, Any]],
    seen: set[tuple[Any, ...]],
    warning: dict[str, Any],
) -> None:
    key = _warning_key(warning)
    if key in seen:
        return
    seen.add(key)
    warnings.append(warning)


@dataclass
class AnchorUidIndex:
    by_exact: dict[tuple[str, str, str], str] = field(default_factory=dict)
    by_node_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    by_resource: dict[str, list[str]] = field(default_factory=dict)

    def add(self, anchor: dict[str, Any], entry: dict[str, Any]) -> None:
        resource_id = normalize_text(entry.get("resource_id"))
        anchor_ref = normalize_text(entry.get("anchor_uid"))
        if not resource_id or not anchor_ref:
            return

        location = _anchor_location(anchor)
        anchor_name = _anchor_name(anchor)
        if location:
            self.by_exact[(resource_id, anchor_name, location)] = anchor_ref
        for node_id in _anchor_node_ids(anchor):
            _append_unique(self.by_node_id, (resource_id, node_id), anchor_ref)
        _append_unique(self.by_resource, resource_id, anchor_ref)

    def resolve(
        self,
        anchor: dict[str, Any],
        *,
        warnings: list[dict[str, Any]],
        seen_warnings: set[tuple[Any, ...]],
    ) -> str | None:
        resource_id = normalize_text(anchor.get("resource_id"))
        if not resource_id:
            return None

        location = _anchor_location(anchor)
        anchor_name = _anchor_name(anchor)
        if location:
            exact = self.by_exact.get((resource_id, anchor_name, location))
            if exact:
                return exact

        for node_id in _anchor_node_ids(anchor):
            node_matches = self.by_node_id.get((resource_id, node_id), [])
            if len(node_matches) == 1:
                return node_matches[0]

        return self._resource_fallback(
            resource_id=resource_id,
            anchor_name=anchor_name,
            location=location,
            cpg_node_id=_json_id(anchor.get("cpg_node_id")),
            warnings=warnings,
            seen_warnings=seen_warnings,
        )

    def _resource_fallback(
        self,
        *,
        resource_id: str,
        anchor_name: str,
        location: str,
        cpg_node_id: str,
        warnings: list[dict[str, Any]],
        seen_warnings: set[tuple[Any, ...]],
    ) -> str | None:
        matches = self.by_resource.get(resource_id, [])
        if not matches:
            return None
        if len(matches) > 1:
            _append_warning_once(
                warnings,
                seen_warnings,
                build_warning(
                    "ambiguous_anchor_uid",
                    f"Multiple anchor UIDs found for resource_id={resource_id}; using deterministic fallback",
                    resource_id=resource_id,
                    anchor_name=anchor_name,
                    location=location,
                    cpg_node_id=cpg_node_id,
                    candidate_count=len(matches),
                ),
            )
        return matches[0]


def _append_anchor(
    *,
    anchor: dict[str, Any],
    anchors: list[dict[str, Any]],
    anchor_index: AnchorUidIndex,
    force_when_resource_missing: bool = False,
) -> None:
    entry = _anchor_entry(anchor)
    if entry is None:
        return
    resource_id = entry["resource_id"]
    if force_when_resource_missing and resource_id in anchor_index.by_resource:
        return
    anchor_index.add(anchor, entry)
    anchors.append(entry)


def build_static_semantic_bundle(
    *,
    legacy_joern_dir: Path,
    run_id: str,
    producer: str = "static-semantics-exporter",
) -> dict[str, Any]:
    view_anchors, uses_authoritative_v2_anchors = _load_view_anchors(legacy_joern_dir)
    anchor_usages = _load_anchor_usages(legacy_joern_dir)
    slicer_result = build_slicer_canonicalization(legacy_joern_dir)

    anchors: list[dict[str, Any]] = []
    anchor_index = AnchorUidIndex()
    analysis_warnings: list[dict[str, Any]] = []
    seen_warnings: set[tuple[Any, ...]] = set()
    for anchor in view_anchors:
        if not isinstance(anchor, dict):
            continue
        resource_id = normalize_text(anchor.get("resource_id"))
        if _is_dynamic_resource_id(resource_id):
            _append_warning_once(
                analysis_warnings,
                seen_warnings,
                build_warning(
                    "dynamic_resource_id",
                    "Java view anchor used a dynamic resource id that was not resolved",
                    resource_id=resource_id,
                    location=_anchor_location(anchor),
                    cpg_node_id=_json_id(anchor.get("cpg_node_id")),
                ),
            )
            continue
        _append_anchor(
            anchor=anchor,
            anchors=anchors,
            anchor_index=anchor_index,
        )

    if not uses_authoritative_v2_anchors:
        for group in anchor_usages:
            if not isinstance(group, dict):
                continue
            anchor = group.get("anchor") or {}
            if isinstance(anchor, dict):
                _append_anchor(
                    anchor=anchor,
                    anchors=anchors,
                    anchor_index=anchor_index,
                    force_when_resource_missing=True,
                )

    usages: list[dict[str, Any]] = []
    semantic_evidence: list[dict[str, Any]] = []
    for group in anchor_usages:
        if not isinstance(group, dict):
            continue
        anchor = group.get("anchor") or {}
        resource_id = normalize_text(anchor.get("resource_id"))
        if _is_dynamic_resource_id(resource_id):
            _append_warning_once(
                analysis_warnings,
                seen_warnings,
                build_warning(
                    "dynamic_resource_id",
                    "Java anchor usage used a dynamic resource id that was not resolved",
                    resource_id=resource_id,
                    location=_anchor_location(anchor),
                    cpg_node_id=_json_id(anchor.get("cpg_node_id")),
                ),
            )
            continue
        anchor_ref = anchor_index.resolve(
            anchor,
            warnings=analysis_warnings,
            seen_warnings=seen_warnings,
        )
        if not anchor_ref:
            _append_warning_once(
                analysis_warnings,
                seen_warnings,
                build_warning(
                    "unresolved_anchor_uid",
                    f"Anchor UID not found for resource_id={resource_id}",
                    resource_id=resource_id,
                    anchor_name=_anchor_name(anchor),
                    location=_anchor_location(anchor),
                    cpg_node_id=_json_id(anchor.get("cpg_node_id")),
                )
            )
            continue
        for usage in (group.get("usages") or []):
            if not isinstance(usage, dict):
                continue
            usage_point = usage.get("usage_point") or {}
            method_full_name = normalize_text(usage.get("enclosing_method_full_name"))
            if not method_full_name:
                continue
            usages.append(
                {
                    "anchor_uid": anchor_ref,
                    "method_full_name": method_full_name,
                    "resource_ids": [resource_id] if resource_id else [],
                    "source_code": usage_point.get("code"),
                    "source_file": usage_point.get("file"),
                }
            )

            usage_kind = normalize_text(usage_point.get("usage_kind")).upper()
            evidence_kind = (
                "write" if usage_kind in {"SETTER", "ASSIGNMENT", "MUTATION"} else
                "read" if usage_kind in {"GETTER", "READ"} else
                "sink" if usage_kind in {"LISTENER", "CALLBACK"} else
                "api_call" if usage_kind in {"CALL", "API"} else
                "unknown"
            )
            evidence_ref = evidence_uid(
                anchor_ref,
                method_full_name,
                usage_point.get("code"),
                usage_point.get("file"),
                usage_point.get("start_line"),
            )
            semantic_evidence.append(
                {
                    "evidence_uid": evidence_ref,
                    "anchor_uid": anchor_ref,
                    "kind": evidence_kind,
                    "code_snippet": usage_point.get("code"),
                    "location": (
                        f"{usage_point.get('file')}:{usage_point.get('start_line')}"
                        if usage_point.get("file") and usage_point.get("start_line") is not None
                        else usage_point.get("file")
                    ),
                }
            )

    handler_inventory: list[dict[str, Any]] = []
    for handler_candidate in slicer_result.handler_candidates:
        resource_id = handler_candidate["resource_id"]
        trigger_anchor_uid = anchor_index.resolve(
            {"resource_id": resource_id},
            warnings=analysis_warnings,
            seen_warnings=seen_warnings,
        )
        handler_inventory.append(
            {
                "handler_uid": handler_uid(
                    handler_candidate["root_callback"],
                    handler_candidate["callback_kind"],
                    resource_id,
                ),
                "kind": handler_candidate["callback_kind"],
                "trigger_anchor_uid": trigger_anchor_uid,
                "enclosing_method": handler_candidate["root_callback"],
            }
        )
    analysis_warnings.extend(slicer_result.analysis_warnings)

    bundle: dict[str, Any] = {
        "header": {
            "schema_version": "0.1.0",
            "artifact_type": "StaticSemanticBundle",
            "producer": producer,
            "created_at": _iso_now(),
            "run_id": run_id,
        },
        "anchors": anchors,
        "usages": usages,
        "method_slices": slicer_result.method_slices,
        "semantic_evidence": semantic_evidence,
        "handler_inventory": handler_inventory,
        "domain_types": slicer_result.domain_types,
        "analysis_warnings": analysis_warnings,
    }
    return bundle


def save_static_semantic_bundle(output_path: Path, bundle: dict[str, Any]) -> Path:
    _write_json(output_path, bundle)
    return output_path
