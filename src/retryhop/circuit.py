"""Thread-safe circuit breaker.

States:
    closed     normal operation; consecutive failures are counted
    open       calls are rejected at once with CircuitOpenError
    half_open  after ``recovery_time`` one trial call is let through;
               success closes the circuit, failure opens it again
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit is open."""

    def __init__(self, retry_in: float) -> None:
        self.retry_in = retry_in
        super().__init__(f"circuit is open; next trial in {retry_in:.1f}s")


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_time: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_time <= 0:
            raise ValueError("recovery_time must be > 0")
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_running = False

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state == self.OPEN and self._clock() - self._opened_at >= self.recovery_time:
            self._state = self.HALF_OPEN
            self._trial_running = False

    def before_call(self) -> None:
        """Raise CircuitOpenError if the call must not go through."""
        with self._lock:
            self._maybe_half_open()
            if self._state == self.OPEN:
                remaining = self.recovery_time - (self._clock() - self._opened_at)
                raise CircuitOpenError(max(0.0, remaining))
            if self._state == self.HALF_OPEN:
                if self._trial_running:
                    raise CircuitOpenError(0.0)
                self._trial_running = True

    def record_success(self) -> None:
        with self._lock:
            self._state = self.CLOSED
            self._failures = 0
            self._trial_running = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = self._clock()
                self._trial_running = False

    def release_trial(self) -> None:
        """Free the half-open trial slot without recording a result.

        For calls that end with no outcome, such as a cancelled task. Without
        this the breaker would stay half-open and reject every later call.
        """
        with self._lock:
            self._trial_running = False

    def reset(self) -> None:
        self.record_success()
