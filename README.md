# retryhop

**Retries, rate limiting and circuit breaking for LLM and HTTP API calls.**
Zero dependencies. Works with sync and async code, and with exceptions from
`openai`, `anthropic`, `httpx`, `requests` and `aiohttp` out of the box.

**专为大模型 API 调用设计的重试 / 限流 / 熔断工具。** 零依赖，同步异步通用，
直接识别 openai、anthropic、httpx、requests、aiohttp 抛出的异常。

```bash
pip install retryhop
```

## Why / 为什么需要它

Calling LLM APIs at any real volume means dealing with:

| Problem 问题 | What retryhop does 处理方式 |
|---|---|
| `429 Too Many Requests` | Waits exactly as long as the server's `Retry-After` / `retry-after-ms` says. 按服务器给出的等待时间重试 |
| `500/502/503/504`, Anthropic `529 overloaded` | Exponential backoff with jitter. 指数退避 + 随机抖动 |
| Timeouts, dropped connections | Retried. 网络错误自动重试 |
| `400/401/403/404/422` | **Not** retried — fixing the request is the only fix. 客户端错误直接报错，不浪费重试 |
| Hitting RPM / TPM limits at all | Client-side `RateLimiter` keeps you under them. 客户端限流，从源头减少 429 |
| Provider outage | `CircuitBreaker` fails fast instead of piling up retries. 熔断，避免越重试越糟 |

## Quick start / 快速上手

```python
from openai import OpenAI
from retryhop import llm_retry

client = OpenAI(max_retries=0)   # let retryhop own the retries (see note below)

@llm_retry()                     # 6 attempts, 5-minute budget, Retry-After aware
def ask(prompt: str) -> str:
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.choices[0].message.content
```

Anthropic, async:

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

> **Note / 注意**: the official SDKs already retry twice by default. Create the
> client with `max_retries=0`, otherwise every retryhop attempt hides up to 3
> real requests. 官方 SDK 默认会自己重试 2 次，请设置 `max_retries=0`，否则重试次数会相乘。

When retryhop gives up it re-raises the SDK's own exception, so your existing
`except openai.RateLimitError:` blocks keep working.
放弃时抛出的仍是 SDK 原始异常，原有的异常处理代码不用改。

## Logging retries / 记录重试日志

```python
import logging
from retryhop import llm_retry, describe

log = logging.getLogger("llm")

@llm_retry(on_retry=lambda s: log.warning(describe(s)))
def ask(prompt): ...

# attempt 1 failed: RateLimitError (HTTP 429); waiting 1.10s (server Retry-After), elapsed 0.0s
# attempt 2 failed: InternalServerError (HTTP 503); waiting 1.64s (backoff), elapsed 1.1s
```

## Rate limiting / 客户端限流

Stay under your plan's requests-per-minute and tokens-per-minute limits.
Put the limiter **inside** the retry decorator so every attempt is counted.

```python
from retryhop import RateLimiter, llm_retry

limiter = RateLimiter(requests_per_minute=500, tokens_per_minute=200_000)

def estimate(prompt, **_):
    return len(prompt) // 4 + 1024        # rough input estimate + max output

@llm_retry()
@limiter.limit(tokens=estimate)
def ask(prompt): ...
```

The limiter is shared safely across threads and asyncio tasks. If you learn
the real usage from the response, correct the estimate with
`limiter.consume(actual - estimated)` (negative values give tokens back).

Without decorators: `limiter.acquire(tokens=n)` / `await limiter.acquire_async(tokens=n)`.

## Circuit breaker / 熔断器

```python
from retryhop import CircuitBreaker, CircuitOpenError, llm_retry

breaker = CircuitBreaker(failure_threshold=5, recovery_time=30)

@llm_retry(circuit=breaker)
def ask(prompt): ...

try:
    ask("hi")
except CircuitOpenError as e:
    print(f"provider is down, try again in {e.retry_in:.0f}s")   # e.g. switch to a fallback model
```

After 5 transient failures in a row the circuit opens and calls fail
immediately. After 30 s one trial call is let through; success closes the
circuit. Client errors (400, 401, ...) do not count as failures.
连续 5 次瞬时错误后熔断，30 秒后放行一次试探请求，成功则恢复。

## `llm_retry` options

| Option | Default | Meaning |
|---|---|---|
| `attempts` | `6` | Total calls including the first. 总调用次数 |
| `deadline` | `300` | Total time budget in seconds; `None` for no limit. If the server asks for a longer wait than is left, give up immediately. 总时间预算 |
| `max_retry_after` | `120` | Cap on a server-requested wait. 服务器要求等待时间的上限 |
| `backoff` | 1s, 2s, 4s … 60s with jitter | Used when the server gives no `Retry-After`. |
| `circuit` | `None` | Shared `CircuitBreaker`. |
| `on_retry` | `None` | Callback with a `RetryState` before each wait. |
| `reraise` | `True` | `False` raises `RetryError` instead of the SDK exception. |

## Building blocks / 底层工具

- `retry(...)` — the general-purpose decorator behind `llm_retry`: choose
  `exceptions`, `retry_on`, `retry_if_result`, `wait_hint`, `backoff`, etc.
  Useful for anything flaky, e.g. polling a batch job until it finishes:

  ```python
  @retry(attempts=60, retry_if_result=lambda job: job.status != "completed",
         backoff=constant(30))
  def wait_for_batch(job_id): return client.batches.retrieve(job_id)
  ```

- `is_transient(exc)` — should this error be retried?
- `retry_after_of(exc)` — seconds from `retry-after-ms` / `Retry-After` (number or HTTP date).
- `status_code_of(exc)` — HTTP status of any SDK / HTTP-library exception.
- `constant`, `linear`, `exponential` — backoff strategies.

Classification order in `is_transient`: the server's `x-should-retry` header
→ HTTP status (408, 409, 429, 500, 502, 503, 504, 529 retry) → network errors
and timeouts.

## Development / 开发

```bash
pip install -e ".[test]"
pytest            # SDK tests run when openai / anthropic / httpx / requests are installed
python -m build && twine check dist/*
```

## License

MIT
