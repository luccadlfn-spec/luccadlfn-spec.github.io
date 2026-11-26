"""Schedule parsing, limits and bookkeeping (shared by the tools, the API and Telegram)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Callable

from croniter import croniter

from .config import Settings
from .db import Database

MAX_PROMPT_CHARS = 4000
MAX_NAME_CHARS = 80
MAX_AHEAD_DAYS = 3660
_CRON_CHECK_FIRES = 100          # more than a full day of fires at the smallest allowed spacing

_IN = re.compile(r"^in\s+(\d{1,5})\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.I)
_AT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::\d{2})?$")
_NUMERIC_FIELD = re.compile(r"^[\d*/,\-]+$")
_NAMED_FIELD = re.compile(r"^[\dA-Za-z*/,\-#]+$")


class ScheduleError(Exception):
    """The request cannot be scheduled. The message is safe to show to the model or the user."""


@dataclass(frozen=True)
class When:
    kind: str                 # "cron" | "once"
    cron: str | None
    next_run: float           # epoch seconds


def next_cron_run(expr: str, tz: tzinfo, after: float) -> float:
    start = datetime.fromtimestamp(after, tz)
    return croniter(expr, start).get_next(datetime).timestamp()


def _check_cron(expr: str, tz: tzinfo, now: float, min_interval_min: int) -> None:
    fields = expr.split()
    if len(fields) != 5:
        raise ScheduleError("a cron expression needs exactly 5 fields: minute hour day month weekday")
    patterns = (_NUMERIC_FIELD, _NUMERIC_FIELD, _NUMERIC_FIELD, _NAMED_FIELD, _NAMED_FIELD)
    if not all(p.match(f) for p, f in zip(patterns, fields)) or not croniter.is_valid(expr):
        raise ScheduleError(f"'{expr}' is not a valid cron expression")
    it = croniter(expr, datetime.fromtimestamp(now, tz))
    try:
        prev = it.get_next(datetime).timestamp()
        for _ in range(_CRON_CHECK_FIRES):
            cur = it.get_next(datetime).timestamp()
            if cur - prev < min_interval_min * 60 - 1:
                raise ScheduleError(
                    f"this schedule would run more often than every {min_interval_min} minutes; "
                    "use a wider interval")
            prev = cur
    except ScheduleError:
        raise
    except Exception:
        raise ScheduleError(f"'{expr}' never fires") from None


def parse_when(when: str, tz: tzinfo, now: float, min_interval_min: int) -> When:
    """Understand `in <n>m|h|d`, `YYYY-MM-DD HH:MM` (in the server time zone) or 5-field cron."""
    text = " ".join(str(when).split())
    if not text or len(text) > 100:
        raise ScheduleError("'when' must be a cron expression, 'in 30m', or 'YYYY-MM-DD HH:MM'")
    m = _IN.match(text)
    if m:
        amount, unit = int(m.group(1)), m.group(2)[0].lower()
        seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
        if seconds <= 0 or seconds > MAX_AHEAD_DAYS * 86400:
            raise ScheduleError("the delay must be between 1 minute and 10 years")
        return When("once", None, now + seconds)
    m = _AT.match(text)
    if m:
        try:
            at = datetime(*(int(g) for g in m.groups()), tzinfo=tz)
        except ValueError:
            raise ScheduleError(f"'{text}' is not a real date and time") from None
        ts = at.timestamp()
        if ts <= now:
            raise ScheduleError("that time is in the past; one-time schedules must be in the future")
        if ts > now + MAX_AHEAD_DAYS * 86400:
            raise ScheduleError("that time is too far in the future")
        return When("once", None, ts)
    _check_cron(text, tz, now, min_interval_min)
    return When("cron", text, next_cron_run(text, tz, now))


class ScheduleService:
    def __init__(self, settings: Settings, db: Database, clock: Callable[[], float] = time.time):
        self.s = settings
        self.db = db
        self._clock = clock

    def fmt(self, ts: float | None) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts, self.s.tz).strftime("%Y-%m-%d %H:%M %Z").strip()

    def add(self, when: str, prompt: str, name: str | None = None) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ScheduleError("the prompt is empty")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ScheduleError(f"the prompt is longer than {MAX_PROMPT_CHARS} characters")
        if self.db.count_schedules() >= self.s.max_schedules:
            raise ScheduleError(f"the limit of {self.s.max_schedules} schedules is reached; cancel one first")
        parsed = parse_when(when, self.s.tz, self._clock(), self.s.min_schedule_interval_min)
        label = " ".join((name or "").split())[:MAX_NAME_CHARS] or prompt[:40]
        spec = " ".join(str(when).split())
        sid = self.db.add_schedule(label, spec, parsed.kind, parsed.cron, prompt, parsed.next_run)
        return self.db.get_schedule(sid)

    def list(self) -> list[dict[str, Any]]:
        return self.db.list_schedules()

    def delete(self, schedule_id: int) -> bool:
        return self.db.delete_schedule(schedule_id)

    def set_enabled(self, schedule_id: int, enabled: bool) -> dict[str, Any] | None:
        sched = self.db.get_schedule(schedule_id)
        if sched is None:
            return None
        fields: dict[str, Any] = {"enabled": int(enabled)}
        if enabled and sched["kind"] == "cron":
            # Resume from now; runs missed while paused are not made up.
            fields["next_run"] = next_cron_run(sched["cron"], self.s.tz, self._clock())
        self.db.update_schedule(schedule_id, **fields)
        return self.db.get_schedule(schedule_id)

    def resume_after_restart(self) -> None:
        """Recurring schedules continue from now; overdue one-shots keep their time and run once."""
        now = self._clock()
        for sched in self.db.list_schedules():
            if sched["kind"] == "cron":
                try:
                    self.db.update_schedule(sched["id"], next_run=next_cron_run(sched["cron"], self.s.tz, now))
                except Exception:
                    self.db.update_schedule(sched["id"], enabled=0)

    def describe(self, sched: dict[str, Any]) -> str:
        state = "on" if sched["enabled"] else "paused"
        return (f"#{sched['id']} [{state}] {sched['name']} | {sched['spec']} | "
                f"next: {self.fmt(sched['next_run']) if sched['enabled'] else '-'}")
