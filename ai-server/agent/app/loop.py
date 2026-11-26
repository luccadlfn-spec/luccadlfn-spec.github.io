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
from .history import TokenEstimator, clip_middle, message_units, trim_history
from .llm import ContextOverflow, LLMClient, LLMError, LLMReply
from .prompts import CONTEXT_FULL_NOTE, FINAL_ANSWER_NOTE, STEP_LIMIT_NOTE, system_prompt, with_time
from .toolcalls import ContentGate, history_arguments, parse_text_calls
from .tools.base import MAX_RESULT_CHARS, CallContext, Toolbox

log = logging.getLogger(__name__)

MAX_STEP_LOG = 80
MALFORMED_NOTE = ("[Your tool call could not be read. Call the tool again with the arguments "
                  "as one valid JSON object, or answer without a tool.]")
MIN_BUDGET = 1500               # prompt tokens, whatever the settings say
MIN_ANSWER_TOKENS = 256         # a final call with less room than this is not worth making
MIN_RESULT_TOKENS = 120         # below this a tool result is replaced by CONTEXT_FULL_NOTE
SLOT_MARGIN = 32


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
    status: str = "done"                # done | max_steps | context_full | error | cancelled
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
        self._think_warned = False

    def thinking_for(self, background: bool) -> bool:
        return self.s.thinking == "on" or (self.s.thinking == "auto" and background)

    def _warn_think_allowance(self) -> None:
        """Once per process: the thinking filled the whole allowance without an answer.
        The usual cause is a model server that allows more thinking (LLM_THINK_BUDGET)
        than the agent was told, which wastes that much generation on every such call."""
        if self._think_warned:
            return
        self._think_warned = True
        log.warning("the model thought for its whole allowance of %d tokens without answering. If the model "
                    "server's LLM_THINK_BUDGET is larger, give the agent the same value (AGENT_THINK_TOKENS or "
                    "LLM_THINK_BUDGET in its environment).", self.s.think_tokens)

    async def run_turn(self, history: list[dict], user_text: str, *, background: bool = False,
                       scheduled: bool = False, schedule_id: int | None = None, system_extra: str = "",
                       listener: TurnListener | None = None, cancel: asyncio.Event | None = None,
                       pause: Callable[[float], Awaitable[str | None]] | None = None,
                       now: datetime | None = None) -> TurnResult:
        """Run one turn. `history` holds earlier user/assistant messages, oldest first."""
        s, est = self.s, self.est
        listen = listener or TurnListener()
        result = TurnResult()
        thinking = self.thinking_for(background)
        # Thinking tokens come out of the same context window, so the prompt gets less room.
        max_tokens = s.max_output_tokens + (s.think_tokens if thinking else 0)
        tools = self.toolbox.schemas
        system = {"role": "system", "content": system_prompt(system_extra)}
        now = now or datetime.now(s.tz)          # one reading, so a refitted prompt keeps the same time line

        messages = self._open(system, tools, history, user_text, max_tokens, now)
        result.user_content = messages[-1]["content"]
        turn_start = len(messages)              # what this turn adds starts here
        ctx = CallContext(background=background, scheduled=scheduled, schedule_id=schedule_id)
        refitted = False

        try:
            for call_no in range(1, s.max_steps + 1):
                while True:
                    self._check(cancel)
                    plan = self._plan(messages, tools, thinking, max_tokens)
                    if plan is None:
                        return self._context_full(result, listen)
                    final, call_thinking, call_max = plan
                    gate = ContentGate(listen.content)

                    def on_delta(kind: str, text: str, gate: ContentGate = gate) -> None:
                        gate.feed(text) if kind == "content" else listen.thought(text)

                    sent_units = est.prompt_units(messages, tools)
                    try:
                        reply: LLMReply = await self._guard(
                            self.llm.chat(messages, tools, thinking=call_thinking, max_tokens=call_max,
                                          on_delta=on_delta), cancel)
                        break
                    except ContextOverflow as exc:
                        # The server checks the size before doing any work, so trying again
                        # with what it told us costs almost nothing. Once only.
                        if refitted:
                            raise
                        refitted = True
                        est.learn_overflow(sent_units, exc.prompt_tokens, exc.n_ctx)
                        self._note(result, listen, f"the prompt did not fit the model's context "
                                                   f"({exc.prompt_tokens or '?'} of {exc.n_ctx or '?'} tokens); "
                                                   "shortened it and tried again")
                        if len(messages) == turn_start:
                            # Nothing has been generated yet, so this is still the start of the turn.
                            messages = self._open(system, tools, history, user_text, max_tokens, now)
                            result.user_content = messages[-1]["content"]
                            turn_start = len(messages)
                        else:
                            self._shrink(messages, turn_start, tools, self._budget(max_tokens))
                est.calibrate(sent_units, reply.prompt_tokens)
                self._account(result, reply, call_no, call_thinking, listen)

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
                    if final:
                        return self._context_full(result, listen)
                    if thinking and call_no < s.max_steps:
                        # The reasoning used the whole output allowance. Ask again without it;
                        # the prompt is unchanged, so this costs only the generation.
                        if reply.finish_reason == "length":
                            self._warn_think_allowance()
                        thinking, max_tokens = False, s.max_output_tokens
                        self._note(result, listen, "empty reply while thinking; retrying without thinking")
                        continue
                    return self._fail(result, "the model returned an empty reply")

                if final:
                    # More tool calls, but no room is left for their results.
                    return self._context_full(result, listen)

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
                        await self._run_tool(call, call_id, call_no, ctx, messages, tools,
                                             self._budget(max_tokens), result, listen, cancel)
                if call_no == s.max_steps - 1:
                    messages[-1]["content"] += STEP_LIMIT_NOTE
                if background and pause is not None:
                    note = await pause(reply.duration)
                    if note:
                        self._note(result, listen, note)

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

    # ---- the prompt budget ----
    def _limit(self) -> int:
        """Prompt plus output must stay within this: the settings' share of one model slot,
        or less once the model server has said how large its slot really is."""
        limit = self.s.ctx_tokens + self.s.max_output_tokens
        if self.est.slot:
            limit = min(limit, self.est.slot - SLOT_MARGIN)
        return limit

    def _budget(self, max_tokens: int) -> int:
        return max(MIN_BUDGET, self._limit() - max_tokens)

    def _open(self, system: dict, tools: list[dict], history: list[dict], user_text: str,
              max_tokens: int, now: datetime) -> list[dict[str, Any]]:
        """Start of turn: the only place where history is trimmed. Part of the budget
        is kept free for the tool results this turn will add."""
        est = self.est
        budget = self._budget(max_tokens)
        start_budget = budget - min(4000, budget // 3)
        fixed = est.prompt_units([system], tools)
        user_room = max(150, start_budget - est.tokens(fixed) - 100)
        user = {"role": "user", "content": with_time(est.clip(user_text, user_room), self.s.tz, now)}
        kept = trim_history(history, fixed + message_units(user), start_budget, est)
        return [system, *kept, user]

    def _plan(self, messages: list[dict], tools: list[dict], thinking: bool,
              max_tokens: int) -> tuple[bool, bool, int] | None:
        """(final, thinking, max_tokens) for the next call, or None when no call fits any more."""
        if self.est.prompt_tokens(messages, tools) <= self._budget(max_tokens):
            return False, thinking, max_tokens
        # The prompt outgrew its budget during this turn, mostly through the model's own
        # tool arguments. History may only be trimmed at the start of a turn, so this call
        # has to be the last: no thinking, and an answer that fits in what is left.
        if not messages[-1]["content"].endswith(FINAL_ANSWER_NOTE):
            messages[-1]["content"] += FINAL_ANSWER_NOTE
        room = min(self.s.max_output_tokens, self._limit() - self.est.prompt_tokens(messages, tools))
        return (True, False, room) if room >= MIN_ANSWER_TOKENS else None

    def _shrink(self, messages: list[dict], start: int, tools: list[dict], budget: int) -> None:
        """After the server refused a prompt mid-turn: shorten this turn's tool results,
        newest first, until the prompt fits the budget again."""
        est = self.est
        for msg in reversed(messages[start:]):
            over = est.prompt_tokens(messages, tools) - budget
            if over <= 0:
                return
            if msg.get("role") != "tool" or msg["content"] == CONTEXT_FULL_NOTE:
                continue
            keep = est.text_tokens(msg["content"]) - over - 40
            msg["content"] = est.clip(msg["content"], keep) if keep >= MIN_RESULT_TOKENS else CONTEXT_FULL_NOTE

    def _context_full(self, result: TurnResult, listen: TurnListener) -> TurnResult:
        result.status = "context_full"
        last = next((st for st in reversed(result.steps) if st.get("t") == "tool"), None)
        result.text = ("I had to stop: this task no longer fits in the model's context window."
                       + (f" The last thing I did was call {last['name']}." if last else "")
                       + " Ask me to continue, or split the work into smaller steps.")
        self._note(result, listen, "the context window is full; stopped")
        return result

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
        room = budget - self.est.prompt_tokens(messages, tools) - 60
        if room < MIN_RESULT_TOKENS:
            return CONTEXT_FULL_NOTE
        return self.est.clip(clip_middle(text, limit + 500), room)

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
