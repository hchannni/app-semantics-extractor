"""critic_runner.py — S1/S2 drop-only critic helpers.

Step 2는 Step 1 predicate set의 부분집합만 반환할 수 있어야 한다.
이 모듈은 LLM critic 응답이 스키마를 만족하더라도 최종 산출물이 Step 1 후보를
넘어서지 않도록 후처리에서 단조감소를 보장한다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..utils import log
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
from .llm_client import StatePredicate, _cache_extra_body, _retry_decorator
from .openai_compat import openai

DEFAULT_CRITIC_MAX_COMPLETION_TOKENS = 32000


class PredicateVerdict(BaseModel):
    predicate_id: str = Field(description="Temporary predicate ID from Step 1, e.g. P001")
    variable_id: str = Field(description="Temporary variable ID from Step 1, e.g. V001")
    verdict: Literal["keep", "drop"] = Field(description="Whether to keep or drop this variable")
    evidence: str = Field(description="One concise line of grounding evidence")


class CriticResponse(BaseModel):
    verdicts: list[PredicateVerdict] = Field(
        description="Variable-level keep/drop verdicts for Step 1 candidates only"
    )


@dataclass
class CriticResult:
    model: str
    response: CriticResponse
    prompt_tokens: int
    completion_tokens: int
    latency_sec: float
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    max_completion_tokens: int | None = None
    request_meta: dict[str, Any] = field(default_factory=dict, repr=False)
    raw_json: str = ""
    messages_sent: list[dict] | None = None


def predicate_id(index: int) -> str:
    return f"P{index + 1:03d}"


def variable_id(index: int) -> str:
    return f"V{index + 1:03d}"


def serialize_predicate_candidates_with_ids(
    predicates: list[StatePredicate],
) -> str:
    """Step 1 predicates를 critic prompt용 임시 ID JSON으로 직렬화한다."""
    rows: list[dict[str, object]] = []
    for pred_index, pred in enumerate(predicates):
        pred_id = predicate_id(pred_index)
        variables = []
        for var_index, var in enumerate(pred.variables):
            variables.append({
                "variable_id": variable_id(var_index),
                "name": var.name,
                "type": var.type,
                "description": var.description,
                "value_options": var.value_options,
            })
        rows.append({
            "predicate_id": pred_id,
            "name": pred.name,
            "description": pred.description,
            "variables": variables,
        })
    return json.dumps(rows, ensure_ascii=False, indent=2)


def apply_monotone_decrease(
    step1_predicates: list[StatePredicate],
    critic_response: CriticResponse,
) -> list[StatePredicate]:
    """Critic drop 판정만 적용하여 Step 1 predicate set의 부분집합을 반환한다.

    누락 verdict는 keep으로 취급한다. Step 1에 없는 임시 ID는 무시한다.
    """
    valid_pairs = {
        (predicate_id(pred_index), variable_id(var_index))
        for pred_index, pred in enumerate(step1_predicates)
        for var_index, _ in enumerate(pred.variables)
    }
    drop_set = {
        (verdict.predicate_id, verdict.variable_id)
        for verdict in critic_response.verdicts
        if verdict.verdict == "drop"
        and (verdict.predicate_id, verdict.variable_id) in valid_pairs
    }

    result: list[StatePredicate] = []
    for pred_index, pred in enumerate(step1_predicates):
        pred_id = predicate_id(pred_index)
        kept_variables = [
            var
            for var_index, var in enumerate(pred.variables)
            if (pred_id, variable_id(var_index)) not in drop_set
        ]
        if kept_variables:
            result.append(pred.model_copy(update={"variables": kept_variables}))
    return result


def _load_critic_template(prompts_dir: Path) -> str:
    path = prompts_dir / "critic_step2.txt"
    if not path.exists():
        raise FileNotFoundError(f"critic prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _strip_analysis_payload_section(template: str) -> str:
    marker = "\n--- ANALYSIS RESULTS ---"
    if marker not in template:
        return template
    return template.split(marker, 1)[0].rstrip() + "\n"


def build_critic_user_prompt(
    *,
    step1_predicates: list[StatePredicate],
    analysis_payload: str,
    prompts_dir: Path,
    include_analysis_payload: bool = True,
) -> str:
    template = _load_critic_template(prompts_dir)
    if not include_analysis_payload:
        template = _strip_analysis_payload_section(template)
    return template.format(
        predicate_candidates_with_ids=serialize_predicate_candidates_with_ids(
            step1_predicates
        ),
        analysis_payload=analysis_payload,
    )


@_retry_decorator()
def query_critic_in_session(
    *,
    system_prompt: str,
    session_messages: list[dict],
    critic_user_prompt: str,
    model: str = "gpt-5.2",
    api_key: str | None = None,
    timeout: float = 120.0,
    prompt_cache_key: str | None = None,
    max_completion_tokens: int | None = DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
) -> CriticResult:
    """멀티턴 세션에서 Step 2 critic turn을 실행한다."""
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "OPENAI_API_KEY 환경 변수가 없거나 api_key 인자가 전달되지 않았습니다."
        )

    messages: list[dict] = (
        [{"role": "system", "content": system_prompt}]
        + list(session_messages)
        + [{"role": "user", "content": [{"type": "text", "text": critic_user_prompt}]}]
    )

    client = openai.OpenAI(api_key=resolved_key, timeout=timeout, max_retries=0)
    _log_request_start(
        prefix="[critic_runner] querying",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        max_completion_tokens=max_completion_tokens,
        extra=f" (prior_turns={len(session_messages)})",
    )
    request_meta, t0 = _start_request_meta(
        api="chat.completions.parse",
        model=model,
        timeout=timeout,
        prompt_cache_key=prompt_cache_key,
        messages=messages,
        response_format_name=CriticResponse.__name__,
        max_completion_tokens=max_completion_tokens,
        extra=f"critic, prior_turns={len(session_messages)}",
    )

    completion_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": CriticResponse,
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
            f"[critic_runner] failed latency={failure_meta['latency_sec']:.1f}s "
            f"error={failure_meta['error_type']} "
            f"request_id={failure_meta['request_id']}",
            "red",
        )
        raise

    choice = completion.choices[0]
    parsed: CriticResponse = choice.message.parsed  # type: ignore[assignment]
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
        f"[critic_runner] done  latency={latency:.1f}s  "
        f"tokens={prompt_tokens}+{completion_tokens}  "
        f"cached={cached_tokens}  reasoning={reasoning_tokens}  "
        f"finish_reason={_choice_finish_reason(choice)}  "
        f"request_id={_completion_request_id(completion)}  "
        f"verdicts={len(parsed.verdicts)}",
        "green",
    )

    return CriticResult(
        model=model,
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


def run_critic_turn(
    *,
    system_prompt: str,
    session_messages: list[dict],
    step1_predicates: list[StatePredicate],
    analysis_payload: str,
    prompts_dir: Path,
    model: str = "gpt-5.2",
    api_key: str | None = None,
    timeout: float = 120.0,
    prompt_cache_key: str | None = None,
    max_completion_tokens: int | None = DEFAULT_CRITIC_MAX_COMPLETION_TOKENS,
    include_analysis_payload: bool = True,
) -> tuple[list[StatePredicate], CriticResult, str]:
    """Critic turn 실행 후 단조감소 적용 결과를 반환한다."""
    critic_prompt = build_critic_user_prompt(
        step1_predicates=step1_predicates,
        analysis_payload=analysis_payload,
        prompts_dir=prompts_dir,
        include_analysis_payload=include_analysis_payload,
    )
    try:
        critic_result = query_critic_in_session(
            system_prompt=system_prompt,
            session_messages=session_messages,
            critic_user_prompt=critic_prompt,
            model=model,
            api_key=api_key,
            timeout=timeout,
            prompt_cache_key=prompt_cache_key,
            max_completion_tokens=max_completion_tokens,
        )
    except Exception as exc:
        try:
            setattr(exc, "aifc_critic_prompt", critic_prompt)
        except Exception:
            pass
        raise
    final_predicates = apply_monotone_decrease(
        step1_predicates,
        critic_result.response,
    )
    return final_predicates, critic_result, critic_prompt
