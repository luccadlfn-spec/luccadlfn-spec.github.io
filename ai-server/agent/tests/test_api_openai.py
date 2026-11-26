"""The /v1 API: pass-through models and the `agent` model."""

from __future__ import annotations

import json

import httpx
import pytest

from app.api_openai import rewrite_passthrough, split_messages
from app.api_common import ApiError
from helpers import BEARER, ByteChunks, FakeLLM, call, say

CHAT = "/v1/chat/completions"


def events(body: bytes) -> list:
    """Parse an SSE body into its data payloads ("[DONE]" stays a string)."""
    out = []
    for line in body.decode().splitlines():
        if line.startswith("data: "):
            data = line[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


# ---- auth and model list ----

async def test_models_lists_the_three_models(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        r = await c.get("/v1/models", headers=BEARER)
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["assistant", "assistant-deep", "agent"]
    assert all(m["object"] == "model" for m in r.json()["data"])


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic dGVzdA=="},
                                     {"Authorization": "Bearer "}, {"Authorization": "test-agent-key"}])
async def test_v1_requires_the_bearer_key(make_services, client_for, headers):
    llm = FakeLLM()
    sv = await make_services(llm)
    async with client_for(sv) as c:
        assert (await c.get("/v1/models", headers=headers)).status_code == 401
        r = await c.post(CHAT, headers=headers, json={"model": "assistant", "messages": []})
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    assert llm.requests == []


async def test_an_empty_key_disables_the_api_instead_of_opening_it(make_services, client_for):
    sv = await make_services(agent_api_key="")
    async with client_for(sv) as c:
        for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer"}):
            assert (await c.get("/v1/models", headers=headers)).status_code == 401
            assert (await c.get("/agent/api/status", headers=headers)).status_code == 401


async def test_unknown_model_and_bad_bodies(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "gpt-4", "messages": []})
        assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
        r = await c.post(CHAT, headers=BEARER, content=b"{not json")
        assert r.status_code == 400
        r = await c.post(CHAT, headers=BEARER, json=["a list"])
        assert r.status_code == 400
        r = await c.post(CHAT, headers={**BEARER, "Content-Length": str(64 * 1024 * 1024)}, content=b"{}")
        assert r.status_code == 413


# ---- pass-through ----

def test_rewrite_changes_only_what_it_must():
    original = {"model": "assistant", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2,
                "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}], "tool_choice": "auto",
                "chat_template_kwargs": {"enable_thinking": True, "custom": 1}, "seed": 7}
    out = json.loads(rewrite_passthrough(dict(original), model="real-model", thinking=False, default_max_tokens=4096))
    assert out["model"] == "real-model" and out["max_tokens"] == 4096
    assert out["chat_template_kwargs"] == {"enable_thinking": False, "custom": 1}
    for key in ("messages", "temperature", "tools", "tool_choice", "seed"):
        assert out[key] == original[key]
    assert list(out) == list(original) + ["max_tokens"]             # key order is preserved too

    kept = json.loads(rewrite_passthrough({"model": "x", "max_tokens": 50}, model="m", thinking=True, default_max_tokens=9))
    assert kept["max_tokens"] == 50 and kept["chat_template_kwargs"] == {"enable_thinking": True}
    newer = json.loads(rewrite_passthrough({"max_completion_tokens": 70}, model="m", thinking=True, default_max_tokens=9))
    assert "max_tokens" not in newer
    odd = json.loads(rewrite_passthrough({"chat_template_kwargs": "nonsense"}, model="m", thinking=False, default_max_tokens=9))
    assert odd["chat_template_kwargs"] == {"enable_thinking": False}


async def test_passthrough_forwards_the_body_and_returns_the_upstream_answer(make_services, client_for):
    upstream_json = {"id": "x", "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                     "timings": {"predicted_per_second": 5.5}}
    llm = FakeLLM(httpx.Response(200, json=upstream_json), httpx.Response(200, json=upstream_json))
    sv = await make_services(llm)
    body = {"model": "assistant", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": [{"type": "function", "function": {"name": "client_tool", "parameters": {"type": "object"}}}],
            "tool_choice": "auto", "temperature": 0.1, "top_p": 0.5, "stream": False, "unknown_future_field": {"a": 1}}
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json=body)
        assert r.status_code == 200 and r.json() == upstream_json                     # untouched, timings included
        await c.post(CHAT, headers=BEARER, json={**body, "model": "assistant-deep", "max_tokens": 77})
    fast, deep = llm.requests
    assert fast == {**body, "model": "assistant", "chat_template_kwargs": {"enable_thinking": False},
                    "max_tokens": sv.settings.chat_max_tokens}
    assert deep["chat_template_kwargs"] == {"enable_thinking": True} and deep["max_tokens"] == 77
    assert llm.headers[0]["authorization"] == "Bearer llm-secret-key"                 # the model's key, not the client's
    assert not sv.llm.lock.locked() and sv.db.list_tasks() == []                      # no lock, no task record


async def test_passthrough_streams_upstream_bytes_unchanged(make_services, client_for):
    raw = (b'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n\n'
           b'data: {"choices":[{"delta":{"reasoning_content":"hm"}}]}\n\n'
           b': a comment line\n\n'
           b'data: {"choices":[{"delta":{"content":"H\xc3\xa9llo"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":3},"timings":{"predicted_per_second":7.1}}\n\n'
           b'data: [DONE]\n\n')
    pieces = [raw[i:i + 37] for i in range(0, len(raw), 37)]        # arbitrary cuts, mid-event and mid-character
    body = ByteChunks(pieces)
    llm = FakeLLM(httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))
    sv = await make_services(llm)
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "assistant-deep", "stream": True,
                                                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.content == raw
    assert body.closed                                   # the upstream connection is released
    assert llm.headers[0]["accept-encoding"] == "identity"
    assert r.headers["content-type"].startswith("text/event-stream")
    assert llm.requests[0]["stream"] is True and llm.requests[0]["chat_template_kwargs"]["enable_thinking"] is True


@pytest.mark.parametrize("status,payload", [
    (400, {"error": {"code": 400, "message": "the request exceeds the available context size", "type": "exceed_context_size_error"}}),
    (500, {"error": {"code": 500, "message": "Failed to parse tool call", "type": "server_error"}}),
    (503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_passthrough_propagates_upstream_errors(make_services, client_for, status, payload, stream):
    llm = FakeLLM(httpx.Response(status, json=payload))
    sv = await make_services(llm)
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "assistant", "stream": stream,
                                                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == status and r.json() == payload
    assert len(llm.requests) == 1                                   # pass-through never retries


async def test_passthrough_reports_an_unreachable_model_server(make_services, client_for):
    llm = FakeLLM(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"))
    sv = await make_services(llm)
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "assistant", "messages": []})
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
        r = await c.post(CHAT, headers=BEARER, json={"model": "assistant", "messages": [], "stream": True})
        assert r.status_code == 504


# ---- agent ----

def test_split_messages_keeps_text_only():
    history, user, system = split_messages([
        {"role": "system", "content": "Answer in French."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "réponse", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "ignored tool output"},
        {"role": "user", "content": [{"type": "text", "text": "look at"}, {"type": "image_url", "image_url": {"url": "data:..."}},
                                     {"type": "text", "text": "this"}]},
        {"role": "assistant", "content": "a trailing assistant message"},
    ])
    assert system == "Answer in French." and user == "look at\nthis"
    assert history == [{"role": "user", "content": "first"}, {"role": "assistant", "content": "réponse"}]
    for bad in ([], "text", [{"role": "assistant", "content": "only me"}], [{"role": "user", "content": ""}], None):
        with pytest.raises(ApiError):
            split_messages(bad)


async def test_agent_streaming_format(make_services, client_for, tmp_path):
    llm = FakeLLM(call("write_file", {"path": "out.txt", "content": "data"}),
                  say("All done: the file is written.", reasoning="Let me confirm."))
    sv = await make_services(llm)
    body = {"model": "agent", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Write out.txt"}],
            "tools": [{"type": "function", "function": {"name": "client_side_tool", "parameters": {}}}]}
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json=body)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = events(r.content)
    assert ev[-1] == "[DONE]"
    chunks = ev[:-1]
    assert all(c["object"] == "chat.completion.chunk" and c["model"] == "agent" for c in chunks)
    assert len({c["id"] for c in chunks}) == 1 and chunks[0]["id"].startswith("chatcmpl-")
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}

    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    reasoning = "".join(d.get("reasoning_content", "") for d in deltas)
    content = "".join(d.get("content", "") for d in deltas)
    assert content == "All done: the file is written."              # only the answer is content
    assert "[tool] write_file" in reasoning and "Wrote 4 bytes to out.txt" in reasoning
    assert "Let me confirm." in reasoning                           # model thoughts go to the same channel
    # Tool activity is announced before the answer starts.
    first_content = next(i for i, d in enumerate(deltas) if d.get("content"))
    assert any("write_file" in d.get("reasoning_content", "") for d in deltas[:first_content])

    finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]]
    assert finishes[-1] == "stop" and all(f is None for f in finishes[:-1])
    assert chunks[-2]["choices"][0]["delta"] == {}
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] > 0   # usage chunk, as requested

    # The client's tools were ignored, its system text was appended to ours, and the run is on record.
    sent = llm.requests[0]
    assert [t["function"]["name"] for t in sent["tools"]] == sv.toolbox.names
    assert sent["messages"][0]["content"].endswith("\n\nBe brief.")
    task = sv.db.list_tasks()[0]
    assert (task["kind"], task["status"], task["prompt"]) == ("webui", "done", "Write out.txt")
    assert sv.db.get_task(task["id"])["steps"][1]["name"] == "write_file"
    assert (tmp_path / "ws" / "out.txt").read_text() == "data"


async def test_agent_stream_without_usage_option_has_no_usage_chunk(make_services, client_for):
    sv = await make_services(FakeLLM(say("Hi.")))
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "agent", "stream": True,
                                                     "messages": [{"role": "user", "content": "hello"}]})
    ev = events(r.content)
    assert ev[-1] == "[DONE]" and ev[-2]["choices"][0]["finish_reason"] == "stop"
    assert all("usage" not in c for c in ev[:-1])


async def test_agent_non_streaming(make_services, client_for):
    llm = FakeLLM(call("remember", {"text": "likes tea"}), say("I will remember that."))
    sv = await make_services(llm)
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "agent", "messages": [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "Remember that I like tea."}]})
    data = r.json()
    assert r.status_code == 200 and data["object"] == "chat.completion" and data["model"] == "agent"
    assert data["choices"][0] == {"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "I will remember that."}}
    assert data["usage"]["prompt_tokens"] == 600
    assert [m["role"] for m in llm.requests[0]["messages"]] == ["system", "user", "assistant", "user"]
    assert sv.db.list_notes()[0]["text"] == "likes tea"


async def test_agent_errors(make_services, client_for):
    llm = FakeLLM(httpx.Response(500, json={"error": {"message": "kaboom"}}),
                  httpx.Response(500, json={"error": {"message": "kaboom"}}))
    sv = await make_services(llm)
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "agent", "messages": [{"role": "assistant", "content": "x"}]})
        assert r.status_code == 400
        r = await c.post(CHAT, headers=BEARER, json={"model": "agent", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 502 and "kaboom" in r.json()["error"]["message"]
        r = await c.post(CHAT, headers=BEARER, json={"model": "agent", "stream": True,
                                                     "messages": [{"role": "user", "content": "x"}]})
    ev = events(r.content)                                          # a stream has already said 200: explain in-band
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in ev[:-1] if c["choices"])
    assert r.status_code == 200 and "kaboom" in text and ev[-1] == "[DONE]"
    assert ev[-2]["choices"][0]["finish_reason"] == "stop"
    assert [t["status"] for t in sv.db.list_tasks()] == ["error", "error"]


async def test_agent_step_limit_message_is_streamed(make_services, client_for):
    llm = FakeLLM(*[call("list_files", {}, call_id=f"c{i}") for i in range(5)])
    sv = await make_services(llm, max_steps=2)
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "agent", "stream": True,
                                                     "messages": [{"role": "user", "content": "loop"}]})
    ev = events(r.content)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in ev[:-1] if c["choices"])
    assert "stopped after 2 steps" in text and len(llm.requests) == 2


async def test_too_many_concurrent_agent_chats_are_refused(make_services, client_for):
    sv = await make_services(FakeLLM())
    sv.runner.inline = 4
    async with client_for(sv) as c:
        r = await c.post(CHAT, headers=BEARER, json={"model": "agent", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 429
    sv.runner.inline = 0
