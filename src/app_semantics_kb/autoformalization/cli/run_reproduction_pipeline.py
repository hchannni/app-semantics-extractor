"""run_reproduction_pipeline.py — legacy-style V1~V4 comparison harness.

This CLI is for paper/evaluation reproduction. It intentionally keeps the
legacy experiment layout while reusing the system package's extractors,
fusion, prompt builder, and LLM client.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ..extractors.a11y_tree_parser import A11yTreeParser
from ..extractors.code_context_loader import load_raw_source_code
from ..extractors.screenshot_maker import ScreenshotMaker
from ..fusion.context_merger import MergedContext, merge_context
from ..fusion.context_window import (
    DOMAIN_ATTRIBUTION_METHOD_LOCAL,
    DOMAIN_ATTRIBUTION_POLICIES,
    build_context_evidence_index,
    pack_evidence_graph_chunks,
    pack_rendered_prompt_chunks,
    render_chunk_evidence,
    slim_rendered_prompt_chunks_manifest,
    slim_chunks_manifest,
)
from ..semantic.api_diagnostics import (
    FAILURE_ARTIFACT_TAIL_CHARS,
    attached_request_meta,
    error_message as _error_message,
    exception_artifact_candidates as _exception_artifact_candidates,
    result_usage_dict as _api_result_usage_dict,
)
from ..semantic.critic_runner import (
    DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
    CriticResult,
    build_critic_user_prompt,
    run_critic_turn,
)
from ..semantic.llm_client import (
    LLMResult,
    PredicateResponse,
    build_user_content,
    query_llm,
)
from ..semantic.predicate_merger import merge_state_definitions
from ..semantic.prompt_builder import build_prompt
from ..semantic.context_window_chunked import (
    prepare_context_window_chunked_generation,
    run_context_window_chunked_generation,
)
from ..semantic.context_window_critic import (
    prepare_v2chunked_context_window_critic,
    run_v2chunked_context_window_critic,
)
from ..semantic.static_critic_evidence import render_static_critic_evidence
from ..semantic.v2_chunked import (
    DEFAULT_V2_CHUNK_MAX_CHARS,
    DEFAULT_V2_CHUNK_TARGET_CHARS,
    prepare_v2_chunked_generation,
    run_v2_chunked_generation,
)
from ..semantic.v2_compacted import (
    has_v2_app_source_compaction,
    load_v2_app_source_compaction,
    prepare_v2_app_source_compaction,
    prepare_v2_compacted_app_generation,
    prepare_v2_compacted_generation,
    prepare_v2_parallel_source_compaction,
    run_v2_app_source_compaction,
    run_v2_compacted_app_generation,
    run_v2_compacted_generation,
    run_v2_compacted_parallel_generation,
    run_v2_parallel_source_compaction,
)
from ..semantic.v2_responses_multiturn import (
    DEFAULT_RESPONSES_COMPACT_THRESHOLD,
    SESSION_MODE as V2_RESPONSES_MULTITURN_SESSION_MODE,
    SOURCE_CONTEXT_MODE as V2_RESPONSES_MULTITURN_SOURCE_CONTEXT_MODE,
    V2ResponsesMultiturnPageInput,
    prepare_v2_responses_multiturn_generation,
    run_v2_responses_multiturn_generation,
)
from ..utils import iso_now, log, read_json, sha256_file, write_json
from .shared.default_paths import (
    DEFAULT_APP_SOURCE_ROOT,
    DEFAULT_RUNS_DIR,
    DEFAULT_STATIC_SEMANTICS_RUNS_DIR,
    PYTHON_DIR,
)
from .shared.static_code_context import build_static_code_context_payloads
from .shared.static_semantics_inputs import (
    StaticSemanticsPaths as StaticSemanticsInputs,
    find_latest_static_semantics_run_dir,
    static_semantics_paths_from_run,
)
from .shared.variant_registry import (
    V2_CHUNKED_BASE_VARIANT,
    V2_CHUNKED_PROMPT_STRATEGY,
    V2_CHUNKED_VARIANT_DIR,
    V2_CHUNKED_VARIANT_KEY,
    V2_COMPACTED_BASE_VARIANT,
    V2_COMPACTED_APP_BASE_VARIANT,
    V2_COMPACTED_APP_PROMPT_STRATEGY,
    V2_COMPACTED_APP_VARIANT_DIR,
    V2_COMPACTED_APP_VARIANT_KEY,
    V2_COMPACTED_PARALLEL_BASE_VARIANT,
    V2_COMPACTED_PARALLEL_PROMPT_STRATEGY,
    V2_COMPACTED_PARALLEL_VARIANT_DIR,
    V2_COMPACTED_PARALLEL_VARIANT_KEY,
    V2_COMPACTED_PROMPT_STRATEGY,
    V2_COMPACTED_VARIANT_DIR,
    V2_COMPACTED_VARIANT_KEY,
    V2_RESPONSES_MULTITURN_BASE_VARIANT,
    V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
    V2_RESPONSES_MULTITURN_VARIANT_DIR,
    V2_RESPONSES_MULTITURN_VARIANT_KEY,
    V2_SOURCE_BASELINE_HELP,
    VariantKey,
    base_variant,
    default_prompt_strategy,
    default_variant_dir_name,
    is_context_window_chunked,
    is_v2_chunked,
    is_v2_compacted,
    is_v2_compacted_app,
    is_v2_compacted_parallel,
    is_v2_responses_multiturn,
    parse_variant_list,
)


V2_CRITIC_VARIANT_KEY = "2_critic"
V2_CRITIC_VARIANT_DIR = "variant_2_critic"
V2_CRITIC_PROMPT_STRATEGY = "v2_integrated_step2_static_critic"
V2_CRITIC_NO_ANALYSIS_VARIANT_KEY = "2_critic_no_analysis"
V2_CRITIC_NO_ANALYSIS_VARIANT_DIR = "variant_2_critic_no_analysis"
V2_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY = "v2_integrated_step2_critic_no_analysis"
V2_CHUNKED_CRITIC_VARIANT_KEY = "2chunked_critic"
V2_CHUNKED_CRITIC_VARIANT_DIR = "variant_2chunked_critic"
V2_CHUNKED_CRITIC_PROMPT_STRATEGY = "v2_chunked_integrated_step2_static_critic"
V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY = "2chunked_critic_chunked"
V2_CHUNKED_CRITIC_CHUNKED_VARIANT_DIR = "variant_2chunked_critic_chunked"
V2_CHUNKED_CRITIC_CHUNKED_PROMPT_STRATEGY = (
    "v2_chunked_context_window_step2_static_critic"
)
V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY = "2chunked_critic_no_analysis"
V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_DIR = (
    "variant_2chunked_critic_no_analysis"
)
V2_CHUNKED_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY = (
    "v2_chunked_integrated_step2_critic_no_analysis"
)


class ReproductionInputError(ValueError):
    pass


@dataclass
class ReproductionConfig:
    variants: list[VariantKey]
    run_id: str
    output_dir: Path
    app_source_root: Path
    model: str
    context_slicer_dir: Path | None = None
    method_cfg_index_path: Path | None = None
    static_semantics_run_dir: Path | None = None
    screenshot_path: Path | None = None
    a11y_path: Path | None = None
    replay_from: Path | None = None
    page_range: str | None = None
    device_serial: str | None = None
    prompt_cache_key: str | None = None
    timeout: float = 120.0
    v2_chunk_target_chars: int = DEFAULT_V2_CHUNK_TARGET_CHARS
    v2_chunk_max_chars: int = DEFAULT_V2_CHUNK_MAX_CHARS
    emit_resource_evidence_chunks: bool = False
    resource_chunk_target_chars: int = 400000
    resource_chunk_max_chars: int = 500000
    context_window_domain_attribution: str = DOMAIN_ATTRIBUTION_METHOD_LOCAL
    context_window_chunk_max_attempts: int = 3
    context_window_chunk_retry_base_delay: float = 30.0
    context_window_chunk_retry_max_delay: float = 120.0
    emit_naive_rendered_prompt_chunks: bool = False
    responses_compact_threshold: int = DEFAULT_RESPONSES_COMPACT_THRESHOLD
    prepare_only: bool = False
    enable_v2_critic: bool = False
    enable_v2_critic_no_analysis: bool = False
    enable_v2chunked_critic_chunked: bool = False
    reuse_v2_from: Path | None = None
    reuse_v2chunked_from: Path | None = None


@dataclass(frozen=True)
class CapturedPage:
    page: int
    input_dir: Path
    screenshot_path: Path
    a11y_path: Path
    a11y_xml: str
    meta: dict[str, Any]


@dataclass(frozen=True)
class VariantRunOutput:
    paths: dict[str, Any]
    result: LLMResult | None = None
    system_prompt: str | None = None
    user_prompt: str | None = None
    source_page_dir: Path | None = None
    step1_source: str = "generated"
    step1_api_called: bool = False


def _variant_dir_name(variant: VariantKey) -> str:
    if str(variant) == V2_CRITIC_VARIANT_KEY:
        return V2_CRITIC_VARIANT_DIR
    if str(variant) == V2_CRITIC_NO_ANALYSIS_VARIANT_KEY:
        return V2_CRITIC_NO_ANALYSIS_VARIANT_DIR
    if str(variant) == V2_CHUNKED_CRITIC_VARIANT_KEY:
        return V2_CHUNKED_CRITIC_VARIANT_DIR
    if str(variant) == V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY:
        return V2_CHUNKED_CRITIC_CHUNKED_VARIANT_DIR
    if str(variant) == V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY:
        return V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_DIR
    return default_variant_dir_name(variant)


def _page_sort_key(path: Path) -> tuple[int, str]:
    digits = "".join(ch for ch in path.name if ch.isdigit())
    return (int(digits), path.name) if digits else (10**9, path.name)


def _page_number(path: Path, fallback: int) -> int:
    digits = "".join(ch for ch in path.name if ch.isdigit())
    return int(digits) if digits else fallback


def _copy_file_if_needed(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dst.resolve():
        return
    shutil.copy2(src, dst)


def _validate_config(config: ReproductionConfig) -> None:
    has_screenshot = config.screenshot_path is not None
    has_a11y = config.a11y_path is not None
    if has_screenshot != has_a11y:
        raise ReproductionInputError(
            "--screenshot-path and --a11y-path must be provided together"
        )
    if config.reuse_v2_from is not None:
        if not (config.enable_v2_critic or config.enable_v2_critic_no_analysis):
            raise ReproductionInputError(
                "--reuse-v2-from requires a V2 critic lane"
            )
        if config.replay_from is not None or has_screenshot or has_a11y:
            raise ReproductionInputError(
                "--reuse-v2-from cannot be combined with --replay-from "
                "or explicit screenshot/a11y paths"
            )
        if not config.reuse_v2_from.is_dir():
            raise ReproductionInputError(
                f"reused V2 run directory not found: {config.reuse_v2_from}"
            )
    if config.reuse_v2chunked_from is not None:
        if not (
            config.enable_v2_critic
            or config.enable_v2_critic_no_analysis
            or config.enable_v2chunked_critic_chunked
        ):
            raise ReproductionInputError(
                "--reuse-v2chunked-from requires a V2 chunked critic lane"
            )
        if not any(is_v2_chunked(variant) for variant in config.variants):
            raise ReproductionInputError(
                "--reuse-v2chunked-from requires v2-chunked in --variants"
            )
        if not config.reuse_v2chunked_from.is_dir():
            raise ReproductionInputError(
                "reused V2-chunked run directory not found: "
                f"{config.reuse_v2chunked_from}"
            )
    if config.replay_from is not None and (has_screenshot or has_a11y):
        raise ReproductionInputError(
            "--replay-from cannot be combined with explicit screenshot/a11y paths"
        )
    if config.page_range is not None and config.replay_from is None:
        if config.reuse_v2_from is None and config.reuse_v2chunked_from is None:
            raise ReproductionInputError(
                "--page-range requires --replay-from, --reuse-v2-from, "
                "or --reuse-v2chunked-from"
            )
    has_regular_v2_source = 2 in config.variants or config.reuse_v2_from is not None
    has_v2_chunked_source = any(
        is_v2_chunked(variant) for variant in config.variants
    )
    has_v2_source = has_regular_v2_source or has_v2_chunked_source
    if config.enable_v2_critic and not has_v2_source:
        raise ReproductionInputError(
            "--enable-v2-critic requires regular variant 2 or v2-chunked in --variants"
        )
    if (
        config.enable_v2_critic_no_analysis
        and not has_regular_v2_source
        and not has_v2_chunked_source
    ):
        raise ReproductionInputError(
            "--enable-v2-critic-no-analysis requires regular variant 2 "
            "or v2-chunked in --variants, or --reuse-v2-from"
        )
    if config.enable_v2chunked_critic_chunked and not has_v2_chunked_source:
        raise ReproductionInputError(
            "--enable-v2chunked-critic-chunked requires v2-chunked in --variants"
        )
    if config.resource_chunk_target_chars <= 0:
        raise ReproductionInputError("--resource-chunk-target-chars must be positive")
    if config.resource_chunk_max_chars <= 0:
        raise ReproductionInputError("--resource-chunk-max-chars must be positive")
    if config.context_window_domain_attribution not in DOMAIN_ATTRIBUTION_POLICIES:
        allowed = ", ".join(sorted(DOMAIN_ATTRIBUTION_POLICIES))
        raise ReproductionInputError(
            "--context-window-domain-attribution must be one of: " + allowed
        )


def _resolve_page_file(page_dir: Path, names: tuple[str, ...], label: str) -> Path:
    for name in names:
        path = page_dir / name
        if path.is_file():
            return path
    raise ReproductionInputError(
        f"{label} not found in {page_dir}; tried {', '.join(names)}"
    )


def _discover_replay_pages(replay_from: Path) -> list[Path]:
    if not replay_from.is_dir():
        raise FileNotFoundError(f"replay input directory not found: {replay_from}")
    pages = [
        path for path in replay_from.iterdir()
        if path.is_dir() and path.name.startswith("page_")
    ]
    if not pages:
        raise ReproductionInputError(f"no page_N directories found in {replay_from}")
    return sorted(pages, key=_page_sort_key)


def _parse_positive_page_number(value: str, *, label: str) -> int:
    try:
        page = int(value)
    except ValueError as exc:
        raise ReproductionInputError(f"{label} must be a positive integer: {value}") from exc
    if page <= 0:
        raise ReproductionInputError(f"{label} must be a positive integer: {value}")
    return page


def _parse_page_range(page_range: str | None) -> tuple[int | None, int | None] | None:
    if page_range is None:
        return None

    value = page_range.strip()
    if not value:
        raise ReproductionInputError("--page-range cannot be empty")

    if ":" not in value:
        page = _parse_positive_page_number(value, label="page range")
        return page, page

    start_raw, end_raw = value.split(":", 1)
    if not start_raw and not end_raw:
        raise ReproductionInputError("--page-range must include a start or end page")

    start = (
        _parse_positive_page_number(start_raw, label="page range start")
        if start_raw
        else None
    )
    end = (
        _parse_positive_page_number(end_raw, label="page range end")
        if end_raw
        else None
    )
    if start is not None and end is not None and start > end:
        raise ReproductionInputError("--page-range start must be <= end")
    return start, end


def _filter_replay_pages(
    pages: list[Path],
    page_range: str | None,
) -> list[Path]:
    parsed_range = _parse_page_range(page_range)
    if parsed_range is None:
        return pages

    start, end = parsed_range
    selected = []
    for page_dir in pages:
        page = _page_number(page_dir, 0)
        if page <= 0:
            continue
        if start is not None and page < start:
            continue
        if end is not None and page > end:
            continue
        selected.append(page_dir)

    if not selected:
        raise ReproductionInputError(
            f"no page_N directories matched --page-range {page_range}"
        )
    return selected


def _next_page_number(run_dir: Path) -> int:
    state_path = run_dir / "experiment_state.json"
    if state_path.exists():
        try:
            state = read_json(state_path)
            return int(state.get("page_count", 0)) + 1
        except Exception:
            pass
    inputs_dir = run_dir / "inputs"
    if not inputs_dir.is_dir():
        return 1
    page_numbers = [
        _page_number(path, 0)
        for path in inputs_dir.iterdir()
        if path.is_dir() and path.name.startswith("page_")
    ]
    return max(page_numbers, default=0) + 1


def _write_input_meta(
    *,
    input_dir: Path,
    source_input_dir: Path | None,
    screenshot_path: Path,
    a11y_path: Path,
    device_serial: str | None,
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = {
        "captured_at": iso_now(),
        "device_serial": device_serial,
        "source_input_dir": str(source_input_dir) if source_input_dir else None,
        "input_screenshot_path": str(screenshot_path),
        "input_a11y_path": str(a11y_path),
        "input_screenshot_sha256": sha256_file(screenshot_path),
        "input_a11y_sha256": sha256_file(a11y_path),
    }
    if source_meta:
        app_id = source_meta.get("app_id")
        if isinstance(app_id, str) and app_id.strip():
            meta["app_id"] = app_id.strip()
    write_json(input_dir / "input_meta.json", meta)
    return meta


def _copy_page_inputs(
    *,
    source_dir: Path,
    input_dir: Path,
    page: int,
    device_serial: str | None,
) -> CapturedPage:
    src_screenshot = _resolve_page_file(
        source_dir,
        ("input_screenshot.png", "screenshot.png", "screen.png"),
        "screenshot",
    )
    src_a11y = _resolve_page_file(
        source_dir,
        ("input_a11y.xml", "a11y.xml", "accessibility.xml"),
        "a11y XML",
    )
    screenshot_path = input_dir / "input_screenshot.png"
    a11y_path = input_dir / "input_a11y.xml"
    same_dir = source_dir.resolve() == input_dir.resolve()
    if same_dir:
        screenshot_path = src_screenshot
        a11y_path = src_a11y
    else:
        _copy_file_if_needed(src_screenshot, screenshot_path)
        _copy_file_if_needed(src_a11y, a11y_path)
    a11y_xml = a11y_path.read_text(encoding="utf-8")
    if not a11y_xml.strip():
        raise ReproductionInputError(f"a11y XML is empty: {a11y_path}")
    source_meta_path = source_dir / "input_meta.json"
    source_meta = read_json(source_meta_path) if source_meta_path.exists() else None
    if same_dir and source_meta is not None:
        meta = source_meta
    else:
        meta = _write_input_meta(
            input_dir=input_dir,
            source_input_dir=source_dir,
            screenshot_path=screenshot_path,
            a11y_path=a11y_path,
            device_serial=device_serial,
            source_meta=source_meta,
        )
    return CapturedPage(page, input_dir, screenshot_path, a11y_path, a11y_xml, meta)


def _capture_live_page(
    *,
    input_dir: Path,
    page: int,
    config: ReproductionConfig,
) -> CapturedPage:
    input_dir.mkdir(parents=True, exist_ok=True)
    if config.screenshot_path and config.a11y_path:
        screenshot_path = input_dir / "input_screenshot.png"
        a11y_path = input_dir / "input_a11y.xml"
        _copy_file_if_needed(config.screenshot_path, screenshot_path)
        _copy_file_if_needed(config.a11y_path, a11y_path)
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
    meta = _write_input_meta(
        input_dir=input_dir,
        source_input_dir=None,
        screenshot_path=screenshot_path,
        a11y_path=a11y_path,
        device_serial=config.device_serial,
    )
    return CapturedPage(page, input_dir, screenshot_path, a11y_path, a11y_xml, meta)


def _prepare_pages(config: ReproductionConfig) -> list[CapturedPage]:
    replay_source = config.replay_from
    if config.reuse_v2_from is not None:
        replay_source = config.reuse_v2_from / "inputs"
    elif config.reuse_v2chunked_from is not None and replay_source is None:
        replay_source = config.reuse_v2chunked_from / "inputs"

    if replay_source is None:
        page = _next_page_number(config.output_dir)
        input_dir = config.output_dir / "inputs" / f"page_{page}"
        return [_capture_live_page(input_dir=input_dir, page=page, config=config)]

    pages: list[CapturedPage] = []
    replay_pages = _filter_replay_pages(
        _discover_replay_pages(replay_source),
        config.page_range,
    )
    for index, source_page in enumerate(replay_pages, start=1):
        page = _page_number(source_page, index)
        input_dir = config.output_dir / "inputs" / f"page_{page}"
        pages.append(
            _copy_page_inputs(
                source_dir=source_page,
                input_dir=input_dir,
                page=page,
                device_serial=config.device_serial,
            )
        )
    return pages


def _resolve_static_semantics(config: ReproductionConfig) -> StaticSemanticsInputs:
    run_dir = config.static_semantics_run_dir
    if run_dir is None and (
        any(v in (3, 4) for v in config.variants)
        or any(is_context_window_chunked(v) for v in config.variants)
        or config.enable_v2_critic
        or config.enable_v2chunked_critic_chunked
    ):
        if config.context_slicer_dir is None and config.method_cfg_index_path is None:
            run_dir = find_latest_static_semantics_run_dir(
                DEFAULT_STATIC_SEMANTICS_RUNS_DIR,
                require_bundle=False,
            )
    return static_semantics_paths_from_run(
        run_dir=run_dir,
        context_slicer_dir=config.context_slicer_dir,
        method_cfg_index_path=config.method_cfg_index_path,
    )


def _build_page_code_contexts(
    *,
    page: CapturedPage,
    config: ReproductionConfig,
    static_inputs: StaticSemanticsInputs,
    raw_source_code: str | None,
) -> dict[VariantKey, dict[str, Any]]:
    contexts: dict[VariantKey, dict[str, Any]] = {}
    if 2 in config.variants:
        contexts[2] = {"raw_source_code": raw_source_code or ""}
    needs_context_window_chunks = any(
        is_context_window_chunked(v) for v in config.variants
    )
    needs_static_context = any(v in (3, 4) for v in config.variants) or (
        config.enable_v2_critic
        or config.enable_v2chunked_critic_chunked
        or config.emit_resource_evidence_chunks
        or needs_context_window_chunks
    )
    if not needs_static_context:
        return contexts
    sliced_path = page.input_dir / "sliced_methods_context.json"
    static_payloads = build_static_code_context_payloads(
        a11y_path=page.a11y_path,
        context_slicer_dir=static_inputs.context_slicer_dir,
        method_cfg_index_path=static_inputs.method_cfg_index_path,
        include_sliced_methods=(
            3 in config.variants
            or config.enable_v2_critic
            or config.enable_v2chunked_critic_chunked
            or config.emit_resource_evidence_chunks
            or needs_context_window_chunks
        ),
        include_static_analysis=(
            4 in config.variants
            or config.enable_v2_critic
            or config.enable_v2chunked_critic_chunked
            or config.emit_resource_evidence_chunks
            or needs_context_window_chunks
        ),
        sliced_output_path=sliced_path,
        context_required_message=(
            "variant 3/4, V2 critic, or context-window chunks require "
            "context-slicer-output"
        ),
        cfg_required_message=(
            "variant 4, V2 critic, or context-window chunks require "
            "method-cfg-index.json"
        ),
    )
    sliced_payload = static_payloads.sliced_methods_payload
    cfg_payload = static_payloads.static_analysis_payload
    if 3 in config.variants:
        contexts[3] = {"sliced_methods_payload": sliced_payload}
    if config.emit_resource_evidence_chunks or needs_context_window_chunks:
        contexts["context_window"] = {
            "sliced_methods_payload": sliced_payload,
            "static_analysis_payload": cfg_payload,
        }
    if (
        4 in config.variants
        or config.enable_v2_critic
        or config.enable_v2chunked_critic_chunked
    ):
        if (
            config.prepare_only
            or config.enable_v2_critic
            or config.enable_v2chunked_critic_chunked
        ):
            write_json(page.input_dir / "static_analysis_context.json", cfg_payload)
        if config.enable_v2_critic:
            contexts[V2_CRITIC_VARIANT_KEY] = {
                "sliced_methods_payload": sliced_payload,
                "static_analysis_payload": cfg_payload,
            }
            contexts[V2_CHUNKED_CRITIC_VARIANT_KEY] = contexts[V2_CRITIC_VARIANT_KEY]
            analysis_payload = render_static_critic_evidence(
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=cfg_payload,
                critic_label="V2 Step 2 critic",
            )
            (page.input_dir / "analysis_payload.txt").write_text(
                analysis_payload,
                encoding="utf-8",
            )
        if config.enable_v2chunked_critic_chunked:
            contexts[V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY] = {
                "sliced_methods_payload": sliced_payload,
                "static_analysis_payload": cfg_payload,
            }
        contexts[4] = {
            "sliced_methods_payload": sliced_payload,
            "static_analysis_payload": cfg_payload,
        }
    return contexts


def _load_accumulated_predicates(
    run_dir: Path,
    variant: VariantKey,
) -> list[dict[str, Any]]:
    acc_path = run_dir / _variant_dir_name(variant) / "accumulated_predicates.json"
    if not acc_path.exists():
        return []
    try:
        with open(acc_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        log(f"[reproduction_pipeline] failed to load {acc_path}: {exc}", "yellow")
        return []
    if isinstance(raw, dict):
        return raw.get("State_Definitions") or raw.get("Predicates", [])
    return raw if isinstance(raw, list) else []


def _load_accumulated_predicates_for_page(
    run_dir: Path,
    variant: VariantKey,
    page: int,
) -> list[dict[str, Any]]:
    if page <= 1:
        return []
    return _load_accumulated_predicates(run_dir, variant)


def _read_optional_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = read_json(path)
    return payload if isinstance(payload, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_reused_v2_step1_output(
    *,
    reuse_run_dir: Path,
    page: int,
    fallback_model: str,
) -> VariantRunOutput:
    source_page_dir = reuse_run_dir / "variant_2" / f"page_{page}"
    if not source_page_dir.is_dir():
        raise ReproductionInputError(
            f"reused V2 page output not found: {source_page_dir}"
        )

    system_prompt_path = source_page_dir / "prompt_system.txt"
    user_prompt_path = source_page_dir / "prompt_user.txt"
    parsed_path = source_page_dir / "response_parsed.json"
    missing = [
        path
        for path in (system_prompt_path, user_prompt_path, parsed_path)
        if not path.is_file()
    ]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise ReproductionInputError(f"reused V2 page output is incomplete: {names}")

    system_prompt = system_prompt_path.read_text(encoding="utf-8")
    user_prompt = user_prompt_path.read_text(encoding="utf-8")
    parsed_payload = read_json(parsed_path)
    response = PredicateResponse.model_validate(parsed_payload)

    raw_path = source_page_dir / "response_raw.txt"
    raw_json = (
        raw_path.read_text(encoding="utf-8")
        if raw_path.is_file()
        else response.model_dump_json()
    )
    usage_path = source_page_dir / "usage.json"
    usage = _read_optional_json_object(usage_path)
    run_meta = _read_optional_json_object(source_page_dir / "run_meta.json")
    model = str(run_meta.get("model") or fallback_model)

    result = LLMResult(
        model=model,
        variant=2,
        response=response,
        prompt_tokens=_as_int(usage.get("prompt_tokens")),
        completion_tokens=_as_int(usage.get("completion_tokens")),
        latency_sec=_as_float(usage.get("latency_sec")),
        cached_tokens=_as_int(usage.get("cached_tokens")),
        reasoning_tokens=_as_int(usage.get("reasoning_tokens")),
        raw_json=raw_json,
        messages_sent=[],
    )
    return VariantRunOutput(
        paths={
            "prompt_system": str(system_prompt_path),
            "prompt_user": str(user_prompt_path),
            "response_parsed": str(parsed_path),
            "response_raw": str(raw_path) if raw_path.is_file() else None,
            "usage": str(usage_path) if usage_path.is_file() else None,
            "run_meta": str(source_page_dir / "run_meta.json"),
            "status": "reused",
            "api_called": False,
            "step1_source": "reused_v2",
            "source_run_dir": str(reuse_run_dir),
            "source_page_dir": str(source_page_dir),
            "predicate_count": len(response.State_Definitions),
        },
        result=result,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        source_page_dir=source_page_dir,
        step1_source="reused_v2",
        step1_api_called=False,
    )


def _load_reused_v2chunked_step1_output(
    *,
    reuse_run_dir: Path,
    page: int,
    fallback_model: str,
) -> VariantRunOutput:
    source_page_dir = reuse_run_dir / V2_CHUNKED_VARIANT_DIR / f"page_{page}"
    if not source_page_dir.is_dir():
        raise ReproductionInputError(
            f"reused V2-chunked page output not found: {source_page_dir}"
        )

    parsed_path = source_page_dir / "response_parsed.json"
    if not parsed_path.is_file():
        raise ReproductionInputError(
            f"reused V2-chunked page output is incomplete: {parsed_path}"
        )

    prompt_user_path = source_page_dir / "prompt_user.txt"
    if not prompt_user_path.is_file():
        prompt_candidates = sorted(
            (source_page_dir / "chunk_prompts").glob("*_user.txt"),
            key=lambda path: path.name,
        )
        prompt_user_path = prompt_candidates[0] if prompt_candidates else prompt_user_path
    if not prompt_user_path.is_file():
        raise ReproductionInputError(
            "reused V2-chunked page output is incomplete: missing Step 1 "
            f"user prompt under {source_page_dir}"
        )

    user_prompt = prompt_user_path.read_text(encoding="utf-8").replace(
        "- Raw Source Code: (provided in the system context; use it as the primary grounding source.)",
        "- Raw Source Code: (provided separately during Step 1 extraction.)",
    )
    parsed_payload = read_json(parsed_path)
    response = PredicateResponse.model_validate(parsed_payload)

    raw_path = source_page_dir / "response_raw.txt"
    raw_json = (
        raw_path.read_text(encoding="utf-8")
        if raw_path.is_file()
        else response.model_dump_json()
    )
    usage_path = source_page_dir / "usage.json"
    usage = _read_optional_json_object(usage_path)
    run_meta = _read_optional_json_object(source_page_dir / "run_meta.json")
    model = str(run_meta.get("model") or fallback_model)

    result = LLMResult(
        model=model,
        variant=2,
        response=response,
        prompt_tokens=_as_int(usage.get("prompt_tokens")),
        completion_tokens=_as_int(usage.get("completion_tokens")),
        latency_sec=_as_float(usage.get("latency_sec")),
        cached_tokens=_as_int(usage.get("cached_tokens")),
        reasoning_tokens=_as_int(usage.get("reasoning_tokens")),
        raw_json=raw_json,
        messages_sent=[],
        request_meta=run_meta.get("request") if isinstance(run_meta, dict) else None,
    )
    paths = {
        "response_parsed": str(parsed_path),
        "response_raw": str(raw_path) if raw_path.is_file() else None,
        "usage": str(usage_path) if usage_path.is_file() else None,
        "run_meta": str(source_page_dir / "run_meta.json"),
        "chunks_manifest": str(source_page_dir / "chunks_manifest.json"),
        "chunk_inputs": str(source_page_dir / "chunk_inputs"),
        "chunk_prompts": str(source_page_dir / "chunk_prompts"),
        "chunk_outputs": str(source_page_dir / "chunk_outputs"),
        "dedupe_meta": str(source_page_dir / "dedupe_meta.json"),
        "status": "reused",
        "api_called": False,
        "step1_source": "reused_v2chunked",
        "source_run_dir": str(reuse_run_dir),
        "source_page_dir": str(source_page_dir),
        "predicate_count": len(response.State_Definitions),
    }
    return VariantRunOutput(
        paths=paths,
        result=result,
        user_prompt=user_prompt,
        source_page_dir=source_page_dir,
        step1_source="reused_v2chunked",
        step1_api_called=False,
    )


def _save_accumulated_predicates(
    run_dir: Path,
    variant: VariantKey,
    predicates: list[dict[str, Any]],
) -> None:
    out_dir = run_dir / _variant_dir_name(variant)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "accumulated_predicates.json", {"State_Definitions": predicates})


def _result_predicates(result: LLMResult) -> list[dict[str, Any]]:
    return [pred.model_dump(exclude_none=True) for pred in result.response.State_Definitions]


def _page_app_name(config: ReproductionConfig, page: CapturedPage) -> str:
    app_id = page.meta.get("app_id")
    if isinstance(app_id, str) and app_id.strip():
        return app_id.strip()
    return config.app_source_root.name


def _usage_dict(result: LLMResult) -> dict[str, Any]:
    return _api_result_usage_dict(result)


def _critic_usage_dict(result: CriticResult) -> dict[str, Any]:
    return _api_result_usage_dict(
        result,
        include_max_completion_tokens=True,
        extra={"turn": "step2_critic"},
    )


def _exception_request_meta(exc: BaseException) -> dict[str, Any] | None:
    return attached_request_meta(exc)


def _save_failure_artifacts(variant_dir: Path, exc: BaseException) -> list[dict[str, Any]]:
    artifact_records: list[dict[str, Any]] = []
    candidates = _exception_artifact_candidates(exc)
    if not candidates:
        return artifact_records

    artifacts_dir = variant_dir / "failure_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        text = candidate.get("text")
        filename = candidate.get("filename")
        if not isinstance(text, str) or not isinstance(filename, str):
            continue
        artifact_path = artifacts_dir / filename
        artifact_path.write_text(text, encoding="utf-8")
        record = {
            key: value
            for key, value in candidate.items()
            if key not in {"text", "filename"}
        }
        record["path"] = str(artifact_path)
        if text:
            tail_path = artifacts_dir / f"{artifact_path.stem}.tail.txt"
            tail = text[-FAILURE_ARTIFACT_TAIL_CHARS:]
            tail_path.write_text(tail, encoding="utf-8")
            record["tail_path"] = str(tail_path)
            record["tail_chars"] = len(tail)
        artifact_records.append(record)
    return artifact_records


def _save_api_failure_outputs(
    *,
    variant_dir: Path,
    run_id: str,
    variant: VariantKey,
    page: int,
    model: str,
    prompt_strategy: str,
    prompt_cache_key: str | None,
    exc: BaseException,
    phase: str,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    critic_prompt: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    if system_prompt is not None:
        (variant_dir / "prompt_system.txt").write_text(system_prompt, encoding="utf-8")
    if user_prompt is not None:
        (variant_dir / "prompt_user.txt").write_text(user_prompt, encoding="utf-8")
    if critic_prompt is not None:
        (variant_dir / "prompt_user_step2_critic.txt").write_text(
            critic_prompt,
            encoding="utf-8",
        )
    request_meta = _exception_request_meta(exc)
    failure_meta = {
        "run_id": run_id,
        "variant": variant,
        "page": page,
        "model": model,
        "lane": "reproduction",
        "status": "failed",
        "phase": phase,
        "prompt_strategy": prompt_strategy,
        "prompt_cache_key": prompt_cache_key,
        "api_called": True,
        "failed_at": iso_now(),
        "error_type": type(exc).__name__,
        "error_message": _error_message(exc),
        "request": request_meta,
    }
    artifact_records = _save_failure_artifacts(variant_dir, exc)
    if artifact_records:
        failure_meta["failure_artifacts"] = artifact_records
    if extra_meta:
        failure_meta.update(extra_meta)
    write_json(variant_dir / "failure_meta.json", failure_meta)
    write_json(variant_dir / "run_meta.json", failure_meta)
    return {
        "prompt_system": (
            str(variant_dir / "prompt_system.txt") if system_prompt is not None else None
        ),
        "prompt_user": (
            str(variant_dir / "prompt_user.txt") if user_prompt is not None else None
        ),
        "prompt_user_step2_critic": (
            str(variant_dir / "prompt_user_step2_critic.txt")
            if critic_prompt is not None
            else None
        ),
        "failure_meta": str(variant_dir / "failure_meta.json"),
        "failure_artifacts": (
            str(variant_dir / "failure_artifacts") if artifact_records else None
        ),
        "run_meta": str(variant_dir / "run_meta.json"),
        "status": "failed",
        "api_called": True,
        "prompt_strategy": prompt_strategy,
    }


def _prompt_strategy(variant: VariantKey) -> str:
    if str(variant) == V2_CRITIC_VARIANT_KEY:
        return V2_CRITIC_PROMPT_STRATEGY
    if str(variant) == V2_CRITIC_NO_ANALYSIS_VARIANT_KEY:
        return V2_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY
    if str(variant) == V2_CHUNKED_CRITIC_VARIANT_KEY:
        return V2_CHUNKED_CRITIC_PROMPT_STRATEGY
    if str(variant) == V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY:
        return V2_CHUNKED_CRITIC_CHUNKED_PROMPT_STRATEGY
    if str(variant) == V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY:
        return V2_CHUNKED_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY
    return default_prompt_strategy(variant)


def _build_reproduction_prompt(
    *,
    variant: int,
    context: MergedContext,
) -> tuple[str, str]:
    return build_prompt(variant, context)


def _build_v2_chunked_prior_user_prompt(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    existing_predicates: list[dict[str, Any]],
) -> str:
    prompt = _load_prompt_text("user_variant2.txt").format(
        app_name=_page_app_name(config, page),
        existing_predicates=json.dumps(existing_predicates, ensure_ascii=False, indent=2),
        accessibility_tree=page.a11y_xml,
    )
    return prompt.replace(
        "- Raw Source Code: (provided in the system context; use it as the primary grounding source.)",
        "- Raw Source Code: (provided separately during Step 1 extraction.)",
    )


def _save_variant_outputs(
    *,
    variant_dir: Path,
    system_prompt: str,
    user_prompt: str,
    result: LLMResult,
    run_id: str,
    page: int,
    prompt_cache_key: str | None,
    prompt_strategy: str,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "prompt_system.txt").write_text(system_prompt, encoding="utf-8")
    (variant_dir / "prompt_user.txt").write_text(user_prompt, encoding="utf-8")
    write_json(
        variant_dir / "response_parsed.json",
        {
            "Analysis": result.response.Analysis,
            "State_Definitions": _result_predicates(result),
        },
    )
    if result.raw_json:
        (variant_dir / "response_raw.txt").write_text(result.raw_json, encoding="utf-8")
    usage = _usage_dict(result)
    write_json(variant_dir / "usage.json", usage)
    write_json(
        variant_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": result.variant,
            "page": page,
            "model": result.model,
            "lane": "reproduction",
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "latency_sec": round(result.latency_sec, 3),
            "predicate_count": len(result.response.State_Definitions),
            "request": result.request_meta or None,
        },
    )
    return {
        "prompt_system": str(variant_dir / "prompt_system.txt"),
        "prompt_user": str(variant_dir / "prompt_user.txt"),
        "response_parsed": str(variant_dir / "response_parsed.json"),
        "response_raw": str(variant_dir / "response_raw.txt"),
        "usage": str(variant_dir / "usage.json"),
        "run_meta": str(variant_dir / "run_meta.json"),
        "latency_sec": round(result.latency_sec, 3),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "cached_tokens": result.cached_tokens,
        "cache_hit_rate": usage["cache_hit_rate"],
        "prompt_strategy": prompt_strategy,
        "predicate_count": len(result.response.State_Definitions),
    }


def _save_prepared_variant_outputs(
    *,
    variant_dir: Path,
    system_prompt: str,
    user_prompt: str,
    run_id: str,
    page: int,
    variant: VariantKey,
    model: str,
    prompt_cache_key: str | None,
    prompt_strategy: str,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    prompt_system_path = variant_dir / "prompt_system.txt"
    prompt_user_path = variant_dir / "prompt_user.txt"
    run_meta_path = variant_dir / "run_meta.json"
    prompt_system_path.write_text(system_prompt, encoding="utf-8")
    prompt_user_path.write_text(user_prompt, encoding="utf-8")
    write_json(
        run_meta_path,
        {
            "run_id": run_id,
            "variant": variant,
            "page": page,
            "model": model,
            "lane": "reproduction",
            "status": "prepared",
            "prepare_only": True,
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "api_called": False,
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "prepared_at": iso_now(),
        },
    )
    return {
        "prompt_system": str(prompt_system_path),
        "prompt_user": str(prompt_user_path),
        "response_parsed": None,
        "response_raw": None,
        "usage": None,
        "run_meta": str(run_meta_path),
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "prompt_strategy": prompt_strategy,
        "predicate_count": None,
    }


def _save_prepared_v2_critic_outputs(
    *,
    variant_dir: Path,
    variant_key: str,
    prompt_strategy: str,
    system_prompt: str,
    user_prompt: str,
    run_id: str,
    page: int,
    model: str,
    prompt_cache_key: str | None,
    input_dir: Path,
    analysis_payload_available: bool,
    analysis_payload_path: Path | None = None,
    source_step1_page_dir: Path | None = None,
    step1_result: LLMResult | None = None,
    critic_prompt: str | None = None,
    step1_source: str = "integrated_v2",
    step1_api_called: bool = False,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    if analysis_payload_available and analysis_payload_path is None:
        analysis_payload_path = input_dir / "analysis_payload.txt"
    prompt_system_path = variant_dir / "prompt_system.txt"
    prompt_user_path = variant_dir / "prompt_user.txt"
    prompt_step2_path = variant_dir / "prompt_user_step2_critic.txt"
    response_raw_path = variant_dir / "response_raw.txt"
    usage_path = variant_dir / "usage.json"
    run_meta_path = variant_dir / "run_meta.json"
    prompt_system_path.write_text(system_prompt, encoding="utf-8")
    prompt_user_path.write_text(user_prompt, encoding="utf-8")
    if critic_prompt is not None:
        prompt_step2_path.write_text(critic_prompt, encoding="utf-8")
    if step1_result is not None and step1_result.raw_json:
        response_raw_path.write_text(step1_result.raw_json, encoding="utf-8")
    if step1_result is not None:
        step1_usage = _usage_dict(step1_result)
        step1_usage["turn"] = "step1_reused" if step1_source == "reused_v2" else "step1"
        write_json(usage_path, step1_usage)
    write_json(
        run_meta_path,
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": 2,
            "page": page,
            "model": model,
            "lane": variant_key,
            "mode": "integrated_reproduction_critic",
            "status": "prepared",
            "prepare_only": True,
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "input_dir": str(input_dir),
            "source_step1_page_dir": (
                str(source_step1_page_dir) if source_step1_page_dir else None
            ),
            "source_variant_2_page_dir": (
                str(source_step1_page_dir) if source_step1_page_dir else None
            ),
            "api_called": False,
            "step1_source": step1_source,
            "step1_api_called": step1_api_called,
            "critic_enabled": True,
            "critic_no_analysis": variant_key == V2_CRITIC_NO_ANALYSIS_VARIANT_KEY,
            "critic_api_called": False,
            "critic_step2_prompt_available": critic_prompt is not None,
            "critic_step2_prompt_blocked_reason": (
                None if critic_prompt is not None else "requires_step1_response"
            ),
            "analysis_payload_available": analysis_payload_path is not None,
            "analysis_payload": (
                str(analysis_payload_path) if analysis_payload_path is not None else None
            ),
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "prepared_at": iso_now(),
        },
    )
    return {
        "prompt_system": str(prompt_system_path),
        "prompt_user": str(prompt_user_path),
        "prompt_user_step2_critic": (
            str(prompt_step2_path) if critic_prompt is not None else None
        ),
        "response_parsed": None,
        "response_raw": (
            str(response_raw_path)
            if step1_result is not None and step1_result.raw_json
            else None
        ),
        "response_step2_critic_raw": None,
        "critic_verdicts": None,
        "usage": str(usage_path) if step1_result is not None else None,
        "usage_step2_critic": None,
        "run_meta": str(run_meta_path),
        "analysis_payload": (
            str(analysis_payload_path) if analysis_payload_path is not None else None
        ),
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "step1_source": step1_source,
        "step1_api_called": step1_api_called,
        "source_step1_page_dir": (
            str(source_step1_page_dir) if source_step1_page_dir else None
        ),
        "prompt_strategy": prompt_strategy,
        "predicate_count": None,
    }


def _save_prepared_v2_chunked_critic_outputs(
    *,
    variant_dir: Path,
    variant_key: str = V2_CHUNKED_CRITIC_VARIANT_KEY,
    prompt_strategy: str = V2_CHUNKED_CRITIC_PROMPT_STRATEGY,
    system_prompt: str,
    user_prompt: str,
    run_id: str,
    page: int,
    model: str,
    prompt_cache_key: str | None,
    input_dir: Path,
    source_step1_page_dir: Path,
    analysis_payload_available: bool,
    step1_source: str = "integrated_v2chunked",
    step1_api_called: bool = False,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    prompt_system_path = variant_dir / "prompt_system.txt"
    prompt_user_path = variant_dir / "prompt_user.txt"
    run_meta_path = variant_dir / "run_meta.json"
    prompt_system_path.write_text(system_prompt, encoding="utf-8")
    prompt_user_path.write_text(user_prompt, encoding="utf-8")
    write_json(
        run_meta_path,
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": 2,
            "page": page,
            "model": model,
            "lane": variant_key,
            "mode": "integrated_reproduction_critic",
            "status": "prepared",
            "prepare_only": True,
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "input_dir": str(input_dir),
            "source_step1_page_dir": str(source_step1_page_dir),
            "source_variant_2chunked_page_dir": str(source_step1_page_dir),
            "api_called": False,
            "step1_source": step1_source,
            "step1_api_called": step1_api_called,
            "critic_enabled": True,
            "critic_no_analysis": (
                variant_key == V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY
            ),
            "critic_api_called": False,
            "critic_prior_turns": 2,
            "critic_step2_prompt_available": False,
            "critic_step2_prompt_blocked_reason": "requires_step1_response",
            "analysis_payload_available": analysis_payload_available,
            "analysis_payload": (
                str(input_dir / "analysis_payload_v2chunked.txt")
                if analysis_payload_available
                else None
            ),
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "prepared_at": iso_now(),
        },
    )
    return {
        "prompt_system": str(prompt_system_path),
        "prompt_user": str(prompt_user_path),
        "prompt_user_step2_critic": None,
        "response_parsed": None,
        "response_raw": None,
        "response_step2_critic_raw": None,
        "critic_verdicts": None,
        "usage": None,
        "usage_step2_critic": None,
        "run_meta": str(run_meta_path),
        "analysis_payload": (
            str(input_dir / "analysis_payload_v2chunked.txt")
            if analysis_payload_available
            else None
        ),
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "prompt_strategy": prompt_strategy,
        "predicate_count": None,
    }


def _step1_usage_turn(step1_source: str) -> str:
    if step1_source == "reused_v2":
        return "step1_reused"
    if step1_source == "reused_v2chunked":
        return "step1_reused_v2chunked"
    if step1_source == "integrated_v2chunked":
        return "step1_integrated_v2chunked"
    return "step1_integrated_v2"


def _load_prompt_text(name: str) -> str:
    return (PYTHON_DIR / "prompts" / name).read_text(encoding="utf-8")


def _llm_result_with_predicates(
    result: LLMResult,
    predicates: list[Any],
    analysis_suffix: str,
) -> LLMResult:
    analysis = result.response.Analysis
    if analysis_suffix:
        analysis = f"{analysis}\n\n{analysis_suffix}"
    response = result.response.model_copy(
        update={
            "Analysis": analysis,
            "State_Definitions": predicates,
        }
    )
    return replace(
        result,
        response=response,
        raw_json=response.model_dump_json(),
    )


def _save_step2_critic_outputs(
    *,
    variant_dir: Path,
    variant_key: str,
    prompt_strategy: str,
    system_prompt: str,
    user_prompt: str | None,
    step1_result: LLMResult,
    final_result: LLMResult,
    critic_result: CriticResult,
    critic_prompt: str,
    run_id: str,
    page: int,
    prompt_cache_key: str | None,
    input_dir: Path,
    source_step1_page_dir: Path,
    step1_source: str,
    step1_api_called: bool,
    critic_prior_turns: int,
    analysis_payload_path: Path | None = None,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    prompt_system_path = variant_dir / "prompt_system.txt"
    prompt_user_path = variant_dir / "prompt_user.txt"
    prompt_step2_path = variant_dir / "prompt_user_step2_critic.txt"
    prompt_system_path.write_text(system_prompt, encoding="utf-8")
    if user_prompt is not None:
        prompt_user_path.write_text(user_prompt, encoding="utf-8")
    prompt_step2_path.write_text(critic_prompt, encoding="utf-8")
    if step1_result.raw_json:
        (variant_dir / "response_raw.txt").write_text(
            step1_result.raw_json,
            encoding="utf-8",
        )
    write_json(
        variant_dir / "response_parsed.json",
        {
            "Analysis": final_result.response.Analysis,
            "State_Definitions": _result_predicates(final_result),
        },
    )
    if critic_result.raw_json:
        (variant_dir / "response_step2_critic_raw.txt").write_text(
            critic_result.raw_json,
            encoding="utf-8",
        )
    write_json(
        variant_dir / "critic_verdicts.json",
        critic_result.response.model_dump(),
    )
    step1_usage = _usage_dict(step1_result)
    step1_usage["turn"] = _step1_usage_turn(step1_source)
    write_json(variant_dir / "usage.json", step1_usage)
    critic_usage = _critic_usage_dict(critic_result)
    write_json(variant_dir / "usage_step2_critic.json", critic_usage)
    write_json(
        variant_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": 2,
            "page": page,
            "model": final_result.model,
            "lane": variant_key,
            "mode": "integrated_reproduction_critic",
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "input_dir": str(input_dir),
            "source_step1_page_dir": str(source_step1_page_dir),
            "source_variant_2_page_dir": (
                str(source_step1_page_dir)
                if variant_key
                in {V2_CRITIC_VARIANT_KEY, V2_CRITIC_NO_ANALYSIS_VARIANT_KEY}
                else None
            ),
            "source_variant_2chunked_page_dir": (
                str(source_step1_page_dir)
                if variant_key
                in {
                    V2_CHUNKED_CRITIC_VARIANT_KEY,
                    V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY,
                }
                else None
            ),
            "latency_sec": round(final_result.latency_sec, 3),
            "predicate_count": len(final_result.response.State_Definitions),
            "step1_source": step1_source,
            "step1_api_called": step1_api_called,
            "critic_enabled": True,
            "critic_no_analysis": variant_key
            in {
                V2_CRITIC_NO_ANALYSIS_VARIANT_KEY,
                V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY,
            },
            "critic_api_called": True,
            "critic_applied": True,
            "critic_skipped_reason": None,
            "critic_prior_turns": critic_prior_turns,
            "critic_max_completion_tokens": DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
            "analysis_payload_available": analysis_payload_path is not None,
            "analysis_payload": (
                str(analysis_payload_path) if analysis_payload_path is not None else None
            ),
            "api_called": True,
            "step1_request": step1_result.request_meta or None,
            "critic_request": critic_result.request_meta or None,
        },
    )
    return {
        "prompt_system": str(prompt_system_path),
        "prompt_user": str(prompt_user_path) if user_prompt is not None else None,
        "prompt_user_step2_critic": str(prompt_step2_path),
        "response_parsed": str(variant_dir / "response_parsed.json"),
        "response_raw": str(variant_dir / "response_raw.txt"),
        "response_step2_critic_raw": str(
            variant_dir / "response_step2_critic_raw.txt"
        ),
        "critic_verdicts": str(variant_dir / "critic_verdicts.json"),
        "usage": str(variant_dir / "usage.json"),
        "usage_step2_critic": str(variant_dir / "usage_step2_critic.json"),
        "run_meta": str(variant_dir / "run_meta.json"),
        "analysis_payload": (
            str(analysis_payload_path) if analysis_payload_path is not None else None
        ),
        "source_step1_page_dir": str(source_step1_page_dir),
        "step1_source": step1_source,
        "step1_api_called": step1_api_called,
        "latency_sec": round(final_result.latency_sec, 3),
        "prompt_tokens": step1_result.prompt_tokens,
        "completion_tokens": step1_result.completion_tokens,
        "critic_prompt_tokens": critic_result.prompt_tokens,
        "critic_completion_tokens": critic_result.completion_tokens,
        "critic_reasoning_tokens": critic_result.reasoning_tokens,
        "critic_cached_tokens": critic_result.cached_tokens,
        "prompt_strategy": prompt_strategy,
        "predicate_count": len(final_result.response.State_Definitions),
    }


def _page_entry(page: CapturedPage) -> dict[str, Any]:
    return {
        "page": page.page,
        "input_hashes": {
            "screenshot_sha256": page.meta["input_screenshot_sha256"],
            "a11y_sha256": page.meta["input_a11y_sha256"],
        },
        "input_files": {
            "screenshot": str(page.screenshot_path),
            "a11y": str(page.a11y_path),
            "meta": str(page.input_dir / "input_meta.json"),
        },
        "variant_outputs": {},
    }


def _rendered_chunk_paths(base_dir: Path, mode_dir: str, chunk_count: int) -> list[str]:
    return [
        str(base_dir / "rendered" / mode_dir / f"chunk_{idx:04d}.txt")
        for idx in range(1, chunk_count + 1)
    ]


def _direct_chunk_paths(base_dir: Path, mode_dir: str, chunk_count: int) -> list[str]:
    return [
        str(base_dir / mode_dir / f"chunk_{idx:04d}.txt")
        for idx in range(1, chunk_count + 1)
    ]


def _emit_context_window_chunk_artifacts(
    *,
    page: CapturedPage,
    config: ReproductionConfig,
    context_window_ctx: dict[str, Any],
) -> dict[str, Any] | None:
    sliced_payload = context_window_ctx.get("sliced_methods_payload")
    static_payload = context_window_ctx.get("static_analysis_payload")
    if not isinstance(sliced_payload, dict) or not isinstance(static_payload, dict):
        log(
            "[reproduction_pipeline] context-window chunks skipped: "
            "static context payloads are unavailable",
            "yellow",
        )
        return None

    output_dir = page.input_dir / "context_window"
    rendered_dirs = {
        "v3": output_dir / "rendered" / "variant_3",
        "v4": output_dir / "rendered" / "variant_4",
        "critic": output_dir / "rendered" / "v2_critic",
    }
    for rendered_dir in rendered_dirs.values():
        rendered_dir.mkdir(parents=True, exist_ok=True)

    index = build_context_evidence_index(
        sliced_payload,
        static_payload,
        domain_attribution_policy=config.context_window_domain_attribution,
    )
    packed = pack_evidence_graph_chunks(
        index,
        target_chars=config.resource_chunk_target_chars,
        max_chars=config.resource_chunk_max_chars,
    )
    write_json(output_dir / "context_evidence_index.json", index)
    write_json(output_dir / "chunks_manifest.json", slim_chunks_manifest(packed))
    for chunk in packed["chunks"]:
        chunk_id = str(chunk["chunk_id"])
        for mode, rendered_dir in rendered_dirs.items():
            rendered = render_chunk_evidence(chunk, mode=mode)  # type: ignore[arg-type]
            (rendered_dir / f"{chunk_id}.txt").write_text(rendered, encoding="utf-8")

    chunk_count = len(packed["chunks"])
    paths = {
        "context_evidence_index": str(output_dir / "context_evidence_index.json"),
        "chunks_manifest": str(output_dir / "chunks_manifest.json"),
        "rendered": {
            "variant_3": _rendered_chunk_paths(output_dir, "variant_3", chunk_count),
            "variant_4": _rendered_chunk_paths(output_dir, "variant_4", chunk_count),
            "v2_critic": _rendered_chunk_paths(output_dir, "v2_critic", chunk_count),
        },
        "chunk_count": chunk_count,
        "target_chars": config.resource_chunk_target_chars,
        "max_chars": config.resource_chunk_max_chars,
    }
    log(
        f"[reproduction_pipeline] context-window evidence graph chunks page={page.page} "
        f"chunks={chunk_count} -> {output_dir}",
        "cyan",
    )
    return paths


def _naive_prompt_variant_specs() -> dict[str, str]:
    return {
        "3": "variant_3",
        "4": "variant_4",
    }


def _emit_naive_rendered_prompt_chunk_artifacts(
    *,
    page: CapturedPage,
    config: ReproductionConfig,
    page_record: dict[str, Any],
) -> dict[str, Any] | None:
    variant_outputs = page_record.get("variant_outputs") or {}
    if not isinstance(variant_outputs, dict):
        return None
    output_dir = page.input_dir / "context_window" / "naive_rendered_prompt"
    emitted: dict[str, Any] = {}
    for variant_key, mode_dir in _naive_prompt_variant_specs().items():
        output = variant_outputs.get(variant_key) or {}
        if not isinstance(output, dict):
            continue
        prompt_path = output.get("prompt_user")
        if not isinstance(prompt_path, str):
            continue
        source_path = Path(prompt_path)
        if not source_path.is_file():
            continue
        text = source_path.read_text(encoding="utf-8")
        packed = pack_rendered_prompt_chunks(
            text,
            target_chars=config.resource_chunk_target_chars,
            max_chars=config.resource_chunk_max_chars,
        )
        variant_dir = output_dir / mode_dir
        variant_dir.mkdir(parents=True, exist_ok=True)
        for chunk in packed["chunks"]:
            chunk_id = str(chunk["chunk_id"])
            (variant_dir / f"{chunk_id}.txt").write_text(
                str(chunk["text"]),
                encoding="utf-8",
            )
        manifest_path = variant_dir / "chunks_manifest.json"
        write_json(manifest_path, slim_rendered_prompt_chunks_manifest(packed))
        emitted[mode_dir] = {
            "chunks_manifest": str(manifest_path),
            "chunk_count": len(packed["chunks"]),
            "source_prompt": str(source_path),
            "chunks": _direct_chunk_paths(output_dir, mode_dir, len(packed["chunks"])),
        }
    if not emitted:
        log(
            "[reproduction_pipeline] naive rendered-prompt chunks skipped: "
            "variant 3/4 prompt_user artifacts are unavailable",
            "yellow",
        )
        return None
    log(
        f"[reproduction_pipeline] naive rendered-prompt chunks page={page.page} "
        f"variants={sorted(emitted)} -> {output_dir}",
        "cyan",
    )
    return {
        "base_dir": str(output_dir),
        "target_chars": config.resource_chunk_target_chars,
        "max_chars": config.resource_chunk_max_chars,
        "variants": emitted,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except Exception:
        return {}


def _should_record_reused_step1(config: ReproductionConfig) -> bool:
    if config.reuse_v2_from is None:
        return True
    return config.output_dir.resolve() != config.reuse_v2_from.resolve()


def _should_record_reused_v2chunked_step1(config: ReproductionConfig) -> bool:
    if config.reuse_v2chunked_from is None:
        return True
    return config.output_dir.resolve() != config.reuse_v2chunked_from.resolve()


def _manifest_variants(config: ReproductionConfig) -> list[VariantKey]:
    variants = list(config.variants)
    if config.reuse_v2_from is not None and 2 not in variants:
        variants.append(2)
    if (
        config.enable_v2_critic
        and (2 in config.variants or config.reuse_v2_from is not None)
        and V2_CRITIC_VARIANT_KEY not in variants
    ):
        variants.append(V2_CRITIC_VARIANT_KEY)
    if (
        config.enable_v2_critic_no_analysis
        and (2 in config.variants or config.reuse_v2_from is not None)
        and V2_CRITIC_NO_ANALYSIS_VARIANT_KEY not in variants
    ):
        variants.append(V2_CRITIC_NO_ANALYSIS_VARIANT_KEY)
    if (
        config.enable_v2_critic_no_analysis
        and any(is_v2_chunked(variant) for variant in config.variants)
        and V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY not in variants
    ):
        variants.append(V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY)
    if (
        config.enable_v2_critic
        and any(is_v2_chunked(variant) for variant in config.variants)
        and V2_CHUNKED_CRITIC_VARIANT_KEY not in variants
    ):
        variants.append(V2_CHUNKED_CRITIC_VARIANT_KEY)
    if (
        config.enable_v2chunked_critic_chunked
        and any(is_v2_chunked(variant) for variant in config.variants)
        and V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY not in variants
    ):
        variants.append(V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY)
    return variants


def _merge_manifest_variants(
    existing: list[Any],
    current: list[VariantKey],
) -> list[Any]:
    variants = list(existing)
    existing_keys = {str(variant) for variant in variants}
    for variant in current:
        if str(variant) not in existing_keys:
            variants.append(variant)
            existing_keys.add(str(variant))
    return variants


def _merge_page_manifest_entry(
    existing: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    if existing is None:
        return current
    merged = {**existing, **current}
    existing_outputs = existing.get("variant_outputs")
    current_outputs = current.get("variant_outputs")
    if isinstance(existing_outputs, dict) and isinstance(current_outputs, dict):
        merged["variant_outputs"] = {**existing_outputs, **current_outputs}
    return merged


def _write_manifest(
    *,
    config: ReproductionConfig,
    static_inputs: StaticSemanticsInputs,
    pages: list[dict[str, Any]],
) -> None:
    manifest_path = config.output_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    manifest.setdefault("run_id", config.run_id)
    current_variants = _manifest_variants(config)
    recorded_at = iso_now()
    manifest["variants"] = _merge_manifest_variants(
        manifest.get("variants", []),
        current_variants,
    )
    manifest.setdefault("created_at", recorded_at)
    manifest["model"] = config.model
    manifest["lane"] = "reproduction"
    manifest["prepare_only"] = config.prepare_only
    manifest["api_called"] = not config.prepare_only
    manifest["v2_critic_enabled"] = config.enable_v2_critic
    manifest["v2_critic_no_analysis_enabled"] = config.enable_v2_critic_no_analysis
    manifest["v2chunked_critic_chunked_enabled"] = (
        config.enable_v2chunked_critic_chunked
    )
    manifest["reuse_v2_from"] = (
        str(config.reuse_v2_from) if config.reuse_v2_from else None
    )
    manifest["reuse_v2chunked_from"] = (
        str(config.reuse_v2chunked_from)
        if config.reuse_v2chunked_from
        else None
    )
    manifest["v2_critic_step1_source"] = (
        "reused_v2" if config.reuse_v2_from else "integrated"
    ) if config.enable_v2_critic else None
    manifest["v2_critic_no_analysis_step1_source"] = (
        "reused_v2" if config.reuse_v2_from else "integrated"
    ) if config.enable_v2_critic_no_analysis else None
    manifest["v2_chunked_critic_no_analysis_step1_source"] = (
        "reused_v2chunked" if config.reuse_v2chunked_from else "integrated"
    ) if (
        config.enable_v2_critic_no_analysis
        and any(is_v2_chunked(variant) for variant in config.variants)
    ) else None
    manifest["v2_chunked_critic_chunked_step1_source"] = (
        "reused_v2chunked" if config.reuse_v2chunked_from else "integrated"
    ) if (
        config.enable_v2chunked_critic_chunked
        and any(is_v2_chunked(variant) for variant in config.variants)
    ) else None
    manifest["prompt_cache_key"] = config.prompt_cache_key
    manifest["page_range"] = config.page_range
    manifest["v2_chunk_target_chars"] = config.v2_chunk_target_chars
    manifest["v2_chunk_max_chars"] = config.v2_chunk_max_chars
    manifest["resource_evidence_chunks_enabled"] = config.emit_resource_evidence_chunks
    manifest["resource_chunk_target_chars"] = config.resource_chunk_target_chars
    manifest["resource_chunk_max_chars"] = config.resource_chunk_max_chars
    manifest["context_window_domain_attribution"] = (
        config.context_window_domain_attribution
    )
    manifest["naive_rendered_prompt_chunks_enabled"] = (
        config.emit_naive_rendered_prompt_chunks
    )
    manifest["responses_compact_threshold"] = config.responses_compact_threshold
    existing_strategies = manifest.get("variant_prompt_strategies", {})
    if not isinstance(existing_strategies, dict):
        existing_strategies = {}
    current_strategies = {
        str(variant): _prompt_strategy(variant)
        for variant in current_variants
    }
    manifest["variant_prompt_strategies"] = {
        **existing_strategies,
        **current_strategies,
    }
    manifest["app_source_root"] = str(config.app_source_root)
    manifest["static_semantics_run_dir"] = (
        str(static_inputs.run_dir) if static_inputs.run_dir else None
    )
    manifest["context_slicer_dir"] = (
        str(static_inputs.context_slicer_dir) if static_inputs.context_slicer_dir else None
    )
    manifest["method_cfg_index_path"] = (
        str(static_inputs.method_cfg_index_path)
        if static_inputs.method_cfg_index_path
        else None
    )
    invocation = _manifest_invocation_entry(
        config=config,
        static_inputs=static_inputs,
        pages=pages,
        variants=current_variants,
        prompt_strategies=current_strategies,
        recorded_at=recorded_at,
    )
    invocations = manifest.get("invocations", [])
    if not isinstance(invocations, list):
        invocations = []
    invocations.append(invocation)
    manifest["invocations"] = invocations
    manifest["latest_invocation_index"] = len(invocations) - 1
    pages_by_num = {
        int(page["page"]): page
        for page in manifest.get("pages", [])
        if isinstance(page, dict) and "page" in page
    }
    for page in pages:
        page_num = int(page["page"])
        pages_by_num[page_num] = _merge_page_manifest_entry(
            pages_by_num.get(page_num),
            page,
        )
    merged_pages = [pages_by_num[key] for key in sorted(pages_by_num)]
    manifest["pages"] = merged_pages
    if merged_pages:
        latest = merged_pages[-1]
        manifest["variant_outputs"] = latest["variant_outputs"]
        manifest["input_hashes"] = latest["input_hashes"]
        manifest["input_files"] = latest["input_files"]
    write_json(manifest_path, manifest)


def _manifest_invocation_entry(
    *,
    config: ReproductionConfig,
    static_inputs: StaticSemanticsInputs,
    pages: list[dict[str, Any]],
    variants: list[VariantKey],
    prompt_strategies: dict[str, str],
    recorded_at: str,
) -> dict[str, Any]:
    """Return one append-only record for this reproduction pipeline invocation."""
    return {
        "recorded_at": recorded_at,
        "run_id": config.run_id,
        "variants": list(variants),
        "model": config.model,
        "lane": "reproduction",
        "prepare_only": config.prepare_only,
        "api_called": not config.prepare_only,
        "page_range": config.page_range,
        "selected_pages": [
            int(page["page"])
            for page in pages
            if isinstance(page, dict) and "page" in page
        ],
        "prompt_cache_key": config.prompt_cache_key,
        "variant_prompt_strategies": dict(prompt_strategies),
        "v2_critic_enabled": config.enable_v2_critic,
        "v2_critic_no_analysis_enabled": config.enable_v2_critic_no_analysis,
        "v2chunked_critic_chunked_enabled": (
            config.enable_v2chunked_critic_chunked
        ),
        "reuse_v2_from": (
            str(config.reuse_v2_from) if config.reuse_v2_from else None
        ),
        "reuse_v2chunked_from": (
            str(config.reuse_v2chunked_from)
            if config.reuse_v2chunked_from
            else None
        ),
        "v2_chunk_target_chars": config.v2_chunk_target_chars,
        "v2_chunk_max_chars": config.v2_chunk_max_chars,
        "resource_evidence_chunks_enabled": config.emit_resource_evidence_chunks,
        "resource_chunk_target_chars": config.resource_chunk_target_chars,
        "resource_chunk_max_chars": config.resource_chunk_max_chars,
        "context_window_domain_attribution": config.context_window_domain_attribution,
        "naive_rendered_prompt_chunks_enabled": (
            config.emit_naive_rendered_prompt_chunks
        ),
        "responses_compact_threshold": config.responses_compact_threshold,
        "app_source_root": str(config.app_source_root),
        "static_semantics_run_dir": (
            str(static_inputs.run_dir) if static_inputs.run_dir else None
        ),
        "context_slicer_dir": (
            str(static_inputs.context_slicer_dir)
            if static_inputs.context_slicer_dir
            else None
        ),
        "method_cfg_index_path": (
            str(static_inputs.method_cfg_index_path)
            if static_inputs.method_cfg_index_path
            else None
        ),
        "pages": pages,
    }


def _save_v2_chunked_outputs(
    *,
    variant_dir: Path,
    result: LLMResult,
    run_id: str,
    page: int,
    prompt_cache_key: str | None,
    chunk_count: int,
    candidate_count: int,
    deduped_variable_count: int,
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        variant_dir / "response_parsed.json",
        {
            "Analysis": result.response.Analysis,
            "State_Definitions": _result_predicates(result),
        },
    )
    if result.raw_json:
        (variant_dir / "response_raw.txt").write_text(result.raw_json, encoding="utf-8")
    usage = _usage_dict(result)
    usage["map_calls"] = chunk_count
    write_json(variant_dir / "usage.json", usage)
    write_json(
        variant_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": V2_CHUNKED_VARIANT_KEY,
            "base_variant": V2_CHUNKED_BASE_VARIANT,
            "page": page,
            "model": result.model,
            "lane": "reproduction",
            "prompt_strategy": V2_CHUNKED_PROMPT_STRATEGY,
            "prompt_cache_key": prompt_cache_key,
            "latency_sec": round(result.latency_sec, 3),
            "predicate_count": len(result.response.State_Definitions),
            "chunk_count": chunk_count,
            "candidate_count": candidate_count,
            "deduped_variable_count": deduped_variable_count,
            "target_chunk_chars": target_chunk_chars,
            "max_chunk_chars": max_chunk_chars,
            "request": result.request_meta or None,
        },
    )
    return {
        "response_parsed": str(variant_dir / "response_parsed.json"),
        "response_raw": str(variant_dir / "response_raw.txt"),
        "usage": str(variant_dir / "usage.json"),
        "run_meta": str(variant_dir / "run_meta.json"),
        "chunks_manifest": str(variant_dir / "chunks_manifest.json"),
        "chunk_inputs": str(variant_dir / "chunk_inputs"),
        "chunk_prompts": str(variant_dir / "chunk_prompts"),
        "chunk_outputs": str(variant_dir / "chunk_outputs"),
        "dedupe_meta": str(variant_dir / "dedupe_meta.json"),
        "latency_sec": round(result.latency_sec, 3),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "cached_tokens": result.cached_tokens,
        "cache_hit_rate": usage["cache_hit_rate"],
        "prompt_strategy": V2_CHUNKED_PROMPT_STRATEGY,
        "predicate_count": len(result.response.State_Definitions),
        "chunk_count": chunk_count,
        "candidate_count": candidate_count,
        "deduped_variable_count": deduped_variable_count,
    }


def _save_prepared_v2_chunked_outputs(
    *,
    variant_dir: Path,
    run_id: str,
    page: int,
    model: str,
    prompt_cache_key: str | None,
    chunk_count: int,
    source_file_count: int,
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    run_meta_path = variant_dir / "run_meta.json"
    write_json(
        run_meta_path,
        {
            "run_id": run_id,
            "variant": V2_CHUNKED_VARIANT_KEY,
            "base_variant": V2_CHUNKED_BASE_VARIANT,
            "page": page,
            "model": model,
            "lane": "reproduction",
            "status": "prepared",
            "prepare_only": True,
            "prompt_strategy": V2_CHUNKED_PROMPT_STRATEGY,
            "prompt_cache_key": prompt_cache_key,
            "api_called": False,
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "chunk_count": chunk_count,
            "source_file_count": source_file_count,
            "target_chunk_chars": target_chunk_chars,
            "max_chunk_chars": max_chunk_chars,
            "prepared_at": iso_now(),
        },
    )
    return {
        "response_parsed": None,
        "response_raw": None,
        "usage": None,
        "run_meta": str(run_meta_path),
        "chunks_manifest": str(variant_dir / "chunks_manifest.json"),
        "chunk_inputs": str(variant_dir / "chunk_inputs"),
        "chunk_prompts": str(variant_dir / "chunk_prompts"),
        "chunk_outputs": None,
        "dedupe_meta": None,
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "prompt_strategy": V2_CHUNKED_PROMPT_STRATEGY,
        "predicate_count": None,
        "chunk_count": chunk_count,
        "source_file_count": source_file_count,
    }


def _save_v2_compacted_outputs(
    *,
    variant_dir: Path,
    result: LLMResult,
    run_id: str,
    page: int,
    prompt_cache_key: str | None,
    chunk_count: int,
    source_file_count: int,
    compaction_call_count: int,
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        variant_dir / "response_parsed.json",
        {
            "Analysis": result.response.Analysis,
            "State_Definitions": _result_predicates(result),
        },
    )
    if result.raw_json:
        (variant_dir / "response_raw.txt").write_text(result.raw_json, encoding="utf-8")
    usage = _usage_dict(result)
    usage["compaction_calls"] = compaction_call_count
    write_json(variant_dir / "usage.json", usage)
    write_json(
        variant_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": V2_COMPACTED_VARIANT_KEY,
            "base_variant": V2_COMPACTED_BASE_VARIANT,
            "page": page,
            "model": result.model,
            "lane": "reproduction",
            "prompt_strategy": V2_COMPACTED_PROMPT_STRATEGY,
            "prompt_cache_key": prompt_cache_key,
            "latency_sec": round(result.latency_sec, 3),
            "predicate_count": len(result.response.State_Definitions),
            "chunk_count": chunk_count,
            "source_file_count": source_file_count,
            "compaction_call_count": compaction_call_count,
            "target_chunk_chars": target_chunk_chars,
            "max_chunk_chars": max_chunk_chars,
            "request": result.request_meta or None,
        },
    )
    return {
        "prompt_system": str(variant_dir / "prompt_system.txt"),
        "prompt_user": str(variant_dir / "prompt_user.txt"),
        "response_parsed": str(variant_dir / "response_parsed.json"),
        "response_raw": str(variant_dir / "response_raw.txt"),
        "usage": str(variant_dir / "usage.json"),
        "run_meta": str(variant_dir / "run_meta.json"),
        "chunks_manifest": str(variant_dir / "chunks_manifest.json"),
        "chunk_inputs": str(variant_dir / "chunk_inputs"),
        "compaction_steps": str(variant_dir / "compaction_steps"),
        "compaction_meta": str(variant_dir / "compaction_meta.json"),
        "final_response": str(variant_dir / "final_response.json"),
        "latency_sec": round(result.latency_sec, 3),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "cached_tokens": result.cached_tokens,
        "cache_hit_rate": usage["cache_hit_rate"],
        "prompt_strategy": V2_COMPACTED_PROMPT_STRATEGY,
        "predicate_count": len(result.response.State_Definitions),
        "chunk_count": chunk_count,
        "source_file_count": source_file_count,
        "compaction_call_count": compaction_call_count,
    }


def _save_prepared_v2_compacted_outputs(
    *,
    variant_dir: Path,
    run_id: str,
    page: int,
    model: str,
    prompt_cache_key: str | None,
    chunk_count: int,
    source_file_count: int,
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    run_meta_path = variant_dir / "run_meta.json"
    write_json(
        run_meta_path,
        {
            "run_id": run_id,
            "variant": V2_COMPACTED_VARIANT_KEY,
            "base_variant": V2_COMPACTED_BASE_VARIANT,
            "page": page,
            "model": model,
            "lane": "reproduction",
            "status": "prepared",
            "prepare_only": True,
            "prompt_strategy": V2_COMPACTED_PROMPT_STRATEGY,
            "prompt_cache_key": prompt_cache_key,
            "api_called": False,
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "chunk_count": chunk_count,
            "source_file_count": source_file_count,
            "target_chunk_chars": target_chunk_chars,
            "max_chunk_chars": max_chunk_chars,
            "prepared_at": iso_now(),
        },
    )
    return {
        "prompt_system": str(variant_dir / "prompt_system.txt"),
        "prompt_user": str(variant_dir / "prompt_user.txt"),
        "response_parsed": None,
        "response_raw": None,
        "usage": None,
        "run_meta": str(run_meta_path),
        "chunks_manifest": str(variant_dir / "chunks_manifest.json"),
        "chunk_inputs": str(variant_dir / "chunk_inputs"),
        "compaction_steps": None,
        "compaction_meta": None,
        "final_response": None,
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "prompt_strategy": V2_COMPACTED_PROMPT_STRATEGY,
        "predicate_count": None,
        "chunk_count": chunk_count,
        "source_file_count": source_file_count,
    }


def _v2_compacted_app_source_dir(config: ReproductionConfig) -> Path:
    return config.output_dir / "source_compaction" / V2_COMPACTED_APP_VARIANT_DIR


def _v2_compacted_parallel_source_dir(config: ReproductionConfig) -> Path:
    return config.output_dir / "source_compaction" / V2_COMPACTED_PARALLEL_VARIANT_DIR


def _ensure_v2_compacted_app_source_context(config: ReproductionConfig):
    source_dir = _v2_compacted_app_source_dir(config)
    if config.prepare_only:
        if not has_v2_app_source_compaction(source_dir):
            return prepare_v2_app_source_compaction(
                output_dir=source_dir,
                app_source_root=config.app_source_root,
                target_chunk_chars=config.v2_chunk_target_chars,
                max_chunk_chars=config.v2_chunk_max_chars,
            )
        return load_v2_app_source_compaction(source_dir)
    if has_v2_app_source_compaction(source_dir):
        loaded = load_v2_app_source_compaction(source_dir)
        if loaded.window and loaded.compaction_meta.get("status") == "success":
            return loaded
    return run_v2_app_source_compaction(
        output_dir=source_dir,
        app_source_root=config.app_source_root,
        model=config.model,
        timeout=config.timeout,
        prompt_cache_key=config.prompt_cache_key,
        target_chunk_chars=config.v2_chunk_target_chars,
        max_chunk_chars=config.v2_chunk_max_chars,
    )


def _ensure_v2_compacted_parallel_source_context(config: ReproductionConfig):
    source_dir = _v2_compacted_parallel_source_dir(config)
    if config.prepare_only:
        if not has_v2_app_source_compaction(source_dir):
            return prepare_v2_parallel_source_compaction(
                output_dir=source_dir,
                app_source_root=config.app_source_root,
                target_chunk_chars=config.v2_chunk_target_chars,
                max_chunk_chars=config.v2_chunk_max_chars,
            )
        return load_v2_app_source_compaction(source_dir)
    if has_v2_app_source_compaction(source_dir):
        loaded = load_v2_app_source_compaction(source_dir)
        if loaded.window and loaded.compaction_meta.get("status") == "success":
            return loaded
    return run_v2_parallel_source_compaction(
        output_dir=source_dir,
        app_source_root=config.app_source_root,
        model=config.model,
        timeout=config.timeout,
        prompt_cache_key=config.prompt_cache_key,
        target_chunk_chars=config.v2_chunk_target_chars,
        max_chunk_chars=config.v2_chunk_max_chars,
    )


def _save_v2_compacted_app_outputs(
    *,
    variant_dir: Path,
    result: LLMResult,
    run_id: str,
    page: int,
    prompt_cache_key: str | None,
    source_compaction_dir: Path,
    source_compaction_meta: dict[str, Any],
    variant_key: str = V2_COMPACTED_APP_VARIANT_KEY,
    base_variant: int = V2_COMPACTED_APP_BASE_VARIANT,
    prompt_strategy: str = V2_COMPACTED_APP_PROMPT_STRATEGY,
    source_context_mode: str = "app_level_standalone_compaction_reuse",
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        variant_dir / "response_parsed.json",
        {
            "Analysis": result.response.Analysis,
            "State_Definitions": _result_predicates(result),
        },
    )
    if result.raw_json:
        (variant_dir / "response_raw.txt").write_text(result.raw_json, encoding="utf-8")
    usage = _usage_dict(result)
    write_json(variant_dir / "usage.json", usage)
    write_json(
        variant_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": base_variant,
            "page": page,
            "model": result.model,
            "lane": "reproduction",
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "latency_sec": round(result.latency_sec, 3),
            "predicate_count": len(result.response.State_Definitions),
            "source_context_mode": source_context_mode,
            "session_mode": "page_local_reused_source_context",
            "auto_compaction_enabled": False,
            "source_compaction_dir": str(source_compaction_dir),
            "source_compaction": {
                "status": source_compaction_meta.get("status"),
                "prompt_strategy": source_compaction_meta.get("prompt_strategy"),
                "merge_strategy": source_compaction_meta.get("merge_strategy"),
                "chunk_count": source_compaction_meta.get("chunk_count"),
                "source_file_count": source_compaction_meta.get("source_file_count"),
                "compaction_call_count": source_compaction_meta.get("compaction_call_count"),
                "final_window_item_count": source_compaction_meta.get("final_window_item_count"),
            },
            "request": result.request_meta or None,
        },
    )
    return {
        "prompt_system": str(variant_dir / "prompt_system.txt"),
        "prompt_user": str(variant_dir / "prompt_user.txt"),
        "response_parsed": str(variant_dir / "response_parsed.json"),
        "response_raw": str(variant_dir / "response_raw.txt"),
        "usage": str(variant_dir / "usage.json"),
        "run_meta": str(variant_dir / "run_meta.json"),
        "final_response": str(variant_dir / "final_response.json"),
        "source_compaction_dir": str(source_compaction_dir),
        "latency_sec": round(result.latency_sec, 3),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "cached_tokens": result.cached_tokens,
        "cache_hit_rate": usage["cache_hit_rate"],
        "prompt_strategy": prompt_strategy,
        "predicate_count": len(result.response.State_Definitions),
    }


def _save_prepared_v2_compacted_app_outputs(
    *,
    variant_dir: Path,
    run_id: str,
    page: int,
    model: str,
    prompt_cache_key: str | None,
    source_compaction_dir: Path,
    variant_key: str = V2_COMPACTED_APP_VARIANT_KEY,
    base_variant: int = V2_COMPACTED_APP_BASE_VARIANT,
    prompt_strategy: str = V2_COMPACTED_APP_PROMPT_STRATEGY,
    source_context_mode: str = "app_level_standalone_compaction_reuse",
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    run_meta_path = variant_dir / "run_meta.json"
    write_json(
        run_meta_path,
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": base_variant,
            "page": page,
            "model": model,
            "lane": "reproduction",
            "status": "prepared",
            "prepare_only": True,
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "api_called": False,
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "source_context_mode": source_context_mode,
            "source_compaction_dir": str(source_compaction_dir),
            "prepared_at": iso_now(),
        },
    )
    return {
        "prompt_system": str(variant_dir / "prompt_system.txt"),
        "prompt_user": str(variant_dir / "prompt_user.txt"),
        "response_parsed": None,
        "response_raw": None,
        "usage": None,
        "run_meta": str(run_meta_path),
        "final_response": None,
        "source_compaction_dir": str(source_compaction_dir),
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "prompt_strategy": prompt_strategy,
        "predicate_count": None,
    }


def _run_v2_compacted_app_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
) -> VariantRunOutput:
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_COMPACTED_APP_VARIANT_KEY,
        page.page,
    )
    source_dir = _v2_compacted_app_source_dir(config)
    variant_page_dir = config.output_dir / V2_COMPACTED_APP_VARIANT_DIR / f"page_{page.page}"
    if config.prepare_only:
        _ensure_v2_compacted_app_source_context(config)
        prepare_v2_compacted_app_generation(
            output_dir=variant_page_dir,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            existing_predicates=accumulated,
        )
        paths = _save_prepared_v2_compacted_app_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            source_compaction_dir=source_dir,
        )
        log(
            f"[reproduction_pipeline] variant {V2_COMPACTED_APP_VARIANT_KEY} "
            f"page={page.page} prepared",
            "cyan",
        )
        return VariantRunOutput(paths=paths)

    try:
        source_context = _ensure_v2_compacted_app_source_context(config)
        compacted = run_v2_compacted_app_generation(
            output_dir=variant_page_dir,
            source_window=source_context.window,
            source_compaction_meta=source_context.compaction_meta,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            screenshot_path=page.screenshot_path,
            existing_predicates=accumulated,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
        )
    except Exception as exc:
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_COMPACTED_APP_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_COMPACTED_APP_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_compacted_app_generation",
            extra_meta={
                "source_compaction_dir": str(source_dir),
                "target_chunk_chars": config.v2_chunk_target_chars,
                "max_chunk_chars": config.v2_chunk_max_chars,
            },
        )
        raise
    result = compacted.final_result
    paths = _save_v2_compacted_app_outputs(
        variant_dir=variant_page_dir,
        result=result,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        source_compaction_dir=source_dir,
        source_compaction_meta=source_context.compaction_meta,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(result))
    _save_accumulated_predicates(config.output_dir, V2_COMPACTED_APP_VARIANT_KEY, merged)
    log(
        f"[reproduction_pipeline] variant {V2_COMPACTED_APP_VARIANT_KEY} "
        f"page={page.page} defs={len(result.response.State_Definitions)} "
        f"accumulated={len(merged)}",
        "green",
    )
    return VariantRunOutput(paths=paths, result=result)


def _run_v2_compacted_parallel_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
) -> VariantRunOutput:
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_COMPACTED_PARALLEL_VARIANT_KEY,
        page.page,
    )
    source_dir = _v2_compacted_parallel_source_dir(config)
    variant_page_dir = (
        config.output_dir / V2_COMPACTED_PARALLEL_VARIANT_DIR / f"page_{page.page}"
    )
    source_context_mode = "app_level_parallel_compacted_chunks_reuse"
    if config.prepare_only:
        _ensure_v2_compacted_parallel_source_context(config)
        prepare_v2_compacted_app_generation(
            output_dir=variant_page_dir,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            existing_predicates=accumulated,
        )
        paths = _save_prepared_v2_compacted_app_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            source_compaction_dir=source_dir,
            variant_key=V2_COMPACTED_PARALLEL_VARIANT_KEY,
            base_variant=V2_COMPACTED_PARALLEL_BASE_VARIANT,
            prompt_strategy=V2_COMPACTED_PARALLEL_PROMPT_STRATEGY,
            source_context_mode=source_context_mode,
        )
        log(
            f"[reproduction_pipeline] variant {V2_COMPACTED_PARALLEL_VARIANT_KEY} "
            f"page={page.page} prepared",
            "cyan",
        )
        return VariantRunOutput(paths=paths)

    try:
        source_context = _ensure_v2_compacted_parallel_source_context(config)
        compacted = run_v2_compacted_parallel_generation(
            output_dir=variant_page_dir,
            source_window=source_context.window,
            source_compaction_meta=source_context.compaction_meta,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            screenshot_path=page.screenshot_path,
            existing_predicates=accumulated,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
        )
    except Exception as exc:
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_COMPACTED_PARALLEL_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_COMPACTED_PARALLEL_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_compacted_parallel_generation",
            extra_meta={
                "source_compaction_dir": str(source_dir),
                "source_context_mode": source_context_mode,
                "merge_strategy": "parallel_independent_chunks",
                "target_chunk_chars": config.v2_chunk_target_chars,
                "max_chunk_chars": config.v2_chunk_max_chars,
            },
        )
        raise
    result = compacted.final_result
    paths = _save_v2_compacted_app_outputs(
        variant_dir=variant_page_dir,
        result=result,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        source_compaction_dir=source_dir,
        source_compaction_meta=source_context.compaction_meta,
        variant_key=V2_COMPACTED_PARALLEL_VARIANT_KEY,
        base_variant=V2_COMPACTED_PARALLEL_BASE_VARIANT,
        prompt_strategy=V2_COMPACTED_PARALLEL_PROMPT_STRATEGY,
        source_context_mode=source_context_mode,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(result))
    _save_accumulated_predicates(
        config.output_dir,
        V2_COMPACTED_PARALLEL_VARIANT_KEY,
        merged,
    )
    log(
        f"[reproduction_pipeline] variant {V2_COMPACTED_PARALLEL_VARIANT_KEY} "
        f"page={page.page} defs={len(result.response.State_Definitions)} "
        f"accumulated={len(merged)}",
        "green",
    )
    return VariantRunOutput(paths=paths, result=result)


def _run_v2_compacted_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
) -> VariantRunOutput:
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_COMPACTED_VARIANT_KEY,
        page.page,
    )
    variant_page_dir = config.output_dir / V2_COMPACTED_VARIANT_DIR / f"page_{page.page}"
    if config.prepare_only:
        prepared = prepare_v2_compacted_generation(
            output_dir=variant_page_dir,
            app_source_root=config.app_source_root,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            existing_predicates=accumulated,
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
        )
        paths = _save_prepared_v2_compacted_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            chunk_count=int(prepared.chunks_manifest["chunk_count"]),
            source_file_count=int(prepared.chunks_manifest["source_file_count"]),
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
        )
        log(
            f"[reproduction_pipeline] variant {V2_COMPACTED_VARIANT_KEY} "
            f"page={page.page} prepared chunks={prepared.chunks_manifest['chunk_count']}",
            "cyan",
        )
        return VariantRunOutput(paths=paths)

    try:
        compacted = run_v2_compacted_generation(
            output_dir=variant_page_dir,
            app_source_root=config.app_source_root,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            screenshot_path=page.screenshot_path,
            existing_predicates=accumulated,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
        )
    except Exception as exc:
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_COMPACTED_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_COMPACTED_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_compacted_generation",
            extra_meta={
                "target_chunk_chars": config.v2_chunk_target_chars,
                "max_chunk_chars": config.v2_chunk_max_chars,
            },
        )
        raise
    result = compacted.final_result
    paths = _save_v2_compacted_outputs(
        variant_dir=variant_page_dir,
        result=result,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        chunk_count=int(compacted.chunks_manifest["chunk_count"]),
        source_file_count=int(compacted.chunks_manifest["source_file_count"]),
        compaction_call_count=int(compacted.compaction_meta["compaction_call_count"]),
        target_chunk_chars=config.v2_chunk_target_chars,
        max_chunk_chars=config.v2_chunk_max_chars,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(result))
    _save_accumulated_predicates(config.output_dir, V2_COMPACTED_VARIANT_KEY, merged)
    log(
        f"[reproduction_pipeline] variant {V2_COMPACTED_VARIANT_KEY} page={page.page} "
        f"defs={len(result.response.State_Definitions)} accumulated={len(merged)}",
        "green",
    )
    return VariantRunOutput(paths=paths, result=result)


def _run_v2_chunked_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    query_fn: Callable[..., Any],
) -> VariantRunOutput:
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_CHUNKED_VARIANT_KEY,
        page.page,
    )
    prior_user_prompt = _build_v2_chunked_prior_user_prompt(
        config=config,
        page=page,
        existing_predicates=accumulated,
    )
    variant_page_dir = config.output_dir / V2_CHUNKED_VARIANT_DIR / f"page_{page.page}"
    if config.prepare_only:
        prepared = prepare_v2_chunked_generation(
            output_dir=variant_page_dir,
            app_source_root=config.app_source_root,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            existing_predicates=accumulated,
            prompt_cache_key=config.prompt_cache_key,
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
        )
        paths = _save_prepared_v2_chunked_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            chunk_count=int(prepared.chunks_manifest["chunk_count"]),
            source_file_count=int(prepared.chunks_manifest["source_file_count"]),
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
        )
        log(
            f"[reproduction_pipeline] variant {V2_CHUNKED_VARIANT_KEY} "
            f"page={page.page} prepared chunks={prepared.chunks_manifest['chunk_count']}",
            "cyan",
        )
        return VariantRunOutput(
            paths=paths,
            user_prompt=prior_user_prompt,
            source_page_dir=variant_page_dir,
            step1_source="integrated_v2chunked",
            step1_api_called=False,
        )

    chunk_query_fn = None if query_fn is query_llm else query_fn
    try:
        chunked = run_v2_chunked_generation(
            output_dir=variant_page_dir,
            app_source_root=config.app_source_root,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            screenshot_path=page.screenshot_path,
            existing_predicates=accumulated,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
            query_fn=chunk_query_fn,
        )
    except Exception as exc:
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_CHUNKED_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_CHUNKED_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_chunked_map_stage",
            user_prompt=prior_user_prompt,
            extra_meta={
                "target_chunk_chars": config.v2_chunk_target_chars,
                "max_chunk_chars": config.v2_chunk_max_chars,
            },
        )
        raise
    result = chunked.final_result
    paths = _save_v2_chunked_outputs(
        variant_dir=variant_page_dir,
        result=result,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        chunk_count=len(chunked.chunks_manifest["chunks"]),
        candidate_count=chunked.dedupe_meta["candidate_count"],
        deduped_variable_count=chunked.dedupe_meta["deduped_variable_count"],
        target_chunk_chars=config.v2_chunk_target_chars,
        max_chunk_chars=config.v2_chunk_max_chars,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(result))
    _save_accumulated_predicates(config.output_dir, V2_CHUNKED_VARIANT_KEY, merged)
    log(
        f"[reproduction_pipeline] variant {V2_CHUNKED_VARIANT_KEY} page={page.page} "
        f"defs={len(result.response.State_Definitions)} accumulated={len(merged)}",
        "green",
    )
    return VariantRunOutput(
        paths=paths,
        result=result,
        user_prompt=prior_user_prompt,
        source_page_dir=variant_page_dir,
        step1_source="integrated_v2chunked",
        step1_api_called=True,
    )


def _run_context_window_chunked_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    variant: VariantKey,
    context_window_ctx: dict[str, Any],
    query_fn: Callable[..., Any],
) -> VariantRunOutput:
    variant_key = str(variant)
    sliced_payload = context_window_ctx.get("sliced_methods_payload")
    static_payload = context_window_ctx.get("static_analysis_payload")
    if not isinstance(sliced_payload, dict) or not isinstance(static_payload, dict):
        raise ReproductionInputError(
            f"variant {variant_key} requires context-window sliced/static payloads"
        )

    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        variant,
        page.page,
    )
    base = base_variant(variant)
    prompt_strategy = _prompt_strategy(variant)
    variant_page_dir = config.output_dir / _variant_dir_name(variant) / f"page_{page.page}"
    if config.prepare_only:
        prepared = prepare_context_window_chunked_generation(
            output_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            variant_key=variant_key,
            base_variant=base,
            prompt_strategy=prompt_strategy,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            screenshot_path=page.screenshot_path,
            existing_predicates=accumulated,
            sliced_methods_payload=sliced_payload,
            static_analysis_payload=static_payload,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            domain_attribution_policy=config.context_window_domain_attribution,
            target_chars=config.resource_chunk_target_chars,
            max_chars=config.resource_chunk_max_chars,
        )
        log(
            f"[reproduction_pipeline] variant {variant_key} page={page.page} "
            f"prepared chunks={prepared.paths['chunk_count']}",
            "cyan",
        )
        return VariantRunOutput(
            paths=prepared.paths,
            system_prompt=(variant_page_dir / "prompt_system.txt").read_text(
                encoding="utf-8",
            ),
            user_prompt=(variant_page_dir / "prompt_user.txt").read_text(
                encoding="utf-8",
            ),
            source_page_dir=variant_page_dir,
            step1_source="context_window_chunked",
            step1_api_called=False,
        )

    try:
        chunked = run_context_window_chunked_generation(
            output_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            variant_key=variant_key,
            base_variant=base,
            prompt_strategy=prompt_strategy,
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            screenshot_path=page.screenshot_path,
            existing_predicates=accumulated,
            sliced_methods_payload=sliced_payload,
            static_analysis_payload=static_payload,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            domain_attribution_policy=config.context_window_domain_attribution,
            target_chars=config.resource_chunk_target_chars,
            max_chars=config.resource_chunk_max_chars,
            chunk_max_attempts=config.context_window_chunk_max_attempts,
            chunk_retry_base_delay=config.context_window_chunk_retry_base_delay,
            chunk_retry_max_delay=config.context_window_chunk_retry_max_delay,
            query_fn=query_fn,
        )
    except Exception as exc:
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=variant,
            page=page.page,
            model=config.model,
            prompt_strategy=prompt_strategy,
            prompt_cache_key=getattr(exc, "aifc_prompt_cache_key", config.prompt_cache_key),
            exc=exc,
            phase="context_window_chunked_generation",
            system_prompt=getattr(exc, "aifc_system_prompt", None),
            user_prompt=getattr(exc, "aifc_user_prompt", None),
            extra_meta={
                "chunk_id": getattr(exc, "aifc_chunk_id", None),
                "target_chars": config.resource_chunk_target_chars,
                "max_chars": config.resource_chunk_max_chars,
                "domain_attribution_policy": config.context_window_domain_attribution,
            },
        )
        raise

    result = chunked.final_result
    merged = merge_state_definitions(accumulated, _result_predicates(result))
    _save_accumulated_predicates(config.output_dir, variant, merged)
    log(
        f"[reproduction_pipeline] variant {variant_key} page={page.page} "
        f"defs={len(result.response.State_Definitions)} accumulated={len(merged)}",
        "green",
    )
    return VariantRunOutput(
        paths=chunked.paths,
        result=result,
        system_prompt=(variant_page_dir / "prompt_system.txt").read_text(
            encoding="utf-8",
        ),
        user_prompt=(variant_page_dir / "prompt_user.txt").read_text(
            encoding="utf-8",
        ),
        source_page_dir=variant_page_dir,
        step1_source="context_window_chunked",
        step1_api_called=True,
    )


def _v2_responses_multiturn_page_inputs(
    *,
    config: ReproductionConfig,
    pages: list[CapturedPage],
) -> list[V2ResponsesMultiturnPageInput]:
    return [
        V2ResponsesMultiturnPageInput(
            page=page.page,
            output_dir=(
                config.output_dir
                / V2_RESPONSES_MULTITURN_VARIANT_DIR
                / f"page_{page.page}"
            ),
            app_name=_page_app_name(config, page),
            a11y_xml=page.a11y_xml,
            screenshot_path=page.screenshot_path,
        )
        for page in pages
    ]


def _save_v2_responses_multiturn_page_outputs(
    *,
    result,
    run_id: str,
    model: str,
    prompt_cache_key: str | None,
    compact_threshold: int,
) -> dict[str, Any]:
    variant_dir = result.paths["prompt_system"].parent
    llm_result = result.final_result
    write_json(
        variant_dir / "response_parsed.json",
        {
            "Analysis": llm_result.response.Analysis,
            "State_Definitions": _result_predicates(llm_result),
        },
    )
    if llm_result.raw_json:
        (variant_dir / "response_raw.txt").write_text(
            llm_result.raw_json,
            encoding="utf-8",
        )
    usage = _usage_dict(llm_result)
    write_json(variant_dir / "usage.json", usage)
    write_json(
        variant_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": V2_RESPONSES_MULTITURN_VARIANT_KEY,
            "base_variant": V2_RESPONSES_MULTITURN_BASE_VARIANT,
            "page": result.page,
            "model": model,
            "lane": "reproduction",
            "prompt_strategy": V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
            "prompt_cache_key": prompt_cache_key,
            "latency_sec": round(llm_result.latency_sec, 3),
            "predicate_count": len(llm_result.response.State_Definitions),
            "source_context_mode": V2_RESPONSES_MULTITURN_SOURCE_CONTEXT_MODE,
            "session_mode": V2_RESPONSES_MULTITURN_SESSION_MODE,
            "auto_compaction_enabled": True,
            "compact_threshold": compact_threshold,
            "conversation_id": result.conversation_state.get("conversation_id"),
            "response_id": result.conversation_state.get("response_id"),
            "auto_compaction_item_count": result.conversation_state.get(
                "auto_compaction_item_count"
            ),
            "request": llm_result.request_meta or None,
        },
    )
    return {
        "prompt_system": str(variant_dir / "prompt_system.txt"),
        "prompt_user": str(variant_dir / "prompt_user.txt"),
        "response_parsed": str(variant_dir / "response_parsed.json"),
        "response_raw": str(variant_dir / "response_raw.txt"),
        "usage": str(variant_dir / "usage.json"),
        "run_meta": str(variant_dir / "run_meta.json"),
        "final_response": str(variant_dir / "final_response.json"),
        "conversation_state": str(variant_dir / "conversation_state.json"),
        "latency_sec": round(llm_result.latency_sec, 3),
        "prompt_tokens": llm_result.prompt_tokens,
        "completion_tokens": llm_result.completion_tokens,
        "reasoning_tokens": llm_result.reasoning_tokens,
        "cached_tokens": llm_result.cached_tokens,
        "cache_hit_rate": usage["cache_hit_rate"],
        "prompt_strategy": V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
        "predicate_count": len(llm_result.response.State_Definitions),
        "auto_compaction_item_count": result.conversation_state.get(
            "auto_compaction_item_count"
        ),
    }


def _save_prepared_v2_responses_multiturn_page_outputs(
    *,
    variant_dir: Path,
    run_id: str,
    page: int,
    model: str,
    prompt_cache_key: str | None,
    compact_threshold: int,
) -> dict[str, Any]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        variant_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": V2_RESPONSES_MULTITURN_VARIANT_KEY,
            "base_variant": V2_RESPONSES_MULTITURN_BASE_VARIANT,
            "page": page,
            "model": model,
            "lane": "reproduction",
            "status": "prepared",
            "prepare_only": True,
            "prompt_strategy": V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
            "prompt_cache_key": prompt_cache_key,
            "api_called": False,
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "source_context_mode": V2_RESPONSES_MULTITURN_SOURCE_CONTEXT_MODE,
            "session_mode": V2_RESPONSES_MULTITURN_SESSION_MODE,
            "auto_compaction_enabled": True,
            "compact_threshold": compact_threshold,
            "prepared_at": iso_now(),
        },
    )
    return {
        "prompt_system": str(variant_dir / "prompt_system.txt"),
        "prompt_user": str(variant_dir / "prompt_user.txt"),
        "response_parsed": None,
        "response_raw": None,
        "usage": None,
        "run_meta": str(variant_dir / "run_meta.json"),
        "final_response": None,
        "conversation_state": None,
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "prompt_strategy": V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
        "predicate_count": None,
    }


def _run_v2_responses_multiturn_lane(
    *,
    config: ReproductionConfig,
    pages: list[CapturedPage],
    page_records: list[dict[str, Any]],
) -> None:
    variant_dir = config.output_dir / V2_RESPONSES_MULTITURN_VARIANT_DIR
    page_inputs = _v2_responses_multiturn_page_inputs(config=config, pages=pages)
    page_records_by_num = {int(record["page"]): record for record in page_records}
    if config.prepare_only:
        prepared = prepare_v2_responses_multiturn_generation(
            output_dir=variant_dir,
            app_source_root=config.app_source_root,
            pages=page_inputs,
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
        )
        for page in prepared:
            page_records_by_num[page.page]["variant_outputs"][
                V2_RESPONSES_MULTITURN_VARIANT_KEY
            ] = _save_prepared_v2_responses_multiturn_page_outputs(
                variant_dir=page.paths["prompt_system"].parent,
                run_id=config.run_id,
                page=page.page,
                model=config.model,
                prompt_cache_key=config.prompt_cache_key,
                compact_threshold=config.responses_compact_threshold,
            )
        log(
            f"[reproduction_pipeline] variant {V2_RESPONSES_MULTITURN_VARIANT_KEY} "
            f"prepared pages={len(prepared)}",
            "cyan",
        )
        return

    saved_page_results = []

    def _record_page_result(page_result) -> None:
        saved_page_results.append(page_result)
        page_records_by_num[page_result.page]["variant_outputs"][
            V2_RESPONSES_MULTITURN_VARIANT_KEY
        ] = _save_v2_responses_multiturn_page_outputs(
            result=page_result,
            run_id=config.run_id,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            compact_threshold=config.responses_compact_threshold,
        )
        _save_accumulated_predicates(
            config.output_dir,
            V2_RESPONSES_MULTITURN_VARIANT_KEY,
            page_result.accumulated_predicates,
        )

    try:
        _ = run_v2_responses_multiturn_generation(
            output_dir=variant_dir,
            app_source_root=config.app_source_root,
            pages=page_inputs,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            compact_threshold=config.responses_compact_threshold,
            target_chunk_chars=config.v2_chunk_target_chars,
            max_chunk_chars=config.v2_chunk_max_chars,
            on_page_result=_record_page_result,
        )
    except Exception as exc:
        failed_page = int(getattr(exc, "aifc_page", pages[0].page if pages else 0))
        failed_dir = variant_dir / f"page_{failed_page}"
        _save_api_failure_outputs(
            variant_dir=failed_dir,
            run_id=config.run_id,
            variant=V2_RESPONSES_MULTITURN_VARIANT_KEY,
            page=failed_page,
            model=config.model,
            prompt_strategy=V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_responses_multiturn",
            system_prompt=getattr(exc, "aifc_system_prompt", None),
            user_prompt=getattr(exc, "aifc_user_prompt", None),
            extra_meta={
                "compact_threshold": config.responses_compact_threshold,
                "source_context_mode": V2_RESPONSES_MULTITURN_SOURCE_CONTEXT_MODE,
                "session_mode": V2_RESPONSES_MULTITURN_SESSION_MODE,
            },
        )
        raise

    log(
        f"[reproduction_pipeline] variant {V2_RESPONSES_MULTITURN_VARIANT_KEY} "
        f"pages={len(saved_page_results)}",
        "green",
    )


def _run_variant_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    variant: VariantKey,
    code_ctx: dict[str, Any],
    query_fn: Callable[..., Any] = query_llm,
) -> VariantRunOutput:
    if is_v2_responses_multiturn(variant):
        raise ReproductionInputError(
            "2responses_multiturn is a session-level lane and must not run "
            "through the page-local variant dispatcher"
        )
    if is_v2_compacted_parallel(variant):
        return _run_v2_compacted_parallel_for_page(
            config=config,
            page=page,
        )
    if is_v2_compacted_app(variant):
        return _run_v2_compacted_app_for_page(
            config=config,
            page=page,
        )
    if is_v2_compacted(variant):
        return _run_v2_compacted_for_page(
            config=config,
            page=page,
        )
    if is_v2_chunked(variant):
        return _run_v2_chunked_for_page(
            config=config,
            page=page,
            query_fn=query_fn,
        )
    if is_context_window_chunked(variant):
        return _run_context_window_chunked_for_page(
            config=config,
            page=page,
            variant=variant,
            context_window_ctx=code_ctx,
            query_fn=query_fn,
        )

    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        variant,
        page.page,
    )
    base_variant = int(variant)
    merged_ctx = merge_context(
        variant=base_variant,
        a11y_xml_text=page.a11y_xml,
        screenshot_path=page.screenshot_path,
        existing_predicates=accumulated,
        app_name=_page_app_name(config, page),
        raw_source_code=code_ctx.get("raw_source_code"),
        sliced_methods_payload=code_ctx.get("sliced_methods_payload"),
        static_analysis_payload=code_ctx.get("static_analysis_payload"),
    )
    prompt_strategy = _prompt_strategy(variant)
    system_prompt, user_prompt = _build_reproduction_prompt(
        variant=base_variant,
        context=merged_ctx,
    )
    variant_page_dir = config.output_dir / _variant_dir_name(variant) / f"page_{page.page}"
    if config.prepare_only:
        paths = _save_prepared_variant_outputs(
            variant_dir=variant_page_dir,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            run_id=config.run_id,
            page=page.page,
            variant=variant,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            prompt_strategy=prompt_strategy,
        )
        log(
            f"[reproduction_pipeline] variant {variant} page={page.page} prepared",
            "cyan",
        )
        return VariantRunOutput(
            paths=paths,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            source_page_dir=variant_page_dir,
            step1_source="integrated_v2" if variant == 2 else "generated",
            step1_api_called=False,
        )

    try:
        result = query_fn(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            screenshot_path=page.screenshot_path,
            variant=base_variant,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
        )
    except Exception as exc:
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=variant,
            page=page.page,
            model=config.model,
            prompt_strategy=prompt_strategy,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="predicate_generation",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        raise
    paths = _save_variant_outputs(
        variant_dir=variant_page_dir,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        result=result,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        prompt_strategy=prompt_strategy,
    )
    new_defs = _result_predicates(result)
    merged = merge_state_definitions(accumulated, new_defs)
    _save_accumulated_predicates(config.output_dir, variant, merged)
    log(
        f"[reproduction_pipeline] variant {variant} page={page.page} "
        f"defs={len(new_defs)} accumulated={len(merged)}",
        "green",
    )
    return VariantRunOutput(
        paths=paths,
        result=result,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        source_page_dir=variant_page_dir,
        step1_source="integrated_v2" if variant == 2 else "generated",
        step1_api_called=True,
    )


def _run_v2_critic_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    v2_output: VariantRunOutput,
    critic_ctx: dict[str, Any],
) -> dict[str, Any]:
    if v2_output.system_prompt is None or v2_output.user_prompt is None:
        raise ReproductionInputError("V2 critic requires the V2 prompts")

    analysis_payload = render_static_critic_evidence(
        sliced_methods_payload=critic_ctx.get("sliced_methods_payload") or {},
        static_analysis_payload=critic_ctx.get("static_analysis_payload") or {},
        critic_label="V2 Step 2 critic",
    )
    analysis_payload_path = page.input_dir / "analysis_payload.txt"
    analysis_payload_path.write_text(analysis_payload, encoding="utf-8")

    source_step1_page_dir = (
        v2_output.source_page_dir
        or config.output_dir / "variant_2" / f"page_{page.page}"
    )
    step1_source = (
        v2_output.step1_source
        if v2_output.step1_source != "generated"
        else "integrated_v2"
    )
    variant_page_dir = (
        config.output_dir / V2_CRITIC_VARIANT_DIR / f"page_{page.page}"
    )
    if config.prepare_only:
        critic_prompt = None
        if v2_output.result is not None:
            critic_prompt = build_critic_user_prompt(
                step1_predicates=v2_output.result.response.State_Definitions,
                analysis_payload=analysis_payload,
                prompts_dir=PYTHON_DIR / "prompts",
            )
        paths = _save_prepared_v2_critic_outputs(
            variant_dir=variant_page_dir,
            variant_key=V2_CRITIC_VARIANT_KEY,
            prompt_strategy=V2_CRITIC_PROMPT_STRATEGY,
            system_prompt=v2_output.system_prompt,
            user_prompt=v2_output.user_prompt,
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            input_dir=page.input_dir,
            analysis_payload_available=bool(analysis_payload.strip()),
            analysis_payload_path=analysis_payload_path,
            source_step1_page_dir=source_step1_page_dir,
            step1_result=v2_output.result,
            critic_prompt=critic_prompt,
            step1_source=step1_source,
            step1_api_called=v2_output.step1_api_called,
        )
        log(
            f"[reproduction_pipeline] variant {V2_CRITIC_VARIANT_KEY} "
            f"page={page.page} prepared",
            "cyan",
        )
        return paths

    if v2_output.result is None:
        raise ReproductionInputError("V2 critic requires a V2 Step 1 response")

    critic_session_messages = [
        {
            "role": "user",
            "content": build_user_content(v2_output.user_prompt, page.screenshot_path),
        },
        {"role": "assistant", "content": v2_output.result.raw_json},
    ]
    try:
        final_predicates, critic_result, critic_prompt = run_critic_turn(
            system_prompt=v2_output.system_prompt,
            session_messages=critic_session_messages,
            step1_predicates=v2_output.result.response.State_Definitions,
            analysis_payload=analysis_payload,
            prompts_dir=PYTHON_DIR / "prompts",
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            max_completion_tokens=DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
        )
    except Exception as exc:
        critic_prompt = getattr(exc, "aifc_critic_prompt", None)
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_CRITIC_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_CRITIC_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_critic_step2",
            system_prompt=v2_output.system_prompt,
            user_prompt=v2_output.user_prompt,
            critic_prompt=critic_prompt if isinstance(critic_prompt, str) else None,
            extra_meta={
                "source_step1_page_dir": str(source_step1_page_dir),
                "step1_source": step1_source,
                "step1_api_called": v2_output.step1_api_called,
                "analysis_payload": str(analysis_payload_path),
            },
        )
        raise
    final_result = _llm_result_with_predicates(
        v2_output.result,
        final_predicates,
        "Step 2 critic applied as drop-only monotone decrease.",
    )
    paths = _save_step2_critic_outputs(
        variant_dir=variant_page_dir,
        variant_key=V2_CRITIC_VARIANT_KEY,
        prompt_strategy=V2_CRITIC_PROMPT_STRATEGY,
        system_prompt=v2_output.system_prompt,
        user_prompt=v2_output.user_prompt,
        step1_result=v2_output.result,
        final_result=final_result,
        critic_result=critic_result,
        critic_prompt=critic_prompt,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        input_dir=page.input_dir,
        source_step1_page_dir=source_step1_page_dir,
        step1_source=step1_source,
        step1_api_called=v2_output.step1_api_called,
        critic_prior_turns=len(critic_session_messages),
        analysis_payload_path=analysis_payload_path,
    )
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_CRITIC_VARIANT_KEY,
        page.page,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(final_result))
    _save_accumulated_predicates(config.output_dir, V2_CRITIC_VARIANT_KEY, merged)
    log(
        f"[reproduction_pipeline] variant {V2_CRITIC_VARIANT_KEY} page={page.page} "
        f"defs={len(final_result.response.State_Definitions)} "
        f"accumulated={len(merged)}",
        "green",
    )
    return paths


def _run_v2_critic_no_analysis_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    v2_output: VariantRunOutput,
) -> dict[str, Any]:
    if v2_output.system_prompt is None or v2_output.user_prompt is None:
        raise ReproductionInputError("V2 no-analysis critic requires the V2 prompts")

    source_step1_page_dir = (
        v2_output.source_page_dir
        or config.output_dir / "variant_2" / f"page_{page.page}"
    )
    step1_source = (
        v2_output.step1_source
        if v2_output.step1_source != "generated"
        else "integrated_v2"
    )
    variant_page_dir = (
        config.output_dir / V2_CRITIC_NO_ANALYSIS_VARIANT_DIR / f"page_{page.page}"
    )
    if config.prepare_only:
        critic_prompt = None
        if v2_output.result is not None:
            critic_prompt = build_critic_user_prompt(
                step1_predicates=v2_output.result.response.State_Definitions,
                analysis_payload="",
                prompts_dir=PYTHON_DIR / "prompts",
                include_analysis_payload=False,
            )
        paths = _save_prepared_v2_critic_outputs(
            variant_dir=variant_page_dir,
            variant_key=V2_CRITIC_NO_ANALYSIS_VARIANT_KEY,
            prompt_strategy=V2_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY,
            system_prompt=v2_output.system_prompt,
            user_prompt=v2_output.user_prompt,
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            input_dir=page.input_dir,
            analysis_payload_available=False,
            analysis_payload_path=None,
            source_step1_page_dir=source_step1_page_dir,
            step1_result=v2_output.result,
            critic_prompt=critic_prompt,
            step1_source=step1_source,
            step1_api_called=v2_output.step1_api_called,
        )
        log(
            f"[reproduction_pipeline] variant {V2_CRITIC_NO_ANALYSIS_VARIANT_KEY} "
            f"page={page.page} prepared",
            "cyan",
        )
        return paths

    if v2_output.result is None:
        raise ReproductionInputError("V2 no-analysis critic requires a V2 Step 1 response")

    critic_session_messages = [
        {
            "role": "user",
            "content": build_user_content(v2_output.user_prompt, page.screenshot_path),
        },
        {"role": "assistant", "content": v2_output.result.raw_json},
    ]
    try:
        final_predicates, critic_result, critic_prompt = run_critic_turn(
            system_prompt=v2_output.system_prompt,
            session_messages=critic_session_messages,
            step1_predicates=v2_output.result.response.State_Definitions,
            analysis_payload="",
            prompts_dir=PYTHON_DIR / "prompts",
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            max_completion_tokens=DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
            include_analysis_payload=False,
        )
    except Exception as exc:
        critic_prompt = getattr(exc, "aifc_critic_prompt", None)
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_CRITIC_NO_ANALYSIS_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_critic_no_analysis_step2",
            system_prompt=v2_output.system_prompt,
            user_prompt=v2_output.user_prompt,
            critic_prompt=critic_prompt if isinstance(critic_prompt, str) else None,
            extra_meta={
                "source_step1_page_dir": str(source_step1_page_dir),
                "step1_source": step1_source,
                "step1_api_called": v2_output.step1_api_called,
                "analysis_payload_available": False,
            },
        )
        raise
    final_result = _llm_result_with_predicates(
        v2_output.result,
        final_predicates,
        "Step 2 no-analysis critic applied as drop-only monotone decrease.",
    )
    paths = _save_step2_critic_outputs(
        variant_dir=variant_page_dir,
        variant_key=V2_CRITIC_NO_ANALYSIS_VARIANT_KEY,
        prompt_strategy=V2_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY,
        system_prompt=v2_output.system_prompt,
        user_prompt=v2_output.user_prompt,
        step1_result=v2_output.result,
        final_result=final_result,
        critic_result=critic_result,
        critic_prompt=critic_prompt,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        input_dir=page.input_dir,
        source_step1_page_dir=source_step1_page_dir,
        step1_source=step1_source,
        step1_api_called=v2_output.step1_api_called,
        critic_prior_turns=len(critic_session_messages),
        analysis_payload_path=None,
    )
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_CRITIC_NO_ANALYSIS_VARIANT_KEY,
        page.page,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(final_result))
    _save_accumulated_predicates(
        config.output_dir,
        V2_CRITIC_NO_ANALYSIS_VARIANT_KEY,
        merged,
    )
    log(
        f"[reproduction_pipeline] variant {V2_CRITIC_NO_ANALYSIS_VARIANT_KEY} "
        f"page={page.page} defs={len(final_result.response.State_Definitions)} "
        f"accumulated={len(merged)}",
        "green",
    )
    return paths


def _run_v2_chunked_critic_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    chunked_output: VariantRunOutput,
    critic_ctx: dict[str, Any],
) -> dict[str, Any]:
    analysis_payload = render_static_critic_evidence(
        sliced_methods_payload=critic_ctx.get("sliced_methods_payload") or {},
        static_analysis_payload=critic_ctx.get("static_analysis_payload") or {},
        critic_label="V2-chunked Step 2 critic",
    )
    analysis_payload_path = page.input_dir / "analysis_payload_v2chunked.txt"
    analysis_payload_path.write_text(analysis_payload, encoding="utf-8")

    system_prompt = _load_prompt_text("system_prompt_variant234.txt")
    variant_page_dir = (
        config.output_dir / V2_CHUNKED_CRITIC_VARIANT_DIR / f"page_{page.page}"
    )
    if config.prepare_only:
        paths = _save_prepared_v2_chunked_critic_outputs(
            variant_dir=variant_page_dir,
            system_prompt=system_prompt,
            user_prompt=chunked_output.user_prompt or "",
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            input_dir=page.input_dir,
            source_step1_page_dir=(
                config.output_dir / V2_CHUNKED_VARIANT_DIR / f"page_{page.page}"
            ),
            analysis_payload_available=bool(analysis_payload.strip()),
        )
        log(
            f"[reproduction_pipeline] variant {V2_CHUNKED_CRITIC_VARIANT_KEY} "
            f"page={page.page} prepared",
            "cyan",
        )
        return paths

    if chunked_output.result is None:
        raise ReproductionInputError("V2-chunked critic requires a Step 1 response")
    if chunked_output.user_prompt is None:
        raise ReproductionInputError("V2-chunked critic requires a Step 1 user prompt")

    critic_session_messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": build_user_content(
                chunked_output.user_prompt,
                page.screenshot_path,
            ),
        },
        {"role": "assistant", "content": chunked_output.result.raw_json},
    ]
    try:
        final_predicates, critic_result, critic_prompt = run_critic_turn(
            system_prompt=system_prompt,
            session_messages=critic_session_messages,
            step1_predicates=chunked_output.result.response.State_Definitions,
            analysis_payload=analysis_payload,
            prompts_dir=PYTHON_DIR / "prompts",
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            max_completion_tokens=DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
        )
    except Exception as exc:
        critic_prompt = getattr(exc, "aifc_critic_prompt", None)
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_CHUNKED_CRITIC_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_CHUNKED_CRITIC_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_chunked_critic_step2",
            system_prompt=system_prompt,
            user_prompt=chunked_output.user_prompt,
            critic_prompt=critic_prompt if isinstance(critic_prompt, str) else None,
            extra_meta={
                "source_step1_page_dir": str(
                    config.output_dir / V2_CHUNKED_VARIANT_DIR / f"page_{page.page}"
                ),
                "step1_source": "integrated_v2chunked",
                "step1_api_called": chunked_output.step1_api_called,
                "analysis_payload": str(analysis_payload_path),
            },
        )
        raise
    final_result = _llm_result_with_predicates(
        chunked_output.result,
        final_predicates,
        "Step 2 critic applied to V2-chunked map+dedupe output as drop-only monotone decrease.",
    )
    paths = _save_step2_critic_outputs(
        variant_dir=variant_page_dir,
        variant_key=V2_CHUNKED_CRITIC_VARIANT_KEY,
        prompt_strategy=V2_CHUNKED_CRITIC_PROMPT_STRATEGY,
        system_prompt=system_prompt,
        user_prompt=chunked_output.user_prompt,
        step1_result=chunked_output.result,
        final_result=final_result,
        critic_result=critic_result,
        critic_prompt=critic_prompt,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        input_dir=page.input_dir,
        source_step1_page_dir=(
            config.output_dir / V2_CHUNKED_VARIANT_DIR / f"page_{page.page}"
        ),
        step1_source="integrated_v2chunked",
        step1_api_called=chunked_output.step1_api_called,
        critic_prior_turns=len(critic_session_messages),
        analysis_payload_path=analysis_payload_path,
    )
    paths["analysis_payload"] = str(analysis_payload_path)
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_CHUNKED_CRITIC_VARIANT_KEY,
        page.page,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(final_result))
    _save_accumulated_predicates(
        config.output_dir,
        V2_CHUNKED_CRITIC_VARIANT_KEY,
        merged,
    )
    log(
        f"[reproduction_pipeline] variant {V2_CHUNKED_CRITIC_VARIANT_KEY} "
        f"page={page.page} defs={len(final_result.response.State_Definitions)} "
        f"accumulated={len(merged)}",
        "green",
    )
    return paths


def _run_v2_chunked_context_window_critic_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    chunked_output: VariantRunOutput,
    critic_ctx: dict[str, Any],
) -> dict[str, Any]:
    sliced_payload = critic_ctx.get("sliced_methods_payload")
    static_payload = critic_ctx.get("static_analysis_payload")
    if not isinstance(sliced_payload, dict) or not isinstance(static_payload, dict):
        raise ReproductionInputError(
            "V2chunked chunked critic requires context-window sliced/static payloads"
        )

    system_prompt = _load_prompt_text("system_prompt_variant234.txt")
    source_step1_page_dir = (
        chunked_output.source_page_dir
        or config.output_dir / V2_CHUNKED_VARIANT_DIR / f"page_{page.page}"
    )
    step1_source = (
        chunked_output.step1_source
        if chunked_output.step1_source != "generated"
        else "integrated_v2chunked"
    )
    variant_page_dir = (
        config.output_dir
        / V2_CHUNKED_CRITIC_CHUNKED_VARIANT_DIR
        / f"page_{page.page}"
    )
    if config.prepare_only:
        prepared = prepare_v2chunked_context_window_critic(
            output_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            variant_key=V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY,
            prompt_strategy=V2_CHUNKED_CRITIC_CHUNKED_PROMPT_STRATEGY,
            system_prompt=system_prompt,
            step1_user_prompt=chunked_output.user_prompt or "",
            step1_result=chunked_output.result,
            prompts_dir=PYTHON_DIR / "prompts",
            input_dir=page.input_dir,
            source_step1_page_dir=source_step1_page_dir,
            sliced_methods_payload=sliced_payload,
            static_analysis_payload=static_payload,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            domain_attribution_policy=config.context_window_domain_attribution,
            target_chars=config.resource_chunk_target_chars,
            max_chars=config.resource_chunk_max_chars,
            step1_source=step1_source,
            step1_api_called=chunked_output.step1_api_called,
        )
        log(
            f"[reproduction_pipeline] variant "
            f"{V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY} page={page.page} "
            f"prepared chunks={prepared.paths['chunk_count']}",
            "cyan",
        )
        return prepared.paths

    if chunked_output.result is None:
        raise ReproductionInputError(
            "V2chunked chunked critic requires a Step 1 response"
        )
    if chunked_output.user_prompt is None:
        raise ReproductionInputError(
            "V2chunked chunked critic requires a Step 1 user prompt"
        )

    critic_session_messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": build_user_content(
                chunked_output.user_prompt,
                page.screenshot_path,
            ),
        },
        {"role": "assistant", "content": chunked_output.result.raw_json},
    ]
    try:
        chunked_critic = run_v2chunked_context_window_critic(
            output_dir=variant_page_dir,
            run_id=config.run_id,
            page=page.page,
            variant_key=V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY,
            prompt_strategy=V2_CHUNKED_CRITIC_CHUNKED_PROMPT_STRATEGY,
            system_prompt=system_prompt,
            step1_user_prompt=chunked_output.user_prompt,
            step1_result=chunked_output.result,
            session_messages=critic_session_messages,
            prompts_dir=PYTHON_DIR / "prompts",
            input_dir=page.input_dir,
            source_step1_page_dir=source_step1_page_dir,
            sliced_methods_payload=sliced_payload,
            static_analysis_payload=static_payload,
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            domain_attribution_policy=config.context_window_domain_attribution,
            target_chars=config.resource_chunk_target_chars,
            max_chars=config.resource_chunk_max_chars,
            chunk_max_attempts=config.context_window_chunk_max_attempts,
            chunk_retry_base_delay=config.context_window_chunk_retry_base_delay,
            chunk_retry_max_delay=config.context_window_chunk_retry_max_delay,
            step1_source=step1_source,
            step1_api_called=chunked_output.step1_api_called,
        )
    except Exception as exc:
        critic_prompt = getattr(exc, "aifc_critic_prompt", None)
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_CHUNKED_CRITIC_CHUNKED_PROMPT_STRATEGY,
            prompt_cache_key=getattr(
                exc,
                "aifc_prompt_cache_key",
                config.prompt_cache_key,
            ),
            exc=exc,
            phase="v2_chunked_context_window_critic_step2",
            system_prompt=system_prompt,
            user_prompt=chunked_output.user_prompt,
            critic_prompt=critic_prompt if isinstance(critic_prompt, str) else None,
            extra_meta={
                "chunk_id": getattr(exc, "aifc_chunk_id", None),
                "source_step1_page_dir": str(source_step1_page_dir),
                "step1_source": step1_source,
                "step1_api_called": chunked_output.step1_api_called,
                "target_chars": config.resource_chunk_target_chars,
                "max_chars": config.resource_chunk_max_chars,
                "domain_attribution_policy": config.context_window_domain_attribution,
            },
        )
        raise

    final_result = chunked_critic.final_result
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY,
        page.page,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(final_result))
    _save_accumulated_predicates(
        config.output_dir,
        V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY,
        merged,
    )
    log(
        f"[reproduction_pipeline] variant "
        f"{V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY} page={page.page} "
        f"defs={len(final_result.response.State_Definitions)} accumulated={len(merged)}",
        "green",
    )
    return chunked_critic.paths


def _run_v2_chunked_critic_no_analysis_for_page(
    *,
    config: ReproductionConfig,
    page: CapturedPage,
    chunked_output: VariantRunOutput,
) -> dict[str, Any]:
    system_prompt = _load_prompt_text("system_prompt_variant234.txt")
    source_step1_page_dir = (
        chunked_output.source_page_dir
        or config.output_dir / V2_CHUNKED_VARIANT_DIR / f"page_{page.page}"
    )
    step1_source = (
        chunked_output.step1_source
        if chunked_output.step1_source != "generated"
        else "integrated_v2chunked"
    )
    variant_page_dir = (
        config.output_dir
        / V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_DIR
        / f"page_{page.page}"
    )
    if config.prepare_only:
        paths = _save_prepared_v2_chunked_critic_outputs(
            variant_dir=variant_page_dir,
            variant_key=V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY,
            prompt_strategy=V2_CHUNKED_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY,
            system_prompt=system_prompt,
            user_prompt=chunked_output.user_prompt or "",
            run_id=config.run_id,
            page=page.page,
            model=config.model,
            prompt_cache_key=config.prompt_cache_key,
            input_dir=page.input_dir,
            source_step1_page_dir=source_step1_page_dir,
            analysis_payload_available=False,
            step1_source=step1_source,
            step1_api_called=chunked_output.step1_api_called,
        )
        log(
            "[reproduction_pipeline] variant "
            f"{V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY} "
            f"page={page.page} prepared",
            "cyan",
        )
        return paths

    if chunked_output.result is None:
        raise ReproductionInputError(
            "V2-chunked no-analysis critic requires a Step 1 response"
        )
    if chunked_output.user_prompt is None:
        raise ReproductionInputError(
            "V2-chunked no-analysis critic requires a Step 1 user prompt"
        )

    critic_session_messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": build_user_content(
                chunked_output.user_prompt,
                page.screenshot_path,
            ),
        },
        {"role": "assistant", "content": chunked_output.result.raw_json},
    ]
    try:
        final_predicates, critic_result, critic_prompt = run_critic_turn(
            system_prompt=system_prompt,
            session_messages=critic_session_messages,
            step1_predicates=chunked_output.result.response.State_Definitions,
            analysis_payload="",
            prompts_dir=PYTHON_DIR / "prompts",
            model=config.model,
            timeout=config.timeout,
            prompt_cache_key=config.prompt_cache_key,
            max_completion_tokens=DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
            include_analysis_payload=False,
        )
    except Exception as exc:
        critic_prompt = getattr(exc, "aifc_critic_prompt", None)
        _save_api_failure_outputs(
            variant_dir=variant_page_dir,
            run_id=config.run_id,
            variant=V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY,
            page=page.page,
            model=config.model,
            prompt_strategy=V2_CHUNKED_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY,
            prompt_cache_key=config.prompt_cache_key,
            exc=exc,
            phase="v2_chunked_critic_no_analysis_step2",
            system_prompt=system_prompt,
            user_prompt=chunked_output.user_prompt,
            critic_prompt=critic_prompt if isinstance(critic_prompt, str) else None,
            extra_meta={
                "source_step1_page_dir": str(source_step1_page_dir),
                "step1_source": step1_source,
                "step1_api_called": chunked_output.step1_api_called,
                "analysis_payload_available": False,
            },
        )
        raise
    final_result = _llm_result_with_predicates(
        chunked_output.result,
        final_predicates,
        (
            "Step 2 no-analysis critic applied to V2-chunked map+dedupe "
            "output as drop-only monotone decrease."
        ),
    )
    paths = _save_step2_critic_outputs(
        variant_dir=variant_page_dir,
        variant_key=V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY,
        prompt_strategy=V2_CHUNKED_CRITIC_NO_ANALYSIS_PROMPT_STRATEGY,
        system_prompt=system_prompt,
        user_prompt=chunked_output.user_prompt,
        step1_result=chunked_output.result,
        final_result=final_result,
        critic_result=critic_result,
        critic_prompt=critic_prompt,
        run_id=config.run_id,
        page=page.page,
        prompt_cache_key=config.prompt_cache_key,
        input_dir=page.input_dir,
        source_step1_page_dir=source_step1_page_dir,
        step1_source=step1_source,
        step1_api_called=chunked_output.step1_api_called,
        critic_prior_turns=len(critic_session_messages),
        analysis_payload_path=None,
    )
    accumulated = _load_accumulated_predicates_for_page(
        config.output_dir,
        V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY,
        page.page,
    )
    merged = merge_state_definitions(accumulated, _result_predicates(final_result))
    _save_accumulated_predicates(
        config.output_dir,
        V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY,
        merged,
    )
    log(
        "[reproduction_pipeline] variant "
        f"{V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY} "
        f"page={page.page} defs={len(final_result.response.State_Definitions)} "
        f"accumulated={len(merged)}",
        "green",
    )
    return paths


def _run_page(
    *,
    page: CapturedPage,
    config: ReproductionConfig,
    static_inputs: StaticSemanticsInputs,
    raw_source_code: str | None,
    query_fn: Callable[..., Any],
) -> dict[str, Any]:
    page_ctx = _build_page_code_contexts(
        page=page,
        config=config,
        static_inputs=static_inputs,
        raw_source_code=raw_source_code,
    )
    page_record = _page_entry(page)
    if config.emit_resource_evidence_chunks:
        artifacts = _emit_context_window_chunk_artifacts(
            page=page,
            config=config,
            context_window_ctx=page_ctx.get("context_window", {}),
        )
        if artifacts is not None:
            page_record["context_window_artifacts"] = artifacts
    v2_output: VariantRunOutput | None = None
    v2_critic_ran = False
    v2_critic_no_analysis_ran = False
    v2chunked_critic_no_analysis_ran = False
    record_reused_step1 = _should_record_reused_step1(config)
    record_reused_v2chunked_step1 = _should_record_reused_v2chunked_step1(config)
    for variant in config.variants:
        if variant == 2 and config.reuse_v2_from is not None:
            output = _load_reused_v2_step1_output(
                reuse_run_dir=config.reuse_v2_from,
                page=page.page,
                fallback_model=config.model,
            )
            log(
                f"[reproduction_pipeline] variant 2 page={page.page} reused "
                f"from {output.source_page_dir}",
                "cyan",
            )
        elif is_v2_chunked(variant) and config.reuse_v2chunked_from is not None:
            output = _load_reused_v2chunked_step1_output(
                reuse_run_dir=config.reuse_v2chunked_from,
                page=page.page,
                fallback_model=config.model,
            )
            log(
                f"[reproduction_pipeline] variant {V2_CHUNKED_VARIANT_KEY} "
                f"page={page.page} reused from {output.source_page_dir}",
                "cyan",
            )
        else:
            code_ctx_key = "context_window" if is_context_window_chunked(variant) else variant
            output = _run_variant_for_page(
                config=config,
                page=page,
                variant=variant,
                code_ctx=page_ctx.get(code_ctx_key, {}),
                query_fn=query_fn,
            )
        should_record_step1 = True
        if variant == 2 and config.reuse_v2_from is not None:
            should_record_step1 = record_reused_step1
        if is_v2_chunked(variant) and config.reuse_v2chunked_from is not None:
            should_record_step1 = record_reused_v2chunked_step1
        if should_record_step1:
            page_record["variant_outputs"][str(variant)] = output.paths
        if variant == 2:
            v2_output = output
            if config.enable_v2_critic:
                page_record["variant_outputs"][V2_CRITIC_VARIANT_KEY] = (
                    _run_v2_critic_for_page(
                        config=config,
                        page=page,
                        v2_output=v2_output,
                        critic_ctx=page_ctx.get(V2_CRITIC_VARIANT_KEY, {}),
                    )
                )
                v2_critic_ran = True
            if config.enable_v2_critic_no_analysis:
                page_record["variant_outputs"][V2_CRITIC_NO_ANALYSIS_VARIANT_KEY] = (
                    _run_v2_critic_no_analysis_for_page(
                        config=config,
                        page=page,
                        v2_output=v2_output,
                    )
                )
                v2_critic_no_analysis_ran = True
        elif is_v2_chunked(variant) and config.enable_v2_critic:
            page_record["variant_outputs"][V2_CHUNKED_CRITIC_VARIANT_KEY] = (
                _run_v2_chunked_critic_for_page(
                    config=config,
                    page=page,
                    chunked_output=output,
                    critic_ctx=page_ctx.get(V2_CHUNKED_CRITIC_VARIANT_KEY, {}),
                )
            )
        if is_v2_chunked(variant) and config.enable_v2chunked_critic_chunked:
            page_record["variant_outputs"][
                V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY
            ] = _run_v2_chunked_context_window_critic_for_page(
                config=config,
                page=page,
                chunked_output=output,
                critic_ctx=page_ctx.get(V2_CHUNKED_CRITIC_CHUNKED_VARIANT_KEY, {}),
            )
        if is_v2_chunked(variant) and config.enable_v2_critic_no_analysis:
            page_record["variant_outputs"][
                V2_CHUNKED_CRITIC_NO_ANALYSIS_VARIANT_KEY
            ] = _run_v2_chunked_critic_no_analysis_for_page(
                config=config,
                page=page,
                chunked_output=output,
            )
            v2chunked_critic_no_analysis_ran = True
    if (
        (config.enable_v2_critic and not v2_critic_ran)
        or (
            config.enable_v2_critic_no_analysis
            and not v2_critic_no_analysis_ran
            and not v2chunked_critic_no_analysis_ran
        )
    ) and config.reuse_v2_from is not None:
        v2_output = _load_reused_v2_step1_output(
            reuse_run_dir=config.reuse_v2_from,
            page=page.page,
            fallback_model=config.model,
        )
        if record_reused_step1:
            page_record["variant_outputs"]["2"] = v2_output.paths
        if config.enable_v2_critic and not v2_critic_ran:
            page_record["variant_outputs"][V2_CRITIC_VARIANT_KEY] = (
                _run_v2_critic_for_page(
                    config=config,
                    page=page,
                    v2_output=v2_output,
                    critic_ctx=page_ctx.get(V2_CRITIC_VARIANT_KEY, {}),
                )
            )
        if config.enable_v2_critic_no_analysis and not v2_critic_no_analysis_ran:
            page_record["variant_outputs"][V2_CRITIC_NO_ANALYSIS_VARIANT_KEY] = (
                _run_v2_critic_no_analysis_for_page(
                    config=config,
                    page=page,
                    v2_output=v2_output,
                )
            )
    if config.emit_naive_rendered_prompt_chunks:
        artifacts = _emit_naive_rendered_prompt_chunk_artifacts(
            page=page,
            config=config,
            page_record=page_record,
        )
        if artifacts is not None:
            page_record["naive_rendered_prompt_chunks"] = artifacts
    return page_record


def _write_experiment_state(config: ReproductionConfig, pages: list[CapturedPage]) -> None:
    last_page = max((page.page for page in pages), default=0)
    write_json(
        config.output_dir / "experiment_state.json",
        {
            "page_count": last_page,
            "selected_pages": [page.page for page in pages],
            "page_range": config.page_range,
            "updated_at": iso_now(),
        },
    )


def _default_reuse_run_id(reuse_run_dir: Path) -> str:
    manifest_path = reuse_run_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = read_json(manifest_path)
            run_id = manifest.get("run_id") if isinstance(manifest, dict) else None
            if isinstance(run_id, str) and run_id.strip():
                return run_id.strip()
        except Exception:
            pass
    return reuse_run_dir.name


def run_reproduction_pipeline(
    config: ReproductionConfig,
    *,
    query_fn: Callable[..., Any] = query_llm,
) -> None:
    _validate_config(config)
    has_multiturn_lane = any(
        is_v2_responses_multiturn(variant) for variant in config.variants
    )
    page_local_variants = [
        variant
        for variant in config.variants
        if not is_v2_responses_multiturn(variant)
    ]
    page_local_config = replace(config, variants=page_local_variants)
    log(
        f"[reproduction_pipeline] run_id={config.run_id} "
        f"variants={_manifest_variants(config)} model={config.model} "
        f"v2_critic={config.enable_v2_critic} "
        f"v2_critic_no_analysis={config.enable_v2_critic_no_analysis} "
        f"v2chunked_critic_chunked={config.enable_v2chunked_critic_chunked} "
        f"reuse_v2chunked_from={config.reuse_v2chunked_from}",
        "magenta",
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)
    pages = _prepare_pages(config)
    static_inputs = _resolve_static_semantics(config)
    raw_source_code = (
        load_raw_source_code(config.app_source_root)
        if 2 in page_local_config.variants and config.reuse_v2_from is None
        else None
    )
    manifest_pages: list[dict[str, Any]] = []
    for page in pages:
        manifest_pages.append(
            _run_page(
                page=page,
                config=page_local_config,
                static_inputs=static_inputs,
                raw_source_code=raw_source_code,
                query_fn=query_fn,
            )
        )
    if has_multiturn_lane:
        _run_v2_responses_multiturn_lane(
            config=config,
            pages=pages,
            page_records=manifest_pages,
        )

    _write_manifest(config=config, static_inputs=static_inputs, pages=manifest_pages)
    _write_experiment_state(config, pages)
    log(f"[reproduction_pipeline] done -> {config.output_dir}", "green")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run legacy-style V1~V4 reproduction experiments",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        default=None,
        help=f"Variants to run: 1 2 3 4, plus {V2_SOURCE_BASELINE_HELP}",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--model", default="gpt-5.2")
    parser.add_argument("--app-source-root", default=str(DEFAULT_APP_SOURCE_ROOT))
    parser.add_argument("--static-semantics-run-dir", default=None)
    parser.add_argument("--context-slicer-dir", default=None)
    parser.add_argument("--method-cfg-index-path", default=None)
    parser.add_argument("--screenshot-path", default=None)
    parser.add_argument("--a11y-path", default=None)
    parser.add_argument("--replay-from", default=None)
    parser.add_argument(
        "--page-range",
        default=None,
        help=(
            "Inclusive replay page range while preserving original page numbers. "
            "Forms: '5', '1:6', '5:8', ':6', or '7:'."
        ),
    )
    parser.add_argument("--device-serial", default=None)
    parser.add_argument("--prompt-cache-key", default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
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
    parser.add_argument(
        "--emit-resource-evidence-chunks",
        action="store_true",
        help=(
            "Emit resource-seeded context-window chunking artifacts under "
            "inputs/page_N/context_window without changing variant prompts."
        ),
    )
    parser.add_argument(
        "--resource-chunk-target-chars",
        type=int,
        default=400000,
        help="Soft target rendered characters per context-window evidence chunk.",
    )
    parser.add_argument(
        "--resource-chunk-max-chars",
        type=int,
        default=500000,
        help="Hard max rendered characters per context-window evidence chunk.",
    )
    parser.add_argument(
        "--context-window-domain-attribution",
        choices=sorted(DOMAIN_ATTRIBUTION_POLICIES),
        default=DOMAIN_ATTRIBUTION_METHOD_LOCAL,
        help="Domain type attachment policy for context-window evidence chunks.",
    )
    parser.add_argument(
        "--context-window-chunk-max-attempts",
        type=int,
        default=3,
        help=(
            "Maximum attempts per context-window chunk API call. Retryable "
            "timeouts, connection errors, rate limits, and 5xx failures are retried."
        ),
    )
    parser.add_argument(
        "--context-window-chunk-retry-base-delay",
        type=float,
        default=30.0,
        help="Initial retry sleep in seconds for context-window chunk calls.",
    )
    parser.add_argument(
        "--context-window-chunk-retry-max-delay",
        type=float,
        default=120.0,
        help="Maximum retry sleep in seconds for context-window chunk calls.",
    )
    parser.add_argument(
        "--emit-naive-rendered-prompt-chunks",
        action="store_true",
        help=(
            "Emit naive rendered V3/V4 prompt text chunks under "
            "inputs/page_N/context_window/naive_rendered_prompt."
        ),
    )
    parser.add_argument(
        "--responses-compact-threshold",
        type=int,
        default=DEFAULT_RESPONSES_COMPACT_THRESHOLD,
        help=(
            "Responses API server-side auto-compaction threshold for "
            "2responses_multiturn."
        ),
    )
    parser.add_argument(
        "--enable-v2-critic",
        "--enable-critic",
        dest="enable_v2_critic",
        action="store_true",
        help=(
            "After regular variant 2 or v2-chunked, run the Step 2 static critic "
            "in the same command and save it under variant_2_critic/page_N or "
            "variant_2chunked_critic/page_N. Requires static semantics inputs."
        ),
    )
    parser.add_argument(
        "--enable-v2-critic-no-analysis",
        dest="enable_v2_critic_no_analysis",
        action="store_true",
        help=(
            "After regular variant 2, run the Step 2 critic with the static "
            "analysis section removed from the critic prompt and save it under "
            "variant_2_critic_no_analysis/page_N. Also works with --reuse-v2-from."
        ),
    )
    parser.add_argument(
        "--enable-v2chunked-critic-chunked",
        dest="enable_v2chunked_critic_chunked",
        action="store_true",
        help=(
            "After v2-chunked, run the Step 2 static critic over context-window "
            "evidence chunks and save it under "
            "variant_2chunked_critic_chunked/page_N. Keeps existing critic prompts "
            "and applies only explicit drop verdicts."
        ),
    )
    parser.add_argument(
        "--reuse-v2-from",
        default=None,
        help=(
            "Existing reproduction run directory. When set, variant_2/page_N "
            "prompts and responses are reused as Step 1, and this command runs "
            "or prepares V2 critic lanes unless --variants is explicitly provided. "
            "Defaults --output-dir to the reused run."
        ),
    )
    parser.add_argument(
        "--reuse-v2chunked-from",
        default=None,
        help=(
            "Existing reproduction run directory containing variant_2chunked/page_N. "
            "When set with --variants 2chunked, Step 1 chunked outputs are reused "
            "for critic lanes instead of rerunning V2-chunked."
        ),
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help=(
            "Write replay inputs, variant contexts, prompts, and manifest without "
            "calling any LLM API. Later pages cannot include newly generated "
            "accumulated predicates because no responses are produced."
        ),
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    reuse_v2_from = Path(args.reuse_v2_from) if args.reuse_v2_from else None
    reuse_v2chunked_from = (
        Path(args.reuse_v2chunked_from) if args.reuse_v2chunked_from else None
    )
    variant_values = args.variants or (
        ["2"]
        if reuse_v2_from
        else [V2_CHUNKED_VARIANT_KEY]
        if reuse_v2chunked_from
        else ["1", "2", "3", "4"]
    )
    variants = parse_variant_list(variant_values)
    timestamp = time.strftime("%Y-%m-%dT%H-%M-%SZ")
    run_id = args.run_id or (
        _default_reuse_run_id(reuse_v2_from)
        if reuse_v2_from is not None
        else f"{timestamp}__reproduction"
    )
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else reuse_v2_from
        if reuse_v2_from is not None
        else DEFAULT_RUNS_DIR / run_id
    )
    config = ReproductionConfig(
        variants=variants,
        run_id=run_id,
        output_dir=output_dir,
        app_source_root=Path(args.app_source_root),
        model=args.model,
        context_slicer_dir=Path(args.context_slicer_dir) if args.context_slicer_dir else None,
        method_cfg_index_path=Path(args.method_cfg_index_path) if args.method_cfg_index_path else None,
        static_semantics_run_dir=(
            Path(args.static_semantics_run_dir)
            if args.static_semantics_run_dir
            else None
        ),
        screenshot_path=Path(args.screenshot_path) if args.screenshot_path else None,
        a11y_path=Path(args.a11y_path) if args.a11y_path else None,
        replay_from=Path(args.replay_from) if args.replay_from else None,
        page_range=args.page_range,
        device_serial=args.device_serial,
        prompt_cache_key=args.prompt_cache_key or run_id,
        timeout=args.timeout,
        v2_chunk_target_chars=args.v2_chunk_target_chars,
        v2_chunk_max_chars=args.v2_chunk_max_chars,
        emit_resource_evidence_chunks=args.emit_resource_evidence_chunks,
        resource_chunk_target_chars=args.resource_chunk_target_chars,
        resource_chunk_max_chars=args.resource_chunk_max_chars,
        context_window_domain_attribution=args.context_window_domain_attribution,
        context_window_chunk_max_attempts=args.context_window_chunk_max_attempts,
        context_window_chunk_retry_base_delay=args.context_window_chunk_retry_base_delay,
        context_window_chunk_retry_max_delay=args.context_window_chunk_retry_max_delay,
        emit_naive_rendered_prompt_chunks=args.emit_naive_rendered_prompt_chunks,
        responses_compact_threshold=args.responses_compact_threshold,
        prepare_only=args.prepare_only,
        enable_v2_critic=(
            args.enable_v2_critic
            or (
                reuse_v2_from is not None
                and not args.enable_v2_critic_no_analysis
            )
        ),
        enable_v2_critic_no_analysis=args.enable_v2_critic_no_analysis,
        enable_v2chunked_critic_chunked=args.enable_v2chunked_critic_chunked,
        reuse_v2_from=reuse_v2_from,
        reuse_v2chunked_from=reuse_v2chunked_from,
    )
    run_reproduction_pipeline(config)


if __name__ == "__main__":
    main()
