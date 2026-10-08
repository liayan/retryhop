"""Classify many texts concurrently with an LLM, without tripping rate limits.

    pip install retryhop openai
    OPENAI_API_KEY=... MODEL=<model-name> python batch_classify.py

Shows retry + rate limiting + circuit breaking working together on asyncio.
"""

import asyncio
import logging
import os

from openai import AsyncOpenAI

from retryhop import CircuitBreaker, CircuitOpenError, RateLimiter, describe, llm_retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("batch")

MODEL = os.environ["MODEL"]
client = AsyncOpenAI(max_retries=0)          # retryhop owns retries
limiter = RateLimiter(requests_per_minute=300, tokens_per_minute=100_000)
breaker = CircuitBreaker(failure_threshold=10, recovery_time=60)
MAX_OUTPUT = 5


@llm_retry(attempts=6, circuit=breaker, on_retry=lambda s: log.warning(describe(s)))
@limiter.limit(tokens=lambda text: len(text) // 4 + MAX_OUTPUT + 50)
async def classify(text: str) -> str:
    r = await client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_OUTPUT,
        messages=[
            {"role": "system", "content": "Answer with one word: positive, negative or neutral."},
            {"role": "user", "content": text},
        ],
    )
    return r.choices[0].message.content.strip().lower()


async def main() -> None:
    texts = [f"Review #{i}: the product was {'great' if i % 2 else 'awful'}." for i in range(50)]
    sem = asyncio.Semaphore(20)                 # at most 20 requests in flight

    async def one(text: str):
        async with sem:
            try:
                return await classify(text)
            except CircuitOpenError:
                return "skipped (provider down)"
            except Exception as e:              # non-retryable or out of attempts
                return f"error: {type(e).__name__}"

    results = await asyncio.gather(*(one(t) for t in texts))
    for text, label in zip(texts, results):
        print(f"{label:>24}  {text}")


if __name__ == "__main__":
    asyncio.run(main())
