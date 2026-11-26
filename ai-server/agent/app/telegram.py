"""Optional Telegram bot: long polling, owner allowlist, pairing, a few commands.

Nothing here opens a port. Only numeric user IDs on the allowlist are answered;
everyone else is ignored without a reply. The bot token is part of every request
URL, so error text is built from fixed strings and scrubbed, never from the URL.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from .config import Settings
from .db import Database
from .runner import QueueFull, TaskRunner
from .schedules import ScheduleService

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50
MESSAGE_LIMIT = 4000
MAX_CHUNKS = 10
TYPING_EVERY = 4.0
TYPING_MAX_SECONDS = 3 * 3600.0
PAIR_ATTEMPTS = 5
PAIR_WINDOW = 600.0
MAX_BACKOFF = 120.0

HELP = ("I am your server's assistant. Send me a message and I will work on it.\n\n"
        "/new - start a fresh conversation\n"
        "/status - model, queue and CPU state\n"
        "/tasks - recent tasks\n"
        "/schedules - scheduled tasks\n"
        "/cancel <id> - cancel a task\n"
        "/help - this text")


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Cut a long reply into pieces Telegram accepts, at line or word breaks when possible."""
    text = text.strip() or "(empty reply)"
    chunks: list[str] = []
    while len(text) > limit and len(chunks) < MAX_CHUNKS - 1:
        cut = text.rfind("\n", limit // 2, limit)
        if cut < 0:
            cut = text.rfind(" ", limit // 2, limit)
        if cut < 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if len(text) > limit:
        text = text[:limit - 20].rstrip() + "\n[reply cut short]"
    chunks.append(text)
    return [c for c in chunks if c]


class TelegramBot:
    def __init__(self, settings: Settings, db: Database, runner: TaskRunner, schedules: ScheduleService,
                 status: Callable[[], Awaitable[dict[str, Any]]],
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.time):
        self.s, self.db, self.runner, self.schedules = settings, db, runner, schedules
        self._status, self._sleep, self._clock = status, sleep, clock
        self._token = settings.telegram_bot_token
        self.enabled = bool(self._token)
        if self.enabled:
            # httpx logs each request URL at INFO, and these URLs contain the token.
            # Enforced here as well as in the logging setup, so it holds for any entry point.
            for name in ("httpx", "httpcore"):
                if logging.getLogger(name).level < logging.WARNING:     # also when it is unset
                    logging.getLogger(name).setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(transport=transport, trust_env=False,
                                       limits=httpx.Limits(max_connections=6))
        self._pair_attempts: list[float] = []
        self._tasks: set[asyncio.Task] = set()
        self.last_error: str | None = None

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._http.aclose()

    # ---- Telegram API ----
    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "<token>") if self._token else text

    async def _api(self, method: str, payload: dict[str, Any], timeout: float = 20.0) -> Any:
        url = f"{self.s.telegram_api_base}/bot{self._token}/{method}"
        try:
            resp = await self._http.post(url, json=payload, timeout=httpx.Timeout(timeout, connect=10.0))
        except httpx.HTTPError as exc:
            # str(exc) can contain the URL, and the URL contains the token.
            raise TelegramError(f"{method}: network error ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: unreadable answer (HTTP {resp.status_code})",
                                code=resp.status_code) from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            data = data if isinstance(data, dict) else {}
            params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
            retry = params.get("retry_after")
            raise TelegramError(
                self._scrub(f"{method}: {str(data.get('description') or 'request refused')[:200]}"),
                code=data.get("error_code") or resp.status_code,
                retry_after=float(retry) if isinstance(retry, (int, float)) else None)
        return data.get("result")

    async def send(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            payload = {"chat_id": chat_id, "text": chunk, "link_preview_options": {"is_disabled": True}}
            try:
                await self._api("sendMessage", payload)
            except TelegramError as exc:
                if exc.retry_after is None or exc.retry_after > 60:
                    raise
                await self._sleep(exc.retry_after + 1)      # flood control: wait as told, once
                await self._api("sendMessage", payload)

    # ---- owners and pairing ----
    def owners(self) -> set[int]:
        paired = self.db.kv_get("tg_owners", [])
        return set(self.s.telegram_allowed_ids) | {i for i in paired if isinstance(i, int)}

    async def _pair(self, user_id: int, chat_id: int, text: str) -> None:
        """First-owner pairing. Ignored once any owner exists, and when attempts come too fast."""
        code = self.s.pairing_code
        if not code or self.owners():
            return
        now = self._clock()
        self._pair_attempts = [t for t in self._pair_attempts if now - t < PAIR_WINDOW]
        if len(self._pair_attempts) >= PAIR_ATTEMPTS:
            return
        self._pair_attempts.append(now)
        parts = text.split(maxsplit=1)
        given = parts[1].strip() if len(parts) > 1 else ""
        if hmac.compare_digest(given.encode("utf-8", "replace"), code.encode()):
            self.db.kv_set("tg_owners", [user_id])
            log.info("telegram: user %s paired as owner", user_id)
            await self.send(chat_id, "Paired. You are now the owner of this assistant.\n\n" + HELP)
        else:
            await self.send(chat_id, "That code is not right.")

    # ---- updates ----
    async def poll_once(self) -> int:
        offset = self.db.kv_get("tg_offset", 0)
        updates = await self._api("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT,
                                                 "allowed_updates": ["message"]},
                                  timeout=POLL_TIMEOUT + 15)
        if not isinstance(updates, list):
            raise TelegramError("getUpdates: unexpected answer")
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                log.warning("telegram: %s", exc)
            except Exception as exc:
                log.error("telegram: could not handle an update (%s)", type(exc).__name__)
            finally:
                if isinstance(update.get("update_id"), int):
                    # Stored per update, so a restart never replays a message.
                    self.db.kv_set("tg_offset", update["update_id"] + 1)
        return len(updates)

    async def handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        chat_id, user_id = chat.get("id"), sender.get("id")
        if chat.get("type") != "private" or not isinstance(chat_id, int) or not isinstance(user_id, int):
            return                                         # groups and channels are ignored
        if sender.get("is_bot"):
            return
        text = msg.get("text") if isinstance(msg.get("text"), str) else None
        words = text.split() if text else []
        command = words[0].split("@", 1)[0].lower() if words and words[0].startswith("/") else ""
        if user_id not in self.owners():
            if command == "/pair":
                await self._pair(user_id, chat_id, text or "")
            return                                         # strangers get no reply at all
        if text is None:
            await self.send(chat_id, "I can only read text messages.")
            return
        text = text.strip()
        if not text:
            return
        arg = text.split(maxsplit=1)[1].strip() if len(words) > 1 else ""
        if command in ("/start", "/help"):
            await self.send(chat_id, HELP)
        elif command == "/pair":
            await self.send(chat_id, "You are already paired.")
        elif command == "/new":
            self.db.kv_set(f"tg_conv:{user_id}", self.db.create_conversation("telegram"))
            await self.send(chat_id, "Started a new conversation.")
        elif command == "/status":
            await self.send(chat_id, self._format_status(await self._status()))
        elif command == "/tasks":
            await self.send(chat_id, self._format_tasks())
        elif command == "/schedules":
            items = self.schedules.list()
            await self.send(chat_id, "\n".join(self.schedules.describe(x) for x in items) or "No schedules.")
        elif command == "/cancel":
            await self.send(chat_id, self._cancel(arg))
        elif command:
            await self.send(chat_id, "I do not know that command. /help lists what I understand.")
        else:
            await self._chat(user_id, chat_id, text)

    async def _chat(self, user_id: int, chat_id: int, text: str) -> None:
        conv = self.db.kv_get(f"tg_conv:{user_id}")
        if not isinstance(conv, int) or not self.db.conversation_exists(conv):
            conv = self.db.create_conversation("telegram")
            self.db.kv_set(f"tg_conv:{user_id}", conv)
        try:
            task_id = self.runner.submit("telegram", text, conversation_id=conv, reply_to=chat_id)
        except QueueFull:
            await self.send(chat_id, "I am too busy right now: the task queue is full. Try again later.")
            return
        except ValueError as exc:
            await self.send(chat_id, f"I cannot take that message: {exc}.")
            return
        if len(self._tasks) < 20:
            task = asyncio.create_task(self._typing(chat_id, task_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _typing(self, chat_id: int, task_id: int) -> None:
        """Telegram shows 'typing' for about five seconds per call; keep it alive while working."""
        deadline = time.monotonic() + TYPING_MAX_SECONDS
        while self.runner.is_open(task_id) and time.monotonic() < deadline:
            try:
                await self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
            except TelegramError:
                pass
            await self._sleep(TYPING_EVERY)

    def _cancel(self, arg: str) -> str:
        digits = arg.lstrip("#")
        if not (digits.isascii() and digits.isdigit() and len(digits) <= 12):
            return "Usage: /cancel <task id>   (see /tasks)"
        task_id = int(digits)
        outcome = self.runner.cancel(task_id)
        return {"cancelled": f"Task #{task_id} cancelled.",
                "cancelling": f"Stopping task #{task_id}.",
                "finished": f"Task #{task_id} has already finished.",
                "not_found": f"There is no task #{task_id}."}[outcome]

    def _format_tasks(self) -> str:
        tasks = self.db.list_tasks(8)
        if not tasks:
            return "No tasks yet."
        return "\n".join(f"#{t['id']} {t['kind']} {t['status']}: {' '.join(t['prompt'].split())[:60]}"
                         for t in tasks)

    @staticmethod
    def _format_status(st: dict[str, Any]) -> str:
        model, queue, gov = st.get("model", {}), st.get("queue", {}), st.get("governor", {})
        lines = [f"Model: {model.get('state', 'unknown')}",
                 f"Queue: {queue.get('queued', 0)} waiting, {queue.get('running', 0)} running"
                 + (f"; task #{queue['held']} is paused by the CPU governor" if queue.get("held") else "")]
        if gov.get("enabled"):
            lines.append(f"CPU: {gov.get('busy_pct')}% busy, {gov.get('steal_pct')}% steal, "
                         f"credits about {gov.get('credits_pct')}% ({gov.get('reason')})")
        else:
            lines.append("CPU governor: off")
        return "\n".join(lines)

    # ---- results ----
    async def on_task_finished(self, task: dict[str, Any]) -> None:
        """Deliver results: chat replies to the chat, scheduled results to every owner."""
        if not self.enabled:
            return
        if task["status"] == "cancelled":
            body = "Cancelled."
        elif task["status"] == "error":
            body = task.get("result") or f"That did not work: {task.get('error') or 'unknown error'}."
        else:
            body = task.get("result") or "(no answer)"
        try:
            if task["kind"] == "telegram" and task.get("reply_to"):
                await self.send(task["reply_to"], body)
            elif task["kind"] == "scheduled":
                title = f"Scheduled task \"{task.get('name') or task['id']}\":"
                for owner in sorted(self.owners()):
                    await self.send(owner, f"{title}\n\n{body}")
        except TelegramError as exc:
            self.last_error = str(exc)
            log.warning("telegram: could not deliver a result: %s", exc)

    async def report_interrupted(self, tasks: list[dict[str, Any]]) -> None:
        """After a restart: tell the owner about work it cut off, instead of dropping it silently."""
        if not self.enabled:
            return
        for task in tasks:
            preview = " ".join(str(task.get("prompt") or "").split())
            preview = preview[:80] + ("…" if len(preview) > 80 else "")
            try:
                if task["kind"] == "telegram" and task.get("reply_to"):
                    await self.send(task["reply_to"], "The server restarted while I was working on your "
                                    f"message, so it was not finished. Please send it again.\n\n({preview})")
                elif task["kind"] == "scheduled":
                    note = f'Scheduled task "{task.get("name") or task["id"]}" was interrupted by a server restart.'
                    for owner in sorted(self.owners()):
                        await self.send(owner, f"{note} {task.get('next') or ''}".strip())
            except TelegramError as exc:
                self.last_error = str(exc)
                log.warning("telegram: could not report an interrupted task: %s", exc)

    # ---- main loop ----
    async def run(self) -> None:
        if not self.enabled:
            return
        log.info("telegram: bot enabled (long polling)")
        backoff = 2.0
        while True:
            try:
                await self.poll_once()
                backoff, self.last_error = 2.0, None
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                self.last_error = str(exc)
                # 401: wrong token. 409: another program is polling with the same token.
                delay = MAX_BACKOFF if exc.code in (401, 404, 409) else (exc.retry_after or backoff)
                log.warning("telegram: %s; next try in %.0f s", exc, delay)
                await self._sleep(min(max(delay, 1.0), 600.0))
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.error("telegram: unexpected %s; next try in %.0f s", type(exc).__name__, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
