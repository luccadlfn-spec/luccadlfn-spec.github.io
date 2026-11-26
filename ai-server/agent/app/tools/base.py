"""Tool definitions, argument validation and dispatch.

A tool call never raises into the agent loop: every failure becomes an
"Error: ..." string the model can read and recover from.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 12_000      # the most a tool may return when the model asks for more than the default


class ToolError(Exception):
    """An expected failure whose message is safe and useful to show to the model."""


@dataclass(frozen=True)
class Param:
    type: str                              # string | integer | boolean
    required: bool = False
    enum: tuple[str, ...] | None = None
    description: str | None = None


@dataclass(frozen=True)
class CallContext:
    background: bool = False               # governed, low-priority run
    scheduled: bool = False                # started by the scheduler, nobody is watching


Handler = Callable[[dict[str, Any], CallContext], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Handler
    params: dict[str, Param] = field(default_factory=dict)
    timeout: float = 30.0

    def schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for name, p in self.params.items():
            props[name] = {"type": p.type}
            if p.enum:
                props[name]["enum"] = list(p.enum)
            if p.description:
                props[name]["description"] = p.description
        parameters: dict[str, Any] = {"type": "object", "properties": props}
        required = [n for n, p in self.params.items() if p.required]
        if required:
            parameters["required"] = required
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": parameters}}


def _coerce(name: str, param: Param, value: Any) -> Any:
    """Accept the value, or the unambiguous string form small models often send."""
    if param.type == "string":
        if isinstance(value, str):
            ok = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            ok = str(value)
        else:
            raise ToolError(f"argument '{name}' must be a string")
        if param.enum and ok not in param.enum:
            raise ToolError(f"argument '{name}' must be one of: {', '.join(param.enum)}")
        return ok
    if param.type == "integer":
        if isinstance(value, bool):
            raise ToolError(f"argument '{name}' must be a whole number")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise ToolError(f"argument '{name}' must be a whole number")
    if param.type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ToolError(f"argument '{name}' must be true or false")
    raise ToolError(f"argument '{name}' has an unsupported type")


def validate_args(tool: Tool, raw: Any) -> dict[str, Any]:
    """Parse and check arguments (a JSON string or an object) against the tool's parameters."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raw = {}
        else:
            try:
                raw = json.loads(text)
            except ValueError as exc:
                raise ToolError(f"arguments are not valid JSON ({exc}). Send one JSON object.") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolError("arguments must be a JSON object")
    out: dict[str, Any] = {}
    for name, param in tool.params.items():
        value = raw.get(name)
        if value is None:
            if param.required:
                raise ToolError(f"missing required argument '{name}'")
            continue
        out[name] = _coerce(name, param, value)
    return out


class Toolbox:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}
        # Built once: the schema text is part of the cached prompt prefix.
        self.schemas: list[dict[str, Any]] = [t.schema() for t in tools]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def call(self, name: str, raw_args: Any, ctx: CallContext) -> tuple[str, bool]:
        """Run one tool. Returns (text for the model, ok)."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name[:60]}'. Available tools: {', '.join(self._tools)}.", False
        try:
            args = validate_args(tool, raw_args)
            async with asyncio.timeout(tool.timeout):
                text = await tool.handler(args, ctx)
            return (text if text.strip() else "(no output)"), True
        except ToolError as exc:
            return f"Error: {exc}", False
        except TimeoutError:
            return f"Error: {name} timed out after {tool.timeout:.0f} s.", False
        except Exception as exc:  # a bug or an unexpected library error must not end the turn
            log.exception("tool %s failed", name)
            return f"Error: {name} failed unexpectedly ({type(exc).__name__}).", False
