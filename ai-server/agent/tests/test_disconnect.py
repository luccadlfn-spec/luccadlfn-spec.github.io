"""When a chat client goes away, the model server must stop working for it.

This is the one test that uses real sockets (loopback only): the bug it guards
against lives in the interplay of the ASGI server, the HTTP client and task
cancellation, which mocks cannot reproduce. On a CPU-only box a leaked request
keeps a model slot and the CPU busy for minutes.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace

import httpx
import pytest
import uvicorn

from app.api import create_app
from app.services import Services
from helpers import BEARER


class SlowModelServer:
    """Minimal HTTP/1.1 server: streams an SSE event every 20 ms until its client disconnects."""

    def __init__(self, silent: bool = False):
        self.silent = silent                # True: never send a token (like a long prompt evaluation)
        self.requests = 0
        self.gone = asyncio.Event()         # set when a client connection ends
        self.server: asyncio.AbstractServer | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length")))
            await reader.readexactly(length)
            self.requests += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            eof = asyncio.ensure_future(reader.read(1))          # returns b"" when the peer closes
            for i in range(1500):                                # 30 s at most
                if eof.done():
                    break
                if not self.silent:
                    event = b'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": f"w{i} "},
                                                                 "finish_reason": None}]}).encode() + b"\n\n"
                    writer.write(b"%x\r\n%s\r\n" % (len(event), event))
                    await writer.drain()
                await asyncio.sleep(0.02)
            eof.cancel()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.gone.set()
            writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def stack(settings):
    """A real uvicorn server running the app, in front of the slow fake model server."""
    started: list = []

    async def make(silent: bool = False):
        try:
            model = SlowModelServer(silent)
            model_port = await model.start()
            port = _free_port()
        except OSError as exc:
            pytest.skip(f"loopback sockets are not available here ({exc})")
        sv = Services(replace(settings, llm_base_url=f"http://127.0.0.1:{model_port}/v1"))
        await sv.start(background=False)
        server = uvicorn.Server(uvicorn.Config(create_app(sv), host="127.0.0.1", port=port,
                                               log_level="warning", lifespan="off"))
        task = asyncio.create_task(server.serve())
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        started.append((model, sv, server, task))
        return model, sv, f"http://127.0.0.1:{port}"

    yield make
    for model, sv, server, task in started:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        await model.stop()
        await sv.stop()


async def _leave_mid_stream(base: str, body: dict, read_some: bool) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False) as client:
        async with client.stream("POST", "/v1/chat/completions", headers=BEARER, json=body) as resp:
            assert resp.status_code == 200
            if read_some:
                seen = b""
                async for chunk in resp.aiter_raw():
                    seen += chunk
                    if b"w3 " in seen:                           # the model is generating: hang up now
                        break
            else:
                await asyncio.sleep(0.3)


@pytest.mark.parametrize("model_name", ["assistant", "agent"])
@pytest.mark.parametrize("silent", [False, True])
async def test_streaming_client_disconnect_stops_the_model(stack, model_name, silent):
    model, sv, base = await stack(silent=silent)
    body = {"model": model_name, "stream": True, "messages": [{"role": "user", "content": "long story"}]}
    await _leave_mid_stream(base, body, read_some=not silent)
    await asyncio.wait_for(model.gone.wait(), 5)                 # upstream connection closed promptly
    assert model.requests == 1
    if model_name == "agent":
        for _ in range(100):
            if sv.db.list_tasks()[0]["status"] != "running":
                break
            await asyncio.sleep(0.02)
        assert sv.db.list_tasks()[0]["status"] == "cancelled" and not sv.llm.lock.locked()


@pytest.mark.parametrize("model_name", ["assistant", "agent"])
async def test_non_streaming_client_disconnect_stops_the_model(stack, model_name):
    model, sv, base = await stack(silent=True)
    body = {"model": model_name, "messages": [{"role": "user", "content": "long story"}]}
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(10, read=0.5), trust_env=False) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post("/v1/chat/completions", headers=BEARER, json=body)
    await asyncio.wait_for(model.gone.wait(), 5)
    assert model.requests == 1


async def test_cancelling_a_task_through_the_api_stops_the_model(stack):
    model, sv, base = await stack(silent=True)
    task_id = sv.runner.submit("chat", "long job")
    worker = asyncio.create_task(sv.runner.run_forever())
    try:
        for _ in range(200):
            if model.requests:
                break
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False) as client:
            r = await client.post(f"/agent/api/tasks/{task_id}/cancel", headers=BEARER)
        assert r.json() == {"id": task_id, "result": "cancelling"}
        await asyncio.wait_for(model.gone.wait(), 5)
        for _ in range(100):
            if sv.db.get_task(task_id)["status"] == "cancelled":
                break
            await asyncio.sleep(0.02)
        assert sv.db.get_task(task_id)["status"] == "cancelled"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
