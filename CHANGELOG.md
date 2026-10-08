# Changelog

## 0.1.0

- `llm_retry`: retry preset for LLM / HTTP APIs that retries only transient
  errors, honours `Retry-After` / `retry-after-ms`, and re-raises SDK exceptions.
- `is_transient`, `retry_after_of`, `status_code_of`, `describe` helpers that work
  with openai, anthropic, httpx, requests and aiohttp exceptions by duck typing.
- `RateLimiter`: requests-per-minute and tokens-per-minute limits, thread- and
  asyncio-safe, with `consume()` to correct token estimates.
- `CircuitBreaker` with closed / open / half-open states.
- General `retry` decorator (sync + async), `retry_call`, `constant` / `linear` /
  `exponential` backoff, `deadline`, `retry_on`, `retry_if_result`, `wait_hint`.
