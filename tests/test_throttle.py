"""Rate limiting, retry and accounting tests.

The property that matters: a rate-limited call must not be recorded as an
agent failure. A throttled request never reached the model, so recording it as
api_error would violate the integrity contract — it would be indistinguishable
from an agent that genuinely failed to produce a move.

Run: python3 -W ignore::DeprecationWarning -m unittest discover -s tests -v
"""

import asyncio
import sys
import time
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*", category=DeprecationWarning)

from throttle import (
    BudgetExceeded, CallGate, CallStats, RateLimiter, is_retryable,
)


class Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


class Usage:
    def __init__(self, p=10, c=20):
        self.prompt_tokens, self.completion_tokens, self.total_tokens = p, c, p + c


class Resp:
    def __init__(self):
        self.usage = Usage()


def flaky(fail_times, exc):
    """A call that fails `fail_times` times then succeeds."""
    state = {"n": 0}

    async def fn():
        if state["n"] < fail_times:
            state["n"] += 1
            raise exc
        return Resp()

    return fn, state


class TestRetryClassification(unittest.TestCase):
    def test_rate_limit_and_server_errors_are_retryable(self):
        self.assertTrue(is_retryable(Err("rate limit exceeded", 429)))
        self.assertTrue(is_retryable(Err("Internal Server Error", 500)))
        self.assertTrue(is_retryable(Err("Service Unavailable", 503)))

    def test_client_errors_are_not_retryable(self):
        # Retrying a malformed request just burns the rate limit.
        self.assertFalse(is_retryable(Err("Bad Request", 400)))
        self.assertFalse(is_retryable(Err("Unauthorized", 401)))

    def test_transport_failures_without_a_status_are_retryable(self):
        self.assertTrue(is_retryable(Err("connection reset by peer")))
        self.assertTrue(is_retryable(Err("request timed out")))

    def test_unrecognised_errors_are_not_retried(self):
        self.assertFalse(is_retryable(Err("something structural went wrong")))


class TestCallGate(unittest.TestCase):
    def _gate(self, **kw):
        kw.setdefault("per_minute", None)
        kw.setdefault("max_concurrent", 4)
        return CallGate(**kw)

    def test_success_first_time_counts_once(self):
        gate = self._gate()
        fn, _ = flaky(0, Err("x", 429))
        _, attempts = asyncio.run(gate.run(fn))
        self.assertEqual(attempts, 1)
        self.assertEqual(gate.stats.succeeded, 1)
        self.assertEqual(gate.stats.retried, 0)
        self.assertEqual(gate.stats.failed, 0)

    def test_rate_limited_call_succeeds_and_is_not_a_failure(self):
        # The central property. A 429 that later succeeds is a slow success,
        # never an agent failure.
        gate = self._gate(max_retries=4)
        fn, state = flaky(2, Err("rate limit exceeded", 429))
        _, attempts = asyncio.run(gate.run(fn))
        self.assertEqual(state["n"], 2)
        self.assertEqual(attempts, 3)
        self.assertEqual(gate.stats.succeeded, 1)
        self.assertEqual(gate.stats.failed, 0, "a retried success must not count as failed")
        self.assertEqual(gate.stats.retried, 1)
        self.assertEqual(gate.stats.rate_limited, 2)

    def test_exhausted_retries_raise_so_the_caller_records_api_error(self):
        gate = self._gate(max_retries=3)
        fn, _ = flaky(99, Err("rate limit exceeded", 429))
        with self.assertRaises(Err):
            asyncio.run(gate.run(fn))
        self.assertEqual(gate.stats.failed, 1)
        self.assertEqual(gate.stats.succeeded, 0)

    def test_non_retryable_error_fails_immediately(self):
        gate = self._gate(max_retries=5)
        fn, state = flaky(99, Err("Bad Request", 400))
        with self.assertRaises(Err):
            asyncio.run(gate.run(fn))
        self.assertEqual(state["n"], 1, "a 400 must not be retried")
        self.assertEqual(gate.stats.retry_attempts, 0)

    def test_token_usage_is_accumulated(self):
        gate = self._gate()
        fn, _ = flaky(0, Err("x"))
        asyncio.run(gate.run(fn))
        self.assertEqual(gate.stats.total_tokens, 30)
        self.assertEqual(gate.stats.prompt_tokens, 10)

    def test_call_ceiling_stops_the_run(self):
        gate = self._gate(max_calls=2)

        async def go():
            fn, _ = flaky(0, Err("x"))
            await gate.run(fn)
            await gate.run(fn)
            await gate.run(fn)

        with self.assertRaises(BudgetExceeded):
            asyncio.run(go())
        self.assertEqual(gate.stats.succeeded, 2)

    def test_concurrency_is_capped(self):
        gate = self._gate(max_concurrent=2)
        live, peak = {"n": 0}, {"n": 0}

        async def fn():
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
            await asyncio.sleep(0.02)
            live["n"] -= 1
            return Resp()

        async def go():
            await asyncio.gather(*[gate.run(fn) for _ in range(8)])

        asyncio.run(go())
        self.assertLessEqual(peak["n"], 2)


class TestRateLimiter(unittest.TestCase):
    def test_unlimited_limiter_does_not_block(self):
        limiter = RateLimiter(None)

        async def go():
            start = time.monotonic()
            for _ in range(50):
                await limiter.acquire()
            return time.monotonic() - start

        self.assertLess(asyncio.run(go()), 0.5)

    def test_limiter_admits_up_to_the_ceiling_without_delay(self):
        limiter = RateLimiter(10)

        async def go():
            start = time.monotonic()
            for _ in range(10):
                await limiter.acquire()
            return time.monotonic() - start

        self.assertLess(asyncio.run(go()), 0.5, "first N in a window should not block")

    def test_limiter_blocks_once_the_window_is_full(self):
        limiter = RateLimiter(2)

        async def go():
            for _ in range(2):
                await limiter.acquire()
            # The third must wait for the window to roll.
            task = asyncio.ensure_future(limiter.acquire())
            await asyncio.sleep(0.05)
            done = task.done()
            task.cancel()
            return done

        self.assertFalse(asyncio.run(go()), "third call should still be waiting")


class TestCallStats(unittest.TestCase):
    def test_reports_throughput(self):
        s = CallStats()
        s.succeeded = 60
        d = s.as_dict()
        self.assertIn("calls_per_minute", d)
        self.assertIn("elapsed_s", d)
        self.assertNotIn("started_at", d)


if __name__ == "__main__":
    unittest.main()
