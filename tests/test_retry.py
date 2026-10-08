import asyncio

import pytest

from retryhop import RetryError, constant, exponential, linear, retry, retry_call


class Flaky:
    """Fails ``fail_times`` times, then returns ``value``."""

    def __init__(self, fail_times, exc=ConnectionError, value="ok"):
        self.fail_times = fail_times
        self.exc = exc
        self.value = value
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc(f"fail #{self.calls}")
        return self.value


def no_sleep(_):
    pass


def test_succeeds_after_failures():
    f = Flaky(2)
    wrapped = retry(attempts=3, sleep=no_sleep)(f)
    assert wrapped() == "ok"
    assert f.calls == 3


def test_bare_decorator():
    calls = []

    @retry
    def ok():
        calls.append(1)
        return 42

    assert ok() == 42
    assert len(calls) == 1


def test_gives_up_with_retry_error():
    f = Flaky(10)
    wrapped = retry(attempts=3, sleep=no_sleep)(f)
    with pytest.raises(RetryError) as info:
        wrapped()
    assert info.value.attempts == 3
    assert isinstance(info.value.last_exception, ConnectionError)
    assert f.calls == 3


def test_reraise_original_exception():
    wrapped = retry(attempts=2, reraise=True, sleep=no_sleep)(Flaky(10))
    with pytest.raises(ConnectionError):
        wrapped()


def test_unlisted_exception_not_retried():
    f = Flaky(1, exc=ValueError)
    wrapped = retry(attempts=5, exceptions=ConnectionError, sleep=no_sleep)(f)
    with pytest.raises(ValueError):
        wrapped()
    assert f.calls == 1


def test_retry_if_result():
    results = iter([None, None, "data"])
    wrapped = retry(attempts=5, retry_if_result=lambda r: r is None, sleep=no_sleep)(
        lambda: next(results)
    )
    assert wrapped() == "data"


def test_retry_if_result_gives_up():
    wrapped = retry(attempts=2, retry_if_result=lambda r: r is None, sleep=no_sleep)(
        lambda: None
    )
    with pytest.raises(RetryError) as info:
        wrapped()
    assert info.value.last_exception is None
    assert info.value.last_result is None


def test_on_retry_and_delays():
    states, slept = [], []
    wrapped = retry(attempts=4, backoff=linear(1, 1), on_retry=states.append,
                    sleep=slept.append)(Flaky(3))
    assert wrapped() == "ok"
    assert [s.attempt for s in states] == [1, 2, 3]
    assert slept == [1, 2, 3]


def test_deadline_stops_early():
    slept = []
    wrapped = retry(attempts=100, deadline=0.05, backoff=constant(0.02),
                    sleep=lambda d: (slept.append(d), __import__("time").sleep(d)))(Flaky(1000))
    with pytest.raises(RetryError):
        wrapped()
    assert len(slept) < 10


def test_retry_call():
    f = Flaky(1)
    assert retry_call(f, 1, x=2, retry_options={"attempts": 2, "sleep": no_sleep}) == "ok"


def test_async_function():
    f = Flaky(2)

    async def no_async_sleep(_):
        pass

    @retry(attempts=3, sleep=no_async_sleep)
    async def fetch():
        return f()

    assert asyncio.run(fetch()) == "ok"
    assert f.calls == 3


def test_exponential_backoff_values():
    b = exponential(base=1, factor=2, max_delay=5, jitter=False)
    assert [b(i) for i in range(1, 6)] == [1, 2, 4, 5, 5]
    j = exponential(base=1, factor=2, max_delay=5, jitter=True)
    assert all(0 <= j(i) <= 5 for i in range(1, 10))


def test_invalid_arguments():
    with pytest.raises(ValueError):
        retry(attempts=0)
    with pytest.raises(ValueError):
        exponential(factor=0.5)
