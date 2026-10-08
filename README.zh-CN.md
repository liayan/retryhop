# retryhop

[English](README.md) | 简体中文

用于大模型 API 和 HTTP API 调用的重试、客户端限流和熔断。没有运行时依赖，同步和异步函数都可以用。

错误分类依据异常的属性（`status_code`、`status`、`response.headers`）和类名，因此 retryhop 不会 import 任何 SDK。测试使用 `openai`、`anthropic`、`httpx`、`requests` 的真实异常对象。`aiohttp` 的异常走同样的判断逻辑，但目前没有测试覆盖。

状态：alpha，1.0 之前 API 可能会变。

```bash
pip install retryhop
```

需要 Python 3.9+。

## 用法

```python
from openai import OpenAI
from retryhop import llm_retry

client = OpenAI(max_retries=0)

@llm_retry()  # 默认：最多 6 次，总时限 300 秒
def ask(prompt: str) -> str:
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.choices[0].message.content
```

异步函数用法相同：

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

创建 SDK 客户端时要设置 `max_retries=0`。openai 和 anthropic 客户端默认自己会重试 2 次，不关掉的话，retryhop 的每一次尝试最多会变成 3 个 HTTP 请求。

放弃重试时，retryhop 会重新抛出最后一次的异常，所以已有的 `except openai.RateLimitError:` 之类的处理不用改。如果想统一拿到 `RetryError`，传 `reraise=False`。

## 哪些错误会重试

由 `is_transient(exc)` 判断，按以下顺序检查：

1. 响应头 `x-should-retry`（服务器有返回时）。
2. HTTP 状态码。408、409、429、500、502、503、504、529 会重试，其他状态码直接抛出。
3. 没有状态码时：连接错误和超时会重试。包括内置的 `ConnectionError`、`TimeoutError`，以及 openai、anthropic、httpx、requests/urllib3、aiohttp 中对应的异常类。

其余异常直接抛出。

409 在列表里，是因为 openai 和 anthropic 的 SDK 也会重试它（当作锁超时处理）。529 是 Anthropic 的 "overloaded"。如果你调用的 API 返回 409 表示真正的冲突，请改用 `retry(...)` 并传入自己的 `retry_on`。

## 等待时间

- 如果错误响应带有 `retry-after-ms` 或 `Retry-After`（秒数或 HTTP 日期），就等这么久，再加 0-0.25 秒的随机抖动，上限为 `max_retry_after`。
- 否则使用带 full jitter 的指数退避：等待时间分别在 [0, 1 秒]、[0, 2 秒]、[0, 4 秒]……中随机取值，上界最大 60 秒。
- `deadline` 是所有尝试加起来的总时间预算。如果服务器要求的等待时间超过剩余预算，retryhop 直接放弃，不再等待；退避时间超过剩余预算时会被截短。

`deadline` 只在两次尝试之间检查，不会中断正在进行的请求，所以客户端上也要设置请求超时。

## `llm_retry` 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `attempts` | `6` | 总调用次数，包括第一次。 |
| `deadline` | `300` | 所有尝试的总秒数。`None` 表示不限制。 |
| `max_retry_after` | `120` | 服务器要求等待时间的上限（秒）。 |
| `backoff` | 指数退避，见上文 | 没有 retry-after 头时使用。可以传任意 `f(attempt) -> 秒数`。 |
| `circuit` | `None` | 共享的 `CircuitBreaker`。 |
| `on_retry` | `None` | 每次等待前调用，参数是 `RetryState`。 |
| `reraise` | `True` | 为 `False` 时抛出 `RetryError`，而不是最后一次的异常。 |
| `sleep` | `None` | 替换 sleep 函数，测试时用。 |

## 记录日志

```python
import logging
from retryhop import llm_retry, describe

log = logging.getLogger("llm")

@llm_retry(on_retry=lambda s: log.warning(describe(s)))
def ask(prompt): ...
```

输出示例：

```
attempt 1 failed: RateLimitError (HTTP 429); waiting 1.10s (server Retry-After), elapsed 0.0s
attempt 2 failed: InternalServerError (HTTP 503); waiting 1.64s (backoff), elapsed 1.1s
```

## 限流

`RateLimiter` 为每分钟请求数和每分钟 token 数各维护一个令牌桶。调用会超出任一限制时，会先 sleep 到有余量为止。

```python
from retryhop import RateLimiter, llm_retry

limiter = RateLimiter(requests_per_minute=500, tokens_per_minute=200_000)

def estimate(prompt, **_):
    return len(prompt) // 4 + 1024        # 输入的粗略估计 + 最大输出

@llm_retry()
@limiter.limit(tokens=estimate)
def ask(prompt): ...
```

`@limiter.limit` 要写在 `@llm_retry` 下面，这样重试也会被限流。

token 数是你自己估计的，服务商有自己的计数方式。如果响应里有实际用量，可以调用 `limiter.consume(actual - estimated)` 修正，传负数表示退回多估的部分。

不用装饰器时：`limiter.acquire(tokens=n)` 或 `await limiter.acquire_async(tokens=n)`。

注意事项：

- 限流器是线程安全的，也可以在 asyncio task 里用，但状态只在当前进程内。如果多个进程或多台机器共用一个 API key，需要自己把额度分给它们。
- 令牌桶初始是满的，所以刚启动时可能一下子用掉一整分钟的额度。
- 单次调用需要的 token 数超过 `tokens_per_minute` 时，会抛出 `ValueError`。

## 熔断器

```python
from retryhop import CircuitBreaker, CircuitOpenError, llm_retry

breaker = CircuitBreaker(failure_threshold=5, recovery_time=30)

@llm_retry(circuit=breaker)
def ask(prompt): ...

try:
    ask("hi")
except CircuitOpenError as e:
    ...  # 例如切换到备用模型；e.retry_in 是距离下次试探的秒数
```

- 连续失败的尝试次数达到 `failure_threshold` 后熔断打开，之后的调用直接抛出 `CircuitOpenError`，不会请求 API。
- 经过 `recovery_time` 秒后放行一次试探调用。成功则关闭熔断，失败则重新打开。试探期间的其他调用会收到 `CircuitOpenError`。
- 失败按每次尝试计数，不是按每次调用。在默认设置下（6 次尝试，阈值 5），一个持续收到 503 的调用自己就能触发熔断，而且这个调用最终抛出的是 `CircuitOpenError`，不是 SDK 的异常。
- 400 这类不重试的错误算作成功，因为 API 确实有响应。它会把失败计数清零。
- `CircuitOpenError` 不会被重试。
- 状态只在当前进程内。

## 底层 API

- `retry(...)`：`llm_retry` 基于的装饰器。参数有 `attempts`、`exceptions`、`retry_on`、`retry_if_result`、`wait_hint`、`backoff`、`deadline`、`on_retry`、`reraise`、`circuit`、`sleep`。默认值和 `llm_retry` 不同：3 次尝试，任何 `Exception` 都重试，没有 deadline，放弃时抛出 `RetryError`。

  轮询 batch 任务：

  ```python
  @retry(attempts=60, retry_if_result=lambda job: job.status != "completed",
         backoff=constant(30))
  def wait_for_batch(job_id):
      return client.batches.retrieve(job_id)
  ```

- `retry_call(func, *args, retry_options={...}, **kwargs)`：和 `retry` 相同，但不用装饰器。
- `is_transient(exc)`、`status_code_of(exc)`、`retry_after_of(exc)`：上文描述的判断函数。
- `describe(state)`：把 `RetryState` 格式化成一行日志。
- `constant`、`linear`、`exponential`：退避函数。

## 开发

```bash
pip install -e ".[sdk-test]"   # 用 ".[test]" 会跳过需要 SDK 的测试
pytest
python -m build && twine check dist/*
```

## 许可证

MIT
