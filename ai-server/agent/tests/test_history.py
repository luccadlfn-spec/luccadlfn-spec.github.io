"""History budget: what may be trimmed, and when."""

from __future__ import annotations

from app.history import TokenEstimator, clip_middle, split_exchanges, trim_history
from app.prompts import time_line, with_time
from helpers import FakeLLM, call, say
from datetime import datetime, timezone


def exchange(i: int, size: int = 400) -> list[dict]:
    return [{"role": "user", "content": f"question {i} " + "q" * size},
            {"role": "assistant", "content": f"answer {i} " + "a" * size}]


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
    kept = trim_history(history, fixed_chars=1000, budget_tokens=1200, est=est)
    assert 0 < len(kept) < len(history) and len(kept) % 2 == 0
    assert kept == history[-len(kept):]                 # a suffix: only the oldest went
    assert kept[0]["role"] == "user"                    # never starts in the middle of an exchange
    assert est.tokens(1000 + sum(len(m["content"]) + 16 for m in kept)) <= 1200
    assert trim_history(history, 1000, 10_000_000, est) == history
    assert trim_history(history, 1000, 10, est) == []   # nothing fits: history goes, caller keeps the rest


def test_estimator_calibrates_within_bounds_and_persists():
    saved: list[float] = []
    est = TokenEstimator(load=lambda: 3.0, save=saved.append)
    assert est.ratio == 3.0
    est.calibrate(sent_chars=4000, real_tokens=1000)     # observed 4.0 chars/token
    assert 3.0 < est.ratio < 4.0 and saved == [est.ratio]
    est.calibrate(sent_chars=100, real_tokens=10)        # too small to trust
    est.calibrate(sent_chars=4000, real_tokens=None)
    assert saved == [est.ratio]
    for _ in range(50):
        est.calibrate(sent_chars=100_000, real_tokens=1000)   # absurd reading is clamped
    assert est.ratio <= 5.0
    assert TokenEstimator(load=lambda: "garbage").ratio == 3.3


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
    # ...and the prompt stays inside the budget.
    assert sv.estimator.prompt_tokens(llm.requests[-1]["messages"], llm.requests[-1]["tools"]) <= 6000


async def test_oversized_user_message_is_shortened_not_dropped(make_services):
    llm = FakeLLM(say("ok"))
    sv = await make_services(llm, ctx_tokens=3000)
    await sv.agent.run_turn([], "START " + "w" * 200_000 + " END")
    user = llm.requests[0]["messages"][-1]["content"]
    assert user.startswith("START ") and " END\n\n[Current time:" in user and "omitted" in user
    assert sv.estimator.prompt_tokens(llm.requests[0]["messages"], llm.requests[0]["tools"]) <= 3000
