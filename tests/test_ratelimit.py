import asyncio

import pytest

from retryhop import RateLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_requests_per_minute():
    clock = Clock()
    rl = RateLimiter(requests_per_minute=60, clock=clock)  # 1 request / second
    waits = [rl.reserve() for _ in range(62)]
    assert waits[:60] == [0.0] * 60        # the full bucket allows a burst
    assert waits[60] == pytest.approx(1.0)
    assert waits[61] == pytest.approx(2.0)  # callers queue up in order
    clock.t = 10
    assert rl.reserve() == 0.0              # refilled over time


def test_tokens_per_minute():
    clock = Clock()
    rl = RateLimiter(tokens_per_minute=6000, clock=clock)  # 100 tokens / second
    assert rl.reserve(tokens=5000) == 0.0
    assert rl.reserve(tokens=2000) == pytest.approx(10.0)


def test_both_limits_take_the_longer_wait():
    clock = Clock()
    rl = RateLimiter(requests_per_minute=1000, tokens_per_minute=600, clock=clock)
    rl.reserve(tokens=600)
    assert rl.reserve(tokens=60) == pytest.approx(6.0)


def test_consume_and_refund():
    clock = Clock()
    rl = RateLimiter(tokens_per_minute=600, clock=clock)  # 10 tokens / second
    rl.reserve(tokens=500)
    rl.consume(200)                          # real usage was higher
    assert rl.reserve(tokens=100) == pytest.approx(20.0)
    rl2 = RateLimiter(tokens_per_minute=600, clock=clock)
    rl2.reserve(tokens=600)
    rl2.consume(-300)                        # estimate was too high: give back
    assert rl2.reserve(tokens=300) == 0.0


def test_single_call_over_limit_is_rejected():
    rl = RateLimiter(tokens_per_minute=1000)
    with pytest.raises(ValueError):
        rl.reserve(tokens=5000)


def test_needs_a_limit():
    with pytest.raises(ValueError):
        RateLimiter()


def test_limit_decorator_sync_and_async():
    rl = RateLimiter(requests_per_minute=10_000, tokens_per_minute=1_000_000)
    seen = []

    @rl.limit(tokens=lambda prompt: len(prompt))
    def ask(prompt):
        seen.append(prompt)
        return prompt.upper()

    @rl.limit()
    async def ask_async(prompt):
        return prompt[::-1]

    assert ask("hi") == "HI"
    assert asyncio.run(ask_async("abc")) == "cba"
