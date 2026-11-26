"""Token estimates and the history budget.

Rules (they keep the model server's prompt cache valid):
- history is trimmed only at the start of a turn, never while a turn is running;
- whole oldest exchanges are dropped (a user message and everything up to the next one);
- the system prompt and the newest user message are never dropped;
- tool results are shortened once, when they are inserted.

How many tokens a character costs depends a lot on what it is. Measured on a
Qwen3 tokenizer: English prose about 0.2 tokens per character, digits 1 (numbers
are split into single digits), Chinese 0.55, Hindi 1.1, Telugu 1.75. One
characters-per-token figure cannot cover that, so text is first converted into
weighted "units" (one unit is roughly one character of ordinary English), and
only the units-per-token ratio is calibrated against the server's real counts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

DEFAULT_UNITS_PER_TOKEN = 3.0     # deliberately low: over-estimating tokens is the safe direction
MIN_UNITS_PER_TOKEN = 1.5         # calibration bounds; measured text lands between about 3 and 5.5
MAX_UNITS_PER_TOKEN = 4.5
MESSAGE_OVERHEAD_UNITS = 16       # role markers and separators added by the chat template

# Units per character. ASCII letters, spaces and anything not listed below count 1.
_DIGIT_UNITS = 4.5                # every digit is its own token
_PUNCT_UNITS = 1.5
_CODED_RUN_UNITS = 2.0            # letters inside long letter-and-digit runs (hashes, base64, IDs)
_OTHER_UNITS = 6.5                # non-ASCII characters not in the table below
_SCRIPT_UNITS = (                 # (first code point, last code point, units per character)
    (0x0080, 0x00BF, 3.5),        # Latin-1 punctuation and symbols, no-break space
    (0x00C0, 0x024F, 2.5),        # accented Latin letters
    (0x0370, 0x03FF, 3.8), (0x1F00, 0x1FFF, 3.8),                         # Greek
    (0x0400, 0x052F, 1.8),        # Cyrillic
    (0x0530, 0x058F, 4.5),        # Armenian
    (0x0590, 0x05FF, 1.8),        # Hebrew
    (0x0600, 0x06FF, 2.0), (0x0750, 0x077F, 2.0), (0x08A0, 0x08FF, 2.0),
    (0xFB50, 0xFDFF, 2.0), (0xFE70, 0xFEFF, 2.0),                         # Arabic script
    (0x0900, 0x097F, 4.5),        # Devanagari
    (0x0980, 0x09FF, 5.0), (0x0B80, 0x0BFF, 5.0), (0x0D00, 0x0D7F, 5.0),  # Bengali, Tamil, Malayalam
    (0x0E00, 0x0E7F, 2.2),        # Thai
    (0x0E80, 0x0FFF, 5.0),        # Lao, Tibetan
    (0x10A0, 0x10FF, 4.5),        # Georgian
    (0x1780, 0x17FF, 5.0),        # Khmer
    (0x1E00, 0x1EFF, 2.5),        # more accented Latin (Vietnamese)
    (0x2000, 0x206F, 2.0),        # dashes, curly quotes and other punctuation
    (0x2070, 0x2BFF, 4.0),        # symbols, arrows, mathematics, box drawing
    (0x2E80, 0x9FFF, 2.2),        # CJK ideographs, kana, CJK punctuation
    (0xAC00, 0xD7FF, 3.0),        # Hangul
    (0xF900, 0xFAFF, 2.2),        # CJK compatibility ideographs
    (0xFF00, 0xFFEF, 4.0),        # full-width forms
    (0x10000, 0x10FFFF, 4.8),     # emoji and the other supplementary planes
)


def _script_patterns() -> list[tuple[re.Pattern[str], float]]:
    by_weight: dict[float, list[str]] = {}
    for first, last, weight in _SCRIPT_UNITS:
        by_weight.setdefault(weight, []).append(f"\\U{first:08x}-\\U{last:08x}")
    return [(re.compile(f"[{''.join(ranges)}]+"), weight) for weight, ranges in by_weight.items()]


_SCRIPTS = _script_patterns()
_DIGITS = re.compile(r"[0-9]+")
_PUNCT = re.compile(r"[!-/:-@\[-`{-~]+")
_CODED = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
_NOT_LETTERS = re.compile(r"[^A-Za-z]+")


def text_units(text: str) -> float:
    """The weighted length of `text` (linear time; a few regular-expression passes)."""
    units = (len(text) + (_DIGIT_UNITS - 1) * sum(map(len, _DIGITS.findall(text)))
             + (_PUNCT_UNITS - 1) * sum(map(len, _PUNCT.findall(text))))
    for run in _CODED.findall(text):
        if _DIGITS.search(run):
            units += (_CODED_RUN_UNITS - 1) * len(_NOT_LETTERS.sub("", run))
    if text.isascii():
        return units
    other = len(text) - len(text.encode("ascii", "ignore"))
    for pattern, weight in _SCRIPTS:
        count = sum(map(len, pattern.findall(text)))
        units += (weight - 1) * count
        other -= count
    return units + (_OTHER_UNITS - 1) * other


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


def message_units(message: dict[str, Any]) -> float:
    n = MESSAGE_OVERHEAD_UNITS + text_units(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        n += text_units(fn.get("name") or "") + text_units(fn.get("arguments") or "") + 24
    return n


class TokenEstimator:
    """Token estimate from weighted units, calibrated from the server's real prompt counts."""

    def __init__(self, load: Callable[[], Any] | None = None, save: Callable[[float], None] | None = None):
        self._save = save
        self.ratio = DEFAULT_UNITS_PER_TOKEN
        self.slot: int | None = None       # the model server's context size, once it has told us
        stored = load() if load else None
        if isinstance(stored, (int, float)) and MIN_UNITS_PER_TOKEN <= stored <= MAX_UNITS_PER_TOKEN:
            self.ratio = float(stored)

    def tokens(self, units: float) -> int:
        return int(units / self.ratio) + 1

    def text_tokens(self, text: str) -> int:
        return self.tokens(text_units(text))

    def prompt_units(self, messages: list[dict], tools: list[dict]) -> float:
        tool_units = text_units(json.dumps(tools, separators=(",", ":"))) if tools else 0
        return tool_units + sum(message_units(m) for m in messages)

    def prompt_tokens(self, messages: list[dict], tools: list[dict]) -> int:
        return self.tokens(self.prompt_units(messages, tools))

    def clip(self, text: str, max_tokens: int) -> str:
        """Shorten `text` (head and tail kept) until it is estimated at `max_tokens` or fewer."""
        if max_tokens <= 0:
            return ""
        total = self.text_tokens(text)
        if total <= max_tokens:
            return text
        limit = int(len(text) * max_tokens / total)
        while limit > 0:
            out = clip_middle(text, limit)
            if self.text_tokens(out) <= max_tokens:     # the cut part may have been lighter than the rest
                return out
            limit = int(limit * 0.85)
        return ""

    def calibrate(self, sent_units: float, real_tokens: int | None) -> None:
        # Tiny prompts are dominated by template overhead and would skew the ratio.
        if not real_tokens or real_tokens < 200 or sent_units < 600:
            return
        observed = min(MAX_UNITS_PER_TOKEN, max(MIN_UNITS_PER_TOKEN, sent_units / real_tokens))
        self._set(0.7 * self.ratio + 0.3 * observed)

    def learn_overflow(self, sent_units: float, real_tokens: int | None, slot: int | None) -> None:
        """The server refused a prompt as too long. Believe its numbers at once, not gradually."""
        if real_tokens and real_tokens > 0 and sent_units > 0:
            self._set(min(self.ratio, 0.9 * sent_units / real_tokens))
        else:
            self._set(0.7 * self.ratio)                # no numbers: just be more careful
        if isinstance(slot, int) and slot > 0:
            self.slot = slot

    def _set(self, ratio: float) -> None:
        self.ratio = round(min(MAX_UNITS_PER_TOKEN, max(MIN_UNITS_PER_TOKEN, ratio)), 3)
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


def trim_history(history: list[dict], fixed_units: float, budget_tokens: int,
                 est: TokenEstimator) -> list[dict]:
    """Drop whole oldest exchanges until `fixed_units` plus the history fit the budget.

    Measured from the newest exchange back, so a long history costs only what is kept.
    """
    groups = split_exchanges(history)
    total, start = fixed_units, len(groups)
    while start > 0:
        size = sum(message_units(m) for m in groups[start - 1])
        if est.tokens(total + size) > budget_tokens:
            break
        total += size
        start -= 1
    return [m for g in groups[start:] for m in g]
