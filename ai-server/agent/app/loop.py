"""The agent loop: one user turn, up to AGENT_MAX_STEPS model calls with tool use in between.

Everything here is bounded: the number of model calls, the size of every tool
result, the size of the prompt. Nothing calls the model behind the user's back
(no summaries, titles or retries of a finished answer).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from .config import Settings
from .history import TokenEstimator, clip_middle, message_chars, trim_history
from .llm import LLMClient, LLMError, LLMReply
from .prompts import CONTEXT_FULL_NOTE, STEP_LIMIT_NOTE, system_prompt, with_time
from .toolcalls import ContentGate, history_arguments, parse_text_calls
from .tools.base import MAX_RESULT_CHARS, CallContext, Toolbox

log = logging.getLogger(__name__)

MAX_STEP_LOG = 80
MALFORMED_NOTE = ("[Your tool call could not be read. Call the tool again with the arguments "
                  "as one valid JSON object, or answer without a tool.]")


class TurnListener:
    """Receives what happens during a turn. All methods are optional no-ops."""

    def content(self, text: str) -> None:      # a piece of the answer
        pass

    def thought(self, text: str) -> None:      # a piece of the model's reasoning
        pass

    def activity(self, text: str) -> None:     # a line about tool use
        pass

    def steps(self, steps: list[dict]) -> None:  # the step log grew
        pass


@dataclass
class TurnResult:
    status: str = "done"                # done | max_steps | error | cancelled
    text: str = ""
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    user_content: str = ""              # the newest user message exactly as it was sent
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    first_prompt_tokens: int | None = None


class _Cancelled(Exception):
    pass


class Agent:
    def __init__(self, settings: Settings, llm: LLMClient, toolbox: Toolbox, estimator: TokenEstimator):
        self.s = settings
        self.llm = llm
        self.toolbox = toolbox
        self.est = estimator

    def thinking_for(self, background: bool) -> bool:
        return self.s.thinking == "on" or (self.s.thinking == "auto" and background)

    async def run_turn(self, history: list[dict], user_text: str, *, background: bool = False,
                       scheduled: bool = False, system_extra: str = "",
                       listener: TurnListener | None = None, cancel: asyncio.Event | None = None,
                       pause: Callable[[float], Awaitable[None]] | None = None,
                       now: datetime | None = None) -> TurnResult:
        """Run one turn. `history` holds earlier user/assistant messages, oldest first."""
        s, est = self.s, self.est
        listen = listener or TurnListener()
        result = TurnResult()
        thinking = self.thinking_for(background)
        # Thinking tokens come out of the same context window, so the prompt gets less room.
        budget = max(1500, s.ctx_tokens - (s.think_tokens if thinking else 0))
        max_tokens = s.max_output_tokens + (s.think_tokens if thinking else 0)
        tools = self.toolbox.schemas
        system = {"role": "system", "content": system_prompt(system_extra)}

        # Start of turn: the only place where history is trimmed. Part of the budget
        # is kept free for the tool results this turn will add.
        start_budget = budget - min(4000, budget // 3)
        fixed = est.prompt_chars([system], tools)
        user_room = max(500, est.chars(start_budget) - fixed - 300)
        user = {"role": "user", "content": with_time(clip_middle(user_text, user_room), s.tz, now)}
        result.user_content = user["content"]
        kept = trim_history(history, fixed + message_chars(user), start_budget, est)
        messages: list[dict[str, Any]] = [system, *kept, user]
        ctx = CallContext(background=background, scheduled=scheduled)

        try:
            for call_no in range(1, s.max_steps + 1):
                self._check(cancel)
                gate = ContentGate(listen.content)

                def on_delta(kind: str, text: str, gate: ContentGate = gate) -> None:
                    gate.feed(text) if kind == "content" else listen.thought(text)

                sent_chars = est.prompt_chars(messages, tools)
                reply: LLMReply = await self._guard(
                    self.llm.chat(messages, tools, thinking=thinking, max_tokens=max_tokens,
                                  on_delta=on_delta), cancel)
                est.calibrate(sent_chars, reply.prompt_tokens)
                self._account(result, reply, call_no, thinking, listen)

                calls, preface, malformed = reply.tool_calls, reply.content, False
                if not calls:
                    preface, calls, saw_marker = parse_text_calls(reply.content)
                    malformed = saw_marker and not calls

                if not calls and not malformed:
                    gate.flush()
                    text = reply.content.strip()
                    if text:
                        if reply.finish_reason == "length":
                            self._note(result, listen, "the answer was cut off at the output token limit")
                        result.text = text
                        return result
                    if thinking and call_no < s.max_steps:
                        # The reasoning used the whole output allowance. Ask again without it;
                        # the prompt is unchanged, so this costs only the generation.
                        thinking, max_tokens = False, s.max_output_tokens
                        self._note(result, listen, "empty reply while thinking; retrying without thinking")
                        continue
                    return self._fail(result, "the model returned an empty reply")

                if malformed:
                    messages.append({"role": "assistant", "content": reply.content})
                    messages.append({"role": "user", "content": MALFORMED_NOTE})
                    self._note(result, listen, "unreadable tool call; asked the model to repeat it")
                else:
                    if gate.emitted:
                        listen.content("\n\n")
                    messages.append({"role": "assistant", "content": preface.strip(), "tool_calls": [
                        {"id": c["id"] or f"call_{call_no}_{i}", "type": "function",
                         "function": {"name": c["name"], "arguments": history_arguments(c["arguments"])}}
                        for i, c in enumerate(calls)]})
                    for i, call in enumerate(calls):
                        call_id = call["id"] or f"call_{call_no}_{i}"
                        await self._run_tool(call, call_id, call_no, ctx, messages, tools, budget,
                                             result, listen, cancel)
                if call_no == s.max_steps - 1:
                    messages[-1]["content"] += STEP_LIMIT_NOTE
                if background and pause is not None:
                    await pause(reply.duration)

            result.status = "max_steps"
            last = next((st for st in reversed(result.steps) if st.get("t") == "tool"), None)
            result.text = (f"I stopped after {s.max_steps} steps without reaching a final answer."
                           + (f" The last thing I did was call {last['name']}." if last else "")
                           + " You can ask me to continue from here, or give me a narrower task.")
            return result
        except _Cancelled:
            result.status, result.text = "cancelled", "Cancelled."
            return result
        except LLMError as exc:
            return self._fail(result, str(exc))

    # ---- helpers ----
    async def _run_tool(self, call: dict, call_id: str, call_no: int, ctx: CallContext,
                        messages: list[dict], tools: list[dict], budget: int,
                        result: TurnResult, listen: TurnListener, cancel: asyncio.Event | None) -> None:
        name, raw = str(call["name"]), call["arguments"]
        shown = clip_middle(" ".join(str(raw).split()), 300)
        listen.activity(f"[tool] {name} {shown}\n")
        started = time.monotonic()
        text, ok = await self._guard(self.toolbox.call(name, raw, ctx), cancel)
        took = time.monotonic() - started
        text = self._fit(text, raw, messages, tools, budget)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
        summary = clip_middle(" ".join(text.split()), 200)
        listen.activity(f"[{'ok' if ok else 'failed'}, {took:.1f} s] {summary}\n")
        self._log(result, listen, {"t": "tool", "n": call_no, "name": name[:60], "args": shown,
                                   "ok": ok, "s": round(took, 1), "out": summary})

    def _fit(self, text: str, raw_args: Any, messages: list[dict], tools: list[dict], budget: int) -> str:
        """Shorten a tool result once, at insertion, to its limit and to the room that is left."""
        limit = self.s.tool_result_chars
        try:
            asked = json.loads(raw_args).get("max_chars") if isinstance(raw_args, str) else None
            if isinstance(asked, int) and not isinstance(asked, bool):
                limit = max(limit, min(MAX_RESULT_CHARS, asked))
        except (ValueError, AttributeError):
            pass
        room = self.est.chars(budget - self.est.prompt_tokens(messages, tools)) - 200
        if room < 400:
            return CONTEXT_FULL_NOTE
        return clip_middle(text, min(limit + 500, room))

    def _account(self, result: TurnResult, reply: LLMReply, call_no: int, thinking: bool,
                 listen: TurnListener) -> None:
        result.llm_calls += 1
        result.prompt_tokens += reply.prompt_tokens or 0
        result.completion_tokens += reply.completion_tokens or 0
        if result.first_prompt_tokens is None:
            result.first_prompt_tokens = reply.prompt_tokens
        self._log(result, listen, {"t": "llm", "n": call_no, "s": round(reply.duration, 1),
                                   "in": reply.prompt_tokens, "cached": reply.cached_tokens,
                                   "out": reply.completion_tokens, "think": thinking})

    def _note(self, result: TurnResult, listen: TurnListener, text: str) -> None:
        self._log(result, listen, {"t": "note", "text": text})

    @staticmethod
    def _log(result: TurnResult, listen: TurnListener, entry: dict) -> None:
        if len(result.steps) < MAX_STEP_LOG:
            result.steps.append(entry)
            listen.steps(result.steps)

    @staticmethod
    def _fail(result: TurnResult, message: str) -> TurnResult:
        result.status, result.error = "error", message
        result.text = f"Sorry, that did not work: {message}."
        return result

    @staticmethod
    def _check(cancel: asyncio.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise _Cancelled()

    @staticmethod
    async def _guard(awaitable: Awaitable[Any], cancel: asyncio.Event | None) -> Any:
        """Await something, but give up at once when the turn is cancelled."""
        if cancel is None:
            return await awaitable
        work = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
            if not work.done():
                # One cancel only, then wait with asyncio.wait (which, unlike gather, never
                # forwards a second cancel): a repeated cancel would interrupt the HTTP
                # client's cleanup and leave the model generating for nobody.
                work.cancel()
                work.add_done_callback(_retrieve)
                await asyncio.wait({work})
        if work.cancelled():
            raise _Cancelled()
        return work.result()


def _retrieve(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()
