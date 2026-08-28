"""Responses API server-managed multiturn V2 experiment support.

This lane keeps the existing V2 final prompt template intact, but changes the
execution shape from page-local requests to one ordered Responses conversation.
Source context and prior page turns are stored in an OpenAI Conversation object,
and server-side auto-compaction is enabled through ``context_management``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..extractors.code_context_loader import RawSourceChunk, build_raw_source_file_chunks
from ..utils import log, write_json
from .api_diagnostics import empty_usage as _empty_usage
from .api_diagnostics import response_id as _response_id
from .llm_client import (
    DEFAULT_SESSION_MAX_COMPLETION_TOKENS,
    LLMResult,
    PredicateResponse,
)
from .predicate_merger import merge_state_definitions
from .v2_compacted import (
    DEFAULT_V2_COMPACT_MAX_CHARS,
    DEFAULT_V2_COMPACT_TARGET_CHARS,
    _build_bootstrap_message,
    _build_client,
    _build_final_prompts,
    _build_source_chunk_message,
    _create_final_response,
    _final_user_message,
    _plain_output_items,
    _predicate_response_from_final,
    _to_plain,
    _usage_dict,
)

V2_RESPONSES_MULTITURN_VARIANT_KEY = "2responses_multiturn"
V2_RESPONSES_MULTITURN_VARIANT_DIR = "variant_2responses_multiturn"
V2_RESPONSES_MULTITURN_BASE_VARIANT = 2
V2_RESPONSES_MULTITURN_PROMPT_STRATEGY = "v2_responses_true_multiturn_auto_compaction"
DEFAULT_RESPONSES_COMPACT_THRESHOLD = 200000
SOURCE_CONTEXT_MODE = "responses_conversation_server_managed_source_state"
SESSION_MODE = "server_managed_conversation"
CONVERSATION_SOURCE_BATCH_SIZE = 20


@dataclass(frozen=True)
class V2ResponsesMultiturnPageInput:
    page: int
    output_dir: Path
    app_name: str
    a11y_xml: str
    screenshot_path: Path | None


@dataclass(frozen=True)
class V2ResponsesMultiturnPreparedPage:
    page: int
    paths: dict[str, Path]


@dataclass(frozen=True)
class V2ResponsesMultiturnPageResult:
    page: int
    final_result: LLMResult
    accumulated_predicates: list[dict[str, Any]]
    paths: dict[str, Path]
    conversation_state: dict[str, Any]


@dataclass(frozen=True)
class V2ResponsesMultiturnRunResult:
    source_manifest: dict[str, Any]
    page_results: list[V2ResponsesMultiturnPageResult]
    session_state: dict[str, Any]


def _source_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "source_manifest": output_dir / "source_manifest.json",
        "source_inputs": output_dir / "source_inputs",
        "conversation_state": output_dir / "conversation_state.json",
        "session_state": output_dir / "session_state.json",
    }


def _page_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "prompt_system": output_dir / "prompt_system.txt",
        "prompt_user": output_dir / "prompt_user.txt",
        "final_response": output_dir / "final_response.json",
        "conversation_state": output_dir / "conversation_state.json",
    }


def _chunk_manifest_entry(chunk: RawSourceChunk) -> dict[str, Any]:
    return {key: value for key, value in chunk.items() if key != "source_text"}


def _build_source_manifest(
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
        "prompt_strategy": V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
        "source_context_mode": SOURCE_CONTEXT_MODE,
        "session_mode": SESSION_MODE,
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


def _write_source_inputs(
    *,
    output_dir: Path,
    chunks: list[RawSourceChunk],
    manifest: dict[str, Any],
) -> None:
    paths = _source_paths(output_dir)
    paths["source_inputs"].mkdir(parents=True, exist_ok=True)
    write_json(paths["source_manifest"], manifest)
    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        (paths["source_inputs"] / f"{chunk_id}.txt").write_text(
            chunk["source_text"],
            encoding="utf-8",
        )


def _write_page_prompts(
    *,
    output_dir: Path,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Path]:
    paths = _page_paths(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths["prompt_system"].write_text(system_prompt, encoding="utf-8")
    paths["prompt_user"].write_text(user_prompt, encoding="utf-8")
    return paths


def prepare_v2_responses_multiturn_generation(
    *,
    output_dir: Path,
    app_source_root: Path,
    pages: list[V2ResponsesMultiturnPageInput],
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
) -> list[V2ResponsesMultiturnPreparedPage]:
    chunks, manifest = _build_source_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    _write_source_inputs(output_dir=output_dir, chunks=chunks, manifest=manifest)
    prepared_pages = []
    for page in pages:
        system_prompt, user_prompt = _build_final_prompts(
            app_name=page.app_name,
            a11y_xml=page.a11y_xml,
            existing_predicates=[],
        )
        paths = _write_page_prompts(
            output_dir=page.output_dir,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        prepared_pages.append(V2ResponsesMultiturnPreparedPage(page.page, paths))
    return prepared_pages


def run_v2_responses_multiturn_generation(
    *,
    output_dir: Path,
    app_source_root: Path,
    pages: list[V2ResponsesMultiturnPageInput],
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    compact_threshold: int = DEFAULT_RESPONSES_COMPACT_THRESHOLD,
    target_chunk_chars: int = DEFAULT_V2_COMPACT_TARGET_CHARS,
    max_chunk_chars: int = DEFAULT_V2_COMPACT_MAX_CHARS,
    max_completion_tokens: int | None = DEFAULT_SESSION_MAX_COMPLETION_TOKENS,
    on_page_result: Callable[[V2ResponsesMultiturnPageResult], None] | None = None,
) -> V2ResponsesMultiturnRunResult:
    chunks, manifest = _build_source_manifest(
        app_source_root=app_source_root,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    _write_source_inputs(output_dir=output_dir, chunks=chunks, manifest=manifest)
    client = _build_client(timeout=timeout)
    conversation_state = _create_source_conversation(
        client=client,
        output_dir=output_dir,
        chunks=chunks,
        timeout=timeout,
    )
    conversation_id = conversation_state["conversation_id"]
    accumulated: list[dict[str, Any]] = []
    page_results: list[V2ResponsesMultiturnPageResult] = []
    t0 = time.perf_counter()
    for page in pages:
        page_result, accumulated = _run_page_turn(
            client=client,
            model=model,
            timeout=timeout,
            prompt_cache_key=prompt_cache_key,
            compact_threshold=compact_threshold,
            max_completion_tokens=max_completion_tokens,
            page=page,
            conversation_id=conversation_id,
            accumulated=accumulated,
        )
        page_results.append(page_result)
        if on_page_result is not None:
            on_page_result(page_result)

    session_state = {
        "prompt_strategy": V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
        "source_context_mode": SOURCE_CONTEXT_MODE,
        "session_mode": SESSION_MODE,
        "auto_compaction_enabled": True,
        "compact_threshold": compact_threshold,
        "conversation_id": conversation_id,
        "source_item_count": conversation_state["source_item_count"],
        "source_item_batch_count": conversation_state["source_item_batch_count"],
        "page_count": len(pages),
        "latency_sec": round(time.perf_counter() - t0, 3),
        "source_manifest": str(_source_paths(output_dir)["source_manifest"]),
        "conversation_state": str(_source_paths(output_dir)["conversation_state"]),
    }
    write_json(_source_paths(output_dir)["session_state"], session_state)
    return V2ResponsesMultiturnRunResult(
        source_manifest=manifest,
        page_results=page_results,
        session_state=session_state,
    )


def _source_conversation_items(chunks: list[RawSourceChunk]) -> list[dict[str, Any]]:
    return [
        _build_bootstrap_message(),
        *[_build_source_chunk_message(chunk) for chunk in chunks],
    ]


def _conversation_id(conversation: Any) -> str:
    value = getattr(conversation, "id", None)
    if value is None and isinstance(conversation, dict):
        value = conversation.get("id")
    if value is None:
        plain = _to_plain(conversation)
        if isinstance(plain, dict):
            value = plain.get("id")
    if not value:
        raise RuntimeError("OpenAI conversation create response did not include an id.")
    return str(value)


def _batches(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _create_source_conversation(
    *,
    client: Any,
    output_dir: Path,
    chunks: list[RawSourceChunk],
    timeout: float,
) -> dict[str, Any]:
    conversations = getattr(client, "conversations", None)
    if conversations is None or not callable(getattr(conversations, "create", None)):
        raise RuntimeError(
            "The installed OpenAI SDK does not expose client.conversations.create(). "
            "Upgrade the OpenAI Python SDK before running 2responses_multiturn."
        )
    items_resource = getattr(conversations, "items", None)
    if items_resource is None or not callable(getattr(items_resource, "create", None)):
        raise RuntimeError(
            "The installed OpenAI SDK does not expose client.conversations.items.create(). "
            "Upgrade the OpenAI Python SDK before running 2responses_multiturn."
        )

    source_items = _source_conversation_items(chunks)
    conversation = conversations.create(
        metadata={
            "variant": V2_RESPONSES_MULTITURN_VARIANT_KEY,
            "source_context_mode": SOURCE_CONTEXT_MODE,
        },
        timeout=timeout,
    )
    conversation_id = _conversation_id(conversation)
    batches = _batches(source_items, CONVERSATION_SOURCE_BATCH_SIZE)
    batch_records: list[dict[str, Any]] = []
    for index, batch in enumerate(batches, start=1):
        items_resource.create(
            conversation_id,
            items=batch,
            timeout=timeout,
        )
        batch_records.append(
            {
                "batch_index": index,
                "item_count": len(batch),
            }
        )

    state = {
        "prompt_strategy": V2_RESPONSES_MULTITURN_PROMPT_STRATEGY,
        "source_context_mode": SOURCE_CONTEXT_MODE,
        "session_mode": SESSION_MODE,
        "conversation_id": conversation_id,
        "source_item_count": len(source_items),
        "source_item_batch_count": len(batch_records),
        "source_item_batches": batch_records,
        "source_chunk_count": len(chunks),
        "conversation": _to_plain(conversation),
    }
    write_json(_source_paths(output_dir)["conversation_state"], state)
    return state


def _context_management(compact_threshold: int) -> list[dict[str, Any]]:
    return [{"type": "compaction", "compact_threshold": compact_threshold}]


def _run_page_turn(
    *,
    client: Any,
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    compact_threshold: int,
    max_completion_tokens: int | None,
    page: V2ResponsesMultiturnPageInput,
    conversation_id: str,
    accumulated: list[dict[str, Any]],
) -> tuple[V2ResponsesMultiturnPageResult, list[dict[str, Any]]]:
    system_prompt, user_prompt = _build_final_prompts(
        app_name=page.app_name,
        a11y_xml=page.a11y_xml,
        existing_predicates=accumulated,
    )
    paths = _write_page_prompts(
        output_dir=page.output_dir,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    page_message = _final_user_message(
        user_prompt=user_prompt,
        screenshot_path=page.screenshot_path,
    )
    input_window = [page_message]
    t0 = time.perf_counter()
    try:
        response, request_meta = _create_final_response(
            client=client,
            model=model,
            timeout=timeout,
            input_window=input_window,
            system_prompt=system_prompt,
            prompt_cache_key=prompt_cache_key,
            max_completion_tokens=max_completion_tokens,
            operation=f"v2_responses_multiturn_page:{page.page}",
            context_management=_context_management(compact_threshold),
            conversation=conversation_id,
            store=None,
        )
    except Exception as exc:
        setattr(exc, "aifc_page", page.page)
        setattr(exc, "aifc_system_prompt", system_prompt)
        setattr(exc, "aifc_user_prompt", user_prompt)
        raise
    usage = _usage_dict(response)
    parsed, raw_json = _predicate_response_from_final(response)
    latency = time.perf_counter() - t0
    aggregate = {**_empty_usage(), **usage, "latency_sec": latency}
    output_items = _plain_output_items(response)
    output_types = _output_item_type_counts(output_items)
    auto_compaction_item_count = _auto_compaction_item_count(output_items)
    conversation_state = {
        "page": page.page,
        "session_mode": SESSION_MODE,
        "source_context_mode": SOURCE_CONTEXT_MODE,
        "conversation_id": conversation_id,
        "response_id": _response_id(response),
        "auto_compaction_enabled": True,
        "compact_threshold": compact_threshold,
        "input_window_item_count": len(input_window),
        "output_item_count": len(output_items),
        "output_item_types": output_types,
        "auto_compaction_item_count": auto_compaction_item_count,
        "request": request_meta,
    }
    write_json(paths["final_response"], _to_plain(response))
    write_json(paths["conversation_state"], conversation_state)
    result = _build_llm_result(
        model=model,
        parsed=parsed,
        aggregate=aggregate,
        latency=latency,
        max_completion_tokens=max_completion_tokens,
        raw_json=raw_json,
        request_meta={
            "status": "success",
            "api": request_meta.get("api", "responses.parse"),
            "operation": f"v2_responses_multiturn_page:{page.page}",
            "latency_sec": round(latency, 3),
            "usage": aggregate,
            "session_mode": SESSION_MODE,
            "source_context_mode": SOURCE_CONTEXT_MODE,
            "conversation_id": conversation_id,
            "response_id": _response_id(response),
            "auto_compaction_enabled": True,
            "compact_threshold": compact_threshold,
            "auto_compaction_item_count": auto_compaction_item_count,
            "output_item_types": output_types,
            "request": request_meta,
        },
    )
    new_accumulated = merge_state_definitions(
        accumulated,
        [pred.model_dump(exclude_none=True) for pred in parsed.State_Definitions],
    )
    log(
        f"[v2_responses_multiturn] page={page.page} "
        f"predicates={len(parsed.State_Definitions)} "
        f"conversation={conversation_id} "
        f"auto_compaction_items={auto_compaction_item_count}",
        "green",
    )
    return (
        V2ResponsesMultiturnPageResult(
            page=page.page,
            final_result=result,
            accumulated_predicates=new_accumulated,
            paths=paths,
            conversation_state=conversation_state,
        ),
        new_accumulated,
    )


def _build_llm_result(
    *,
    model: str,
    parsed: PredicateResponse,
    aggregate: dict[str, float | int],
    latency: float,
    max_completion_tokens: int | None,
    raw_json: str,
    request_meta: dict[str, Any],
) -> LLMResult:
    return LLMResult(
        model=model,
        variant=V2_RESPONSES_MULTITURN_BASE_VARIANT,
        response=parsed,
        prompt_tokens=int(aggregate["prompt_tokens"]),
        completion_tokens=int(aggregate["completion_tokens"]),
        latency_sec=latency,
        cached_tokens=int(aggregate["cached_tokens"]),
        reasoning_tokens=int(aggregate["reasoning_tokens"]),
        max_completion_tokens=max_completion_tokens,
        request_meta=request_meta,
        raw_json=raw_json,
        messages_sent=[],
    )


def _output_item_type_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        item_type = str(item.get("type") or "unknown")
        counts[item_type] = counts.get(item_type, 0) + 1
    return counts


def _auto_compaction_item_count(items: list[dict[str, Any]]) -> int:
    return sum(
        1
        for item in items
        if "compact" in str(item.get("type") or "").lower()
    )
