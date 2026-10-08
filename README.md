# retryhop

English | [简体中文](https://github.com/liayan/retryhop/blob/main/README.zh-CN.md)

Retries, client-side rate limiting and a circuit breaker for LLM and HTTP API
calls. No runtime dependencies. Works on sync and async functions.

Errors are classified by attribute (`status_code`, `status`,
`response.headers`) and by class name, so retryhop does not import any SDK.
The tests use real exception objects from `openai`, `anthropic`, `httpx` and
`requests`. `aiohttp` errors go through the same checks but are not covered by
tests yet.

Status: alpha. The API may change before 1.0.

```bash
pip install retryhop
```

Python 3.9+.

## Usage

```python
from openai import OpenAI
from retryhop import llm_retry

client = OpenAI(max_retries=0)

@llm_retry()  # defaults: 6 attempts, 300 s deadline
def ask(prompt: str) -> str:
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.choices[0].message.content
```

Async functions work the same way:

```python
from anthropic import AsyncAnthropic
from retryhop import llm_retry

client = AsyncAnthropic(max_retries=0)

@llm_retry(attempts=8, deadline=600)
async def ask(prompt: str) -> str:
    msg = await client.messages.create(
        model=MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text
```

Create the SDK client with `max_retries=0`. The openai and anthropic clients
retry twice by default, so otherwise each retryhop attempt can be up to three
HTTP requests.

When retryhop gives up it re-raises the last exception, so existing handlers
such as `except openai.RateLimitError:` still work. Pass `reraise=False` to
get a `RetryError` instead.

## What is retried

`is_transient(exc)` makes the decision. It checks, in order:

1. The `x-should-retry` response header, if the server sent one.
2. The HTTP status. 408, 409, 429, 500, 502, 503, 504 and 529 are retried.
   Any other status is raised immediately.
3. If there is no status: connection errors and timeouts are retried. This
   covers the built-in `ConnectionError` and `TimeoutError` and the matching
   classes in openai, anthropic, httpx, requests/urllib3 and aiohttp.

Anything else is raised immediately.

409 is on the list because the openai and anthropic SDKs retry it (they treat
it as a lock timeout). 529 is Anthropic's "overloaded". If a 409 from your API
means a real conflict, use `retry(...)` with your own `retry_on`.

## Wait time

- If the error response has `retry-after-ms` or `Retry-After` (seconds or an
  HTTP date), retryhop waits that long plus 0-0.25 s of jitter, capped at
  `max_retry_after`.
- Otherwise it uses exponential backoff with full jitter: a random wait in
  [0, 1 s], then [0, 2 s], [0, 4 s], and so on, with the upper bound capped
  at 60 s.
- `deadline` is the total budget across attempts. If the server asks for a
  wait longer than what is left, retryhop gives up without sleeping. A
  backoff wait is cut short to fit.

The deadline is checked between attempts. It does not cancel a request that
is already running, so also set a request timeout on the client.

## `llm_retry` options

| Option | Default | Description |
|---|---|---|
| `attempts` | `6` | Total calls, including the first. |
| `deadline` | `300` | Seconds across all attempts. `None` disables it. |
| `max_retry_after` | `120` | Upper bound, in seconds, on a server-requested wait. |
| `backoff` | exponential, see above | Used when there is no retry-after header. Any `f(attempt) -> seconds`. |
| `circuit` | `None` | A shared `CircuitBreaker`. |
| `on_retry` | `None` | Called with a `RetryState` before each wait. |
| `reraise` | `True` | `False` raises `RetryError` instead of the last exception. |
| `sleep` | `None` | Replacement sleep function, for tests. |

## Logging

```python
import logging
from retryhop import llm_retry, describe

log = logging.getLogger("llm")

@llm_retry(on_retry=lambda s: log.warning(describe(s)))
def ask(prompt): ...
```

Output looks like:

```
attempt 1 failed: RateLimitError (HTTP 429); waiting 1.10s (server Retry-After), elapsed 0.0s
attempt 2 failed: InternalServerError (HTTP 503); waiting 1.64s (backoff), elapsed 1.1s
```

## Rate limiting

`RateLimiter` keeps one token bucket for requests per minute and one for
tokens per minute. A call that would go over either limit sleeps until there
is room.

```python
from retryhop import RateLimiter, llm_retry

limiter = RateLimiter(requests_per_minute=500, tokens_per_minute=200_000)

def estimate(prompt, **_):
    return len(prompt) // 4 + 1024        # rough input estimate + max output

@llm_retry()
@limiter.limit(tokens=estimate)
def ask(prompt): ...
```

Put `@limiter.limit` below `@llm_retry` so retries are limited too.

The token count is your own estimate, and the provider counts tokens its own
way. If the response reports actual usage, call
`limiter.consume(actual - estimated)`. A negative value gives tokens back.

Without the decorator: `limiter.acquire(tokens=n)` or
`await limiter.acquire_async(tokens=n)`.

Things to know:

- The limiter is thread-safe and can be used from asyncio tasks, but its
  state is per process. If several processes or hosts share one API key,
  split the limits between them.
- The buckets start full, so a full minute's quota can go out at once right
  after startup.
- A single call that needs more tokens than `tokens_per_minute` raises
  `ValueError`.

## Circuit breaker

```python
from retryhop import CircuitBreaker, CircuitOpenError, llm_retry

breaker = CircuitBreaker(failure_threshold=5, recovery_time=30)

@llm_retry(circuit=breaker)
def ask(prompt): ...

try:
    ask("hi")
except CircuitOpenError as e:
    ...  # e.g. fall back to another model; e.retry_in = seconds until the next trial
```

- After `failure_threshold` consecutive failed attempts the circuit opens.
  Calls then raise `CircuitOpenError` without hitting the API.
- After `recovery_time` seconds one trial call is let through. Success closes
  the circuit; failure opens it again. Other calls made during the trial get
  `CircuitOpenError`. If the trial call is cancelled or interrupted, the next
  call becomes the trial.
- Failures are counted per attempt, not per call. With the defaults
  (6 attempts, threshold 5) a single call that keeps getting 503 opens the
  circuit by itself, and that call ends with `CircuitOpenError` rather than
  the SDK exception.
- A non-retryable error such as a 400 counts as a success, because the API
  did respond. It resets the failure count.
- `CircuitOpenError` is never retried.
- State is per process.

## Lower-level API

- `retry(...)`: the decorator `llm_retry` is built on. It takes `attempts`,
  `exceptions`, `retry_on`, `retry_if_result`, `wait_hint`, `backoff`,
  `deadline`, `on_retry`, `reraise`, `circuit` and `sleep`. Its defaults are
  different: 3 attempts, any `Exception` is retried, no deadline, and
  `RetryError` is raised on give-up.

  Polling a batch job:

  ```python
  @retry(attempts=60, retry_if_result=lambda job: job.status != "completed",
         backoff=constant(30))
  def wait_for_batch(job_id):
      return client.batches.retrieve(job_id)
  ```

- `retry_call(func, *args, retry_options={...}, **kwargs)`: same as `retry`
  without decorating.
- `is_transient(exc)`, `status_code_of(exc)`, `retry_after_of(exc)`: the
  checks described above.
- `describe(state)`: formats a `RetryState` for logging.
- `constant`, `linear`, `exponential`: backoff functions.

## Development

```bash
pip install -e ".[sdk-test]"   # ".[test]" skips the tests that need the SDKs
pytest
python -m build && twine check dist/*
```

## License

MIT
