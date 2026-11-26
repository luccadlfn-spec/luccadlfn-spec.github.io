"""Token estimates and the history budget.

Rules (they keep the model server's prompt cache valid):
- history is trimmed only at the start of a turn, never while a turn is running;
- whole oldest exchanges are dropped (a user message and everything up to the next one);
- the system prompt and the newest user message are never dropped;
- tool results are shortened once, when they are inserted.
"""

from __future__ import annotations

import json
from typing import Any, Callable

DEFAULT_CHARS_PER_TOKEN = 3.3     # deliberately low: over-estimating tokens is the safe direction
MESSAGE_OVERHEAD_CHARS = 16       # role markers and separators added by the chat template


def clip_middle(text: str, limit: int) -> str:
    """Keep the head and the tail of `text`, marking what was cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = f"\n[... {len(text) - limit} characters omitted ...]\n"
    keep = max(0, limit - len(marker))
    head = keep * 2 // 3
    return text[:head] + marker + text[len(text) - (keep - head):] if keep else text[:limit]


def message_chars(message: dict[str, Any]) -> int:
    n = MESSAGE_OVERHEAD_CHARS + len(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        n += len(fn.get("name") or "") + len(fn.get("arguments") or "") + 24
    return n


class TokenEstimator:
    """Characters-per-token estimate, calibrated from the server's real prompt counts."""

    def __init__(self, load: Callable[[], Any] | None = None, save: Callable[[float], None] | None = None):
        self._save = save
        self.ratio = DEFAULT_CHARS_PER_TOKEN
        stored = load() if load else None
        if isinstance(stored, (int, float)) and 2.0 <= stored <= 5.0:
            self.ratio = float(stored)

    def tokens(self, chars: int) -> int:
        return int(chars / self.ratio) + 1

    def chars(self, tokens: int) -> int:
        return int(max(0, tokens) * self.ratio)

    def prompt_chars(self, messages: list[dict], tools: list[dict]) -> int:
        tool_chars = len(json.dumps(tools, separators=(",", ":"))) if tools else 0
        return tool_chars + sum(message_chars(m) for m in messages)

    def prompt_tokens(self, messages: list[dict], tools: list[dict]) -> int:
        return self.tokens(self.prompt_chars(messages, tools))

    def calibrate(self, sent_chars: int, real_tokens: int | None) -> None:
        # Tiny prompts are dominated by template overhead and would skew the ratio.
        if not real_tokens or real_tokens < 200 or sent_chars < 600:
            return
        observed = min(5.0, max(2.0, sent_chars / real_tokens))
        self.ratio = round(0.7 * self.ratio + 0.3 * observed, 3)
        if self._save:
            self._save(self.ratio)


def split_exchanges(history: list[dict]) -> list[list[dict]]:
    """Group messages into exchanges, each starting at a user message."""
    groups: list[list[dict]] = []
    for msg in history:
        if msg.get("role") == "user" or not groups:
            groups.append([])
        groups[-1].append(msg)
    return groups


def trim_history(history: list[dict], fixed_chars: int, budget_tokens: int,
                 est: TokenEstimator) -> list[dict]:
    """Drop whole oldest exchanges until `fixed_chars` plus the history fit the budget."""
    groups = split_exchanges(history)
    sizes = [sum(message_chars(m) for m in g) for g in groups]
    total = fixed_chars + sum(sizes)
    start = 0
    while start < len(groups) and est.tokens(total) > budget_tokens:
        total -= sizes[start]
        start += 1
    return [m for g in groups[start:] for m in g]
