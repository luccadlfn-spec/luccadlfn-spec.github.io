"""CPU governor: keeps background work inside a burstable VPS's CPU allowance.

Hostinger lets a VPS use 100% of its vCPUs only for a limited time (about three
hours of banked credit above a 60% baseline). After that the whole machine is
throttled, which shows up inside the VM as CPU *steal*. The provider does not
expose the real balance, so this keeps an estimate from /proc/stat and watches
steal as the ground truth. Only background work is ever held back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 10.0
CREDIT_SECONDS = 10800.0          # ~3 h of full use above the baseline
THROTTLE_WINDOW = 180.0           # mean steal over this window ...
THROTTLE_STEAL = 0.25             # ... at or above this means the VPS is being throttled
CALM_STEAL = 0.10                 # steal must stay below this ...
CALM_SECONDS = 300.0              # ... for this long before background work resumes
MIN_WINDOW = 60.0                 # do not judge from less data than this
LOW_CREDIT = 0.25                 # background work waits below this share of the maximum
MAX_REST = 600.0
KV_KEY = "governor_credit"
PERSIST_EVERY = 6                 # samples (about once a minute)


@dataclass(frozen=True)
class CpuSample:
    busy: int         # jiffies doing work for this VM
    steal: int        # jiffies the hypervisor withheld
    total: int
    ncpu: int


def read_proc_stat(path: str) -> CpuSample | None:
    """Parse the aggregate `cpu` line; None when the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            lines = fh.read(65536).splitlines()
    except OSError:
        return None
    agg, ncpu = None, 0
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            agg = parts[1:]
        elif parts[0].startswith("cpu") and parts[0][3:].isdigit():
            ncpu += 1
    if agg is None or len(agg) < 4:
        return None
    try:
        # user nice system idle iowait irq softirq steal (guest time is already inside user)
        v = [int(x) for x in agg[:8]] + [0] * (8 - len(agg[:8]))
    except ValueError:
        return None
    user, nice, system, idle, iowait, irq, softirq, steal = v
    busy = user + nice + system + irq + softirq
    return CpuSample(busy, steal, busy + idle + iowait + steal, ncpu or os.cpu_count() or 1)


class Governor:
    def __init__(self, enabled: bool, baseline: float, rest_ratio: float, stat_path: str = "/proc/stat",
                 load: Callable[[], Any] | None = None, save: Callable[[float], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.enabled = enabled
        self.baseline = baseline
        self.rest_ratio = rest_ratio
        self._path = stat_path
        self._load, self._save, self._clock = load, save, clock
        self._prev: tuple[float, CpuSample] | None = None
        self._window: deque[tuple[float, float, float]] = deque()   # (time, seconds covered, steal share)
        self._calm_since: float | None = None
        self._since_persist = 0
        self.ncpu = os.cpu_count() or 1
        self.credit: float | None = None      # estimated core-seconds left
        self.throttled = False
        self.busy = 0.0                       # share of all vCPUs busy in the last interval
        self.steal = 0.0                      # share of CPU time stolen in the last interval
        self.steal_mean = 0.0                 # mean steal over the throttle window
        self.available = True                 # False when /proc/stat cannot be read

    @property
    def max_credit(self) -> float:
        return CREDIT_SECONDS * self.ncpu * (1.0 - self.baseline)

    def _initial_credit(self) -> float:
        stored = self._load() if self._load else None
        if isinstance(stored, (int, float)) and stored >= 0:
            return min(float(stored), self.max_credit)
        return 0.5 * self.max_credit           # unknown: assume half

    def sample(self) -> None:
        """Take one reading and update the estimate. Called every SAMPLE_SECONDS."""
        now = self._clock()
        cur = read_proc_stat(self._path)
        if cur is None:
            self.available = False
            return
        self.available = True
        self.ncpu = cur.ncpu
        if self.credit is None:
            self.credit = self._initial_credit()
        prev, self._prev = self._prev, (now, cur)
        if prev is None:
            return
        dt = now - prev[0]
        d_total = cur.total - prev[1].total
        if dt <= 0 or d_total <= 0:
            return
        self.busy = max(0.0, min(1.0, (cur.busy - prev[1].busy) / d_total))
        self.steal = max(0.0, min(1.0, (cur.steal - prev[1].steal) / d_total))
        busy_cores = self.busy * self.ncpu
        self.credit = max(0.0, min(self.max_credit, self.credit + (self.baseline * self.ncpu - busy_cores) * dt))

        self._window.append((now, dt, self.steal))
        while self._window and self._window[0][0] < now - THROTTLE_WINDOW:
            self._window.popleft()
        covered = sum(w[1] for w in self._window)
        self.steal_mean = sum(w[1] * w[2] for w in self._window) / covered if covered else 0.0
        if covered >= MIN_WINDOW:
            if self.steal_mean >= THROTTLE_STEAL:
                if not self.throttled:
                    log.warning("CPU steal is %.0f%%: the VPS is being throttled; pausing background work",
                                self.steal_mean * 100)
                self.throttled, self.credit, self._calm_since = True, 0.0, None
            elif self.throttled:
                if self.steal_mean < CALM_STEAL:
                    if self._calm_since is None:
                        self._calm_since = now
                    if now - self._calm_since >= CALM_SECONDS:
                        self.throttled, self._calm_since = False, None
                        log.info("CPU steal has stayed low; background work may resume")
                else:
                    self._calm_since = None
        self._since_persist += 1
        if self._since_persist >= PERSIST_EVERY:
            self.persist()

    def persist(self) -> None:
        self._since_persist = 0
        if self._save and self.credit is not None:
            try:
                self._save(round(self.credit, 1))
            except Exception:
                log.exception("could not store the CPU credit estimate")

    def may_run_background(self) -> bool:
        if not self.enabled or not self.available:
            return True
        if self.throttled:
            return False
        return self.credit is None or self.credit >= LOW_CREDIT * self.max_credit

    def rest_seconds(self, call_seconds: float) -> float:
        """Idle time owed after a background model call of the given length."""
        if not self.enabled:
            return 0.0
        return max(0.0, min(MAX_REST, self.rest_ratio * call_seconds))

    def reason(self) -> str:
        if not self.enabled:
            return "governor is off"
        if not self.available:
            return "CPU statistics are not readable; nothing is held back"
        if self.throttled:
            return "the VPS is being throttled (high CPU steal); background work is paused"
        if not self.may_run_background():
            return "estimated CPU credits are low; background work is paused"
        return "ok"

    def status(self) -> dict[str, Any]:
        credit_pct = None
        if self.credit is not None and self.max_credit > 0:
            credit_pct = round(100.0 * self.credit / self.max_credit, 1)
        return {"enabled": self.enabled, "throttled": self.throttled,
                "steal_pct": round(self.steal_mean * 100, 1), "busy_pct": round(self.busy * 100, 1),
                "credits_pct": credit_pct, "background_allowed": self.may_run_background(),
                "reason": self.reason()}

    async def run(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                self.sample()
            except Exception:
                log.exception("CPU governor sample failed")
            await asyncio.sleep(SAMPLE_SECONDS)
