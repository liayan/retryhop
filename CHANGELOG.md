# Changelog

## 0.1.0

First release.

- `llm_retry`: retry decorator for LLM and HTTP API calls. Retries only
  transient errors, uses `Retry-After` / `retry-after-ms` when present, and
  re-raises the original exception on give-up.
- `is_transient`, `retry_after_of`, `status_code_of`, `describe`. Exceptions
  are matched by attribute and class name. Tested against openai, anthropic,
  httpx and requests; aiohttp is not tested yet.
- `RateLimiter`: requests-per-minute and tokens-per-minute buckets, usable
  from threads and asyncio, with `consume()` to adjust token estimates.
- `CircuitBreaker` with closed / open / half-open states.
- `retry` decorator (sync and async), `retry_call`, and `constant` / `linear` /
  `exponential` backoff.
