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

Cooldowns outlive the process. A service that has told us to wait hours has told
us something about *our application*, not about this run of it, so the deadline
is written to the settings table and reloaded on start. Holding it in memory
alone meant every restart - a deploy, a crash, a machine coming back from a power
cut - walked straight into the same refusal and started a fresh streak, which is
how an app being punished for a burst kept re-announcing itself to the service
punishing it.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .. import db

# Multipliers applied to the configured base cooldown as throttling persists.
# For a service that only says "503, go away" with no indication of for how long,
# guessing long is the safe direction.
DEFAULT_STEPS = (1, 3, 10, 30)

# For a service that answers with an accurate `Retry-After`, the header already
# carries the real answer and our own floor is only there to stop a tight
# probe-fail-probe loop. Escalating it 30x just idles the queue long after the
# quota window has rolled over, so these providers escalate gently instead.
GENTLE_STEPS = (1, 2, 4, 8)


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


class Meter:
    """Rolling count of the requests one provider actually received.

    A rate limit you cannot see is one you find out about by being banned. Every
    number in this file - the intervals, the floors, the caps - was chosen against
    an *estimate* of how much traffic this app generates, and the Spotify ban
    happened because one of those estimates was wrong in a direction nothing
    reported. The breaker records refusals, which is the symptom; this records
    the send rate, which is the cause, and it is the only figure that can be
    compared against a service's published budget before the refusal arrives.

    Counts every request put on the wire, including ones that come back 429:
    a refusal still costs quota, which is exactly why probing a cooldown is not
    free.
    """

    WINDOW = 3600.0

    def __init__(self) -> None:
        self._hits: deque[float] = deque()

    def _trim(self, now: float) -> None:
        cutoff = now - self.WINDOW
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

    def record(self) -> None:
        now = time.monotonic()
        self._hits.append(now)
        self._trim(now)

    def snapshot(self) -> dict[str, int]:
        """Shape merged into each provider's `status()` for the dashboard."""
        now = time.monotonic()
        self._trim(now)
        minute = now - 60.0
        return {
            "requests_1m": sum(1 for t in self._hits if t >= minute),
            "requests_1h": len(self._hits),
        }

    def reset(self) -> None:
        """Drop all state. For tests."""
        self._hits.clear()


class Breaker:
    """Per-provider circuit breaker driven by the service's own backpressure."""

    def __init__(self, name: str, setting_key: str, default_seconds: float = 60.0,
                 steps: tuple[int, ...] = DEFAULT_STEPS,
                 max_seconds: float = 900.0,
                 fallback_note: str = "") -> None:
        self.name = name
        self.setting_key = setting_key
        self.default_seconds = default_seconds
        self.steps = steps
        # Ceiling on a single pause. Set at or above the top escalation step so
        # it never touches our own backoff - it exists only to bound a service
        # that asks for an unreasonable wait. See `open` for why.
        self.max_seconds = max_seconds
        # Appended to the log line, e.g. "Matching continues on Spotify alone."
        self.fallback_note = fallback_note
        # Wall clock, not monotonic: a deadline that has to survive a restart
        # cannot be expressed in a clock that resets to zero on one.
        self._until = 0.0
        self._streak = 0
        self._loaded = False

    # --- persisted state -----------------------------------------------------

    @property
    def _until_key(self) -> str:
        return f"{self.name.lower()}_cooldown_until"

    @property
    def _streak_key(self) -> str:
        return f"{self.name.lower()}_throttle_streak"

    def _load(self) -> None:
        """Restore a cooldown left behind by an earlier run. Runs once.

        Lazy rather than done in `__init__` because the breakers are module-level
        singletons built at import time, before the database exists.

        The restored wait is clamped to `max_seconds` for the same reason `open`
        clamps: this value came off disk and is compared against a wall clock, so
        a container that boots before NTP corrects it could otherwise read a
        deadline days away and take the provider out with no way to notice.
        """
        if self._loaded:
            return
        self._loaded = True
        until = db.get_float(self._until_key, 0.0)
        if until <= 0.0:
            return
        self._until = min(until, time.time() + self.max_seconds)
        self._streak = db.get_int(self._streak_key, 0)

    def _persist(self) -> None:
        db.set_setting(self._until_key, f"{self._until:.0f}")
        db.set_setting(self._streak_key, str(self._streak))

    def remaining(self) -> float:
        """Seconds until the provider may be called again. 0 when available."""
        self._load()
        return max(0.0, self._until - time.time())

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

    def open(self, retry_after_header: str | None = None, status: int | None = None,
             detail: str = "") -> float:
        """Record backpressure and refuse local calls until the cooldown expires.

        Takes the **raw** `Retry-After` header rather than a parsed number, and
        parses it here. A computed delay cannot be reasoned backwards into the
        header that produced it: `62373` and `Sun, 10 Aug 2026 13:30:00 GMT` are
        indistinguishable once both are floats, and they mean very different
        things about the service refusing us. So the header is recorded verbatim
        in the log, alongside the seconds it resolved to.

        Returns the delay applied, for the caller's error message.

        `Retry-After` leads whenever it is longer than our own floor, but it is
        capped: a service can name a wait of many hours, and obeying that
        literally takes the provider out for the rest of the day with no way to
        notice it has come back. Capping means one refused request per cap period
        instead, which costs nothing and lets matching resume on its own the
        moment the refusal actually lifts.
        """
        self._load()
        self._streak += 1
        seconds = parse_retry_after(retry_after_header)
        base = max(1.0, db.get_float(self.setting_key, self.default_seconds))
        step = base * self.steps[min(self._streak, len(self.steps)) - 1]
        asked = max(step, seconds or 0.0)
        delay = min(asked, self.max_seconds)
        self._until = max(self._until, time.time() + delay)
        self._persist()

        parts = [f"{self.name} asked us to back off; pausing calls for {delay:.0f}s "
                 f"(consecutive throttles: {self._streak})."]
        code = f"HTTP {status}" if status else "Refused"
        if retry_after_header is None:
            parts.append(f"{code}, no Retry-After header.")
        else:
            resolved = "unusable" if seconds is None else f"{seconds:.0f}s"
            parts.append(f'{code}, Retry-After: "{retry_after_header[:80]}" '
                         f"read as {resolved}.")
        if detail:
            said = detail[:200].strip()
            parts.append(f"It said: {said}" + ("" if said.endswith((".", "!", "?")) else "."))
        # Say so when we are deliberately not obeying in full, so a capped wait
        # is never mistaken for what the service actually asked for.
        if delay < asked:
            parts.append(f"Capped from {asked:.0f}s so we notice when it recovers.")
        if self.fallback_note:
            parts.append(self.fallback_note)

        db.log_event(" ".join(parts), level="warn", source=self.name.lower())
        return delay

    def close(self) -> None:
        """A successful request means we are inside the budget again."""
        self._load()
        if not self._streak and not self._until:
            return  # already healthy; nothing to clear and nothing worth saying
        if self._streak:
            db.log_event(f"{self.name} is responding normally again.",
                         level="info", source=self.name.lower())
        self._streak = 0
        self._until = 0.0
        self._persist()

    def resume(self) -> float:
        """Clear the cooldown on the user's instruction. Returns seconds skipped.

        The cooldown is a guess about when the service will accept us again, and
        the user may know better - they have fixed the quota, or the ban has been
        lifted, or they simply want to find out. Nothing here overrides the
        service itself: the very next refusal opens a new cooldown.
        """
        skipped = self.remaining()
        self._streak = 0
        self._until = 0.0
        self._persist()
        if skipped > 0:
            db.log_event(
                f"{self.name} cooldown cleared by hand with {skipped:.0f}s left; "
                "calls resume now.", level="info", source=self.name.lower(),
            )
        return skipped

    def reset(self) -> None:
        """Drop all state without logging or touching the database. For tests."""
        self._streak = 0
        self._until = 0.0
        self._loaded = True
