"""run_code: send code to the sandbox service and report what happened."""

from __future__ import annotations

import httpx

from .base import ToolError

MAX_CODE_CHARS = 100_000
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
# The sandbox runs one program at a time; allow for waiting behind another one.
QUEUE_ALLOWANCE = 45.0


class Sandbox:
    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._url = f"{base_url.rstrip('/')}/exec"
        self._token = token
        self._http = httpx.AsyncClient(transport=transport, trust_env=False,
                                       limits=httpx.Limits(max_connections=2))

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def total_timeout(timeout: int | None) -> float:
        return max(1, min(MAX_TIMEOUT, timeout or DEFAULT_TIMEOUT)) + QUEUE_ALLOWANCE

    async def run(self, language: str, code: str, timeout: int | None) -> str:
        if not self._token:
            raise ToolError("code execution is not configured on this server (no sandbox token)")
        if not code.strip():
            raise ToolError("there is no code to run")
        if len(code) > MAX_CODE_CHARS:
            raise ToolError("the code is too long (limit 100,000 characters)")
        seconds = max(1, min(MAX_TIMEOUT, timeout or DEFAULT_TIMEOUT))
        try:
            resp = await self._http.post(
                self._url, json={"language": language, "code": code, "timeout": seconds},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(seconds + QUEUE_ALLOWANCE - 5, connect=5.0))
        except httpx.TimeoutException:
            raise ToolError("the sandbox did not answer in time") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"the sandbox is not reachable ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise ToolError(f"the sandbox refused the request (HTTP {resp.status_code})")
        try:
            data = resp.json()
            exit_code = data["exit_code"]
            stdout, stderr = str(data.get("stdout") or ""), str(data.get("stderr") or "")
        except (ValueError, KeyError, TypeError):
            raise ToolError("the sandbox sent an unreadable answer") from None
        head = f"exit code: {exit_code}"
        if data.get("timed_out"):
            head += f" (stopped after the {seconds} s time limit)"
        parts = [head]
        if stdout.strip():
            parts.append(f"stdout:\n{stdout.rstrip()}")
        if stderr.strip():
            parts.append(f"stderr:\n{stderr.rstrip()}")
        if len(parts) == 1:
            parts.append("(no output)")
        return "\n".join(parts)
