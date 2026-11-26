"""The internal OpenAI-compatible API (`/v1`), used by Open WebUI. Bearer key only.

`assistant` and `assistant-deep` are a transparent pass-through to the model
server. `agent` runs the tool loop and streams its progress.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .api_common import ApiError, ClientGone, close_soon, openai_error, read_body, until_disconnect
from .loop import TurnListener, TurnResult
from .services import Services

MAX_CHAT_BODY = 32 * 1024 * 1024       # pass-through bodies may carry images
MAX_CLIENT_MESSAGES = 400
MAX_INLINE_TURNS = 4
KEEPALIVE_SECONDS = 20.0
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

MODELS = (("assistant", "Assistant (fast chat)"),
          ("assistant-deep", "Assistant (thinks before answering)"),
          ("agent", "Agent (can search, run code, remember, schedule)"))


def rewrite_passthrough(body: dict[str, Any], *, model: str, thinking: bool, default_max_tokens: int) -> bytes:
    """The only changes made to a pass-through request."""
    body["model"] = model
    kwargs = body.get("chat_template_kwargs")
    body["chat_template_kwargs"] = {**(kwargs if isinstance(kwargs, dict) else {}), "enable_thinking": thinking}
    if body.get("max_tokens") is None and body.get("max_completion_tokens") is None:
        body["max_tokens"] = default_max_tokens
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def split_messages(messages: Any) -> tuple[list[dict[str, str]], str, str]:
    """Client messages -> (earlier user/assistant turns, newest user text, client system text).

    Only text is kept. Tool messages and anything after the newest user message are dropped.
    """
    if not isinstance(messages, list) or not messages:
        raise ApiError(400, "'messages' must be a non-empty list")
    system: list[str] = []
    turns: list[dict[str, str]] = []
    for msg in messages[-MAX_CLIENT_MESSAGES:]:
        if not isinstance(msg, dict):
            continue
        role, text = msg.get("role"), _text_of(msg.get("content")).strip()
        if not text:
            continue
        if role in ("system", "developer"):
            system.append(text)
        elif role in ("user", "assistant"):
            turns.append({"role": role, "content": text})
    last_user = max((i for i, t in enumerate(turns) if t["role"] == "user"), default=-1)
    if last_user < 0:
        raise ApiError(400, "there is no user message with text")
    return turns[:last_user], turns[last_user]["content"], "\n\n".join(system)


class _QueueListener(TurnListener):
    """Turns loop events into ("content" | "reasoning", text) items for the SSE writer."""

    def __init__(self, queue: asyncio.Queue):
        self._q = queue
        self._line_start = True

    def content(self, text: str) -> None:
        self._q.put_nowait(("content", text))

    def thought(self, text: str) -> None:
        self._line_start = text.endswith("\n")
        self._q.put_nowait(("reasoning", text))

    def activity(self, text: str) -> None:
        if not self._line_start:
            text = "\n" + text
        self._line_start = text.endswith("\n")
        self._q.put_nowait(("reasoning", text))


def build_router(sv: Services) -> APIRouter:
    router = APIRouter(prefix="/v1")
    s = sv.settings

    def check_key(request: Request) -> None:
        if not sv.auth.bearer_ok(request.headers.get("authorization")):
            raise ApiError(401, "invalid or missing API key")

    @router.get("/models")
    async def models(request: Request) -> JSONResponse:
        check_key(request)
        return JSONResponse({"object": "list", "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "myai", "name": name}
            for mid, name in MODELS]})

    @router.post("/chat/completions")
    async def chat_completions(request: Request) -> Response:
        check_key(request)
        raw = await read_body(request, MAX_CHAT_BODY)
        try:
            body = json.loads(raw)
        except ValueError:
            raise ApiError(400, "the request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "the request body must be a JSON object")
        model = body.get("model")
        if model in ("assistant", "assistant-deep"):
            return await passthrough(request, body, thinking=model == "assistant-deep")
        if model == "agent":
            return await agent_chat(request, body)
        return openai_error(404, f"The model '{str(model)[:80]}' does not exist.", code="model_not_found")

    # ---- assistant / assistant-deep ----
    async def passthrough(request: Request, body: dict[str, Any], thinking: bool) -> Response:
        stream = bool(body.get("stream"))
        payload = rewrite_passthrough(body, model=s.llm_model, thinking=thinking,
                                      default_max_tokens=s.chat_max_tokens)
        try:
            upstream = await until_disconnect(request, sv.llm.open_passthrough(payload))
        except ClientGone:
            return Response(status_code=499)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return openai_error(502, "The model server is not reachable.", "upstream_error")
        except httpx.TimeoutException:
            return openai_error(504, "The model server did not answer in time.", "upstream_error")
        except httpx.HTTPError as exc:
            return openai_error(502, f"The model server connection failed ({type(exc).__name__}).",
                                "upstream_error")
        ctype = upstream.headers.get("content-type", "application/json")
        if stream and upstream.status_code == 200:
            return StreamingResponse(relay(request, upstream), media_type=ctype, headers=SSE_HEADERS)
        try:
            data = await until_disconnect(request, upstream.aread())
        except ClientGone:
            return Response(status_code=499)
        except httpx.HTTPError as exc:
            return openai_error(502, f"The model server connection failed ({type(exc).__name__}).",
                                "upstream_error")
        finally:
            await upstream.aclose()
        # Status and body go back exactly as the model server sent them, errors included.
        return Response(content=data, status_code=upstream.status_code, media_type=ctype)

    async def relay(request: Request, upstream: httpx.Response) -> AsyncIterator[bytes]:
        """Copy upstream SSE bytes to the client; close upstream as soon as the client is gone."""
        chunks = upstream.aiter_raw().__aiter__()
        try:
            while True:
                try:
                    chunk = await until_disconnect(request, chunks.__anext__())
                except (StopAsyncIteration, ClientGone, httpx.HTTPError):
                    break
                yield chunk
        finally:
            close_soon(upstream)        # no await here: this block also runs while being cancelled

    # ---- agent ----
    def completion(result: TurnResult, cid: str, created: int) -> dict[str, Any]:
        return {"id": cid, "object": "chat.completion", "created": created, "model": "agent",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": result.text}}],
                "usage": usage(result)}

    def usage(result: TurnResult) -> dict[str, int]:
        return {"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                "total_tokens": result.prompt_tokens + result.completion_tokens}

    async def agent_chat(request: Request, body: dict[str, Any]) -> Response:
        history, user_text, system_extra = split_messages(body.get("messages"))
        if sv.runner.inline >= MAX_INLINE_TURNS:
            return openai_error(429, "Too many agent chats are running. Try again in a moment.",
                                "rate_limit_error")
        cid, created = "chatcmpl-" + secrets.token_hex(12), int(time.time())
        if body.get("stream"):
            opts = body.get("stream_options")
            want_usage = isinstance(opts, dict) and bool(opts.get("include_usage"))
            return StreamingResponse(
                agent_stream(request, history, user_text, system_extra, cid, created, want_usage),
                media_type="text/event-stream", headers=SSE_HEADERS)
        cancel = asyncio.Event()
        try:
            _task_id, result = await until_disconnect(request, sv.runner.run_inline(
                "webui", history, user_text, system_extra=system_extra, cancel=cancel))
        except ClientGone:
            return Response(status_code=499)
        if result.status == "error":
            return openai_error(502, result.error or "the agent failed", "agent_error")
        return JSONResponse(completion(result, cid, created))

    async def agent_stream(request: Request, history: list[dict], user_text: str, system_extra: str,
                           cid: str, created: int, want_usage: bool) -> AsyncIterator[bytes]:
        def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
            data = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "agent",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")

        queue: asyncio.Queue = asyncio.Queue()
        cancel = asyncio.Event()
        run = sv.runner.detach(sv.runner.run_inline(
            "webui", history, user_text, system_extra=system_extra,
            listener=_QueueListener(queue), cancel=cancel))
        run.add_done_callback(lambda _t: queue.put_nowait(None))
        try:
            yield chunk({"role": "assistant", "content": ""})
            quiet_since = time.monotonic()
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    if time.monotonic() - quiet_since >= KEEPALIVE_SECONDS:
                        # An SSE comment: keeps idle-timeout proxies and clients from giving up
                        # while the model is silently reading a long prompt.
                        yield b": keep-alive\n\n"
                        quiet_since = time.monotonic()
                    continue
                if item is None:
                    break
                kind, text = item
                yield chunk({"content": text} if kind == "content" else {"reasoning_content": text})
                quiet_since = time.monotonic()
            try:
                _task_id, result = run.result()
            except (Exception, asyncio.CancelledError):
                result = TurnResult(status="error", text="Sorry, that did not work: the run was interrupted.")
            if result.status != "done":
                # The answer itself was already streamed; these outcomes add a closing message.
                yield chunk({"content": result.text})
            yield chunk({}, "stop")
            if want_usage:
                data = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "agent",
                        "choices": [], "usage": usage(result)}
                yield f"data: {json.dumps(data)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            # Reached on normal completion and when the client disconnects mid-stream.
            cancel.set()

    return router
