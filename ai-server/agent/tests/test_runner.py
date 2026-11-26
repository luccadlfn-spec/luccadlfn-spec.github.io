"""Task queue, priorities, restart recovery and cancellation."""

from __future__ import annotations

import asyncio

import pytest

from app.db import Database
from app.runner import MAX_QUEUED, QueueFull
from helpers import FakeLLM, call, say


async def drain(sv, limit: int = 20) -> None:
    """Run queued tasks the way the worker does, until none can be claimed."""
    for _ in range(limit):
        task = sv.db.claim_next_task(sv.governor.may_run_background())
        if task is None:
            return
        await sv.runner._execute(task)


async def test_task_lifecycle_and_step_log(make_services):
    llm = FakeLLM(call("remember", {"text": "the door code is 4411"}), say("Noted."))
    sv = await make_services(llm)
    task_id = sv.runner.submit("api", "Remember the door code 4411")
    assert sv.db.get_task(task_id)["status"] == "queued"
    await drain(sv)
    task = sv.db.get_task(task_id)
    assert (task["status"], task["result"], task["error"]) == ("done", "Noted.", None)
    assert task["started_at"] and task["finished_at"] >= task["started_at"]
    assert [s["t"] for s in task["steps"]] == ["llm", "tool", "llm"]
    assert task["steps"][1]["name"] == "remember" and task["steps"][1]["ok"] is True
    assert sv.db.list_notes()[0]["text"] == "the door code is 4411"


async def test_interactive_tasks_run_before_background_ones(make_services):
    sv = await make_services(FakeLLM(*[say(f"answer {i}") for i in range(4)]))
    first_bg = sv.runner.submit("scheduled", "nightly report")
    second_bg = sv.runner.submit("api", "low priority", background=True)
    chat = sv.runner.submit("chat", "quick question")
    tg = sv.runner.submit("telegram", "hello")
    order = []
    sv.runner.on_finish(lambda task: _record(order, task))
    await drain(sv)
    assert order == [chat, tg, first_bg, second_bg]
    assert sv.db.get_task(first_bg)["background"] is True and sv.db.get_task(chat)["background"] is False


async def _record(order, task):
    order.append(task["id"])


async def test_background_tasks_wait_for_the_governor_but_interactive_ones_do_not(make_services):
    sv = await make_services(FakeLLM(say("interactive done"), say("background done")))
    sv.governor.enabled = True
    sv.governor.throttled = True                       # the VPS is being throttled
    bg = sv.runner.submit("scheduled", "heavy job")
    chat = sv.runner.submit("chat", "are you there?")
    await drain(sv)
    assert sv.db.get_task(chat)["status"] == "done"
    assert sv.db.get_task(bg)["status"] == "queued"    # held back, not dropped
    sv.governor.throttled = False
    sv.governor.credit = None
    await drain(sv)
    assert sv.db.get_task(bg)["status"] == "done"


async def test_background_thinking_and_scheduled_context(make_services):
    llm = FakeLLM(say("bg"), say("fg"))
    sv = await make_services(llm)
    sv.runner.submit("scheduled", "job")
    await drain(sv)
    sv.runner.submit("chat", "question")
    await drain(sv)
    assert [r["chat_template_kwargs"]["enable_thinking"] for r in llm.requests] == [True, False]


async def test_running_background_task_lets_interactive_work_in_between_calls(make_services):
    """A long background job must not make an interactive task wait for all of its steps."""
    events: list[str] = []
    holder: dict = {}

    def bg_step_one(_body):
        # While the background job's first model call is in flight, a chat task arrives.
        holder["chat"] = holder["sv"].runner.submit("chat", "urgent question")
        events.append("bg call 1")
        return call("list_files", {})

    def chat_answer(body):
        events.append("chat call: " + body["messages"][-1]["content"].split("\n")[0])
        return say("urgent answer")

    def bg_step_two(_body):
        events.append("bg call 2")
        return say("background finished")

    llm = FakeLLM(bg_step_one, chat_answer, bg_step_two)
    sv = holder["sv"] = await make_services(llm)
    bg = sv.runner.submit("scheduled", "long job")
    await drain(sv)
    assert events == ["bg call 1", "chat call: urgent question", "bg call 2"]
    chat_task, bg_task = sv.db.get_task(holder["chat"]), sv.db.get_task(bg)
    assert chat_task["status"] == "done" and bg_task["status"] == "done"
    assert chat_task["finished_at"] <= bg_task["finished_at"]
    assert sv.runner.current is None


async def test_rest_between_background_calls_follows_the_governor(make_services, monkeypatch):
    sv = await make_services(FakeLLM(call("list_files", {}), say("done")))
    sv.governor.enabled = True
    asked: list[float] = []
    monkeypatch.setattr(sv.governor, "rest_seconds", lambda seconds: asked.append(seconds) or 0.0)
    sv.runner.submit("scheduled", "job")
    await drain(sv)
    assert len(asked) == 2 and min(asked) >= 0.0       # after each of the two model calls


async def test_tasks_interrupted_by_a_restart_are_marked(settings, make_services):
    db = Database(settings.db_path)
    running = db.create_task("chat", "was in progress", status="running")
    queued = db.create_task("scheduled", "still waiting", background=True)
    db.close()
    sv = await make_services(FakeLLM(say("ran after restart")))      # start() performs the recovery
    task = sv.db.get_task(running)
    assert task["status"] == "error" and task["error"] == "interrupted by restart" and task["finished_at"]
    assert sv.db.get_task(queued)["status"] == "queued"
    await drain(sv)
    assert sv.db.get_task(queued)["result"] == "ran after restart"


async def test_cancel_queued_running_finished_and_unknown(make_services):
    started = asyncio.Event()

    async def slow(_body):
        started.set()
        await asyncio.sleep(30)
        return say("never")

    sv = await make_services(FakeLLM(slow, say("second")))
    running = sv.runner.submit("chat", "slow one")
    queued = sv.runner.submit("chat", "waiting one")
    worker = asyncio.create_task(drain(sv))
    await started.wait()
    assert sv.runner.cancel(queued) == "cancelled" and sv.db.get_task(queued)["status"] == "cancelled"
    assert sv.runner.cancel(running) == "cancelling"
    await asyncio.wait_for(worker, 5)
    task = sv.db.get_task(running)
    assert task["status"] == "cancelled" and task["result"] == "Cancelled."
    assert sv.runner.cancel(running) == "finished"
    assert sv.runner.cancel(12345) == "not_found"
    assert sv.runner.is_open(running) is False


async def test_failed_turn_is_recorded_as_error(make_services):
    import httpx
    sv = await make_services(FakeLLM(httpx.Response(500, json={"error": {"message": "boom"}})))
    task_id = sv.runner.submit("chat", "x")
    await drain(sv)
    task = sv.db.get_task(task_id)
    assert task["status"] == "error" and "boom" in task["error"] and task["result"].startswith("Sorry")


async def test_queue_and_prompt_limits(make_services):
    sv = await make_services(FakeLLM())
    with pytest.raises(ValueError):
        sv.runner.submit("chat", "   ")
    with pytest.raises(ValueError):
        sv.runner.submit("chat", "x" * 20_000)
    for i in range(MAX_QUEUED):
        sv.runner.submit("chat", f"task {i}")
    with pytest.raises(QueueFull):
        sv.runner.submit("chat", "one too many")


async def test_conversation_history_is_kept_for_chat_channels(make_services):
    llm = FakeLLM(say("Nice to meet you, Ada."), say("Your name is Ada."))
    sv = await make_services(llm)
    conv = sv.db.create_conversation("telegram")
    sv.runner.submit("telegram", "My name is Ada.", conversation_id=conv)
    await drain(sv)
    sv.runner.submit("telegram", "What is my name?", conversation_id=conv)
    await drain(sv)
    second = llm.requests[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    # Earlier turns are replayed byte-for-byte (time line included), so the prompt cache still matches.
    assert second[1] == llm.requests[0]["messages"][1]
    assert second[2] == {"role": "assistant", "content": "Nice to meet you, Ada."}
    assert len(sv.db.get_messages(conv)) == 4


async def test_worker_loop_picks_up_tasks(make_services):
    sv = await make_services(FakeLLM(say("from the worker")))
    worker = asyncio.create_task(sv.runner.run_forever())
    task_id = sv.runner.submit("chat", "hello")
    for _ in range(200):
        if not sv.runner.is_open(task_id):
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    assert sv.db.get_task(task_id)["result"] == "from the worker"
