"""Retry helpers tuned for LLM / HTTP APIs.

Works with exceptions from the ``openai`` and ``anthropic`` SDKs, ``httpx``,
``requests`` and ``aiohttp`` without importing any of them: errors are
recognised by duck typing (``status_code``, ``response.headers``, class names).
"""

from __future__ import annotations

import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Optional

from .backoff import Backoff, exponential
from .circuit import CircuitBreaker
from .core import RetryState, retry

#: HTTP status codes worth retrying.
#: 408 request timeout, 409 conflict/lock timeout, 429 rate limited,
#: 5xx server errors, 529 Anthropic "overloaded".
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

# Network-level failures, matched by class name anywhere in the MRO so that no
# third-party package has to be imported.
_TRANSIENT_CLASS_NAMES = frozenset({
    # openai / anthropic SDKs
    "APIConnectionError", "APITimeoutError",
    # httpx
    "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "NetworkError", "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
    # requests / urllib3
    "ConnectionError", "Timeout", "ChunkedEncodingError", "ProtocolError",
    # aiohttp
    "ClientConnectionError", "ServerDisconnectedError", "ServerTimeoutError",
})


def _get_header(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    try:
        value = headers.get(name)  # httpx/requests/aiohttp headers are case-insensitive
        if value is not None:
            return str(value)
    except Exception:
        pass
    if isinstance(headers, Mapping):  # plain dict: compare case-insensitively
        for key, value in headers.items():
            if str(key).lower() == name:
                return str(value)
    return None


def status_code_of(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status code of an exception, or None."""
    for obj, attr in ((exc, "status_code"), (exc, "status"),
                      (getattr(exc, "response", None), "status_code"),
                      (getattr(exc, "response", None), "status")):
        value = getattr(obj, attr, None) if obj is not None else None
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def headers_of(exc: BaseException) -> Any:
    """Best-effort response headers of an exception, or None."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    return headers


def retry_after_of(exc: BaseException) -> Optional[float]:
    """Seconds the server asked us to wait, from ``retry-after-ms`` or ``Retry-After``.

    ``Retry-After`` may be a number of seconds or an HTTP date.
    """
    headers = headers_of(exc)
    raw_ms = _get_header(headers, "retry-after-ms")
    if raw_ms is not None:
        try:
            return max(0.0, float(raw_ms) / 1000.0)
        except ValueError:
            pass
    raw = _get_header(headers, "retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def is_transient(exc: BaseException) -> bool:
    """True if ``exc`` looks like a temporary failure worth retrying.

    Order of checks:
      1. the server's ``x-should-retry`` header, if present
      2. the HTTP status code (429, 5xx, ... retry; 400/401/403/404/422 do not)
      3. network errors and timeouts (by type or class name)
    """
    should = _get_header(headers_of(exc), "x-should-retry")
    if should is not None:
        if should.lower() == "true":
            return True
        if should.lower() == "false":
            return False
    code = status_code_of(exc)
    if code is not None:
        return code in RETRYABLE_STATUS
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return any(cls.__name__ in _TRANSIENT_CLASS_NAMES for cls in type(exc).__mro__)


def describe(state: RetryState) -> str:
    """One-line human-readable description of a retry, handy for logging."""
    exc = state.exception
    if exc is None:
        what = f"rejected result {state.result!r}"
    else:
        code = status_code_of(exc)
        what = f"{type(exc).__name__}" + (f" (HTTP {code})" if code else "")
    source = "server Retry-After" if state.from_server else "backoff"
    return (f"attempt {state.attempt} failed: {what}; "
            f"waiting {state.delay:.2f}s ({source}), elapsed {state.elapsed:.1f}s")


def llm_retry(
    _func: Optional[Callable[..., Any]] = None,
    *,
    attempts: int = 6,
    deadline: Optional[float] = 300.0,
    backoff: Optional[Backoff] = None,
    max_retry_after: float = 120.0,
    circuit: Optional[CircuitBreaker] = None,
    on_retry: Optional[Callable[[RetryState], None]] = None,
    reraise: bool = True,
    sleep: Optional[Callable[[float], Any]] = None,
) -> Any:
    """``retry`` preset for LLM and HTTP API calls (sync or async).

    * retries only transient errors (:func:`is_transient`); 400/401/403/404
      and other client errors are raised at once
    * honours the server's ``Retry-After`` / ``retry-after-ms`` (plus a little
      jitter), capped at ``max_retry_after`` seconds; if the server asks for a
      longer wait than ``deadline`` allows, it gives up instead of sleeping
    * otherwise uses exponential backoff with jitter (1s, 2s, 4s ... up to 60s)
    * by default re-raises the SDK's own exception when giving up, so your
      existing ``except openai.RateLimitError`` blocks keep working

    Tip: the official SDKs also retry internally (2 times by default). Create
    the client with ``max_retries=0`` so retries are not multiplied.
    """

    def hint(exc: Optional[BaseException], result: Any) -> Optional[float]:
        if exc is None:
            return None
        seconds = retry_after_of(exc)
        if seconds is None:
            return None
        return min(seconds, max_retry_after) + random.uniform(0, 0.25)

    return retry(
        _func,
        attempts=attempts,
        exceptions=Exception,
        retry_on=is_transient,
        backoff=backoff or exponential(base=1.0, factor=2.0, max_delay=60.0, jitter=True),
        wait_hint=hint,
        on_retry=on_retry,
        deadline=deadline,
        reraise=reraise,
        circuit=circuit,
        sleep=sleep,
    )
