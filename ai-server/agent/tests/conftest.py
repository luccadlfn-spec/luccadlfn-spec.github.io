"""Shared fixtures. Nothing here touches the network: every HTTP client gets a mock transport."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from typing import Any, Callable

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.services import Services  # noqa: E402
from helpers import API_KEY, PASSWORD, FakeLLM  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    (tmp_path / "ws").mkdir()
    return Settings(data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"),
                    llm_base_url="http://llm.test/v1", llm_api_key="llm-secret-key",
                    agent_api_key=API_KEY, dashboard_password=PASSWORD,
                    searxng_url="http://searxng.test", sandbox_url="http://sandbox.test",
                    sandbox_token="sandbox-secret", llm_retry_seconds=60.0)


@pytest.fixture
async def make_services(settings):
    """Factory: make_services(fake_llm, **settings_overrides, **Services kwargs) -> started Services."""
    made: list[Services] = []

    async def factory(llm: FakeLLM | None = None, *, services_kwargs: dict | None = None,
                      **overrides: Any) -> Services:
        cfg = replace(settings, **overrides) if overrides else settings
        sv = Services(cfg, llm_transport=(llm or FakeLLM()).transport, **(services_kwargs or {}))
        sv.llm._sleep = _no_sleep
        await sv.start(background=False)
        made.append(sv)
        return sv

    yield factory
    for sv in made:
        await sv.stop()


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def client_for() -> Callable[[Services], httpx.AsyncClient]:
    """An HTTP client wired straight into the ASGI app (https so Secure cookies are kept)."""

    def make(sv: Services, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(sv)),
                                 base_url="https://agent.test", **kwargs)

    return make

