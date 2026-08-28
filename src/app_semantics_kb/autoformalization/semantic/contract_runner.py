"""
contract_runner.py — target-home semantic contract generation runner

legacy lane를 건드리지 않고, target-home 내부에서 semantic 단계가
PredicateBundle + PredicateEvidenceBundle 을 직접 생성·저장할 수 있게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..utils import log, read_json
from .bundle_builder import (
    MechanicalProvenanceSeed,
    build_and_save_semantic_contract_bundles,
)
from .llm_client import LLMResult, query_llm


@dataclass
class SemanticContractRunResult:
    llm_result: LLMResult
    predicate_bundle: dict
    predicate_evidence_bundle: dict
    paths: dict[str, Path]


def load_provenance_seed_map(
    seed_json_path: Path | None,
) -> dict[str, MechanicalProvenanceSeed]:
    """predicate_name -> MechanicalProvenanceSeed 매핑 JSON을 로드한다."""
    if seed_json_path is None:
        return {}

    raw = read_json(seed_json_path)
    out: dict[str, MechanicalProvenanceSeed] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        out[key] = {
            seed_key: seed_val
            for seed_key, seed_val in value.items()
            if seed_key in {
                "element_uids",
                "resource_ids",
                "anchor_uids",
                "method_uids",
                "evidence_uids",
                "handler_uids",
                "type_uids",
                "notes",
            }
        }
    return out


def run_semantic_contract_generation(
    *,
    system_prompt: str,
    user_prompt: str,
    screenshot_path: Path | None,
    variant: int,
    output_dir: Path,
    run_id: str,
    fusion_ref: str | None = None,
    static_semantic_ref: str | None = None,
    screen_context: str | None = None,
    provenance_by_predicate_name: dict[str, MechanicalProvenanceSeed] | None = None,
    model: str = "gpt-5.2",
    api_key: str | None = None,
    timeout: float = 120.0,
    query_fn: Callable[..., LLMResult] = query_llm,
) -> SemanticContractRunResult:
    """semantic 입력을 실행하고 target-home contract bundles를 생성·저장한다."""
    llm_result = query_fn(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        screenshot_path=screenshot_path,
        variant=variant,
        model=model,
        api_key=api_key,
        timeout=timeout,
    )

    predicate_bundle, evidence_bundle, paths = build_and_save_semantic_contract_bundles(
        output_dir,
        llm_result,
        run_id=run_id,
        fusion_ref=fusion_ref,
        static_semantic_ref=static_semantic_ref,
        screen_context=screen_context,
        provenance_by_predicate_name=provenance_by_predicate_name,
    )

    log(
        f"[contract_runner] saved semantic contracts → "
        f"{paths['predicate_bundle']} / {paths['predicate_evidence_bundle']}",
        "green",
    )

    return SemanticContractRunResult(
        llm_result=llm_result,
        predicate_bundle=predicate_bundle,
        predicate_evidence_bundle=evidence_bundle,
        paths=paths,
    )
