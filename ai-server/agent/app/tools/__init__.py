"""The agent's tools.

Names, descriptions and parameters are sent to the model with every request, so
they are kept as short as they can be while staying unambiguous, and they are
built in a fixed order so the prompt prefix is byte-identical between calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..db import Database
from ..schedules import ScheduleError, ScheduleService
from .base import CallContext, Param, Tool, Toolbox, ToolError
from .code import Sandbox
from .files import Workspace
from .web import Fetcher, Searcher, fetch_url

__all__ = ["CallContext", "Toolbox", "ToolDeps", "build_toolbox"]


@dataclass
class ToolDeps:
    settings: Settings
    db: Database
    schedules: ScheduleService
    workspace: Workspace
    fetcher: Fetcher
    searcher: Searcher
    sandbox: Sandbox


def build_toolbox(d: ToolDeps) -> Toolbox:
    s = d.settings
    default_chars = s.tool_result_chars

    async def web_search(a: dict, _: CallContext) -> str:
        return await d.searcher.search(a["query"], a.get("max_results", 5))

    async def fetch(a: dict, _: CallContext) -> str:
        return await fetch_url(d.fetcher, a["url"], a.get("max_chars", default_chars))

    async def run_code(a: dict, _: CallContext) -> str:
        return await d.sandbox.run(a["language"], a["code"], a.get("timeout"))

    async def read_file(a: dict, _: CallContext) -> str:
        return d.workspace.read(a["path"], a.get("max_chars", default_chars))

    async def write_file(a: dict, _: CallContext) -> str:
        return d.workspace.write(a["path"], a["content"], a.get("append", False))

    async def list_files(a: dict, _: CallContext) -> str:
        return d.workspace.list(a.get("path", "."))

    async def remember(a: dict, _: CallContext) -> str:
        text = a["text"].strip()
        if not text:
            raise ToolError("the note is empty")
        return f"Saved note #{d.db.add_note(text)}."

    async def recall(a: dict, _: CallContext) -> str:
        notes = d.db.search_notes(a.get("query", ""), limit=5)
        if not notes:
            return "No matching notes."
        return "\n".join(f"#{n['id']}: {n['text'][:600]}" for n in notes)

    async def schedule_task(a: dict, ctx: CallContext) -> str:
        if ctx.scheduled:
            # A scheduled run reads the web unattended; it must not be able to plant new jobs.
            raise ToolError("scheduled tasks cannot create new schedules")
        try:
            sched = d.schedules.add(a["when"], a["prompt"], a.get("name"))
        except ScheduleError as exc:
            raise ToolError(str(exc)) from None
        return f"Scheduled #{sched['id']} '{sched['name']}'. Next run: {d.schedules.fmt(sched['next_run'])}."

    async def list_schedules(_a: dict, _: CallContext) -> str:
        items = d.schedules.list()
        if not items:
            return "No schedules."
        return "\n".join(f"{d.schedules.describe(x)} | {x['prompt'][:80]}" for x in items)

    async def cancel_schedule(a: dict, ctx: CallContext) -> str:
        if ctx.scheduled and a["id"] != ctx.schedule_id:
            # Same reason as above: a page read unattended must not remove the owner's other
            # jobs. Ending its own schedule ("stop once the parcel has arrived") is fine.
            raise ToolError("a scheduled task can only cancel its own schedule")
        if not d.schedules.delete(a["id"]):
            raise ToolError(f"there is no schedule #{a['id']}")
        return f"Schedule #{a['id']} cancelled."

    tools = [
        Tool("web_search", "Search the web.", web_search,
             {"query": Param("string", True), "max_results": Param("integer")}, timeout=30),
        Tool("fetch_url", "Get the readable text of a web page.", fetch,
             {"url": Param("string", True), "max_chars": Param("integer")}, timeout=40),
    ]
    if s.enable_run_code:
        tools.append(Tool(
            "run_code", "Run Python or bash in a sandbox that shares the workspace files. No internet.",
            run_code, {"language": Param("string", True, ("python", "bash")), "code": Param("string", True),
                       "timeout": Param("integer")},
            timeout=Sandbox.total_timeout(300)))
    tools += [
        Tool("read_file", "Read a text file from the workspace.", read_file,
             {"path": Param("string", True), "max_chars": Param("integer")}, timeout=15),
        Tool("write_file", "Write a text file in the workspace. append=true adds to the end.", write_file,
             {"path": Param("string", True), "content": Param("string", True), "append": Param("boolean")},
             timeout=15),
        Tool("list_files", "List a workspace directory.", list_files, {"path": Param("string")}, timeout=15),
        Tool("remember", "Save a note for later conversations.", remember, {"text": Param("string", True)},
             timeout=10),
        Tool("recall", "Search the saved notes.", recall, {"query": Param("string", True)}, timeout=10),
        Tool("schedule_task", "Run a prompt later or repeatedly.", schedule_task,
             {"when": Param("string", True, description="5-field cron, 'in 30m', 'in 2h', 'in 1d' "
                                                        "or 'YYYY-MM-DD HH:MM'"),
              "prompt": Param("string", True), "name": Param("string")}, timeout=10),
        Tool("list_schedules", "List scheduled tasks.", list_schedules, timeout=10),
        Tool("cancel_schedule", "Delete a scheduled task.", cancel_schedule, {"id": Param("integer", True)},
             timeout=10),
    ]
    return Toolbox(tools)
