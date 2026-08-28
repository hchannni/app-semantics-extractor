"""Shared variant naming and alias rules for autoformalization CLIs."""

from __future__ import annotations

import argparse

VariantKey = int | str

BASE_VARIANTS: frozenset[int] = frozenset({1, 2, 3, 4})

V2_CHUNKED_VARIANT_KEY = "2chunked"
V2_CHUNKED_VARIANT_DIR = "variant_2chunked"
V2_CHUNKED_BASE_VARIANT = 2
V2_CHUNKED_PROMPT_STRATEGY = "v2_file_chunked_source_cached_system_prefix_append_dedupe"

V2_COMPACTED_VARIANT_KEY = "2compacted"
V2_COMPACTED_VARIANT_DIR = "variant_2compacted"
V2_COMPACTED_BASE_VARIANT = 2
V2_COMPACTED_PROMPT_STRATEGY = "v2_responses_compaction_raw_source_chain"

V2_COMPACTED_APP_VARIANT_KEY = "2compacted_app"
V2_COMPACTED_APP_VARIANT_DIR = "variant_2compacted_app"
V2_COMPACTED_APP_BASE_VARIANT = 2
V2_COMPACTED_APP_PROMPT_STRATEGY = "v2_app_level_responses_compaction_reused_source"

V2_COMPACTED_PARALLEL_VARIANT_KEY = "2compacted_parallel"
V2_COMPACTED_PARALLEL_VARIANT_DIR = "variant_2compacted_parallel"
V2_COMPACTED_PARALLEL_BASE_VARIANT = 2
V2_COMPACTED_PARALLEL_PROMPT_STRATEGY = (
    "v2_app_level_responses_compaction_parallel_reused_chunks"
)

V2_RESPONSES_MULTITURN_VARIANT_KEY = "2responses_multiturn"
V2_RESPONSES_MULTITURN_VARIANT_DIR = "variant_2responses_multiturn"
V2_RESPONSES_MULTITURN_BASE_VARIANT = 2
V2_RESPONSES_MULTITURN_PROMPT_STRATEGY = "v2_responses_true_multiturn_auto_compaction"

V3_CHUNKED_VARIANT_KEY = "3chunked"
V3_CHUNKED_VARIANT_DIR = "variant_3chunked"
V3_CHUNKED_BASE_VARIANT = 3
V3_CHUNKED_PROMPT_STRATEGY = "v3_context_window_evidence_graph_chunked_merge"

V4_CHUNKED_VARIANT_KEY = "4chunked"
V4_CHUNKED_VARIANT_DIR = "variant_4chunked"
V4_CHUNKED_BASE_VARIANT = 4
V4_CHUNKED_PROMPT_STRATEGY = "v4_context_window_evidence_graph_chunked_merge"

V2_SOURCE_BASELINE_HELP = (
    "v2-chunked aliases 2c/2chunked/v2c and "
    "v2-compacted aliases 2compacted/v2compacted, "
    "plus 2compacted_app, 2compacted_parallel, 2responses_multiturn, "
    "3chunked aliases 3c/v3chunked, and 4chunked aliases 4c/v4chunked"
)

_V2_CHUNKED_ALIASES: frozenset[str] = frozenset({
    "2c",
    "2chunked",
    "v2c",
    "v2-chunked",
    "v2_chunked",
    "v2chunked",
})
_V2_COMPACTED_ALIASES: frozenset[str] = frozenset({
    "2compact",
    "2compacted",
    "v2compact",
    "v2compacted",
    "v2-compact",
    "v2-compacted",
    "v2_compact",
    "v2_compacted",
})
_V2_COMPACTED_APP_ALIASES: frozenset[str] = frozenset({
    "2compactapp",
    "2compact_app",
    "2-compact-app",
    "2compactedapp",
    "2compacted_app",
    "2-compacted-app",
    "v2compactapp",
    "v2compact_app",
    "v2-compact-app",
    "v2compactedapp",
    "v2compacted_app",
    "v2-compacted-app",
})
_V2_COMPACTED_PARALLEL_ALIASES: frozenset[str] = frozenset({
    "2compactparallel",
    "2compact_parallel",
    "2-compact-parallel",
    "2compactedparallel",
    "2compacted_parallel",
    "2-compacted-parallel",
    "v2compactparallel",
    "v2compact_parallel",
    "v2-compact-parallel",
    "v2compactedparallel",
    "v2compacted_parallel",
    "v2-compacted-parallel",
})
_V2_RESPONSES_MULTITURN_ALIASES: frozenset[str] = frozenset({
    "2multiturn",
    "2_multiturn",
    "2-multiturn",
    "2responsemultiturn",
    "2response_multiturn",
    "2-response-multiturn",
    "2responsesmultiturn",
    "2responses_multiturn",
    "2-responses-multiturn",
    "v2multiturn",
    "v2_multiturn",
    "v2-multiturn",
    "v2responsemultiturn",
    "v2response_multiturn",
    "v2-response-multiturn",
    "v2responsesmultiturn",
    "v2responses_multiturn",
    "v2-responses-multiturn",
})

_V3_CHUNKED_ALIASES: frozenset[str] = frozenset({
    "3c",
    "3chunked",
    "3_chunked",
    "3-chunked",
    "v3c",
    "v3chunked",
    "v3_chunked",
    "v3-chunked",
})
_V4_CHUNKED_ALIASES: frozenset[str] = frozenset({
    "4c",
    "4chunked",
    "4_chunked",
    "4-chunked",
    "v4c",
    "v4chunked",
    "v4_chunked",
    "v4-chunked",
})


def is_v2_chunked(variant: VariantKey) -> bool:
    return str(variant).strip().lower() in _V2_CHUNKED_ALIASES


def is_v2_compacted(variant: VariantKey) -> bool:
    return str(variant).strip().lower() in _V2_COMPACTED_ALIASES


def is_v2_compacted_app(variant: VariantKey) -> bool:
    return str(variant).strip().lower() in _V2_COMPACTED_APP_ALIASES


def is_v2_compacted_parallel(variant: VariantKey) -> bool:
    return str(variant).strip().lower() in _V2_COMPACTED_PARALLEL_ALIASES


def is_v2_responses_multiturn(variant: VariantKey) -> bool:
    return str(variant).strip().lower() in _V2_RESPONSES_MULTITURN_ALIASES


def is_v3_chunked(variant: VariantKey) -> bool:
    return str(variant).strip().lower() in _V3_CHUNKED_ALIASES


def is_v4_chunked(variant: VariantKey) -> bool:
    return str(variant).strip().lower() in _V4_CHUNKED_ALIASES


def is_context_window_chunked(variant: VariantKey) -> bool:
    return is_v3_chunked(variant) or is_v4_chunked(variant)


def base_variant(variant: VariantKey) -> int:
    if is_v4_chunked(variant):
        return V4_CHUNKED_BASE_VARIANT
    if is_v3_chunked(variant):
        return V3_CHUNKED_BASE_VARIANT
    if is_v2_responses_multiturn(variant):
        return V2_RESPONSES_MULTITURN_BASE_VARIANT
    if is_v2_compacted_parallel(variant):
        return V2_COMPACTED_PARALLEL_BASE_VARIANT
    if is_v2_compacted_app(variant):
        return V2_COMPACTED_APP_BASE_VARIANT
    if is_v2_compacted(variant):
        return V2_COMPACTED_BASE_VARIANT
    if is_v2_chunked(variant):
        return V2_CHUNKED_BASE_VARIANT
    return int(variant)


def default_variant_dir_name(variant: VariantKey) -> str:
    if is_v4_chunked(variant):
        return V4_CHUNKED_VARIANT_DIR
    if is_v3_chunked(variant):
        return V3_CHUNKED_VARIANT_DIR
    if is_v2_responses_multiturn(variant):
        return V2_RESPONSES_MULTITURN_VARIANT_DIR
    if is_v2_compacted_parallel(variant):
        return V2_COMPACTED_PARALLEL_VARIANT_DIR
    if is_v2_compacted_app(variant):
        return V2_COMPACTED_APP_VARIANT_DIR
    if is_v2_compacted(variant):
        return V2_COMPACTED_VARIANT_DIR
    if is_v2_chunked(variant):
        return V2_CHUNKED_VARIANT_DIR
    return f"variant_{variant}"


def default_prompt_strategy(variant: VariantKey) -> str:
    if is_v4_chunked(variant):
        return V4_CHUNKED_PROMPT_STRATEGY
    if is_v3_chunked(variant):
        return V3_CHUNKED_PROMPT_STRATEGY
    if is_v2_responses_multiturn(variant):
        return V2_RESPONSES_MULTITURN_PROMPT_STRATEGY
    if is_v2_compacted_parallel(variant):
        return V2_COMPACTED_PARALLEL_PROMPT_STRATEGY
    if is_v2_compacted_app(variant):
        return V2_COMPACTED_APP_PROMPT_STRATEGY
    if is_v2_compacted(variant):
        return V2_COMPACTED_PROMPT_STRATEGY
    if is_v2_chunked(variant):
        return V2_CHUNKED_PROMPT_STRATEGY
    if variant == 2:
        return "v2_source_cached_system_prefix"
    return "legacy_variant_prompt"


def parse_variant(value: str) -> VariantKey:
    normalized = value.strip().lower()
    if normalized in _V4_CHUNKED_ALIASES:
        return V4_CHUNKED_VARIANT_KEY
    if normalized in _V3_CHUNKED_ALIASES:
        return V3_CHUNKED_VARIANT_KEY
    if normalized in _V2_RESPONSES_MULTITURN_ALIASES:
        return V2_RESPONSES_MULTITURN_VARIANT_KEY
    if normalized in _V2_COMPACTED_PARALLEL_ALIASES:
        return V2_COMPACTED_PARALLEL_VARIANT_KEY
    if normalized in _V2_COMPACTED_APP_ALIASES:
        return V2_COMPACTED_APP_VARIANT_KEY
    if normalized in _V2_COMPACTED_ALIASES:
        return V2_COMPACTED_VARIANT_KEY
    if normalized in _V2_CHUNKED_ALIASES:
        return V2_CHUNKED_VARIANT_KEY
    try:
        variant = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "variant must be 1..4, v2-chunked, v2-compacted, "
            "2compacted_app, 2compacted_parallel, 2responses_multiturn, "
            f"3chunked, or 4chunked, got {value}"
        ) from exc
    if variant not in BASE_VARIANTS:
        raise argparse.ArgumentTypeError(
            "variant must be 1..4, v2-chunked, v2-compacted, "
            "2compacted_app, 2compacted_parallel, 2responses_multiturn, "
            f"3chunked, or 4chunked, got {variant}"
        )
    return variant


def parse_variant_list(values: list[str]) -> list[VariantKey]:
    variants: list[VariantKey] = []
    for value in values:
        variant = parse_variant(value)
        if variant not in variants:
            variants.append(variant)
    return variants
