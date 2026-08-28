"""
bundle_builder.py — semantic 출력물을 target contracts로 변환하는 브리지

현재 legacy LLMResult / PredicateResponse를
PredicateBundle + PredicateEvidenceBundle 형식으로 변환한다.

설계 원칙:
- PredicateBundle은 ontology-first 상태 정의만 담는다.
- PredicateEvidenceBundle은 기계적 provenance trace만 담는다.
- provenance_scope의 해석적 판정은 reviewer가 담당하므로 semantic은 기본값으로
  'unknown'만 넣는다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, TypedDict

from ..utils import iso_now, write_json
from .api_diagnostics import llm_trace_dict

SCREEN_PROVENANCE_KEY = "__screen__"

if TYPE_CHECKING:
    from .llm_client import LLMResult


class MechanicalProvenanceSeed(TypedDict, total=False):
    """semantic 단계가 기계적으로 채울 수 있는 provenance trace."""

    element_uids: list[str]
    resource_ids: list[str]
    anchor_uids: list[str]
    method_uids: list[str]
    evidence_uids: list[str]
    handler_uids: list[str]
    type_uids: list[str]
    notes: str


def _header(
    *,
    artifact_type: str,
    run_id: str,
    producer: str,
    parents: list[str] | None = None,
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "schema_version": "0.1.0",
        "artifact_type": artifact_type,
        "producer": producer,
        "created_at": iso_now(),
        "run_id": run_id,
    }
    if parents:
        header["parents"] = parents
    return header


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower())
    return slug.strip("_") or "unnamed"


def _make_predicate_uid(name: str, used: set[str]) -> str:
    base = f"pred_{_slugify(name)}"
    candidate = base
    i = 2
    while candidate in used:
        candidate = f"{base}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def _make_predicate_items(
    result: LLMResult,
    *,
    screen_context: str | None = None,
) -> list[dict[str, Any]]:
    used_uids: set[str] = set()
    predicates: list[dict[str, Any]] = []

    for pred in result.response.State_Definitions:
        item: dict[str, Any] = {
            "predicate_uid": _make_predicate_uid(pred.name, used_uids),
            "predicate_name": pred.name,
            "description": pred.description,
            "variables": [var.model_dump(exclude_none=True) for var in pred.variables],
        }
        if screen_context:
            item["screen_context"] = screen_context
        predicates.append(item)

    return predicates


def build_predicate_bundle(
    result: LLMResult,
    *,
    run_id: str,
    fusion_ref: str | None = None,
    screen_context: str | None = None,
    producer: str = "semantic",
) -> dict[str, Any]:
    """LLMResult를 ontology-first PredicateBundle로 변환한다."""
    predicates = _make_predicate_items(result, screen_context=screen_context)
    bundle: dict[str, Any] = {
        "header": _header(
            artifact_type="PredicateBundle",
            run_id=run_id,
            producer=producer,
            parents=[f"FusionBundle:{fusion_ref}"] if fusion_ref else None,
        ),
        "variant": result.variant,
        "predicates": predicates,
        "llm_model": result.model,
        "llm_trace": llm_trace_dict(
            result,
            analysis_reasoning=result.response.Analysis,
        ),
    }
    if fusion_ref:
        bundle["fusion_ref"] = fusion_ref
    return bundle


def _evidence_entry(
    *,
    predicate_uid: str,
    seed: MechanicalProvenanceSeed | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "predicate_evidence_uid": f"pe_{predicate_uid}",
        "predicate_uid": predicate_uid,
        "provenance_scope": "unknown",
    }
    if not seed:
        return entry

    for key in (
        "element_uids",
        "resource_ids",
        "anchor_uids",
        "method_uids",
        "evidence_uids",
        "handler_uids",
        "type_uids",
    ):
        values = seed.get(key)
        if values:
            entry[key] = list(values)

    notes = seed.get("notes")
    if notes:
        entry["notes"] = notes
    return entry


def build_predicate_evidence_bundle(
    predicate_bundle: dict[str, Any],
    *,
    run_id: str,
    fusion_ref: str | None = None,
    static_semantic_ref: str | None = None,
    provenance_by_predicate_name: Mapping[str, MechanicalProvenanceSeed] | None = None,
    producer: str = "semantic",
) -> dict[str, Any]:
    """PredicateBundle과 mechanical provenance seeds를 합쳐 PredicateEvidenceBundle을 만든다."""
    predicate_entries = predicate_bundle.get("predicates") or []
    evidence_entries: list[dict[str, Any]] = []

    for pred in predicate_entries:
        if not isinstance(pred, dict):
            continue
        predicate_name = str(pred.get("predicate_name") or "")
        predicate_uid = str(pred.get("predicate_uid") or "")
        if not predicate_uid:
            continue
        seed = None
        if provenance_by_predicate_name:
            seed = provenance_by_predicate_name.get(predicate_name)
            if seed is None:
                seed = provenance_by_predicate_name.get(SCREEN_PROVENANCE_KEY)
        evidence_entries.append(_evidence_entry(predicate_uid=predicate_uid, seed=seed))

    bundle: dict[str, Any] = {
        "header": _header(
            artifact_type="PredicateEvidenceBundle",
            run_id=run_id,
            producer=producer,
            parents=[f"PredicateBundle:{run_id}"],
        ),
        "predicate_bundle_ref": run_id,
        "predicate_evidence": evidence_entries,
    }
    if fusion_ref:
        bundle["fusion_ref"] = fusion_ref
    if static_semantic_ref:
        bundle["static_semantic_ref"] = static_semantic_ref
    return bundle


def build_semantic_contract_bundles(
    result: LLMResult,
    *,
    run_id: str,
    fusion_ref: str | None = None,
    static_semantic_ref: str | None = None,
    screen_context: str | None = None,
    provenance_by_predicate_name: Mapping[str, MechanicalProvenanceSeed] | None = None,
    producer: str = "semantic",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """semantic 단계의 target contracts를 한 번에 생성한다."""
    predicate_bundle = build_predicate_bundle(
        result,
        run_id=run_id,
        fusion_ref=fusion_ref,
        screen_context=screen_context,
        producer=producer,
    )
    evidence_bundle = build_predicate_evidence_bundle(
        predicate_bundle,
        run_id=run_id,
        fusion_ref=fusion_ref,
        static_semantic_ref=static_semantic_ref,
        provenance_by_predicate_name=provenance_by_predicate_name,
        producer=producer,
    )
    return predicate_bundle, evidence_bundle


def save_semantic_contract_bundles(
    output_dir: Path,
    predicate_bundle: dict[str, Any],
    evidence_bundle: dict[str, Any],
) -> dict[str, Path]:
    """생성된 semantic contract bundle들을 output_dir 아래 JSON 파일로 저장한다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    predicate_path = output_dir / "predicate_bundle.json"
    evidence_path = output_dir / "predicate_evidence_bundle.json"
    write_json(predicate_path, predicate_bundle)
    write_json(evidence_path, evidence_bundle)
    return {
        "predicate_bundle": predicate_path,
        "predicate_evidence_bundle": evidence_path,
    }


def build_and_save_semantic_contract_bundles(
    output_dir: Path,
    result: LLMResult,
    *,
    run_id: str,
    fusion_ref: str | None = None,
    static_semantic_ref: str | None = None,
    screen_context: str | None = None,
    provenance_by_predicate_name: Mapping[str, MechanicalProvenanceSeed] | None = None,
    producer: str = "semantic",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path]]:
    """semantic target contracts를 생성하고 JSON 파일까지 저장한다."""
    predicate_bundle, evidence_bundle = build_semantic_contract_bundles(
        result,
        run_id=run_id,
        fusion_ref=fusion_ref,
        static_semantic_ref=static_semantic_ref,
        screen_context=screen_context,
        provenance_by_predicate_name=provenance_by_predicate_name,
        producer=producer,
    )
    paths = save_semantic_contract_bundles(output_dir, predicate_bundle, evidence_bundle)
    return predicate_bundle, evidence_bundle, paths
