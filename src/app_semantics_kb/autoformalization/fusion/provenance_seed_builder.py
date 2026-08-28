"""
provenance_seed_builder.py — fusion/static-analysis 입력에서 기계적 provenance seed를 생성

semantic 단계가 reviewer-like 해석 없이 PredicateEvidenceBundle의 초기 trace를 만들 수 있도록,
현재 이용 가능한 sliced_methods_payload / static_analysis_payload에서 screen-level seed를 추출한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypedDict

from ..utils import read_json

SCREEN_PROVENANCE_KEY = "__screen__"


class MechanicalProvenanceSeedPayload(TypedDict, total=False):
    element_uids: list[str]
    resource_ids: list[str]
    anchor_uids: list[str]
    method_uids: list[str]
    evidence_uids: list[str]
    handler_uids: list[str]
    type_uids: list[str]
    notes: str


def _slug_uid(prefix: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_") or "unknown"
    return f"{prefix}_{slug}"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _from_sliced_methods_payload(payload: dict[str, Any]) -> MechanicalProvenanceSeedPayload:
    methods = payload.get("methods") or []
    domain_types = payload.get("domain_types") or []
    visible_resource_ids = payload.get("visible_resource_ids") or []

    method_uids: list[str] = []
    handler_uids: list[str] = []
    type_uids: list[str] = []

    for method in methods:
        if not isinstance(method, dict):
            continue
        full_name = str(method.get("method_full_name") or "").strip()
        if full_name:
            method_uids.append(_slug_uid("method", full_name))

        for bcp in (method.get("backward_call_paths") or []):
            if not isinstance(bcp, dict):
                continue
            root_callback = str(bcp.get("root_callback") or "").strip()
            callback_kind = str(bcp.get("callback_kind") or "OTHER").strip().lower()
            if root_callback:
                handler_uids.append(_slug_uid("handler", f"{callback_kind}:{root_callback}"))

    for type_entry in domain_types:
        if not isinstance(type_entry, dict):
            continue
        type_name = str(type_entry.get("type_full_name") or type_entry.get("short_name") or "").strip()
        if type_name:
            type_uids.append(_slug_uid("type", type_name))

    return {
        "resource_ids": _dedupe([str(r) for r in visible_resource_ids if isinstance(r, str)]),
        "method_uids": _dedupe(method_uids),
        "handler_uids": _dedupe(handler_uids),
        "type_uids": _dedupe(type_uids),
    }


def _from_static_analysis_payload(payload: dict[str, Any]) -> MechanicalProvenanceSeedPayload:
    method_uids: list[str] = []

    cfg_list = payload.get("method_cfg_list") or []
    for cfg in cfg_list:
        if not isinstance(cfg, dict):
            continue
        analysis = cfg.get("analysis") or {}
        if not isinstance(analysis, dict):
            continue
        method = analysis.get("method") or {}
        if not isinstance(method, dict):
            continue
        full_name = str(method.get("fullName") or method.get("name") or "").strip()
        if full_name:
            method_uids.append(_slug_uid("method", full_name))

    return {
        "method_uids": _dedupe(method_uids),
    }


def build_screen_provenance_seed(
    *,
    sliced_methods_payload: dict[str, Any] | None = None,
    static_analysis_payload: dict[str, Any] | None = None,
) -> MechanicalProvenanceSeedPayload:
    """screen-level 기계적 provenance seed를 생성한다."""
    seed: MechanicalProvenanceSeedPayload = {}

    if sliced_methods_payload:
        sliced_seed = _from_sliced_methods_payload(sliced_methods_payload)
        for key, values in sliced_seed.items():
            if key == "notes":
                continue
            seed[key] = _dedupe(list(seed.get(key, [])) + list(values))

    if static_analysis_payload:
        static_seed = _from_static_analysis_payload(static_analysis_payload)
        for key, values in static_seed.items():
            if key == "notes":
                continue
            seed[key] = _dedupe(list(seed.get(key, [])) + list(values))

    notes: list[str] = []
    if sliced_methods_payload:
        notes.append("derived from sliced_methods_payload")
    if static_analysis_payload:
        notes.append("derived from static_analysis_payload")
    if notes:
        seed["notes"] = "; ".join(notes)

    return seed


def build_provenance_seed_map(
    *,
    sliced_methods_payload: dict[str, Any] | None = None,
    static_analysis_payload: dict[str, Any] | None = None,
) -> dict[str, MechanicalProvenanceSeedPayload]:
    """현재는 screen-level fallback seed만 제공한다."""
    screen_seed = build_screen_provenance_seed(
        sliced_methods_payload=sliced_methods_payload,
        static_analysis_payload=static_analysis_payload,
    )
    if not screen_seed:
        return {}
    return {SCREEN_PROVENANCE_KEY: screen_seed}


def load_optional_payload(path: Path | None) -> dict[str, Any] | None:
    """JSON payload 파일을 선택적으로 로드한다."""
    if path is None:
        return None
    return read_json(path)
