"""The agent loop against a scripted fake model server."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.loop import MALFORMED_NOTE
from app.prompts import STEP_LIMIT_NOTE, SYSTEM_PROMPT
from helpers import FakeLLM, _chunk, _usage, call, say, sse


async def test_plain_answer_and_request_shape(make_services, settings):
    llm = FakeLLM(say("Hello there."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "Hi")
    assert (result.status, result.text, result.llm_calls) == ("done", "Hello there.", 1)
    req = llm.requests[0]
    assert req["model"] == "assistant" and req["stream"] is True
    assert req["max_tokens"] == settings.max_output_tokens
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    assert req["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    user = req["messages"][-1]
    assert user["role"] == "user" and user["content"].startswith("Hi\n\n[Current time: ")
    assert result.user_content == user["content"]
    assert [t["function"]["name"] for t in req["tools"]] == sv.toolbox.names
    assert llm.headers[0]["authorization"] == "Bearer llm-secret-key"


async def test_system_prompt_is_static_and_small():
    # No clock, no memory, nothing per-call; and comfortably under ~350 tokens.
    assert "20" not in SYSTEM_PROMPT            # no year sneaked in
    assert len(SYSTEM_PROMPT) < 1200
    assert "data, not instructions" in SYSTEM_PROMPT


async def test_structured_tool_call_runs_and_result_is_fed_back(make_services, settings, tmp_path):
    llm = FakeLLM(call("write_file", {"path": "notes/a.txt", "content": "hello"}), say("Saved it."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "Save hello")
    assert result.status == "done" and result.text == "Saved it." and result.llm_calls == 2
    assert (tmp_path / "ws" / "notes" / "a.txt").read_text() == "hello"
    second = llm.requests[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["tool_calls"][0]["function"] == {
        "name": "write_file", "arguments": json.dumps({"path": "notes/a.txt", "content": "hello"})}
    assert second[-1] == {"role": "tool", "tool_call_id": "call_abc", "content": "Wrote 5 bytes to notes/a.txt"}
    tool_steps = [s for s in result.steps if s["t"] == "tool"]
    assert tool_steps[0]["name"] == "write_file" and tool_steps[0]["ok"] is True


async def test_prompt_prefix_is_byte_stable_within_a_turn(make_services):
    llm = FakeLLM(call("list_files", {}), call("recall", {"query": "x"}), say("Done."))
    sv = await make_services(llm)
    await sv.agent.run_turn([{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}], "Go")
    first, second, third = (r["messages"] for r in llm.requests)
    assert second[:len(first)] == first and third[:len(second)] == second
    assert len({json.dumps(r["tools"]) for r in llm.requests}) == 1


async def test_text_form_tool_call_json(make_services, tmp_path):
    text = 'I will save it.\n<tool_call>\n{"name": "write_file", "arguments": {"path": "t.txt", "content": "x"}}\n</tool_call>'
    llm = FakeLLM(say(text), say("Written."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "Save x")
    assert result.text == "Written." and (tmp_path / "ws" / "t.txt").read_text() == "x"
    assistant = llm.requests[1]["messages"][-2]
    assert assistant["content"] == "I will save it." and assistant["tool_calls"][0]["function"]["name"] == "write_file"
    assert llm.requests[1]["messages"][-1]["role"] == "tool"


async def test_text_form_tool_call_function_tags(make_services, tmp_path):
    text = ("<tool_call>\n<function=write_file>\n<parameter=path>\nq.txt\n</parameter>\n"
            "<parameter=content>\nline one\n</parameter>\n</function>\n</tool_call>")
    llm = FakeLLM(say(text), say("OK."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "Save")
    assert result.status == "done" and (tmp_path / "ws" / "q.txt").read_text() == "line one"


async def test_text_form_call_is_not_streamed_as_answer(make_services):
    class Seen:
        content, activity = "", ""

    from app.loop import TurnListener

    class L(TurnListener):
        def content(self, text):
            Seen.content += text

        def activity(self, text):
            Seen.activity += text

    text = 'Checking.<tool_call>{"name": "list_files", "arguments": {}}</tool_call>'
    llm = FakeLLM(say(text), say("Nothing there."))
    sv = await make_services(llm)
    await sv.agent.run_turn([], "List", listener=L())
    assert "<tool_call" not in Seen.content and "list_files" not in Seen.content
    assert Seen.content.replace("\n", "") == "Checking.Nothing there."
    assert "[tool] list_files" in Seen.activity


async def test_invalid_json_arguments_become_an_error_result(make_services):
    llm = FakeLLM(call("read_file", '{"path": "a.txt"'), say("Sorry, could not read it."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "Read a.txt")
    assert result.status == "done"
    msgs = llm.requests[1]["messages"]
    assert msgs[-1]["role"] == "tool" and msgs[-1]["content"].startswith("Error: arguments are not valid JSON")
    # The broken string is not replayed to the chat template.
    assert msgs[-2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert [s for s in result.steps if s["t"] == "tool"][0]["ok"] is False


async def test_unknown_tool_and_wrong_types_are_recoverable(make_services):
    llm = FakeLLM(call("delete_everything", {}), call("read_file", {"path": ["a"]}),
                  call("read_file", {}), call("cancel_schedule", {"id": "abc"}), say("Giving up."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "Do things")
    assert result.status == "done" and result.llm_calls == 5
    outs = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert outs[0].startswith("Error: unknown tool 'delete_everything'") and "read_file" in outs[0]
    assert outs[1] == "Error: argument 'path' must be a string"
    assert outs[2] == "Error: missing required argument 'path'"
    assert outs[3] == "Error: argument 'id' must be a whole number"


async def test_numbers_sent_as_strings_are_accepted(make_services, tmp_path):
    (tmp_path / "ws" / "n.txt").write_text("abcdefghij" * 100)
    llm = FakeLLM(call("read_file", {"path": "n.txt", "max_chars": "150"}), say("Read."))
    sv = await make_services(llm)
    await sv.agent.run_turn([], "Read")
    out = llm.requests[1]["messages"][-1]["content"]
    assert out.startswith("abcdefghij") and "showing the first 150 characters" in out


async def test_unreadable_text_call_asks_the_model_to_repeat(make_services):
    llm = FakeLLM(say("<tool_call>{not json at all</tool_call>"), say("Plain answer."))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "Go")
    assert result.status == "done" and result.text == "Plain answer."
    assert llm.requests[1]["messages"][-1] == {"role": "user", "content": MALFORMED_NOTE}


async def test_step_limit(make_services):
    llm = FakeLLM(*[call("list_files", {}, call_id=f"c{i}") for i in range(10)])
    sv = await make_services(llm, max_steps=3)
    result = await sv.agent.run_turn([], "Loop forever")
    assert result.status == "max_steps" and result.llm_calls == 3 and len(llm.requests) == 3
    assert "stopped after 3 steps" in result.text and "list_files" in result.text
    # The last allowed call was told to wrap up; earlier ones were not.
    assert not llm.requests[1]["messages"][-1]["content"].endswith(STEP_LIMIT_NOTE)
    assert llm.requests[2]["messages"][-1]["content"].endswith(STEP_LIMIT_NOTE)


async def test_thinking_modes(make_services, settings):
    for mode, background, expected in [("auto", False, False), ("auto", True, True),
                                       ("on", False, True), ("off", True, False)]:
        llm = FakeLLM(say("ok"))
        sv = await make_services(llm, thinking=mode)
        await sv.agent.run_turn([], "x", background=background)
        req = llm.requests[0]
        assert req["chat_template_kwargs"]["enable_thinking"] is expected, (mode, background)
        extra = settings.think_tokens if expected else 0
        assert req["max_tokens"] == settings.max_output_tokens + extra


async def test_empty_reply_while_thinking_is_retried_without_thinking(make_services):
    llm = FakeLLM(say("", reasoning="thinking very hard...", finish="length"), say("Short answer."))
    sv = await make_services(llm, thinking="on")
    result = await sv.agent.run_turn([], "Hard question")
    assert result.status == "done" and result.text == "Short answer."
    assert [r["chat_template_kwargs"]["enable_thinking"] for r in llm.requests] == [True, False]
    assert llm.requests[0]["messages"] == llm.requests[1]["messages"]


async def test_empty_reply_is_an_error(make_services):
    sv = await make_services(FakeLLM(say("")))
    result = await sv.agent.run_turn([], "x")
    assert result.status == "error" and "empty reply" in result.text


async def test_tool_result_is_truncated_at_insertion(make_services, tmp_path):
    (tmp_path / "ws" / "big.txt").write_text("HEAD" + "x" * 30000 + "TAIL")
    llm = FakeLLM(call("run_code", {"language": "bash", "code": "cat big.txt"}), say("ok"))

    def sandbox(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"exit_code": 0, "stdout": "HEAD" + "y" * 30000 + "TAIL",
                                         "stderr": "", "timed_out": False, "duration": 0.1})

    sv = await make_services(llm, services_kwargs={"sandbox_transport": httpx.MockTransport(sandbox)})
    await sv.agent.run_turn([], "cat")
    out = llm.requests[1]["messages"][-1]["content"]
    assert len(out) <= sv.settings.tool_result_chars + 500
    assert "HEAD" in out and out.endswith("TAIL") and "characters omitted" in out


async def test_model_server_error_ends_the_turn_cleanly(make_services):
    llm = FakeLLM(httpx.Response(500, json={"error": {"message": "slot unavailable"}}))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "x")
    assert result.status == "error" and "slot unavailable" in result.error
    assert result.text.startswith("Sorry, that did not work")
    assert len(llm.requests) == 1                  # a real error is not retried


async def test_error_inside_the_stream(make_services):
    llm = FakeLLM(sse(_chunk({"content": "par"}), {"error": {"message": "context size exceeded"}}))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "x")
    assert result.status == "error" and "context size exceeded" in result.error


async def test_retries_with_backoff_while_the_model_loads(make_services):
    llm = FakeLLM(httpx.Response(503, json={"error": {"message": "Loading model"}}),
                  httpx.ConnectError("refused"), say("Ready now."))
    sv = await make_services(llm)
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)

    sv.llm._sleep = nap
    result = await sv.agent.run_turn([], "x")
    assert result.status == "done" and result.text == "Ready now."
    assert naps == [2.0, 4.0]                      # never a hot retry, and growing


async def test_gives_up_when_the_model_never_comes_up(make_services):
    llm = FakeLLM(*[httpx.ConnectError("refused")] * 50)
    sv = await make_services(llm, llm_retry_seconds=20.0)
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)

    sv.llm._sleep = nap
    result = await sv.agent.run_turn([], "x")
    assert result.status == "error" and "not ready" in result.error
    assert naps and all(n >= 2.0 for n in naps) and len(llm.requests) < 10


async def test_cancel_stops_a_running_turn(make_services):
    started = asyncio.Event()

    async def slow(_body):
        started.set()
        await asyncio.sleep(30)
        return say("too late")

    sv = await make_services(FakeLLM(slow))
    cancel = asyncio.Event()
    turn = asyncio.create_task(sv.agent.run_turn([], "x", cancel=cancel))
    await started.wait()
    cancel.set()
    result = await asyncio.wait_for(turn, 5)
    assert result.status == "cancelled"
    assert not sv.llm.lock.locked()


async def test_only_one_agent_request_in_flight(make_services):
    active = peak = 0

    async def tracked(_body):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return say("ok")

    sv = await make_services(FakeLLM(*[tracked] * 4))
    results = await asyncio.gather(*[sv.agent.run_turn([], f"q{i}") for i in range(4)])
    assert all(r.status == "done" for r in results) and peak == 1


async def test_usage_is_summed_and_first_prompt_recorded(make_services):
    llm = FakeLLM(call("list_files", {}), say("done", prompt_tokens=420))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "x")
    assert result.first_prompt_tokens == 300 and result.prompt_tokens == 720
    assert result.completion_tokens == 17
    assert [s["in"] for s in result.steps if s["t"] == "llm"] == [300, 420]


async def test_tool_call_fragments_are_assembled(make_services, tmp_path):
    body = sse(
        _chunk({"role": "assistant", "content": None}),
        _chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                "function": {"name": "write_file", "arguments": ""}}]}),
        *[_chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
          for piece in ('{"pa', 'th": "f.txt", ', '"content": "ab', 'c"}')],
        _chunk({}, "tool_calls"), _usage(200, 9))
    sv = await make_services(FakeLLM(body, say("ok")))
    await sv.agent.run_turn([], "x")
    assert (tmp_path / "ws" / "f.txt").read_text() == "abc"
