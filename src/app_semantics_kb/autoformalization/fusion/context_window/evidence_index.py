"""Evidence-graph indexes for context-window chunking.

This module only reorganizes precomputed context-slicer and Joern CFG payloads.
It does not parse app source code or run static analysis.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import re
from typing import Any, Iterable


DOMAIN_ATTRIBUTION_METHOD_LOCAL = "method_local"
DOMAIN_ATTRIBUTION_SLICE_LOCAL = "slice_local"
DOMAIN_ATTRIBUTION_POLICIES = {
    DOMAIN_ATTRIBUTION_METHOD_LOCAL,
    DOMAIN_ATTRIBUTION_SLICE_LOCAL,
}


def _dedupe_strings(values: Iterable[object] | object) -> list[str]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, dict)):
        return []
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _method_name(method: dict[str, Any]) -> str:
    return str(method.get("method_full_name") or "").strip()


def _type_name(domain_type: dict[str, Any]) -> str:
    return str(domain_type.get("type_full_name") or "").strip()


def _cfg_method_name(cfg: dict[str, Any]) -> str:
    method = (cfg.get("analysis") or {}).get("method") or {}
    return str(method.get("fullName") or "").strip()


def _methods_by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    methods = payload.get("methods") or []
    if not isinstance(methods, list):
        return {}
    return {
        name: method
        for method in methods
        if isinstance(method, dict) and (name := _method_name(method))
    }


def _domain_types_by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    domain_types = payload.get("domain_types") or []
    if not isinstance(domain_types, list):
        return {}
    return {
        name: entry
        for entry in domain_types
        if isinstance(entry, dict) and (name := _type_name(entry))
    }


def _raw_cfg_by_method(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    cfg_list = (payload or {}).get("method_cfg_list") or []
    if not isinstance(cfg_list, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw in cfg_list:
        if not isinstance(raw, dict):
            continue
        full_name = _cfg_method_name(raw)
        if full_name:
            out[full_name] = raw
    return out


def _fallback_method_entry(method_full_name: str) -> dict[str, Any]:
    return {
        "method_full_name": method_full_name,
        "method_slice_role": "body_ref",
        "resource_ids": [],
        "backward_call_paths": [],
        "forward_call_paths": [],
        "source_file": "",
        "anchor_location": None,
        "range_start_line": None,
        "range_end_line": None,
        "source_code": "",
    }


def _empty_method_info(method_full_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "method_full_name": method_full_name,
        "payload": deepcopy(payload),
        "primary_for_resource_ids": [],
        "body_ref_for_primary_methods": [],
        "body_ref_for_resource_ids": [],
        "slice_indices": [],
        "usage_codes": [],
        "domain_type_refs": [],
        "slice_domain_type_refs": [],
        "available_but_not_attached_domain_type_refs": [],
        "domain_attribution": {
            "policy": DOMAIN_ATTRIBUTION_SLICE_LOCAL,
            "reasons": {},
        },
        "cfg_available": False,
    }


def _append_unique(target: list[str], values: Iterable[object] | object) -> None:
    for value in _dedupe_strings(values):
        if value not in target:
            target.append(value)


def _ensure_method_info(
    methods: OrderedDict[str, dict[str, Any]],
    method_full_name: str,
    methods_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if method_full_name not in methods:
        payload = methods_by_name.get(method_full_name) or _fallback_method_entry(
            method_full_name
        )
        methods[method_full_name] = _empty_method_info(method_full_name, payload)
    return methods[method_full_name]


def _ensure_domain_info(
    domain_types: OrderedDict[str, dict[str, Any]],
    type_full_name: str,
    domain_types_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if type_full_name not in domain_types:
        domain_types[type_full_name] = {
            "type_full_name": type_full_name,
            "payload": deepcopy(domain_types_by_name.get(type_full_name, {})),
            "referred_by_methods": [],
            "referred_by_resource_ids": [],
            "slice_indices": [],
        }
    return domain_types[type_full_name]


def _slice_index(record: dict[str, Any]) -> int | None:
    raw = record.get("slice_index")
    return raw if isinstance(raw, int) else None


def _resource_summary(resource_id: str, slice_records: list[dict[str, Any]]) -> dict[str, Any]:
    primary_methods: list[str] = []
    method_names: list[str] = []
    domain_names: list[str] = []
    usage_codes: list[str] = []
    slice_indices: list[int] = []
    for record in slice_records:
        primary_method = str(record.get("primary_method") or "").strip()
        if primary_method:
            _append_unique(primary_methods, [primary_method])
            _append_unique(method_names, [primary_method])
        _append_unique(method_names, record.get("method_body_refs") or [])
        _append_unique(domain_names, record.get("domain_type_refs") or [])
        _append_unique(usage_codes, record.get("usage_codes") or [])
        idx = _slice_index(record)
        if idx is not None and idx not in slice_indices:
            slice_indices.append(idx)
    return {
        "resource_id": resource_id,
        "slice_indices": slice_indices,
        "primary_methods": primary_methods,
        "method_full_names": method_names,
        "domain_type_refs": domain_names,
        "usage_codes": usage_codes,
        "stats": {
            "slice_count": len(slice_records),
            "primary_method_count": len(primary_methods),
            "method_count": len(method_names),
            "domain_type_count": len(domain_names),
        },
    }


def _record_primary_method(
    *,
    methods: OrderedDict[str, dict[str, Any]],
    methods_by_name: dict[str, dict[str, Any]],
    primary_method: str,
    resource_id: str,
    record: dict[str, Any],
    cfg_by_method: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    info = _ensure_method_info(methods, primary_method, methods_by_name)
    _append_unique(info["primary_for_resource_ids"], [resource_id])
    _append_unique(info["usage_codes"], record.get("usage_codes") or [])
    _append_unique(info["slice_domain_type_refs"], record.get("domain_type_refs") or [])
    idx = _slice_index(record)
    if idx is not None and idx not in info["slice_indices"]:
        info["slice_indices"].append(idx)
    info["cfg_available"] = primary_method in cfg_by_method
    return info


def _record_body_ref_method(
    *,
    methods: OrderedDict[str, dict[str, Any]],
    methods_by_name: dict[str, dict[str, Any]],
    body_ref: str,
    primary_method: str,
    resource_id: str,
    record: dict[str, Any],
    cfg_by_method: dict[str, dict[str, Any]],
) -> None:
    info = _ensure_method_info(methods, body_ref, methods_by_name)
    _append_unique(info["body_ref_for_primary_methods"], [primary_method])
    _append_unique(info["body_ref_for_resource_ids"], [resource_id])
    _append_unique(info["slice_domain_type_refs"], record.get("domain_type_refs") or [])
    idx = _slice_index(record)
    if idx is not None and idx not in info["slice_indices"]:
        info["slice_indices"].append(idx)
    info["cfg_available"] = body_ref in cfg_by_method


def _record_domain_ref(
    *,
    domain_types: OrderedDict[str, dict[str, Any]],
    domain_types_by_name: dict[str, dict[str, Any]],
    type_full_name: str,
    primary_method: str,
    resource_id: str,
    record: dict[str, Any],
) -> None:
    info = _ensure_domain_info(domain_types, type_full_name, domain_types_by_name)
    _append_unique(info["referred_by_methods"], [primary_method])
    _append_unique(info["referred_by_resource_ids"], [resource_id])
    idx = _slice_index(record)
    if idx is not None and idx not in info["slice_indices"]:
        info["slice_indices"].append(idx)


def _build_edges(
    *,
    resource_id: str,
    record: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    primary_method = str(record.get("primary_method") or "").strip()
    if not primary_method:
        return {
            "resource_to_primary_methods": [],
            "primary_to_body_refs": [],
            "method_to_domain_types": [],
        }
    slice_indices = [_slice_index(record)] if _slice_index(record) is not None else []
    body_edges = [
        {
            "primary_method": primary_method,
            "body_ref_method": body_ref,
            "resource_id": resource_id,
            "slice_indices": slice_indices,
        }
        for body_ref in _dedupe_strings(record.get("method_body_refs") or [])
    ]
    domain_edges = [
        {
            "method_full_name": primary_method,
            "type_full_name": type_full_name,
            "resource_id": resource_id,
            "slice_indices": slice_indices,
        }
        for type_full_name in _dedupe_strings(record.get("domain_type_refs") or [])
    ]
    return {
        "resource_to_primary_methods": [
            {
                "resource_id": resource_id,
                "method_full_name": primary_method,
                "slice_indices": slice_indices,
            }
        ],
        "primary_to_body_refs": body_edges,
        "method_to_domain_types": domain_edges,
    }


def _append_edges(
    target: dict[str, list[dict[str, Any]]],
    source: dict[str, list[dict[str, Any]]],
) -> None:
    for key, entries in source.items():
        target.setdefault(key, [])
        for entry in entries:
            if entry not in target[key]:
                target[key].append(entry)


def _declaring_type(method_full_name: str) -> str:
    name_part = str(method_full_name or "").split(":", 1)[0]
    if "." not in name_part:
        return ""
    return name_part.rsplit(".", 1)[0]


def _type_match_tokens(type_full_name: str) -> list[str]:
    short_name = str(type_full_name or "").split(".")[-1]
    values = [type_full_name, short_name]
    values.extend(part for part in short_name.split("$") if part)
    return [value for value in dict.fromkeys(values) if len(value) >= 3]


def _contains_type_token(text: str, token: str, *, full_name: str) -> bool:
    if not text or not token:
        return False
    if token == full_name:
        return token in text
    pattern = r"(?<![A-Za-z0-9_$])" + re.escape(token) + r"(?![A-Za-z0-9_$])"
    return re.search(pattern, text) is not None


def _type_match_reason(type_full_name: str, text: str, *, label: str) -> str | None:
    for token in _type_match_tokens(type_full_name):
        if _contains_type_token(text, token, full_name=type_full_name):
            return f"{label}_direct_type_match"
    return None


def _add_reason(reasons: dict[str, list[str]], type_full_name: str, reason: str) -> None:
    reasons.setdefault(type_full_name, [])
    if reason not in reasons[type_full_name]:
        reasons[type_full_name].append(reason)


def _method_texts(method_full_name: str, method_info: dict[str, Any]) -> tuple[str, str]:
    payload = method_info.get("payload") or {}
    signature = "\n".join(
        str(value or "")
        for value in (
            method_full_name,
            payload.get("signature"),
            payload.get("method_name"),
        )
    )
    body = str(payload.get("source_code") or payload.get("body") or "")
    return signature, body


def _method_local_domain_refs(
    *,
    method_full_name: str,
    method_info: dict[str, Any],
    domain_types_by_name: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, list[str]]]:
    slice_refs = _dedupe_strings(method_info.get("slice_domain_type_refs") or [])
    candidates = list(slice_refs)
    declaring = _declaring_type(method_full_name)
    if declaring and declaring in domain_types_by_name and declaring not in candidates:
        candidates.append(declaring)

    mandatory: list[str] = []
    reasons: dict[str, list[str]] = {}
    signature, body = _method_texts(method_full_name, method_info)
    if declaring and declaring in domain_types_by_name:
        _append_unique(mandatory, [declaring])
        _add_reason(reasons, declaring, "declaring_class")

    for type_full_name in candidates:
        if type_full_name in mandatory:
            continue
        signature_reason = _type_match_reason(
            type_full_name,
            signature,
            label="signature",
        )
        body_reason = _type_match_reason(type_full_name, body, label="body")
        if signature_reason or body_reason:
            _append_unique(mandatory, [type_full_name])
            if signature_reason:
                _add_reason(reasons, type_full_name, signature_reason)
            if body_reason:
                _add_reason(reasons, type_full_name, body_reason)
    return mandatory, reasons


def _slice_local_domain_refs(
    method_info: dict[str, Any],
) -> tuple[list[str], dict[str, list[str]]]:
    refs = _dedupe_strings(method_info.get("slice_domain_type_refs") or [])
    return refs, {ref: ["slice_local"] for ref in refs}


def _ensure_mandatory_domains(
    *,
    domain_types: OrderedDict[str, dict[str, Any]],
    domain_types_by_name: dict[str, dict[str, Any]],
    method_full_name: str,
    method_info: dict[str, Any],
    mandatory_refs: list[str],
) -> None:
    resource_ids: list[str] = []
    _append_unique(resource_ids, method_info.get("primary_for_resource_ids") or [])
    _append_unique(resource_ids, method_info.get("body_ref_for_resource_ids") or [])
    for type_full_name in mandatory_refs:
        if type_full_name not in domain_types_by_name:
            continue
        info = _ensure_domain_info(domain_types, type_full_name, domain_types_by_name)
        _append_unique(info["referred_by_methods"], [method_full_name])
        _append_unique(info["referred_by_resource_ids"], resource_ids)
        for idx in method_info.get("slice_indices") or []:
            if isinstance(idx, int) and idx not in info["slice_indices"]:
                info["slice_indices"].append(idx)


def _apply_domain_attribution(
    *,
    methods: OrderedDict[str, dict[str, Any]],
    domain_types: OrderedDict[str, dict[str, Any]],
    domain_types_by_name: dict[str, dict[str, Any]],
    policy: str,
) -> dict[str, Any]:
    if policy not in DOMAIN_ATTRIBUTION_POLICIES:
        raise ValueError(
            "domain_attribution_policy must be one of: "
            + ", ".join(sorted(DOMAIN_ATTRIBUTION_POLICIES))
        )
    for method_full_name, method_info in methods.items():
        if policy == DOMAIN_ATTRIBUTION_SLICE_LOCAL:
            mandatory_refs, reasons = _slice_local_domain_refs(method_info)
        else:
            mandatory_refs, reasons = _method_local_domain_refs(
                method_full_name=method_full_name,
                method_info=method_info,
                domain_types_by_name=domain_types_by_name,
            )
        slice_refs = _dedupe_strings(method_info.get("slice_domain_type_refs") or [])
        available_refs = [ref for ref in slice_refs if ref not in mandatory_refs]
        method_info["domain_type_refs"] = mandatory_refs
        method_info["available_but_not_attached_domain_type_refs"] = available_refs
        method_info["domain_attribution"] = {
            "policy": policy,
            "reasons": reasons,
        }
        _ensure_mandatory_domains(
            domain_types=domain_types,
            domain_types_by_name=domain_types_by_name,
            method_full_name=method_full_name,
            method_info=method_info,
            mandatory_refs=mandatory_refs,
        )

    original_refs: list[str] = []
    mandatory_refs: list[str] = []
    for method_info in methods.values():
        _append_unique(original_refs, method_info.get("slice_domain_type_refs") or [])
        _append_unique(mandatory_refs, method_info.get("domain_type_refs") or [])
    omitted = [ref for ref in original_refs if ref not in mandatory_refs]
    return {
        "policy": policy,
        "original_slice_domain_type_count": len(original_refs),
        "mandatory_domain_type_count": len(mandatory_refs),
        "omitted_from_all_chunks_count": len(omitted),
        "omitted_from_all_chunks": omitted,
        "available_but_not_attached_by_method": {
            name: list(info.get("available_but_not_attached_domain_type_refs") or [])
            for name, info in methods.items()
            if info.get("available_but_not_attached_domain_type_refs")
        },
    }


def build_context_evidence_index(
    sliced_methods_payload: dict[str, Any],
    static_analysis_payload: dict[str, Any] | None = None,
    *,
    domain_attribution_policy: str = DOMAIN_ATTRIBUTION_METHOD_LOCAL,
) -> dict[str, Any]:
    """Build a deduped page-local evidence graph from sliced and CFG payloads."""
    visible_resource_ids = _dedupe_strings(
        sliced_methods_payload.get("visible_resource_ids") or []
    )
    resource_slice_index = sliced_methods_payload.get("resource_slice_index") or {}
    if not isinstance(resource_slice_index, dict):
        resource_slice_index = {}

    methods_by_name = _methods_by_name(sliced_methods_payload)
    domain_types_by_name = _domain_types_by_name(sliced_methods_payload)
    cfg_by_method = _raw_cfg_by_method(static_analysis_payload)
    methods: OrderedDict[str, dict[str, Any]] = OrderedDict()
    domain_types: OrderedDict[str, dict[str, Any]] = OrderedDict()
    resources: list[dict[str, Any]] = []
    edges: dict[str, list[dict[str, Any]]] = {
        "resource_to_primary_methods": [],
        "primary_to_body_refs": [],
        "method_to_domain_types": [],
    }

    for resource_id in visible_resource_ids:
        raw_records = resource_slice_index.get(resource_id) or []
        records = [r for r in raw_records if isinstance(r, dict)]
        resources.append(_resource_summary(resource_id, records))
        for record in records:
            primary_method = str(record.get("primary_method") or "").strip()
            if not primary_method:
                continue
            _record_primary_method(
                methods=methods,
                methods_by_name=methods_by_name,
                primary_method=primary_method,
                resource_id=resource_id,
                record=record,
                cfg_by_method=cfg_by_method,
            )
            for body_ref in _dedupe_strings(record.get("method_body_refs") or []):
                _record_body_ref_method(
                    methods=methods,
                    methods_by_name=methods_by_name,
                    body_ref=body_ref,
                    primary_method=primary_method,
                    resource_id=resource_id,
                    record=record,
                    cfg_by_method=cfg_by_method,
                )
            for type_full_name in _dedupe_strings(record.get("domain_type_refs") or []):
                _record_domain_ref(
                    domain_types=domain_types,
                    domain_types_by_name=domain_types_by_name,
                    type_full_name=type_full_name,
                    primary_method=primary_method,
                    resource_id=resource_id,
                    record=record,
                )
            _append_edges(edges, _build_edges(resource_id=resource_id, record=record))

    domain_attribution = _apply_domain_attribution(
        methods=methods,
        domain_types=domain_types,
        domain_types_by_name=domain_types_by_name,
        policy=domain_attribution_policy,
    )
    method_cfg_names = [name for name in methods if name in cfg_by_method]
    resource_method_occurrences = sum(r["stats"]["method_count"] for r in resources)
    resource_domain_occurrences = sum(r["stats"]["domain_type_count"] for r in resources)
    resource_cfg_occurrences = sum(
        len([name for name in r["method_full_names"] if name in cfg_by_method])
        for r in resources
    )
    return {
        "meta": {
            "source": "context_evidence_graph_index",
            "collection_policy": "resource_seeded_method_graph_dedupe",
            "resource_id_policy": "provenance_anchor_not_chunk_boundary",
            "domain_attribution_policy": domain_attribution_policy,
        },
        "visible_resource_ids": visible_resource_ids,
        "resources": resources,
        "ordered_method_full_names": list(methods.keys()),
        "methods": list(methods.values()),
        "domain_types": list(domain_types.values()),
        "cfgs": [
            {"method_full_name": name, "payload": deepcopy(cfg_by_method[name])}
            for name in method_cfg_names
        ],
        "edges": edges,
        "domain_attribution": domain_attribution,
        "stats": {
            "resource_count": len(resources),
            "non_empty_resource_count": len(
                [r for r in resources if r["stats"]["slice_count"] > 0]
            ),
            "resource_local_method_occurrence_count": resource_method_occurrences,
            "resource_local_domain_type_occurrence_count": resource_domain_occurrences,
            "resource_local_cfg_occurrence_count": resource_cfg_occurrences,
            "unique_method_count": len(methods),
            "unique_domain_type_count": len(domain_types),
            "unique_cfg_method_count": len(method_cfg_names),
            "mandatory_domain_type_count": domain_attribution[
                "mandatory_domain_type_count"
            ],
            "omitted_from_all_chunks_count": domain_attribution[
                "omitted_from_all_chunks_count"
            ],
            "method_duplicate_occurrences_avoided": max(
                resource_method_occurrences - len(methods),
                0,
            ),
            "domain_type_duplicate_occurrences_avoided": max(
                resource_domain_occurrences - len(domain_types),
                0,
            ),
            "cfg_duplicate_occurrences_avoided": max(
                resource_cfg_occurrences - len(method_cfg_names),
                0,
            ),
        },
    }


def _index_maps(index: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    method_map = {
        str(entry.get("method_full_name")): entry
        for entry in index.get("methods", [])
        if isinstance(entry, dict) and entry.get("method_full_name")
    }
    domain_map = {
        str(entry.get("type_full_name")): entry
        for entry in index.get("domain_types", [])
        if isinstance(entry, dict) and entry.get("type_full_name")
    }
    cfg_map = {
        str(entry.get("method_full_name")): entry.get("payload")
        for entry in index.get("cfgs", [])
        if isinstance(entry, dict) and entry.get("method_full_name")
    }
    return method_map, domain_map, cfg_map


def _method_payload(method_info: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(method_info.get("payload") or {})
    resource_ids: list[str] = []
    _append_unique(resource_ids, payload.get("resource_ids") or [])
    _append_unique(resource_ids, method_info.get("primary_for_resource_ids") or [])
    _append_unique(resource_ids, method_info.get("body_ref_for_resource_ids") or [])
    payload["resource_ids"] = resource_ids
    payload["context_window_provenance"] = {
        "primary_for_resource_ids": list(
            method_info.get("primary_for_resource_ids") or []
        ),
        "body_ref_for_primary_methods": list(
            method_info.get("body_ref_for_primary_methods") or []
        ),
        "body_ref_for_resource_ids": list(
            method_info.get("body_ref_for_resource_ids") or []
        ),
        "slice_indices": list(method_info.get("slice_indices") or []),
        "slice_domain_type_refs": list(
            method_info.get("slice_domain_type_refs") or []
        ),
        "mandatory_domain_type_refs": list(method_info.get("domain_type_refs") or []),
        "available_but_not_attached_domain_type_refs": list(
            method_info.get("available_but_not_attached_domain_type_refs") or []
        ),
        "domain_attribution": dict(method_info.get("domain_attribution") or {}),
    }
    return payload


def _domain_payload(domain_info: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(domain_info.get("payload") or {})
    payload.setdefault("type_full_name", domain_info.get("type_full_name"))
    payload.setdefault(
        "short_name",
        str(payload.get("type_full_name") or "").split(".")[-1],
    )
    payload["context_window_referred_by_methods"] = list(
        domain_info.get("referred_by_methods") or []
    )
    payload["context_window_referred_by_resource_ids"] = list(
        domain_info.get("referred_by_resource_ids") or []
    )
    return payload


def _chunk_domain_names(
    method_names: list[str],
    method_map: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    for method_name in method_names:
        method_info = method_map.get(method_name) or {}
        _append_unique(out, method_info.get("domain_type_refs") or [])
    return out


def _chunk_available_domain_names(
    method_names: list[str],
    method_map: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    for method_name in method_names:
        method_info = method_map.get(method_name) or {}
        _append_unique(
            out,
            method_info.get("available_but_not_attached_domain_type_refs") or [],
        )
    return out


def _chunk_slice_domain_names(
    method_names: list[str],
    method_map: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    for method_name in method_names:
        method_info = method_map.get(method_name) or {}
        _append_unique(out, method_info.get("slice_domain_type_refs") or [])
    return out


def _chunk_resources(
    resource_ids: list[str],
    index: dict[str, Any],
) -> list[dict[str, Any]]:
    resource_map = {
        str(resource.get("resource_id")): resource
        for resource in index.get("resources", [])
        if isinstance(resource, dict) and resource.get("resource_id")
    }
    return [deepcopy(resource_map[rid]) for rid in resource_ids if rid in resource_map]


def _chunk_edges(
    *,
    index: dict[str, Any],
    method_names: set[str],
    domain_names: set[str],
) -> dict[str, list[dict[str, Any]]]:
    edges = index.get("edges") or {}
    resource_edges = [
        deepcopy(edge)
        for edge in edges.get("resource_to_primary_methods", [])
        if edge.get("method_full_name") in method_names
    ]
    body_edges = [
        deepcopy(edge)
        for edge in edges.get("primary_to_body_refs", [])
        if edge.get("primary_method") in method_names
        or edge.get("body_ref_method") in method_names
    ]
    domain_edges = [
        deepcopy(edge)
        for edge in edges.get("method_to_domain_types", [])
        if edge.get("method_full_name") in method_names
        and edge.get("type_full_name") in domain_names
    ]
    return {
        "resource_to_primary_methods": resource_edges,
        "primary_to_body_refs": body_edges,
        "method_to_domain_types": domain_edges,
    }


def build_context_chunk(
    *,
    index: dict[str, Any],
    chunk_id: str,
    method_names: list[str],
    oversized: bool = False,
    split_reason: str | None = None,
) -> dict[str, Any]:
    """Build one deduped method-centric context chunk from graph index names."""
    method_map, domain_map, cfg_map = _index_maps(index)
    methods = [
        _method_payload(method_map[name])
        for name in method_names
        if name in method_map
    ]
    resource_ids: list[str] = []
    for method in methods:
        _append_unique(resource_ids, method.get("resource_ids") or [])
    domain_names = _chunk_domain_names(method_names, method_map)
    available_domain_names = _chunk_available_domain_names(method_names, method_map)
    available_domain_names = [
        name for name in available_domain_names if name not in domain_names
    ]
    slice_domain_names = _chunk_slice_domain_names(method_names, method_map)
    domain_types = [
        _domain_payload(domain_map[name])
        for name in domain_names
        if name in domain_map
    ]
    cfg_list = [deepcopy(cfg_map[name]) for name in method_names if name in cfg_map]
    return {
        "chunk_id": chunk_id,
        "method_full_names": list(method_names),
        "resource_ids": resource_ids,
        "resources": _chunk_resources(resource_ids, index),
        "methods": methods,
        "domain_types": domain_types,
        "mandatory_domain_type_refs": domain_names,
        "available_but_not_attached_domain_type_refs": available_domain_names,
        "slice_domain_type_refs": slice_domain_names,
        "static_analysis_payload": {"method_cfg_list": cfg_list},
        "edges": _chunk_edges(
            index=index,
            method_names=set(method_names),
            domain_names=set(domain_names),
        ),
        "oversized": oversized,
        "split_reason": split_reason,
        "stats": {
            "resource_count": len(resource_ids),
            "method_count": len(methods),
            "domain_type_count": len(domain_types),
            "available_but_not_attached_domain_type_count": len(available_domain_names),
            "cfg_method_count": len(cfg_list),
        },
    }


def context_chunk_to_sliced_payload(chunk: dict[str, Any]) -> dict[str, Any]:
    """Convert a graph chunk into the sliced payload shape expected by renderers."""
    anchor_usage_overview = {
        str(resource["resource_id"]): [
            {"usage_code": usage_code, "path_overviews": []}
            for usage_code in resource.get("usage_codes", [])
        ]
        for resource in chunk.get("resources", [])
        if isinstance(resource, dict)
    }
    return {
        "meta": {
            "source": "context_evidence_graph_chunk",
            "chunk_id": chunk.get("chunk_id"),
        },
        "visible_resource_ids": list(chunk.get("resource_ids", [])),
        "anchor_usage_overview": anchor_usage_overview,
        "methods": list(chunk.get("methods", [])),
        "domain_types": list(chunk.get("domain_types", [])),
        "stats": dict(chunk.get("stats") or {}),
    }


def context_chunk_to_static_payload(chunk: dict[str, Any]) -> dict[str, Any]:
    """Return the method-CFG subset for a graph context chunk."""
    payload = chunk.get("static_analysis_payload") or {}
    cfg_list = payload.get("method_cfg_list") or []
    return {
        "method_cfg_list": list(cfg_list) if isinstance(cfg_list, list) else [],
        "meta": {
            "source": "context_evidence_graph_chunk",
            "chunk_id": chunk.get("chunk_id"),
        },
    }
