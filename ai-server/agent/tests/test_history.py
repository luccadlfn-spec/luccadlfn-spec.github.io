"""History budget: what may be trimmed, and when."""

from __future__ import annotations

import httpx
import pytest

from app.history import (DEFAULT_UNITS_PER_TOKEN, MAX_UNITS_PER_TOKEN, MIN_UNITS_PER_TOKEN, TokenEstimator,
                         clip_middle, message_units, split_exchanges, text_units, trim_history)
from app.prompts import FINAL_ANSWER_NOTE, time_line, with_time
from helpers import FakeLLM, call, say
from datetime import datetime, timezone


def exchange(i: int, size: int = 400) -> list[dict]:
    return [{"role": "user", "content": f"question {i} " + "q" * size},
            {"role": "assistant", "content": f"answer {i} " + "a" * size}]


# Each sample, repeated ten times, and its real token count on the Qwen3 tokenizer of the
# development model server (POST /tokenize). Characters per token range from 4.8 (English)
# to 0.8 (Telugu): a plain characters-per-token estimate is off by up to six times.
MEASURED = [
    ("Please summarise the latest news about the economy and tell me what it means for my savings plan next year. ", 221),
    ("def fetch(url, timeout=20.0):\n    with urllib.request.urlopen(url, timeout=timeout) as resp:\n        return resp.read()\n", 300),
    ("412.07,88.31,5.62,901.44,37.90,610.25\n", 380),
    ("Пожалуйста, кратко изложи последние новости об экономике и расскажи, что это значит для моего плана. ", 321),
    ("请帮我总结一下今天关于经济的最新新闻，并告诉我这对我明年的储蓄计划意味着什么。", 210),
    ("오늘 경제에 관한 최신 뉴스를 요약해 주시고, 그것이 내년 저축 계획에 어떤 의미가 있는지 알려 주세요. ", 372),
    ("कृपया आज की अर्थव्यवस्था से जुड़ी ताज़ा खबरों का सारांश दें और बताएं कि इसका क्या मतलब है। ", 861),
    ("దయచేసి ఆర్థిక వ్యవస్థ గురించి తాజా వార్తలను సంగ్రహించి, దాని అర్థం ఏమిటో చెప్పండి. ", 1280),
    ("กรุณาสรุปข่าวล่าสุดเกี่ยวกับเศรษฐกิจในวันนี้ และบอกฉันว่าสิ่งนี้มีความหมายอย่างไร ", 451),
    ("Great job 🎉 see you soon 😀👍 party time 🥳🍕 ", 161),
]


@pytest.mark.parametrize("sample,real", MEASURED)
def test_estimate_follows_the_kind_of_text(sample, real):
    text = sample * 10
    fresh = TokenEstimator().text_tokens(text)
    assert real <= fresh <= 2 * real                 # a fresh install never under-estimates
    calibrated = TokenEstimator(load=lambda: MAX_UNITS_PER_TOKEN).text_tokens(text)
    assert calibrated >= 0.7 * real                  # nor does calibration push it far below


def test_text_units_is_plain_length_for_plain_words():
    assert text_units("hello world") == len("hello world")
    assert text_units("") == 0
    assert text_units("a1") == 1 + 4.5 and text_units("a.") == 1 + 1.5
    # Long letter-and-digit runs (hashes, base64) are dense; ordinary long words are not.
    assert text_units("x" * 30) == 30 and text_units("a1" * 15) > 15 * (1 + 4.5)


def test_clip_to_a_token_budget_keeps_head_and_tail():
    est = TokenEstimator()
    text = "START " + "कृपया आज की अर्थव्यवस्था " * 500 + " END"
    out = est.clip(text, 300)
    assert est.text_tokens(out) <= 300 and out.startswith("START ") and out.endswith(" END") and "omitted" in out
    assert est.clip("short", 300) == "short" and est.clip("anything", 0) == ""


def test_clip_middle_keeps_head_and_tail():
    text = "HEAD" + "x" * 5000 + "TAIL"
    out = clip_middle(text, 500)
    assert len(out) == 500 and out.startswith("HEAD") and out.endswith("TAIL") and "omitted" in out
    assert clip_middle("short", 500) == "short"
    assert clip_middle("anything", 0) == ""


def test_exchanges_start_at_user_messages():
    history = exchange(1) + [{"role": "user", "content": "u"}, {"role": "assistant", "content": "", "tool_calls": []},
                             {"role": "tool", "content": "r"}, {"role": "assistant", "content": "a"}]
    groups = split_exchanges(history)
    assert [len(g) for g in groups] == [2, 4]
    assert all(g[0]["role"] == "user" for g in groups)


def test_trim_drops_whole_oldest_exchanges_only():
    est = TokenEstimator()
    history = [m for i in range(10) for m in exchange(i)]
    kept = trim_history(history, fixed_units=1000, budget_tokens=1200, est=est)
    assert 0 < len(kept) < len(history) and len(kept) % 2 == 0
    assert kept == history[-len(kept):]                 # a suffix: only the oldest went
    assert kept[0]["role"] == "user"                    # never starts in the middle of an exchange
    assert est.tokens(1000 + sum(message_units(m) for m in kept)) <= 1200
    one_more = history[-len(kept) - 2:]                 # the next older exchange would not have fitted
    assert est.tokens(1000 + sum(message_units(m) for m in one_more)) > 1200
    assert trim_history(history, 1000, 10_000_000, est) == history
    assert trim_history(history, 1000, 10, est) == []   # nothing fits: history goes, caller keeps the rest


def test_estimator_calibrates_within_bounds_and_persists():
    saved: list[float] = []
    est = TokenEstimator(load=lambda: 3.0, save=saved.append)
    assert est.ratio == 3.0
    est.calibrate(sent_units=4000, real_tokens=1000)     # observed 4.0 units/token
    assert 3.0 < est.ratio < 4.0 and saved == [est.ratio]
    est.calibrate(sent_units=100, real_tokens=10)        # too small to trust
    est.calibrate(sent_units=4000, real_tokens=None)
    assert saved == [est.ratio]
    for _ in range(50):
        est.calibrate(sent_units=100_000, real_tokens=1000)   # absurd readings are clamped
    assert est.ratio == pytest.approx(MAX_UNITS_PER_TOKEN, abs=0.01)
    for _ in range(50):
        est.calibrate(sent_units=1000, real_tokens=5000)
    assert est.ratio == pytest.approx(MIN_UNITS_PER_TOKEN, abs=0.01)
    assert TokenEstimator(load=lambda: "garbage").ratio == DEFAULT_UNITS_PER_TOKEN
    assert TokenEstimator(load=lambda: 0.5).ratio == DEFAULT_UNITS_PER_TOKEN


def test_an_overflow_report_is_believed_at_once():
    saved: list[float] = []
    est = TokenEstimator(save=saved.append)
    est.learn_overflow(sent_units=50_000, real_tokens=17_907, slot=16_384)
    assert est.ratio == pytest.approx(0.9 * 50_000 / 17_907, abs=0.01) and saved == [est.ratio]
    assert est.slot == 16_384
    est.learn_overflow(sent_units=90_000, real_tokens=17_907, slot=None)   # never makes it less careful
    assert est.ratio == saved[0] and est.slot == 16_384
    est.learn_overflow(sent_units=50_000, real_tokens=None, slot=None)     # no numbers: more careful anyway
    assert est.ratio == pytest.approx(0.7 * saved[0], abs=0.01)


def test_time_line_is_short_and_in_the_configured_zone():
    now = datetime(2026, 11, 26, 14, 5, tzinfo=timezone.utc)
    assert time_line(timezone.utc, now) == "[Current time: 2026-11-26 14:05 Thursday, UTC]"
    assert with_time("Hi  \n", timezone.utc, now) == "Hi\n\n[Current time: 2026-11-26 14:05 Thursday, UTC]"


async def test_turn_start_trims_history_but_keeps_system_and_newest_user(make_services):
    llm = FakeLLM(say("ok"))
    sv = await make_services(llm, ctx_tokens=3000)
    history = [m for i in range(30) for m in exchange(i)]
    await sv.agent.run_turn(history, "the newest question")
    sent = llm.requests[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[-1]["role"] == "user" and sent[-1]["content"].startswith("the newest question")
    middle = sent[1:-1]
    assert 0 < len(middle) < len(history) and middle == history[-len(middle):]
    assert middle[0]["role"] == "user"
    assert sv.estimator.prompt_tokens(sent, llm.requests[0]["tools"]) <= 3000


async def test_nothing_is_trimmed_mid_turn(make_services, tmp_path):
    (tmp_path / "ws" / "big.txt").write_text("z" * 20000)
    llm = FakeLLM(*[call("read_file", {"path": "big.txt"}, call_id=f"c{i}") for i in range(5)], say("done"))
    sv = await make_services(llm, ctx_tokens=6000)
    sv.estimator.calibrate = lambda *_: None            # the fake server's token counts are not real
    history = [m for i in range(4) for m in exchange(i)]
    result = await sv.agent.run_turn(history, "read it many times")
    assert result.status == "done"
    first = llm.requests[0]["messages"]
    for later in llm.requests[1:]:
        assert later["messages"][:len(first)] == first      # every earlier message is still there, unchanged
    # Later results shrink to the room that is left instead of pushing history out...
    tool_sizes = [len(m["content"]) for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert tool_sizes[0] > tool_sizes[-1]
    assert "context is full" in llm.requests[-1]["messages"][-1]["content"]
    # ...and every request, prompt plus answer allowance, stays inside the window.
    for body in llm.requests:
        assert (sv.estimator.prompt_tokens(body["messages"], body["tools"]) + body["max_tokens"]
                <= 6000 + sv.settings.max_output_tokens)


async def test_oversized_user_message_is_shortened_not_dropped(make_services):
    llm = FakeLLM(say("ok"))
    sv = await make_services(llm, ctx_tokens=3000)
    await sv.agent.run_turn([], "START " + "w" * 200_000 + " END")
    user = llm.requests[0]["messages"][-1]["content"]
    assert user.startswith("START ") and " END\n\n[Current time:" in user and "omitted" in user
    assert sv.estimator.prompt_tokens(llm.requests[0]["messages"], llm.requests[0]["tools"]) <= 3000


# ---- when the estimate is wrong anyway: the server's refusal ----

def overflow_response(prompt_tokens: int, n_ctx: int) -> httpx.Response:
    """llama-server's answer to a prompt that does not fit (it refuses before doing any work)."""
    return httpx.Response(400, json={"error": {
        "code": 400, "type": "exceed_context_size_error", "n_prompt_tokens": prompt_tokens, "n_ctx": n_ctx,
        "message": f"request ({prompt_tokens} tokens) exceeds the available context size ({n_ctx} tokens), "
                   "try increasing it"}})


class DenseTokenizer:
    """Stands in for a model whose tokenizer is twice as dense as a fresh estimate assumes."""

    def __init__(self, sv, n_ctx: int):
        self.sv, self.n_ctx = sv, n_ctx

    def size(self, body: dict) -> int:
        return int(2 * self.sv.estimator.prompt_units(body["messages"], body["tools"]) / DEFAULT_UNITS_PER_TOKEN)

    def __call__(self, reply_text: str):
        def answer(body: dict):
            size = self.size(body)
            if size >= self.n_ctx:
                return overflow_response(size, self.n_ctx)
            return say(reply_text, prompt_tokens=size)
        return answer


async def test_overflow_at_the_start_of_a_turn_is_refitted_and_retried_once(make_services):
    llm = FakeLLM()
    sv = await make_services(llm, ctx_tokens=6000, max_output_tokens=500)
    dense = DenseTokenizer(sv, n_ctx=6200)
    llm.script = [dense("Here is the summary.")] * 2
    history = [m for i in range(8) for m in exchange(i)]
    text = "START " + "कृपया आज की अर्थव्यवस्था से जुड़ी ताज़ा खबरों का सारांश दें। " * 300 + " END"
    result = await sv.agent.run_turn(history, text)

    assert (result.status, result.text) == ("done", "Here is the summary.")
    assert len(llm.requests) == 2                                         # refused once, then it fitted
    first, second = llm.requests
    assert dense.size(first) >= 6200 and dense.size(second) + second["max_tokens"] <= 6200
    user = second["messages"][-1]["content"]
    assert user.startswith("START ") and " END\n\n[Current time:" in user and "omitted" in user
    assert len(user) < len(first["messages"][-1]["content"])
    assert result.user_content == user                                    # what the conversation stores
    assert second["messages"][0] == first["messages"][0]                   # the system prompt is untouched
    assert sv.estimator.slot == 6200
    assert any(s["t"] == "note" and "did not fit" in s["text"] for s in result.steps)


async def test_a_second_overflow_ends_the_turn_with_a_clear_message(make_services):
    llm = FakeLLM(overflow_response(20_000, 16_384), overflow_response(17_000, 16_384))
    sv = await make_services(llm)
    result = await sv.agent.run_turn([], "x" * 2000)
    assert result.status == "error" and len(llm.requests) == 2
    assert "too long for the model's context window (17000 tokens; the model takes at most 16384)" in result.text
    assert "try increasing" not in result.text                            # advice meant for the server's owner


async def test_overflow_in_the_middle_of_a_turn_shortens_this_turns_tool_results(make_services, tmp_path):
    (tmp_path / "ws" / "big.txt").write_text("1234567890," * 2000)          # digits: about one token each
    llm = FakeLLM()
    sv = await make_services(llm, ctx_tokens=9000, max_output_tokens=500)
    dense = DenseTokenizer(sv, n_ctx=9000)
    llm.script = [call("read_file", {"path": "big.txt", "max_chars": 12000}), *[dense("The file holds digits.")] * 2]
    sv.estimator.calibrate = lambda *_: None                              # keep the fresh, too-low estimate
    history = [m for i in range(3) for m in exchange(i)]
    result = await sv.agent.run_turn(history, "What is in big.txt?")

    assert (result.status, result.text) == ("done", "The file holds digits.")
    assert len(llm.requests) == 3
    refused, retried = llm.requests[1], llm.requests[2]
    assert dense.size(refused) >= 9000 and dense.size(retried) + retried["max_tokens"] <= 9000
    assert retried["messages"][:-1] == refused["messages"][:-1]            # history and the call itself stay
    assert len(retried["messages"][-1]["content"]) < len(refused["messages"][-1]["content"])
    assert retried["messages"][-1]["role"] == "tool"


@pytest.mark.parametrize("last_reply", ["answer", "another tool call"])
async def test_tool_arguments_cannot_push_the_prompt_past_the_window(make_services, tmp_path, last_reply):
    """The model's own tool calls grow the prompt; history cannot be trimmed mid-turn, so the
    agent makes one final, smaller call and executes nothing more."""
    part = "The committee reviewed the quarterly figures and agreed on a common format. " * 30
    writes = [call("write_file", {"path": f"part{n}.md", "content": part}, call_id=f"c{n}") for n in range(1, 30)]
    llm = FakeLLM(*writes)
    sv = await make_services(llm, ctx_tokens=6000, max_output_tokens=600, max_steps=30)
    sv.estimator.calibrate = lambda *_: None                              # the fake server's counts are not real
    limit = 6000 + 600

    def final_reply(body):
        return say("Wrote the parts I could.") if last_reply == "answer" else call(
            "write_file", {"path": "late.md", "content": "x"}, call_id="late")

    def scripted(body):
        if body["messages"][-1]["content"].endswith(FINAL_ANSWER_NOTE):
            return final_reply(body)
        return writes.pop(0)

    llm.script = [scripted] * 30
    result = await sv.agent.run_turn([], "Write the report, one part per file.")

    for body in llm.requests:                                              # every request fits the window
        assert sv.estimator.prompt_tokens(body["messages"], body["tools"]) + body["max_tokens"] <= limit
    final = llm.requests[-1]
    assert final["messages"][-1]["content"].endswith(FINAL_ANSWER_NOTE)
    assert final["chat_template_kwargs"]["enable_thinking"] is False and final["max_tokens"] <= 600
    assert 3 <= len(llm.requests) < 30
    if last_reply == "answer":
        assert (result.status, result.text) == ("done", "Wrote the parts I could.")
    else:
        assert result.status == "context_full" and "context window" in result.text
        assert "write_file" in result.text and not (tmp_path / "ws" / "late.md").exists()
