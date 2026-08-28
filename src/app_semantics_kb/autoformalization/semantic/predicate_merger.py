"""
predicate_merger.py — State Predicate 병합 및 영속 저장 모듈

POC(predicate_generation_poc.py)의 merge_state_definitions / merge_variables /
normalize_value_description_keys 로직을 semantic 모듈 내 독립 파일로 이식한다.

병합 정책:
  - Predicate는 `name`을 Key로 upsert (동명이면 description·variables를 갱신)
  - Variable은 `name`을 Key로 upsert (동명이면 필드를 덮어씀)
  - `value_options` 키는 대소문자 혼용을 `value_options`으로 정규화
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..utils import log, read_json, write_json
from .llm_client import LLMResult


# ─────────────────────────────────────────────
# 키 정규화
# ─────────────────────────────────────────────

def normalize_value_description_keys(obj: Any) -> Any:
    """재귀적으로 구 키('value_description', 'Value_Description')를 'value_options'으로 마이그레이션한다."""
    if isinstance(obj, dict):
        return {
            ("value_options" if k in ("value_options", "Value_Options", "value_description", "Value_Description") else k): normalize_value_description_keys(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [normalize_value_description_keys(x) for x in obj]
    return obj


# ─────────────────────────────────────────────
# Variable 병합
# ─────────────────────────────────────────────

def merge_variables(
    old_vars: list[dict[str, Any]],
    new_vars: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """두 Variable 리스트를 name 기준 upsert로 병합한다.

    - 동명 Variable이 있으면 new의 필드로 덮어쓴다.
    - 새 Variable은 순서 보존하며 추가한다.
    """
    old_vars = old_vars or []
    new_vars = new_vars or []

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for v in old_vars:
        if not isinstance(v, dict):
            continue
        v = normalize_value_description_keys(v)
        name = (v.get("name") or "").strip()
        if not name:
            continue
        if name not in merged:
            order.append(name)
            merged[name] = dict(v)
        else:
            merged[name].update(v)

    for v in new_vars:
        if not isinstance(v, dict):
            continue
        v = normalize_value_description_keys(v)
        name = (v.get("name") or "").strip()
        if not name:
            continue
        if name in merged:
            merged[name].update(v)
        else:
            order.append(name)
            merged[name] = dict(v)

    return [merged[n] for n in order]


# ─────────────────────────────────────────────
# Predicate 병합
# ─────────────────────────────────────────────

def merge_state_definitions(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """두 State Predicate 리스트를 name 기준 upsert로 병합한다.

    - 동명 Predicate가 있으면 variables를 merge_variables()로 병합하고,
      나머지 필드(description 등)는 new 값으로 덮어쓴다.
    - 신규 Predicate는 순서 보존하며 추가한다.
    """
    existing = existing or []
    new = new or []

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _upsert(p: dict[str, Any]) -> None:
        p = normalize_value_description_keys(p)
        name = (p.get("name") or "").strip()
        if not name:
            return

        if name not in merged:
            order.append(name)
            entry = dict(p)
            entry.setdefault("variables", [])
            merged[name] = entry
            return

        entry = merged[name]
        entry["variables"] = merge_variables(
            entry.get("variables", []),
            p.get("variables", []),
        )
        # evidence는 최신 값으로 덮어쓰기 (누적 merge 하지 않음)
        entry["evidence"] = _merge_evidence(
            entry.get("evidence"),
            p.get("evidence"),
        )
        for k, v in p.items():
            if k in ("variables", "evidence"):
                continue
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            entry[k] = v

    for p in existing:
        if isinstance(p, dict):
            _upsert(p)
    for p in new:
        if isinstance(p, dict):
            _upsert(p)

    return [merged[n] for n in order]


def _merge_evidence(
    old_evidence: dict[str, Any] | None,
    new_evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """evidence 필드 병합 정책: 새 evidence가 있으면 덮어쓴다.

    evidence는 특정 page/호출 시점에서의 grounding을 기록하는 audit trail이므로
    variables처럼 누적 merge하지 않고 가장 최신 값을 채택한다.
    """
    if new_evidence is not None:
        return new_evidence
    return old_evidence


# ─────────────────────────────────────────────
# LLMResult 연동
# ─────────────────────────────────────────────

def load_and_merge_predicates(
    predicate_path: Path,
    new_result: LLMResult,
) -> list[dict[str, Any]]:
    """기존 predicates.json을 읽어 LLMResult의 Predicate를 병합한 새 리스트를 반환한다.

    파일이 없거나 파싱에 실패하면 new_result의 Predicate만으로 시작한다.

    Args:
        predicate_path: 누적 predicates.json 경로.
        new_result: query_llm()의 반환값.

    Returns:
        병합 완료된 Predicate dict 리스트.
    """
    existing: list[dict[str, Any]] = []
    if predicate_path.exists():
        try:
            raw = read_json(predicate_path)
            if isinstance(raw, dict):
                existing = raw.get("State_Definitions") or raw.get("Predicates", [])
            elif isinstance(raw, list):
                existing = raw
        except Exception as exc:
            log(f"[predicate_merger] failed to load existing predicates: {exc}", "yellow")

    new_defs = [pred.model_dump(exclude_none=True) for pred in new_result.response.State_Definitions]
    merged = merge_state_definitions(existing, new_defs)
    log(
        f"[predicate_merger] existing={len(existing)} new={len(new_defs)} merged={len(merged)}",
        "blue",
    )
    return merged


def save_predicates(
    predicate_path: Path,
    merged: list[dict[str, Any]],
    analysis: str,
) -> None:
    """병합된 Predicate 리스트를 predicates.json에 저장한다.

    기존 파일의 Analysis_History를 보존하면서 새 analysis 항목을 추가한다.

    Args:
        predicate_path: 저장 대상 파일 경로.
        merged: merge_state_definitions() 결과.
        analysis: LLMResult.response.Analysis 텍스트.
    """
    existing_doc: dict[str, Any] = {}
    if predicate_path.exists():
        try:
            existing_doc = read_json(predicate_path)
            if not isinstance(existing_doc, dict):
                existing_doc = {}
        except Exception:
            existing_doc = {}

    history: list[dict[str, Any]] = existing_doc.get("Analysis_History", [])
    if not isinstance(history, list):
        history = []

    if isinstance(analysis, str) and analysis.strip():
        history.append(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "analysis": analysis,
            }
        )

    out: dict[str, Any] = dict(existing_doc)
    out["State_Definitions"] = merged
    out["Analysis_History"] = history

    write_json(predicate_path, out)
    log(f"[predicate_merger] saved {len(merged)} predicates → {predicate_path}", "cyan")
