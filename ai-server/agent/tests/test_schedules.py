"""Schedule parsing, limits, and the scheduler loop."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import Database
from app.schedules import ScheduleError, ScheduleService, parse_when
from app.scheduler import Scheduler
from app.tools.base import CallContext
from helpers import FakeLLM, call, say

UTC = timezone.utc
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC).timestamp()      # a Thursday


def when(text: str, tz=UTC, min_interval: int = 15):
    return parse_when(text, tz, NOW, min_interval)


@pytest.mark.parametrize("text,seconds", [("in 30m", 1800), ("in 1 minute", 60), ("IN 2h", 7200),
                                          ("in 3 hours", 10800), ("in 1d", 86400), ("in 2 days", 172800)])
def test_relative_delays(text, seconds):
    parsed = when(text)
    assert parsed.kind == "once" and parsed.cron is None and parsed.next_run == NOW + seconds


def test_absolute_time_uses_the_server_time_zone():
    assert when("2026-11-27 09:30").next_run == datetime(2026, 11, 27, 9, 30, tzinfo=UTC).timestamp()
    assert when("2026-11-27T09:30").kind == "once"
    berlin = when("2026-11-27 09:30", ZoneInfo("Europe/Berlin"))
    assert berlin.next_run == datetime(2026, 11, 27, 8, 30, tzinfo=UTC).timestamp()


@pytest.mark.parametrize("text", ["2026-11-26 11:59", "2020-01-01 00:00", "2026-11-26 12:00"])
def test_one_shot_times_must_be_in_the_future(text):
    with pytest.raises(ScheduleError, match="past"):
        when(text)


@pytest.mark.parametrize("text", ["2026-02-30 10:00", "2026-13-01 10:00", "2026-11-27 25:00", "in 0m",
                                  "tomorrow", "", "in five minutes", "every day", "2099-01-01 00:00"])
def test_nonsense_is_refused(text):
    with pytest.raises(ScheduleError):
        when(text)


@pytest.mark.parametrize("expr,first", [("0 8 * * *", "2026-11-27 08:00"), ("*/15 * * * *", "2026-11-26 12:15"),
                                        ("30 9 * * 1-5", "2026-11-27 09:30"), ("0 */6 * * *", "2026-11-26 18:00"),
                                        ("0 9 1 * *", "2026-12-01 09:00"), ("0 0 * * mon", "2026-11-30 00:00")])
def test_cron_expressions(expr, first):
    parsed = when(expr)
    assert parsed.kind == "cron" and parsed.cron == expr
    assert datetime.fromtimestamp(parsed.next_run, UTC).strftime("%Y-%m-%d %H:%M") == first


def test_cron_runs_in_the_configured_time_zone():
    parsed = when("0 8 * * *", ZoneInfo("America/New_York"))
    assert datetime.fromtimestamp(parsed.next_run, UTC).strftime("%Y-%m-%d %H:%M") == "2026-11-26 13:00"


@pytest.mark.parametrize("expr", ["* * * * *", "*/5 * * * *", "*/14 * * * *", "0,10 * * * *", "0,59 * * * *",
                                  "0-30 9 * * *", "55 23 * * *,0 0 * * *"])
def test_cron_more_often_than_the_minimum_is_refused(expr):
    with pytest.raises(ScheduleError):
        when(expr)


def test_minimum_interval_is_configurable_and_checks_consecutive_fires():
    assert when("*/5 * * * *", min_interval=5).kind == "cron"
    assert when("0,30 * * * *").kind == "cron"                 # 30 minutes apart everywhere
    with pytest.raises(ScheduleError, match="more often"):
        when("0,50 * * * *")                                    # 50 then 10 minutes apart
    with pytest.raises(ScheduleError, match="more often"):
        when("*/30 * * * *", min_interval=60)


@pytest.mark.parametrize("expr", ["0 8 * *", "0 8 * * * *", "0 8 * * * 2026", "61 8 * * *", "0 25 * * *",
                                  "@daily", "@reboot", "0 8 * * * ; rm -rf /", "a b c d e", "R * * * *"])
def test_invalid_cron_is_refused(expr):
    with pytest.raises(ScheduleError):
        when(expr)


# ---- the service: limits and bookkeeping ----

@pytest.fixture
def service(settings, tmp_path):
    db = Database(str(tmp_path / "s.db"))
    clock = {"now": NOW}
    svc = ScheduleService(replace(settings, max_schedules=3), db, clock=lambda: clock["now"])
    svc.clock_box = clock
    yield svc
    db.close()


def test_add_list_pause_resume_delete(service):
    sched = service.add("0 8 * * *", "Summarise the news", "  Morning   news ")
    assert sched["name"] == "Morning news" and sched["kind"] == "cron" and sched["enabled"] is True
    assert "next: 2026-11-27 08:00 UTC" in service.describe(sched)
    paused = service.set_enabled(sched["id"], False)
    assert paused["enabled"] is False and "[paused]" in service.describe(paused)
    service.clock_box["now"] = NOW + 5 * 86400                       # resumed days later: next run is from now
    resumed = service.set_enabled(sched["id"], True)
    assert resumed["next_run"] == datetime(2026, 12, 2, 8, 0, tzinfo=UTC).timestamp()
    assert service.set_enabled(999, True) is None
    assert service.delete(sched["id"]) is True and service.delete(sched["id"]) is False
    assert service.list() == []


def test_schedule_count_and_prompt_limits(service):
    for i in range(3):
        service.add("in 1h", f"task {i}")
    with pytest.raises(ScheduleError, match="limit of 3"):
        service.add("in 1h", "one too many")
    service.delete(service.list()[0]["id"])
    with pytest.raises(ScheduleError, match="empty"):
        service.add("in 1h", "   ")
    with pytest.raises(ScheduleError, match="longer"):
        service.add("in 1h", "x" * 5000)
    assert service.add("in 1h", "fits again")["name"] == "fits again"


def test_restart_resumes_recurring_from_now_and_keeps_overdue_one_shots(service):
    cron = service.add("0 8 * * *", "daily")
    once = service.add("in 1h", "one time")
    service.clock_box["now"] = NOW + 3 * 86400                       # the server was off for three days
    service.resume_after_restart()
    assert service.db.get_schedule(cron["id"])["next_run"] == datetime(2026, 11, 30, 8, 0, tzinfo=UTC).timestamp()
    assert service.db.get_schedule(once["id"])["next_run"] == NOW + 3600      # overdue: will run once


# ---- the scheduler loop ----

async def test_tick_fires_due_schedules_once(make_services):
    sv = await make_services(FakeLLM())
    clock = {"now": NOW}
    sv.schedules._clock = lambda: clock["now"]
    scheduler = Scheduler(sv.settings, sv.db, sv.schedules, sv.runner, clock=lambda: clock["now"])
    cron = sv.schedules.add("0 8 * * *", "daily digest", "Digest")
    once = sv.schedules.add("in 30m", "ping me")
    assert scheduler.tick() == 0                                     # nothing is due yet

    clock["now"] = NOW + 1800
    assert scheduler.tick() == 1 and scheduler.tick() == 0
    fired = sv.db.get_schedule(once["id"])                          # kept while its run is open ...
    assert fired["next_run"] is None and sv.schedules.next_text(fired) == "running now"
    tasks = sv.db.list_tasks()
    assert [(t["kind"], t["status"], t["background"], t["schedule_id"]) for t in tasks] == \
        [("scheduled", "queued", True, once["id"])]
    await sv.runner._execute(sv.db.claim_next_task(True))
    assert scheduler.tick() == 0 and sv.db.get_schedule(once["id"]) is None     # ... and removed after it

    clock["now"] = datetime(2026, 11, 27, 8, 0, 5, tzinfo=UTC).timestamp()
    assert scheduler.tick() == 1
    after = sv.db.get_schedule(cron["id"])
    assert after["next_run"] == datetime(2026, 11, 28, 8, 0, tzinfo=UTC).timestamp() and after["last_run"]
    assert sv.db.list_tasks()[0]["name"] == "Digest"


async def test_tick_skips_a_run_while_the_previous_one_is_still_open(make_services):
    sv = await make_services(FakeLLM())
    clock = {"now": NOW}
    sv.schedules._clock = lambda: clock["now"]
    scheduler = Scheduler(sv.settings, sv.db, sv.schedules, sv.runner, clock=lambda: clock["now"])
    sv.schedules.add("0 * * * *", "hourly")
    clock["now"] = NOW + 3600
    assert scheduler.tick() == 1
    clock["now"] = NOW + 7200
    assert scheduler.tick() == 0                                     # first run is still queued
    assert sv.db.count_tasks("queued") == 1
    assert sv.db.list_schedules()[0]["next_run"] == NOW + 10800      # and the schedule still moves on


async def test_paused_schedules_do_not_fire(make_services):
    sv = await make_services(FakeLLM())
    clock = {"now": NOW}
    sv.schedules._clock = lambda: clock["now"]
    scheduler = Scheduler(sv.settings, sv.db, sv.schedules, sv.runner, clock=lambda: clock["now"])
    sched = sv.schedules.add("in 10m", "later")
    sv.schedules.set_enabled(sched["id"], False)
    clock["now"] = NOW + 3600
    assert scheduler.tick() == 0 and sv.db.count_tasks("queued") == 0


# ---- the tools ----

async def test_schedule_tools(make_services):
    sv = await make_services(FakeLLM())
    ctx = CallContext()
    out, ok = await sv.toolbox.call("schedule_task", {"when": "in 2h", "prompt": "check the oven", "name": "Oven"}, ctx)
    assert ok and out.startswith("Scheduled #1 'Oven'. Next run: ")
    out, ok = await sv.toolbox.call("list_schedules", {}, ctx)
    assert ok and out.startswith("#1 [on] Oven | in 2h | next: ") and out.endswith("| check the oven")
    out, ok = await sv.toolbox.call("schedule_task", {"when": "* * * * *", "prompt": "spam"}, ctx)
    assert not ok and "more often than every 15 minutes" in out
    out, ok = await sv.toolbox.call("schedule_task", {"when": "2001-01-01 00:00", "prompt": "late"}, ctx)
    assert not ok and "past" in out
    out, ok = await sv.toolbox.call("cancel_schedule", {"id": 7}, ctx)
    assert not ok and out == "Error: there is no schedule #7"
    out, ok = await sv.toolbox.call("cancel_schedule", {"id": 1}, ctx)
    assert ok and sv.schedules.list() == []


async def test_scheduled_runs_cannot_create_schedules(make_services):
    llm = FakeLLM(call("schedule_task", {"when": "0 * * * *", "prompt": "replicate"}), say("Could not."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "unattended job", background=True, scheduled=True)
    assert result.status == "done" and sv.schedules.list() == []
    assert "scheduled tasks cannot create new schedules" in llm.requests[1]["messages"][-1]["content"]


async def test_a_one_shot_whose_run_was_cancelled_is_removed(make_services):
    sv = await make_services(FakeLLM())
    clock = {"now": NOW}
    sv.schedules._clock = lambda: clock["now"]
    scheduler = Scheduler(sv.settings, sv.db, sv.schedules, sv.runner, clock=lambda: clock["now"])
    once = sv.schedules.add("in 10m", "later")
    clock["now"] = NOW + 600
    assert scheduler.tick() == 1
    assert sv.runner.cancel(sv.db.list_tasks()[0]["id"]) == "cancelled"
    scheduler.tick()
    assert sv.db.get_schedule(once["id"]) is None and sv.db.count_tasks("queued") == 0


async def test_a_one_shot_cut_off_by_a_restart_runs_once_more_but_not_forever(settings, make_services):
    from app.services import Services
    clock = {"now": NOW}

    def restart(running_task: int | None):
        """The stop of a process whose worker was in the middle of `running_task`."""
        if running_task is not None:
            sv.db._x("UPDATE tasks SET status='running' WHERE id=?", (running_task,))

    sv = await make_services(FakeLLM())
    sv.schedules._clock = lambda: clock["now"]
    scheduler = Scheduler(sv.settings, sv.db, sv.schedules, sv.runner, clock=lambda: clock["now"])
    once = sv.schedules.add("in 10m", "check the backup", "Backup")
    clock["now"] = NOW + 600
    assert scheduler.tick() == 1
    first = sv.db.list_tasks()[0]["id"]
    restart(first)

    again = Services(settings)
    again.schedules._clock = lambda: clock["now"]
    await again.start(background=False)
    try:
        assert [t["id"] for t in again.interrupted] == [first]
        assert again.db.get_task(first)["error"] == "interrupted by restart"
        assert again.interrupted[0]["next"] == "It will be run again now."
        scheduler = Scheduler(again.settings, again.db, again.schedules, again.runner, clock=lambda: clock["now"])
        assert scheduler.tick() == 1                                     # the overdue one-shot runs once more
        second = again.db.list_tasks()[0]["id"]
        assert second != first and again.db.get_task(second)["schedule_id"] == once["id"]
        again.db._x("UPDATE tasks SET status='running' WHERE id=?", (second,))
    finally:
        await again.stop()

    third = Services(settings)
    await third.start(background=False)
    try:
        assert third.interrupted[0]["next"] == "It was interrupted twice, so it will not be run again."
        assert third.db.get_schedule(once["id"]) is None
    finally:
        await third.stop()


async def test_a_scheduled_run_may_cancel_only_its_own_schedule(make_services):
    """A page read unattended must not be able to remove the owner's other jobs."""
    news = None
    llm = FakeLLM(lambda _b: call("cancel_schedule", {"id": news["id"] + 1}, call_id="other"),
                  lambda _b: call("cancel_schedule", {"id": news["id"]}, call_id="own"),
                  say("Stopped checking."))
    sv = await make_services(llm)
    news = sv.schedules.add("0 7 * * *", "Morning news summary", "news")
    invoices = sv.schedules.add("0 9 * * 1", "Weekly invoice reminder", "invoices")
    task_id = sv.runner.submit("scheduled", news["prompt"], background=True, schedule_id=news["id"])
    await sv.runner._execute(sv.db.claim_next_task(True))
    outs = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert outs == ["Error: a scheduled task can only cancel its own schedule", f"Schedule #{news['id']} cancelled."]
    assert [s["id"] for s in sv.schedules.list()] == [invoices["id"]]
    assert sv.db.get_task(task_id)["status"] == "done"
    out, ok = await sv.toolbox.call("cancel_schedule", {"id": invoices["id"]}, CallContext())
    assert ok                                               # the owner, interactively, still can


NEW_YORK = ZoneInfo("America/New_York")


def local(ts: float, tz=NEW_YORK) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M %Z")


@pytest.mark.parametrize("expr,runs", [
    # A fixed hour inside the hour that repeats: once, in the first copy.
    ("30 1 * * *", ["2026-11-01 01:30 EDT", "2026-11-02 01:30 EST", "2026-11-03 01:30 EST"]),
    ("45 1 * * 0", ["2026-11-01 01:45 EDT", "2026-11-08 01:45 EST"]),
    # A wildcard hour keeps its rhythm through both copies, as before.
    ("30 * * * *", ["2026-11-01 00:30 EDT", "2026-11-01 01:30 EDT", "2026-11-01 01:30 EST", "2026-11-01 02:30 EST"]),
    ("*/30 * * * *", ["2026-11-01 00:30 EDT", "2026-11-01 01:00 EDT", "2026-11-01 01:30 EDT",
                      "2026-11-01 01:00 EST", "2026-11-01 01:30 EST"]),
])
def test_cron_runs_through_the_night_the_clocks_go_back(expr, runs):
    from app.schedules import cron_runs
    start = datetime(2026, 11, 1, 0, 10, tzinfo=NEW_YORK).timestamp()
    seq = cron_runs(expr, NEW_YORK, start)
    assert [local(next(seq)) for _ in runs] == runs


async def test_the_scheduler_runs_a_daily_job_once_on_the_night_the_clocks_go_back(make_services):
    sv = await make_services(FakeLLM(), tz_name="America/New_York")
    clock = {"now": datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK).timestamp()}
    sv.schedules._clock = lambda: clock["now"]
    scheduler = Scheduler(sv.settings, sv.db, sv.schedules, sv.runner, clock=lambda: clock["now"])
    sched = sv.schedules.add("30 1 * * *", "nightly summary")
    fired = []
    for _ in range(3 * 24 * 60 // 5):                     # three days in five-minute ticks
        clock["now"] += 300
        if scheduler.tick():
            fired.append(local(clock["now"]))
            await sv.runner._execute(sv.db.claim_next_task(True))
    assert fired == ["2026-11-01 01:30 EDT", "2026-11-02 01:30 EST", "2026-11-03 01:30 EST"]
    assert sv.db.get_schedule(sched["id"])["enabled"] is True


def test_a_daily_job_is_not_refused_because_of_the_repeated_hour():
    before = datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK).timestamp()
    assert parse_when("30 1 * * *", NEW_YORK, before, 120).kind == "cron"    # was: "more often than every 120 minutes"
