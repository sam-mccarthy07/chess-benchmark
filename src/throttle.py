"""Rate limiting, retries and call accounting.

Free-tier providers are rate limited hard. Without this layer every 429 lands
in the record as a permanent `api_error`, which is not merely lost data — it is
*wrong* data, because a rate-limited call is indistinguishable from an agent
that failed to produce a move. The integrity contract says a record reflects
what the model actually did; a throttled request never reached the model.

So retryable transport failures are retried, and a call that eventually
succeeds is recorded as a success that needed N attempts. Only a call that
exhausts its attempts becomes api_error.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Awaitable, Callable, Optional

from config import (
    MAX_RETRIES,
    RETRY_BASE_DELAY_S,
    RETRY_MAX_DELAY_S,
    MAX_CONCURRENT_CALLS,
    REQUESTS_PER_MINUTE,
)


class BudgetExceeded(RuntimeError):
    """Raised when a run hits its configured call ceiling."""


@dataclass
class CallStats:
    """Accounting for one run.

    `retried` counts calls that needed at least one extra attempt but
    succeeded. A run with a high retry count is still valid — the models did
    respond — but it is slow, and worth seeing rather than inferring from
    wall-clock time.
    """
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    retry_attempts: int = 0
    rate_limited: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        elapsed = max(time.time() - self.started_at, 1e-6)
        d = asdict(self)
        d.pop("started_at", None)
        d["elapsed_s"] = round(elapsed, 1)
        d["calls_per_minute"] = round(self.succeeded / elapsed * 60, 1)
        return d


class RateLimiter:
    """Simple requests-per-minute gate.

    A sliding window rather than a token bucket: providers publish limits as
    "N requests per minute", and matching that shape directly is easier to
    reason about than tuning a bucket to approximate it.
    """

    def __init__(self, per_minute: Optional[int]):
        self.per_minute = per_minute
        self._times: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        if not self.per_minute:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60.0]
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return
                sleep_for = 60.0 - (now - self._times[0]) + 0.01
                await asyncio.sleep(max(sleep_for, 0.01))


def is_retryable(exc: Exception) -> bool:
    """Transport-level failures worth another attempt.

    Deliberately conservative: a 400 means we sent something malformed and
    retrying will produce the same result, while a 401 means the key is wrong.
    Retrying either just burns the rate limit.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        if status == 429 or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False

    text = f"{type(exc).__name__}: {exc}".lower()
    retryable_markers = (
        "429", "rate limit", "rate_limit", "too many requests",
        "timeout", "timed out", "connection", "temporarily unavailable",
        "overloaded", "502", "503", "504", "internal server error",
    )
    return any(m in text for m in retryable_markers)


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    text = f"{exc}".lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


class CallGate:
    """Concurrency cap, rate limit, retry policy and accounting in one object.

    Held per-run rather than global so a test or a second run cannot inherit
    another run's budget state.
    """

    def __init__(
        self,
        per_minute: Optional[int] = REQUESTS_PER_MINUTE,
        max_concurrent: int = MAX_CONCURRENT_CALLS,
        max_retries: int = MAX_RETRIES,
        max_calls: Optional[int] = None,
    ):
        self.limiter = RateLimiter(per_minute)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_retries = max_retries
        self.max_calls = max_calls
        self.stats = CallStats()

    def _check_budget(self):
        if self.max_calls is not None and self.stats.attempted >= self.max_calls:
            raise BudgetExceeded(
                f"call ceiling reached ({self.max_calls}). "
                f"Raise --max-calls or narrow the run."
            )

    async def run(self, fn: Callable[[], Awaitable]):
        """Execute an API call under the gate. Returns (result, attempts).

        Raises the last exception if every attempt fails, so the caller can
        record a genuine api_error — the distinction between "the model failed"
        and "we never reached the model" is preserved by the fact that we only
        give up after exhausting retries.
        """
        self._check_budget()
        self.stats.attempted += 1

        last: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with self.semaphore:
                    await self.limiter.acquire()
                    result = await fn()
            except Exception as e:  # noqa: BLE001 - classified below
                last = e
                if _is_rate_limit(e):
                    self.stats.rate_limited += 1
                if attempt >= self.max_retries or not is_retryable(e):
                    break
                self.stats.retry_attempts += 1
                # Exponential backoff with full jitter. Without jitter, agents
                # revising in parallel would retry in lockstep and collide
                # again on exactly the same tick.
                delay = min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), RETRY_MAX_DELAY_S)
                await asyncio.sleep(random.uniform(0, delay))
                continue

            self.stats.succeeded += 1
            if attempt > 1:
                self.stats.retried += 1
            usage = getattr(result, "usage", None)
            if usage is not None:
                self.stats.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.stats.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
                self.stats.total_tokens += getattr(usage, "total_tokens", 0) or 0
            return result, attempt

        self.stats.failed += 1
        raise last if last else RuntimeError("call failed with no exception recorded")


# Process-wide default, replaced per run by the runner. Kept so that a direct
# call to agents.* outside a configured run still gets retries rather than
# silently having none.
_default_gate: Optional[CallGate] = None


def get_gate() -> CallGate:
    global _default_gate
    if _default_gate is None:
        _default_gate = CallGate()
    return _default_gate


def set_gate(gate: Optional[CallGate]) -> None:
    global _default_gate
    _default_gate = gate
