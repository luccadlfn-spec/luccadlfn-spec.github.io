"""Routes under `/agent`: health check, the dashboard page and its JSON API.

Caddy exposes these (behind the gate cookie). Access needs the dashboard session
cookie or the bearer key. Cookie-authenticated requests that change something
must be sent as JSON, which a cross-site form cannot do (simple CSRF guard).
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .api_common import ApiError, client_id, is_json, read_json
from .auth import SESSION_COOKIE, SESSION_SECONDS
from .runner import QueueFull
from .schedules import ScheduleError
from .services import Services

SMALL_BODY = 64 * 1024
_PAGE = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def _page_headers(nonce: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'none'; connect-src 'self'; img-src data:; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }


def build_router(sv: Services) -> APIRouter:
    router = APIRouter(prefix="/agent")
    auth = sv.auth

    def authorize(request: Request, write: bool = False) -> str:
        """Returns 'bearer' or 'session'; raises 401/415 otherwise."""
        if auth.bearer_ok(request.headers.get("authorization")):
            return "bearer"
        if auth.session_ok(request.cookies.get(SESSION_COOKIE)):
            if write and not is_json(request):
                raise ApiError(415, "Content-Type must be application/json")
            return "session"
        raise ApiError(401, "not signed in")

    def schedule_view(sched: dict[str, Any]) -> dict[str, Any]:
        return {**sched, "next_run_text": sv.schedules.fmt(sched["next_run"]) if sched["enabled"] else "-"}

    # ---- public ----
    @router.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @router.get("", include_in_schema=False)
    async def no_slash() -> Response:
        return RedirectResponse("/agent/", status_code=308)

    @router.get("/", include_in_schema=False)
    async def dashboard() -> Response:
        nonce = secrets.token_urlsafe(16)
        return HTMLResponse(_PAGE.replace("__NONCE__", nonce), headers=_page_headers(nonce))

    @router.post("/api/login")
    async def login(request: Request) -> Response:
        if not auth.password_enabled:
            raise ApiError(403, "password login is disabled on this server")
        if not is_json(request):
            raise ApiError(415, "Content-Type must be application/json")
        who = client_id(request)
        wait = auth.retry_after(who)
        if wait:
            raise ApiError(429, f"too many failed attempts; try again in {wait} seconds",
                           headers={"Retry-After": str(wait)})
        body = await read_json(request, 4096)
        if not auth.password_ok(body.get("password")):
            auth.record_failure(who)
            raise ApiError(401, "wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, auth.new_session(), max_age=SESSION_SECONDS, path="/agent",
                        httponly=True, secure=True, samesite="strict")
        return resp

    @router.post("/api/logout")
    async def logout(request: Request) -> Response:
        if auth.session_ok(request.cookies.get(SESSION_COOKIE)):
            if not is_json(request):
                raise ApiError(415, "Content-Type must be application/json")
            auth.revoke_sessions()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/agent", httponly=True, secure=True, samesite="strict")
        return resp

    # ---- status ----
    @router.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        authorize(request)
        return await sv.status()

    # ---- tasks ----
    @router.get("/api/tasks")
    async def list_tasks(request: Request, limit: int = 20) -> dict[str, Any]:
        authorize(request)
        return {"tasks": sv.db.list_tasks(limit)}

    @router.post("/api/tasks")
    async def create_task(request: Request) -> dict[str, Any]:
        how = authorize(request, write=True)
        body = await read_json(request, SMALL_BODY)
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError(400, "'prompt' must be a non-empty string")
        try:
            task_id = sv.runner.submit("api" if how == "bearer" else "chat", prompt,
                                       background=body.get("background") is True)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        except QueueFull as exc:
            raise ApiError(429, f"the task queue is full ({exc})") from None
        return {"id": task_id}

    @router.get("/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: int) -> dict[str, Any]:
        authorize(request)
        task = sv.db.get_task(task_id)
        if task is None:
            raise ApiError(404, "no such task")
        return task

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: int) -> dict[str, Any]:
        authorize(request, write=True)
        outcome = sv.runner.cancel(task_id)
        if outcome == "not_found":
            raise ApiError(404, "no such task")
        return {"id": task_id, "result": outcome}

    # ---- schedules ----
    @router.get("/api/schedules")
    async def list_schedules(request: Request) -> dict[str, Any]:
        authorize(request)
        return {"schedules": [schedule_view(x) for x in sv.schedules.list()],
                "limit": sv.settings.max_schedules,
                "min_interval_minutes": sv.settings.min_schedule_interval_min}

    @router.post("/api/schedules")
    async def add_schedule(request: Request) -> dict[str, Any]:
        authorize(request, write=True)
        body = await read_json(request, SMALL_BODY)
        when, prompt, name = body.get("when"), body.get("prompt"), body.get("name")
        if not isinstance(when, str) or not isinstance(prompt, str) or not isinstance(name, (str, type(None))):
            raise ApiError(400, "'when' and 'prompt' must be strings")
        try:
            return schedule_view(sv.schedules.add(when, prompt, name))
        except ScheduleError as exc:
            raise ApiError(400, str(exc)) from None

    @router.patch("/api/schedules/{schedule_id}")
    async def patch_schedule(request: Request, schedule_id: int) -> dict[str, Any]:
        authorize(request, write=True)
        body = await read_json(request, 4096)
        if not isinstance(body.get("enabled"), bool):
            raise ApiError(400, "'enabled' must be true or false")
        sched = sv.schedules.set_enabled(schedule_id, body["enabled"])
        if sched is None:
            raise ApiError(404, "no such schedule")
        return schedule_view(sched)

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(request: Request, schedule_id: int) -> dict[str, bool]:
        authorize(request, write=True)
        if not sv.schedules.delete(schedule_id):
            raise ApiError(404, "no such schedule")
        return {"ok": True}

    # ---- notes ----
    @router.get("/api/notes")
    async def list_notes(request: Request, limit: int = 100) -> dict[str, Any]:
        authorize(request)
        return {"notes": sv.db.list_notes(limit)}

    @router.delete("/api/notes/{note_id}")
    async def delete_note(request: Request, note_id: int) -> dict[str, bool]:
        authorize(request, write=True)
        if not sv.db.delete_note(note_id):
            raise ApiError(404, "no such note")
        return {"ok": True}

    return router
