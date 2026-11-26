"""Client for the OpenAI-compatible llama-server.

Agent calls are streamed (so thoughts and answers can be shown as they arrive and
a cancelled turn frees the CPU at once), serialised by one lock, and retried with
backoff only while the server is unreachable or still loading the model.
Pass-through chat uses `open_passthrough` and never takes the lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from .config import Settings

log = logging.getLogger(__name__)

RETRY_DELAYS = (2.0, 4.0, 8.0, 15.0, 30.0)   # then 30 s for as long as the window allows
MAX_REPLY_CHARS = 200_000
MAX_TOOL_CALLS = 8

DeltaFn = Callable[[str, str], None]          # ("content" | "reasoning", text)

# llama-server: "request (17907 tokens) exceeds the available context size (16384 tokens), ..."
_OVERFLOW_TEXT = re.compile(r"\((\d+) tokens\) exceeds the available context size \((\d+) tokens\)")


class LLMError(Exception):
    """The model server answered with an error or the stream broke."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LLMUnavailable(LLMError):
    """The model server could not be reached, or was still loading, for the whole retry window."""


class ContextOverflow(LLMError):
    """The model server refused the prompt because it is larger than its context window.

    It refuses before doing any work, and usually says how large the prompt really was.
    """

    def __init__(self, prompt_tokens: int | None, n_ctx: int | None):
        self.prompt_tokens, self.n_ctx = prompt_tokens, n_ctx
        size = f" ({prompt_tokens} tokens; the model takes at most {n_ctx})" if prompt_tokens and n_ctx else ""
        super().__init__(f"this is too long for the model's context window{size}. "
                         "Start a new conversation or send a shorter message", 400)


def _overflow(err: Any) -> ContextOverflow | None:
    """A ContextOverflow when an error object from the model server says the prompt was too long."""
    if not isinstance(err, dict):
        return None
    message = str(err.get("message") or "")
    if err.get("type") != "exceed_context_size_error" and "exceeds the available context size" not in message:
        return None
    n_prompt, n_ctx = err.get("n_prompt_tokens"), err.get("n_ctx")
    if not (isinstance(n_prompt, int) and isinstance(n_ctx, int)):
        m = _OVERFLOW_TEXT.search(message)
        n_prompt, n_ctx = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    return ContextOverflow(n_prompt, n_ctx)


def _error_object(body: bytes) -> Any:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data.get("error", data) if isinstance(data, dict) else None


@dataclass
class LLMReply:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    timings: dict[str, Any] | None = None
    duration: float = 0.0


def _error_text(body: bytes, status: int) -> str:
    try:
        data = json.loads(body)
        err = data.get("error", data) if isinstance(data, dict) else data
        msg = err.get("message") if isinstance(err, dict) else str(err)
        if msg:
            return f"model server error {status}: {str(msg)[:400]}"
    except ValueError:
        pass
    return f"model server error {status}: {body[:200].decode('utf-8', 'replace')}"


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.s = settings
        self.lock = asyncio.Lock()            # one agent request in flight at a time
        self._sleep = sleep
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
        timeout = httpx.Timeout(connect=settings.llm_connect_timeout, read=settings.llm_read_timeout,
                                write=60.0, pool=settings.llm_connect_timeout)
        # trust_env=False: the model server is on the compose network, never behind a proxy.
        self._http = httpx.AsyncClient(headers=headers, timeout=timeout, transport=transport,
                                       trust_env=False,
                                       limits=httpx.Limits(max_connections=16, max_keepalive_connections=4))
        self._health: tuple[float, dict[str, Any]] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def chat_url(self) -> str:
        return f"{self.s.llm_base_url}/chat/completions"

    # ---- agent calls ----
    async def chat(self, messages: list[dict], tools: list[dict], *, thinking: bool, max_tokens: int,
                   on_delta: DeltaFn | None = None) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.s.llm_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": bool(thinking)},
        }
        if tools:
            body["tools"] = tools
        async with self.lock:
            started = time.monotonic()
            attempt = 0
            while True:
                try:
                    reply = await self._stream_once(body, on_delta)
                    reply.duration = time.monotonic() - started
                    return reply
                except _Retryable as exc:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    waited = time.monotonic() - started
                    if waited + delay > self.s.llm_retry_seconds:
                        raise LLMUnavailable(
                            f"the model server is not ready ({exc}); gave up after {waited:.0f} s") from None
                    log.warning("model server not ready (%s); retrying in %.0f s", exc, delay)
                    attempt += 1
                    await self._sleep(delay)

    async def _stream_once(self, body: dict, on_delta: DeltaFn | None) -> LLMReply:
        reply = LLMReply()
        calls: dict[int, dict[str, Any]] = {}
        size = 0
        try:
            async with self._http.stream("POST", self.chat_url, json=body) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread())[:4000]
                    if resp.status_code == 503:
                        raise _Retryable("503 loading")
                    overflow = _overflow(_error_object(raw))
                    if overflow is not None:
                        raise overflow
                    raise LLMError(_error_text(raw, resp.status_code), resp.status_code)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:
                        err = chunk["error"]
                        overflow = _overflow(err)
                        if overflow is not None:
                            raise overflow
                        msg = err.get("message") if isinstance(err, dict) else err
                        raise LLMError(f"model server error: {str(msg)[:400]}")
                    self._usage(chunk, reply)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        for key, kind in (("reasoning_content", "reasoning"), ("content", "content")):
                            text = delta.get(key)
                            if isinstance(text, str) and text:
                                size += len(text)
                                if kind == "content":
                                    reply.content += text
                                else:
                                    reply.reasoning += text
                                if on_delta:
                                    on_delta(kind, text)
                        for tc in delta.get("tool_calls") or []:
                            size += self._merge_call(calls, tc)
                        if choice.get("finish_reason"):
                            reply.finish_reason = choice["finish_reason"]
                    if size > MAX_REPLY_CHARS:
                        raise LLMError("model reply exceeded the size limit")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _Retryable(type(exc).__name__) from None
        except httpx.TimeoutException:
            raise LLMError(f"the model server did not answer within {self.s.llm_read_timeout:.0f} s") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"connection to the model server failed: {type(exc).__name__}") from None
        reply.tool_calls = [calls[i] for i in sorted(calls)][:MAX_TOOL_CALLS]
        return reply

    @staticmethod
    def _merge_call(calls: dict[int, dict[str, Any]], tc: dict) -> int:
        """Accumulate one streamed tool-call fragment; returns the characters added."""
        if not isinstance(tc, dict):
            return 0
        index = tc.get("index") if isinstance(tc.get("index"), int) else len(calls)
        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        fn = tc.get("function") or {}
        if tc.get("id"):
            call["id"] = str(tc["id"])
        if fn.get("name"):
            call["name"] = str(fn["name"])
        args = fn.get("arguments")
        if isinstance(args, dict):                 # some servers send a parsed object
            args = json.dumps(args)
        if isinstance(args, str):
            call["arguments"] += args
            return len(args)
        return 0

    @staticmethod
    def _usage(chunk: dict, reply: LLMReply) -> None:
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            reply.prompt_tokens = usage.get("prompt_tokens", reply.prompt_tokens)
            reply.completion_tokens = usage.get("completion_tokens", reply.completion_tokens)
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict):
                reply.cached_tokens = details.get("cached_tokens", reply.cached_tokens)
        if isinstance(chunk.get("timings"), dict):
            reply.timings = chunk["timings"]

    # ---- pass-through ----
    async def open_passthrough(self, body: bytes) -> httpx.Response:
        """Send a prepared chat body and return the open (unread) upstream response."""
        # identity: the bytes are relayed as they arrive, so they must not be compressed.
        req = self._http.build_request("POST", self.chat_url, content=body, headers={
            "Content-Type": "application/json", "Accept-Encoding": "identity"})
        return await self._http.send(req, stream=True)

    # ---- health ----
    async def health(self, max_age: float = 10.0) -> dict[str, Any]:
        """Cheap reachability check for the status page; cached so polling costs nothing."""
        now = time.monotonic()
        if self._health and now - self._health[0] < max_age:
            return self._health[1]
        base = self.s.llm_base_url
        root = base[:-3] if base.endswith("/v1") else base
        result: dict[str, Any]
        try:
            resp = await self._http.get(f"{root.rstrip('/')}/health", timeout=4.0)
            if resp.status_code == 200:
                result = {"reachable": True, "state": "ready"}
            elif resp.status_code == 503:
                result = {"reachable": True, "state": "loading"}
            else:
                result = {"reachable": False, "state": f"http {resp.status_code}"}
        except httpx.HTTPError as exc:
            result = {"reachable": False, "state": type(exc).__name__}
        result["model"] = self.s.llm_model
        self._health = (now, result)
        return result


class _Retryable(Exception):
    pass
