"""Quiet hours and human-like send timing."""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


class ActiveHours:
    """A daily window in local time, e.g. 09:00-23:00. The window may cross midnight (22:00-02:00)."""

    def __init__(self, start: time, end: time, tz: ZoneInfo) -> None:
        self.start, self.end, self.tz = start, end, tz

    def is_active(self, moment: datetime) -> bool:
        t = moment.astimezone(self.tz).time()
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_start(self, moment: datetime) -> datetime:
        """The first moment >= `moment` that is inside the window."""
        if self.is_active(moment):
            return moment
        local = moment.astimezone(self.tz)
        candidate = datetime.combine(local.date(), self.start, tzinfo=self.tz)
        if candidate <= local:
            candidate = datetime.combine(local.date() + timedelta(days=1), self.start, tzinfo=self.tz)
        return candidate.astimezone(moment.tzinfo)


def compute_send_time(
    now: datetime,
    delay_range: tuple[int, int],
    hours: ActiveHours,
    rng: random.Random | None = None,
) -> datetime:
    """When to send an approved message: a random delay, pushed into active hours if needed.

    Outside active hours the message is queued until the window opens, plus a small random offset
    so that morning replies don't all go out at exactly 09:00.
    """
    rng = rng or random.Random()
    lo, hi = delay_range
    at = now + timedelta(seconds=rng.randint(lo, hi))
    if hours.is_active(at):
        return at
    return hours.next_start(at) + timedelta(seconds=rng.randint(60, 20 * 60))


def typing_seconds(text: str, chars_per_second: float = 7.0, low: float = 2.0, high: float = 25.0) -> float:
    """How long to show "typing…" for a message of this length."""
    return max(low, min(high, len(text) / chars_per_second))
