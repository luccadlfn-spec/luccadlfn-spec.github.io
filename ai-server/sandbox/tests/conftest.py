"""Fixtures: start the real server.py on a free loopback port. No Docker needed."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "test-token-0123456789abcdef"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def process_alive(pid: int) -> bool:
    """False for missing processes and for zombies (dead, just not reaped yet)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def wait_until(condition, seconds: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return bool(condition())


class Sandbox:
    """One running server.py with its own workspace and temp directory."""

    def __init__(self, root: Path, **env: str) -> None:
        self.workdir = root / "workspace"
        self.tmpdir = root / "tmp"
        self.workdir.mkdir(parents=True)
        self.tmpdir.mkdir()
        self.log = root / "server.log"
        self.port = free_port()
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "SANDBOX_TOKEN": TOKEN,
            "SANDBOX_HOST": "127.0.0.1",
            "SANDBOX_PORT": str(self.port),
            "SANDBOX_WORKDIR": str(self.workdir),
            "SANDBOX_TMPDIR": str(self.tmpdir),
            # The process limit counts everything the test user runs on this
            # machine, so it is switched off here; the container test covers it.
            "SANDBOX_MAX_PROCS": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CANARY_FROM_SERVER_ENV": "must-not-leak",
        }
        self.env.update(env)
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Sandbox":
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen([sys.executable, str(SERVER_PATH)], env=self.env, stdout=log, stderr=log)

        def up() -> bool:
            try:
                return self.request("GET", "/healthz", timeout=1)[0] == 200
            except OSError:
                return False

        if not wait_until(up, 10):
            self.stop()
            raise RuntimeError("server did not start:\n" + self.log.read_text())
        return self

    def stop(self) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode

    def request(self, method: str, path: str, body=None, headers: dict | None = None, timeout: float = 60):
        """Returns (status, headers, decoded JSON or raw bytes)."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw
            return response.status, dict(response.getheaders()), payload
        finally:
            conn.close()

    def post_exec(self, body, token: str | None = TOKEN, timeout: float = 60, headers: dict | None = None):
        headers = dict(headers or {})
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.request("POST", "/exec", body=body, headers=headers, timeout=timeout)

    def run(self, language: str, code: str, timeout: float | None = None, http_timeout: float = 60) -> dict:
        body = {"language": language, "code": code}
        if timeout is not None:
            body["timeout"] = timeout
        status, _, payload = self.post_exec(body, timeout=http_timeout)
        assert status == 200, payload
        return payload

    def raw(self, data: bytes, read: bool = True, timeout: float = 10) -> bytes:
        """Send bytes on a plain socket; for requests http.client cannot produce."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.sendall(data)
            if not read:
                return b""
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)


@pytest.fixture(scope="session")
def sandbox(tmp_path_factory):
    box = Sandbox(tmp_path_factory.mktemp("sandbox")).start()
    yield box
    box.stop()


@pytest.fixture
def make_sandbox(tmp_path):
    """Factory for servers with a non-default configuration."""
    started: list[Sandbox] = []

    def make(**env: str) -> Sandbox:
        box = Sandbox(tmp_path / f"box{len(started)}", **env)
        started.append(box)
        return box.start()

    yield make
    for box in started:
        box.stop()


@pytest.fixture(scope="session")
def server_module():
    spec = importlib.util.spec_from_file_location("sandbox_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_server"] = module
    spec.loader.exec_module(module)
    return module
