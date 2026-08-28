"""
llm_client.py — Pydantic Structured Output 기반 LLM 호출 모듈

AGENTS.md §3.2~3.3 준수:
- Pydantic BaseModel 정의 후 OpenAI Structured Outputs API로 스키마 강제
- tenacity를 이용한 지수 백오프 재시도 로직
- 타입 안전한 LLMResult dataclass 반환
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from ..utils import log
from dotenv import load_dotenv
from .api_diagnostics import (
    attach_failure_meta as _attach_failure_meta,
    choice_finish_reason as _choice_finish_reason,
    completion_request_id as _completion_request_id,
    extract_cached_tokens as _extract_cached_tokens,
    extract_reasoning_tokens as _extract_reasoning_tokens,
    finalize_chat_request_success as _finalize_request_success,
    log_chat_request_start as _log_request_start,
    start_chat_request_meta as _start_request_meta,
)
from .openai_compat import openai

load_dotenv()

DEFAULT_SESSION_MAX_COMPLETION_TOKENS = 12000

# ─────────────────────────────────────────────
# Pydantic 스키마 (Structured Outputs)
# ─────────────────────────────────────────────

class Variable(BaseModel):
    name: str = Field(description="Variable identifier within the predicate")
    type: str = Field(
        description="VSA DSL type: Boolean | String | Number | Time | Date | Enum"
    )
    description: str = Field(description="What this variable represents")
    value_options: list[str] | None = Field(
        default=None,
        description="Allowed enum values — only required when type is Enum",
    )


class StatePredicate(BaseModel):
    name: str = Field(description="Predicate name in PascalCase, concept-focused")
    description: str = Field(description="What app state this predicate captures")
    variables: list[Variable] = Field(
        description="Variables forming the predicate's constraints"
    )


class PredicateResponse(BaseModel):
    """OpenAI Structured Outputs 응답 루트 모델.

    response_format=PredicateResponse 로 API에 전달되면
    LLM이 이 스키마를 반드시 준수하는 JSON을 생성한다.
    """

    Analysis: str = Field(
        description="Brief reasoning about the identified states and predicates"
    )
    State_Definitions: list[StatePredicate] = Field(
        description="List of state predicates extracted from the current screen"
    )


class V2ChunkCandidate(BaseModel):
    predicate: str = Field(description="Predicate/model name this variable belongs to")
    variable: str = Field(description="Source-level variable identifier to include")
    type: str = Field(
        description="VSA DSL type: Boolean | String | Number | Time | Date | Enum"
    )
    description: str = Field(description="Plain-language description of the variable")


class V2ChunkCandidateResponse(BaseModel):
    """Structured map-stage response for V2-chunked raw source experiments."""

    Analysis: str = Field(
        description="Brief note about which identifiers in this chunk were selected"
    )
    candidates: list[V2ChunkCandidate] = Field(
        description="Predicate-variable candidates grounded in this source chunk"
    )


# ─────────────────────────────────────────────
# FP/FN 분석 스키마 (2nd turn)
# ─────────────────────────────────────────────

class FNVariableItem(BaseModel):
    predicate_name: str = Field(
        description="Name of the predicate this variable belongs to (from ground truth)."
    )
    variable_name: str = Field(
        description="Name of the variable that was missing from the generated output."
    )
    evidence_in_context: str = Field(
        description=(
            "Whether evidence for this variable was present in the provided context "
            "(accessibility tree resource IDs, source code fields/methods). "
            "Be specific: name the actual identifier if it was there."
        )
    )
    what_would_help: str = Field(
        description=(
            "What additional information or context (e.g., a specific code field, "
            "a clearer UI element, a different prompt instruction) would have caused "
            "you to generate this variable."
        )
    )


class FPVariableItem(BaseModel):
    predicate_name: str = Field(
        description="Name of the predicate this variable belongs to (from LLM output)."
    )
    variable_name: str = Field(
        description="Name of the variable that was generated but is not in the ground truth."
    )
    generation_trigger: str = Field(
        description=(
            "What specifically in the provided context (screenshot, accessibility tree, "
            "source code) triggered you to generate this variable."
        )
    )
    why_likely_not_needed: str = Field(
        description=(
            "Why this variable is likely not a necessary state dimension "
            "for testing purposes, in retrospect."
        )
    )


class FPFNAnalysisResponse(BaseModel):
    """2nd turn FP/FN 원인 분석 응답 스키마. 모든 분석은 variable 단위로 수행된다."""

    fn_variables: list[FNVariableItem] = Field(
        description=(
            "Variables present in ground truth but not generated. "
            "Includes variables from fully missed predicates and from matched predicates "
            "that are missing some variables."
        )
    )
    fp_variables: list[FPVariableItem] = Field(
        description=(
            "Variables generated but not present in ground truth. "
            "Includes variables from spurious predicates and from matched predicates "
            "that have extra variables."
        )
    )
    reflection: str = Field(
        description=(
            "1-3 sentence overall pattern: what types of variables does "
            "the current context cause to be missed or over-generated, and why?"
        )
    )


# ─────────────────────────────────────────────
# 반환 타입
# ─────────────────────────────────────────────

@dataclass
class LLMResult:
    model: str
    variant: int
    response: PredicateResponse
    prompt_tokens: int
    completion_tokens: int
    latency_sec: float
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    max_completion_tokens: int | None = None
    request_meta: dict[str, Any] = field(default_factory=dict, repr=False)
    raw_json: str = field(default="", repr=False)
    messages_sent: list[dict] = field(default_factory=list, repr=False)


@dataclass
class FPFNResult:
    model: str
    variant: int
    response: FPFNAnalysisResponse
    prompt_tokens: int
    completion_tokens: int
    latency_sec: float
    reasoning_tokens: int = 0
    request_meta: dict[str, Any] = field(default_factory=dict, repr=False)
    raw_json: str = field(default="", repr=False)


@dataclass
class V2ChunkResult:
    model: str
    variant: int
    response: V2ChunkCandidateResponse
    prompt_tokens: int
    completion_tokens: int
    latency_sec: float
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    request_meta: dict[str, Any] = field(default_factory=dict, repr=False)
    raw_json: str = field(default="", repr=False)
    messages_sent: list[dict] = field(default_factory=list, repr=False)


# ─────────────────────────────────────────────
# 재시도 데코레이터
# ─────────────────────────────────────────────

_RETRYABLE = (
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.InternalServerError,
)


def _retry_decorator():
    """Fail-fast decorator placeholder.

    V2'/S1/S2 requests are large enough that automatic retries can multiply
    cost after client-side timeouts. Keep the decorator name for import
    compatibility, but do not retry here.
    """
    def _decorator(func):
        return func

    return _decorator


# ─────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────

def _encode_screenshot(path: Path) -> str:
    """이미지 파일을 base64 data URL로 인코딩한다."""
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def _build_messages(
    system_prompt: str,
    user_prompt: str,
    screenshot_path: Path | None,
) -> list[dict]:
    """OpenAI Chat Completions messages 리스트를 구성한다."""
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": build_user_content(user_prompt, screenshot_path)})
    return messages


def build_user_content(
    user_prompt: str,
    screenshot_path: Path | None,
) -> list[dict]:
    """OpenAI user content 배열을 구성한다.

    단일 turn과 멀티턴 호출이 같은 image/text content shape을 공유하도록 분리한다.
    """
    if screenshot_path is not None and screenshot_path.exists():
        return [
            {
                "type": "image_url",
                "image_url": {"url": _encode_screenshot(screenshot_path), "detail": "high"},
            },
            {"type": "text", "text": user_prompt},
        ]
    return [{"type": "text", "text": user_prompt}]


def _cache_extra_body(prompt_cache_key: str | None) -> dict[str, str] | None:
    if prompt_cache_key is None:
        return None
    stripped = prompt_cache_key.strip()
    if not stripped:
        return None
    return {"prompt_cache_key": stripped}


# ─────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────

@_retry_decorator()
def query_llm(
    system_prompt: str,
    user_prompt: str,
    screenshot_path: Path | None,
    variant: int,
    model: str = "gpt-5.2",
    api_key: str | None = None,
    timeout: float = 120.0,
    prompt_cache_key: str | None = None,
) -> LLMResult:
    """LLM에 프롬프트를 전송하고 Pydantic 구조화 응답을 반환한다.

    Args:
        system_prompt: 시스템 프롬프트 문자열.
        user_prompt: 사용자 프롬프트 문자열.
        screenshot_path: 스크린샷 이미지 경로 (없으면 None).
        variant: Test Variant 번호 (1~4) — 반환 LLMResult에 기록됨.
        model: OpenAI 모델 ID (기본값: gpt-5.2).
        api_key: OpenAI API 키. None이면 OPENAI_API_KEY 환경 변수 사용.
        timeout: API 응답 타임아웃(초). 기본 120초.

    Returns:
        타입 안전한 LLMResult 인스턴스.

    Raises:
        openai.OpenAIError: 최대 재시도 횟수 초과 시 원본 예외를 재발생.
        ValueError: API 키가 없는 경우.
    """
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "OPENAI_API_KEY 환경 변수가 없거나 api_key 인자가 전달되지 않았습니다."
        )

    client = openai.OpenAI(api_key=resolved_key, timeout=timeout, max_retries=0)
    messages = _build_messages(system_prompt, user_prompt, screenshot_path)

    _log_request_start(
        prefix="[llm_client] querying",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        extra=f" (variant={variant})",
    )
    request_meta, t0 = _start_request_meta(
        api="chat.completions.parse",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        response_format_name=PredicateResponse.__name__,
        extra=f"variant={variant}",
    )

    completion_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": PredicateResponse,
    }
    extra_body = _cache_extra_body(prompt_cache_key)
    if extra_body is not None:
        completion_kwargs["extra_body"] = extra_body

    try:
        completion = client.chat.completions.parse(**completion_kwargs)  # type: ignore[arg-type]
    except Exception as exc:
        failure_meta = _attach_failure_meta(exc, request_meta, started_perf=t0)
        log(
            f"[llm_client] failed latency={failure_meta['latency_sec']:.1f}s "
            f"error={failure_meta['error_type']} "
            f"request_id={failure_meta['request_id']}",
            "red",
        )
        raise

    choice = completion.choices[0]
    parsed: PredicateResponse = choice.message.parsed  # type: ignore[assignment]
    usage = completion.usage
    request_meta = _finalize_request_success(
        request_meta,
        completion=completion,
        choice=choice,
        usage=usage,
        started_perf=t0,
    )
    latency = float(request_meta["latency_sec"])

    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    cached_tokens = _extract_cached_tokens(usage)
    reasoning_tokens = _extract_reasoning_tokens(usage)
    raw_json = choice.message.content or ""

    log(
        f"[llm_client] done  latency={latency:.1f}s  "
        f"tokens={prompt_tokens}+{completion_tokens}  "
        f"cached={cached_tokens}  "
        f"reasoning={reasoning_tokens}  "
        f"finish_reason={_choice_finish_reason(choice)}  "
        f"request_id={_completion_request_id(completion)}  "
        f"predicates={len(parsed.State_Definitions)}",
        "green",
    )

    return LLMResult(
        model=model,
        variant=variant,
        response=parsed,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_sec=latency,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        request_meta=request_meta,
        raw_json=raw_json,
        messages_sent=messages,
    )


@_retry_decorator()
def query_v2_chunk_candidates(
    system_prompt: str,
    user_prompt: str,
    screenshot_path: Path | None,
    variant: int,
    model: str = "gpt-5.2",
    api_key: str | None = None,
    timeout: float = 120.0,
    prompt_cache_key: str | None = None,
) -> V2ChunkResult:
    """V2-chunked map turn: extract predicate-variable candidates from one chunk."""
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "OPENAI_API_KEY 환경 변수가 없거나 api_key 인자가 전달되지 않았습니다."
        )

    client = openai.OpenAI(api_key=resolved_key, timeout=timeout, max_retries=0)
    messages = _build_messages(system_prompt, user_prompt, screenshot_path)

    _log_request_start(
        prefix="[llm_client] querying",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        extra=f" (variant={variant}, v2_chunked_map)",
    )
    request_meta, t0 = _start_request_meta(
        api="chat.completions.parse",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        response_format_name=V2ChunkCandidateResponse.__name__,
        extra=f"variant={variant}, v2_chunked_map",
    )

    completion_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": V2ChunkCandidateResponse,
    }
    extra_body = _cache_extra_body(prompt_cache_key)
    if extra_body is not None:
        completion_kwargs["extra_body"] = extra_body

    try:
        completion = client.chat.completions.parse(**completion_kwargs)  # type: ignore[arg-type]
    except Exception as exc:
        failure_meta = _attach_failure_meta(exc, request_meta, started_perf=t0)
        log(
            f"[llm_client] v2 chunk failed latency={failure_meta['latency_sec']:.1f}s "
            f"error={failure_meta['error_type']} "
            f"request_id={failure_meta['request_id']}",
            "red",
        )
        raise

    choice = completion.choices[0]
    parsed: V2ChunkCandidateResponse = choice.message.parsed  # type: ignore[assignment]
    usage = completion.usage
    request_meta = _finalize_request_success(
        request_meta,
        completion=completion,
        choice=choice,
        usage=usage,
        started_perf=t0,
    )
    latency = float(request_meta["latency_sec"])

    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    cached_tokens = _extract_cached_tokens(usage)
    reasoning_tokens = _extract_reasoning_tokens(usage)
    raw_json = choice.message.content or ""

    log(
        f"[llm_client] v2 chunk done  latency={latency:.1f}s  "
        f"tokens={prompt_tokens}+{completion_tokens}  "
        f"cached={cached_tokens}  reasoning={reasoning_tokens}  "
        f"finish_reason={_choice_finish_reason(choice)}  "
        f"request_id={_completion_request_id(completion)}  "
        f"candidates={len(parsed.candidates)}",
        "green",
    )

    return V2ChunkResult(
        model=model,
        variant=variant,
        response=parsed,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_sec=latency,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        request_meta=request_meta,
        raw_json=raw_json,
        messages_sent=messages,
    )


@_retry_decorator()
def query_llm_followup(
    prior_messages: list[dict],
    prior_response_json: str,
    fpfn_user_prompt: str,
    variant: int,
    model: str = "gpt-5.2",
    api_key: str | None = None,
    timeout: float = 120.0,
) -> FPFNResult:
    """2nd turn: 동일 대화 세션에서 FP/FN 원인을 구조화된 형식으로 질문한다.

    Args:
        prior_messages: query_llm()이 반환한 LLMResult.messages_sent (1st turn 메시지).
        prior_response_json: query_llm()이 반환한 LLMResult.raw_json (1st turn 응답).
        fpfn_user_prompt: Python이 계산한 FP/FN 항목 + 질문 텍스트.
        variant: Test Variant 번호 — FPFNResult에 기록됨.
        model: OpenAI 모델 ID.
        api_key: OpenAI API 키. None이면 환경 변수 사용.
        timeout: API 타임아웃(초).

    Returns:
        FPFNResult 인스턴스.
    """
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "OPENAI_API_KEY 환경 변수가 없거나 api_key 인자가 전달되지 않았습니다."
        )

    messages: list[dict] = prior_messages + [
        {"role": "assistant", "content": prior_response_json},
        {"role": "user", "content": [{"type": "text", "text": fpfn_user_prompt}]},
    ]

    client = openai.OpenAI(api_key=resolved_key, timeout=timeout, max_retries=0)
    _log_request_start(
        prefix="[llm_client] FP/FN followup querying",
        model=model,
        timeout=timeout,
        prompt_cache_key=None,
        messages=messages,
        extra=f" (variant={variant})",
    )
    request_meta, t0 = _start_request_meta(
        api="chat.completions.parse",
        model=model,
        timeout=timeout,
        prompt_cache_key=None,
        messages=messages,
        response_format_name=FPFNAnalysisResponse.__name__,
        extra=f"variant={variant}, fpfn_followup",
    )

    try:
        completion = client.chat.completions.parse(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0,
            response_format=FPFNAnalysisResponse,
        )
    except Exception as exc:
        failure_meta = _attach_failure_meta(exc, request_meta, started_perf=t0)
        log(
            f"[llm_client] FP/FN failed latency={failure_meta['latency_sec']:.1f}s "
            f"error={failure_meta['error_type']} "
            f"request_id={failure_meta['request_id']}",
            "red",
        )
        raise

    choice = completion.choices[0]
    parsed: FPFNAnalysisResponse = choice.message.parsed  # type: ignore[assignment]
    usage = completion.usage
    request_meta = _finalize_request_success(
        request_meta,
        completion=completion,
        choice=choice,
        usage=usage,
        started_perf=t0,
    )
    latency = float(request_meta["latency_sec"])
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    reasoning_tokens = _extract_reasoning_tokens(usage)
    raw_json = choice.message.content or ""

    log(
        f"[llm_client] FP/FN done  latency={latency:.1f}s  "
        f"tokens={prompt_tokens}+{completion_tokens}  "
        f"reasoning={reasoning_tokens}  "
        f"finish_reason={_choice_finish_reason(choice)}  "
        f"request_id={_completion_request_id(completion)}  "
        f"fn={len(parsed.fn_variables)}  fp={len(parsed.fp_variables)}",
        "green",
    )

    return FPFNResult(
        model=model,
        variant=variant,
        response=parsed,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_sec=latency,
        reasoning_tokens=reasoning_tokens,
        request_meta=request_meta,
        raw_json=raw_json,
    )


@_retry_decorator()
def query_llm_in_session(
    system_prompt: str,
    session_messages: list[dict],
    new_user_content: list[dict],
    variant: int,
    model: str = "gpt-5.2",
    api_key: str | None = None,
    timeout: float = 120.0,
    prompt_cache_key: str | None = None,
    max_completion_tokens: int | None = DEFAULT_SESSION_MAX_COMPLETION_TOKENS,
) -> LLMResult:
    """멀티턴 세션에서 다음 predicate-generation turn을 실행한다.

    API surface는 기존 Chat Completions Structured Outputs를 그대로 사용한다.
    차이는 messages 배열에 이전 user/assistant turn을 누적해 전달한다는 점뿐이다.
    """
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "OPENAI_API_KEY 환경 변수가 없거나 api_key 인자가 전달되지 않았습니다."
        )

    messages: list[dict] = (
        [{"role": "system", "content": system_prompt}]
        + list(session_messages)
        + [{"role": "user", "content": new_user_content}]
    )

    client = openai.OpenAI(api_key=resolved_key, timeout=timeout, max_retries=0)
    _log_request_start(
        prefix="[llm_client] session querying",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        max_completion_tokens=max_completion_tokens,
        extra=f" (variant={variant}, prior_turns={len(session_messages)})",
    )
    request_meta, t0 = _start_request_meta(
        api="chat.completions.parse",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        response_format_name=PredicateResponse.__name__,
        max_completion_tokens=max_completion_tokens,
        extra=f"variant={variant}, prior_turns={len(session_messages)}",
    )

    completion_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": PredicateResponse,
    }
    if max_completion_tokens is not None:
        completion_kwargs["max_completion_tokens"] = max_completion_tokens
    extra_body = _cache_extra_body(prompt_cache_key)
    if extra_body is not None:
        completion_kwargs["extra_body"] = extra_body

    try:
        completion = client.chat.completions.parse(**completion_kwargs)  # type: ignore[arg-type]
    except Exception as exc:
        failure_meta = _attach_failure_meta(exc, request_meta, started_perf=t0)
        log(
            f"[llm_client] session failed latency={failure_meta['latency_sec']:.1f}s "
            f"error={failure_meta['error_type']} "
            f"request_id={failure_meta['request_id']}",
            "red",
        )
        raise

    choice = completion.choices[0]
    parsed: PredicateResponse = choice.message.parsed  # type: ignore[assignment]
    usage = completion.usage
    request_meta = _finalize_request_success(
        request_meta,
        completion=completion,
        choice=choice,
        usage=usage,
        started_perf=t0,
    )
    latency = float(request_meta["latency_sec"])
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    cached_tokens = _extract_cached_tokens(usage)
    reasoning_tokens = _extract_reasoning_tokens(usage)
    raw_json = choice.message.content or ""

    log(
        f"[llm_client] session done  latency={latency:.1f}s  "
        f"tokens={prompt_tokens}+{completion_tokens}  "
        f"cached={cached_tokens}  "
        f"reasoning={reasoning_tokens}  "
        f"finish_reason={_choice_finish_reason(choice)}  "
        f"request_id={_completion_request_id(completion)}  "
        f"predicates={len(parsed.State_Definitions)}",
        "green",
    )

    return LLMResult(
        model=model,
        variant=variant,
        response=parsed,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_sec=latency,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        max_completion_tokens=max_completion_tokens,
        request_meta=request_meta,
        raw_json=raw_json,
        messages_sent=messages,
    )


def result_to_dict_list(result: LLMResult) -> list[dict]:
    """LLMResult의 State_Definitions를 raw dict 리스트로 직렬화한다.

    기존 predicate_generation_poc.py의 merge/save 로직과
    호환되는 형태로 변환한다.
    """
    return [pred.model_dump(exclude_none=True) for pred in result.response.State_Definitions]
