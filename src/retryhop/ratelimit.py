"""Client-side rate limiter for requests per minute and tokens per minute.

LLM providers limit both the number of requests and the number of tokens per
minute. Throttling before sending cuts down on 429 responses.

Each limit is a token bucket that refills continuously and starts full. A
caller reserves its share right away (the bucket may go negative) and then
sleeps until the deficit has been refilled, so callers wake in the order they
reserved. Threads and asyncio tasks use the same code path. State is per
process.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
from typing import Any, Callable, Optional


class _Bucket:
    def __init__(self, per_minute: float, now: float) -> None:
        if per_minute <= 0:
            raise ValueError("limits must be > 0")
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0  # units per second
        self.level = float(per_minute)
        self.updated = now

    def reserve(self, amount: float, now: float) -> float:
        """Take ``amount`` and return how long to wait before it is covered."""
        self.level = min(self.capacity, self.level + (now - self.updated) * self.rate)
        self.updated = now
        self.level -= amount
        return 0.0 if self.level >= 0 else -self.level / self.rate


class RateLimiter:
    """Limit requests and/or tokens per minute.

    Example::

        limiter = RateLimiter(requests_per_minute=500, tokens_per_minute=200_000)
        limiter.acquire(tokens=estimated_tokens)       # blocks until allowed
        await limiter.acquire_async(tokens=1200)       # same, for asyncio
    """

    def __init__(
        self,
        requests_per_minute: Optional[float] = None,
        tokens_per_minute: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if requests_per_minute is None and tokens_per_minute is None:
            raise ValueError("set requests_per_minute and/or tokens_per_minute")
        self._clock = clock
        now = clock()
        self._requests = _Bucket(requests_per_minute, now) if requests_per_minute else None
        self._tokens = _Bucket(tokens_per_minute, now) if tokens_per_minute else None
        self._lock = threading.Lock()

    def reserve(self, tokens: float = 0) -> float:
        """Reserve one request plus ``tokens``; return seconds the caller must wait."""
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        if self._tokens is not None and tokens > self._tokens.capacity:
            raise ValueError(
                f"a single call needs {tokens} tokens, more than the "
                f"{self._tokens.capacity:.0f} tokens-per-minute limit"
            )
        with self._lock:
            now = self._clock()
            wait = 0.0
            if self._requests is not None:
                wait = max(wait, self._requests.reserve(1, now))
            if self._tokens is not None and tokens:
                wait = max(wait, self._tokens.reserve(tokens, now))
            return wait

    def consume(self, tokens: float) -> None:
        """Record extra token usage after a call, without waiting.

        Useful when you only know the real token count from the response
        (e.g. output tokens): acquire with an estimate, then ``consume`` the
        difference. Pass a negative number to give back an over-estimate.
        """
        if self._tokens is None or not tokens:
            return
        with self._lock:
            now = self._clock()
            if tokens > 0:
                self._tokens.reserve(tokens, now)
            else:
                b = self._tokens
                b.reserve(0, now)  # refill to now
                b.level = min(b.capacity, b.level - tokens)

    def acquire(self, tokens: float = 0) -> None:
        wait = self.reserve(tokens)
        if wait > 0:
            time.sleep(wait)

    async def acquire_async(self, tokens: float = 0) -> None:
        wait = self.reserve(tokens)
        if wait > 0:
            await asyncio.sleep(wait)

    def limit(self, tokens: Optional[Callable[..., float]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form. ``tokens`` receives the call's arguments and returns an estimate.

        Put it *inside* ``@retry`` so every retry attempt is rate-limited too::

            @llm_retry()
            @limiter.limit(tokens=lambda prompt, **kw: len(prompt) // 4 + 500)
            def ask(prompt): ...
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if inspect.iscoroutinefunction(func):
                @functools.wraps(func)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    await self.acquire_async(tokens(*args, **kwargs) if tokens else 0)
                    return await func(*args, **kwargs)

                return async_wrapper

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                self.acquire(tokens(*args, **kwargs) if tokens else 0)
                return func(*args, **kwargs)

            return wrapper

        return decorator
