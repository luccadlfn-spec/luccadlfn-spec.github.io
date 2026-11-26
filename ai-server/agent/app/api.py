"""The FastAPI application: `/v1` for Open WebUI, `/agent` for the dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, api_dashboard, api_openai
from .api_common import ApiError, openai_error
from .config import Settings
from .services import Services


def create_app(services: Services | None = None) -> FastAPI:
    sv = services or Services(Settings.from_env())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await sv.start()
        try:
            yield
        finally:
            await sv.stop()

    # No interactive docs or schema endpoints: nothing extra to expose.
    app = FastAPI(title="myai agent", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = sv
    app.include_router(api_openai.build_router(sv))
    app.include_router(api_dashboard.build_router(sv))

    app.add_middleware(_NoStore)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        if request.url.path.startswith("/v1"):
            kind = "authentication_error" if exc.status == 401 else "invalid_request_error"
            resp = openai_error(exc.status, exc.message, kind)
        else:
            resp = JSONResponse({"error": exc.message}, status_code=exc.status)
        resp.headers.update(exc.headers or {})
        return resp

    @app.exception_handler(RequestValidationError)
    async def bad_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "invalid request parameters"}, status_code=400)

    return app


class _NoStore:
    """Adds no-store and nosniff to everything under /agent (task results are private).

    Plain ASGI on purpose: it only touches the response headers, so streaming and
    client-disconnect detection work exactly as without it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/agent"):
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
                message = {**message, "headers": list(message.get("headers", []))
                           + [h for h in extra if h[0] not in names]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
