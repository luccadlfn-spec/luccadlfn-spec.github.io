"""Small helpers shared by the HTTP routes."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers


class ClientGone(Exception):
    """The HTTP client disconnected while we were still working for it."""


def is_json(request: Request) -> bool:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json"


async def read_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything larger than `limit` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "request body is too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json(request: Request, limit: int) -> dict[str, Any]:
    raw = await read_body(request, limit)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise ApiError(400, "the request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "the request body must be a JSON object")
    return data


async def until_disconnect(request: Request, awaitable: Awaitable[Any], poll: float = 1.0) -> Any:
    """Await `awaitable`, but cancel it and raise ClientGone when the client goes away.

    The model can be silent for minutes while it reads a long prompt; without this
    check a closed browser tab would keep the CPU busy until the answer is done.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll)
            if done:
                return task.result()
            if await request.is_disconnected():
                raise ClientGone()
    finally:
        if not task.done():
            # Cancel exactly once and do not wait for it here. This code can itself be
            # running under a cancellation that is delivered repeatedly (the server
            # cancels a streaming response that way when the client leaves); awaiting
            # the task would pass a second cancel into it and interrupt the HTTP
            # client's own cleanup, leaving the upstream connection open and the
            # model generating for nobody.
            task.cancel()
            task.add_done_callback(_retrieve)


def _retrieve(task: asyncio.Future) -> None:
    """Consume a finished task's outcome so asyncio does not log it as never retrieved."""
    if not task.cancelled():
        task.exception()


_closing: set[asyncio.Task] = set()


def close_soon(response: Any) -> None:
    """Close an httpx response from its own task, immune to the caller being cancelled."""
    task = asyncio.ensure_future(response.aclose())
    _closing.add(task)
    task.add_done_callback(_closing.discard)
    task.add_done_callback(_retrieve)


def client_id(request: Request) -> str:
    """Who is calling, for rate limits: the address the nearest proxy saw, else the peer."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip()[:64]
    return request.client.host if request.client else "unknown"


def openai_error(status: int, message: str, kind: str = "invalid_request_error",
                 code: str | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "param": None, "code": code}},
                        status_code=status)
