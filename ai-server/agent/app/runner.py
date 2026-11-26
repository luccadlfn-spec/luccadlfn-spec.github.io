"""Task queue and the single worker.

One task runs at a time. Interactive tasks always go first; a background task
that is already running hands the worker over to waiting interactive tasks
between its model calls (see `_pause`), so nothing interactive ever waits for
more than one background model call. Between those calls a background task
also rests, and waits for as long as the CPU governor holds background work back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .config import Settings
from .db import INTERACTIVE_KINDS, Database
from .governor import Governor
from .loop import Agent, TurnListener, TurnResult

log = logging.getLogger(__name__)

MAX_QUEUED = 100
MAX_PROMPT_CHARS = 16_000
FINISHED = ("done", "max_steps", "context_full")     # turn outcomes that count as a finished task
IDLE_POLL = 5.0                # how often the worker rechecks the governor while idle
STEP_FLUSH_SECONDS = 1.0

FinishHook = Callable[[dict[str, Any]], Awaitable[None]]


class QueueFull(Exception):
    pass


class _StepRecorder(TurnListener):
    """Writes the step log to the database as it grows, at most about once a second."""

    def __init__(self, db: Database, task_id: int, inner: TurnListener | None = None):
        self._db, self._id, self._inner = db, task_id, inner or TurnListener()
        self._last = 0.0

    def content(self, text: str) -> None:
        self._inner.content(text)

    def thought(self, text: str) -> None:
        self._inner.thought(text)

    def activity(self, text: str) -> None:
        self._inner.activity(text)

    def steps(self, steps: list[dict]) -> None:
        now = time.monotonic()
        if now - self._last >= STEP_FLUSH_SECONDS:
            self._last = now
            self._db.update_steps(self._id, steps)


class TaskRunner:
    def __init__(self, settings: Settings, db: Database, agent: Agent, governor: Governor):
        self.s, self.db, self.agent, self.governor = settings, db, agent, governor
        self._wake = asyncio.Event()
        self._cancels: dict[int, asyncio.Event] = {}
        self._hooks: list[FinishHook] = []
        self.current: int | None = None       # id of the queue task being worked on
        self.held: int | None = None          # background task waiting for the CPU governor
        self.inline = 0                       # turns running inside API requests
        self.detached: set[asyncio.Task] = set()   # streamed turns that outlive their request handler

    def on_finish(self, hook: FinishHook) -> None:
        self._hooks.append(hook)

    def detach(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Start a turn as its own task and keep it referenced until it ends."""
        task = asyncio.ensure_future(coro)
        self.detached.add(task)
        task.add_done_callback(self.detached.discard)
        return task

    async def stop_detached(self) -> None:
        tasks = list(self.detached)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---- queue ----
    def submit(self, kind: str, prompt: str, *, background: bool = False, schedule_id: int | None = None,
               name: str | None = None, conversation_id: int | None = None,
               reply_to: int | None = None) -> int:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("the prompt is empty")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"the prompt is longer than {MAX_PROMPT_CHARS} characters")
        if self.db.count_tasks("queued") >= MAX_QUEUED:
            raise QueueFull(f"{MAX_QUEUED} tasks are already waiting")
        task_id = self.db.create_task(kind, prompt, background=background or kind not in INTERACTIVE_KINDS,
                                      schedule_id=schedule_id, name=name,
                                      conversation_id=conversation_id, reply_to=reply_to)
        self._wake.set()
        return task_id

    def cancel(self, task_id: int) -> str:
        """Returns cancelled | cancelling | finished | not_found."""
        task = self.db.get_task(task_id)
        if task is None:
            return "not_found"
        if self.db.cancel_queued_task(task_id):
            return "cancelled"
        event = self._cancels.get(task_id)
        if event is not None:
            event.set()
            self._wake.set()                  # a task resting in `_pause` stops waiting at once
            return "cancelling"
        return "finished"

    def is_open(self, task_id: int) -> bool:
        task = self.db.get_task(task_id)
        return task is not None and task["status"] in ("queued", "running")

    def queue_status(self) -> dict[str, Any]:
        current = self.db.get_task(self.current) if self.current else None
        return {"queued": self.db.count_tasks("queued"), "running": self.db.count_tasks("running"),
                "current": ({"id": current["id"], "kind": current["kind"], "prompt": current["prompt"][:120]}
                            if current else None),
                "held": self.held}

    # ---- worker ----
    async def run_forever(self) -> None:
        while True:
            try:
                task = self.db.claim_next_task(self.governor.may_run_background())
                if task is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), IDLE_POLL)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._execute(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker error")
                await asyncio.sleep(2.0)      # never spin on a persistent failure

    async def _pause(self, call_seconds: float, *, hold: bool = False, task_id: int | None = None,
                     cancel: asyncio.Event | None = None, timer: asyncio.Timeout | None = None) -> str | None:
        """After a model call of a background task: rest, and serve interactive work meanwhile.

        With `hold` (between two calls of the same task) it then also waits for as long as
        the CPU governor holds background work back. Returns a note for the step log when
        it had to wait for that.
        """
        loop = asyncio.get_running_loop()
        began = loop.time()
        # The task's own time limit is for its work, not for this pause, which may run
        # interactive tasks and wait out CPU throttling for a long time. It is switched off
        # meanwhile, so it can never cancel an interactive task that runs in here.
        deadline = timer.when() if timer is not None else None
        if deadline is not None:
            timer.reschedule(None)
        rest_until = time.monotonic() + self.governor.rest_seconds(call_seconds)
        held_since: float | None = None
        try:
            while True:
                task = self.db.claim_next_task(include_background=False)
                if task is not None:
                    await self._execute(task)
                    continue
                if cancel is not None and cancel.is_set():
                    break
                now = time.monotonic()
                if now < rest_until:
                    wait = min(rest_until - now, IDLE_POLL)
                elif hold and not self.governor.may_run_background():
                    if held_since is None:
                        held_since, self.held = now, task_id
                        log.info("task %s waits for the CPU governor: %s", task_id, self.governor.reason())
                    wait = IDLE_POLL
                else:
                    break
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            if held_since is not None and self.held == task_id:
                self.held = None
            if deadline is not None:
                timer.reschedule(deadline + (loop.time() - began))
        if held_since is None:
            return None
        waited = time.monotonic() - held_since
        took = f"{waited:.0f} s" if waited < 90 else f"{waited / 60:.0f} min"
        return f"waited {took} for the CPU governor to allow background work again"

    async def _execute(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        outer, self.current = self.current, task_id
        background = bool(task["background"])
        history: list[dict] = []
        if task["conversation_id"] and self.db.conversation_exists(task["conversation_id"]):
            history = self.db.get_messages(task["conversation_id"])
        try:
            result = await self._run(task_id, history, task["prompt"], background=background,
                                     scheduled=task["kind"] == "scheduled", schedule_id=task["schedule_id"])
            if result.status in FINISHED and task["conversation_id"] \
                    and self.db.conversation_exists(task["conversation_id"]):
                # Stored exactly as sent, so the next turn's prompt prefix matches the cache.
                self.db.add_message(task["conversation_id"], "user", result.user_content)
                self.db.add_message(task["conversation_id"], "assistant", result.text)
        finally:
            self.current = outer
        if background:
            # The idle gap also applies after the last call, before the next background task.
            last_call = next((st.get("s") or 0.0 for st in reversed(result.steps) if st.get("t") == "llm"), 0.0)
            await self._pause(last_call)

    async def run_inline(self, kind: str, history: list[dict], user_text: str, *, system_extra: str = "",
                         listener: TurnListener | None = None,
                         cancel: asyncio.Event | None = None) -> tuple[int, TurnResult]:
        """Run a turn right now, inside the caller (the chat API), and record it as a task."""
        task_id = self.db.create_task(kind, user_text[:MAX_PROMPT_CHARS], status="running")
        self.inline += 1
        try:
            result = await self._run(task_id, history, user_text, background=False, scheduled=False,
                                     system_extra=system_extra, listener=listener, cancel=cancel,
                                     inline=True)
        finally:
            self.inline -= 1
        return task_id, result

    async def _run(self, task_id: int, history: list[dict], user_text: str, *, background: bool,
                   scheduled: bool, schedule_id: int | None = None, system_extra: str = "",
                   listener: TurnListener | None = None, cancel: asyncio.Event | None = None,
                   inline: bool = False) -> TurnResult:
        cancel = cancel or asyncio.Event()
        self._cancels[task_id] = cancel
        recorder = _StepRecorder(self.db, task_id, listener)
        limit = self.s.turn_timeout * (3 if background else 1)
        result = TurnResult(status="error", error="internal error")
        try:
            async with asyncio.timeout(limit) as timer:
                async def pause(seconds: float) -> str | None:
                    return await self._pause(seconds, hold=True, task_id=task_id, cancel=cancel, timer=timer)

                result = await self.agent.run_turn(
                    history, user_text, background=background, scheduled=scheduled, schedule_id=schedule_id,
                    system_extra=system_extra, listener=recorder, cancel=cancel,
                    pause=pause if background else None)
        except TimeoutError:
            result = TurnResult(status="error", error=f"timed out after {limit / 60:.0f} minutes")
            result.text = f"Sorry, that did not work: the task {result.error}."
        except asyncio.CancelledError:
            # The caller went away (inline): record that. Otherwise the service is stopping;
            # the task stays "running" so that the next start marks it like a task cut off
            # by a crash, and tells its owner (Services.start). Then let the cancellation go on.
            if inline:
                self._finish(task_id, TurnResult(status="cancelled", text="Cancelled."))
            raise
        except Exception as exc:
            log.exception("task %s failed", task_id)
            result = TurnResult(status="error", error=f"internal error ({type(exc).__name__})")
            result.text = "Sorry, that did not work: an internal error occurred."
        finally:
            self._cancels.pop(task_id, None)
        self._finish(task_id, result)
        await self._notify(task_id)
        return result

    def _finish(self, task_id: int, result: TurnResult) -> None:
        status = "done" if result.status in FINISHED else "cancelled" if result.status == "cancelled" else "error"
        try:
            self.db.finish_task(task_id, status, result=result.text, error=result.error, steps=result.steps)
        except Exception:
            log.exception("could not store the result of task %s", task_id)

    async def _notify(self, task_id: int) -> None:
        task = self.db.get_task(task_id)
        if task is None:
            return
        for hook in self._hooks:
            try:
                await hook(task)
            except Exception:
                log.exception("task finish hook failed")
