"""Fixtures: start the real server.py on a free loopback port. No Docker needed.

Besides the plain server there are two variants, for behaviour that only
shows up in the container:
- `make_unprivileged_sandbox`: the server runs as an ordinary user (uid 10001
  when the tests run as root), so file permissions apply to it.
- `make_init_sandbox`: the server is PID 1 of new PID and mount namespaces,
  with its own small tmpfs /tmp and /dev/shm, as uid 10001. Needs root and
  unshare; the tests that use it are skipped otherwise.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "test-token-0123456789abcdef"
SANDBOX_UID = 10001  # the image's user


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
            # The tests are clients on the same machine, which the server
            # refuses by default (in the container that would be a snippet).
            "SANDBOX_ALLOW_LOCAL_EXEC": "1",
            # How full this machine's disk is must not change the results;
            # the disk tests set their own floor.
            "SANDBOX_MIN_FREE_MB": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CANARY_FROM_SERVER_ENV": "must-not-leak",
        }
        self.env.update(env)
        self.proc: subprocess.Popen | None = None

    def command(self) -> list[str]:
        return [sys.executable, str(SERVER_PATH)]

    def popen_options(self) -> dict:
        return {}

    def start(self) -> "Sandbox":
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen(self.command(), env=self.env, stdout=log, stderr=log, **self.popen_options())

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


# --------------------------------------------------------------------------
# variants for behaviour that only shows up in the container
# --------------------------------------------------------------------------


def _traversable_root(prefix: str) -> Path:
    """A fresh directory every user can enter. pytest's own directories are
    private to the user running the tests, so a server running as another
    user could not reach a workspace inside them."""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    root.chmod(0o755)
    return root


def _readable_copy_of_the_server(root: Path) -> Path:
    """server.py (with startup/) copied where any user can read it: the
    checkout itself may be private (under /root, for example)."""
    app = root / "app"
    app.mkdir(mode=0o755)
    shutil.copy2(SERVER_PATH, app / "server.py")
    startup = SERVER_PATH.parent / "startup"
    if startup.is_dir():
        shutil.copytree(startup, app / "startup")
    for path in [app, *app.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return app / "server.py"


def _only_child(pid: int, seconds: float = 10.0) -> int | None:
    """The pid of the single child of `pid` (unshare --fork has exactly one)."""
    found: list[int] = []

    def look() -> bool:
        try:
            found[:] = [int(x) for x in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
        except (OSError, ValueError):
            return False
        return bool(found)

    return found[0] if wait_until(look, seconds) else None


class UnprivilegedSandbox(Sandbox):
    """The server as an ordinary user, so file permissions apply to it: uid
    10001 (the image's user) when the tests run as root, otherwise the user
    running the tests, who is not root anyway."""

    def __init__(self, root: Path, **env: str) -> None:
        super().__init__(root, **env)
        self.server_path = _readable_copy_of_the_server(root)
        if os.geteuid() == 0:
            for path in (self.workdir, self.tmpdir):
                os.chown(path, SANDBOX_UID, SANDBOX_UID)
                path.chmod(0o750)

    def command(self) -> list[str]:
        return [sys.executable, str(self.server_path)]

    def popen_options(self) -> dict:
        if os.geteuid() != 0:
            return {}
        return {"user": SANDBOX_UID, "group": SANDBOX_UID, "extra_groups": []}


_NAMESPACES: list[str | None] = []


def namespaces_unavailable() -> str | None:
    """None when the server can be run as PID 1 here, else why not."""
    if not _NAMESPACES:
        reason = None
        if os.geteuid() != 0:
            reason = "needs root (unshare and mount)"
        else:
            missing = [tool for tool in ("unshare", "setpriv", "nsenter", "mount") if shutil.which(tool) is None]
            if missing:
                reason = "needs " + ", ".join(missing)
            else:
                probe = subprocess.run(["unshare", "--pid", "--fork", "--mount-proc", "true"],
                                       capture_output=True, timeout=30)
                if probe.returncode != 0:
                    reason = "cannot create PID namespaces: " + probe.stderr.decode(errors="replace").strip()
        _NAMESPACES.append(reason)
    return _NAMESPACES[0]


class InitSandbox(Sandbox):
    """server.py as PID 1 of new PID and mount namespaces, as in the container:
    every orphan becomes its child, kill(-1) reaches only its own namespace,
    /tmp and /dev/shm are small tmpfs mounts of its own, and it runs as uid
    10001 without capabilities. The network is shared with the tests, which
    reach it on 127.0.0.1. Files in its tmpfs mounts are visible only inside;
    tests look at them through snippets."""

    TMP_MB, SHM_MB = 64, 64  # the container: /tmp 256 MB (compose), /dev/shm 64 MB (Docker's default)

    def __init__(self, root: Path, workspace_mb: int | None = None, **env: str) -> None:
        super().__init__(root, **env)
        self.shmdir = root / "shm"
        self.shmdir.mkdir()
        self.env["SANDBOX_SHMDIR"] = str(self.shmdir)
        self.workspace_mb = workspace_mb
        self.server_path = _readable_copy_of_the_server(root)
        os.chown(self.workdir, SANDBOX_UID, SANDBOX_UID)
        self.workdir.chmod(0o750)
        self.inner_pid: int | None = None

    def command(self) -> list[str]:
        q = shlex.quote
        mounts = [f"mount -t tmpfs -o size={self.TMP_MB}m,mode=1777 tmpfs {q(str(self.tmpdir))}",
                  f"mount -t tmpfs -o size={self.SHM_MB}m,mode=1777 tmpfs {q(str(self.shmdir))}"]
        if self.workspace_mb:
            mounts.append(f"mount -t tmpfs -o size={self.workspace_mb}m,mode=750,uid={SANDBOX_UID},"
                          f"gid={SANDBOX_UID} tmpfs {q(str(self.workdir))}")
        server = (f"exec setpriv --reuid={SANDBOX_UID} --regid={SANDBOX_UID} --clear-groups --no-new-privs "
                  f"--inh-caps=-all --bounding-set=-all {q(sys.executable)} {q(str(self.server_path))}")
        return ["unshare", "--pid", "--fork", "--mount-proc", "--propagation", "private",
                "sh", "-c", " && ".join(mounts + [server])]

    def start(self) -> "InitSandbox":
        super().start()
        self.inner_pid = _only_child(self.proc.pid)
        assert self.inner_pid, "could not find the server inside the namespace"
        return self

    def stop(self) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            inner = self.inner_pid or _only_child(self.proc.pid, 1)
            if inner:
                os.kill(inner, signal.SIGTERM)  # from outside the namespace, as `docker stop` does
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if inner:
                    os.kill(inner, signal.SIGKILL)
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode

    def exec_inside(self, argv: list[str]) -> subprocess.Popen:
        """Start a process in the server's namespaces the way `docker exec`
        and health checks are started: from outside (so inside its parent
        shows as 0) and as the container's user."""
        return subprocess.Popen(["nsenter", "-t", str(self.inner_pid), "-p", "-m", "--",
                                 "setpriv", f"--reuid={SANDBOX_UID}", f"--regid={SANDBOX_UID}", "--clear-groups",
                                 *argv], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _factory(cls, prefix: str):
    started: list[Sandbox] = []
    roots: list[Path] = []

    def make(*args, **env: str):
        root = _traversable_root(prefix)
        roots.append(root)
        box = cls(root, *args, **env)
        started.append(box)
        return box.start()

    def cleanup() -> None:
        for box in started:
            box.stop()
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    return make, cleanup


@pytest.fixture
def make_unprivileged_sandbox():
    make, cleanup = _factory(UnprivilegedSandbox, "sandbox-test-")
    yield make
    cleanup()


@pytest.fixture
def make_init_sandbox():
    """Factory: make(workspace_mb=None, **env). Skips when namespaces are not available."""
    reason = namespaces_unavailable()
    if reason:
        pytest.skip(reason)
    make, cleanup = _factory(InitSandbox, "sandbox-init-")
    yield make
    cleanup()
