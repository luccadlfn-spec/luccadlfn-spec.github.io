"""Test helpers: a scripted fake model server that speaks llama-server's streaming format."""

from __future__ import annotations

import json
from typing import Any

import httpx

API_KEY = "test-agent-key"
PASSWORD = "correct horse battery"
BEARER = {"Authorization": f"Bearer {API_KEY}"}


def sse(*chunks: dict[str, Any]) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"object": "chat.completion.chunk", "model": "assistant",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _usage(prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    return {"object": "chat.completion.chunk", "choices": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "prompt_tokens_details": {"cached_tokens": 0}},
            "timings": {"prompt_n": prompt_tokens, "predicted_n": completion_tokens}}


def say(text: str, *, reasoning: str = "", finish: str = "stop", prompt_tokens: int = 300) -> bytes:
    """A streamed plain-text answer, split into fragments the way a real server sends it."""
    chunks = [_chunk({"role": "assistant", "content": None})]
    if reasoning:
        chunks.append(_chunk({"reasoning_content": reasoning}))
    step = max(1, len(text) // 3)
    chunks += [_chunk({"content": text[i:i + step]}) for i in range(0, len(text), step)]
    chunks += [_chunk({}, finish), _usage(prompt_tokens, 5)]
    return sse(*chunks)


def call(name: str, arguments: Any, *, call_id: str = "call_abc", content: str = "") -> bytes:
    """A streamed structured tool call; `arguments` may be a dict or a raw (possibly bad) string."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    half = len(raw) // 2
    chunks = [_chunk({"role": "assistant", "content": content or None})]
    chunks.append(_chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                          "function": {"name": name, "arguments": raw[:half]}}]}))
    chunks.append(_chunk({"tool_calls": [{"index": 0, "function": {"arguments": raw[half:]}}]}))
    chunks += [_chunk({}, "tool_calls"), _usage(300, 12)]
    return sse(*chunks)


class FakeLLM:
    """Plays back a script of replies and records every request body it receives."""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        item = self.script.pop(0) if self.script else say("(script exhausted)")
        if callable(item):
            item = item(body)
            if hasattr(item, "__await__"):
                item = await item
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=item)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class ByteChunks(httpx.AsyncByteStream):
    """A response body that really streams, one chunk at a time, like a network response."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True

