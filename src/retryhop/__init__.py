"""retryhop - retries, rate limiting and circuit breaking for LLM and HTTP API calls."""

from .backoff import constant, exponential, linear
from .circuit import CircuitBreaker, CircuitOpenError
from .core import RetryError, RetryState, retry, retry_call
from .llm import (
    RETRYABLE_STATUS,
    describe,
    is_transient,
    llm_retry,
    retry_after_of,
    status_code_of,
)
from .ratelimit import RateLimiter

__version__ = "0.1.0"

__all__ = [
    "retry",
    "retry_call",
    "llm_retry",
    "RetryError",
    "RetryState",
    "RateLimiter",
    "CircuitBreaker",
    "CircuitOpenError",
    "is_transient",
    "retry_after_of",
    "status_code_of",
    "describe",
    "RETRYABLE_STATUS",
    "constant",
    "linear",
    "exponential",
    "__version__",
]
