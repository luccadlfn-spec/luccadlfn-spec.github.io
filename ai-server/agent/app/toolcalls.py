"""Tool calls that a model printed as text instead of returning them structured.

llama-server normally parses these itself; this is the safety net for the times
it does not (a slightly malformed call, a template without native tool support).
Two spellings are understood:

    <tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>

    <tool_call><function=read_file><parameter=path>a.txt</parameter></function></tool_call>
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

MARK = "<tool_call>"
_BLOCK = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.S)
_FUNCTION = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|\Z)", re.S)
_PARAMETER = re.compile(r"<parameter=([\w.\-]+)>\n?(.*?)\n?</parameter>", re.S)


def _first_json_object(text: str) -> Any:
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text[start:])
        return obj
    except ValueError:
        return None


def parse_text_calls(content: str) -> tuple[str, list[dict[str, str]], bool]:
    """Return (text before the first call, calls, whether a call marker was present).

    Each call is {"id", "name", "arguments"} with arguments as a JSON string, the
    same shape as a structured call, so both kinds take the same path afterwards.
    """
    idx = content.find(MARK)
    if idx < 0:
        return content, [], False
    calls: list[dict[str, str]] = []
    for block in _BLOCK.finditer(content[idx:]):
        body = block.group(1).strip()
        name: str | None = None
        args: Any = {}
        fn = _FUNCTION.search(body)
        if fn:
            name = fn.group(1)
            args = {k: v for k, v in _PARAMETER.findall(fn.group(2))}
        else:
            obj = _first_json_object(body)
            if isinstance(obj, dict) and isinstance(obj.get("name"), str):
                name = obj["name"]
                args = obj.get("arguments", obj.get("parameters", {}))
        if name:
            calls.append({"id": f"call_{len(calls) + 1}", "name": name,
                          "arguments": args if isinstance(args, str) else json.dumps(args)})
    return content[:idx].strip(), calls, True


def history_arguments(raw: str) -> str:
    """Arguments as they are written back into the conversation.

    Chat templates parse this string as JSON; a broken one could fail the next
    request, so it is replaced by an empty object (the model still reads the
    error in the tool result).
    """
    try:
        return raw if isinstance(json.loads(raw or "{}"), dict) else "{}"
    except ValueError:
        return "{}"


class ContentGate:
    """Passes answer text through as it streams, but stops at a text-form tool call.

    A tail that could be the beginning of the marker is held back until the next
    fragment decides it, so half a marker is never shown to the user.
    """

    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self._buf = ""
        self._closed = False
        self.emitted = False

    def _out(self, text: str) -> None:
        if text:
            self.emitted = True
            self._emit(text)

    def feed(self, text: str) -> None:
        if self._closed:
            return
        self._buf += text
        idx = self._buf.find(MARK)
        if idx >= 0:
            self._out(self._buf[:idx])
            self._buf, self._closed = "", True
            return
        hold = 0
        for k in range(min(len(MARK) - 1, len(self._buf)), 0, -1):
            if MARK.startswith(self._buf[-k:]):
                hold = k
                break
        cut = len(self._buf) - hold
        self._out(self._buf[:cut])
        self._buf = self._buf[cut:]

    def flush(self) -> None:
        if not self._closed:
            self._out(self._buf)
        self._buf = ""
