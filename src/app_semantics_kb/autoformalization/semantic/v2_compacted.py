"""V2-compacted raw-source experiment support.

This lane is a provider-side context-compression baseline. It keeps raw source
chunking mechanical, uses the Responses API standalone compaction endpoint to
fold chunks into an opaque context window, then asks the final predicate
question with the existing PredicateResponse contract.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..extractors.code_context_loader import RawSourceChunk, build_raw_source_file_chunks
from ..utils import log, write_json
from .api_diagnostics import (
    attach_failure_meta as _attach_failure_meta,
    empty_usage as _empty_usage,
    finalize_responses_request_success as _responses_finalize_success,
    response_id as _response_id,
    responses_usage_dict as _usage_dict,
    start_responses_request_meta as _responses_start_request_meta,
)
from .llm_client import (
    DEFAULT_SESSION_MAX_COMPLETION_TOKENS,
    LLMResult,
    PredicateResponse,
)

V2_COMPACTED_VARIANT_KEY = "2compacted"
V2_COMPACTED_VARIANT_DIR = "variant_2compacted"
V2_COMPACTED_BASE_VARIANT = 2
V2_COMPACTED_PROMPT_STRATEGY = "v2_responses_compaction_raw_source_chain"
V2_COMPACTED_APP_VARIANT_KEY = "2compacted_app"
V2_COMPACTED_APP_VARIANT_DIR = "variant_2compacted_app"
V2_COMPACTED_APP_PROMPT_STRATEGY = "v2_app_level_responses_compaction_reused_source"
V2_COMPACTED_PARALLEL_VARIANT_KEY = "2compacted_parallel"
V2_COMPACTED_PARALLEL_VARIANT_DIR = "variant_2compacted_parallel"
V2_COMPACTED_PARALLEL_PROMPT_STRATEGY = (
    "v2_app_level_responses_compaction_parallel_reused_chunks"
)
V2_COMPACTED_APP_SOURCE_CONTEXT_MODE = "app_level_standalone_compaction_reuse"
V2_COMPACTED_PARALLEL_SOURCE_CONTEXT_MODE = (
    "app_level_parallel_compacted_chunks_reuse"
)
DEFAULT_V2_COMPACT_TARGET_CHARS = 400000
DEFAULT_V2_COMPACT_MAX_CHARS = 500000

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


@dataclass(frozen=True)
class V2CompactedRunResult:
    final_result: LLMResult
    chunks_manifest: dict[str, Any]
    compaction_meta: dict[str, Any]
    compaction_steps: list[dict[str, Any]]
    paths: dict[str, Path]


@dataclass(frozen=True)
class V2CompactedPreparedResult:
    chunks_manifest: dict[str, Any]
    paths: dict[str, Path]


@dataclass(frozen=True)
class V2AppSourceCompactionResult:
    window: list[dict[str, Any]]
    chunks_manifest: dict[str, Any]
    compaction_meta: dict[str, Any]
    compaction_steps: list[dict[str, Any]]
    paths: dict[str, Path]


@dataclass(frozen=True)
class V2CompactedAppPreparedResult:
    paths: dict[str, Path]


@dataclass(frozen=True)
class V2CompactedAppRunResult:
    final_result: LLMResult
    paths: dict[str, Path]


@dataclass(frozen=True)
class _PreparedArtifacts:
    chunks: list[RawSourceChunk]
    manifest: dict[str, Any]
    paths: dict[str, Path]
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True)
class _CompactionChain:
    window: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    usage: dict[str, float | int]
    requests: list[dict[str, Any]]


def _load_template(name: str) -> str:
    path = _PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _read_json_list(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def _chunk_manifest_entry(chunk: RawSourceChunk) -> dict[str, Any]:
    return {key: value for key, value in chunk.items() if key != "source_text"}


def _source_files_text(chunk: RawSourceChunk) -> str:
    return "\n".join(
        (
            f"- {file_meta['source_path']} "
            f"(lines {file_meta['start_line']}-{file_meta['end_line']}, "
            f"chars={file_meta['char_count']})"
        )
        for file_meta in chunk["files"]
    )


def _build_source_chunk_message(chunk: RawSourceChunk) -> dict[str, Any]:
    source_text = chunk["source_text"].replace("```", "` ` `")
    text = (
        f"**[Raw Source Chunk: {chunk['chunk_id']}]**\n"
        "This is app runtime source code for a later source-grounded "
        "predicate-generation request. Preserve exact class, method, field, "
        "variable, enum, resource, and preference identifiers during compaction.\n\n"
        f"Source files:\n{_source_files_text(chunk)}\n\n"
        f"--- RAW SOURCE CODE ---\n{source_text}"
    )
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _build_bootstrap_message() -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "We will compact Android app source code chunks for a later "
                    "source-grounded predicate-generation task. Keep exact source "
                    "identifiers available whenever possible; do not generate the "
                    "final predicates until the final screen-specific request."
                ),
            }
        ],
    }


def _build_final_prompts(
    *,
    app_name: str,
    a11y_xml: str,
    existing_predicates: list[dict[str, Any]],
) -> tuple[str, str]:
    system_prompt = _load_template("system_prompt_variant234.txt")
    user_template = _load_template("user_variant2.txt")
    user_prompt = user_template.format(
        app_name=app_name,
        existing_predicates=_json_text(existing_predicates),
        accessibility_tree=a11y_xml,
    )
    user_prompt = user_prompt.replace(
        "- Raw Source Code: (provided in the system context; use it as the primary grounding source.)",
        "- Raw Source Code: (provided through the compacted Responses context; use exact source identifiers only when supported by that context.)",
    )
    return system_prompt, user_prompt


def _encode_screenshot(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def _final_user_message(
    *,
    user_prompt: str,
    screenshot_path: Path | None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if screenshot_path is not None and screenshot_path.exists():
        content.append(
            {
                "type": "input_image",
                "image_url": _encode_screenshot(screenshot_path),
                "detail": "high",
            }
        )
    content.append({"type": "input_text", "text": user_prompt})
    return {"type": "message", "role": "user", "content": content}


def _paths(output_dir: Path) -> dict[str, Path]:
    return {
        "chunks_manifest": output_dir / "chunks_manifest.json",
        "chunk_inputs": output_dir / "chunk_inputs",
        "compaction_steps": output_dir / "compaction_steps",
        "final_prompt_system": output_dir / "prompt_system.txt",
        "final_prompt_user": output_dir / "prompt_user.txt",
        "final_response": output_dir / "final_response.json",
        "compaction_meta": output_dir / "compaction_meta.json",
    }


def _source_compaction_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "chunks_manifest": output_dir / "chunks_manifest.json",
        "chunk_inputs": output_dir / "chunk_inputs",
        "compaction_steps": output_dir / "compaction_steps",
        "compaction_window": output_dir / "compaction_window.json",
        "compaction_meta": output_dir / "compaction_meta.json",
    }


def _page_generation_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "final_prompt_system": output_dir / "prompt_system.txt",
        "final_prompt_user": output_dir / "prompt_user.txt",
        "final_response": output_dir / "final_response.json",
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
        "compaction_policy": {
            "mode": "standalone_responses_compact",
            "input_unit": "file-packed raw source chunk",
            "output_item": "opaque compaction item",
            "final_generation": "compacted window + current screenshot/a11y request",
        },
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


def _write_prepared_inputs(
    *,
    output_dir: Path,
    chunks: list[RawSourceChunk],
    system_prompt: str,
    user_prompt: str,
) -> None:
    paths = _paths(output_dir)
    paths["chunk_inputs"].mkdir(parents=True, exist_ok=True)
    paths["final_prompt_system"].write_text(system_prompt, encoding="utf-8")
    paths["final_prompt_user"].write_text(user_prompt, encoding="utf-8")
    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        (paths["chunk_inputs"] / f"{chunk_id}.txt").write_text(
            chunk["source_text"],
            encoding="utf-8",
        )


def _write_source_compaction_inputs(
    *,
    output_dir: Path,
    chunks: list[RawSourceChunk],
) -> None:
    paths = _source_compaction_paths(output_dir)
    paths["chunk_inputs"].mkdir(parents=True, exist_ok=True)
    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        (paths["chunk_inputs"] / f"{chunk_id}.txt").write_text(
            chunk["source_text"],
            encoding="utf-8",
        )


def _write_final_prompts(
    *,
    output_dir: Path,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Path]:
    paths = _page_generation_paths(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths["final_prompt_system"].write_text(system_prompt, encoding="utf-8")
    paths["final_prompt_user"].write_text(user_prompt, encoding="utf-8")
    return paths


def prepare_v2_compacted_generation(
    *,
    output_dir: Path,
    app_source_root: Path,
    app_name: str,
    a11y_xml: str,
    existing_predicates: list[dict[str, Any]],
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
) -> V2CompactedPreparedResult:
    prepared = _prepare_generation_artifacts(
        output_dir=output_dir,
        app_source_root=app_source_root,
        app_name=app_name,
        a11y_xml=a11y_xml,
        existing_predicates=existing_predicates,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    return V2CompactedPreparedResult(prepared.manifest, prepared.paths)


def prepare_v2_app_source_compaction(
    *,
    output_dir: Path,
    app_source_root: Path,
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
    prompt_strategy: str = V2_COMPACTED_APP_PROMPT_STRATEGY,
    merge_strategy: str = "sequential_folding",
) -> V2AppSourceCompactionResult:
    chunks, manifest = _build_chunks_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    paths = _source_compaction_paths(output_dir)
    write_json(paths["chunks_manifest"], manifest)
    _write_source_compaction_inputs(output_dir=output_dir, chunks=chunks)
    compaction_meta = {
        "prompt_strategy": prompt_strategy,
        "status": "prepared",
        "api_called": False,
        "merge_strategy": merge_strategy,
        "source_file_count": int(manifest["source_file_count"]),
        "chunk_count": int(manifest["chunk_count"]),
        "target_chunk_chars": target_chunk_chars,
        "max_chunk_chars": max_chunk_chars,
    }
    write_json(paths["compaction_meta"], compaction_meta)
    write_json(paths["compaction_window"], [])
    return V2AppSourceCompactionResult(
        window=[],
        chunks_manifest=manifest,
        compaction_meta=compaction_meta,
        compaction_steps=[],
        paths=paths,
    )


def prepare_v2_parallel_source_compaction(
    *,
    output_dir: Path,
    app_source_root: Path,
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
) -> V2AppSourceCompactionResult:
    return prepare_v2_app_source_compaction(
        output_dir=output_dir,
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
        prompt_strategy=V2_COMPACTED_PARALLEL_PROMPT_STRATEGY,
        merge_strategy="parallel_independent_chunks",
    )


def load_v2_app_source_compaction(output_dir: Path) -> V2AppSourceCompactionResult:
    paths = _source_compaction_paths(output_dir)
    manifest = _read_json_object(paths["chunks_manifest"])
    compaction_meta = _read_json_object(paths["compaction_meta"])
    window_payload = _read_json_list(paths["compaction_window"])
    step_dir = paths["compaction_steps"]
    steps = []
    if step_dir.is_dir():
        for path in sorted(step_dir.glob("*.json")):
            payload = _read_json_object(path)
            if payload:
                steps.append(payload)
    result = V2AppSourceCompactionResult(
        window=[item for item in window_payload if isinstance(item, dict)],
        chunks_manifest=manifest,
        compaction_meta=compaction_meta,
        compaction_steps=steps,
        paths=paths,
    )
    if compaction_meta.get("status") == "success":
        return _normalize_app_source_compaction_window(result)
    return result


def has_v2_app_source_compaction(output_dir: Path) -> bool:
    paths = _source_compaction_paths(output_dir)
    return (
        paths["chunks_manifest"].is_file()
        and paths["compaction_meta"].is_file()
        and paths["compaction_window"].is_file()
    )


def _normalize_app_source_compaction_window(
    result: V2AppSourceCompactionResult,
) -> V2AppSourceCompactionResult:
    if not result.window:
        return result
    try:
        compacted_window = _compaction_context_items(
            result.window,
            chunk_id="stored_app_source_window",
        )
    except RuntimeError:
        compaction_meta = {
            **result.compaction_meta,
            "status": "invalid_no_compaction_items",
            "window_filter": "compaction_items_only",
            "stored_window_item_count_before_filter": len(result.window),
            "raw_source_items_removed_from_window": len(result.window),
        }
        write_json(result.paths["compaction_window"], [])
        write_json(result.paths["compaction_meta"], compaction_meta)
        return V2AppSourceCompactionResult(
            window=[],
            chunks_manifest=result.chunks_manifest,
            compaction_meta=compaction_meta,
            compaction_steps=result.compaction_steps,
            paths=result.paths,
        )
    if len(compacted_window) == len(result.window):
        return result

    removed_count = len(result.window) - len(compacted_window)
    compaction_meta = {
        **result.compaction_meta,
        "final_window_item_count": len(compacted_window),
        "window_filter": "compaction_items_only",
        "stored_window_item_count_before_filter": len(result.window),
        "raw_source_items_removed_from_window": removed_count,
    }
    write_json(result.paths["compaction_window"], compacted_window)
    write_json(result.paths["compaction_meta"], compaction_meta)
    return V2AppSourceCompactionResult(
        window=compacted_window,
        chunks_manifest=result.chunks_manifest,
        compaction_meta=compaction_meta,
        compaction_steps=result.compaction_steps,
        paths=result.paths,
    )


def run_v2_app_source_compaction(
    *,
    output_dir: Path,
    app_source_root: Path,
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
    prompt_strategy: str = V2_COMPACTED_APP_PROMPT_STRATEGY,
    operation: str = "v2_compacted_app_source_compaction",
    operation_prefix: str = "v2_compacted_app_source_chunk",
    log_label: str = "v2_compacted_app",
    merge_strategy: str = "sequential_folding",
) -> V2AppSourceCompactionResult:
    chunks, manifest = _build_chunks_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    paths = _source_compaction_paths(output_dir)
    write_json(paths["chunks_manifest"], manifest)
    _write_source_compaction_inputs(output_dir=output_dir, chunks=chunks)
    client = _build_client(timeout=timeout)
    t0 = time.perf_counter()
    chain = _run_compaction_chain(
        client=client,
        model=model,
        timeout=timeout,
        chunks=chunks,
        paths=paths,
        prompt_cache_key=prompt_cache_key,
        operation_prefix=operation_prefix,
        log_label=log_label,
    )
    latency = time.perf_counter() - t0
    compaction_meta = _write_source_compaction_meta(
        paths=paths,
        manifest=manifest,
        chain=chain,
        aggregate={**chain.usage, "latency_sec": latency},
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
        prompt_strategy=prompt_strategy,
        operation=operation,
        merge_strategy=merge_strategy,
    )
    write_json(paths["compaction_window"], chain.window)
    log(
        f"[{log_label}] source compacted chunks={len(chunks)} "
        f"window_items={len(chain.window)}",
        "green",
    )
    return V2AppSourceCompactionResult(
        window=chain.window,
        chunks_manifest=manifest,
        compaction_meta=compaction_meta,
        compaction_steps=chain.steps,
        paths=paths,
    )


def run_v2_parallel_source_compaction(
    *,
    output_dir: Path,
    app_source_root: Path,
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
) -> V2AppSourceCompactionResult:
    chunks, manifest = _build_chunks_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    paths = _source_compaction_paths(output_dir)
    write_json(paths["chunks_manifest"], manifest)
    _write_source_compaction_inputs(output_dir=output_dir, chunks=chunks)
    client = _build_client(timeout=timeout)
    t0 = time.perf_counter()
    chain = _run_parallel_compaction_chunks(
        client=client,
        model=model,
        timeout=timeout,
        chunks=chunks,
        paths=paths,
        prompt_cache_key=prompt_cache_key,
    )
    latency = time.perf_counter() - t0
    compaction_meta = _write_source_compaction_meta(
        paths=paths,
        manifest=manifest,
        chain=chain,
        aggregate={**chain.usage, "latency_sec": latency},
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
        prompt_strategy=V2_COMPACTED_PARALLEL_PROMPT_STRATEGY,
        operation="v2_compacted_parallel_source_compaction",
        merge_strategy="parallel_independent_chunks",
    )
    write_json(paths["compaction_window"], chain.window)
    log(
        f"[v2_compacted_parallel] source compacted chunks={len(chunks)} "
        f"window_items={len(chain.window)}",
        "green",
    )
    return V2AppSourceCompactionResult(
        window=chain.window,
        chunks_manifest=manifest,
        compaction_meta=compaction_meta,
        compaction_steps=chain.steps,
        paths=paths,
    )


def prepare_v2_compacted_app_generation(
    *,
    output_dir: Path,
    app_name: str,
    a11y_xml: str,
    existing_predicates: list[dict[str, Any]],
) -> V2CompactedAppPreparedResult:
    system_prompt, user_prompt = _build_final_prompts(
        app_name=app_name,
        a11y_xml=a11y_xml,
        existing_predicates=existing_predicates,
    )
    paths = _write_final_prompts(
        output_dir=output_dir,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    return V2CompactedAppPreparedResult(paths=paths)


def run_v2_compacted_app_generation(
    *,
    output_dir: Path,
    source_window: list[dict[str, Any]],
    source_compaction_meta: dict[str, Any],
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    max_completion_tokens: int | None = DEFAULT_SESSION_MAX_COMPLETION_TOKENS,
    prompt_strategy: str = V2_COMPACTED_APP_PROMPT_STRATEGY,
    source_context_mode: str = V2_COMPACTED_APP_SOURCE_CONTEXT_MODE,
    operation: str = "v2_compacted_app_final_generation",
) -> V2CompactedAppRunResult:
    if not source_window:
        raise ValueError(
            "V2 compacted-app generation requires a non-empty app-level "
            "compacted source window."
        )
    system_prompt, user_prompt = _build_final_prompts(
        app_name=app_name,
        a11y_xml=a11y_xml,
        existing_predicates=existing_predicates,
    )
    paths = _write_final_prompts(
        output_dir=output_dir,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    client = _build_client(timeout=timeout)
    t0 = time.perf_counter()
    final_response, final_request = _run_final_turn(
        client=client,
        model=model,
        timeout=timeout,
        chain_window=source_window,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        screenshot_path=screenshot_path,
        prompt_cache_key=prompt_cache_key,
        max_completion_tokens=max_completion_tokens,
        operation=operation,
    )
    final_usage = _usage_dict(final_response)
    parsed, raw_json = _predicate_response_from_final(final_response)
    latency = time.perf_counter() - t0
    aggregate = {**_empty_usage(), **final_usage, "latency_sec": latency}
    write_json(paths["final_response"], _to_plain(final_response))
    result = _build_app_final_llm_result(
        model=model,
        parsed=parsed,
        aggregate=aggregate,
        latency=latency,
        max_completion_tokens=max_completion_tokens,
        raw_json=raw_json,
        source_compaction_meta=source_compaction_meta,
        final_request=final_request,
        source_context_mode=source_context_mode,
        operation=operation,
    )
    log(
        f"[{prompt_strategy}] predicates={len(parsed.State_Definitions)} "
        f"tokens={result.prompt_tokens}+{result.completion_tokens}",
        "green",
    )
    return V2CompactedAppRunResult(final_result=result, paths=paths)


def run_v2_compacted_parallel_generation(
    *,
    output_dir: Path,
    source_window: list[dict[str, Any]],
    source_compaction_meta: dict[str, Any],
    app_name: str,
    a11y_xml: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    max_completion_tokens: int | None = DEFAULT_SESSION_MAX_COMPLETION_TOKENS,
) -> V2CompactedAppRunResult:
    return run_v2_compacted_app_generation(
        output_dir=output_dir,
        source_window=source_window,
        source_compaction_meta=source_compaction_meta,
        app_name=app_name,
        a11y_xml=a11y_xml,
        screenshot_path=screenshot_path,
        existing_predicates=existing_predicates,
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        max_completion_tokens=max_completion_tokens,
        prompt_strategy=V2_COMPACTED_PARALLEL_PROMPT_STRATEGY,
        source_context_mode=V2_COMPACTED_PARALLEL_SOURCE_CONTEXT_MODE,
        operation="v2_compacted_parallel_final_generation",
    )


def _prepare_generation_artifacts(
    *,
    output_dir: Path,
    app_source_root: Path,
    app_name: str,
    a11y_xml: str,
    existing_predicates: list[dict[str, Any]],
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> _PreparedArtifacts:
    chunks, manifest = _build_chunks_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    paths = _paths(output_dir)
    write_json(paths["chunks_manifest"], manifest)
    system_prompt, user_prompt = _build_final_prompts(
        app_name=app_name,
        a11y_xml=a11y_xml,
        existing_predicates=existing_predicates,
    )
    _write_prepared_inputs(
        output_dir=output_dir,
        chunks=chunks,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    return _PreparedArtifacts(chunks, manifest, paths, system_prompt, user_prompt)


def run_v2_compacted_generation(
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
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
    max_completion_tokens: int | None = DEFAULT_SESSION_MAX_COMPLETION_TOKENS,
) -> V2CompactedRunResult:
    prepared = _prepare_generation_artifacts(
        output_dir=output_dir,
        app_source_root=app_source_root,
        app_name=app_name,
        a11y_xml=a11y_xml,
        existing_predicates=existing_predicates,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    client = _build_client(timeout=timeout)
    t0 = time.perf_counter()
    chain = _run_compaction_chain(
        client=client,
        model=model,
        timeout=timeout,
        chunks=prepared.chunks,
        paths=prepared.paths,
        prompt_cache_key=prompt_cache_key,
    )
    final_response, final_request = _run_final_turn(
        client=client,
        model=model,
        timeout=timeout,
        chain_window=chain.window,
        system_prompt=prepared.system_prompt,
        user_prompt=prepared.user_prompt,
        screenshot_path=screenshot_path,
        prompt_cache_key=prompt_cache_key,
        max_completion_tokens=max_completion_tokens,
    )
    final_usage = _usage_dict(final_response)
    aggregate = dict(chain.usage)
    _add_usage(aggregate, final_usage)
    parsed, raw_json = _predicate_response_from_final(final_response)
    latency = time.perf_counter() - t0
    aggregate["latency_sec"] = latency

    write_json(prepared.paths["final_response"], _to_plain(final_response))
    compaction_meta = _write_compaction_meta(
        paths=prepared.paths,
        manifest=prepared.manifest,
        chain=chain,
        final_response=final_response,
        final_request=final_request,
        aggregate=aggregate,
    )
    result = _build_final_llm_result(
        model=model,
        parsed=parsed,
        aggregate=aggregate,
        latency=latency,
        max_completion_tokens=max_completion_tokens,
        raw_json=raw_json,
        compaction_requests=chain.requests,
        final_request=final_request,
    )
    log(
        f"[v2_compacted] chunks={len(prepared.chunks)} "
        f"predicates={len(parsed.State_Definitions)} "
        f"tokens={result.prompt_tokens}+{result.completion_tokens}",
        "green",
    )
    return V2CompactedRunResult(
        final_result=result,
        chunks_manifest=prepared.manifest,
        compaction_meta=compaction_meta,
        compaction_steps=chain.steps,
        paths=prepared.paths,
    )


def _run_compaction_chain(
    *,
    client: Any,
    model: str,
    timeout: float,
    chunks: list[RawSourceChunk],
    paths: dict[str, Path],
    prompt_cache_key: str | None,
    operation_prefix: str = "v2_compacted_chunk",
    log_label: str = "v2_compacted",
) -> _CompactionChain:
    window: list[dict[str, Any]] = [_build_bootstrap_message()]
    steps: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    aggregate = _empty_usage()
    for chunk in chunks:
        response, request_meta = _compact_window(
            client=client,
            model=model,
            timeout=timeout,
            chunk_id=str(chunk["chunk_id"]),
            input_window=[*window, _build_source_chunk_message(chunk)],
            prompt_cache_key=prompt_cache_key,
            operation=f"{operation_prefix}:{chunk['chunk_id']}",
            log_label=log_label,
        )
        record = _compaction_step_record(
            chunk=chunk,
            response=response,
            request_meta=request_meta,
        )
        _add_usage(aggregate, record["usage"])
        output_items = _plain_output_items(response)
        window = _compaction_context_items(
            output_items,
            chunk_id=str(chunk["chunk_id"]),
        )
        steps.append(record)
        requests.append(request_meta)
        write_json(paths["compaction_steps"] / f"{chunk['chunk_id']}.json", record)
        log(
            f"[{log_label}] compacted {chunk['chunk_id']} "
            f"compaction_items={len(window)} "
            f"raw_output_items={record['output_item_count']} "
            f"input_tokens={record['usage']['prompt_tokens']}",
            "cyan",
        )
    return _CompactionChain(window, steps, aggregate, requests)


def _run_parallel_compaction_chunks(
    *,
    client: Any,
    model: str,
    timeout: float,
    chunks: list[RawSourceChunk],
    paths: dict[str, Path],
    prompt_cache_key: str | None,
) -> _CompactionChain:
    steps: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    window: list[dict[str, Any]] = []
    aggregate = _empty_usage()
    for chunk in chunks:
        response, request_meta = _compact_window(
            client=client,
            model=model,
            timeout=timeout,
            chunk_id=str(chunk["chunk_id"]),
            input_window=[_build_bootstrap_message(), _build_source_chunk_message(chunk)],
            prompt_cache_key=prompt_cache_key,
            operation=f"v2_compacted_parallel_source_chunk:{chunk['chunk_id']}",
            log_label="v2_compacted_parallel",
        )
        record = _compaction_step_record(
            chunk=chunk,
            response=response,
            request_meta=request_meta,
        )
        compacted_items = _compaction_context_items(
            _plain_output_items(response),
            chunk_id=str(chunk["chunk_id"]),
        )
        _add_usage(aggregate, record["usage"])
        window.extend(compacted_items)
        steps.append(
            {
                **record,
                "merge_strategy": "parallel_independent_chunks",
                "window_item_offset": len(window) - len(compacted_items),
            }
        )
        requests.append(request_meta)
        write_json(paths["compaction_steps"] / f"{chunk['chunk_id']}.json", steps[-1])
        log(
            f"[v2_compacted_parallel] compacted {chunk['chunk_id']} "
            f"compaction_items={len(compacted_items)} "
            f"total_window_items={len(window)} "
            f"raw_output_items={record['output_item_count']} "
            f"input_tokens={record['usage']['prompt_tokens']}",
            "cyan",
        )
    return _CompactionChain(window, steps, aggregate, requests)


def _compaction_step_record(
    *,
    chunk: RawSourceChunk,
    response: Any,
    request_meta: dict[str, Any],
) -> dict[str, Any]:
    output_items = _plain_output_items(response)
    compacted_items = _compaction_context_items(
        output_items,
        chunk_id=str(chunk["chunk_id"]),
    )
    return {
        **_chunk_manifest_entry(chunk),
        "response_id": _response_id(response),
        "output_item_count": len(output_items),
        "output_item_types": _output_item_type_counts(output_items),
        "output_text_chars": _items_text_chars(output_items),
        "compaction_window_item_count": len(compacted_items),
        "compaction_window_text_chars": _items_text_chars(compacted_items),
        "raw_source_output_item_count": len(output_items) - len(compacted_items),
        "usage": request_meta.get("usage", _usage_dict(response)),
        "request": request_meta,
    }


def _compaction_context_items(
    output_items: list[dict[str, Any]],
    *,
    chunk_id: str,
) -> list[dict[str, Any]]:
    compacted_items = [
        item
        for item in output_items
        if "compact" in str(item.get("type") or "").lower()
    ]
    if not compacted_items:
        raise RuntimeError(
            "Responses compact returned no compaction items for "
            f"{chunk_id}; refusing to reuse raw source messages as compacted context."
        )
    return compacted_items


def _output_item_type_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        item_type = str(item.get("type") or "unknown")
        counts[item_type] = counts.get(item_type, 0) + 1
    return counts


def _items_text_chars(items: list[dict[str, Any]]) -> int:
    return sum(_item_text_chars(item) for item in items)


def _item_text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_item_text_chars(item) for item in value.values())
    if isinstance(value, list):
        return sum(_item_text_chars(item) for item in value)
    return 0


def _run_final_turn(
    *,
    client: Any,
    model: str,
    timeout: float,
    chain_window: list[dict[str, Any]],
    system_prompt: str,
    user_prompt: str,
    screenshot_path: Path | None,
    prompt_cache_key: str | None,
    max_completion_tokens: int | None,
    operation: str = "v2_compacted_final_generation",
    context_management: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    return _create_final_response(
        client=client,
        model=model,
        timeout=timeout,
        input_window=[
            *chain_window,
            _final_user_message(
                user_prompt=user_prompt,
                screenshot_path=screenshot_path,
            ),
        ],
        system_prompt=system_prompt,
        prompt_cache_key=prompt_cache_key,
        max_completion_tokens=max_completion_tokens,
        operation=operation,
        context_management=context_management,
    )


def _write_compaction_meta(
    *,
    paths: dict[str, Path],
    manifest: dict[str, Any],
    chain: _CompactionChain,
    final_response: Any,
    final_request: dict[str, Any],
    aggregate: dict[str, float | int],
) -> dict[str, Any]:
    compaction_meta = {
        "prompt_strategy": V2_COMPACTED_PROMPT_STRATEGY,
        "compaction_call_count": len(chain.steps),
        "final_response_id": _response_id(final_response),
        "final_window_item_count": len(chain.window),
        "source_file_count": int(manifest["source_file_count"]),
        "chunk_count": int(manifest["chunk_count"]),
        "usage": aggregate,
        "requests": {
            "compaction": chain.requests,
            "final": final_request,
        },
    }
    write_json(paths["compaction_meta"], compaction_meta)
    return compaction_meta


def _write_source_compaction_meta(
    *,
    paths: dict[str, Path],
    manifest: dict[str, Any],
    chain: _CompactionChain,
    aggregate: dict[str, float | int],
    target_chunk_chars: int,
    max_chunk_chars: int,
    prompt_strategy: str = V2_COMPACTED_APP_PROMPT_STRATEGY,
    operation: str = "v2_compacted_app_source_compaction",
    merge_strategy: str = "sequential_folding",
) -> dict[str, Any]:
    compaction_meta = {
        "prompt_strategy": prompt_strategy,
        "status": "success",
        "api": "responses.compact",
        "operation": operation,
        "merge_strategy": merge_strategy,
        "compaction_call_count": len(chain.steps),
        "final_window_item_count": len(chain.window),
        "source_file_count": int(manifest["source_file_count"]),
        "chunk_count": int(manifest["chunk_count"]),
        "target_chunk_chars": target_chunk_chars,
        "max_chunk_chars": max_chunk_chars,
        "usage": aggregate,
        "requests": {"compaction": chain.requests},
    }
    write_json(paths["compaction_meta"], compaction_meta)
    return compaction_meta


def _build_final_llm_result(
    *,
    model: str,
    parsed: PredicateResponse,
    aggregate: dict[str, float | int],
    latency: float,
    max_completion_tokens: int | None,
    raw_json: str,
    compaction_requests: list[dict[str, Any]],
    final_request: dict[str, Any],
) -> LLMResult:
    return LLMResult(
        model=model,
        variant=V2_COMPACTED_BASE_VARIANT,
        response=parsed,
        prompt_tokens=int(aggregate["prompt_tokens"]),
        completion_tokens=int(aggregate["completion_tokens"]),
        latency_sec=latency,
        cached_tokens=int(aggregate["cached_tokens"]),
        reasoning_tokens=int(aggregate["reasoning_tokens"]),
        max_completion_tokens=max_completion_tokens,
        request_meta={
            "status": "success",
            "api": "responses.compact+responses.parse",
            "operation": "v2_compacted_aggregate",
            "latency_sec": round(latency, 3),
            "usage": aggregate,
            "compaction_requests": compaction_requests,
            "final_request": final_request,
        },
        raw_json=raw_json,
        messages_sent=[],
    )


def _build_app_final_llm_result(
    *,
    model: str,
    parsed: PredicateResponse,
    aggregate: dict[str, float | int],
    latency: float,
    max_completion_tokens: int | None,
    raw_json: str,
    source_compaction_meta: dict[str, Any],
    final_request: dict[str, Any],
    source_context_mode: str,
    operation: str,
) -> LLMResult:
    return LLMResult(
        model=model,
        variant=V2_COMPACTED_BASE_VARIANT,
        response=parsed,
        prompt_tokens=int(aggregate["prompt_tokens"]),
        completion_tokens=int(aggregate["completion_tokens"]),
        latency_sec=latency,
        cached_tokens=int(aggregate["cached_tokens"]),
        reasoning_tokens=int(aggregate["reasoning_tokens"]),
        max_completion_tokens=max_completion_tokens,
        request_meta={
            "status": "success",
            "api": final_request.get("api", "responses.parse"),
            "operation": operation,
            "latency_sec": round(latency, 3),
            "usage": aggregate,
            "source_context_mode": source_context_mode,
            "source_compaction": {
                "status": source_compaction_meta.get("status"),
                "prompt_strategy": source_compaction_meta.get("prompt_strategy"),
                "merge_strategy": source_compaction_meta.get("merge_strategy"),
                "chunk_count": source_compaction_meta.get("chunk_count"),
                "source_file_count": source_compaction_meta.get("source_file_count"),
                "compaction_call_count": source_compaction_meta.get("compaction_call_count"),
                "final_window_item_count": source_compaction_meta.get("final_window_item_count"),
                "usage": source_compaction_meta.get("usage"),
            },
            "final_request": final_request,
        },
        raw_json=raw_json,
        messages_sent=[],
    )


def _build_client(*, timeout: float) -> Any:
    resolved_key = os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        raise ValueError("OPENAI_API_KEY 환경 변수가 설정되어 있지 않습니다.")
    try:
        import openai
    except ImportError as exc:
        raise RuntimeError(
            "V2-compacted requires the OpenAI Python SDK with Responses API support."
        ) from exc

    client = openai.OpenAI(api_key=resolved_key, timeout=timeout, max_retries=0)
    responses = getattr(client, "responses", None)
    if responses is None or not hasattr(responses, "compact"):
        raise RuntimeError(
            "The installed OpenAI SDK does not expose client.responses.compact(). "
            "Upgrade the OpenAI Python SDK before running V2-compacted."
        )
    return client


def _structured_text_format() -> dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "PredicateResponse",
            "schema": PredicateResponse.model_json_schema(),
            "strict": True,
        }
    }


def _extra_body(prompt_cache_key: str | None) -> dict[str, str] | None:
    if prompt_cache_key is None:
        return None
    stripped = prompt_cache_key.strip()
    if not stripped:
        return None
    return {"prompt_cache_key": stripped}


def _compact_window(
    *,
    client: Any,
    model: str,
    timeout: float,
    chunk_id: str,
    input_window: list[dict[str, Any]],
    prompt_cache_key: str | None,
    operation: str,
    log_label: str,
) -> tuple[Any, dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "model": model,
        "input": input_window,
    }
    extra_body = _extra_body(prompt_cache_key)
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    request_meta, t0 = _responses_start_request_meta(
        api="responses.compact",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        input_window=input_window,
        operation=operation,
    )
    log(
        f"[{log_label}] compacting {chunk_id} ... "
        f"timeout={timeout:g}s text_chars={request_meta['text_chars']}",
        "cyan",
    )
    try:
        response = client.responses.compact(**kwargs)
    except Exception as exc:
        failure_meta = _attach_failure_meta(exc, request_meta, started_perf=t0)
        log(
            f"[{log_label}] compact failed chunk={chunk_id} "
            f"latency={failure_meta['latency_sec']:.1f}s "
            f"error={failure_meta['error_type']} "
            f"request_id={failure_meta['request_id']}",
            "red",
        )
        raise
    return response, _responses_finalize_success(
        request_meta,
        response=response,
        started_perf=t0,
    )


def _create_final_response(
    *,
    client: Any,
    model: str,
    timeout: float,
    input_window: list[dict[str, Any]],
    system_prompt: str,
    prompt_cache_key: str | None,
    max_completion_tokens: int | None,
    operation: str = "v2_compacted_final_generation",
    context_management: list[dict[str, Any]] | None = None,
    conversation: str | dict[str, Any] | None = None,
    store: bool | None = False,
) -> tuple[Any, dict[str, Any]]:
    base_kwargs: dict[str, Any] = {
        "model": model,
        "instructions": system_prompt,
        "input": input_window,
    }
    if store is not None:
        base_kwargs["store"] = store
    if conversation is not None:
        base_kwargs["conversation"] = conversation
    if max_completion_tokens is not None:
        base_kwargs["max_output_tokens"] = max_completion_tokens
    if context_management is not None:
        base_kwargs["context_management"] = context_management
    extra_body = _extra_body(prompt_cache_key)
    if extra_body is not None:
        base_kwargs["extra_body"] = extra_body

    parse_method = getattr(client.responses, "parse", None)
    if callable(parse_method):
        try:
            request_meta, t0 = _responses_start_request_meta(
                api="responses.parse",
                model=model,
                timeout=timeout,
                prompt_cache_key=prompt_cache_key,
                input_window=input_window,
                max_completion_tokens=max_completion_tokens,
                instructions=system_prompt,
                operation=operation,
            )
            if context_management is not None:
                request_meta["context_management"] = context_management
            if conversation is not None:
                request_meta["conversation"] = conversation
                request_meta["server_managed_conversation"] = True
            if store is not None:
                request_meta["store"] = store
            response = parse_method(
                **base_kwargs,
                text_format=PredicateResponse,
            )
            return response, _responses_finalize_success(
                request_meta,
                response=response,
                started_perf=t0,
            )
        except TypeError:
            pass
        except Exception as exc:
            failure_meta = _attach_failure_meta(exc, request_meta, started_perf=t0)
            log(
                f"[v2_compacted] final parse failed "
                f"latency={failure_meta['latency_sec']:.1f}s "
                f"error={failure_meta['error_type']} "
                f"request_id={failure_meta['request_id']}",
                "red",
            )
            raise

    kwargs = {**base_kwargs, "text": _structured_text_format()}
    request_meta, t0 = _responses_start_request_meta(
        api="responses.create",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        input_window=input_window,
        max_completion_tokens=max_completion_tokens,
        instructions=system_prompt,
        operation=operation,
    )
    if context_management is not None:
        request_meta["context_management"] = context_management
    if conversation is not None:
        request_meta["conversation"] = conversation
        request_meta["server_managed_conversation"] = True
    if store is not None:
        request_meta["store"] = store
    try:
        response = client.responses.create(**kwargs)
    except Exception as exc:
        failure_meta = _attach_failure_meta(exc, request_meta, started_perf=t0)
        log(
            f"[v2_compacted] final create failed "
            f"latency={failure_meta['latency_sec']:.1f}s "
            f"error={failure_meta['error_type']} "
            f"request_id={failure_meta['request_id']}",
            "red",
        )
        raise
    return response, _responses_finalize_success(
        request_meta,
        response=response,
        started_perf=t0,
    )


def _plain_output_items(response: Any) -> list[dict[str, Any]]:
    output = getattr(response, "output", None)
    if output is None:
        return []
    plain = _to_plain(output)
    if isinstance(plain, list):
        return [item for item in plain if isinstance(item, dict)]
    return []


def _response_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    for item in _plain_output_items(response):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text
    raise ValueError("Responses final output did not contain text to parse.")


def _predicate_response_from_final(response: Any) -> tuple[PredicateResponse, str]:
    parsed = getattr(response, "output_parsed", None)
    if isinstance(parsed, PredicateResponse):
        return parsed, parsed.model_dump_json()
    if isinstance(parsed, dict):
        predicate_response = PredicateResponse.model_validate(parsed)
        return predicate_response, predicate_response.model_dump_json()

    raw_json = _response_output_text(response)
    return PredicateResponse.model_validate_json(raw_json), raw_json


def _add_usage(target: dict[str, float | int], usage: dict[str, int | float]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens"):
        target[key] = int(target[key]) + int(usage.get(key, 0))


def _to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    return value
