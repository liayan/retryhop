"""Core retry logic: the ``retry`` decorator and ``retry_call`` helper."""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Tuple, Type, TypeVar, Union

from .backoff import Backoff, exponential
from .circuit import CircuitBreaker

T = TypeVar("T")

ExcTypes = Union[Type[BaseException], Tuple[Type[BaseException], ...]]
WaitHint = Callable[[Optional[BaseException], Any], Optional[float]]


@dataclass(frozen=True)
class RetryState:
    """Information passed to the ``on_retry`` callback before each wait."""

    attempt: int  # the attempt that just failed (1-based)
    delay: float  # seconds we are about to sleep
    exception: Optional[BaseException]  # set when the attempt raised
    result: Any  # set when the attempt returned a rejected result
    elapsed: float  # seconds since the first attempt started
    from_server: bool = False  # True if delay came from wait_hint (e.g. Retry-After)


class RetryError(Exception):
    """Raised when all attempts are used up (or the deadline is reached).

    ``last_exception`` is the exception from the final attempt, if any;
    ``last_result`` is the rejected return value, if the final attempt
    returned something that ``retry_if_result`` asked to retry.
    """

    def __init__(
        self,
        attempts: int,
        last_exception: Optional[BaseException] = None,
        last_result: Any = None,
    ) -> None:
        self.attempts = attempts
        self.last_exception = last_exception
        self.last_result = last_result
        reason = repr(last_exception) if last_exception else f"result {last_result!r}"
        super().__init__(f"gave up after {attempts} attempt(s); last outcome: {reason}")


@dataclass(frozen=True)
class _Policy:
    attempts: int
    exceptions: Tuple[Type[BaseException], ...]
    backoff: Backoff
    retry_on: Optional[Callable[[BaseException], bool]]
    retry_if_result: Optional[Callable[[Any], bool]]
    wait_hint: Optional[WaitHint]
    on_retry: Optional[Callable[[RetryState], None]]
    deadline: Optional[float]
    reraise: bool
    circuit: Optional[CircuitBreaker]


def _evaluate(policy: _Policy, exc: Optional[BaseException], result: Any) -> bool:
    """Return True if this outcome is a failure that should be retried.

    Raises ``exc`` directly when the exception is not retryable.
    """
    if exc is not None:
        if policy.retry_on is not None and not policy.retry_on(exc):
            raise exc
        return True
    return policy.retry_if_result is not None and bool(policy.retry_if_result(result))


def _give_up(policy: _Policy, attempt: int, exc: Optional[BaseException], result: Any) -> Any:
    if exc is not None and policy.reraise:
        raise exc
    raise RetryError(attempt, exc, result) from exc


def _plan_wait(policy: _Policy, attempt: int, start: float,
               exc: Optional[BaseException], result: Any) -> Optional[Tuple[float, bool]]:
    """Return (seconds to wait, came_from_server) or None to stop retrying."""
    if attempt >= policy.attempts:
        return None
    hint = policy.wait_hint(exc, result) if policy.wait_hint is not None else None
    from_server = hint is not None
    delay = max(0.0, float(hint)) if from_server else max(0.0, float(policy.backoff(attempt)))
    if policy.deadline is not None:
        remaining = policy.deadline - (time.monotonic() - start)
        if remaining <= 0:
            return None
        if delay > remaining:
            # The server-requested wait does not fit in the remaining budget.
            # Give up now instead of sleeping and then failing anyway.
            if from_server:
                return None
            delay = remaining
    return delay, from_server


def _run_sync(policy: _Policy, func: Callable[..., T], args: tuple, kwargs: dict,
              sleep: Callable[[float], None]) -> T:
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        if policy.circuit is not None:
            policy.circuit.before_call()  # CircuitOpenError is never retried
        exc: Optional[BaseException] = None
        result: Any = None
        try:
            result = func(*args, **kwargs)
        except policy.exceptions as e:
            exc = e
        except BaseException:
            # No outcome to record (e.g. cancelled), but don't hold the
            # half-open trial slot forever.
            if policy.circuit is not None:
                policy.circuit.release_trial()
            raise

        try:
            failed = _evaluate(policy, exc, result)
        except BaseException:
            if policy.circuit is not None:
                policy.circuit.record_success()  # a non-transient error is not an outage
            raise
        if policy.circuit is not None:
            (policy.circuit.record_failure if failed else policy.circuit.record_success)()
        if not failed:
            return result

        plan = _plan_wait(policy, attempt, start, exc, result)
        if plan is None:
            return _give_up(policy, attempt, exc, result)
        delay, from_server = plan
        if policy.on_retry is not None:
            policy.on_retry(RetryState(attempt, delay, exc, result,
                                       time.monotonic() - start, from_server))
        sleep(delay)


async def _run_async(policy: _Policy, func: Callable[..., Awaitable[T]], args: tuple,
                     kwargs: dict, sleep: Callable[[float], Awaitable[None]]) -> T:
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        if policy.circuit is not None:
            policy.circuit.before_call()
        exc: Optional[BaseException] = None
        result: Any = None
        try:
            result = await func(*args, **kwargs)
        except policy.exceptions as e:
            exc = e
        except BaseException:
            # No outcome to record (e.g. cancelled), but don't hold the
            # half-open trial slot forever.
            if policy.circuit is not None:
                policy.circuit.release_trial()
            raise

        try:
            failed = _evaluate(policy, exc, result)
        except BaseException:
            if policy.circuit is not None:
                policy.circuit.record_success()
            raise
        if policy.circuit is not None:
            (policy.circuit.record_failure if failed else policy.circuit.record_success)()
        if not failed:
            return result

        plan = _plan_wait(policy, attempt, start, exc, result)
        if plan is None:
            return _give_up(policy, attempt, exc, result)
        delay, from_server = plan
        if policy.on_retry is not None:
            policy.on_retry(RetryState(attempt, delay, exc, result,
                                       time.monotonic() - start, from_server))
        await sleep(delay)


def retry(
    _func: Optional[Callable[..., Any]] = None,
    *,
    attempts: int = 3,
    exceptions: ExcTypes = Exception,
    retry_on: Optional[Callable[[BaseException], bool]] = None,
    backoff: Optional[Backoff] = None,
    retry_if_result: Optional[Callable[[Any], bool]] = None,
    wait_hint: Optional[WaitHint] = None,
    on_retry: Optional[Callable[[RetryState], None]] = None,
    deadline: Optional[float] = None,
    reraise: bool = False,
    circuit: Optional[CircuitBreaker] = None,
    sleep: Optional[Callable[[float], Any]] = None,
) -> Any:
    """Retry a sync or async function.

    Usable as ``@retry`` or ``@retry(attempts=5, exceptions=ConnectionError)``.

    Args:
        attempts: total number of calls, including the first one.
        exceptions: exception type(s) that may trigger a retry; others propagate at once.
        retry_on: extra check on a caught exception; return False to raise it at once
            (e.g. retry HTTP 429/503 but not 400/401).
        backoff: ``f(attempt) -> seconds``; default is exponential with jitter.
        retry_if_result: return True to retry on a returned value (e.g. ``None``).
        wait_hint: ``f(exception, result) -> seconds | None``; when it returns a number
            (e.g. a server's ``Retry-After``) that wait is used instead of ``backoff``.
        on_retry: callback receiving a :class:`RetryState` before each wait.
        deadline: total time budget in seconds across all attempts. Checked
            between attempts; a running call is not interrupted.
        reraise: when giving up, re-raise the last exception instead of ``RetryError``.
        circuit: a shared :class:`CircuitBreaker`; when open, calls raise
            ``CircuitOpenError`` without running ``func``.
        sleep: custom sleep function (useful in tests); async for async functions.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if deadline is not None and deadline <= 0:
        raise ValueError("deadline must be > 0")
    if not isinstance(exceptions, tuple):
        exceptions = (exceptions,)
    policy = _Policy(
        attempts=attempts,
        exceptions=exceptions,
        backoff=backoff if backoff is not None else exponential(),
        retry_on=retry_on,
        retry_if_result=retry_if_result,
        wait_hint=wait_hint,
        on_retry=on_retry,
        deadline=deadline,
        reraise=reraise,
        circuit=circuit,
    )

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):
            async_sleep = sleep or asyncio.sleep

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _run_async(policy, func, args, kwargs, async_sleep)

            return async_wrapper

        sync_sleep = sleep or time.sleep

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _run_sync(policy, func, args, kwargs, sync_sleep)

        return wrapper

    if _func is not None:  # used as bare @retry
        return decorator(_func)
    return decorator


def retry_call(func: Callable[..., T], *args: Any, retry_options: Optional[dict] = None,
               **kwargs: Any) -> T:
    """Call ``func(*args, **kwargs)`` with retries, without decorating it.

    Example: ``retry_call(requests.get, url, timeout=5, retry_options={"attempts": 5})``
    """
    return retry(**(retry_options or {}))(func)(*args, **kwargs)
