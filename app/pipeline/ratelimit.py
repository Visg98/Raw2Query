"""Client-side request pacer for the LLM provider.

Gemini's free tier meters **requests per minute**, not tokens. That is worth
stating plainly because the obvious assumption is the opposite, and the whole
design follows from it. Measured against the live API:

- `gemini-3.5-flash-lite`: 15 requests/minute
- `gemini-2.5-flash`: 5 requests/minute
- a token limit: never reached, on either model

The numbers come from the provider itself. A 429 body carries a
`google.rpc.QuotaFailure` detail with the exact `quotaValue` and a
`google.rpc.RetryInfo` with a `retryDelay`, so the real quota can be adopted at
runtime instead of trusted from config.

Why this module is not a token budget. The previous provider (Groq) metered
8000 tokens/minute, and one document's extraction run cost ~9,200 tokens - it
could not fit, whatever the pacing. This is now the opposite kind of limit, and
the difference is structural:

- A request limit is **independent of document size**. A 40k-char document
  costs the same one extraction call as a 1k-char one, so throughput no longer
  degrades as documents grow.
- It makes *calls* the thing worth economising, not characters. That is why
  `max_extract_chars` is set high (fewer chunks, fewer requests) rather than
  low, and why merging two classify calls into one is the main throughput
  lever rather than trimming prompts.

Two hard-won details kept from the token-budget version:

**Never advance the refill clock into the future.** An earlier attempt honoured
`retry-after` by pushing the window's start time ahead of now. Because the
projection clamps elapsed time with `max(0.0, now - at)`, the window then read
"full" forever: every iteration computed the same wait from zero capacity. The
observed result was 14 identical 28-second waits - a worker pinned for six and
a half minutes, which starved every other document in the queue. A 429 marks
the window full *as of now* and nothing more; honouring `retry-after` per
request is the SDK's job, and it does it.

**Bound the total wait.** A worker blocked indefinitely on one job stops the
whole queue, and the symptom is a queue that quietly stops moving. Past
`MAX_TOTAL_WAIT_SECONDS` this raises rather than sleeps.

Scope: the window lives in this process's memory, so it is only truthful while
one process is calling the provider - which is why the worker defaults to a
single replica (see run.sh / .env.example). `max_retries` on the client in
llm.py is the backstop for spend this pacer cannot see.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from app.config import get_settings

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0

# The provider counts a request against the minute it arrives in, so pacing to
# exactly 60s leaves no room for clock skew or in-flight requests. A small
# overhang is cheap insurance.
WINDOW_OVERHANG_SECONDS = 1.0

# Longest single sleep before re-checking the window.
MAX_WAIT_SECONDS = 90.0

# Longest a single call will wait in total before giving up. The window is a
# minute wide, so anything beyond a couple of minutes means capacity is not
# coming back and the job should fail loudly instead of holding the worker.
MAX_TOTAL_WAIT_SECONDS = 150.0


class RateLimitTimeout(RuntimeError):
    """Raised when request capacity will not free up in a reasonable time."""


def quota_from_error(exc) -> tuple[int | None, float | None]:
    """Pull `(quotaValue, retryDelaySeconds)` out of a provider 429.

    Gemini returns a list-wrapped body whose `error.details` carry a
    `QuotaFailure` with the real per-minute quota and a `RetryInfo` with the
    delay. Both are reliable - they are how the 15/min and 5/min figures in
    this module's docstring were established. Everything here is defensive:
    a provider that stops sending them must degrade to the configured value,
    not crash the pipeline.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, list):
        body = body[0] if body else None
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None

    quota: int | None = None
    retry_after: float | None = None
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            # Only per-minute request quotas are actionable here; a daily quota
            # is not something waiting a few seconds fixes.
            quota_id = violation.get("quotaId") or ""
            if "PerMinute" not in quota_id:
                continue
            try:
                quota = int(violation["quotaValue"])
            except (KeyError, TypeError, ValueError):
                pass
        raw_delay = detail.get("retryDelay")
        if isinstance(raw_delay, str) and raw_delay.endswith("s"):
            try:
                retry_after = float(raw_delay[:-1])
            except ValueError:
                pass
    return quota, retry_after


class RequestRateLimiter:
    """A rolling one-minute window of request timestamps, safe across threads."""

    def __init__(self, *, requests_per_minute: int, margin: float) -> None:
        self._limit = max(1, requests_per_minute)
        self._margin = margin
        self._capacity = max(1, int(self._limit * margin))
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self._sent and self._sent[0] <= cutoff:
            self._sent.popleft()

    def in_window(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return len(self._sent)

    def acquire(self) -> None:
        """Block until a request slot is free, then take it."""
        started = time.monotonic()
        rounds = 0
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)
                if len(self._sent) < self._capacity:
                    self._sent.append(now)
                    return
                # The oldest request leaving the window is the soonest moment
                # the answer changes, so this wakes once per queued call rather
                # than polling.
                wait = (self._sent[0] + WINDOW_SECONDS + WINDOW_OVERHANG_SECONDS) - now
                used = len(self._sent)

            wait = max(0.0, min(MAX_WAIT_SECONDS, wait))
            waited_so_far = now - started
            if waited_so_far + wait > MAX_TOTAL_WAIT_SECONDS:
                raise RateLimitTimeout(
                    f"waited {waited_so_far:.0f}s for a request slot with {used}/{self._capacity} "
                    "used in the last minute. Something else is likely sharing this API key, or "
                    "LLM_REQUESTS_PER_MINUTE is set higher than the account actually allows."
                )

            rounds += 1
            # First wait is ordinary pacing. Repeats mean capacity is not coming
            # back, which is the shape the future-clock bug took, so escalate
            # rather than emitting the same cheerful line every minute.
            log = logger.info if rounds == 1 else logger.warning
            log(
                "request budget: %d of %d used in the last minute; waiting %.1fs%s",
                used,
                self._capacity,
                wait,
                "" if rounds == 1 else f" [still waiting after {waited_so_far:.0f}s, round {rounds}]",
            )
            time.sleep(wait)

    def note_rate_limited(self, *, reported_quota: int | None = None) -> None:
        """Record that the provider refused a request as rate-limited.

        Fills the window as of now, so the next `acquire` waits for real
        capacity instead of a count that has just been proven wrong. It
        deliberately does not hold the window shut until `retry-after`: see the
        module docstring for what that cost last time.
        """
        with self._lock:
            if reported_quota and reported_quota != self._limit:
                logger.info(
                    "provider reports a %d request/min quota (configured %d); adopting it",
                    reported_quota,
                    self._limit,
                )
                self._limit = max(1, reported_quota)
                self._capacity = max(1, int(self._limit * self._margin))
            now = time.monotonic()
            self._prune(now)
            # Treat the window as full from this instant: the oldest slot frees
            # a minute from now, which is exactly the provider's own reset.
            self._sent = deque([now] * self._capacity)


_limiter: RequestRateLimiter | None = None
_limiter_lock = threading.Lock()


def get_limiter() -> RequestRateLimiter:
    """The process-wide limiter, built from settings on first use."""
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            settings = get_settings()
            _limiter = RequestRateLimiter(
                requests_per_minute=settings.llm_requests_per_minute,
                margin=settings.llm_rate_limit_margin,
            )
            logger.info(
                "LLM request pacer active: %d of %d requests/min",
                _limiter.capacity,
                settings.llm_requests_per_minute,
            )
        return _limiter


def reset_limiter_for_tests() -> None:
    global _limiter
    with _limiter_lock:
        _limiter = None
