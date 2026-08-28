"""Context-window chunked V2chunked static critic runner.

This runner keeps the V2chunked Step 1 result fixed and splits only the
Step 2 static-analysis evidence across independent critic calls.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ..fusion.context_window import (
    build_context_evidence_index,
    pack_evidence_graph_chunks,
    render_chunk_evidence,
    slim_chunks_manifest,
)
from ..utils import iso_now, log, write_json
from .api_diagnostics import (
    aggregate_result_usage,
    attached_request_meta,
    result_usage_dict,
)
from .critic_runner import (
    DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
    CriticResponse,
    CriticResult,
    PredicateVerdict,
    apply_monotone_decrease,
    build_critic_user_prompt,
    predicate_id,
    query_critic_in_session,
    variable_id,
)
from .llm_client import LLMResult, StatePredicate

ChunkedCriticQueryFn = Callable[..., CriticResult]

DEFAULT_CHUNK_MAX_ATTEMPTS = 3
DEFAULT_CHUNK_RETRY_BASE_DELAY_SEC = 30.0
DEFAULT_CHUNK_RETRY_MAX_DELAY_SEC = 120.0
NO_STATIC_EVIDENCE_REASON = "no_static_evidence_chunks"


@dataclass(frozen=True)
class ContextWindowCriticChunkPrompt:
    chunk_id: str
    rendered_evidence: str
    analysis_payload: str
    critic_prompt: str
    prompt_cache_key: str | None


@dataclass(frozen=True)
class ContextWindowCriticPreparedResult:
    chunks_manifest: dict[str, Any]
    merge_meta: dict[str, Any]
    paths: dict[str, Any]


@dataclass(frozen=True)
class ContextWindowCriticRunResult:
    final_result: LLMResult
    chunks_manifest: dict[str, Any]
    merge_meta: dict[str, Any]
    chunk_outputs: list[dict[str, Any]]
    paths: dict[str, Any]


def prepare_v2chunked_context_window_critic(
    *,
    output_dir: Path,
    run_id: str,
    page: int,
    variant_key: str,
    prompt_strategy: str,
    system_prompt: str,
    step1_user_prompt: str,
    step1_result: LLMResult | None,
    prompts_dir: Path,
    input_dir: Path,
    source_step1_page_dir: Path,
    sliced_methods_payload: dict[str, Any],
    static_analysis_payload: dict[str, Any],
    model: str,
    prompt_cache_key: str | None,
    domain_attribution_policy: str,
    target_chars: int = 400000,
    max_chars: int = 500000,
    step1_source: str = "integrated_v2chunked",
    step1_api_called: bool = False,
) -> ContextWindowCriticPreparedResult:
    """Create chunked critic prompts/artifacts without calling the API."""
    bundle = _prepare_critic_chunk_bundle(
        output_dir=output_dir,
        variant_key=variant_key,
        page=page,
        step1_predicates=(
            step1_result.response.State_Definitions if step1_result is not None else []
        ),
        sliced_methods_payload=sliced_methods_payload,
        static_analysis_payload=static_analysis_payload,
        prompts_dir=prompts_dir,
        prompt_cache_key=prompt_cache_key,
        domain_attribution_policy=domain_attribution_policy,
        target_chars=target_chars,
        max_chars=max_chars,
    )
    merge_meta = _prepared_merge_meta(
        variant_key=variant_key,
        chunks_manifest=bundle["chunks_manifest"],
    )
    write_json(output_dir / "merge_meta.json", merge_meta)
    _write_prepared_outputs(
        output_dir=output_dir,
        run_id=run_id,
        page=page,
        variant_key=variant_key,
        prompt_strategy=prompt_strategy,
        system_prompt=system_prompt,
        step1_user_prompt=step1_user_prompt,
        step1_result=step1_result,
        input_dir=input_dir,
        source_step1_page_dir=source_step1_page_dir,
        model=model,
        prompt_cache_key=prompt_cache_key,
        domain_attribution_policy=domain_attribution_policy,
        target_chars=target_chars,
        max_chars=max_chars,
        step1_source=step1_source,
        step1_api_called=step1_api_called,
        chunk_count=len(bundle["prompts"]),
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
        f"[context_window_critic] variant={variant_key} page={page} "
        f"prepared chunks={len(bundle['prompts'])}",
        "cyan",
    )
    return ContextWindowCriticPreparedResult(
        chunks_manifest=bundle["chunks_manifest"],
        merge_meta=merge_meta,
        paths=paths,
    )


def run_v2chunked_context_window_critic(
    *,
    output_dir: Path,
    run_id: str,
    page: int,
    variant_key: str,
    prompt_strategy: str,
    system_prompt: str,
    step1_user_prompt: str,
    step1_result: LLMResult,
    session_messages: list[dict[str, Any]],
    prompts_dir: Path,
    input_dir: Path,
    source_step1_page_dir: Path,
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
    step1_source: str = "integrated_v2chunked",
    step1_api_called: bool = False,
    query_fn: ChunkedCriticQueryFn | None = None,
) -> ContextWindowCriticRunResult:
    """Run chunked static critic calls and merge explicit drop verdicts."""
    bundle = _prepare_critic_chunk_bundle(
        output_dir=output_dir,
        variant_key=variant_key,
        page=page,
        step1_predicates=step1_result.response.State_Definitions,
        sliced_methods_payload=sliced_methods_payload,
        static_analysis_payload=static_analysis_payload,
        prompts_dir=prompts_dir,
        prompt_cache_key=prompt_cache_key,
        domain_attribution_policy=domain_attribution_policy,
        target_chars=target_chars,
        max_chars=max_chars,
    )
    resolved_query = query_fn or query_critic_in_session
    if not bundle["prompts"]:
        final_result, merge_meta, chunk_outputs = _skip_no_static_evidence(
            step1_result=step1_result,
            variant_key=variant_key,
            chunks_manifest=bundle["chunks_manifest"],
        )
    else:
        chunk_outputs = []
        critic_results: list[CriticResult] = []
        for prompt in bundle["prompts"]:
            existing_record = _load_reusable_chunk_output(output_dir, prompt)
            if resume_existing_chunks and existing_record is not None:
                record = _mark_reused_chunk_record(existing_record)
                critic_result = _critic_result_from_chunk_output_record(
                    record,
                    model=model,
                )
                log(
                    f"[context_window_critic] reuse {variant_key} page={page} "
                    f"{prompt.chunk_id}",
                    "cyan",
                )
            else:
                critic_result, retry_failures = _query_chunk_with_retry(
                    output_dir=output_dir,
                    prompt=prompt,
                    resolved_query=resolved_query,
                    system_prompt=system_prompt,
                    session_messages=session_messages,
                    model=model,
                    timeout=timeout,
                    max_attempts=chunk_max_attempts,
                    retry_base_delay=chunk_retry_base_delay,
                    retry_max_delay=chunk_retry_max_delay,
                )
                record = _chunk_output_record(
                    prompt=prompt,
                    result=critic_result,
                    attempt_count=len(retry_failures) + 1,
                    retry_failures=retry_failures,
                )
                _write_chunk_output(output_dir, record)
            chunk_outputs.append(record)
            critic_results.append(critic_result)
        final_result, merge_meta = _merge_chunked_critic_results(
            step1_result=step1_result,
            critic_results=critic_results,
            chunk_outputs=chunk_outputs,
            run_id=run_id,
            page=page,
            variant_key=variant_key,
            model=model,
            prompt_strategy=prompt_strategy,
            prompt_cache_key=prompt_cache_key,
            chunks_manifest=bundle["chunks_manifest"],
            chunk_execution=_chunk_execution_meta(chunk_outputs),
        )
    cleanup_meta = _cleanup_stale_failure_artifacts(output_dir)
    merge_meta["stale_failure_cleanup"] = cleanup_meta
    _write_run_outputs(
        output_dir=output_dir,
        run_id=run_id,
        page=page,
        variant_key=variant_key,
        prompt_strategy=prompt_strategy,
        system_prompt=system_prompt,
        step1_user_prompt=step1_user_prompt,
        step1_result=step1_result,
        final_result=final_result,
        merge_meta=merge_meta,
        chunk_outputs=chunk_outputs,
        input_dir=input_dir,
        source_step1_page_dir=source_step1_page_dir,
        model=model,
        prompt_cache_key=prompt_cache_key,
        domain_attribution_policy=domain_attribution_policy,
        target_chars=target_chars,
        max_chars=max_chars,
        chunk_max_attempts=chunk_max_attempts,
        resume_existing_chunks=resume_existing_chunks,
        step1_source=step1_source,
        step1_api_called=step1_api_called,
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
        f"[context_window_critic] variant={variant_key} page={page} "
        f"chunks={len(bundle['prompts'])} defs={len(final_result.response.State_Definitions)}",
        "green",
    )
    return ContextWindowCriticRunResult(
        final_result=final_result,
        chunks_manifest=bundle["chunks_manifest"],
        merge_meta=merge_meta,
        chunk_outputs=chunk_outputs,
        paths=paths,
    )


def _prepare_critic_chunk_bundle(
    *,
    output_dir: Path,
    variant_key: str,
    page: int,
    step1_predicates: list[StatePredicate],
    sliced_methods_payload: dict[str, Any],
    static_analysis_payload: dict[str, Any],
    prompts_dir: Path,
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
    manifest = slim_chunks_manifest(packed)
    write_json(output_dir / "context_evidence_index.json", index)
    write_json(output_dir / "chunks_manifest.json", manifest)
    prompts = [
        _build_chunk_prompt(
            chunk=chunk,
            variant_key=variant_key,
            page=page,
            step1_predicates=step1_predicates,
            prompts_dir=prompts_dir,
            prompt_cache_key=prompt_cache_key,
        )
        for chunk in packed["chunks"]
    ]
    for prompt in prompts:
        _write_chunk_prompt(output_dir, prompt)
    _write_root_prompt_summary(
        output_dir=output_dir,
        variant_key=variant_key,
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
    page: int,
    step1_predicates: list[StatePredicate],
    prompts_dir: Path,
    prompt_cache_key: str | None,
) -> ContextWindowCriticChunkPrompt:
    rendered = render_chunk_evidence(chunk, mode="critic")
    analysis_payload = rendered
    critic_prompt = build_critic_user_prompt(
        step1_predicates=step1_predicates,
        analysis_payload=analysis_payload,
        prompts_dir=prompts_dir,
    )
    chunk_id = str(chunk["chunk_id"])
    return ContextWindowCriticChunkPrompt(
        chunk_id=chunk_id,
        rendered_evidence=rendered,
        analysis_payload=analysis_payload,
        critic_prompt=critic_prompt,
        prompt_cache_key=_chunk_prompt_cache_key(
            base_key=prompt_cache_key,
            variant_key=variant_key,
            page=page,
            chunk_id=chunk_id,
            critic_prompt=critic_prompt,
        ),
    )


def _chunk_prompt_cache_key(
    *,
    base_key: str | None,
    variant_key: str,
    page: int,
    chunk_id: str,
    critic_prompt: str,
) -> str | None:
    if base_key is None or not base_key.strip():
        return None
    sha12 = hashlib.sha256(critic_prompt.encode("utf-8")).hexdigest()[:12]
    return f"{base_key.strip()}:{variant_key}:page_{page}:{chunk_id}:{sha12}"


def _write_chunk_prompt(
    output_dir: Path,
    prompt: ContextWindowCriticChunkPrompt,
) -> None:
    path = output_dir / "chunk_prompts" / f"{prompt.chunk_id}_step2_critic.txt"
    path.write_text(prompt.critic_prompt, encoding="utf-8")


def _write_root_prompt_summary(
    *,
    output_dir: Path,
    variant_key: str,
    prompts: list[ContextWindowCriticChunkPrompt],
) -> None:
    lines = [
        "Context-window chunked V2chunked static critic summary.",
        "",
        f"variant: {variant_key}",
        f"chunk_count: {len(prompts)}",
        "",
        "Actual critic API prompts are stored under chunk_prompts/.",
    ]
    for prompt in prompts:
        lines.append(
            f"- {prompt.chunk_id}: "
            f"chunk_prompts/{prompt.chunk_id}_step2_critic.txt"
        )
    (output_dir / "prompt_user_step2_critic.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _query_chunk_with_retry(
    *,
    output_dir: Path,
    prompt: ContextWindowCriticChunkPrompt,
    resolved_query: ChunkedCriticQueryFn,
    system_prompt: str,
    session_messages: list[dict[str, Any]],
    model: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
    retry_max_delay: float,
) -> tuple[CriticResult, list[dict[str, Any]]]:
    attempts = max(1, int(max_attempts or 1))
    retry_failures: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        try:
            result = resolved_query(
                system_prompt=system_prompt,
                session_messages=session_messages,
                critic_user_prompt=prompt.critic_prompt,
                model=model,
                timeout=timeout,
                prompt_cache_key=prompt.prompt_cache_key,
                max_completion_tokens=DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
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


def _record_chunk_query_failure(
    *,
    output_dir: Path,
    prompt: ContextWindowCriticChunkPrompt,
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
    prompt: ContextWindowCriticChunkPrompt,
    exc: BaseException,
    attempt: int,
    max_attempts: int,
    delay: float | None,
) -> None:
    sleep = float(delay or 0.0)
    log(
        f"[context_window_critic] retry {prompt.chunk_id} "
        f"attempt={attempt}/{max_attempts} error={type(exc).__name__} "
        f"sleep={sleep:g}s",
        "yellow",
    )
    if sleep > 0:
        time.sleep(sleep)


def _chunk_failure_record(
    *,
    prompt: ContextWindowCriticChunkPrompt,
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
        "critic_prompt_chars": len(prompt.critic_prompt),
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
        output_dir / "chunk_outputs" / f"{record['chunk_id']}_critic.failure.json",
        record,
    )


def _attach_failure_prompts(
    exc: BaseException,
    prompt: ContextWindowCriticChunkPrompt,
) -> None:
    setattr(exc, "aifc_critic_prompt", prompt.critic_prompt)
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


def _chunk_output_record(
    *,
    prompt: ContextWindowCriticChunkPrompt,
    result: CriticResult,
    attempt_count: int = 1,
    retry_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "chunk_id": prompt.chunk_id,
        "prompt_cache_key": prompt.prompt_cache_key,
        "rendered_evidence_chars": len(prompt.rendered_evidence),
        "critic_prompt_chars": len(prompt.critic_prompt),
        "status": "success",
        "attempt_count": attempt_count,
        "retry_failure_count": len(retry_failures or []),
        "retry_failures": retry_failures or [],
        "reused_from_existing": False,
        "response": result.response.model_dump(),
        "verdicts": [verdict.model_dump() for verdict in result.response.verdicts],
        "usage": _critic_usage_dict(result),
        "request": result.request_meta or None,
        "raw_json": result.raw_json,
    }


def _write_chunk_output(output_dir: Path, record: dict[str, Any]) -> None:
    write_json(
        output_dir / "chunk_outputs" / f"{record['chunk_id']}_critic.json",
        record,
    )


def _load_reusable_chunk_output(
    output_dir: Path,
    prompt: ContextWindowCriticChunkPrompt,
) -> dict[str, Any] | None:
    path = output_dir / "chunk_outputs" / f"{prompt.chunk_id}_critic.json"
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
    if not isinstance(record.get("verdicts"), list):
        return None
    if record.get("status") == "failure":
        return None
    return record


def _mark_reused_chunk_record(record: dict[str, Any]) -> dict[str, Any]:
    reused = dict(record)
    reused["reused_from_existing"] = True
    reused["resume_reused_at"] = iso_now()
    return reused


def _critic_result_from_chunk_output_record(
    record: dict[str, Any],
    *,
    model: str,
) -> CriticResult:
    response_payload = record.get("response")
    if not isinstance(response_payload, dict):
        response_payload = {"verdicts": record.get("verdicts", [])}
    response = CriticResponse.model_validate(response_payload)
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
            "api": "context_window_critic_reused_chunk",
            "status": "success",
            "reused_from_existing_chunk_output": True,
            "reused_chunk_id": record.get("chunk_id"),
        }
    return CriticResult(
        model=model,
        response=response,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_sec=float(usage.get("latency_sec") or 0.0),
        cached_tokens=int(usage.get("cached_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
        max_completion_tokens=usage.get("max_completion_tokens"),
        request_meta=request_meta,
        raw_json=str(record.get("raw_json") or response.model_dump_json(indent=2)),
        messages_sent=[],
    )


def _merge_chunked_critic_results(
    *,
    step1_result: LLMResult,
    critic_results: list[CriticResult],
    chunk_outputs: list[dict[str, Any]],
    run_id: str,
    page: int,
    variant_key: str,
    model: str,
    prompt_strategy: str,
    prompt_cache_key: str | None,
    chunks_manifest: dict[str, Any],
    chunk_execution: list[dict[str, Any]],
) -> tuple[LLMResult, dict[str, Any]]:
    merged_response, verdict_meta = _merged_drop_response(
        step1_result.response.State_Definitions,
        chunk_outputs,
    )
    final_predicates = apply_monotone_decrease(
        step1_result.response.State_Definitions,
        merged_response,
    )
    final_result = _final_llm_result(
        step1_result=step1_result,
        final_predicates=final_predicates,
        critic_results=critic_results,
        run_id=run_id,
        page=page,
        variant_key=variant_key,
        model=model,
        prompt_strategy=prompt_strategy,
        prompt_cache_key=prompt_cache_key,
    )
    merge_meta = {
        "status": "success",
        "variant": variant_key,
        "base_variant": 2,
        "merge_policy": (
            "union_explicit_drop_verdicts_only_keep_verdicts_do_not_override"
        ),
        "chunk_count": _manifest_chunk_count(chunks_manifest),
        "source_chunk_ids": _manifest_chunk_ids(chunks_manifest),
        "chunk_execution": chunk_execution,
        "drop_count": verdict_meta["drop_count"],
        "keep_verdict_count": verdict_meta["keep_verdict_count"],
        "invalid_or_out_of_scope_verdict_count": verdict_meta[
            "invalid_or_out_of_scope_verdict_count"
        ],
        "missing_verdict_policy": "keep",
        "absence_of_evidence_policy": "keep",
        "critic_skipped_reason": None,
        "critic_api_called": True,
        "critic_applied": True,
        "step1_predicate_count": len(step1_result.response.State_Definitions),
        "final_predicate_count": len(final_result.response.State_Definitions),
        "domain_attribution": chunks_manifest.get("domain_attribution", {}),
        "shared_evidence_duplicates": chunks_manifest.get(
            "shared_evidence_duplicates",
            {},
        ),
        "merged_drop_verdicts": verdict_meta["merged_drop_verdicts"],
        "chunk_verdict_counts": verdict_meta["chunk_verdict_counts"],
    }
    return final_result, merge_meta


def _merged_drop_response(
    step1_predicates: list[StatePredicate],
    chunk_outputs: list[dict[str, Any]],
) -> tuple[CriticResponse, dict[str, Any]]:
    valid_pairs = {
        (predicate_id(pred_index), variable_id(var_index))
        for pred_index, pred in enumerate(step1_predicates)
        for var_index, _ in enumerate(pred.variables)
    }
    drops: dict[tuple[str, str], dict[str, Any]] = {}
    keep_count = 0
    invalid_count = 0
    chunk_verdict_counts: list[dict[str, Any]] = []
    for record in chunk_outputs:
        chunk_id = str(record.get("chunk_id") or "")
        chunk_drop_count = 0
        chunk_keep_count = 0
        for verdict in _record_verdicts(record):
            pair = (verdict.predicate_id, verdict.variable_id)
            if pair not in valid_pairs:
                invalid_count += 1
                continue
            if verdict.verdict == "keep":
                keep_count += 1
                chunk_keep_count += 1
                continue
            chunk_drop_count += 1
            entry = drops.setdefault(
                pair,
                {
                    "predicate_id": verdict.predicate_id,
                    "variable_id": verdict.variable_id,
                    "verdict": "drop",
                    "evidence": verdict.evidence,
                    "source_chunk_ids": [],
                    "evidence_by_chunk": [],
                },
            )
            if chunk_id and chunk_id not in entry["source_chunk_ids"]:
                entry["source_chunk_ids"].append(chunk_id)
            entry["evidence_by_chunk"].append(
                {
                    "chunk_id": chunk_id,
                    "evidence": verdict.evidence,
                }
            )
        chunk_verdict_counts.append(
            {
                "chunk_id": chunk_id,
                "drop_count": chunk_drop_count,
                "keep_count": chunk_keep_count,
                "verdict_count": chunk_drop_count + chunk_keep_count,
            }
        )
    merged_verdicts = [
        PredicateVerdict(
            predicate_id=entry["predicate_id"],
            variable_id=entry["variable_id"],
            verdict="drop",
            evidence=_merged_evidence_line(entry),
        )
        for entry in sorted(drops.values(), key=_drop_entry_sort_key)
    ]
    return CriticResponse(verdicts=merged_verdicts), {
        "drop_count": len(merged_verdicts),
        "keep_verdict_count": keep_count,
        "invalid_or_out_of_scope_verdict_count": invalid_count,
        "merged_drop_verdicts": list(sorted(drops.values(), key=_drop_entry_sort_key)),
        "chunk_verdict_counts": chunk_verdict_counts,
    }


def _record_verdicts(record: dict[str, Any]) -> list[PredicateVerdict]:
    response_payload = record.get("response")
    if isinstance(response_payload, dict):
        try:
            return CriticResponse.model_validate(response_payload).verdicts
        except Exception:
            pass
    verdicts = record.get("verdicts")
    if not isinstance(verdicts, list):
        return []
    parsed: list[PredicateVerdict] = []
    for item in verdicts:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append(PredicateVerdict.model_validate(item))
        except Exception:
            continue
    return parsed


def _drop_entry_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (str(entry.get("predicate_id") or ""), str(entry.get("variable_id") or ""))


def _merged_evidence_line(entry: dict[str, Any]) -> str:
    chunk_ids = ", ".join(str(value) for value in entry.get("source_chunk_ids", []))
    evidence = str(entry.get("evidence") or "").strip()
    if chunk_ids:
        return f"{evidence} [source chunks: {chunk_ids}]"
    return evidence


def _final_llm_result(
    *,
    step1_result: LLMResult,
    final_predicates: list[StatePredicate],
    critic_results: list[CriticResult],
    run_id: str,
    page: int,
    variant_key: str,
    model: str,
    prompt_strategy: str,
    prompt_cache_key: str | None,
) -> LLMResult:
    analysis = (
        f"{step1_result.response.Analysis}\n\n"
        "Step 2 chunked static critic applied as conservative union-drop: "
        "only explicit drop verdicts from evidence chunks were removed."
    )
    response = step1_result.response.model_copy(
        update={
            "Analysis": analysis,
            "State_Definitions": final_predicates,
        }
    )
    usage = aggregate_result_usage(critic_results)
    return LLMResult(
        model=model,
        variant=2,
        response=response,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_sec=float(usage.get("latency_sec") or 0.0),
        cached_tokens=int(usage.get("cached_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
        request_meta={
            "api": "context_window_critic_aggregate",
            "status": "success",
            "run_id": run_id,
            "page": page,
            "variant": variant_key,
            "base_variant": 2,
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "chunk_count": len(critic_results),
            "requests": usage.get("requests", []),
        },
        raw_json=response.model_dump_json(indent=2),
        messages_sent=[],
    )


def _skip_no_static_evidence(
    *,
    step1_result: LLMResult,
    variant_key: str,
    chunks_manifest: dict[str, Any],
) -> tuple[LLMResult, dict[str, Any], list[dict[str, Any]]]:
    response = step1_result.response.model_copy(
        update={
            "Analysis": (
                f"{step1_result.response.Analysis}\n\n"
                "Step 2 chunked static critic skipped: no page-local "
                "static evidence chunks were available."
            ),
        }
    )
    final_result = replace(
        step1_result,
        response=response,
        raw_json=response.model_dump_json(indent=2),
    )
    merge_meta = {
        "status": "skipped",
        "variant": variant_key,
        "base_variant": 2,
        "merge_policy": (
            "union_explicit_drop_verdicts_only_keep_verdicts_do_not_override"
        ),
        "chunk_count": 0,
        "source_chunk_ids": _manifest_chunk_ids(chunks_manifest),
        "drop_count": 0,
        "keep_verdict_count": 0,
        "invalid_or_out_of_scope_verdict_count": 0,
        "missing_verdict_policy": "keep",
        "absence_of_evidence_policy": "keep",
        "critic_skipped_reason": NO_STATIC_EVIDENCE_REASON,
        "critic_api_called": False,
        "critic_applied": False,
        "step1_predicate_count": len(step1_result.response.State_Definitions),
        "final_predicate_count": len(step1_result.response.State_Definitions),
        "domain_attribution": chunks_manifest.get("domain_attribution", {}),
        "shared_evidence_duplicates": chunks_manifest.get(
            "shared_evidence_duplicates",
            {},
        ),
        "merged_drop_verdicts": [],
        "chunk_verdict_counts": [],
        "chunk_execution": [],
    }
    return final_result, merge_meta, []


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


def _critic_usage_dict(result: CriticResult) -> dict[str, Any]:
    return result_usage_dict(
        result,
        include_max_completion_tokens=True,
        extra={"turn": "step2_critic_chunk"},
    )


def _aggregate_critic_usage(chunk_outputs: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "turn": "step2_critic_chunked",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "latency_sec": 0.0,
        "chunk_count": len(chunk_outputs),
        "requests": [],
    }
    for record in chunk_outputs:
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
        ):
            totals[key] += int(usage.get(key) or 0)
        totals["latency_sec"] += float(usage.get("latency_sec") or 0.0)
        request = usage.get("request") or record.get("request")
        if request:
            totals["requests"].append(
                {
                    "chunk_id": record.get("chunk_id"),
                    "request": request,
                    "reused_from_existing": bool(record.get("reused_from_existing")),
                }
            )
    totals["latency_sec"] = round(float(totals["latency_sec"]), 3)
    prompt_tokens = int(totals["prompt_tokens"] or 0)
    totals["cache_hit_rate"] = (
        round(int(totals["cached_tokens"]) / prompt_tokens, 6)
        if prompt_tokens
        else 0.0
    )
    return totals


def _critic_verdicts_payload(
    *,
    chunk_outputs: list[dict[str, Any]],
    merge_meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        "merge_policy": merge_meta.get("merge_policy"),
        "missing_verdict_policy": merge_meta.get("missing_verdict_policy"),
        "absence_of_evidence_policy": merge_meta.get("absence_of_evidence_policy"),
        "drop_count": merge_meta.get("drop_count", 0),
        "keep_verdict_count": merge_meta.get("keep_verdict_count", 0),
        "invalid_or_out_of_scope_verdict_count": merge_meta.get(
            "invalid_or_out_of_scope_verdict_count",
            0,
        ),
        "merged_drop_verdicts": merge_meta.get("merged_drop_verdicts", []),
        "chunks": [
            {
                "chunk_id": record.get("chunk_id"),
                "status": record.get("status"),
                "reused_from_existing": bool(record.get("reused_from_existing")),
                "verdicts": record.get("verdicts", []),
            }
            for record in chunk_outputs
        ],
    }


def _write_prepared_outputs(
    *,
    output_dir: Path,
    run_id: str,
    page: int,
    variant_key: str,
    prompt_strategy: str,
    system_prompt: str,
    step1_user_prompt: str,
    step1_result: LLMResult | None,
    input_dir: Path,
    source_step1_page_dir: Path,
    model: str,
    prompt_cache_key: str | None,
    domain_attribution_policy: str,
    target_chars: int,
    max_chars: int,
    step1_source: str,
    step1_api_called: bool,
    chunk_count: int,
) -> None:
    (output_dir / "prompt_system.txt").write_text(system_prompt, encoding="utf-8")
    (output_dir / "prompt_user.txt").write_text(step1_user_prompt, encoding="utf-8")
    if step1_result is not None:
        step1_usage = result_usage_dict(step1_result)
        step1_usage["turn"] = _step1_usage_turn(step1_source)
        write_json(output_dir / "usage.json", step1_usage)
    write_json(
        output_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": 2,
            "page": page,
            "model": model,
            "lane": variant_key,
            "mode": "context_window_chunked_static_critic",
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
            "critic_chunked": True,
            "critic_no_analysis": False,
            "critic_api_called": False,
            "critic_prior_turns": 2,
            "critic_step2_prompt_available": chunk_count > 0,
            "critic_step2_prompt_blocked_reason": (
                None if chunk_count > 0 else NO_STATIC_EVIDENCE_REASON
            ),
            "analysis_payload_available": chunk_count > 0,
            "context_evidence_index": str(output_dir / "context_evidence_index.json"),
            "chunks_manifest": str(output_dir / "chunks_manifest.json"),
            "chunk_count": chunk_count,
            "target_chars": target_chars,
            "max_chars": max_chars,
            "domain_attribution_policy": domain_attribution_policy,
            "response_available": False,
            "usage_available": step1_result is not None,
            "accumulated_predicates_updated": False,
            "prepared_at": iso_now(),
        },
    )


def _write_run_outputs(
    *,
    output_dir: Path,
    run_id: str,
    page: int,
    variant_key: str,
    prompt_strategy: str,
    system_prompt: str,
    step1_user_prompt: str,
    step1_result: LLMResult,
    final_result: LLMResult,
    merge_meta: dict[str, Any],
    chunk_outputs: list[dict[str, Any]],
    input_dir: Path,
    source_step1_page_dir: Path,
    model: str,
    prompt_cache_key: str | None,
    domain_attribution_policy: str,
    target_chars: int,
    max_chars: int,
    chunk_max_attempts: int,
    resume_existing_chunks: bool,
    step1_source: str,
    step1_api_called: bool,
) -> None:
    (output_dir / "prompt_system.txt").write_text(system_prompt, encoding="utf-8")
    (output_dir / "prompt_user.txt").write_text(step1_user_prompt, encoding="utf-8")
    write_json(
        output_dir / "response_parsed.json",
        {
            "Analysis": final_result.response.Analysis,
            "State_Definitions": [
                predicate.model_dump(exclude_none=True)
                for predicate in final_result.response.State_Definitions
            ],
        },
    )
    (output_dir / "response_raw.txt").write_text(
        final_result.response.model_dump_json(indent=2),
        encoding="utf-8",
    )
    step1_usage = result_usage_dict(step1_result)
    step1_usage["turn"] = _step1_usage_turn(step1_source)
    write_json(output_dir / "usage.json", step1_usage)
    write_json(output_dir / "usage_step2_critic.json", _aggregate_critic_usage(chunk_outputs))
    write_json(output_dir / "merge_meta.json", merge_meta)
    write_json(
        output_dir / "critic_verdicts.json",
        _critic_verdicts_payload(
            chunk_outputs=chunk_outputs,
            merge_meta=merge_meta,
        ),
    )
    write_json(
        output_dir / "run_meta.json",
        {
            "run_id": run_id,
            "variant": variant_key,
            "base_variant": 2,
            "page": page,
            "model": model,
            "lane": variant_key,
            "mode": "context_window_chunked_static_critic",
            "status": "success",
            "prompt_strategy": prompt_strategy,
            "prompt_cache_key": prompt_cache_key,
            "input_dir": str(input_dir),
            "source_step1_page_dir": str(source_step1_page_dir),
            "source_variant_2chunked_page_dir": str(source_step1_page_dir),
            "latency_sec": round(final_result.latency_sec, 3),
            "predicate_count": len(final_result.response.State_Definitions),
            "api_called": bool(merge_meta.get("critic_api_called")),
            "step1_source": step1_source,
            "step1_api_called": step1_api_called,
            "critic_enabled": True,
            "critic_chunked": True,
            "critic_no_analysis": False,
            "critic_api_called": bool(merge_meta.get("critic_api_called")),
            "critic_applied": bool(merge_meta.get("critic_applied")),
            "critic_skipped_reason": merge_meta.get("critic_skipped_reason"),
            "critic_prior_turns": 2,
            "critic_max_completion_tokens": DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
            "context_evidence_index": str(output_dir / "context_evidence_index.json"),
            "chunks_manifest": str(output_dir / "chunks_manifest.json"),
            "chunk_count": int(merge_meta.get("chunk_count") or 0),
            "drop_count": int(merge_meta.get("drop_count") or 0),
            "keep_verdict_count": int(merge_meta.get("keep_verdict_count") or 0),
            "missing_verdict_policy": merge_meta.get("missing_verdict_policy"),
            "absence_of_evidence_policy": merge_meta.get("absence_of_evidence_policy"),
            "target_chars": target_chars,
            "max_chars": max_chars,
            "domain_attribution_policy": domain_attribution_policy,
            "chunk_max_attempts": chunk_max_attempts,
            "resume_existing_chunks": resume_existing_chunks,
            "request": final_result.request_meta or None,
        },
    )


def _step1_usage_turn(step1_source: str) -> str:
    if step1_source == "reused_v2chunked":
        return "step1_reused_v2chunked"
    if step1_source == "integrated_v2chunked":
        return "step1_integrated_v2chunked"
    return "step1"


def _prepared_merge_meta(
    *,
    variant_key: str,
    chunks_manifest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "prepared",
        "variant": variant_key,
        "base_variant": 2,
        "merge_policy": (
            "union_explicit_drop_verdicts_only_keep_verdicts_do_not_override"
        ),
        "chunk_count": _manifest_chunk_count(chunks_manifest),
        "source_chunk_ids": _manifest_chunk_ids(chunks_manifest),
        "missing_verdict_policy": "keep",
        "absence_of_evidence_policy": "keep",
        "domain_attribution": chunks_manifest.get("domain_attribution", {}),
        "shared_evidence_duplicates": chunks_manifest.get(
            "shared_evidence_duplicates",
            {},
        ),
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
    failure_paths = [
        output_dir / "failure_meta.json",
        *sorted((output_dir / "chunk_outputs").glob("*.failure.json")),
    ]
    for path in failure_paths:
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
        "prompt_user_step2_critic": str(output_dir / "prompt_user_step2_critic.txt"),
        "response_parsed": None,
        "response_raw": None,
        "usage": str(output_dir / "usage.json") if (output_dir / "usage.json").exists() else None,
        "usage_step2_critic": None,
        "run_meta": str(output_dir / "run_meta.json"),
        "context_evidence_index": str(output_dir / "context_evidence_index.json"),
        "chunks_manifest": str(output_dir / "chunks_manifest.json"),
        "merge_meta": str(output_dir / "merge_meta.json"),
        "chunk_prompts": str(output_dir / "chunk_prompts"),
        "chunk_outputs": str(output_dir / "chunk_outputs"),
        "critic_verdicts": None,
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
        "prompt_user_step2_critic": str(output_dir / "prompt_user_step2_critic.txt"),
        "response_parsed": str(output_dir / "response_parsed.json"),
        "response_raw": str(output_dir / "response_raw.txt"),
        "usage": str(output_dir / "usage.json"),
        "usage_step2_critic": str(output_dir / "usage_step2_critic.json"),
        "run_meta": str(output_dir / "run_meta.json"),
        "context_evidence_index": str(output_dir / "context_evidence_index.json"),
        "chunks_manifest": str(output_dir / "chunks_manifest.json"),
        "merge_meta": str(output_dir / "merge_meta.json"),
        "chunk_prompts": str(output_dir / "chunk_prompts"),
        "chunk_outputs": str(output_dir / "chunk_outputs"),
        "critic_verdicts": str(output_dir / "critic_verdicts.json"),
        "status": "success"
        if merge_meta.get("critic_skipped_reason") is None
        else "skipped",
        "api_called": bool(merge_meta.get("critic_api_called")),
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
        "drop_count": int(merge_meta.get("drop_count") or 0),
        "domain_attribution": merge_meta.get("domain_attribution", {}),
    }
