"""Per-user, per-endpoint request rate limiter for the LLM endpoints
(auth hardening, 2026-10-14; plan D7 / P7).

Every LLM-backed endpoint (/analyze, /analyze-pasals, /check-compliance,
/analyze-scenario, /chat) costs real tokens, so a single logged-in account
must not be able to hammer them. The limit is a sliding window: at most
LLM_RATE_PER_MIN requests (default 6) in any 60 s window, counted per user
AND per endpoint bucket. Buckets are separate on purpose: DocumentDrawer
fires /analyze and /analyze-pasals back-to-back and then chat, so one shared
bucket would throttle normal use.

State is in-process memory (a deque of timestamps per key). That is correct
here because there is exactly ONE backend container running ONE uvicorn
process: the counters are not shared across processes or containers, and they
reset when the backend restarts. If the backend is ever scaled out, move the
counters to Postgres/Redis first or the effective limit multiplies by the
replica count.

Usage in a router:

    current_user: dict = Depends(auth.get_current_user),
    _rl: dict = Depends(rate_limit.llm_rate_limit("analyze")),

Declare the role dependency BEFORE the limiter so a 403 never consumes
budget (FastAPI resolves dependencies in parameter order).
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Tuple

from fastapi import Depends, HTTPException

DEFAULT_PER_MIN = 6
WINDOW_SECONDS = 60.0
# Idle keys are swept at most this often so memory stays bounded by the number
# of users active in the last window.
_SWEEP_EVERY_SECONDS = 60.0


def get_limit() -> int:
    """LLM_RATE_PER_MIN, read per call so an env change needs only a restart.
    Anything unparseable or < 1 falls back to the default."""
    try:
        value = int(os.getenv("LLM_RATE_PER_MIN", str(DEFAULT_PER_MIN)))
    except ValueError:
        return DEFAULT_PER_MIN
    return value if value >= 1 else DEFAULT_PER_MIN


class SlidingWindowLimiter:
    """Thread-safe sliding-window counter. ``clock`` is injectable for tests."""

    def __init__(self, limit: Callable[[], int] | int = get_limit,
                 window: float = WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self._limit = limit if callable(limit) else (lambda: limit)
        self._window = window
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def check(self, key: str) -> Tuple[bool, int]:
        """Record a request for ``key`` if it fits in the window.

        Returns ``(allowed, retry_after_seconds)``. A denied request is NOT
        recorded (it does not extend the penalty); retry_after is the time
        until the oldest hit leaves the window (>= 1)."""
        now = self._clock()
        limit = self._limit()
        with self._lock:
            self._sweep(now)
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
            cutoff = now - self._window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, math.ceil(hits[0] + self._window - now))
                return False, retry_after
            hits.append(now)
            return True, 0

    def _sweep(self, now: float) -> None:
        # Caller holds the lock. Drop keys whose newest hit is out of window.
        if now - self._last_sweep < _SWEEP_EVERY_SECONDS:
            return
        self._last_sweep = now
        cutoff = now - self._window
        for k in [k for k, d in self._hits.items() if not d or d[-1] <= cutoff]:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._last_sweep = self._clock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)


# Process-wide limiter shared by every LLM endpoint (keys carry the bucket).
limiter = SlidingWindowLimiter()


def llm_rate_limit(bucket: str):
    """Dependency factory: authenticate, then enforce the per-user window for
    ``bucket``. Returns the current user so it can replace a plain
    ``Depends(get_current_user)``. Raises 429 + Retry-After when exceeded."""

    # Imported here, not at module level: auth connects to Postgres at import
    # time, and the limiter itself (SlidingWindowLimiter) must stay importable
    # and unit-testable without a database.
    import auth

    def dependency(current_user: dict = Depends(auth.get_current_user)) -> dict:
        allowed, retry_after = limiter.check(f"{bucket}:{current_user['id']}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many requests, try again later",
                headers={"Retry-After": str(retry_after)},
            )
        return current_user

    return dependency
