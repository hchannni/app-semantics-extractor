"""Shared API-call diagnostics for autoformalization LLM requests."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from ..utils import log

MAX_FAILURE_ARTIFACT_CHARS = 1_000_000
FAILURE_ARTIFACT_TAIL_CHARS = 12_000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cache_label(prompt_cache_key: str | None) -> str:
    return "yes" if prompt_cache_key and prompt_cache_key.strip() else "no"


def completion_request_id(completion: object) -> str:
    value = (
        getattr(completion, "_request_id", None)
        or getattr(completion, "request_id", None)
    )
    return str(value) if value else "n/a"


def response_request_id(response: Any) -> str:
    return completion_request_id(response)


def response_id(response: Any) -> str:
    value = getattr(response, "id", None)
    if value is None and isinstance(response, dict):
        value = response.get("id")
    return str(value) if value else "n/a"


def choice_finish_reason(choice: object) -> str:
    value = getattr(choice, "finish_reason", None)
    return str(value) if value else "n/a"


def extract_cached_tokens(usage: object | None) -> int:
    """OpenAI usage 객체에서 prompt cache hit token 수를 방어적으로 추출한다."""
    if usage is None:
        return 0
    details = _attr_or_key(usage, "prompt_tokens_details")
    if details is None:
        return 0
    cached = _attr_or_key(details, "cached_tokens")
    try:
        return int(cached or 0)
    except (TypeError, ValueError):
        return 0


def extract_reasoning_tokens(usage: object | None) -> int:
    """OpenAI usage 객체에서 hidden reasoning token 수를 방어적으로 추출한다."""
    if usage is None:
        return 0
    details = _attr_or_key(usage, "completion_tokens_details")
    if details is None:
        details = _attr_or_key(usage, "output_tokens_details")
    if details is None:
        return 0
    reasoning = _attr_or_key(details, "reasoning_tokens")
    try:
        return int(reasoning or 0)
    except (TypeError, ValueError):
        return 0


def chat_usage_meta(usage: object | None) -> dict[str, Any]:
    prompt_tokens = _usage_int(usage, ("prompt_tokens",)) if usage else 0
    completion_tokens = _usage_int(usage, ("completion_tokens",)) if usage else 0
    meta: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cached_tokens": extract_cached_tokens(usage),
        "reasoning_tokens": extract_reasoning_tokens(usage),
    }
    _add_plain_attr(
        meta,
        usage,
        "prompt_tokens_details",
        ("prompt_tokens_details",),
    )
    _add_plain_attr(
        meta,
        usage,
        "completion_tokens_details",
        ("completion_tokens_details", "output_tokens_details"),
    )
    return meta


def responses_usage_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return empty_usage()
    prompt_tokens = _usage_int(usage, ("input_tokens", "prompt_tokens"))
    completion_tokens = _usage_int(usage, ("output_tokens", "completion_tokens"))
    meta: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": _nested_usage_int(
            usage,
            ("output_tokens_details", "completion_tokens_details"),
            "reasoning_tokens",
        ),
        "cached_tokens": _nested_usage_int(
            usage,
            ("input_tokens_details", "prompt_tokens_details"),
            "cached_tokens",
        ),
        "latency_sec": 0.0,
    }
    meta["total_tokens"] = prompt_tokens + completion_tokens
    _add_plain_attr(
        meta,
        usage,
        "input_tokens_details",
        ("input_tokens_details", "prompt_tokens_details"),
    )
    _add_plain_attr(
        meta,
        usage,
        "output_tokens_details",
        ("output_tokens_details", "completion_tokens_details"),
    )
    return meta


def empty_usage() -> dict[str, float | int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "latency_sec": 0.0,
    }


def start_chat_request_meta(
    *,
    api: str,
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    messages: list[dict],
    response_format_name: str,
    max_completion_tokens: int | None = None,
    extra: str = "",
) -> tuple[dict[str, Any], float]:
    meta = {
        "api": api,
        "model": model,
        "timeout_sec": timeout,
        "sdk_max_retries": 0,
        "prompt_cache_key": prompt_cache_key,
        "prompt_cache_key_present": bool(prompt_cache_key and prompt_cache_key.strip()),
        "response_format": response_format_name,
        "max_completion_tokens": max_completion_tokens,
        "message_count": len(messages),
        "text_chars": _messages_text_chars(messages),
        "text_sha256": _messages_text_sha256(messages),
        "image_count": _messages_image_count(messages),
        "image_url_chars": _messages_image_url_chars(messages),
        "operation": extra.strip() or None,
        "started_at": utc_now_iso(),
    }
    return meta, time.perf_counter()


def finalize_chat_request_success(
    meta: dict[str, Any],
    *,
    completion: object,
    choice: object,
    usage: object | None,
    started_perf: float,
) -> dict[str, Any]:
    latency = time.perf_counter() - started_perf
    return {
        **meta,
        "status": "success",
        "ended_at": utc_now_iso(),
        "latency_sec": round(latency, 3),
        "request_id": completion_request_id(completion),
        "response_id": response_id(completion),
        "finish_reason": choice_finish_reason(choice),
        "response": openai_response_state(completion),
        "choice": openai_choice_state(choice),
        "usage": chat_usage_meta(usage),
    }


def start_responses_request_meta(
    *,
    api: str,
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    input_window: list[dict[str, Any]],
    max_completion_tokens: int | None = None,
    instructions: str | None = None,
    operation: str | None = None,
) -> tuple[dict[str, Any], float]:
    meta = {
        "api": api,
        "model": model,
        "timeout_sec": timeout,
        "sdk_max_retries": 0,
        "prompt_cache_key": prompt_cache_key,
        "prompt_cache_key_present": bool(prompt_cache_key and prompt_cache_key.strip()),
        "max_completion_tokens": max_completion_tokens,
        "input_item_count": len(input_window),
        "text_chars": _responses_input_text_chars(input_window)
        + (len(instructions) if instructions else 0),
        "instructions_chars": len(instructions) if instructions else 0,
        "input_sha256": _responses_input_sha256(
            input_window,
            instructions=instructions,
        ),
        "input_window_sha256": _responses_input_sha256(input_window),
        "instructions_sha256": _text_sha256(instructions or ""),
        "image_count": _responses_input_image_count(input_window),
        "image_url_chars": _responses_input_image_url_chars(input_window),
        "operation": operation,
        "started_at": utc_now_iso(),
    }
    return meta, time.perf_counter()


def finalize_responses_request_success(
    meta: dict[str, Any],
    *,
    response: Any,
    started_perf: float,
) -> dict[str, Any]:
    latency = time.perf_counter() - started_perf
    usage = dict(responses_usage_dict(response))
    usage["latency_sec"] = round(latency, 3)
    return {
        **meta,
        "status": "success",
        "ended_at": utc_now_iso(),
        "latency_sec": round(latency, 3),
        "request_id": response_request_id(response),
        "response_id": response_id(response),
        "response": openai_response_state(response),
        "usage": usage,
    }


def attach_failure_meta(
    exc: BaseException,
    meta: dict[str, Any],
    *,
    started_perf: float,
) -> dict[str, Any]:
    failure_meta = finalize_request_failure(
        meta,
        exc=exc,
        started_perf=started_perf,
    )
    try:
        setattr(exc, "aifc_request_meta", failure_meta)
    except Exception:
        pass
    return failure_meta


def attached_request_meta(exc: BaseException) -> dict[str, Any] | None:
    request_meta = getattr(exc, "aifc_request_meta", None)
    return request_meta if isinstance(request_meta, dict) else None


def exception_artifact_candidates(
    exc: BaseException,
    *,
    max_chars: int = MAX_FAILURE_ARTIFACT_CHARS,
) -> list[dict[str, Any]]:
    """Extract raw/partial response payloads from SDK or validation errors.

    The OpenAI SDK and Pydantic expose failed structured outputs through
    different surfaces. Keep this helper defensive so callers can persist
    whatever is available without depending on one exception shape.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_text(filename: str, source: str, text: str) -> int | None:
        if not text:
            return None
        digest = _text_sha256(text)
        if digest in seen:
            return None
        seen.add(digest)
        stored_text = text
        truncated = False
        if max_chars > 0 and len(stored_text) > max_chars:
            stored_text = stored_text[:max_chars]
            truncated = True
        candidates.append(
            {
                "filename": filename,
                "source": source,
                "content_type": "text/plain",
                "char_count": len(text),
                "stored_chars": len(stored_text),
                "truncated": truncated,
                "sha256": digest,
                "text": stored_text,
            }
        )
        return len(candidates) - 1

    def add_json(filename: str, source: str, value: Any) -> None:
        plain = _to_plain(value)
        try:
            text = json.dumps(plain, ensure_ascii=False, indent=2)
        except TypeError:
            text = str(plain)
        index = add_text(filename, source, text)
        if index is not None:
            candidates[index]["content_type"] = "application/json"

    _add_pydantic_error_artifacts(exc, add_text, add_json)
    _add_exception_attr_artifacts(exc, add_text, add_json)
    return candidates


def finalize_request_failure(
    meta: dict[str, Any],
    *,
    exc: BaseException,
    started_perf: float,
) -> dict[str, Any]:
    latency = time.perf_counter() - started_perf
    status_code = exception_status_code(exc)
    return {
        **meta,
        "status": "failure",
        "ended_at": utc_now_iso(),
        "latency_sec": round(latency, 3),
        "request_id": exception_request_id(exc),
        "error_type": type(exc).__name__,
        "error_message": error_message(exc),
        "status_code": status_code,
        "timeout_exceeded": latency >= float(meta.get("timeout_sec") or 0),
        "exception": exception_debug_state(exc),
    }


def exception_debug_state(exc: BaseException) -> dict[str, Any]:
    state: dict[str, Any] = {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message_chars": len(str(exc)),
    }
    if exc.args:
        state["args"] = [error_message(arg) for arg in exc.args[:5]]
    errors_method = getattr(exc, "errors", None)
    if callable(errors_method):
        try:
            errors = errors_method(include_url=False)
        except TypeError:
            try:
                errors = errors_method()
            except Exception:
                errors = None
        except Exception:
            errors = None
        if isinstance(errors, list):
            state["validation_error_count"] = len(errors)
            state["validation_errors"] = [
                _summarize_validation_error(error)
                for error in errors[:10]
                if isinstance(error, dict)
            ]
    response = getattr(exc, "response", None)
    if response is not None:
        state["response"] = openai_response_state(response) or _plain_attrs(
            response,
            ("status_code", "reason_phrase", "url"),
        )
    return state


def _add_pydantic_error_artifacts(
    exc: BaseException,
    add_text: Any,
    add_json: Any,
) -> None:
    errors_method = getattr(exc, "errors", None)
    if not callable(errors_method):
        return
    try:
        errors = errors_method(include_url=False)
    except TypeError:
        try:
            errors = errors_method()
        except Exception:
            return
    except Exception:
        return
    if not isinstance(errors, list):
        return

    sanitized_errors = []
    for index, error in enumerate(errors):
        if not isinstance(error, dict):
            continue
        input_value = error.get("input")
        if isinstance(input_value, str):
            add_text(
                f"pydantic_error_{index}_input.txt",
                f"pydantic_errors[{index}].input",
                input_value,
            )
        sanitized_errors.append(_summarize_validation_error(error))
    if sanitized_errors:
        add_json("pydantic_errors_summary.json", "pydantic_errors", sanitized_errors)


def _add_exception_attr_artifacts(
    exc: BaseException,
    add_text: Any,
    add_json: Any,
) -> None:
    for attr in (
        "body",
        "content",
        "output_text",
        "text",
        "raw_response",
        "raw_json",
    ):
        value = getattr(exc, attr, None)
        if isinstance(value, bytes):
            add_text(
                f"exception_{attr}.txt",
                f"exception.{attr}",
                value.decode("utf-8", errors="replace"),
            )
        elif isinstance(value, str):
            add_text(f"exception_{attr}.txt", f"exception.{attr}", value)
        elif value is not None:
            add_json(f"exception_{attr}.json", f"exception.{attr}", value)

    response = getattr(exc, "response", None)
    if response is None:
        return
    response_text = getattr(response, "text", None)
    if isinstance(response_text, str):
        add_text("exception_response_text.txt", "exception.response.text", response_text)
    response_content = getattr(response, "content", None)
    if isinstance(response_content, bytes):
        add_text(
            "exception_response_content.txt",
            "exception.response.content",
            response_content.decode("utf-8", errors="replace"),
        )
    response_json = getattr(response, "json", None)
    if callable(response_json):
        try:
            add_json("exception_response_json.json", "exception.response.json()", response_json())
        except Exception:
            pass
    response_state = openai_response_state(response) or _to_plain(response)
    if response_state:
        add_json("exception_response_state.json", "exception.response", response_state)


def _summarize_validation_error(error: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("type", "loc", "msg", "ctx"):
        if key in error:
            summary[key] = _to_plain(error[key])
    input_value = error.get("input")
    if isinstance(input_value, str):
        summary["input"] = {
            "type": "str",
            "char_count": len(input_value),
            "sha256": _text_sha256(input_value),
            "head": input_value[:500],
            "tail": input_value[-500:],
        }
    elif input_value is not None:
        summary["input"] = {
            "type": type(input_value).__name__,
            "value": error_message(input_value),
        }
    return summary


def exception_request_id(exc: BaseException) -> str:
    value = getattr(exc, "request_id", None)
    if value:
        return str(value)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            header_value = headers.get("x-request-id")
        except Exception:
            header_value = None
        if header_value:
            return str(header_value)
    return "n/a"


def exception_status_code(exc: BaseException) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def error_message(exc: BaseException) -> str:
    text = str(exc)
    if len(text) > 1000:
        return text[:1000] + "...[truncated]"
    return text


def log_chat_request_start(
    *,
    prefix: str,
    model: str,
    timeout: float,
    prompt_cache_key: str | None,
    messages: list[dict],
    max_completion_tokens: int | None = None,
    extra: str = "",
) -> None:
    max_tokens = "default" if max_completion_tokens is None else str(max_completion_tokens)
    details = (
        f"{prefix} {model}{extra} ... "
        f"timeout={timeout:g}s sdk_retries=0 "
        f"cache_key={cache_label(prompt_cache_key)} "
        f"text_chars={_messages_text_chars(messages)} "
        f"images={_messages_image_count(messages)} "
        f"max_completion_tokens={max_tokens}"
    )
    log(details, "cyan")


def result_usage_dict(
    result: Any,
    *,
    include_cache_fields: bool = True,
    include_request: bool = True,
    include_max_completion_tokens: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_tokens = int(getattr(result, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(result, "completion_tokens", 0) or 0)
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": int(getattr(result, "reasoning_tokens", 0) or 0),
        "total_tokens": prompt_tokens + completion_tokens,
        "latency_sec": round(float(getattr(result, "latency_sec", 0.0) or 0.0), 3),
    }
    if include_cache_fields:
        cached_tokens = int(getattr(result, "cached_tokens", 0) or 0)
        usage["cached_tokens"] = cached_tokens
        usage["cache_hit_rate"] = (
            cached_tokens / prompt_tokens if prompt_tokens > 0 else 0
        )
    if include_max_completion_tokens:
        usage["max_completion_tokens"] = getattr(
            result,
            "max_completion_tokens",
            None,
        )
    request_meta = getattr(result, "request_meta", None)
    if include_request and isinstance(request_meta, dict) and request_meta:
        usage["request"] = request_meta
    if extra:
        usage.update(extra)
    return usage


def llm_trace_dict(
    result: Any,
    *,
    analysis_reasoning: str | None = None,
    include_request: bool = True,
) -> dict[str, Any]:
    trace = {
        "prompt_tokens": int(getattr(result, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(result, "completion_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(result, "reasoning_tokens", 0) or 0),
        "latency_sec": float(getattr(result, "latency_sec", 0.0) or 0.0),
    }
    cached_tokens = int(getattr(result, "cached_tokens", 0) or 0)
    if cached_tokens:
        trace["cached_tokens"] = cached_tokens
    request_meta = getattr(result, "request_meta", None)
    if include_request and isinstance(request_meta, dict) and request_meta:
        trace["request"] = request_meta
    if analysis_reasoning is not None:
        trace["analysis_reasoning"] = analysis_reasoning
    return trace


def aggregate_result_usage(results: list[Any]) -> dict[str, Any]:
    prompt_tokens = sum(
        int(getattr(result, "prompt_tokens", 0) or 0) for result in results
    )
    completion_tokens = sum(
        int(getattr(result, "completion_tokens", 0) or 0) for result in results
    )
    cached_tokens = sum(
        int(getattr(result, "cached_tokens", 0) or 0) for result in results
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": sum(
            int(getattr(result, "reasoning_tokens", 0) or 0) for result in results
        ),
        "cached_tokens": cached_tokens,
        "latency_sec": sum(
            float(getattr(result, "latency_sec", 0.0) or 0.0)
            for result in results
        ),
        "cache_hit_rate": cached_tokens / prompt_tokens if prompt_tokens else 0,
        "requests": [
            request_meta
            for result in results
            for request_meta in [getattr(result, "request_meta", None)]
            if isinstance(request_meta, dict) and request_meta
        ],
    }


def openai_response_state(response: Any) -> dict[str, Any]:
    return _plain_attrs(
        response,
        (
            "id",
            "object",
            "created",
            "model",
            "service_tier",
            "system_fingerprint",
            "status",
            "incomplete_details",
            "error",
        ),
    )


def openai_choice_state(choice: Any) -> dict[str, Any]:
    state = _plain_attrs(choice, ("index", "finish_reason"))
    message = getattr(choice, "message", None)
    if message is not None:
        message_state = _plain_attrs(message, ("role", "refusal"))
        if message_state:
            state["message"] = message_state
    return state


def _content_text_chars(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                total += len(text)
    return total


def _add_plain_attr(
    target: dict[str, Any],
    source: Any,
    output_name: str,
    source_names: tuple[str, ...],
) -> None:
    for name in source_names:
        value = _attr_or_key(source, name)
        if value is None:
            continue
        target[output_name] = _to_plain(value)
        return


def _plain_attrs(source: Any, names: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in names:
        value = _attr_or_key(source, name)
        if value is not None:
            out[name] = _to_plain(value)
    return out


def _attr_or_key(source: Any, name: str) -> Any:
    value = getattr(source, name, None)
    if value is None and isinstance(source, dict):
        value = source.get(name)
    return value


def _to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _content_image_url_chars(content: object) -> int:
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url")
                if isinstance(url, str):
                    total += len(url)
    return total


def _content_image_count(content: object) -> int:
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for part in content
        if isinstance(part, dict) and part.get("type") == "image_url"
    )


def _messages_text_chars(messages: list[dict]) -> int:
    return sum(_content_text_chars(message.get("content")) for message in messages)


def _messages_image_url_chars(messages: list[dict]) -> int:
    return sum(_content_image_url_chars(message.get("content")) for message in messages)


def _messages_image_count(messages: list[dict]) -> int:
    return sum(_content_image_count(message.get("content")) for message in messages)


def _messages_text_sha256(messages: list[dict]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        role = str(message.get("role") or "")
        digest.update(role.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        content = message.get("content")
        if isinstance(content, str):
            digest.update(content.encode("utf-8", errors="replace"))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        digest.update(text.encode("utf-8", errors="replace"))
                digest.update(b"\0")
        digest.update(b"\0\0")
    return digest.hexdigest()


def _responses_input_text_chars(input_window: list[dict[str, Any]]) -> int:
    total = 0
    for item in input_window:
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and part.get("type") in {"input_text", "text"}:
                total += len(text)
    return total


def _responses_input_image_count(input_window: list[dict[str, Any]]) -> int:
    count = 0
    for item in input_window:
        content = item.get("content")
        if not isinstance(content, list):
            continue
        count += sum(
            1
            for part in content
            if isinstance(part, dict) and part.get("type") == "input_image"
        )
    return count


def _responses_input_image_url_chars(input_window: list[dict[str, Any]]) -> int:
    total = 0
    for item in input_window:
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "input_image":
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, str):
                total += len(image_url)
    return total


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _responses_input_sha256(
    input_window: list[dict[str, Any]],
    *,
    instructions: str | None = None,
) -> str:
    payload_value: Any
    if instructions is None:
        payload_value = input_window
    else:
        payload_value = {
            "instructions": instructions,
            "input": input_window,
        }
    payload = json.dumps(payload_value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _usage_int(usage: Any, names: tuple[str, ...]) -> int:
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _nested_usage_int(
    usage: Any,
    detail_names: tuple[str, ...],
    field_name: str,
) -> int:
    for detail_name in detail_names:
        details = getattr(usage, detail_name, None)
        if details is None and isinstance(usage, dict):
            details = usage.get(detail_name)
        if details is None:
            continue
        value = getattr(details, field_name, None)
        if value is None and isinstance(details, dict):
            value = details.get(field_name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0
