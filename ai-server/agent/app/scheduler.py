"""Scheduler loop: turns due schedules into queued background tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .config import Settings
from .db import Database
from .runner import QueueFull, TaskRunner
from .schedules import ScheduleService, next_cron_run

log = logging.getLogger(__name__)

TICK_SECONDS = 20.0


class Scheduler:
    def __init__(self, settings: Settings, db: Database, schedules: ScheduleService, runner: TaskRunner,
                 clock: Callable[[], float] = time.time):
        self.s, self.db, self.schedules, self.runner = settings, db, schedules, runner
        self._clock = clock

    def tick(self) -> int:
        """Queue everything that is due. Returns how many tasks were queued."""
        now = self._clock()
        queued = 0
        for sched in self.db.list_schedules():
            if sched["kind"] == "once" and sched["next_run"] is None:
                # A one-shot that has fired is kept until its run is over, so that a run cut
                # off by a restart can be made again (ScheduleService.after_interruption).
                if not self.db.schedule_has_open_task(sched["id"]):
                    self.db.delete_schedule(sched["id"])
                continue
            if not sched["enabled"] or sched["next_run"] is None or sched["next_run"] > now:
                continue
            try:
                queued += self._fire(sched, now)
            except Exception:
                # One broken schedule must not stop the others; try it again next tick.
                log.exception("schedule %s could not be processed", sched["id"])
        return queued

    def _fire(self, sched: dict, now: float) -> int:
        fired = 0
        if self.db.schedule_has_open_task(sched["id"]):
            # The previous run has not finished (slow CPU, paused background work).
            # Piling up copies would only make that worse, so this run is skipped.
            log.info("schedule %s: previous run still open, skipping this one", sched["id"])
        else:
            try:
                self.runner.submit("scheduled", sched["prompt"], background=True,
                                   schedule_id=sched["id"], name=sched["name"])
                fired = 1
            except QueueFull:
                log.warning("schedule %s: task queue is full, will retry", sched["id"])
                return 0
        if sched["kind"] == "cron":
            self.db.update_schedule(sched["id"], last_run=now, next_run=next_cron_run(
                sched["cron"], self.s.tz, now, previous=sched["next_run"]))
        elif fired:
            self.db.update_schedule(sched["id"], last_run=now, next_run=None)    # removed after its run
        else:
            self.db.update_schedule(sched["id"], next_run=now + 300)
        return fired

    async def run(self) -> None:
        self.schedules.resume_after_restart()
        while True:
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)
