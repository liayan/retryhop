"""Backoff strategies.

A backoff strategy is any callable ``f(attempt: int) -> float`` that returns
how many seconds to wait after the given (1-based) failed attempt.
"""

from __future__ import annotations

import random
from typing import Callable

Backoff = Callable[[int], float]


def constant(delay: float = 1.0) -> Backoff:
    """Always wait the same number of seconds."""
    if delay < 0:
        raise ValueError("delay must be >= 0")

    def _backoff(attempt: int) -> float:
        return delay

    return _backoff


def linear(start: float = 1.0, step: float = 1.0, max_delay: float = 60.0) -> Backoff:
    """Wait ``start``, ``start + step``, ``start + 2*step`` ... capped at ``max_delay``."""
    if start < 0 or step < 0 or max_delay < 0:
        raise ValueError("start, step and max_delay must be >= 0")

    def _backoff(attempt: int) -> float:
        return min(start + step * (attempt - 1), max_delay)

    return _backoff


def exponential(
    base: float = 0.5,
    factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: bool = True,
) -> Backoff:
    """Wait ``base * factor**(attempt-1)`` seconds, capped at ``max_delay``.

    With ``jitter=True`` ("full jitter") the actual wait is a random value in
    ``[0, computed_delay]``, which spreads retries from many clients apart.
    """
    if base < 0 or max_delay < 0:
        raise ValueError("base and max_delay must be >= 0")
    if factor < 1:
        raise ValueError("factor must be >= 1")

    def _backoff(attempt: int) -> float:
        delay = min(base * factor ** (attempt - 1), max_delay)
        return random.uniform(0, delay) if jitter else delay

    return _backoff
