#!/usr/bin/env python3
"""Sandbox server: runs short Python or bash snippets for the agent.

Standard library only. The container around this process provides the real
isolation (no route to the internet, read-only root filesystem, no
capabilities, pid/memory/CPU limits). This file adds what the container
cannot: one run at a time, per-run resource limits, a timeout that kills the
whole process group, and bounded output.

    GET  /healthz   -> 200 {"ok": true}                       (no auth)
    POST /exec      Authorization: Bearer <SANDBOX_TOKEN>
                    {"language": "python"|"bash", "code": "...", "timeout": 60}
                    -> 200 {"exit_code", "stdout", "stderr", "timed_out", "duration"}
                    401 bad or missing token, 400 bad input, 413 body over 1 MB

`exit_code` is negative when the snippet was killed by a signal (-9 after a
timeout), as in Python's subprocess module.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import select
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 300.0
OUTPUT_LIMIT = 20_000  # characters kept per stream, cut marker included
_KEEP_CHARS = (OUTPUT_LIMIT - 100) // 2  # head and tail; 100 characters are reserved for the marker
_KEEP_BYTES = _KEEP_CHARS * 4  # a UTF-8 character is at most 4 bytes
MAX_PENDING = 8  # requests running or waiting for their turn; more are refused with 503
REQUEST_TIMEOUT = 30  # seconds a client may stall while sending a request
_DRAIN_BYTES = 8 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


HOST = os.environ.get("SANDBOX_HOST", "0.0.0.0")
PORT = _env_int("SANDBOX_PORT", 8070)
WORK_DIR = os.environ.get("SANDBOX_WORKDIR", "/workspace")
TMP_DIR = os.environ.get("SANDBOX_TMPDIR") or tempfile.gettempdir()
MAX_FILE_BYTES = _env_int("SANDBOX_MAX_FILE_MB", 100) * 1024 * 1024
MAX_OPEN_FILES = _env_int("SANDBOX_MAX_OPEN_FILES", 256)
# RLIMIT_NPROC counts every process and thread of this uid on the whole host,
# not only in this container (the agent container uses the same uid), so it
# cannot be tight. It sits below the container's pids limit (128) so that a
# fork bomb leaves room for this server to keep answering. 0 turns it off.
MAX_PROCESSES = _env_int("SANDBOX_MAX_PROCS", 96)

# Runs inside the child before the snippet: apply the limits, then become the
# real interpreter. Done in a fresh interpreter instead of Popen(preexec_fn=)
# because running Python between fork and exec is unsafe in a threaded server.
_BOOTSTRAP = r"""
import os, resource, sys
cpu, fsize, nofile, nproc = (int(x) for x in sys.argv[1:5])
def cap(kind, soft, hard):
    current_hard = resource.getrlimit(kind)[1]
    if current_hard != resource.RLIM_INFINITY:
        hard = min(hard, current_hard)  # raising a hard limit needs privileges we do not have
    resource.setrlimit(kind, (min(soft, hard), hard))
cap(resource.RLIMIT_CORE, 0, 0)
cap(resource.RLIMIT_CPU, cpu, cpu + 5)
cap(resource.RLIMIT_FSIZE, fsize, fsize)
cap(resource.RLIMIT_NOFILE, nofile, nofile)
if nproc > 0:
    cap(resource.RLIMIT_NPROC, nproc, nproc)
os.execv(sys.argv[5], sys.argv[5:])
"""

_active: subprocess.Popen | None = None  # the running snippet, for shutdown


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", file=sys.stderr, flush=True)


class OutputBuffer:
    """Keeps the first and the last part of a stream, so memory stays bounded
    however much a snippet prints."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, data: bytes) -> None:
        self.total += len(data)
        room = _KEEP_BYTES - len(self.head)
        if room > 0:
            self.head += data[:room]
            data = data[room:]
        if data:
            self.tail += data
            if len(self.tail) > 2 * _KEEP_BYTES:
                del self.tail[:-_KEEP_BYTES]

    def text(self) -> str:
        if self.total == len(self.head) + len(self.tail):  # nothing was dropped
            text = bytes(self.head + self.tail).decode("utf-8", "replace")
            if len(text) <= OUTPUT_LIMIT:
                return text
            first, last = text[:_KEEP_CHARS], text[-_KEEP_CHARS:]
        else:
            first = bytes(self.head).decode("utf-8", "replace")[:_KEEP_CHARS]
            last = bytes(self.tail[-_KEEP_BYTES:]).decode("utf-8", "replace")[-_KEEP_CHARS:]
        return f"{first}\n[... output truncated: {self.total} bytes in total, middle omitted ...]\n{last}"


def parse_request(body: bytes) -> tuple[str, str, float]:
    """Validate an /exec body. Raises ValueError with a message for the client."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("body must be UTF-8 encoded JSON") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    language = data.get("language")
    if language not in ("python", "bash"):
        raise ValueError('language must be "python" or "bash"')
    code = data.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    try:
        code.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("code must be valid Unicode text") from None
    timeout = data.get("timeout")
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a number of seconds")
    if isinstance(timeout, int):
        timeout = min(timeout, int(MAX_TIMEOUT))  # also keeps huge integers out of float()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    return language, code, min(float(timeout), MAX_TIMEOUT)


def _child_env() -> dict[str, str]:
    """A fixed, minimal environment. Nothing from the server (least of all the
    token) is passed on, except the time zone."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": TMP_DIR,
        "TMPDIR": TMP_DIR,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": WORK_DIR,  # modules saved in the workspace can be imported
        "PYTHONUNBUFFERED": "1",  # output printed before a timeout is not lost
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "MPLBACKEND": "Agg",
        # The container gets one CPU; more BLAS/OpenMP threads would only add
        # contention and count against the process limit.
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
    if os.environ.get("TZ"):
        env["TZ"] = os.environ["TZ"]
    return env


def _write_script(language: str, code: str) -> tuple[str, str | None, tuple[int, ...]]:
    """Put the code in a temp file. Returns (path, dir to delete, fds to pass on).

    If the temp directory cannot be written (typically because earlier snippets
    filled it), fall back to an in-memory file so the model can still run
    something to clean up.
    """
    data = code.encode("utf-8")
    name = "main.py" if language == "python" else "main.sh"
    directory = None
    try:
        directory = tempfile.mkdtemp(prefix="exec-", dir=TMP_DIR)
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path, directory, ()
    except OSError:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
    fd = os.memfd_create(name)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        os.close(fd)
        raise
    return f"/proc/self/fd/{fd}", None, (fd,)


def _has_exited(pid: int) -> bool:
    """True once the child is dead, without reaping it: while it stays a zombie
    its pid, and so its process group id, cannot be reused by someone else."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the snippet's whole process group. The child leads its own
    session, so it cannot have left the group it leads."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_strays() -> None:
    """Kill whatever a snippet left behind outside its process group (anything
    that called setsid). Only as PID 1, which is the case in the container:
    there kill(-1) reaches exactly the other processes of this container and
    never PID 1 itself. Anywhere else it would hit unrelated processes."""
    if os.getpid() != 1:
        return
    try:
        os.kill(-1, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:  # orphans are re-parented to us; reap them
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.01)


def _peer_closed(sock: socket.socket) -> bool:
    """True if the client has closed its end (nothing is read from the socket)."""
    try:
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        if not poller.poll(0):
            return False
        return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def execute(language: str, code: str, timeout: float, client: socket.socket | None = None) -> dict:
    """Run one snippet to completion, timeout, or until the client hangs up."""
    global _active
    path, directory, pass_fds = _write_script(language, code)
    interpreter = sys.executable if language == "python" else (shutil.which("bash") or "/bin/bash")
    cpu_seconds = math.ceil(timeout) + 5  # backstop only; the wall-clock timeout comes first
    argv = [
        sys.executable, "-I", "-S", "-c", _BOOTSTRAP,
        str(cpu_seconds), str(MAX_FILE_BYTES), str(MAX_OPEN_FILES), str(MAX_PROCESSES),
        interpreter, path,
    ]  # fmt: skip
    stdout, stderr = OutputBuffer(), OutputBuffer()
    timed_out = False
    selector = selectors.DefaultSelector()
    pidfd = None
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORK_DIR,
            env=_child_env(),
            start_new_session=True,  # own session and process group: one signal reaches the whole tree
            pass_fds=pass_fds,
        )
    except BaseException:
        for fd in pass_fds:
            os.close(fd)
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    _active = proc
    try:
        for fd in pass_fds:
            os.close(fd)
        for pipe, buffer in ((proc.stdout, stdout), (proc.stderr, stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, buffer)
        if client is not None:
            selector.register(client, selectors.EVENT_READ, "client")
        exit_watch = False
        try:  # wakes the loop the moment the child exits; without it we poll
            pidfd = os.pidfd_open(proc.pid)
            selector.register(pidfd, selectors.EVENT_READ, "exit")
            exit_watch = True
        except (AttributeError, OSError):
            pass

        def pump(wait: float) -> bool:
            """Move available output into the buffers. False if the client left."""
            nonlocal exit_watch
            for key, _ in selector.select(wait):
                if isinstance(key.data, OutputBuffer):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if chunk:
                        key.data.add(chunk)
                    else:
                        selector.unregister(key.fileobj)
                elif key.data == "exit":
                    selector.unregister(key.fileobj)  # stays readable for good; watch it only once
                    exit_watch = False
                else:
                    selector.unregister(key.fileobj)
                    if _peer_closed(key.fileobj):
                        return False
            return True

        deadline = started + timeout
        while not _has_exited(proc.pid):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not pump(min(remaining, 1.0 if exit_watch else 0.05)):
                _log("client disconnected; stopping the run")
                break
        # Always: this is the timeout kill, and after a normal exit it removes
        # background jobs that would otherwise keep running and hold the pipes.
        _kill_group(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _log("child did not exit after SIGKILL")
        duration = time.monotonic() - started
        _kill_strays()
        # Collect what is still in the pipes. A short limit, in case a process
        # that escaped the group still holds them open.
        drain_until = time.monotonic() + 1.0
        while any(isinstance(key.data, OutputBuffer) for key in selector.get_map().values()):
            remaining = drain_until - time.monotonic()
            if remaining <= 0:
                break
            pump(remaining)
    finally:
        _active = None
        if proc.returncode is None:  # only reached when something above failed
            _kill_group(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        selector.close()
        if pidfd is not None:
            os.close(pidfd)
        proc.stdout.close()
        proc.stderr.close()
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
    return {
        "exit_code": proc.returncode if proc.returncode is not None else -signal.SIGKILL,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "timed_out": timed_out,
        "duration": round(duration, 3),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT

    def version_string(self) -> str:
        return "sandbox"  # no Python version in the Server header

    def log_request(self, code="-", size="-") -> None:
        pass  # /exec is logged once with its result; /healthz would only be noise

    def log_message(self, format: str, *args) -> None:
        _log("http " + (format % args))

    def _send(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.close_connection = True  # one request per connection keeps the server simple
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass  # the client is gone; nothing to tell it

    def _reject(self, status: int, message: str, length: int | None, headers: dict[str, str] | None = None) -> None:
        """Answer, then read and drop the unread request body. Closing a socket
        with unread data resets the connection, and the client could lose the
        answer."""
        self._send(status, {"error": message}, headers)
        remaining = _DRAIN_BYTES if length is None else min(length, _DRAIN_BYTES)
        deadline = time.monotonic() + (0.2 if length is None else 5.0)
        try:
            self.connection.settimeout(0.2 if length is None else 2.0)
            while remaining > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass

    def _content_length(self) -> int | None:
        raw = self.headers.get("Content-Length", "")
        if not raw.isascii() or not raw.isdigit():
            return None
        return int(raw) if len(raw) <= 15 else 10**15  # absurd lengths are simply "too large"

    def _authorized(self) -> bool:
        expected = self.server.token
        scheme, _, given = self.headers.get("Authorization", "").partition(" ")
        if not expected or scheme.lower() != "bearer":
            return False  # no token configured means nobody gets in
        return hmac.compare_digest(given.strip().encode("utf-8"), expected.encode("utf-8"))

    def do_GET(self) -> None:
        path = self.path.partition("?")[0]
        if path == "/healthz":
            self._send(200, {"ok": True})
        elif path == "/exec":
            self._send(405, {"error": "use POST"}, {"Allow": "POST"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = self._content_length()
        path = self.path.partition("?")[0]
        if path != "/exec":
            if path == "/healthz":
                return self._reject(405, "use GET", length, {"Allow": "GET"})
            return self._reject(404, "not found", length)
        if not self._authorized():
            return self._reject(401, "missing or wrong token", length, {"WWW-Authenticate": "Bearer"})
        if length is None:
            return self._reject(400, "a valid Content-Length header is required", None)
        if length > MAX_BODY_BYTES:
            return self._reject(413, f"request body is larger than {MAX_BODY_BYTES} bytes", length)
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True  # the client gave up half way
            return
        try:
            language, code, timeout = parse_request(body)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})

        server = self.server
        if not server.pending.acquire(blocking=False):
            return self._send(503, {"error": "too many requests are waiting; try again later"}, {"Retry-After": "5"})
        try:
            # One run at a time. While waiting, notice a caller that gave up,
            # so its code is not run for nobody.
            while not server.run_lock.acquire(timeout=0.25):
                if _peer_closed(self.connection):
                    self.close_connection = True
                    return
            try:
                result = execute(language, code, timeout, self.connection)
            finally:
                server.run_lock.release()
        except Exception as exc:  # answer instead of dropping the connection
            _log(f"exec failed: {type(exc).__name__}: {exc}")
            return self._send(500, {"error": f"sandbox failure: {type(exc).__name__}: {exc}"})
        finally:
            server.pending.release()
        _log(
            f"exec language={language} bytes={length} timeout={timeout:g} exit={result['exit_code']} "
            f"timed_out={result['timed_out']} duration={result['duration']}"
        )
        self._send(200, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], token: str) -> None:
        super().__init__(address, Handler)
        self.token = token
        self.run_lock = threading.Lock()
        self.pending = threading.BoundedSemaphore(MAX_PENDING)

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, OSError):  # timeouts and resets from clients are routine
            return
        _log(f"request from {client_address[0]} failed: {type(error).__name__}: {error}")


def _hide_from_children() -> None:
    """Snippets run under our own uid. Marking this process non-dumpable makes
    /proc/<pid>/environ, /proc/<pid>/mem and ptrace on it root-only, so a
    snippet cannot read the token out of the server or tamper with it."""
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except Exception:  # best effort: not available outside Linux
        pass


def _stop(signum, frame) -> None:
    raise SystemExit(0)


def main() -> int:
    token = os.environ.pop("SANDBOX_TOKEN", "").strip()
    _hide_from_children()
    if not token:
        _log("warning: SANDBOX_TOKEN is empty; every /exec request will be refused")
    if not os.path.isdir(WORK_DIR):
        _log(f"warning: workspace {WORK_DIR} does not exist; runs will fail until it does")
    server = Server((HOST, PORT), token)
    # As PID 1 in a container, signals without a handler are ignored; without
    # these, `docker stop` would wait out its grace period.
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _log(f"listening on {HOST}:{server.server_address[1]}, workspace {WORK_DIR}")
    try:
        server.serve_forever()
    finally:
        if _active is not None:
            _kill_group(_active)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
