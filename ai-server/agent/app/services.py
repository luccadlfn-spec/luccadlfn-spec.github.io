"""Wiring: builds every component once and runs the background loops."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from . import __version__
from .auth import Auth
from .config import Settings
from .db import Database
from .governor import KV_KEY, Governor
from .history import TokenEstimator
from .llm import LLMClient
from .loop import Agent
from .runner import TaskRunner
from .scheduler import Scheduler
from .schedules import ScheduleService
from .telegram import TelegramBot
from .tools import ToolDeps, build_toolbox
from .tools.code import Sandbox
from .tools.files import Workspace
from .tools.netguard import Resolver, system_resolver
from .tools.web import Fetcher, Searcher

log = logging.getLogger(__name__)


class Services:
    def __init__(self, settings: Settings, *,
                 llm_transport: httpx.AsyncBaseTransport | None = None,
                 telegram_transport: httpx.AsyncBaseTransport | None = None,
                 search_transport: httpx.AsyncBaseTransport | None = None,
                 sandbox_transport: httpx.AsyncBaseTransport | None = None,
                 fetch_transport: httpx.AsyncBaseTransport | None = None,
                 resolver: Resolver = system_resolver):
        s = self.settings = settings
        self.db = Database(s.db_path)
        self.llm = LLMClient(s, transport=llm_transport)
        self.estimator = TokenEstimator(load=lambda: self.db.kv_get("units_per_token"),
                                        save=lambda v: self.db.kv_set("units_per_token", v))
        self.schedules = ScheduleService(s, self.db)
        self.workspace = Workspace(s.workspace_dir)
        self.fetcher = Fetcher(resolver=resolver, transport=fetch_transport)
        self.searcher = Searcher(s.searxng_url, transport=search_transport)
        self.sandbox = Sandbox(s.sandbox_url, s.sandbox_token, transport=sandbox_transport)
        self.toolbox = build_toolbox(ToolDeps(s, self.db, self.schedules, self.workspace,
                                              self.fetcher, self.searcher, self.sandbox))
        self.agent = Agent(s, self.llm, self.toolbox, self.estimator)
        self.governor = Governor(s.cpu_governor, s.cpu_baseline, s.background_rest_ratio, s.proc_stat_path,
                                 load=lambda: self.db.kv_get(KV_KEY),
                                 save=lambda v: self.db.kv_set(KV_KEY, v))
        self.runner = TaskRunner(s, self.db, self.agent, self.governor)
        self.scheduler = Scheduler(s, self.db, self.schedules, self.runner)
        self.telegram = TelegramBot(s, self.db, self.runner, self.schedules, self.status,
                                    transport=telegram_transport)
        self.runner.on_finish(self.telegram.on_task_finished)
        self.auth = Auth(s, self.db)
        self._tasks: list[asyncio.Task] = []
        self.interrupted: list[dict[str, Any]] = []     # tasks the last stop or crash cut off
        self.started = False

    async def start(self, background: bool = True) -> None:
        """Recover from the previous run and start the loops (`background=False` for tests)."""
        if self.started:
            return
        self.started = True
        s = self.settings
        try:
            os.makedirs(s.workspace_dir, exist_ok=True)
        except OSError:
            log.warning("workspace directory %s is not available; file tools will fail", s.workspace_dir)
        self.interrupted = self.db.mark_interrupted()
        if self.interrupted:
            log.info("%d task(s) were interrupted by the restart", len(self.interrupted))
        for task in self.interrupted:
            if task["kind"] == "scheduled":
                task["next"] = self.schedules.after_interruption(task)
        self.db.prune_conversations()
        if not s.agent_api_key:
            log.warning("AGENT_API_KEY is empty: the /v1 API and bearer access are disabled")
        if not s.dashboard_password:
            log.warning("DASHBOARD_PASSWORD is empty: dashboard password login is disabled")
        if not background:
            return
        loops = {"worker": self.runner.run_forever(), "scheduler": self.scheduler.run(),
                 "governor": self.governor.run(), "telegram": self.telegram.run(),
                 "restart notices": self.telegram.report_interrupted(self.interrupted)}
        for name, coro in loops.items():
            self._tasks.append(asyncio.create_task(coro, name=name))
        log.info("agent service %s started (model %s at %s, tools: %s)", __version__, s.llm_model,
                 s.llm_base_url, ", ".join(self.toolbox.names))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.runner.stop_detached()
        self.governor.persist()
        for closer in (self.telegram.aclose, self.llm.aclose, self.fetcher.aclose,
                       self.searcher.aclose, self.sandbox.aclose):
            try:
                await closer()
            except Exception:
                log.exception("error while closing a client")
        self.db.close()
        self.started = False

    async def status(self) -> dict[str, Any]:
        s = self.settings
        return {
            "version": __version__,
            "model": await self.llm.health(),
            "queue": {**self.runner.queue_status(), "inline": self.runner.inline},
            "governor": self.governor.status(),
            "telegram": {"enabled": self.telegram.enabled, "owners": len(self.telegram.owners()),
                         "error": self.telegram.last_error},
            "settings": {"thinking": s.thinking, "max_steps": s.max_steps, "ctx_tokens": s.ctx_tokens,
                         "max_output_tokens": s.max_output_tokens, "run_code": s.enable_run_code,
                         "timezone": s.tz_name, "tools": self.toolbox.names,
                         "password_login": self.auth.password_enabled},
        }
