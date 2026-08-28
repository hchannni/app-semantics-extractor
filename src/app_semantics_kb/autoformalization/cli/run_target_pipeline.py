"""
run_target_pipeline.py — target-home end-to-end semantic pipeline entry

target-home 내부에서 extractors -> fusion -> semantic contract generation 흐름을
한 번에 실행한다.

Variant 3/4는 official static-semantics producer output을 입력으로 사용하며,
기본 계약은:

- `--static-semantics-run-dir`
  또는
- target-home static-semantics `runs/` 아래의 latest valid run auto-discovery

이다.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..extractors.a11y_tree_parser import A11yTreeParser
from ..extractors.code_context_loader import (
    load_existing_predicates,
    load_raw_source_code,
)
from ..extractors.screenshot_maker import ScreenshotMaker
from ..fusion import build_provenance_seed_map
from ..fusion.context_merger import merge_context
from ..semantic import build_prompt
from ..semantic.api_diagnostics import result_usage_dict
from ..semantic.bundle_builder import build_and_save_semantic_contract_bundles
from ..semantic.contract_runner import (
    SemanticContractRunResult,
    run_semantic_contract_generation,
)
from ..semantic.llm_client import FPFNResult, LLMResult, query_llm_followup
from ..semantic.v2_chunked import (
    DEFAULT_V2_CHUNK_MAX_CHARS,
    DEFAULT_V2_CHUNK_TARGET_CHARS,
    run_v2_chunked_generation,
)
from ..semantic.v2_compacted import (
    run_v2_compacted_generation,
)
from ..utils import iso_now, log, read_json, sha256_file, write_json
from .shared.default_paths import (
    DEFAULT_APP_SOURCE_ROOT,
    DEFAULT_RUNS_DIR,
    DEFAULT_STATIC_SEMANTICS_RUNS_DIR,
)
from .shared.static_code_context import build_static_code_context_payloads
from .shared.static_semantics_inputs import (
    StaticSemanticsPaths as ResolvedStaticSemanticsInputs,
    find_latest_static_semantics_run_dir,
    infer_run_dir_from_context_slicer_dir,
    is_valid_static_semantics_run_dir,
    read_static_semantic_ref_from_run_dir,
    static_semantics_paths_from_run,
    validate_and_infer_run_dir_from_variant4_paths,
)
from .shared.variant_registry import (
    V2_CHUNKED_BASE_VARIANT,
    V2_CHUNKED_PROMPT_STRATEGY,
    V2_CHUNKED_VARIANT_KEY,
    V2_COMPACTED_BASE_VARIANT,
    V2_COMPACTED_PROMPT_STRATEGY,
    V2_COMPACTED_VARIANT_KEY,
    VariantKey,
    base_variant,
    is_v2_chunked,
    is_v2_compacted,
    is_v2_compacted_app,
    is_v2_compacted_parallel,
    is_v2_responses_multiturn,
    parse_variant,
)


@dataclass
class TargetPipelineConfig:
    variant: VariantKey
    run_id: str
    output_dir: Path
    app_source_root: Path
    context_slicer_dir: Path | None
    method_cfg_index_path: Path | None
    model: str
    static_semantics_run_dir: Path | None = None
    screenshot_path: Path | None = None
    a11y_path: Path | None = None
    device_serial: str | None = None
    existing_predicates_path: Path | None = None
    # FP/FN 2nd turn: ground truth predicates JSON (None → skip 2nd turn)
    ground_truth_path: Path | None = None
    v2_chunk_target_chars: int = DEFAULT_V2_CHUNK_TARGET_CHARS
    v2_chunk_max_chars: int = DEFAULT_V2_CHUNK_MAX_CHARS


def _base_variant(variant: VariantKey) -> int:
    return base_variant(variant)


def _resolve_static_semantics_inputs(
    config: TargetPipelineConfig,
    runs_root: Path = DEFAULT_STATIC_SEMANTICS_RUNS_DIR,
) -> ResolvedStaticSemanticsInputs:
    variant = _base_variant(config.variant)
    needs_static_semantics = variant in (3, 4)
    run_dir = config.static_semantics_run_dir
    has_explicit_context = config.context_slicer_dir is not None
    has_explicit_index = config.method_cfg_index_path is not None

    if needs_static_semantics and run_dir is not None and not is_valid_static_semantics_run_dir(
        run_dir,
        require_bundle=True,
    ):
        raise ValueError(
            "static_semantics_run_dir must contain context-slicer-output/, method-cfg-index.json, and static_semantic_bundle.json"
        )

    if run_dir is not None and (has_explicit_context or has_explicit_index):
        raise ValueError(
            "Use either static_semantics_run_dir or explicit static-semantics file overrides, not both"
        )

    if variant == 4 and has_explicit_context != has_explicit_index:
        raise ValueError(
            "variant 4 requires both context_slicer_dir and method_cfg_index_path when overriding static-semantics inputs explicitly"
        )

    if needs_static_semantics and run_dir is None and has_explicit_context:
        if variant == 4 and config.method_cfg_index_path is not None:
            run_dir = validate_and_infer_run_dir_from_variant4_paths(
                config.context_slicer_dir,
                config.method_cfg_index_path,
                require_bundle=True,
            )
        else:
            run_dir = infer_run_dir_from_context_slicer_dir(
                config.context_slicer_dir,
                require_bundle=True,
            )

    if needs_static_semantics and run_dir is None and (
        config.context_slicer_dir is None
        or (variant == 4 and config.method_cfg_index_path is None)
    ):
        run_dir = find_latest_static_semantics_run_dir(
            runs_root,
            require_bundle=True,
        )

    return static_semantics_paths_from_run(
        run_dir=run_dir,
        context_slicer_dir=config.context_slicer_dir,
        method_cfg_index_path=config.method_cfg_index_path,
    )


def _capture_or_load_inputs(config: TargetPipelineConfig) -> dict[str, object]:
    input_dir = config.output_dir / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)

    if config.screenshot_path and config.a11y_path:
        screenshot_path = config.screenshot_path
        a11y_path = config.a11y_path
        a11y_xml = a11y_path.read_text(encoding="utf-8")
    else:
        screenshot_path = ScreenshotMaker(temp_dir=input_dir).capture_png(
            serial=config.device_serial,
            filename="input_screenshot.png",
        )
        a11y_path = input_dir / "input_a11y.xml"
        a11y_xml = A11yTreeParser().dump_to_file_from_adb(
            a11y_path,
            serial=config.device_serial,
        )

    meta = {
        "captured_at": iso_now(),
        "device_serial": config.device_serial,
        "input_screenshot_path": str(screenshot_path),
        "input_a11y_path": str(a11y_path),
        "input_screenshot_sha256": sha256_file(screenshot_path),
        "input_a11y_sha256": sha256_file(a11y_path),
    }
    write_json(input_dir / "input_meta.json", meta)

    return {
        "input_dir": input_dir,
        "screenshot_path": screenshot_path,
        "a11y_path": a11y_path,
        "a11y_xml": a11y_xml,
        "meta": meta,
    }


def _load_code_context(
    config: TargetPipelineConfig,
    a11y_path: Path,
    work_dir: Path,
) -> dict[str, object]:
    variant = _base_variant(config.variant)
    result: dict[str, object] = {
        "raw_source_code": None,
        "sliced_methods_payload": None,
        "static_analysis_payload": None,
    }

    if variant == 2 and not is_v2_chunked(config.variant):
        result["raw_source_code"] = load_raw_source_code(config.app_source_root)

    if variant in (3, 4):
        sliced_path = work_dir / "sliced_methods_context.json"
        static_payloads = build_static_code_context_payloads(
            a11y_path=a11y_path,
            context_slicer_dir=config.context_slicer_dir,
            method_cfg_index_path=config.method_cfg_index_path,
            include_sliced_methods=True,
            include_static_analysis=variant == 4,
            sliced_output_path=sliced_path,
        )
        result["sliced_methods_payload"] = static_payloads.sliced_methods_payload
        result["static_analysis_payload"] = static_payloads.static_analysis_payload

    return result


# ─────────────────────────────────────────────
# FP/FN 2nd turn (legacy 동등 동작 — 출력은 output_dir 직속 fpfn_*.json)
# ─────────────────────────────────────────────

def _load_ground_truth(gt_path: Path) -> list[dict[str, Any]]:
    """GT JSON에서 predicate 리스트를 로드한다. 'Predicates' 또는 'State_Definitions' 키를 지원."""
    raw = read_json(gt_path)
    predicates = raw.get("Predicates") or raw.get("State_Definitions") or []
    log(f"[target_pipeline] ground truth loaded: {len(predicates)} predicates from {gt_path}", "blue")
    return predicates


def _compute_fpfn(
    page_predicates: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
) -> dict[str, Any]:
    """LLM 출력과 GT를 variable 단위로 비교하여 FP/FN을 계산한다.

    모든 결과를 variable 단위로 flatten한다.
    - 완전히 누락된 predicate → 해당 predicate의 모든 variable을 fn_variables로
    - 완전히 추가된 predicate → 해당 predicate의 모든 variable을 fp_variables로
    - 이름 매칭된 predicate → 내부 variable 단위로 fn/fp 계산
    """
    gt_by_name = {p["name"].lower(): p for p in ground_truth}
    pred_by_name = {p["name"].lower(): p for p in page_predicates}

    fn_variables: list[dict[str, Any]] = []
    fp_variables: list[dict[str, Any]] = []

    # 완전히 누락된 predicates → 모든 variable이 FN
    for name in set(gt_by_name) - set(pred_by_name):
        p = gt_by_name[name]
        for v in p.get("variables", []):
            fn_variables.append({
                "predicate_name": p["name"],
                "variable_name": v["name"],
                "variable_def": v,
            })

    # 완전히 추가된 predicates → 모든 variable이 FP
    for name in set(pred_by_name) - set(gt_by_name):
        p = pred_by_name[name]
        for v in p.get("variables", []):
            fp_variables.append({
                "predicate_name": p["name"],
                "variable_name": v["name"],
                "variable_def": v,
            })

    # 이름 매칭된 predicates → variable 단위 diff
    for name in set(gt_by_name) & set(pred_by_name):
        gt_p = gt_by_name[name]
        pred_p = pred_by_name[name]
        gt_vars = {v["name"].lower(): v for v in gt_p.get("variables", [])}
        pred_vars = {v["name"].lower(): v for v in pred_p.get("variables", [])}
        for vname in set(gt_vars) - set(pred_vars):
            fn_variables.append({
                "predicate_name": gt_p["name"],
                "variable_name": gt_vars[vname]["name"],
                "variable_def": gt_vars[vname],
            })
        for vname in set(pred_vars) - set(gt_vars):
            fp_variables.append({
                "predicate_name": pred_p["name"],
                "variable_name": pred_vars[vname]["name"],
                "variable_def": pred_vars[vname],
            })

    return {
        "fn_variables": fn_variables,
        "fp_variables": fp_variables,
    }


def _build_fpfn_prompt(fpfn: dict[str, Any]) -> str | None:
    """FP/FN 계산 결과를 기반으로 LLM에게 전달할 구조화된 질문 텍스트를 생성한다.

    모든 항목은 variable 단위로 나열한다.
    FP/FN이 모두 없으면 None을 반환하여 2nd turn을 skip한다.
    """
    fn_variables: list[dict] = fpfn["fn_variables"]
    fp_variables: list[dict] = fpfn["fp_variables"]

    if not fn_variables and not fp_variables:
        return None

    lines: list[str] = [
        "Below is a variable-level comparison between what you just generated and the ground truth.",
        "For each item, explain the reason using the structured schema.",
        "",
    ]

    if fn_variables:
        lines.append("## False Negative variables — in ground truth but NOT generated:")
        for item in fn_variables:
            v = item["variable_def"]
            vtype = v.get("type", "")
            desc = v.get("description", "")
            lines.append(f"  - {item['predicate_name']}.{item['variable_name']} ({vtype}): {desc}")
        lines.append("")

    if fp_variables:
        lines.append("## False Positive variables — generated but NOT in ground truth:")
        for item in fp_variables:
            v = item["variable_def"]
            vtype = v.get("type", "")
            desc = v.get("description", "")
            lines.append(f"  - {item['predicate_name']}.{item['variable_name']} ({vtype}): {desc}")
        lines.append("")

    lines += [
        "For each False Negative variable: was there evidence in the accessibility tree or provided code context?",
        "What additional information would have caused you to generate it?",
        "For each False Positive variable: what specifically triggered its generation?",
        "Why is it likely not a necessary state variable for testing?",
    ]

    return "\n".join(lines)


def _save_fpfn_outputs(output_dir: Path, result: FPFNResult) -> None:
    """output_dir에 fpfn_analysis.json과 fpfn_usage.json을 저장한다 (legacy 동등 파일명)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    analysis_dict = {
        "fn_variables": [item.model_dump() for item in result.response.fn_variables],
        "fp_variables": [item.model_dump() for item in result.response.fp_variables],
        "reflection": result.response.reflection,
    }
    write_json(output_dir / "fpfn_analysis.json", analysis_dict)
    write_json(
        output_dir / "fpfn_usage.json",
        result_usage_dict(result, include_cache_fields=False),
    )


def _run_fpfn_turn(
    llm_result: LLMResult,
    page_predicates: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    output_dir: Path,
    model: str,
    variant: int,
) -> None:
    """FP/FN을 계산하고 조건 충족 시 2nd turn을 실행하여 결과를 저장한다."""
    fpfn = _compute_fpfn(page_predicates, ground_truth)
    fn_count = len(fpfn["fn_variables"])
    fp_count = len(fpfn["fp_variables"])
    log(
        f"[target_pipeline] FP/FN computed: fn_variables={fn_count}  fp_variables={fp_count}",
        "blue",
    )

    fpfn_prompt = _build_fpfn_prompt(fpfn)
    if fpfn_prompt is None:
        log("[target_pipeline] FP/FN: no discrepancies — skipping 2nd turn.", "green")
        return

    fpfn_result = query_llm_followup(
        prior_messages=llm_result.messages_sent,
        prior_response_json=llm_result.raw_json,
        fpfn_user_prompt=fpfn_prompt,
        variant=variant,
        model=model,
    )
    _save_fpfn_outputs(output_dir, fpfn_result)
    log(
        f"[target_pipeline] FP/FN 2nd turn done  "
        f"fn_explained={len(fpfn_result.response.fn_variables)}  "
        f"fp_explained={len(fpfn_result.response.fp_variables)}  "
        f"latency={fpfn_result.latency_sec:.1f}s",
        "green",
    )


def _v2_chunked_provenance_seed_map(
    dedupe_meta: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    by_predicate: dict[str, dict[str, set[str]]] = {}
    for item in dedupe_meta.get("deduped_variables", []):
        if not isinstance(item, dict):
            continue
        predicate = str(item.get("predicate") or "").strip()
        if not predicate:
            continue
        entry = by_predicate.setdefault(
            predicate,
            {"chunks": set(), "paths": set()},
        )
        entry["chunks"].update(str(v) for v in item.get("source_chunks", []))
        entry["paths"].update(str(v) for v in item.get("source_paths", []))

    seeds: dict[str, dict[str, Any]] = {}
    for predicate, values in by_predicate.items():
        chunks = _limited_join(values["chunks"])
        paths = _limited_join(values["paths"])
        seeds[predicate] = {
            "notes": (
                "derived from V2-chunked raw source candidates; "
                f"chunks=[{chunks}]; source_paths=[{paths}]"
            )
        }
    return seeds


def _limited_join(values: set[str], *, limit: int = 12) -> str:
    ordered = sorted(values)
    if len(ordered) <= limit:
        return ", ".join(ordered)
    visible = ", ".join(ordered[:limit])
    return f"{visible}, ... (+{len(ordered) - limit} more)"


def _write_target_usage(
    output_dir: Path,
    result: SemanticContractRunResult,
    extra: dict[str, Any] | None = None,
) -> None:
    usage = result_usage_dict(result.llm_result, extra=extra)
    write_json(output_dir / "usage.json", usage)
    if result.llm_result.raw_json:
        (output_dir / "response_raw.txt").write_text(
            result.llm_result.raw_json,
            encoding="utf-8",
        )


def _run_v2_chunked_target_pipeline(
    *,
    config: TargetPipelineConfig,
    captured: dict[str, object],
    existing_predicates: list[dict[str, Any]],
    query_fn: Callable[..., object] | None,
) -> SemanticContractRunResult:
    chunked = run_v2_chunked_generation(
        output_dir=config.output_dir,
        app_source_root=config.app_source_root,
        app_name=config.app_source_root.name,
        a11y_xml=captured["a11y_xml"],  # type: ignore[arg-type]
        screenshot_path=captured["screenshot_path"],  # type: ignore[arg-type]
        existing_predicates=existing_predicates,
        model=config.model,
        timeout=120.0,
        prompt_cache_key=config.run_id,
        target_chunk_chars=config.v2_chunk_target_chars,
        max_chunk_chars=config.v2_chunk_max_chars,
        query_fn=query_fn,  # type: ignore[arg-type]
    )
    predicate_bundle, evidence_bundle, paths = build_and_save_semantic_contract_bundles(
        config.output_dir,
        chunked.final_result,
        run_id=config.run_id,
        fusion_ref=config.run_id,
        provenance_by_predicate_name=_v2_chunked_provenance_seed_map(
            chunked.dedupe_meta
        ),
    )
    write_json(
        config.output_dir / "run_meta.json",
        {
            "run_id": config.run_id,
            "variant": V2_CHUNKED_VARIANT_KEY,
            "base_variant": V2_CHUNKED_BASE_VARIANT,
            "model": config.model,
            "fusion_ref": config.run_id,
            "prompt_strategy": V2_CHUNKED_PROMPT_STRATEGY,
            "chunk_count": len(chunked.chunks_manifest["chunks"]),
            "candidate_count": chunked.dedupe_meta["candidate_count"],
            "deduped_variable_count": chunked.dedupe_meta["deduped_variable_count"],
            "target_chunk_chars": config.v2_chunk_target_chars,
            "max_chunk_chars": config.v2_chunk_max_chars,
            "captured_at": captured["meta"]["captured_at"],  # type: ignore[index]
        },
    )
    result = SemanticContractRunResult(
        llm_result=chunked.final_result,
        predicate_bundle=predicate_bundle,
        predicate_evidence_bundle=evidence_bundle,
        paths=paths,
    )
    _write_target_usage(
        config.output_dir,
        result,
        extra={
            "map_calls": len(chunked.chunks_manifest["chunks"]),
            "candidate_count": chunked.dedupe_meta["candidate_count"],
            "deduped_variable_count": chunked.dedupe_meta["deduped_variable_count"],
            "target_chunk_chars": config.v2_chunk_target_chars,
            "max_chunk_chars": config.v2_chunk_max_chars,
        },
    )
    return result


def _v2_compacted_provenance_seed_map(
    result: LLMResult,
    *,
    chunk_count: int,
    source_file_count: int,
) -> dict[str, dict[str, Any]]:
    return {
        pred.name: {
            "notes": (
                "derived from V2-compacted raw source baseline; "
                "source evidence is carried by opaque Responses compaction items; "
                f"chunks={chunk_count}; source_files={source_file_count}"
            )
        }
        for pred in result.response.State_Definitions
    }


def _run_v2_compacted_target_pipeline(
    *,
    config: TargetPipelineConfig,
    captured: dict[str, object],
    existing_predicates: list[dict[str, Any]],
) -> SemanticContractRunResult:
    compacted = run_v2_compacted_generation(
        output_dir=config.output_dir,
        app_source_root=config.app_source_root,
        app_name=config.app_source_root.name,
        a11y_xml=captured["a11y_xml"],  # type: ignore[arg-type]
        screenshot_path=captured["screenshot_path"],  # type: ignore[arg-type]
        existing_predicates=existing_predicates,
        model=config.model,
        timeout=120.0,
        prompt_cache_key=config.run_id,
        target_chunk_chars=config.v2_chunk_target_chars,
        max_chunk_chars=config.v2_chunk_max_chars,
    )
    chunk_count = int(compacted.chunks_manifest["chunk_count"])
    source_file_count = int(compacted.chunks_manifest["source_file_count"])
    predicate_bundle, evidence_bundle, paths = build_and_save_semantic_contract_bundles(
        config.output_dir,
        compacted.final_result,
        run_id=config.run_id,
        fusion_ref=config.run_id,
        provenance_by_predicate_name=_v2_compacted_provenance_seed_map(
            compacted.final_result,
            chunk_count=chunk_count,
            source_file_count=source_file_count,
        ),
    )
    write_json(
        config.output_dir / "run_meta.json",
        {
            "run_id": config.run_id,
            "variant": V2_COMPACTED_VARIANT_KEY,
            "base_variant": V2_COMPACTED_BASE_VARIANT,
            "model": config.model,
            "fusion_ref": config.run_id,
            "prompt_strategy": V2_COMPACTED_PROMPT_STRATEGY,
            "chunk_count": chunk_count,
            "source_file_count": source_file_count,
            "compaction_call_count": compacted.compaction_meta["compaction_call_count"],
            "target_chunk_chars": config.v2_chunk_target_chars,
            "max_chunk_chars": config.v2_chunk_max_chars,
            "captured_at": captured["meta"]["captured_at"],  # type: ignore[index]
        },
    )
    result = SemanticContractRunResult(
        llm_result=compacted.final_result,
        predicate_bundle=predicate_bundle,
        predicate_evidence_bundle=evidence_bundle,
        paths=paths,
    )
    _write_target_usage(
        config.output_dir,
        result,
        extra={
            "compaction_calls": compacted.compaction_meta["compaction_call_count"],
            "chunk_count": chunk_count,
            "source_file_count": source_file_count,
            "target_chunk_chars": config.v2_chunk_target_chars,
            "max_chunk_chars": config.v2_chunk_max_chars,
        },
    )
    return result


def run_target_pipeline(
    config: TargetPipelineConfig,
    *,
    query_fn: Callable[..., object] | None = None,
) -> SemanticContractRunResult:
    """target-home semantic pipeline을 한 번 실행한다."""
    if (
        is_v2_compacted_app(config.variant)
        or is_v2_compacted_parallel(config.variant)
        or is_v2_responses_multiturn(config.variant)
    ):
        raise ValueError(
            "2compacted_app, 2compacted_parallel, and 2responses_multiturn "
            "are reproduction-only ablation lanes; use run_reproduction_pipeline.py."
        )
    log(
        f"[target_pipeline] run_id={config.run_id} variant={config.variant} model={config.model}",
        "magenta",
    )
    captured = _capture_or_load_inputs(config)
    resolved_static_semantics = _resolve_static_semantics_inputs(config)

    base_variant_for_static = _base_variant(config.variant)
    if base_variant_for_static in (3, 4):
        log(
            "[target_pipeline] static semantics inputs — "
            f"run_dir={resolved_static_semantics.run_dir or 'None'} "
            f"context_slicer_dir={resolved_static_semantics.context_slicer_dir or 'None'} "
            f"method_cfg_index_path={resolved_static_semantics.method_cfg_index_path or 'None'}",
            "blue",
        )

    existing_predicates = (
        load_existing_predicates(config.existing_predicates_path)
        if config.existing_predicates_path
        else []
    )

    if is_v2_compacted(config.variant):
        result = _run_v2_compacted_target_pipeline(
            config=config,
            captured=captured,
            existing_predicates=existing_predicates,
        )
        if config.ground_truth_path is not None:
            ground_truth = _load_ground_truth(config.ground_truth_path)
            page_predicates = [
                pred.model_dump(exclude_none=True)
                for pred in result.llm_result.response.State_Definitions
            ]
            _run_fpfn_turn(
                llm_result=result.llm_result,
                page_predicates=page_predicates,
                ground_truth=ground_truth,
                output_dir=config.output_dir,
                model=config.model,
                variant=V2_COMPACTED_BASE_VARIANT,
            )
        log(
            f"[target_pipeline] done → {result.paths['predicate_bundle']} / {result.paths['predicate_evidence_bundle']}",
            "green",
        )
        return result

    if is_v2_chunked(config.variant):
        result = _run_v2_chunked_target_pipeline(
            config=config,
            captured=captured,
            existing_predicates=existing_predicates,
            query_fn=query_fn,
        )
        if config.ground_truth_path is not None:
            ground_truth = _load_ground_truth(config.ground_truth_path)
            page_predicates = [
                pred.model_dump(exclude_none=True)
                for pred in result.llm_result.response.State_Definitions
            ]
            _run_fpfn_turn(
                llm_result=result.llm_result,
                page_predicates=page_predicates,
                ground_truth=ground_truth,
                output_dir=config.output_dir,
                model=config.model,
                variant=V2_CHUNKED_BASE_VARIANT,
            )
        log(
            f"[target_pipeline] done → {result.paths['predicate_bundle']} / {result.paths['predicate_evidence_bundle']}",
            "green",
        )
        return result

    code_ctx = _load_code_context(
        TargetPipelineConfig(
            variant=config.variant,
            run_id=config.run_id,
            output_dir=config.output_dir,
            app_source_root=config.app_source_root,
            context_slicer_dir=resolved_static_semantics.context_slicer_dir,
            method_cfg_index_path=resolved_static_semantics.method_cfg_index_path,
            model=config.model,
            static_semantics_run_dir=resolved_static_semantics.run_dir,
            screenshot_path=config.screenshot_path,
            a11y_path=config.a11y_path,
            device_serial=config.device_serial,
            existing_predicates_path=config.existing_predicates_path,
        ),
        captured["a11y_path"],  # type: ignore[arg-type]
        config.output_dir,
    )

    static_semantic_ref = None
    if base_variant_for_static in (3, 4):
        static_semantic_ref = read_static_semantic_ref_from_run_dir(
            resolved_static_semantics.run_dir
        )

    base_variant = _base_variant(config.variant)
    merged_ctx = merge_context(
        variant=base_variant,
        a11y_xml_text=captured["a11y_xml"],  # type: ignore[arg-type]
        screenshot_path=captured["screenshot_path"],  # type: ignore[arg-type]
        existing_predicates=existing_predicates,
        app_name=config.app_source_root.name,
        raw_source_code=code_ctx.get("raw_source_code"),  # type: ignore[arg-type]
        sliced_methods_payload=code_ctx.get("sliced_methods_payload"),  # type: ignore[arg-type]
        static_analysis_payload=code_ctx.get("static_analysis_payload"),  # type: ignore[arg-type]
    )

    system_prompt, user_prompt = build_prompt(base_variant, merged_ctx)
    prompts_dir = config.output_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "prompt_system.txt").write_text(system_prompt, encoding="utf-8")
    (prompts_dir / "prompt_user.txt").write_text(user_prompt, encoding="utf-8")

    provenance_seed_map = build_provenance_seed_map(
        sliced_methods_payload=code_ctx.get("sliced_methods_payload"),  # type: ignore[arg-type]
        static_analysis_payload=code_ctx.get("static_analysis_payload"),  # type: ignore[arg-type]
    )

    run_meta = {
        "run_id": config.run_id,
        "variant": config.variant,
        "model": config.model,
        "fusion_ref": config.run_id,
        "static_semantic_ref": static_semantic_ref,
        "static_semantics_run_dir": (
            str(resolved_static_semantics.run_dir)
            if resolved_static_semantics.run_dir
            else None
        ),
        "context_slicer_dir": (
            str(resolved_static_semantics.context_slicer_dir)
            if resolved_static_semantics.context_slicer_dir
            else None
        ),
        "method_cfg_index_path": (
            str(resolved_static_semantics.method_cfg_index_path)
            if resolved_static_semantics.method_cfg_index_path
            else None
        ),
        "captured_at": captured["meta"]["captured_at"],  # type: ignore[index]
    }
    write_json(config.output_dir / "run_meta.json", run_meta)

    runner_kwargs = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "screenshot_path": captured["screenshot_path"],
        "variant": base_variant,
        "output_dir": config.output_dir,
        "run_id": config.run_id,
        "fusion_ref": config.run_id,
        "static_semantic_ref": static_semantic_ref,
        "provenance_by_predicate_name": provenance_seed_map,
        "model": config.model,
    }
    if query_fn is not None:
        runner_kwargs["query_fn"] = query_fn

    result = run_semantic_contract_generation(**runner_kwargs)  # type: ignore[arg-type]

    _write_target_usage(config.output_dir, result)

    # FP/FN 2nd turn (--ground-truth 가 주어진 경우에 한해)
    if config.ground_truth_path is not None:
        ground_truth = _load_ground_truth(config.ground_truth_path)
        page_predicates = [
            pred.model_dump(exclude_none=True)
            for pred in result.llm_result.response.State_Definitions
        ]
        _run_fpfn_turn(
            llm_result=result.llm_result,
            page_predicates=page_predicates,
            ground_truth=ground_truth,
            output_dir=config.output_dir,
            model=config.model,
            variant=base_variant,
        )

    log(
        f"[target_pipeline] done → {result.paths['predicate_bundle']} / {result.paths['predicate_evidence_bundle']}",
        "green",
    )
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the target-home semantic pipeline and emit contract bundles",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--variant",
        type=_parse_variant_arg,
        required=True,
        help=(
            "Variant to run: 1, 2, 3, 4, v2-chunked aliases "
            "2c/2chunked/v2c, or v2-compacted aliases "
            "2compacted/v2compacted. Reproduction-only ablations include "
            "2compacted_app, 2compacted_parallel, and 2responses_multiturn."
        ),
    )
    parser.add_argument("--run-id", default=None, help="Artifact run_id (default: timestamp-based)")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: ai_friendly_compiler_system/runs/<run-id>)")
    parser.add_argument("--model", default="gpt-5.2", help="OpenAI model ID")
    parser.add_argument("--app-source-root", default=str(DEFAULT_APP_SOURCE_ROOT))
    parser.add_argument(
        "--static-semantics-run-dir",
        default=None,
        help=(
            "Static-semantics run root. If omitted, variant 3/4 resolve the latest valid "
            "target-home run under ai_friendly_compiler_system/engines/static_semantics/scala/runs/"
        ),
    )
    parser.add_argument(
        "--context-slicer-dir",
        default=None,
        help="Optional override for context-slicer-output directory (variant 3/4)",
    )
    parser.add_argument(
        "--method-cfg-index-path",
        default=None,
        help="Optional override for method-cfg-index.json (variant 4)",
    )
    parser.add_argument("--device-serial", default=None)
    parser.add_argument("--screenshot-path", default=None, help="Optional existing screenshot path")
    parser.add_argument("--a11y-path", default=None, help="Optional existing a11y XML path")
    parser.add_argument("--existing-predicates-path", default=None, help="Optional existing predicates JSON path")
    parser.add_argument(
        "--ground-truth",
        default=None,
        help=(
            "Optional ground-truth predicates JSON path. When provided, the pipeline runs "
            "an additional FP/FN 2nd turn against the LLM (legacy compatibility) and writes "
            "fpfn_analysis.json / fpfn_usage.json into output_dir."
        ),
    )
    parser.add_argument(
        "--v2-chunk-target-chars",
        type=int,
        default=DEFAULT_V2_CHUNK_TARGET_CHARS,
        help="Soft target source characters per V2-chunked file pack.",
    )
    parser.add_argument(
        "--v2-chunk-max-chars",
        type=int,
        default=DEFAULT_V2_CHUNK_MAX_CHARS,
        help="Hard max source characters per V2-chunked file pack before flushing.",
    )
    return parser


def _parse_variant_arg(value: str) -> VariantKey:
    return parse_variant(value)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    timestamp = time.strftime("%Y-%m-%dT%H-%M-%SZ")
    run_id = args.run_id or f"{timestamp}__target__v{args.variant}"
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_DIR / run_id

    config = TargetPipelineConfig(
        variant=args.variant,
        run_id=run_id,
        output_dir=output_dir,
        app_source_root=Path(args.app_source_root),
        context_slicer_dir=Path(args.context_slicer_dir) if args.context_slicer_dir else None,
        method_cfg_index_path=Path(args.method_cfg_index_path) if args.method_cfg_index_path else None,
        model=args.model,
        static_semantics_run_dir=Path(args.static_semantics_run_dir) if args.static_semantics_run_dir else None,
        screenshot_path=Path(args.screenshot_path) if args.screenshot_path else None,
        a11y_path=Path(args.a11y_path) if args.a11y_path else None,
        device_serial=args.device_serial,
        existing_predicates_path=Path(args.existing_predicates_path) if args.existing_predicates_path else None,
        ground_truth_path=Path(args.ground_truth) if args.ground_truth else None,
        v2_chunk_target_chars=args.v2_chunk_target_chars,
        v2_chunk_max_chars=args.v2_chunk_max_chars,
    )
    run_target_pipeline(config)


if __name__ == "__main__":
    main()
