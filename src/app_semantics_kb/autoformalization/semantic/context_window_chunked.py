"""Context-window chunked V3/V4 semantic runner.

This lane keeps the page-level PredicateResponse contract unchanged while
splitting static-code evidence into independent, self-contained chunks.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from ..fusion._a11y import compact_a11y
from ..fusion.context_merger import MergedContext
from ..fusion.context_window import (
    build_context_evidence_index,
    pack_evidence_graph_chunks,
    render_chunk_evidence,
    slim_chunks_manifest,
)
from ..utils import iso_now, log, pretty_json, write_json
from .api_diagnostics import (
    aggregate_result_usage,
    attached_request_meta,
    result_usage_dict,
)
from .llm_client import LLMResult, PredicateResponse, StatePredicate, query_llm
from .predicate_merger import merge_state_definitions
from .prompt_builder import build_prompt

ContextWindowChunkQueryFn = Callable[..., LLMResult]
ChunkMode = Literal["v3", "v4"]
DEFAULT_CHUNK_MAX_ATTEMPTS = 3
DEFAULT_CHUNK_RETRY_BASE_DELAY_SEC = 30.0
DEFAULT_CHUNK_RETRY_MAX_DELAY_SEC = 120.0
NO_STATIC_EVIDENCE_CHUNK_ID = "chunk_0001"
NO_STATIC_EVIDENCE_REASON = "no_static_evidence_chunks"


@dataclass(frozen=True)
class ContextWindowChunkPrompt:
    chunk_id: str
    rendered_evidence: str
    system_prompt: str
    user_prompt: str
    prompt_cache_key: str | None


@dataclass(frozen=True)
class ContextWindowChunkedPreparedResult:
    chunks_manifest: dict[str, Any]
    merge_meta: dict[str, Any]
    paths: dict[str, Any]


@dataclass(frozen=True)
class ContextWindowChunkedRunResult:
    final_result: LLMResult
    chunks_manifest: dict[str, Any]
    merge_meta: dict[str, Any]
    chunk_outputs: list[dict[str, Any]]
    paths: dict[str, Any]


def prepare_context_window_chunked_generation(
    *,
    output_dir: Path,
    run_id: str,
    page: int,
    variant_key: str,
    base_variant: int,
    prompt_strategy: str,
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    sliced_methods_payload: dict[str, Any],
    static_analysis_payload: dict[str, Any],
    model: str,
    prompt_cache_key: str | None,
    domain_attribution_policy: str,
    target_chars: int = 400000,
    max_chars: int = 500000,
) -> ContextWindowChunkedPreparedResult:
    """Create chunked prompts/artifacts without calling the LLM API."""
    bundle = _prepare_chunk_bundle(
        output_dir=output_dir,
        variant_key=variant_key,
        base_variant=base_variant,
        page=page,
        app_name=app_name,
        a11y_xml=a11y_xml,
        screenshot_path=screenshot_path,
        existing_predicates=existing_predicates,
        sliced_methods_payload=sliced_methods_payload,
        static_analysis_payload=static_analysis_payload,
        prompt_cache_key=prompt_cache_key,
        domain_attribution_policy=domain_attribution_policy,
        target_chars=target_chars,
        max_chars=max_chars,
    )
    merge_meta = _prepared_merge_meta(
        variant_key=variant_key,
        base_variant=base_variant,
        chunks_manifest=bundle["chunks_manifest"],
    )
    write_json(output_dir / "merge_meta.json", merge_meta)
    write_json(
        output_dir / "run_meta.json",
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
            "chunk_count": len(bundle["prompts"]),
            "target_chars": target_chars,
            "max_chars": max_chars,
            "domain_attribution_policy": domain_attribution_policy,
            "response_available": False,
            "usage_available": False,
            "accumulated_predicates_updated": False,
            "prepared_at": iso_now(),
        },
    )
    paths = _prepared_paths(
        output_dir,
        bundle["chunks_manifest"],
        merge_meta,
        prompt_strategy=prompt_strategy,
        target_chars=target_chars,
        max_chars=max_chars,
    )
    log(
        f"[context_window_chunked] variant={variant_key} page={page} "
        f"prepared chunks={len(bundle['prompts'])}",
        "cyan",
    )
    return ContextWindowChunkedPreparedResult(
        chunks_manifest=bundle["chunks_manifest"],
        merge_meta=merge_meta,
        paths=paths,
    )


def run_context_window_chunked_generation(
    *,
    output_dir: Path,
    run_id: str,
    page: int,
    variant_key: str,
    base_variant: int,
    prompt_strategy: str,
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path,
    existing_predicates: list[dict[str, Any]],
    sliced_methods_payload: dict[str, Any],
    static_analysis_payload: dict[str, Any],
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    domain_attribution_policy: str,
    target_chars: int = 400000,
    max_chars: int = 500000,
    chunk_max_attempts: int = DEFAULT_CHUNK_MAX_ATTEMPTS,
    chunk_retry_base_delay: float = DEFAULT_CHUNK_RETRY_BASE_DELAY_SEC,
    chunk_retry_max_delay: float = DEFAULT_CHUNK_RETRY_MAX_DELAY_SEC,
    resume_existing_chunks: bool = True,
    query_fn: ContextWindowChunkQueryFn | None = None,
) -> ContextWindowChunkedRunResult:
    """Run independent chunk calls and merge their PredicateResponse outputs."""
    bundle = _prepare_chunk_bundle(
        output_dir=output_dir,
        variant_key=variant_key,
        base_variant=base_variant,
        page=page,
        app_name=app_name,
        a11y_xml=a11y_xml,
        screenshot_path=screenshot_path,
        existing_predicates=existing_predicates,
        sliced_methods_payload=sliced_methods_payload,
        static_analysis_payload=static_analysis_payload,
        prompt_cache_key=prompt_cache_key,
        domain_attribution_policy=domain_attribution_policy,
        target_chars=target_chars,
        max_chars=max_chars,
    )
    resolved_query = query_fn or query_llm
    chunk_outputs: list[dict[str, Any]] = []
    results: list[LLMResult] = []
    for prompt in bundle["prompts"]:
        existing_record = _load_reusable_chunk_output(output_dir, prompt)
        if resume_existing_chunks and existing_record is not None:
            record = _mark_reused_chunk_record(existing_record)
            result = _result_from_chunk_output_record(
                record,
                model=model,
                base_variant=base_variant,
            )
            log(
                f"[context_window_chunked] reuse {variant_key} page={page} "
                f"{prompt.chunk_id}",
                "cyan",
            )
        else:
            result, retry_failures = _query_chunk_with_retry(
                output_dir=output_dir,
                prompt=prompt,
                resolved_query=resolved_query,
                screenshot_path=screenshot_path,
                base_variant=base_variant,
                model=model,
                timeout=timeout,
                max_attempts=chunk_max_attempts,
                retry_base_delay=chunk_retry_base_delay,
                retry_max_delay=chunk_retry_max_delay,
            )
            record = _chunk_output_record(
                prompt=prompt,
                result=result,
                attempt_count=len(retry_failures) + 1,
                retry_failures=retry_failures,
            )
            _write_chunk_output(output_dir, record)
        chunk_outputs.append(record)
        results.append(result)

    final_result, merge_meta = _merge_chunk_results(
        results=results,
        chunk_outputs=chunk_outputs,
        run_id=run_id,
        page=page,
        variant_key=variant_key,
        base_variant=base_variant,
        model=model,
        prompt_strategy=prompt_strategy,
        prompt_cache_key=prompt_cache_key,
        chunks_manifest=bundle["chunks_manifest"],
        chunk_execution=_chunk_execution_meta(chunk_outputs),
    )
    cleanup_meta = _cleanup_stale_failure_artifacts(output_dir)
    merge_meta["stale_failure_cleanup"] = cleanup_meta
    _write_final_outputs(
        output_dir=output_dir,
        result=final_result,
        merge_meta=merge_meta,
        run_id=run_id,
        page=page,
        variant_key=variant_key,
        base_variant=base_variant,
        model=model,
        prompt_strategy=prompt_strategy,
        prompt_cache_key=prompt_cache_key,
        target_chars=target_chars,
        max_chars=max_chars,
        domain_attribution_policy=domain_attribution_policy,
        chunk_max_attempts=chunk_max_attempts,
        resume_existing_chunks=resume_existing_chunks,
    )
    paths = _run_paths(
        output_dir,
        final_result,
        bundle["chunks_manifest"],
        merge_meta,
        prompt_strategy=prompt_strategy,
        target_chars=target_chars,
        max_chars=max_chars,
    )
    log(
        f"[context_window_chunked] variant={variant_key} page={page} "
        f"chunks={len(results)} defs={len(final_result.response.State_Definitions)}",
        "green",
    )
    return ContextWindowChunkedRunResult(
        final_result=final_result,
        chunks_manifest=bundle["chunks_manifest"],
        merge_meta=merge_meta,
        chunk_outputs=chunk_outputs,
        paths=paths,
    )


def _prepare_chunk_bundle(
    *,
    output_dir: Path,
    variant_key: str,
    base_variant: int,
    page: int,
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    sliced_methods_payload: dict[str, Any],
    static_analysis_payload: dict[str, Any],
    prompt_cache_key: str | None,
    domain_attribution_policy: str,
    target_chars: int,
    max_chars: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "chunk_prompts").mkdir(parents=True, exist_ok=True)
    (output_dir / "chunk_outputs").mkdir(parents=True, exist_ok=True)

    index = build_context_evidence_index(
        sliced_methods_payload,
        static_analysis_payload,
        domain_attribution_policy=domain_attribution_policy,
    )
    packed = pack_evidence_graph_chunks(
        index,
        target_chars=target_chars,
        max_chars=max_chars,
    )
    write_json(output_dir / "context_evidence_index.json", index)
    manifest = slim_chunks_manifest(packed)
    prompts = [
        _build_chunk_prompt(
            chunk=chunk,
            variant_key=variant_key,
            base_variant=base_variant,
            page=page,
            app_name=app_name,
            a11y_xml=a11y_xml,
            screenshot_path=screenshot_path,
            existing_predicates=existing_predicates,
            prompt_cache_key=prompt_cache_key,
        )
        for chunk in packed["chunks"]
    ]
    if not prompts:
        manifest = _manifest_with_no_static_evidence_fallback(
            manifest=manifest,
            index=index,
            base_variant=base_variant,
        )
        prompts = [
            _build_no_static_evidence_prompt(
                variant_key=variant_key,
                base_variant=base_variant,
                page=page,
                app_name=app_name,
                a11y_xml=a11y_xml,
                screenshot_path=screenshot_path,
                existing_predicates=existing_predicates,
                prompt_cache_key=prompt_cache_key,
            )
        ]
    write_json(output_dir / "chunks_manifest.json", manifest)
    for prompt in prompts:
        _write_chunk_prompt(output_dir, prompt)
    _write_root_prompt_summary(
        output_dir=output_dir,
        variant_key=variant_key,
        base_variant=base_variant,
        prompts=prompts,
    )
    return {
        "index": index,
        "packed": packed,
        "chunks_manifest": manifest,
        "prompts": prompts,
    }


def _build_chunk_prompt(
    *,
    chunk: dict[str, Any],
    variant_key: str,
    base_variant: int,
    page: int,
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    prompt_cache_key: str | None,
) -> ContextWindowChunkPrompt:
    rendered = render_chunk_evidence(chunk, mode=_chunk_mode(base_variant))
    context: MergedContext = {
        "variant": base_variant,
        "app_name": app_name,
        "screenshot_path": screenshot_path,
        "accessibility_tree": compact_a11y(a11y_xml),
        "existing_predicates": pretty_json(existing_predicates),
        "raw_source_code": None,
        "sliced_methods_source_blob": rendered,
        "cfg_context_json": None,
    }
    system_prompt, user_prompt = build_prompt(base_variant, context)
    chunk_id = str(chunk["chunk_id"])
    return ContextWindowChunkPrompt(
        chunk_id=chunk_id,
        rendered_evidence=rendered,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        prompt_cache_key=_chunk_prompt_cache_key(
            base_key=prompt_cache_key,
            variant_key=variant_key,
            page=page,
            chunk_id=chunk_id,
            rendered_evidence=rendered,
        ),
    )


def _build_no_static_evidence_prompt(
    *,
    variant_key: str,
    base_variant: int,
    page: int,
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    prompt_cache_key: str | None,
) -> ContextWindowChunkPrompt:
    rendered = _no_static_evidence_blob(base_variant)
    context: MergedContext = {
        "variant": base_variant,
        "app_name": app_name,
        "screenshot_path": screenshot_path,
        "accessibility_tree": compact_a11y(a11y_xml),
        "existing_predicates": pretty_json(existing_predicates),
        "raw_source_code": None,
        "sliced_methods_source_blob": rendered,
        "cfg_context_json": None,
    }
    system_prompt, user_prompt = build_prompt(base_variant, context)
    return ContextWindowChunkPrompt(
        chunk_id=NO_STATIC_EVIDENCE_CHUNK_ID,
        rendered_evidence=rendered,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        prompt_cache_key=_chunk_prompt_cache_key(
            base_key=prompt_cache_key,
            variant_key=variant_key,
            page=page,
            chunk_id=NO_STATIC_EVIDENCE_CHUNK_ID,
            rendered_evidence=rendered,
        ),
    )


def _no_static_evidence_blob(base_variant: int) -> str:
    lines = [
        "# Sliced Methods Source Context",
        "",
        "No page-local static method, domain type, or CFG evidence was available for this screen.",
        "Use the attached screenshot, accessibility tree, and existing predicates only.",
    ]
    if base_variant == 4:
        lines.extend(
            [
                "",
                "# Resource -> Method Index",
                "",
                "No static resource-to-method mappings were available.",
                "",
                "# Call Path Overview",
                "",
                "No static call paths were available.",
                "",
                "# CFG Details",
                "",
                "No method CFG entries were available.",
            ]
        )
    return "\n".join(lines) + "\n"


def _manifest_with_no_static_evidence_fallback(
    *,
    manifest: dict[str, Any],
    index: dict[str, Any],
    base_variant: int,
) -> dict[str, Any]:
    updated = dict(manifest)
    meta = dict(updated.get("meta") or {})
    stats = dict(updated.get("stats") or {})
    static_chunk_count = int(stats.get("chunk_count") or 0)
    static_covered_resource_count = int(stats.get("covered_resource_count") or 0)
    resource_ids = _resource_ids_from_index(index)
    rendered = _no_static_evidence_blob(base_variant)
    render_counts = {
        "v3" if base_variant == 3 else "v4": len(rendered),
    }
    meta.update(
        {
            "prompt_fallback": "image_a11y_only_when_static_evidence_empty",
            "fallback_reason": NO_STATIC_EVIDENCE_REASON,
        }
    )
    stats.update(
        {
            "static_evidence_chunk_count": static_chunk_count,
            "static_evidence_covered_resource_count": static_covered_resource_count,
            "llm_prompt_chunk_count": 1,
            "llm_prompt_resource_count": len(resource_ids),
            "chunk_count": 1,
            "covered_resource_count": len(resource_ids),
            "post_merge_chunk_count": 1,
        }
    )
    updated["meta"] = meta
    updated["stats"] = stats
    updated["chunks"] = [
        {
            "chunk_id": NO_STATIC_EVIDENCE_CHUNK_ID,
            "fallback": True,
            "fallback_reason": NO_STATIC_EVIDENCE_REASON,
            "resource_ids_covered": resource_ids,
            "method_full_names": [],
            "domain_type_refs": [],
            "cfg_method_full_names": [],
            "estimated_chars": len(rendered),
            "rendered_char_counts": render_counts,
            "oversized": False,
        }
    ]
    return updated


def _resource_ids_from_index(index: dict[str, Any]) -> list[str]:
    resource_ids: list[str] = []
    for entry in index.get("resources", []) or []:
        if isinstance(entry, dict):
            resource_id = str(entry.get("resource_id") or "").strip()
            if resource_id:
                resource_ids.append(resource_id)
    return resource_ids


def _chunk_mode(base_variant: int) -> ChunkMode:
    if base_variant == 3:
        return "v3"
    if base_variant == 4:
        return "v4"
    raise ValueError(f"context-window chunking supports base variant 3/4, got {base_variant}")


def _chunk_prompt_cache_key(
    *,
    base_key: str | None,
    variant_key: str,
    page: int,
    chunk_id: str,
    rendered_evidence: str,
) -> str | None:
    if base_key is None or not base_key.strip():
        return None
    sha12 = hashlib.sha256(rendered_evidence.encode("utf-8")).hexdigest()[:12]
    return f"{base_key.strip()}:{variant_key}:page_{page}:{chunk_id}:{sha12}"


def _write_chunk_prompt(output_dir: Path, prompt: ContextWindowChunkPrompt) -> None:
    prompt_dir = output_dir / "chunk_prompts"
    (prompt_dir / f"{prompt.chunk_id}_system.txt").write_text(
        prompt.system_prompt,
        encoding="utf-8",
    )
    (prompt_dir / f"{prompt.chunk_id}_user.txt").write_text(
        prompt.user_prompt,
        encoding="utf-8",
    )


def _write_root_prompt_summary(
    *,
    output_dir: Path,
    variant_key: str,
    base_variant: int,
    prompts: list[ContextWindowChunkPrompt],
) -> None:
    root_system = prompts[0].system_prompt if prompts else ""
    (output_dir / "prompt_system.txt").write_text(root_system, encoding="utf-8")
    lines = [
        "Context-window chunked variant summary.",
        "",
        f"variant: {variant_key}",
        f"base_variant: {base_variant}",
        f"chunk_count: {len(prompts)}",
        "",
        "Actual API user prompts are stored under chunk_prompts/.",
    ]
    for prompt in prompts:
        lines.append(f"- {prompt.chunk_id}: chunk_prompts/{prompt.chunk_id}_user.txt")
    (output_dir / "prompt_user.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _chunk_output_record(
    *,
    prompt: ContextWindowChunkPrompt,
    result: LLMResult,
    attempt_count: int = 1,
    retry_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "chunk_id": prompt.chunk_id,
        "prompt_cache_key": prompt.prompt_cache_key,
        "rendered_evidence_chars": len(prompt.rendered_evidence),
        "status": "success",
        "attempt_count": attempt_count,
        "retry_failure_count": len(retry_failures or []),
        "retry_failures": retry_failures or [],
        "reused_from_existing": False,
        "Analysis": result.response.Analysis,
        "State_Definitions": [
            predicate.model_dump(exclude_none=True)
            for predicate in result.response.State_Definitions
        ],
        "usage": result_usage_dict(result),
    }


def _write_chunk_output(output_dir: Path, record: dict[str, Any]) -> None:
    write_json(output_dir / "chunk_outputs" / f"{record['chunk_id']}.json", record)


def _load_reusable_chunk_output(
    output_dir: Path,
    prompt: ContextWindowChunkPrompt,
) -> dict[str, Any] | None:
    path = output_dir / "chunk_outputs" / f"{prompt.chunk_id}.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    if str(record.get("chunk_id") or "") != prompt.chunk_id:
        return None
    if record.get("prompt_cache_key") != prompt.prompt_cache_key:
        return None
    if not isinstance(record.get("State_Definitions"), list):
        return None
    if record.get("status") == "failure":
        return None
    return record


def _mark_reused_chunk_record(record: dict[str, Any]) -> dict[str, Any]:
    reused = dict(record)
    reused["reused_from_existing"] = True
    reused["resume_reused_at"] = iso_now()
    return reused


def _result_from_chunk_output_record(
    record: dict[str, Any],
    *,
    model: str,
    base_variant: int,
) -> LLMResult:
    response = PredicateResponse(
        Analysis=str(record.get("Analysis") or ""),
        State_Definitions=[
            StatePredicate.model_validate(item)
            for item in record.get("State_Definitions", [])
            if isinstance(item, dict)
        ],
    )
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    request_meta = usage.get("request") if isinstance(usage, dict) else None
    if isinstance(request_meta, dict):
        request_meta = {
            **request_meta,
            "reused_from_existing_chunk_output": True,
            "reused_chunk_id": record.get("chunk_id"),
        }
    else:
        request_meta = {
            "api": "context_window_chunked_reused_chunk",
            "status": "success",
            "reused_from_existing_chunk_output": True,
            "reused_chunk_id": record.get("chunk_id"),
        }
    return LLMResult(
        model=model,
        variant=base_variant,
        response=response,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_sec=float(usage.get("latency_sec") or 0.0),
        cached_tokens=int(usage.get("cached_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
        request_meta=request_meta,
        raw_json=response.model_dump_json(indent=2),
        messages_sent=[],
    )


def _query_chunk_with_retry(
    *,
    output_dir: Path,
    prompt: ContextWindowChunkPrompt,
    resolved_query: ContextWindowChunkQueryFn,
    screenshot_path: Path,
    base_variant: int,
    model: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
    retry_max_delay: float,
) -> tuple[LLMResult, list[dict[str, Any]]]:
    attempts = max(1, int(max_attempts or 1))
    retry_failures: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        try:
            result = _call_chunk_query(
                prompt=prompt,
                resolved_query=resolved_query,
                screenshot_path=screenshot_path,
                base_variant=base_variant,
                model=model,
                timeout=timeout,
            )
        except Exception as exc:
            failure, should_retry, delay = _record_chunk_query_failure(
                output_dir=output_dir,
                prompt=prompt,
                exc=exc,
                attempt=attempt,
                max_attempts=attempts,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            retry_failures.append(failure)
            if not should_retry:
                raise
            _sleep_before_chunk_retry(prompt, exc, attempt, attempts, delay)
            continue
        return result, retry_failures
    raise RuntimeError(f"unreachable retry loop exit for {prompt.chunk_id}")


def _call_chunk_query(
    *,
    prompt: ContextWindowChunkPrompt,
    resolved_query: ContextWindowChunkQueryFn,
    screenshot_path: Path,
    base_variant: int,
    model: str,
    timeout: float,
) -> LLMResult:
    return resolved_query(
        system_prompt=prompt.system_prompt,
        user_prompt=prompt.user_prompt,
        screenshot_path=screenshot_path,
        variant=base_variant,
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt.prompt_cache_key,
    )


def _record_chunk_query_failure(
    *,
    output_dir: Path,
    prompt: ContextWindowChunkPrompt,
    exc: BaseException,
    attempt: int,
    max_attempts: int,
    retry_base_delay: float,
    retry_max_delay: float,
) -> tuple[dict[str, Any], bool, float | None]:
    _attach_failure_prompts(exc, prompt)
    retryable = _is_retryable_chunk_error(exc)
    should_retry = retryable and attempt < max_attempts
    delay = (
        _retry_delay_seconds(
            exc,
            attempt=attempt,
            base_delay=retry_base_delay,
            max_delay=retry_max_delay,
        )
        if should_retry
        else None
    )
    failure = _chunk_failure_record(
        prompt=prompt,
        exc=exc,
        attempt=attempt,
        max_attempts=max_attempts,
        retryable=retryable,
        next_retry_delay_sec=delay,
    )
    _write_chunk_failure_output(output_dir, record=failure)
    return failure, should_retry, delay


def _sleep_before_chunk_retry(
    prompt: ContextWindowChunkPrompt,
    exc: BaseException,
    attempt: int,
    max_attempts: int,
    delay: float | None,
) -> None:
    sleep = float(delay or 0.0)
    log(
        f"[context_window_chunked] retry {prompt.chunk_id} "
        f"attempt={attempt}/{max_attempts} error={type(exc).__name__} "
        f"sleep={sleep:g}s",
        "yellow",
    )
    if sleep > 0:
        time.sleep(sleep)


def _chunk_failure_record(
    *,
    prompt: ContextWindowChunkPrompt,
    exc: BaseException,
    attempt: int,
    max_attempts: int,
    retryable: bool,
    next_retry_delay_sec: float | None,
) -> dict[str, Any]:
    return {
        "chunk_id": prompt.chunk_id,
        "prompt_cache_key": prompt.prompt_cache_key,
        "rendered_evidence_chars": len(prompt.rendered_evidence),
        "status": "failure",
        "attempt": attempt,
        "max_attempts": max_attempts,
        "retryable": retryable,
        "next_retry_delay_sec": next_retry_delay_sec,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "request": attached_request_meta(exc),
        "failed_at": iso_now(),
    }


def _write_chunk_failure_output(
    output_dir: Path,
    *,
    record: dict[str, Any],
) -> None:
    write_json(
        output_dir / "chunk_outputs" / f"{record['chunk_id']}.failure.json",
        record,
    )


def _attach_failure_prompts(
    exc: BaseException,
    prompt: ContextWindowChunkPrompt,
) -> None:
    setattr(exc, "aifc_system_prompt", prompt.system_prompt)
    setattr(exc, "aifc_user_prompt", prompt.user_prompt)
    setattr(exc, "aifc_chunk_id", prompt.chunk_id)
    setattr(exc, "aifc_prompt_cache_key", prompt.prompt_cache_key)


def _is_retryable_chunk_error(exc: BaseException) -> bool:
    error_type = type(exc).__name__
    if error_type in {
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "RateLimitError",
    }:
        return True
    meta = attached_request_meta(exc) or {}
    status_code = _request_status_code(meta)
    if status_code in {408, 409, 429, 520, 522, 524}:
        return True
    if status_code is not None and status_code >= 500:
        return True
    return False


def _request_status_code(meta: dict[str, Any]) -> int | None:
    for key in ("status_code", "http_status"):
        value = meta.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    response = meta.get("response")
    if isinstance(response, dict):
        try:
            return int(response.get("status_code"))
        except (TypeError, ValueError):
            return None
    return None


def _retry_delay_seconds(
    exc: BaseException,
    *,
    attempt: int,
    base_delay: float,
    max_delay: float,
) -> float:
    bounded_base = max(0.0, float(base_delay))
    bounded_max = max(bounded_base, float(max_delay))
    backoff = min(bounded_base * (2 ** max(attempt - 1, 0)), bounded_max)
    retry_after = _retry_after_from_exception(exc)
    if retry_after is not None:
        return min(max(backoff, retry_after), bounded_max)
    return backoff


def _retry_after_from_exception(exc: BaseException) -> float | None:
    meta = attached_request_meta(exc) or {}
    for candidate in (
        meta.get("retry_after"),
        (meta.get("headers") or {}).get("retry-after")
        if isinstance(meta.get("headers"), dict)
        else None,
    ):
        try:
            return float(candidate)
        except (TypeError, ValueError):
            pass
    match = re.search(r"['\"]retry_after['\"]\s*:\s*([0-9]+(?:\.[0-9]+)?)", str(exc))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _chunk_execution_meta(chunk_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": record.get("chunk_id"),
            "reused_from_existing": bool(record.get("reused_from_existing")),
            "attempt_count": int(record.get("attempt_count") or 1),
            "retry_failure_count": int(record.get("retry_failure_count") or 0),
            "prompt_cache_key": record.get("prompt_cache_key"),
        }
        for record in chunk_outputs
    ]


def _merge_chunk_results(
    *,
    results: list[LLMResult],
    chunk_outputs: list[dict[str, Any]],
    run_id: str,
    page: int,
    variant_key: str,
    base_variant: int,
    model: str,
    prompt_strategy: str,
    prompt_cache_key: str | None,
    chunks_manifest: dict[str, Any],
    chunk_execution: list[dict[str, Any]],
) -> tuple[LLMResult, dict[str, Any]]:
    merged_defs: list[dict[str, Any]] = []
    chunk_predicate_counts: list[dict[str, Any]] = []
    for record in chunk_outputs:
        chunk_defs = [
            item
            for item in record.get("State_Definitions", [])
            if isinstance(item, dict)
        ]
        before_count = len(merged_defs)
        merged_defs = merge_state_definitions(merged_defs, chunk_defs)
        chunk_predicate_counts.append(
            {
                "chunk_id": record.get("chunk_id"),
                "predicate_count": len(chunk_defs),
                "merged_predicate_count_before": before_count,
                "merged_predicate_count_after": len(merged_defs),
            }
        )
    response = PredicateResponse(
        Analysis=_merged_analysis(chunk_outputs),
        State_Definitions=[
            StatePredicate.model_validate(item) for item in merged_defs
        ],
    )
    raw_json = response.model_dump_json(indent=2)
    usage = aggregate_result_usage(results)
    request_meta = {
        "api": "context_window_chunked_aggregate",
        "status": "success",
        "run_id": run_id,
        "page": page,
        "variant": variant_key,
        "base_variant": base_variant,
        "prompt_strategy": prompt_strategy,
        "prompt_cache_key": prompt_cache_key,
        "chunk_count": len(results),
        "requests": usage.get("requests", []),
    }
    final_result = LLMResult(
        model=model,
        variant=base_variant,
        response=response,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_sec=float(usage.get("latency_sec") or 0.0),
        cached_tokens=int(usage.get("cached_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
        request_meta=request_meta,
        raw_json=raw_json,
        messages_sent=[],
    )
    merge_meta = _success_merge_meta(
        variant_key=variant_key,
        base_variant=base_variant,
        chunks_manifest=chunks_manifest,
        chunk_predicate_counts=chunk_predicate_counts,
        final_predicate_count=len(response.State_Definitions),
        chunk_execution=chunk_execution,
    )
    return final_result, merge_meta


def _merged_analysis(chunk_outputs: list[dict[str, Any]]) -> str:
    lines = ["Merged independent context-window chunk responses."]
    for record in chunk_outputs:
        analysis = str(record.get("Analysis") or "").strip()
        if analysis:
            lines.append(f"[{record.get('chunk_id')}] {analysis}")
    return "\n".join(lines)


def _success_merge_meta(
    *,
    variant_key: str,
    base_variant: int,
    chunks_manifest: dict[str, Any],
    chunk_predicate_counts: list[dict[str, Any]],
    final_predicate_count: int,
    chunk_execution: list[dict[str, Any]],
) -> dict[str, Any]:
    duplicate_stats = _predicate_duplicate_stats(chunk_predicate_counts)
    return {
        "status": "success",
        "variant": variant_key,
        "base_variant": base_variant,
        "merge_policy": "sequential_merge_state_definitions_by_manifest_order",
        "chunk_count": _manifest_chunk_count(chunks_manifest),
        "chunk_predicate_counts": chunk_predicate_counts,
        "chunk_execution": chunk_execution,
        "final_predicate_count": final_predicate_count,
        "duplicate_merge_stats": duplicate_stats,
        "source_chunk_ids": _manifest_chunk_ids(chunks_manifest),
        "domain_attribution": chunks_manifest.get("domain_attribution", {}),
        "shared_evidence_duplicates": chunks_manifest.get(
            "shared_evidence_duplicates",
            {},
        ),
    }


def _prepared_merge_meta(
    *,
    variant_key: str,
    base_variant: int,
    chunks_manifest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "prepared",
        "variant": variant_key,
        "base_variant": base_variant,
        "merge_policy": "sequential_merge_state_definitions_by_manifest_order",
        "chunk_count": _manifest_chunk_count(chunks_manifest),
        "source_chunk_ids": _manifest_chunk_ids(chunks_manifest),
        "domain_attribution": chunks_manifest.get("domain_attribution", {}),
        "shared_evidence_duplicates": chunks_manifest.get(
            "shared_evidence_duplicates",
            {},
        ),
    }


def _predicate_duplicate_stats(
    chunk_predicate_counts: list[dict[str, Any]],
) -> dict[str, int]:
    total_chunk_predicates = sum(
        int(record.get("predicate_count") or 0)
        for record in chunk_predicate_counts
    )
    final_count = (
        int(chunk_predicate_counts[-1].get("merged_predicate_count_after") or 0)
        if chunk_predicate_counts
        else 0
    )
    return {
        "chunk_predicate_occurrence_count": total_chunk_predicates,
        "final_predicate_count": final_count,
        "merged_duplicate_or_update_count": max(total_chunk_predicates - final_count, 0),
    }


def _manifest_chunk_ids(chunks_manifest: dict[str, Any]) -> list[str]:
    return [
        str(chunk.get("chunk_id"))
        for chunk in chunks_manifest.get("chunks", [])
        if isinstance(chunk, dict) and chunk.get("chunk_id")
    ]


def _manifest_chunk_count(chunks_manifest: dict[str, Any]) -> int:
    top_level = chunks_manifest.get("chunk_count")
    if top_level is not None:
        try:
            return int(top_level)
        except (TypeError, ValueError):
            pass
    stats = chunks_manifest.get("stats") or {}
    if isinstance(stats, dict):
        try:
            return int(stats.get("chunk_count") or 0)
        except (TypeError, ValueError):
            pass
    return len(_manifest_chunk_ids(chunks_manifest))


def _cleanup_stale_failure_artifacts(output_dir: Path) -> dict[str, Any]:
    removed: list[str] = []
    for path in [
        output_dir / "failure_meta.json",
        *sorted((output_dir / "chunk_outputs").glob("*.failure.json")),
    ]:
        if path.exists() and path.is_file():
            path.unlink()
            removed.append(str(path))
    artifacts_dir = output_dir / "failure_artifacts"
    if artifacts_dir.exists() and artifacts_dir.is_dir():
        shutil.rmtree(artifacts_dir)
        removed.append(str(artifacts_dir))
    return {
        "removed_count": len(removed),
        "removed_paths": removed,
    }


def _write_final_outputs(
    *,
    output_dir: Path,
    result: LLMResult,
    merge_meta: dict[str, Any],
    run_id: str,
    page: int,
    variant_key: str,
    base_variant: int,
    model: str,
    prompt_strategy: str,
    prompt_cache_key: str | None,
    target_chars: int,
    max_chars: int,
    domain_attribution_policy: str,
    chunk_max_attempts: int,
    resume_existing_chunks: bool,
) -> None:
    write_json(
        output_dir / "response_parsed.json",
        {
            "Analysis": result.response.Analysis,
            "State_Definitions": [
                predicate.model_dump(exclude_none=True)
                for predicate in result.response.State_Definitions
            ],
        },
    )
    (output_dir / "response_raw.txt").write_text(result.raw_json, encoding="utf-8")
    usage = result_usage_dict(result)
    write_json(output_dir / "usage.json", usage)
    write_json(output_dir / "merge_meta.json", merge_meta)
    write_json(
        output_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": base_variant,
            "page": page,
            "model": model,
            "lane": "reproduction",
            "status": "success",
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "api_called": True,
            "chunk_count": int(merge_meta.get("chunk_count") or 0),
            "target_chars": target_chars,
            "max_chars": max_chars,
            "domain_attribution_policy": domain_attribution_policy,
            "chunk_max_attempts": chunk_max_attempts,
            "resume_existing_chunks": resume_existing_chunks,
            "latency_sec": round(result.latency_sec, 3),
            "predicate_count": len(result.response.State_Definitions),
            "request": result.request_meta or None,
        },
    )


def _prepared_paths(
    output_dir: Path,
    chunks_manifest: dict[str, Any],
    merge_meta: dict[str, Any],
    *,
    prompt_strategy: str,
    target_chars: int,
    max_chars: int,
) -> dict[str, Any]:
    return {
        "prompt_system": str(output_dir / "prompt_system.txt"),
        "prompt_user": str(output_dir / "prompt_user.txt"),
        "response_parsed": None,
        "response_raw": None,
        "usage": None,
        "run_meta": str(output_dir / "run_meta.json"),
        "chunks_manifest": str(output_dir / "chunks_manifest.json"),
        "context_evidence_index": str(output_dir / "context_evidence_index.json"),
        "merge_meta": str(output_dir / "merge_meta.json"),
        "chunk_prompts": str(output_dir / "chunk_prompts"),
        "chunk_outputs": str(output_dir / "chunk_outputs"),
        "status": "prepared",
        "prepare_only": True,
        "api_called": False,
        "prompt_strategy": prompt_strategy,
        "chunk_count": _manifest_chunk_count(chunks_manifest),
        "target_chars": target_chars,
        "max_chars": max_chars,
        "predicate_count": None,
        "domain_attribution": merge_meta.get("domain_attribution", {}),
    }


def _run_paths(
    output_dir: Path,
    result: LLMResult,
    chunks_manifest: dict[str, Any],
    merge_meta: dict[str, Any],
    *,
    prompt_strategy: str,
    target_chars: int,
    max_chars: int,
) -> dict[str, Any]:
    usage = result_usage_dict(result, include_request=False)
    return {
        "prompt_system": str(output_dir / "prompt_system.txt"),
        "prompt_user": str(output_dir / "prompt_user.txt"),
        "response_parsed": str(output_dir / "response_parsed.json"),
        "response_raw": str(output_dir / "response_raw.txt"),
        "usage": str(output_dir / "usage.json"),
        "run_meta": str(output_dir / "run_meta.json"),
        "chunks_manifest": str(output_dir / "chunks_manifest.json"),
        "context_evidence_index": str(output_dir / "context_evidence_index.json"),
        "merge_meta": str(output_dir / "merge_meta.json"),
        "chunk_prompts": str(output_dir / "chunk_prompts"),
        "chunk_outputs": str(output_dir / "chunk_outputs"),
        "status": "success",
        "api_called": True,
        "prompt_strategy": prompt_strategy,
        "chunk_count": _manifest_chunk_count(chunks_manifest),
        "target_chars": target_chars,
        "max_chars": max_chars,
        "predicate_count": len(result.response.State_Definitions),
        "latency_sec": usage["latency_sec"],
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "cached_tokens": usage["cached_tokens"],
        "cache_hit_rate": usage["cache_hit_rate"],
        "domain_attribution": merge_meta.get("domain_attribution", {}),
    }
