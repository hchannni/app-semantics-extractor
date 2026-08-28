"""Budget-aware packing for deduped context evidence graphs."""

from __future__ import annotations

import json
from typing import Any, Callable

from .evidence_index import build_context_chunk
from .rendering import render_chunk_evidence


EstimateFn = Callable[[list[str]], int]


def _method_name(method: dict[str, Any]) -> str:
    return str(method.get("method_full_name") or "").strip()


def _type_name(domain_type: dict[str, Any]) -> str:
    return str(domain_type.get("type_full_name") or "").strip()


def _cfg_method_name(cfg: dict[str, Any]) -> str:
    method = (cfg.get("analysis") or {}).get("method") or {}
    return str(method.get("fullName") or "").strip()


def _stable_json_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _domain_type_rough_chars(domain_type: dict[str, Any]) -> int:
    body = domain_type.get("body")
    if isinstance(body, str):
        return len(body) + len(str(domain_type.get("type_full_name") or "")) + len(
            str(domain_type.get("file") or "")
        )
    return _stable_json_chars(domain_type)


def _domain_info_rough_chars(domain_info: dict[str, Any]) -> int:
    payload = domain_info.get("payload") or {}
    return _domain_type_rough_chars(payload) if isinstance(payload, dict) else 0


def _render_char_counts(chunk: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for mode in ("v3", "v4", "critic"):
        counts[mode] = len(render_chunk_evidence(chunk, mode=mode))  # type: ignore[arg-type]
    return counts


def _estimated_chars(chunk: dict[str, Any]) -> int:
    counts = _render_char_counts(chunk)
    chunk["rendered_char_counts"] = counts
    chunk["estimated_chars"] = max(counts.values(), default=0)
    return int(chunk["estimated_chars"])


def _draft_chunk(
    *,
    index: dict[str, Any],
    method_names: list[str],
) -> dict[str, Any]:
    chunk = build_context_chunk(
        index=index,
        chunk_id="chunk_0000",
        method_names=method_names,
    )
    _estimated_chars(chunk)
    return chunk


def _finalize_chunk(
    *,
    index: dict[str, Any],
    chunk_id: str,
    method_names: list[str],
    oversized: bool = False,
    split_reason: str | None = None,
) -> dict[str, Any]:
    chunk = build_context_chunk(
        index=index,
        chunk_id=chunk_id,
        method_names=method_names,
        oversized=oversized,
        split_reason=split_reason,
    )
    _estimated_chars(chunk)
    return chunk


def _ordered_method_names(index: dict[str, Any]) -> list[str]:
    return [
        str(name).strip()
        for name in index.get("ordered_method_full_names", [])
        if str(name).strip()
    ]


def _method_infos(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(entry.get("method_full_name")): entry
        for entry in index.get("methods", [])
        if isinstance(entry, dict) and entry.get("method_full_name")
    }


def _domain_infos(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(entry.get("type_full_name")): entry
        for entry in index.get("domain_types", [])
        if isinstance(entry, dict) and entry.get("type_full_name")
    }


def _method_domain_refs(index: dict[str, Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for method_name, info in _method_infos(index).items():
        raw_refs = info.get("domain_type_refs") or []
        refs[method_name] = [str(ref) for ref in raw_refs if str(ref).strip()]
    return refs


def _domain_rough_chars(index: dict[str, Any]) -> dict[str, int]:
    return {
        name: _domain_info_rough_chars(info)
        for name, info in _domain_infos(index).items()
    }


def _sort_methods(
    method_names: list[str],
    method_order: dict[str, int],
) -> list[str]:
    return sorted(method_names, key=lambda name: (method_order.get(name, 10**9), name))


def _estimate_function(index: dict[str, Any]) -> EstimateFn:
    cache: dict[tuple[str, ...], int] = {}

    def estimate(method_names: list[str]) -> int:
        key = tuple(method_names)
        if key not in cache:
            cache[key] = int(
                _draft_chunk(index=index, method_names=method_names).get(
                    "estimated_chars",
                    0,
                )
            )
        return cache[key]

    return estimate


def _partition_oversized_methods(
    method_names: list[str],
    estimate: EstimateFn,
    max_chars: int,
) -> tuple[list[str], list[str]]:
    assignable: list[str] = []
    oversized: list[str] = []
    for method_name in method_names:
        if estimate([method_name]) > max_chars:
            oversized.append(method_name)
        else:
            assignable.append(method_name)
    return assignable, oversized


def _domain_refs_for_unassigned(
    unassigned: set[str],
    method_domains: dict[str, list[str]],
    method_order: dict[str, int],
) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    ordered = sorted(unassigned, key=lambda name: (method_order.get(name, 10**9), name))
    for method_name in ordered:
        for domain_name in method_domains.get(method_name, []):
            refs.setdefault(domain_name, []).append(method_name)
    return refs


def _best_shared_domain(
    refs_by_domain: dict[str, list[str]],
    domain_chars: dict[str, int],
    method_order: dict[str, int],
) -> tuple[str, list[str]] | None:
    candidates: list[tuple[int, int, int, str, list[str]]] = []
    for domain_name, refs in refs_by_domain.items():
        if len(refs) < 2:
            continue
        score = domain_chars.get(domain_name, 0) * (len(refs) - 1)
        if score <= 0:
            continue
        earliest = min(method_order.get(method, 10**9) for method in refs)
        candidates.append((-score, -len(refs), earliest, domain_name, refs))
    if not candidates:
        return None
    _, _, _, domain_name, refs = sorted(candidates)[0]
    return domain_name, refs


def _grow_seed_domain_chunk(
    seed_methods: list[str],
    *,
    estimate: EstimateFn,
    max_chars: int,
    method_order: dict[str, int],
) -> list[str]:
    selected: list[str] = []
    for method_name in seed_methods:
        candidate = _sort_methods(selected + [method_name], method_order)
        if estimate(candidate) <= max_chars:
            selected = candidate
    return selected


def _domain_overlap_score(
    method_name: str,
    selected_domains: set[str],
    method_domains: dict[str, list[str]],
    domain_chars: dict[str, int],
) -> int:
    return sum(
        max(domain_chars.get(domain_name, 0), 1)
        for domain_name in method_domains.get(method_name, [])
        if domain_name in selected_domains
    )


def _backfill_by_domain_overlap(
    selected: list[str],
    unassigned: set[str],
    *,
    estimate: EstimateFn,
    target_chars: int,
    max_chars: int,
    method_domains: dict[str, list[str]],
    domain_chars: dict[str, int],
    method_order: dict[str, int],
) -> list[str]:
    selected_domains = _selected_domains(selected, method_domains)
    while selected and estimate(selected) < target_chars:
        candidates = _overlap_candidates(
            selected,
            unassigned,
            selected_domains,
            method_domains,
            domain_chars,
            method_order,
        )
        added = _add_first_fitting_candidate(
            selected,
            candidates,
            estimate=estimate,
            max_chars=max_chars,
            method_order=method_order,
        )
        if added == selected:
            break
        selected = added
        selected_domains = _selected_domains(selected, method_domains)
    return selected


def _selected_domains(
    selected: list[str],
    method_domains: dict[str, list[str]],
) -> set[str]:
    return {
        domain_name
        for method_name in selected
        for domain_name in method_domains.get(method_name, [])
    }


def _overlap_candidates(
    selected: list[str],
    unassigned: set[str],
    selected_domains: set[str],
    method_domains: dict[str, list[str]],
    domain_chars: dict[str, int],
    method_order: dict[str, int],
) -> list[str]:
    selected_set = set(selected)
    scored: list[tuple[int, int, str]] = []
    for method_name in unassigned - selected_set:
        score = _domain_overlap_score(
            method_name,
            selected_domains,
            method_domains,
            domain_chars,
        )
        if score > 0:
            scored.append((-score, method_order.get(method_name, 10**9), method_name))
    return [method_name for _, _, method_name in sorted(scored)]


def _add_first_fitting_candidate(
    selected: list[str],
    candidates: list[str],
    *,
    estimate: EstimateFn,
    max_chars: int,
    method_order: dict[str, int],
) -> list[str]:
    for method_name in candidates:
        candidate = _sort_methods(selected + [method_name], method_order)
        if estimate(candidate) <= max_chars:
            return candidate
    return selected


def _shared_domain_specs(
    *,
    unassigned: set[str],
    estimate: EstimateFn,
    target_chars: int,
    max_chars: int,
    method_domains: dict[str, list[str]],
    domain_chars: dict[str, int],
    method_order: dict[str, int],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    while True:
        refs_by_domain = _domain_refs_for_unassigned(
            unassigned,
            method_domains,
            method_order,
        )
        best = _best_shared_domain(refs_by_domain, domain_chars, method_order)
        if best is None:
            break
        domain_name, seed_methods = best
        selected = _grow_seed_domain_chunk(
            seed_methods,
            estimate=estimate,
            max_chars=max_chars,
            method_order=method_order,
        )
        if not selected:
            break
        selected = _backfill_by_domain_overlap(
            selected,
            unassigned,
            estimate=estimate,
            target_chars=target_chars,
            max_chars=max_chars,
            method_domains=method_domains,
            domain_chars=domain_chars,
            method_order=method_order,
        )
        _remove_selected(unassigned, selected)
        specs.append(_chunk_spec(selected, split_reason=f"shared_domain:{domain_name}"))
    return specs


def _remove_selected(unassigned: set[str], selected: list[str]) -> None:
    for method_name in selected:
        unassigned.discard(method_name)


def _chunk_spec(
    method_names: list[str],
    *,
    oversized: bool = False,
    split_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "method_names": method_names,
        "oversized": oversized,
        "split_reason": split_reason,
    }


def _ordered_method_specs(
    method_names: list[str],
    *,
    estimate: EstimateFn,
    target_chars: int,
    max_chars: int,
    method_order: dict[str, int],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    current: list[str] = []
    for method_name in method_names:
        if current and estimate(current) >= target_chars:
            specs.append(_chunk_spec(current))
            current = []
        candidate = _sort_methods(current + [method_name], method_order)
        if current and estimate(candidate) > max_chars:
            specs.append(_chunk_spec(current))
            current = [method_name]
        else:
            current = candidate
    if current:
        specs.append(_chunk_spec(current))
    return specs


def _oversized_specs(method_names: list[str]) -> list[dict[str, Any]]:
    return [
        _chunk_spec(
            [method_name],
            oversized=True,
            split_reason="single_method_full_evidence_over_max_chars",
        )
        for method_name in method_names
    ]


def _sort_specs(
    specs: list[dict[str, Any]],
    method_order: dict[str, int],
) -> list[dict[str, Any]]:
    return sorted(
        specs,
        key=lambda spec: min(
            method_order.get(name, 10**9)
            for name in spec.get("method_names", [])
        ),
    )


def _post_merge_specs(
    specs: list[dict[str, Any]],
    *,
    estimate: EstimateFn,
    max_chars: int,
    method_domains: dict[str, list[str]],
    domain_chars: dict[str, int],
    method_order: dict[str, int],
) -> list[dict[str, Any]]:
    merged = list(specs)
    while True:
        best = _best_merge_pair(
            merged,
            estimate=estimate,
            max_chars=max_chars,
            method_domains=method_domains,
            domain_chars=domain_chars,
            method_order=method_order,
        )
        if best is None:
            return merged
        left, right, combined = best
        merged[right] = combined
        del merged[left]


def _best_merge_pair(
    specs: list[dict[str, Any]],
    *,
    estimate: EstimateFn,
    max_chars: int,
    method_domains: dict[str, list[str]],
    domain_chars: dict[str, int],
    method_order: dict[str, int],
) -> tuple[int, int, dict[str, Any]] | None:
    best_key: tuple[int, int, int, int, int] | None = None
    best_value: tuple[int, int, dict[str, Any]] | None = None
    for i, source in enumerate(specs):
        if source.get("oversized"):
            continue
        for j, target in enumerate(specs):
            if i == j or target.get("oversized"):
                continue
            combined_methods = _sort_methods(
                list(target.get("method_names") or [])
                + list(source.get("method_names") or []),
                method_order,
            )
            size = estimate(combined_methods)
            if size > max_chars:
                continue
            overlap = _spec_domain_overlap_chars(
                source,
                target,
                method_domains=method_domains,
                domain_chars=domain_chars,
            )
            combined = _chunk_spec(combined_methods, split_reason="post_pack_merge")
            candidate_key = (
                -overlap,
                max_chars - size,
                len(list(source.get("method_names") or [])),
                max(i, j),
                min(i, j),
            )
            if best_key is None or candidate_key < best_key:
                best_key = candidate_key
                best_value = (max(i, j), min(i, j), combined)
    return best_value


def _spec_domain_overlap_chars(
    source: dict[str, Any],
    target: dict[str, Any],
    *,
    method_domains: dict[str, list[str]],
    domain_chars: dict[str, int],
) -> int:
    source_domains = _selected_domains(
        list(source.get("method_names") or []),
        method_domains,
    )
    target_domains = _selected_domains(
        list(target.get("method_names") or []),
        method_domains,
    )
    return sum(domain_chars.get(domain_name, 0) for domain_name in source_domains & target_domains)


def _finalize_specs(
    *,
    index: dict[str, Any],
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for idx, spec in enumerate(specs, start=1):
        chunks.append(
            _finalize_chunk(
                index=index,
                chunk_id=f"chunk_{idx:04d}",
                method_names=list(spec.get("method_names") or []),
                oversized=bool(spec.get("oversized")),
                split_reason=spec.get("split_reason"),
            )
        )
    return chunks


def _add_location(locations: dict[str, list[str]], key: str, chunk_id: str) -> None:
    if not key:
        return
    locations.setdefault(key, [])
    if chunk_id not in locations[key]:
        locations[key].append(chunk_id)


def _duplicates_list(
    key_name: str,
    locations: dict[str, list[str]],
) -> list[dict[str, Any]]:
    return [
        {key_name: key, "chunk_ids": chunk_ids}
        for key, chunk_ids in sorted(locations.items())
        if len(chunk_ids) > 1
    ]


def _domain_duplicates_list(
    locations: dict[str, list[str]],
    rough_chars: dict[str, int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, chunk_ids in sorted(locations.items()):
        if len(chunk_ids) <= 1:
            continue
        chars = rough_chars.get(key, 0)
        out.append(
            {
                "type_full_name": key,
                "chunk_ids": chunk_ids,
                "occurrence_count": len(chunk_ids),
                "rough_chars": chars,
                "duplicated_rough_chars": chars * (len(chunk_ids) - 1),
            }
        )
    return out


def _shared_evidence_duplicates(
    chunks: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    method_locations: dict[str, list[str]] = {}
    domain_locations: dict[str, list[str]] = {}
    domain_chars: dict[str, int] = {}
    cfg_locations: dict[str, list[str]] = {}
    for chunk in chunks:
        _record_chunk_locations(
            chunk,
            method_locations=method_locations,
            domain_locations=domain_locations,
            domain_chars=domain_chars,
            cfg_locations=cfg_locations,
        )
    return {
        "methods": _duplicates_list("method_full_name", method_locations),
        "domain_types": _domain_duplicates_list(domain_locations, domain_chars),
        "cfg_methods": _duplicates_list("method_full_name", cfg_locations),
    }


def _record_chunk_locations(
    chunk: dict[str, Any],
    *,
    method_locations: dict[str, list[str]],
    domain_locations: dict[str, list[str]],
    domain_chars: dict[str, int],
    cfg_locations: dict[str, list[str]],
) -> None:
    chunk_id = str(chunk["chunk_id"])
    for method in chunk.get("methods", []):
        if isinstance(method, dict):
            _add_location(method_locations, _method_name(method), chunk_id)
    for domain_type in chunk.get("domain_types", []):
        if isinstance(domain_type, dict):
            type_name = _type_name(domain_type)
            _add_location(domain_locations, type_name, chunk_id)
            domain_chars[type_name] = _domain_type_rough_chars(domain_type)
    cfg_items = _chunk_cfg_items(chunk)
    for cfg in cfg_items:
        if isinstance(cfg, dict):
            _add_location(cfg_locations, _cfg_method_name(cfg), chunk_id)


def _chunk_cfg_items(chunk: dict[str, Any]) -> list[Any]:
    cfg_list = (chunk.get("static_analysis_payload") or {}).get(
        "method_cfg_list",
        [],
    )
    return cfg_list if isinstance(cfg_list, list) else []


def _domain_duplicate_stats(
    duplicates: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    domain_duplicates = duplicates.get("domain_types") or []
    return {
        "domain_type_cross_chunk_duplicate_entry_count": len(domain_duplicates),
        "domain_type_cross_chunk_duplicate_occurrence_count": sum(
            max(len(entry.get("chunk_ids") or []) - 1, 0)
            for entry in domain_duplicates
        ),
        "domain_type_cross_chunk_duplicated_rough_chars": sum(
            int(entry.get("duplicated_rough_chars") or 0)
            for entry in domain_duplicates
        ),
    }


def _chunk_record(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": chunk["chunk_id"],
        "resource_ids_covered": chunk.get("resource_ids", []),
        "method_full_names": chunk.get("method_full_names", []),
        "mandatory_domain_type_refs": chunk.get("mandatory_domain_type_refs", []),
        "available_but_not_attached_domain_type_refs": chunk.get(
            "available_but_not_attached_domain_type_refs",
            [],
        ),
        "rendered_char_counts": chunk.get("rendered_char_counts", {}),
        "estimated_chars": chunk.get("estimated_chars", 0),
        "oversized": bool(chunk.get("oversized")),
        "split_reason": chunk.get("split_reason"),
        "stats": chunk.get("stats", {}),
    }


def _dedupe_savings(index: dict[str, Any]) -> dict[str, int]:
    stats = index.get("stats") or {}
    return {
        "method_duplicate_occurrences_avoided": int(
            stats.get("method_duplicate_occurrences_avoided") or 0
        ),
        "domain_type_duplicate_occurrences_avoided": int(
            stats.get("domain_type_duplicate_occurrences_avoided") or 0
        ),
        "cfg_duplicate_occurrences_avoided": int(
            stats.get("cfg_duplicate_occurrences_avoided") or 0
        ),
    }


def _domain_attribution_summary(index: dict[str, Any]) -> dict[str, Any]:
    raw = index.get("domain_attribution") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        "policy": raw.get("policy"),
        "original_slice_domain_type_count": raw.get(
            "original_slice_domain_type_count",
            0,
        ),
        "mandatory_domain_type_count": raw.get("mandatory_domain_type_count", 0),
        "omitted_from_all_chunks_count": raw.get("omitted_from_all_chunks_count", 0),
        "omitted_from_all_chunks": list(raw.get("omitted_from_all_chunks") or []),
        "available_but_not_attached_by_method": dict(
            raw.get("available_but_not_attached_by_method") or {}
        ),
    }


def pack_evidence_graph_chunks(
    index: dict[str, Any],
    *,
    target_chars: int = 400000,
    max_chars: int = 500000,
) -> dict[str, Any]:
    """Pack unique methods by large shared evidence while keeping chunks complete."""
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    effective_target = min(target_chars, max_chars)
    method_names = _ordered_method_names(index)
    method_order = {name: idx for idx, name in enumerate(method_names)}
    estimate = _estimate_function(index)
    method_domains = _method_domain_refs(index)
    domain_chars = _domain_rough_chars(index)
    assignable, oversized = _partition_oversized_methods(
        method_names,
        estimate,
        max_chars,
    )
    unassigned = set(assignable)
    specs = _shared_domain_specs(
        unassigned=unassigned,
        estimate=estimate,
        target_chars=effective_target,
        max_chars=max_chars,
        method_domains=method_domains,
        domain_chars=domain_chars,
        method_order=method_order,
    )
    remaining = _sort_methods(list(unassigned), method_order)
    specs.extend(
        _ordered_method_specs(
            remaining,
            estimate=estimate,
            target_chars=effective_target,
            max_chars=max_chars,
            method_order=method_order,
        )
    )
    specs.extend(_oversized_specs(_sort_methods(oversized, method_order)))
    pre_merge_specs = _sort_specs(specs, method_order)
    post_merge_specs = _post_merge_specs(
        pre_merge_specs,
        estimate=estimate,
        max_chars=max_chars,
        method_domains=method_domains,
        domain_chars=domain_chars,
        method_order=method_order,
    )
    chunks = _finalize_specs(index=index, specs=_sort_specs(post_merge_specs, method_order))
    duplicates = _shared_evidence_duplicates(chunks)
    return _packed_result(
        index=index,
        chunks=chunks,
        duplicates=duplicates,
        method_count=len(method_names),
        pre_merge_chunk_count=len(pre_merge_specs),
        target_chars=target_chars,
        max_chars=max_chars,
    )


def _packed_result(
    *,
    index: dict[str, Any],
    chunks: list[dict[str, Any]],
    duplicates: dict[str, list[dict[str, Any]]],
    method_count: int,
    pre_merge_chunk_count: int,
    target_chars: int,
    max_chars: int,
) -> dict[str, Any]:
    stats = {
        "chunk_count": len(chunks),
        "pre_merge_chunk_count": pre_merge_chunk_count,
        "post_merge_chunk_count": len(chunks),
        "post_merge_savings": max(pre_merge_chunk_count - len(chunks), 0),
        "method_count": method_count,
        "resource_count": len(index.get("resources", []) or []),
        "covered_resource_count": len(
            {rid for chunk in chunks for rid in chunk.get("resource_ids", [])}
        ),
        "oversized_chunk_count": len([c for c in chunks if c.get("oversized")]),
    }
    stats.update(_domain_duplicate_stats(duplicates))
    return {
        "meta": {
            "source": "shared_evidence_aware_evidence_graph_packer",
            "budget_basis": "max_rendered_chars_across_v3_v4_critic",
            "target_chars": target_chars,
            "max_chars": max_chars,
            "resource_id_policy": "provenance_anchor_not_chunk_boundary",
            "shared_evidence_policy": "cluster_methods_by_large_shared_domain_evidence",
        },
        "chunks": chunks,
        "chunk_records": [_chunk_record(chunk) for chunk in chunks],
        "shared_evidence_duplicates": duplicates,
        "domain_attribution": _domain_attribution_summary(index),
        "dedupe_savings": _dedupe_savings(index),
        "stats": stats,
    }


def slim_chunks_manifest(packed: dict[str, Any]) -> dict[str, Any]:
    """Return an audit manifest without duplicated source bodies."""
    return {
        "meta": dict(packed.get("meta") or {}),
        "chunks": list(packed.get("chunk_records") or []),
        "shared_evidence_duplicates": dict(
            packed.get("shared_evidence_duplicates") or {}
        ),
        "domain_attribution": dict(packed.get("domain_attribution") or {}),
        "dedupe_savings": dict(packed.get("dedupe_savings") or {}),
        "stats": dict(packed.get("stats") or {}),
    }
