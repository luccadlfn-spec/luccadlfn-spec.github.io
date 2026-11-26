#!/usr/bin/env python3
"""Sandbox server: runs short Python or bash snippets for the agent.

Standard library only. The container around this process provides the real
isolation (no route to the internet, read-only root filesystem, no
capabilities, pid/memory/CPU limits). This file adds what the container
cannot: one run at a time, per-run resource limits, a timeout that kills the
whole process group, bounded output, a floor of free disk space, and, as
PID 1 of the container, clean-up between runs.

    GET  /healthz   -> 200 {"ok": true}                       (no auth)
    POST /exec      Authorization: Bearer <SANDBOX_TOKEN>
                    {"language": "python"|"bash", "code": "...", "timeout": 60}
                    -> 200 {"exit_code", "stdout", "stderr", "timed_out", "duration"}
                    401 bad or missing token, 400 bad input, 413 body over 1 MB,
                    403 when the request comes from inside the sandbox itself

`exit_code` is negative when the snippet was killed by a signal (-9 after a
timeout), as in Python's subprocess module. Lines in stderr that start with
"[sandbox]" are written by this server, not by the snippet.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import os
import select
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_BODY_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 300.0
OUTPUT_LIMIT = 20_000  # characters kept per stream, cut marker and [sandbox] notes included
_KEEP_CHARS = (OUTPUT_LIMIT - 100) // 2  # head and tail; 100 characters are reserved for the marker
_KEEP_BYTES = _KEEP_CHARS * 4  # a UTF-8 character is at most 4 bytes
MAX_PENDING = 8  # requests running or waiting for their turn; more are refused with 503
REQUEST_TIMEOUT = 30  # seconds a client may stall while sending a request
_DRAIN_BYTES = 8 * 1024 * 1024

# Seconds between checks while a snippet runs (disk space, orphans).
POLL = 0.25
POLL_AS_INIT = 0.05  # as PID 1, orphans that exit must be reaped quickly
POLL_FAST = 0.01  # for a moment after orphans were reaped: more are probably coming
DISK_ALLOWANCE = 32 * MIB  # what a run may still use up once the disk is below the floor
SCRATCH_KEEP = 32 * MIB  # /tmp and /dev/shm are emptied after a run when they hold more

SI_USER, SI_TKILL = 0, -6  # si_code of kill() and tgkill(); a process cannot forge these for another
_STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


HOST = os.environ.get("SANDBOX_HOST", "0.0.0.0")
PORT = _env_int("SANDBOX_PORT", 8070)
WORK_DIR = os.environ.get("SANDBOX_WORKDIR", "/workspace")
TMP_DIR = os.environ.get("SANDBOX_TMPDIR") or tempfile.gettempdir()
SHM_DIR = os.environ.get("SANDBOX_SHMDIR", "/dev/shm")
MAX_FILE_BYTES = _env_int("SANDBOX_MAX_FILE_MB", 100) * 1024 * 1024
MAX_OPEN_FILES = _env_int("SANDBOX_MAX_OPEN_FILES", 256)
# RLIMIT_NPROC counts every process and thread of this uid on the whole host,
# not only in this container (the agent container uses the same uid), so it
# cannot be tight. It sits below the container's pids limit (128) so that a
# fork bomb leaves room for this server to keep answering. 0 turns it off.
MAX_PROCESSES = _env_int("SANDBOX_MAX_PROCS", 96)
# Free space to keep on the workspace's disk. Default: 5% of it, at least 5 GB.
MIN_FREE_BYTES = _env_int("SANDBOX_MIN_FREE_MB", -1) * MIB
# Only for tests and development: accept /exec from this machine itself.
ALLOW_LOCAL_EXEC = os.environ.get("SANDBOX_ALLOW_LOCAL_EXEC", "") == "1"
# Holds the sitecustomize.py that every Python snippet loads (see that file).
STARTUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup")

# Runs inside the child before the snippet: apply the limits, then become the
# real interpreter. Done in a fresh interpreter instead of Popen(preexec_fn=)
# because running Python between fork and exec is unsafe in a threaded server.
# The server blocks SIGTERM and SIGINT (see main); the snippet must not inherit that.
_BOOTSTRAP = r"""
import os, resource, signal, sys
signal.pthread_sigmask(signal.SIG_SETMASK, ())
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


def _as_init() -> bool:
    """True in the container, where this server is PID 1: every orphan becomes
    our child, kill(-1) reaches exactly the other processes of the container,
    and /tmp and /dev/shm belong to the sandbox alone."""
    return os.getpid() == 1


def _size(n: float) -> str:
    return f"{n / GIB:.1f} GB" if n >= GIB else f"{n / MIB:.0f} MB"


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

    def text(self, limit: int = OUTPUT_LIMIT) -> str:
        keep = max(1, min(_KEEP_CHARS, (limit - 100) // 2))
        if self.total == len(self.head) + len(self.tail):  # nothing was dropped
            text = bytes(self.head + self.tail).decode("utf-8", "replace")
            if len(text) <= limit:
                return text
            first, last = text[:keep], text[-keep:]
        else:
            first = bytes(self.head).decode("utf-8", "replace")[:keep]
            last = bytes(self.tail[-_KEEP_BYTES:]).decode("utf-8", "replace")[-keep:]
        return f"{first}\n[... output truncated: {self.total} bytes in total, middle omitted ...]\n{last}"


def with_notes(output: OutputBuffer, before: list[str], after: list[str]) -> str:
    """The stream's text with the server's notes around it, within OUTPUT_LIMIT."""
    head = "".join(note + "\n" for note in before)
    tail = "".join(note + "\n" for note in after)
    text = output.text(OUTPUT_LIMIT - len(head) - len(tail) - 1)
    if tail and text and not text.endswith("\n"):
        text += "\n"
    return head + text + tail


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
        # Not the workspace itself: entries here come before the standard
        # library. startup/sitecustomize.py appends SANDBOX_WORKSPACE instead.
        "PYTHONPATH": STARTUP_DIR,
        "SANDBOX_WORKSPACE": WORK_DIR,
        # The per-user site directory would be under HOME (/tmp), where a
        # snippet could leave a .pth or usercustomize.py for later runs.
        "PYTHONNOUSERSITE": "1",
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


def _prepare_workdir() -> tuple[str, list[str]]:
    """The directory to run in, and notes for the model.

    The workspace is a directory on the host that snippets own and can damage
    (`chmod -R 644 .` removes its x bit, after which nothing could run in it,
    so the model could not repair it). Restore the owner's permissions when
    they are missing; when the workspace still cannot be entered, run in the
    temp directory and say so, rather than refusing to run at all.
    """
    notes: list[str] = []
    try:
        st = os.stat(WORK_DIR)
    except OSError as exc:
        st, problem = None, exc.strerror or "cannot be read"
    else:
        problem = None if stat.S_ISDIR(st.st_mode) else "it is not a directory"
    if st is not None and problem is None:
        wanted = os.R_OK | os.W_OK | os.X_OK
        if not os.access(WORK_DIR, wanted) and st.st_uid == os.geteuid():
            try:
                os.chmod(WORK_DIR, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
                notes.append(f"[sandbox] The workspace {WORK_DIR} had lost its owner's read, write or "
                             "enter permission; it has been given back.")
            except OSError:
                pass
        if os.access(WORK_DIR, os.X_OK):
            if not os.access(WORK_DIR, wanted):
                notes.append(f"[sandbox] The sandbox cannot read or write files in the workspace {WORK_DIR}.")
            return WORK_DIR, notes
        problem = "permission denied"
    notes.append(f"[sandbox] The workspace {WORK_DIR} cannot be used ({problem}). This run works in "
                 f"{TMP_DIR} instead; files there are not kept.")
    return TMP_DIR, notes


def _disk_space(path: str) -> tuple[int, int] | None:
    """(bytes an unprivileged user may still write, size of the filesystem)."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


class DiskGuard:
    """Keeps snippets from filling the server's disk through the workspace (a
    bind mount on the host's disk, which Open WebUI, the agent, Docker and
    the certificates need too). Only total free space is checked, about four
    times a second: a run is stopped once free space is below the floor and
    the run has used up more than DISK_ALLOWANCE of it. A run that starts
    below the floor still runs, with a warning, so that it can delete files."""

    def __init__(self, path: str) -> None:
        self.path = path
        space = _disk_space(path)
        self.enabled = space is not None
        self.start_free, size = space or (0, 0)
        self.floor = MIN_FREE_BYTES if MIN_FREE_BYTES >= 0 else max(5 * GIB, size // 20)
        self.free = self.start_free
        self.next_check = 0.0
        self.tripped = False

    def _state(self) -> str:
        return (f"the server's disk is almost full: {_size(self.free)} free, and the sandbox keeps "
                f"at least {_size(self.floor)} free")

    def warning(self) -> str | None:
        if self.enabled and self.start_free < self.floor:
            return (f"[sandbox] Warning: {self._state()}. Delete files in the workspace that are no "
                    f"longer needed; this run is stopped if it writes more than {_size(DISK_ALLOWANCE)}.")
        return None

    def exceeded(self, now: float) -> bool:
        if not self.enabled or now < self.next_check:
            return False
        self.next_check = now + POLL
        space = _disk_space(self.path)
        if space is None:
            return False
        self.free = space[0]
        self.tripped = self.free < self.floor and self.free < self.start_free - DISK_ALLOWANCE
        return self.tripped

    def stop_note(self) -> str:
        return (f"[sandbox] Stopped: {self._state()}. Delete files in the workspace that are no longer "
                "needed, then try again.")


def _cgroup_value(paths: tuple[str, ...], key: str | None = None) -> int | None:
    """A number from the container's cgroup files (v2 first, then v1)."""
    for path in paths:
        try:
            with open(path, encoding="ascii") as handle:
                text = handle.read()
        except (OSError, ValueError):
            continue
        if key is None:
            raw = text.strip()
            return int(raw) if raw.isdigit() else None
        for line in text.splitlines():
            name, _, value = line.partition(" ")
            if name == key and value.strip().isdigit():
                return int(value)
        return None
    return None


def _oom_kills() -> int | None:
    """How many processes the out-of-memory killer has stopped in this container."""
    return _cgroup_value(("/sys/fs/cgroup/memory.events", "/sys/fs/cgroup/memory/memory.oom_control"), "oom_kill")


def _memory_note(oom_before: int | None, oom_after: int | None, killed_unexplained: bool) -> str | None:
    """Explain a death by memory exhaustion, which otherwise shows up as a bare -9."""
    if oom_before is not None and oom_after is not None:
        certain = oom_after > oom_before
        if not certain:
            return None
    elif killed_unexplained:
        certain = False
    else:
        return None
    limit = _cgroup_value(("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    memory = "its memory" if limit is None or limit >= 1 << 50 else f"its {_size(limit)} of memory"
    what = ("A process of this run was stopped because the sandbox ran out of"
            if certain else "The program was killed (signal 9) before its time limit, most likely because "
            "the sandbox ran out of")
    return f"[sandbox] {what} {memory}. Files in {TMP_DIR} and {SHM_DIR} count against it too."


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


def _reap_others(keep: int) -> bool:
    """As PID 1, every orphan in the container becomes our child, and each one
    that has exited stays a zombie, counting against the process limits, until
    it is reaped. Reap those now, while the snippet (`keep`) is still running;
    the snippet itself is left for proc.wait(). True if anything was reaped."""
    reaped = False
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return reaped
        if info is None or info.si_pid == keep:
            return reaped
        try:
            os.waitpid(info.si_pid, os.WNOHANG)
        except ChildProcessError:
            return reaped
        reaped = True


def _strays_alive() -> bool:
    """True if a process other than PID 1 is still alive and was not started by
    Docker. Health checks and `docker exec` come from outside the container, so
    their parent shows as 0; everything a snippet started has a parent here."""
    for name in os.listdir("/proc"):
        if not name.isdigit() or name == "1":
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] not in (b"Z", b"X") and fields[1] != b"0":
            return True
    return False


def _reap_children() -> None:
    """Reap every child that has exited, without waiting."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _kill_strays(group: int) -> None:
    """Kill whatever a snippet left behind outside its process group (anything
    that called setsid), then reap. Only as PID 1, which is the case in the
    container: there kill(-1) reaches exactly the other processes of this
    container and never PID 1 itself. Anywhere else it would hit unrelated
    processes. kill(-1) is used only when such a process exists, because it
    would also end a health check that happens to be running."""
    if not _as_init():
        return
    # The snippet's group has just been sent SIGKILL, but on a busy CPU its
    # processes take a moment to be scheduled and die. Until the group is
    # gone they would look like strays; a process that joins the dying group
    # to hide keeps it alive and is then found below.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        _reap_children()
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.01)
    try:
        strays = _strays_alive()
    except OSError:
        strays = True
    if strays:
        try:
            os.kill(-1, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:  # orphans are re-parented to us; reap them
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.01)


def _empty_dir(top: str) -> None:
    """Delete everything below `top`, not `top` itself. Snippets own these
    files and may have taken away their own permissions, so each directory is
    opened up before it is read. Iterative: a snippet can nest directories
    deeper than Python's recursion limit."""
    stack = [(top, False)]
    while stack:
        path, emptied = stack.pop()
        if emptied:
            if path != top:
                try:
                    os.rmdir(path)
                except OSError:
                    pass
            continue
        stack.append((path, True))
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    os.chmod(entry.path, stat.S_IRWXU)
                    stack.append((entry.path, False))
                else:
                    os.unlink(entry.path)
            except OSError:
                pass


def _clean_scratch() -> str | None:
    """As PID 1, after a run: /tmp and /dev/shm are memory (tmpfs) and count
    against the container's memory limit until someone deletes the files. When
    one holds more than SCRATCH_KEEP, empty it; small caches (matplotlib's font
    list) are kept."""
    if not _as_init():
        return None
    emptied = []
    for path in (TMP_DIR, SHM_DIR):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        if used > SCRATCH_KEEP:
            _empty_dir(path)
            emptied.append(f"{path} ({_size(used)})")
    if not emptied:
        return None
    return (f"[sandbox] Emptied {' and '.join(emptied)} after this run: files there take up the "
            "sandbox's memory. Keep files you need in the workspace.")


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
    as_init = _as_init()
    cwd, before = _prepare_workdir()
    guard = DiskGuard(WORK_DIR)
    warning = guard.warning()
    if warning:
        before.append(warning)
    after: list[str] = []
    path, directory, pass_fds = _write_script(language, code)
    interpreter = sys.executable if language == "python" else (shutil.which("bash") or "/bin/bash")
    cpu_seconds = math.ceil(timeout) + 5  # backstop only; the wall-clock timeout comes first
    argv = [
        sys.executable, "-I", "-S", "-c", _BOOTSTRAP,
        str(cpu_seconds), str(MAX_FILE_BYTES), str(MAX_OPEN_FILES), str(MAX_PROCESSES),
        interpreter, path,
    ]  # fmt: skip
    stdout, stderr = OutputBuffer(), OutputBuffer()
    timed_out = client_left = False
    selector = selectors.DefaultSelector()
    pidfd = None
    oom_before = _oom_kills()
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
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
        fast_until = 0.0
        while not _has_exited(proc.pid):
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                break
            if guard.exceeded(now):
                _log(f"disk below the floor ({_size(guard.free)} free); stopping the run")
                break
            if as_init and _reap_others(proc.pid):
                fast_until = now + 0.5
            if now < fast_until:
                wait = POLL_FAST
            elif as_init or not exit_watch:
                wait = POLL_AS_INIT
            else:
                wait = POLL
            if not pump(min(remaining, wait)):
                client_left = True
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
        _kill_strays(proc.pid)
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
    exit_code = proc.returncode if proc.returncode is not None else -signal.SIGKILL
    if guard.tripped:
        after.append(guard.stop_note())
    note = _memory_note(oom_before, _oom_kills(),
                        exit_code == -signal.SIGKILL and not (timed_out or client_left or guard.tripped))
    if note:
        after.append(note)
    note = _clean_scratch()
    if note:
        after.append(note)
    return {
        "exit_code": exit_code,
        "stdout": stdout.text(),
        "stderr": with_notes(stderr, before, after),
        "timed_out": timed_out,
        "duration": round(duration, 3),
    }


def _same_host(peer: str, local: str) -> bool:
    """True if a connection was made from this machine (or container) itself."""
    try:
        addresses = [ipaddress.ip_address(peer.partition("%")[0]), ipaddress.ip_address(local.partition("%")[0])]
    except ValueError:
        return True
    peer_ip, local_ip = (getattr(ip, "ipv4_mapped", None) or ip for ip in addresses)
    return peer_ip.is_loopback or peer_ip == local_ip


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

    def _from_inside(self) -> bool:
        """The only legitimate caller is the agent, in another container. A
        request from this container itself comes from a snippet, which may
        have found the token (Docker gives it to every process it starts in
        the container, health checks included)."""
        if ALLOW_LOCAL_EXEC:
            return False
        try:
            local = self.connection.getsockname()[0]
        except OSError:
            return True
        return _same_host(self.client_address[0], local)

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
        if self._from_inside():
            return self._reject(403, "requests from inside the sandbox are refused", length)
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


def stop_requested(info, as_init: bool) -> bool:
    """Whether a SIGTERM or SIGINT should stop the server.

    As PID 1 of the container, the kernel drops signals that have no handler
    when they come from inside the container, so snippets cannot kill this
    server. A handler would undo that: snippets run as the same user and could
    send SIGTERM. So only a signal from outside counts: the kernel reports
    sender 0 for those (Docker's stop). The si_code check matters because
    rt_sigqueueinfo() lets a process choose si_pid, but not si_code SI_USER or
    SI_TKILL, when it signals another process."""
    return not as_init or (info.si_pid == 0 and info.si_code in (SI_USER, SI_TKILL))


def main() -> int:
    token = os.environ.pop("SANDBOX_TOKEN", "").strip()
    _hide_from_children()
    if not token:
        _log("warning: SANDBOX_TOKEN is empty; every /exec request will be refused")
    if not os.path.isdir(WORK_DIR):
        _log(f"warning: workspace {WORK_DIR} does not exist; runs will use {TMP_DIR} until it does")
    server = Server((HOST, PORT), token)
    # Block the stop signals before any thread starts (threads inherit the
    # mask) and take them with sigtimedwait, which tells who sent them. Docker
    # waits out its grace period if they are simply ignored.
    signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    worker = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    worker.start()
    as_init = _as_init()
    _log(f"listening on {HOST}:{server.server_address[1]}, workspace {WORK_DIR}")
    status = 1  # the HTTP thread died
    try:
        while worker.is_alive():
            info = signal.sigtimedwait(_STOP_SIGNALS, 1.0)
            if info is None:
                continue
            if stop_requested(info, as_init):
                status = 0
                break
            _log(f"ignored signal {info.si_signo} from process {info.si_pid} inside the container")
    finally:
        if _active is not None:
            _kill_group(_active)
        server.shutdown()
        server.server_close()
    return status


if __name__ == "__main__":
    sys.exit(main())
