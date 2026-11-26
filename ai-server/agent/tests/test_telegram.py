"""Telegram bot against a fake Bot API: allowlist, pairing, commands, delivery, token hygiene."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest

from app.telegram import MESSAGE_LIMIT, TelegramError, split_message
from helpers import FakeLLM, say

TOKEN = "1234:" + "not-a-real-token"        # assembled so it never looks like a credential to scanners
OWNER, STRANGER = 4242, 6666


class FakeTelegram:
    def __init__(self):
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, httpx.Response | Exception] = {}
        self._next_id = 100

    def push(self, text, user=OWNER, chat_type="private", chat_id=None, **extra):
        self._next_id += 1
        message = {"message_id": self._next_id, "from": {"id": user, "is_bot": False},
                   "chat": {"id": chat_id if chat_id is not None else user, "type": chat_type}, **extra}
        if text is not None:
            message["text"] = text
        self.updates.append({"update_id": self._next_id, "message": message})
        return self._next_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        method = request.url.path.rsplit("/", 1)[1]
        payload = json.loads(request.content)
        self.calls.append((method, payload))
        if method in self.fail:
            outcome = self.fail[method]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if method == "getUpdates":
            fresh = [u for u in self.updates if u["update_id"] >= payload.get("offset", 0)]
            return httpx.Response(200, json={"ok": True, "result": fresh})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, chat=None) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage" and (chat is None or p["chat_id"] == chat)]


@pytest.fixture
async def bot_env(make_services):
    async def make(llm=None, **overrides):
        tg = FakeTelegram()
        opts = dict(telegram_bot_token=TOKEN, telegram_api_base="https://telegram.test",
                    telegram_allowed_ids=frozenset({OWNER}), pairing_code="31415926")
        opts.update(overrides)
        sv = await make_services(llm or FakeLLM(), services_kwargs={
            "telegram_transport": httpx.MockTransport(tg.handler)}, **opts)

        async def brief(_s):
            await asyncio.sleep(0.005)       # must yield, or the typing loop would never let go
        sv.telegram._sleep = brief
        return sv, tg
    return make


async def run_queue(sv):
    while (task := sv.db.claim_next_task(True)) is not None:
        await sv.runner._execute(task)


def test_split_message():
    assert split_message("short") == ["short"]
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= MESSAGE_LIMIT for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")       # nothing lost, cut at line ends
    assert all(len(p) <= MESSAGE_LIMIT for p in split_message("y" * 9000))  # no line breaks at all
    huge = split_message("z" * 200_000)
    assert len(huge) <= 10 and huge[-1].endswith("[reply cut short]")
    assert split_message("   ") == ["(empty reply)"]


async def test_owner_chat_turn_end_to_end(bot_env):
    sv, tg = await bot_env(FakeLLM(say("Paris.")))
    tg.push("What is the capital of France?")
    assert await sv.telegram.poll_once() == 1
    task = sv.db.list_tasks()[0]
    assert (task["kind"], task["status"], task["reply_to"]) == ("telegram", "queued", OWNER)
    await run_queue(sv)
    assert tg.sent(OWNER) == ["Paris."]
    assert sv.db.kv_get("tg_offset") == tg.updates[-1]["update_id"] + 1     # offset survives a restart


async def test_unknown_users_and_groups_are_ignored_silently(bot_env):
    sv, tg = await bot_env()
    tg.push("hello?", user=STRANGER)
    tg.push("/start", user=STRANGER)
    tg.push("/status", user=STRANGER)
    tg.push("hello group", user=OWNER, chat_type="group", chat_id=-100123)
    tg.push("hello supergroup", user=OWNER, chat_type="supergroup", chat_id=-100124)
    tg.updates.append({"update_id": 999, "channel_post": {"text": "x"}})
    tg.updates.append({"update_id": 1000, "message": {"from": {"id": OWNER, "is_bot": True},
                                                      "chat": {"id": OWNER, "type": "private"}, "text": "bot"}})
    await sv.telegram.poll_once()
    assert tg.sent() == [] and sv.db.list_tasks() == []
    assert [m for m, _ in tg.calls] == ["getUpdates"]                       # not even a typing indicator


async def test_pairing_makes_the_first_owner(bot_env):
    sv, tg = await bot_env(telegram_allowed_ids=frozenset())
    tg.push("hi", user=STRANGER)
    tg.push("/pair 00000000", user=STRANGER)
    tg.push("/pair 31415926", user=STRANGER, chat_type="group", chat_id=-5)       # never in a group
    await sv.telegram.poll_once()
    assert tg.sent() == ["That code is not right."] and sv.telegram.owners() == set()
    tg.push("/pair 31415926", user=STRANGER)
    await sv.telegram.poll_once()
    assert sv.telegram.owners() == {STRANGER} and sv.db.kv_get("tg_owners") == [STRANGER]
    assert tg.sent(STRANGER)[-1].startswith("Paired.")
    # With an owner in place the code is dead: nobody else can pair, and they get no answer.
    tg.push("/pair 31415926", user=7777)
    await sv.telegram.poll_once()
    assert sv.telegram.owners() == {STRANGER} and tg.sent(7777) == []


async def test_pairing_is_impossible_when_an_owner_is_configured(bot_env):
    sv, tg = await bot_env()                                                # allowlist already has OWNER
    tg.push("/pair 31415926", user=STRANGER)
    await sv.telegram.poll_once()
    assert sv.telegram.owners() == {OWNER} and tg.sent() == []


async def test_pairing_attempts_are_rate_limited(bot_env):
    sv, tg = await bot_env(telegram_allowed_ids=frozenset())
    now = {"t": 1000.0}
    sv.telegram._clock = lambda: now["t"]
    for i in range(8):
        tg.push(f"/pair 0000000{i}", user=STRANGER + i)                     # different accounts do not help
    await sv.telegram.poll_once()
    assert len(tg.sent()) == 5
    tg.push("/pair 31415926", user=STRANGER)                                # even the right code is ignored now
    await sv.telegram.poll_once()
    assert sv.telegram.owners() == set() and len(tg.sent()) == 5
    now["t"] += 601                                                         # the window passes
    tg.push("/pair 31415926", user=STRANGER)
    await sv.telegram.poll_once()
    assert sv.telegram.owners() == {STRANGER}


async def test_pairing_disabled_without_a_code(bot_env):
    sv, tg = await bot_env(telegram_allowed_ids=frozenset(), pairing_code="")
    tg.push("/pair", user=STRANGER)
    tg.push("/pair ", user=STRANGER)
    await sv.telegram.poll_once()
    assert sv.telegram.owners() == set() and tg.sent() == []


async def test_commands(bot_env):
    sv, tg = await bot_env(FakeLLM(say("done")))
    sv.schedules.add("0 8 * * *", "morning digest", "Digest")
    for text in ("/help", "/start", "/status", "/schedules", "/tasks", "/new", "/cancel", "/cancel 99",
                 "/bogus", "/help@my_bot", "/pair 1"):
        tg.push(text)
    await sv.telegram.poll_once()
    out = tg.sent(OWNER)
    assert out[0].startswith("I am your server's assistant") and out[1] == out[0] and out[9] == out[0]
    assert out[2].startswith("Model: ready") and "Queue: 0 waiting" in out[2] and "CPU governor: off" in out[2]
    assert out[3].startswith("#1 [on] Digest | 0 8 * * * | next: ")
    assert out[4] == "No tasks yet."
    assert out[5] == "Started a new conversation."
    assert out[6].startswith("Usage: /cancel") and out[7] == "There is no task #99."
    assert out[8].startswith("I do not know that command")
    assert out[10] == "You are already paired."
    assert sv.db.list_tasks() == []                                         # commands never reach the model


async def test_new_starts_a_fresh_conversation(bot_env):
    llm = FakeLLM(say("one"), say("two"), say("three"))
    sv, tg = await bot_env(llm)
    for text in ("first", "second", "/new", "third"):
        tg.push(text)
        await sv.telegram.poll_once()
        await run_queue(sv)
    assert [len(r["messages"]) for r in llm.requests] == [2, 4, 2]          # history, history, fresh
    assert tg.sent(OWNER) == ["one", "two", "Started a new conversation.", "three"]


async def test_cancel_command_and_non_text_messages(bot_env):
    sv, tg = await bot_env()
    task_id = sv.runner.submit("chat", "something slow")
    tg.push(f"/cancel {task_id}")
    tg.push(None, photo=[{"file_id": "x"}])
    await sv.telegram.poll_once()
    assert tg.sent(OWNER) == [f"Task #{task_id} cancelled.", "I can only read text messages."]
    assert sv.db.get_task(task_id)["status"] == "cancelled"


async def test_scheduled_results_go_to_every_owner(bot_env):
    sv, tg = await bot_env(FakeLLM(say("Three new articles today.")),
                           telegram_allowed_ids=frozenset({OWNER, 5151}))
    sv.runner.submit("scheduled", "check the news", name="News")
    await run_queue(sv)
    assert tg.sent(OWNER) == tg.sent(5151) == ['Scheduled task "News":\n\nThree new articles today.']
    sv.runner.submit("chat", "dashboard task")                              # not a Telegram matter
    await run_queue(sv)
    assert len(tg.sent()) == 2


async def test_long_replies_are_split(bot_env):
    sv, tg = await bot_env(FakeLLM(say("word " * 2500)))
    tg.push("write a lot")
    await sv.telegram.poll_once()
    await run_queue(sv)
    parts = tg.sent(OWNER)
    assert len(parts) == 4 and all(len(p) <= MESSAGE_LIMIT for p in parts)


async def test_typing_indicator_while_working(bot_env):
    sv, tg = await bot_env(FakeLLM(say("ok")))
    task_id = sv.runner.submit("telegram", "x", reply_to=OWNER)
    beats = 0

    async def sleep_then_finish(_s):
        nonlocal beats
        beats += 1
        if beats == 3:
            await run_queue(sv)

    sv.telegram._sleep = sleep_then_finish
    await sv.telegram._typing(OWNER, task_id)
    actions = [p for m, p in tg.calls if m == "sendChatAction"]
    assert len(actions) == 3 and actions[0] == {"chat_id": OWNER, "action": "typing"}


async def test_ok_false_is_an_error_and_the_token_never_leaks(bot_env, caplog):
    sv, tg = await bot_env()
    tg.fail["getUpdates"] = httpx.Response(401, json={"ok": False, "error_code": 401,
                                                      "description": f"Unauthorized: bad token {TOKEN}"})
    with pytest.raises(TelegramError) as err:
        await sv.telegram.poll_once()
    assert err.value.code == 401 and TOKEN not in str(err.value) and "Unauthorized" in str(err.value)

    tg.fail["getUpdates"] = httpx.Response(200, json={"ok": False, "description": "Conflict"})
    with pytest.raises(TelegramError, match="Conflict"):
        await sv.telegram.poll_once()                                       # HTTP 200 with ok:false is still a failure

    # A network error whose text contains the request URL (and so the token).
    tg.fail["getUpdates"] = httpx.ConnectError(f"cannot connect to https://telegram.test/bot{TOKEN}/getUpdates")
    with pytest.raises(TelegramError) as err:
        await sv.telegram.poll_once()
    assert TOKEN not in str(err.value) and "ConnectError" in str(err.value)

    tg.fail["getUpdates"] = httpx.Response(502, content=b"<html>bad gateway</html>")
    with pytest.raises(TelegramError, match="unreadable answer"):
        await sv.telegram.poll_once()


async def test_main_loop_backs_off_on_errors_without_logging_the_token(bot_env, caplog):
    sv, tg = await bot_env()
    naps: list[float] = []
    outcomes = [httpx.ConnectError(f"https://telegram.test/bot{TOKEN}/getUpdates"),
                httpx.Response(200, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                          "parameters": {"retry_after": 7}}),
                httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict: other poller"})]

    class Stop(Exception):
        pass

    async def nap(seconds):
        naps.append(seconds)
        if not outcomes:
            raise Stop()
        tg.fail["getUpdates"] = outcomes.pop(0)

    tg.fail["getUpdates"] = outcomes.pop(0)
    sv.telegram._sleep = nap
    with caplog.at_level(logging.DEBUG), pytest.raises(Stop):
        await sv.telegram.run()
    assert naps == [2.0, 7.0, 120.0]                    # backoff, the server's retry_after, long wait on conflict
    assert TOKEN not in caplog.text and "ConnectError" in caplog.text
    assert sv.telegram.last_error and TOKEN not in sv.telegram.last_error


async def test_flood_control_on_send_is_respected_once(bot_env):
    sv, tg = await bot_env()
    naps: list[float] = []
    state = {"n": 0}
    real = tg.handler

    def flaky(request):
        if request.url.path.endswith("/sendMessage"):
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                                 "parameters": {"retry_after": 3}})
        return real(request)

    sv.telegram._http = httpx.AsyncClient(transport=httpx.MockTransport(flaky))

    async def nap(seconds):
        naps.append(seconds)
    sv.telegram._sleep = nap
    await sv.telegram.send(OWNER, "hello")
    assert naps == [4.0] and tg.sent(OWNER) == ["hello"]


async def test_bot_is_inert_without_a_token(make_services):
    sv = await make_services(FakeLLM(say("x")), telegram_bot_token="")
    assert sv.telegram.enabled is False
    await sv.telegram.run()                              # returns at once
    await sv.telegram.on_task_finished({"kind": "scheduled", "status": "done", "result": "x", "id": 1})


async def test_redaction_filter_scrubs_secrets_from_any_log_line(settings):
    from app.logs import Redact
    cfg = replace(settings, telegram_bot_token=TOKEN)
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               "HTTP Request: POST %s", (f"https://api.telegram.org/bot{TOKEN}/getUpdates",), None)
    assert Redact(cfg.secrets()).filter(record) is True
    assert TOKEN not in record.getMessage() and "<redacted>" in record.getMessage()
