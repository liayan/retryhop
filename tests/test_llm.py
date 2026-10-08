import asyncio
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest

from retryhop import (
    CircuitBreaker,
    CircuitOpenError,
    RetryError,
    describe,
    is_transient,
    llm_retry,
    retry_after_of,
    status_code_of,
)


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeAPIError(Exception):
    """Shaped like openai/anthropic APIStatusError."""

    def __init__(self, status_code, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = FakeResponse(status_code, headers)


class APIConnectionError(Exception):
    """Same class name as the SDKs' connection error."""


def no_sleep(_):
    pass


@pytest.mark.parametrize("code,expected", [
    (429, True), (500, True), (502, True), (503, True), (504, True), (529, True),
    (408, True), (400, False), (401, False), (403, False), (404, False), (422, False),
])
def test_status_classification(code, expected):
    assert is_transient(FakeAPIError(code)) is expected


def test_network_errors_are_transient():
    assert is_transient(APIConnectionError("reset"))
    assert is_transient(ConnectionResetError())
    assert is_transient(TimeoutError())
    assert not is_transient(ValueError("bad input"))
    assert not is_transient(KeyError("x"))


def test_x_should_retry_header_wins():
    assert not is_transient(FakeAPIError(503, {"x-should-retry": "false"}))
    assert is_transient(FakeAPIError(400, {"X-Should-Retry": "true"}))


def test_retry_after_seconds_ms_and_date():
    assert retry_after_of(FakeAPIError(429, {"Retry-After": "7"})) == 7.0
    assert retry_after_of(FakeAPIError(429, {"retry-after-ms": "1500"})) == 1.5
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    secs = retry_after_of(FakeAPIError(503, {"Retry-After": format_datetime(future, usegmt=True)}))
    assert 25 <= secs <= 31
    assert retry_after_of(FakeAPIError(429)) is None
    assert retry_after_of(FakeAPIError(429, {"Retry-After": "soon"})) is None


def test_status_code_of_response_only():
    class HTTPError(Exception):
        def __init__(self):
            self.response = FakeResponse(502)

    assert status_code_of(HTTPError()) == 502
    assert status_code_of(ValueError()) is None


def test_llm_retry_uses_retry_after():
    calls, slept = [], []
    errors = [FakeAPIError(429, {"retry-after": "3"}), FakeAPIError(503)]

    @llm_retry(sleep=slept.append)
    def call():
        calls.append(1)
        if errors:
            raise errors.pop(0)
        return "answer"

    assert call() == "answer"
    assert len(calls) == 3
    assert 3.0 <= slept[0] <= 3.25      # server hint + small jitter
    assert 0.0 <= slept[1] <= 2.0       # exponential backoff for attempt 2


def test_llm_retry_does_not_retry_client_errors():
    calls = []

    @llm_retry(sleep=no_sleep)
    def call():
        calls.append(1)
        raise FakeAPIError(401)

    with pytest.raises(FakeAPIError):
        call()
    assert len(calls) == 1


def test_llm_retry_reraises_sdk_error_by_default():
    @llm_retry(attempts=2, sleep=no_sleep)
    def call():
        raise FakeAPIError(503)

    with pytest.raises(FakeAPIError):
        call()


def test_llm_retry_retry_error_when_requested():
    @llm_retry(attempts=2, reraise=False, sleep=no_sleep)
    def call():
        raise FakeAPIError(503)

    with pytest.raises(RetryError):
        call()


def test_retry_after_beyond_deadline_gives_up_immediately():
    slept = []

    @llm_retry(deadline=5, sleep=slept.append)
    def call():
        raise FakeAPIError(429, {"retry-after": "60"})

    with pytest.raises(FakeAPIError):
        call()
    assert slept == []


def test_retry_after_is_capped():
    slept = []
    errors = [FakeAPIError(429, {"retry-after": "9999"})]

    @llm_retry(deadline=None, max_retry_after=10, sleep=slept.append)
    def call():
        if errors:
            raise errors.pop(0)
        return 1

    assert call() == 1
    assert 10 <= slept[0] <= 10.25


def test_on_retry_describe():
    lines = []
    errors = [FakeAPIError(429, {"retry-after": "1"})]

    @llm_retry(sleep=no_sleep, on_retry=lambda s: lines.append(describe(s)))
    def call():
        if errors:
            raise errors.pop(0)
        return 1

    call()
    assert "HTTP 429" in lines[0] and "server Retry-After" in lines[0]


def test_llm_retry_async():
    errors = [APIConnectionError(), FakeAPIError(529)]

    async def fake_sleep(_):
        pass

    @llm_retry(sleep=fake_sleep)
    async def call():
        if errors:
            raise errors.pop(0)
        return "ok"

    assert asyncio.run(call()) == "ok"


def test_circuit_breaker_opens_and_recovers():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, recovery_time=10, clock=lambda: now[0])
    calls = []

    @llm_retry(attempts=1, circuit=breaker, sleep=no_sleep)
    def call(fail):
        calls.append(1)
        if fail:
            raise FakeAPIError(503)
        return "ok"

    for _ in range(2):
        with pytest.raises(FakeAPIError):
            call(True)
    assert breaker.state == "open"

    with pytest.raises(CircuitOpenError):
        call(False)
    assert len(calls) == 2               # rejected without calling the API

    now[0] = 11.0
    assert breaker.state == "half_open"
    assert call(False) == "ok"           # trial call succeeds
    assert breaker.state == "closed"


def test_circuit_half_open_failure_reopens():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, recovery_time=5, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 6
    breaker.before_call()                # trial allowed
    with pytest.raises(CircuitOpenError):
        breaker.before_call()            # only one trial at a time
    breaker.record_failure()
    assert breaker.state == "open"


def test_client_errors_do_not_trip_circuit():
    breaker = CircuitBreaker(failure_threshold=1, recovery_time=5)

    @llm_retry(attempts=1, circuit=breaker, sleep=no_sleep)
    def call():
        raise FakeAPIError(400)

    with pytest.raises(FakeAPIError):
        call()
    assert breaker.state == "closed"
