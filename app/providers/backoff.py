"""Shared backpressure handling for the outbound provider clients.

Both MusicBrainz and Spotify shipped with the same shape of bug: a refusal was
retried inline several times, the remaining query spellings were then tried
anyway, and `Retry-After` was clamped to a value far shorter than the one the
service asked for. The result is that a rate limit - which is a request to stop
- was answered by continuing at full rate, and a throttle that should cost a
minute instead cost several minutes *per song* and stalled the match queue.

A `Breaker` turns a refusal into a local, free "no": once open, callers are
refused without a request being sent at all, so the matcher degrades to whatever
provider is still healthy instead of blocking on the one that is not.

The streak only resets on a success, so sustained throttling escalates the pause
rather than settling into a loop that probes every minute forever.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .. import db

# Multipliers applied to the configured base cooldown as throttling persists.
DEFAULT_STEPS = (1, 3, 10, 30)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait, from either Retry-After form. None when absent/unusable.

    The header is delta-seconds in practice, but the spec also allows an
    HTTP-date. Testing with `.isdigit()` silently discards both a date and a
    fractional value, which is how a request to wait a minute became a 2s guess.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class Breaker:
    """Per-provider circuit breaker driven by the service's own backpressure."""

    def __init__(self, name: str, setting_key: str, default_seconds: float = 60.0,
                 steps: tuple[int, ...] = DEFAULT_STEPS,
                 fallback_note: str = "") -> None:
        self.name = name
        self.setting_key = setting_key
        self.default_seconds = default_seconds
        self.steps = steps
        # Appended to the log line, e.g. "Matching continues on Spotify alone."
        self.fallback_note = fallback_note
        self._until = 0.0
        self._streak = 0

    def remaining(self) -> float:
        """Seconds until the provider may be called again. 0 when available."""
        return max(0.0, self._until - time.monotonic())

    def is_open(self) -> bool:
        return self.remaining() > 0

    def status(self) -> dict[str, Any]:
        """Shape consumed by /api/stats, so a cold provider is visible in the UI."""
        remaining = self.remaining()
        return {
            "throttled": remaining > 0,
            "cooldown_seconds": round(remaining, 1),
            "throttle_streak": self._streak,
        }

    def open(self, retry_after: float | None = None) -> float:
        """Record backpressure and refuse local calls until the cooldown expires.

        Returns the delay applied, for the caller's error message.
        """
        self._streak += 1
        base = max(5.0, db.get_float(self.setting_key, self.default_seconds))
        step = base * self.steps[min(self._streak, len(self.steps)) - 1]
        # Honour Retry-After in full when the service names a delay, but never
        # wait less than our own escalating floor.
        delay = max(step, retry_after or 0.0)
        self._until = max(self._until, time.monotonic() + delay)
        note = f" {self.fallback_note}" if self.fallback_note else ""
        db.log_event(
            f"{self.name} asked us to back off; pausing calls for {delay:.0f}s "
            f"(consecutive throttles: {self._streak}).{note}",
            level="warn", source=self.name.lower(),
        )
        return delay

    def close(self) -> None:
        """A successful request means we are inside the budget again."""
        if self._streak:
            db.log_event(f"{self.name} is responding normally again.",
                         level="info", source=self.name.lower())
        self._streak = 0
        self._until = 0.0

    def reset(self) -> None:
        """Drop all state without logging. For tests."""
        self._streak = 0
        self._until = 0.0
