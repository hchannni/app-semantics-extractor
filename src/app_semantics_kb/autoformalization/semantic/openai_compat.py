"""Optional OpenAI import used by prepare-only reproduction paths."""

from __future__ import annotations

from typing import Any


class MissingOpenAIDependencyError(RuntimeError):
    """Raised when an API path is used without the OpenAI SDK installed."""


try:
    import openai as openai  # type: ignore[import-not-found]
except ModuleNotFoundError:

    class _MissingOpenAIClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise MissingOpenAIDependencyError(
                "The 'openai' package is required for LLM API calls. "
                "Install it before running non-prepare-only variants."
            )

    class _MissingOpenAIModule:
        OpenAI = _MissingOpenAIClient
        OpenAIError = MissingOpenAIDependencyError
        APIConnectionError = MissingOpenAIDependencyError
        RateLimitError = MissingOpenAIDependencyError
        APITimeoutError = MissingOpenAIDependencyError
        InternalServerError = MissingOpenAIDependencyError

    openai = _MissingOpenAIModule()  # type: ignore[assignment]
