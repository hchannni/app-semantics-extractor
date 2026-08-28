"""V2-chunked raw-source experiment support.

This lane keeps the final PredicateResponse contract unchanged while replacing
single-pass raw-source injection with deterministic file chunks plus
variable-level append/dedupe.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from ..extractors.code_context_loader import RawSourceChunk, build_raw_source_file_chunks
from ..utils import log, write_json
from .api_diagnostics import (
    aggregate_result_usage,
    attached_request_meta,
    result_usage_dict,
)
from .llm_client import (
    LLMResult,
    PredicateResponse,
    StatePredicate,
    V2ChunkResult,
    Variable,
    query_v2_chunk_candidates,
)

V2_CHUNKED_VARIANT_KEY = "2chunked"
V2_CHUNKED_VARIANT_DIR = "variant_2chunked"
V2_CHUNKED_BASE_VARIANT = 2
V2_CHUNKED_PROMPT_STRATEGY = "v2_file_chunked_source_cached_system_prefix_append_dedupe"
DEFAULT_V2_CHUNK_TARGET_CHARS = 400000
DEFAULT_V2_CHUNK_MAX_CHARS = 500000

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_VALID_TYPES = {"Boolean", "String", "Number", "Time", "Date", "Enum"}

V2ChunkQueryFn = Callable[..., V2ChunkResult]


@dataclass(frozen=True)
class V2ChunkedRunResult:
    final_result: LLMResult
    chunks_manifest: dict[str, Any]
    dedupe_meta: dict[str, Any]
    chunk_outputs: list[dict[str, Any]]
    paths: dict[str, Path]


@dataclass(frozen=True)
class V2ChunkedPreparedResult:
    chunks_manifest: dict[str, Any]
    paths: dict[str, Path]


@lru_cache(maxsize=8)
def _load_template(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _chunk_manifest_entry(chunk: RawSourceChunk) -> dict[str, Any]:
    return {key: value for key, value in chunk.items() if key != "source_text"}


def _chunk_prompt_cache_key(
    *,
    base_key: str | None,
    chunk: RawSourceChunk,
) -> str | None:
    if base_key is None:
        return None
    stripped = base_key.strip()
    if not stripped:
        return None
    sha = str(chunk.get("sha256") or "")[:12]
    chunk_id = str(chunk["chunk_id"])
    return f"{stripped}:v2chunk:{chunk_id}:{sha}"


def _source_files_text(chunk: RawSourceChunk) -> str:
    return "\n".join(
        (
            f"- {file_meta['source_path']} "
            f"(lines {file_meta['start_line']}-{file_meta['end_line']}, "
            f"chars={file_meta['char_count']})"
        )
        for file_meta in chunk["files"]
    )


def _build_map_prompts(
    *,
    chunk: RawSourceChunk,
    app_name: str,
    a11y_xml: str,
    existing_predicates: list[dict[str, Any]],
) -> tuple[str, str]:
    variant234_system_prompt = _load_template(_PROMPTS_DIR / "system_prompt_variant234.txt")
    system_template = _load_template(_PROMPTS_DIR / "system_prompt_v2_chunked_map.txt")
    user_template = _load_template(_PROMPTS_DIR / "user_variant2.txt")
    source_text = chunk["source_text"].replace("```", "` ` `")
    system_prompt = system_template.format(
        variant234_system_prompt=variant234_system_prompt,
        chunk_id=chunk["chunk_id"],
        source_files=_source_files_text(chunk),
        source_text=source_text,
    )
    user_prompt = user_template.format(
        app_name=app_name,
        existing_predicates=_json_text(existing_predicates),
        accessibility_tree=a11y_xml,
        chunk_id=chunk["chunk_id"],
    )
    return system_prompt, user_prompt


def _write_map_inputs(
    *,
    output_dir: Path,
    chunk: RawSourceChunk,
    system_prompt: str,
    user_prompt: str,
) -> None:
    chunk_id = chunk["chunk_id"]
    (output_dir / "chunk_inputs").mkdir(parents=True, exist_ok=True)
    (output_dir / "chunk_prompts").mkdir(parents=True, exist_ok=True)
    (output_dir / "chunk_inputs" / f"{chunk_id}.txt").write_text(
        chunk["source_text"],
        encoding="utf-8",
    )
    (output_dir / "chunk_prompts" / f"{chunk_id}_system.txt").write_text(
        system_prompt,
        encoding="utf-8",
    )
    (output_dir / "chunk_prompts" / f"{chunk_id}_user.txt").write_text(
        user_prompt,
        encoding="utf-8",
    )


def _chunk_output_record(
    *,
    chunk: RawSourceChunk,
    result: V2ChunkResult,
    prompt_cache_key: str | None,
) -> dict[str, Any]:
    return {
        **_chunk_manifest_entry(chunk),
        "prompt_cache_key": prompt_cache_key,
        "Analysis": result.response.Analysis,
        "candidates": [
            candidate.model_dump(exclude_none=True)
            for candidate in result.response.candidates
        ],
        "usage": _chunk_usage(result),
    }


def _chunk_usage(result: V2ChunkResult) -> dict[str, Any]:
    return result_usage_dict(result)


def _write_chunk_output(output_dir: Path, record: dict[str, Any]) -> None:
    chunk_id = str(record["chunk_id"])
    write_json(output_dir / "chunk_outputs" / f"{chunk_id}.json", record)


def _write_chunk_failure_output(
    output_dir: Path,
    *,
    chunk: RawSourceChunk,
    prompt_cache_key: str | None,
    exc: BaseException,
) -> None:
    chunk_id = str(chunk["chunk_id"])
    request_meta = attached_request_meta(exc)
    write_json(
        output_dir / "chunk_outputs" / f"{chunk_id}.failure.json",
        {
            **_chunk_manifest_entry(chunk),
            "prompt_cache_key": prompt_cache_key,
            "status": "failure",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "request": request_meta,
        },
    )


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _coerce_type(value: str) -> str:
    stripped = value.strip()
    for valid in _VALID_TYPES:
        if stripped.lower() == valid.lower():
            return valid
    return stripped or "String"


def _candidate_entries(chunk_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for chunk in chunk_outputs:
        for candidate in chunk.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            entries.append({
                **candidate,
                "chunk_id": chunk["chunk_id"],
                "source_paths": list(chunk.get("source_paths", [])),
            })
    return entries


def _dedupe_candidate_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for item in entries:
        predicate = str(item.get("predicate") or "").strip()
        variable = str(item.get("variable") or "").strip()
        if not predicate or not variable:
            continue
        key = (_norm_key(predicate), _norm_key(variable))
        if key not in deduped:
            deduped[key] = _new_deduped_entry(item, predicate, variable)
        else:
            _merge_duplicate(deduped[key], item, duplicates)
    return list(deduped.values()), duplicates


def _new_deduped_entry(
    item: dict[str, Any],
    predicate: str,
    variable: str,
) -> dict[str, Any]:
    description = str(item.get("description") or "").strip()
    return {
        "predicate": predicate,
        "variable": variable,
        "type": _coerce_type(str(item.get("type") or "")),
        "description": description or f"{variable} state variable.",
        "source_chunks": [str(item["chunk_id"])],
        "source_paths": [str(path) for path in item.get("source_paths", [])],
    }


def _merge_duplicate(
    kept: dict[str, Any],
    item: dict[str, Any],
    duplicates: list[dict[str, Any]],
) -> None:
    chunk_id = str(item["chunk_id"])
    source_paths = [str(path) for path in item.get("source_paths", [])]
    if chunk_id not in kept["source_chunks"]:
        kept["source_chunks"].append(chunk_id)
    for source_path in source_paths:
        if source_path not in kept["source_paths"]:
            kept["source_paths"].append(source_path)
    duplicates.append({
        "predicate": kept["predicate"],
        "variable": kept["variable"],
        "duplicate_chunk": chunk_id,
        "duplicate_source_paths": source_paths,
        "discarded_type": str(item.get("type") or ""),
        "discarded_description": str(item.get("description") or ""),
    })


def _state_definitions_from_deduped(deduped: list[dict[str, Any]]) -> list[StatePredicate]:
    grouped: dict[str, list[Variable]] = {}
    for item in deduped:
        predicate = str(item["predicate"])
        grouped.setdefault(predicate, []).append(
            Variable(
                name=str(item["variable"]),
                type=str(item["type"]),
                description=str(item["description"]),
            )
        )
    return [
        StatePredicate(
            name=predicate,
            description=f"State variables related to {predicate}.",
            variables=variables,
        )
        for predicate, variables in grouped.items()
    ]


def _aggregate_usage(results: list[V2ChunkResult]) -> dict[str, Any]:
    usage = aggregate_result_usage(results)
    usage["chunk_requests"] = usage.pop("requests", [])
    return usage


def _final_result(
    *,
    model: str,
    usage: dict[str, Any],
    chunk_count: int,
    candidate_count: int,
    deduped: list[dict[str, Any]],
) -> LLMResult:
    response = PredicateResponse(
        Analysis=(
            "V2-chunked append+dedupe selected "
            f"{len(deduped)} variables from {candidate_count} chunk candidates "
            f"across {chunk_count} raw source chunks."
        ),
        State_Definitions=_state_definitions_from_deduped(deduped),
    )
    return LLMResult(
        model=model,
        variant=V2_CHUNKED_BASE_VARIANT,
        response=response,
        prompt_tokens=int(usage["prompt_tokens"]),
        completion_tokens=int(usage["completion_tokens"]),
        latency_sec=float(usage["latency_sec"]),
        cached_tokens=int(usage["cached_tokens"]),
        reasoning_tokens=int(usage["reasoning_tokens"]),
        request_meta={
            "status": "success",
            "operation": "v2_chunked_aggregate",
            "chunk_count": chunk_count,
            "candidate_count": candidate_count,
            "chunk_requests": usage.get("chunk_requests", []),
        },
        raw_json=response.model_dump_json(),
        messages_sent=[],
    )


def _paths(output_dir: Path) -> dict[str, Path]:
    return {
        "chunks_manifest": output_dir / "chunks_manifest.json",
        "chunk_inputs": output_dir / "chunk_inputs",
        "chunk_prompts": output_dir / "chunk_prompts",
        "chunk_outputs": output_dir / "chunk_outputs",
        "dedupe_meta": output_dir / "dedupe_meta.json",
    }


def _build_chunks_manifest(
    *,
    app_source_root: Path,
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> tuple[list[RawSourceChunk], dict[str, Any]]:
    chunks = build_raw_source_file_chunks(
        app_source_root,
        target_chars=target_chunk_chars,
        max_chars=max_chunk_chars,
    )
    manifest = {
        "chunking_policy": {
            "unit": "file",
            "file_split": "never",
            "target_chars": target_chunk_chars,
            "max_chars": max_chunk_chars,
            "oversized_file_policy": "single-file chunk with oversized_file=true",
        },
        "chunk_count": len(chunks),
        "source_file_count": sum(chunk["file_count"] for chunk in chunks),
        "chunks": [_chunk_manifest_entry(chunk) for chunk in chunks],
    }
    return chunks, manifest


def _add_chunk_cache_policy(
    manifest: dict[str, Any],
    *,
    base_prompt_cache_key: str | None,
) -> dict[str, Any]:
    enabled = bool(base_prompt_cache_key and base_prompt_cache_key.strip())
    base_key = base_prompt_cache_key.strip() if enabled and base_prompt_cache_key else None
    policy = {
        "base_prompt_cache_key": base_key,
        "actual_key_policy": (
            "<base_prompt_cache_key>:v2chunk:<chunk_id>:<chunk_sha12>"
        ),
        "enabled": enabled,
    }
    manifest["chunk_prompt_cache_key_policy"] = policy
    for chunk in manifest.get("chunks", []):
        if not isinstance(chunk, dict):
            continue
        if not enabled or base_key is None:
            chunk["prompt_cache_key"] = None
            continue
        chunk_id = str(chunk.get("chunk_id") or "")
        sha = str(chunk.get("sha256") or "")[:12]
        chunk["prompt_cache_key"] = f"{base_key}:v2chunk:{chunk_id}:{sha}"
    return manifest


def prepare_v2_chunked_generation(
    *,
    output_dir: Path,
    app_source_root: Path,
    app_name: str,
    a11y_xml: str,
    existing_predicates: list[dict[str, Any]],
    prompt_cache_key: str | None = None,
    target_chunk_chars: int = DEFAULT_V2_CHUNK_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_CHUNK_MAX_CHARS,
) -> V2ChunkedPreparedResult:
    chunks, manifest = _build_chunks_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    _add_chunk_cache_policy(manifest, base_prompt_cache_key=prompt_cache_key)
    paths = _paths(output_dir)
    write_json(paths["chunks_manifest"], manifest)
    for chunk in chunks:
        system_prompt, user_prompt = _build_map_prompts(
            chunk=chunk,
            app_name=app_name,
            a11y_xml=a11y_xml,
            existing_predicates=existing_predicates,
        )
        _write_map_inputs(
            output_dir=output_dir,
            chunk=chunk,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    return V2ChunkedPreparedResult(manifest, paths)


def run_v2_chunked_generation(
    *,
    output_dir: Path,
    app_source_root: Path,
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    target_chunk_chars: int = DEFAULT_V2_CHUNK_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_CHUNK_MAX_CHARS,
    query_fn: V2ChunkQueryFn | None = None,
) -> V2ChunkedRunResult:
    chunks, manifest = _build_chunks_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    _add_chunk_cache_policy(manifest, base_prompt_cache_key=prompt_cache_key)
    paths = _paths(output_dir)
    write_json(paths["chunks_manifest"], manifest)

    results, records = _run_chunk_map_stage(
        chunks=chunks,
        output_dir=output_dir,
        app_name=app_name,
        a11y_xml=a11y_xml,
        screenshot_path=screenshot_path,
        existing_predicates=existing_predicates,
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        query_fn=query_fn or query_v2_chunk_candidates,
    )
    entries = _candidate_entries(records)
    deduped, duplicates = _dedupe_candidate_entries(entries)
    usage = _aggregate_usage(results)
    dedupe_meta = _dedupe_meta(entries, deduped, duplicates)
    write_json(paths["dedupe_meta"], dedupe_meta)
    final_result = _final_result(
        model=model,
        usage=usage,
        chunk_count=len(chunks),
        candidate_count=len(entries),
        deduped=deduped,
    )
    log(
        f"[v2_chunked] chunks={len(chunks)} candidates={len(entries)} "
        f"deduped_variables={len(deduped)}",
        "green",
    )
    return V2ChunkedRunResult(final_result, manifest, dedupe_meta, records, paths)


def _run_chunk_map_stage(
    *,
    chunks: list[RawSourceChunk],
    output_dir: Path,
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    query_fn: V2ChunkQueryFn,
) -> tuple[list[V2ChunkResult], list[dict[str, Any]]]:
    results: list[V2ChunkResult] = []
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        system_prompt, user_prompt = _build_map_prompts(
            chunk=chunk,
            app_name=app_name,
            a11y_xml=a11y_xml,
            existing_predicates=existing_predicates,
        )
        _write_map_inputs(
            output_dir=output_dir,
            chunk=chunk,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        chunk_cache_key = _chunk_prompt_cache_key(
            base_key=prompt_cache_key,
            chunk=chunk,
        )
        try:
            result = query_fn(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                screenshot_path=screenshot_path,
                variant=V2_CHUNKED_BASE_VARIANT,
                model=model,
                timeout=timeout,
                prompt_cache_key=chunk_cache_key,
            )
        except Exception as exc:
            _write_chunk_failure_output(
                output_dir,
                chunk=chunk,
                prompt_cache_key=chunk_cache_key,
                exc=exc,
            )
            raise
        record = _chunk_output_record(
            chunk=chunk,
            result=result,
            prompt_cache_key=chunk_cache_key,
        )
        _write_chunk_output(output_dir, record)
        results.append(result)
        records.append(record)
    return results, records


def _dedupe_meta(
    entries: list[dict[str, Any]],
    deduped: list[dict[str, Any]],
    duplicates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "dedupe_policy": {
            "key": "normalize(predicate) + normalize(variable)",
            "type": "first non-empty candidate wins",
            "description": "first non-empty candidate wins",
            "provenance": "source chunks and source paths are accumulated",
        },
        "candidate_count": len(entries),
        "deduped_variable_count": len(deduped),
        "duplicate_count": len(duplicates),
        "deduped_variables": deduped,
        "duplicates": duplicates,
    }
