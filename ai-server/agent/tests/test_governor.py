"""CPU governor: credit arithmetic and throttle detection from synthetic /proc/stat samples."""

from __future__ import annotations

import pytest

from app.governor import CREDIT_SECONDS, Governor, read_proc_stat

HZ = 100


class Machine:
    """A fake VM: writes /proc/stat for a given busy and steal share, and owns the clock."""

    def __init__(self, path, ncpu: int = 8):
        self.path, self.ncpu, self.now = path, ncpu, 1000.0
        self.busy = self.steal = self.idle = 0
        self.write()

    def write(self) -> None:
        lines = [f"cpu  {self.busy} 0 0 {self.idle} 0 0 0 {self.steal} 0 0"]
        lines += [f"cpu{i} 1 0 0 1 0 0 0 0 0 0" for i in range(self.ncpu)]
        lines += ["intr 12345", "ctxt 999", "btime 1700000000"]
        self.path.write_text("\n".join(lines) + "\n")

    def advance(self, seconds: float, busy: float, steal: float = 0.0) -> None:
        total = int(seconds * HZ * self.ncpu)
        self.busy += int(total * busy)
        self.steal += int(total * steal)
        self.idle += total - int(total * busy) - int(total * steal)
        self.now += seconds
        self.write()


@pytest.fixture
def machine(tmp_path):
    return Machine(tmp_path / "stat")


def governor(machine, **kw) -> Governor:
    opts = dict(enabled=True, baseline=0.60, rest_ratio=0.5, stat_path=str(machine.path), clock=lambda: machine.now)
    opts.update(kw)
    return Governor(**opts)


def run(gov: Governor, machine: Machine, seconds: float, busy: float, steal: float = 0.0) -> None:
    for _ in range(int(seconds / 10)):
        machine.advance(10, busy, steal)
        gov.sample()


def test_proc_stat_parsing(tmp_path):
    p = tmp_path / "stat"
    p.write_text("cpu  100 20 30 400 50 6 4 90 7 0\ncpu0 1 2 3 4 5 6 7 8 9 10\ncpu1 1 2 3 4 5 6 7 8 9 10\nintr 1\n")
    s = read_proc_stat(str(p))
    assert (s.busy, s.steal, s.total, s.ncpu) == (160, 90, 700, 2)
    p.write_text("cpu  5 0 5 90\n")                               # very old kernels: four fields
    assert read_proc_stat(str(p)).total == 100
    p.write_text("garbage\n")
    assert read_proc_stat(str(p)) is None
    assert read_proc_stat(str(tmp_path / "missing")) is None


def test_credit_starts_at_half_and_follows_the_formula(machine):
    gov = governor(machine)
    gov.sample()
    max_credit = CREDIT_SECONDS * 8 * 0.40
    assert gov.max_credit == pytest.approx(max_credit) and gov.credit == pytest.approx(0.5 * max_credit)

    machine.advance(10, busy=1.0)                                 # all 8 cores busy for 10 s
    gov.sample()
    assert gov.credit == pytest.approx(0.5 * max_credit + (0.6 * 8 - 8.0) * 10)     # -32 core-seconds
    assert gov.busy == pytest.approx(1.0)

    before = gov.credit
    machine.advance(10, busy=0.0)                                 # idle: earn baseline * ncpu * dt
    gov.sample()
    assert gov.credit == pytest.approx(before + 0.6 * 8 * 10)

    before = gov.credit
    machine.advance(10, busy=0.60)                                # exactly at the baseline: no change
    gov.sample()
    assert gov.credit == pytest.approx(before, abs=0.5)


def test_credit_is_clamped_between_zero_and_max(machine):
    gov = governor(machine)
    gov.sample()
    run(gov, machine, 6 * 3600, busy=1.0)                         # far more than the 3 h of credit
    assert gov.credit == 0.0 and not gov.may_run_background()
    run(gov, machine, 12 * 3600, busy=0.0)
    assert gov.credit == pytest.approx(gov.max_credit) and gov.may_run_background()


def test_full_load_drains_half_the_credit_in_about_ninety_minutes(machine):
    gov = governor(machine)
    gov.sample()
    run(gov, machine, 45 * 60, busy=1.0)
    assert gov.status()["credits_pct"] == pytest.approx(25.0, abs=0.5)      # 50% -> 25% after 45 min
    assert gov.may_run_background()
    run(gov, machine, 60, busy=1.0)
    assert not gov.may_run_background() and "credits are low" in gov.reason()


def test_credit_is_persisted_and_restored(machine):
    box: dict[str, float] = {}
    gov = governor(machine, load=lambda: box.get("v"), save=lambda v: box.__setitem__("v", v))
    gov.sample()
    run(gov, machine, 120, busy=1.0)
    assert box["v"] == pytest.approx(gov.credit, abs=0.1)
    again = governor(machine, load=lambda: box.get("v"))
    again.sample()
    assert again.credit == pytest.approx(box["v"])
    silly = governor(machine, load=lambda: 10 ** 12)
    silly.sample()
    assert silly.credit == silly.max_credit


def test_sustained_steal_means_throttled(machine):
    gov = governor(machine)
    gov.sample()
    run(gov, machine, 170, busy=0.5, steal=0.30)
    assert gov.throttled and gov.credit == 0.0 and not gov.may_run_background()
    st = gov.status()
    assert st["throttled"] is True and st["steal_pct"] == pytest.approx(30.0, abs=1) and "throttled" in st["reason"]


def test_a_short_steal_spike_is_not_throttling(machine):
    gov = governor(machine)
    gov.sample()
    run(gov, machine, 150, busy=0.3, steal=0.02)
    run(gov, machine, 20, busy=0.3, steal=0.90)                   # 20 s spike: 3-minute mean stays low
    assert not gov.throttled
    run(gov, machine, 30, busy=0.3, steal=0.40)
    assert not gov.throttled                                       # mean is still below 25%
    assert gov.may_run_background()


def test_no_verdict_from_too_little_data(machine):
    gov = governor(machine)
    gov.sample()
    run(gov, machine, 30, busy=0.2, steal=0.80)
    assert not gov.throttled
    run(gov, machine, 40, busy=0.2, steal=0.80)
    assert gov.throttled


def test_throttle_clears_only_after_five_calm_minutes(machine):
    gov = governor(machine)
    gov.sample()
    run(gov, machine, 180, busy=0.5, steal=0.40)
    assert gov.throttled
    run(gov, machine, 240, busy=0.2, steal=0.02)                  # mean needs ~2.5 min to fall, then the wait starts
    assert gov.throttled
    run(gov, machine, 120, busy=0.2, steal=0.15)                  # not calm: the wait starts over
    assert gov.throttled
    # The 3-minute mean drops under 10% about 70 s into this calm stretch; five minutes later it clears.
    run(gov, machine, 330, busy=0.2, steal=0.01)
    assert gov.throttled                                           # calm, but not yet for five minutes
    run(gov, machine, 60, busy=0.2, steal=0.01)
    assert not gov.throttled
    # Credits were zeroed by the throttle, so background work still waits for them to recover.
    assert not gov.may_run_background()
    run(gov, machine, 3 * 3600, busy=0.0)
    assert gov.may_run_background()


def test_rest_between_background_calls(machine):
    gov = governor(machine)
    assert gov.rest_seconds(40.0) == 20.0
    assert gov.rest_seconds(100_000.0) == 600.0                    # bounded
    assert governor(machine, rest_ratio=0.0).rest_seconds(40.0) == 0.0


def test_disabled_governor_never_delays_anything(machine):
    gov = governor(machine, enabled=False)
    gov.sample()
    run(gov, machine, 600, busy=1.0, steal=0.9)
    assert gov.may_run_background() and gov.rest_seconds(100.0) == 0.0
    assert gov.status()["enabled"] is False and gov.reason() == "governor is off"


def test_unreadable_statistics_fail_open(tmp_path):
    gov = Governor(True, 0.6, 0.5, stat_path=str(tmp_path / "nope"), clock=lambda: 0.0)
    gov.sample()
    assert gov.available is False and gov.may_run_background()
    assert "not readable" in gov.status()["reason"]
