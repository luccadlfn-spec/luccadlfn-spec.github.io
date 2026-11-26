"""Tests for what server.py does as PID 1 of the container.

The server runs as PID 1 of new PID and mount namespaces, as uid 10001, with
its own small tmpfs /tmp and /dev/shm (InitSandbox in conftest.py). That needs
root and unshare; without them these tests are skipped. They need no Docker.
"""

from __future__ import annotations

import os
import signal
import time

from conftest import SANDBOX_UID, wait_until

MIB = 1024 * 1024


def _tasks_of(uid: int) -> int:
    """Processes and threads of `uid` on this machine: what RLIMIT_NPROC counts."""
    count = 0
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                if os.stat(f"/proc/{name}").st_uid == uid:
                    count += len(os.listdir(f"/proc/{name}/task"))
            except OSError:
                pass
    return count


# rt_sigqueueinfo() lets a process choose the sender pid it reports: here 0,
# which is what a signal from outside the container (Docker) looks like.
FORGED_SIGTERM = r"""
import ctypes, os, signal, time
number = {"x86_64": 129, "aarch64": 138}[os.uname().machine]
info = (ctypes.c_int * 32)()  # siginfo_t, 128 bytes
info[0] = signal.SIGTERM      # si_signo
info[2] = -1                  # si_code: SI_QUEUE
info[4] = 0                   # si_pid, at offset 16 on 64-bit Linux
info[5] = os.getuid()         # si_uid
libc = ctypes.CDLL(None, use_errno=True)
print("sent", libc.syscall(number, 1, signal.SIGTERM, ctypes.byref(info)), ctypes.get_errno())
time.sleep(0.5)
"""


def test_snippets_cannot_stop_the_server_but_docker_can(make_init_sandbox):
    box = make_init_sandbox()
    result = box.run("bash", "for s in TERM INT HUP KILL STOP; do kill -$s 1; done; sleep 0.5; echo still-here")
    assert result["stdout"] == "still-here\n", result
    if os.uname().machine in ("x86_64", "aarch64"):
        result = box.run("python", FORGED_SIGTERM)
        assert result["stdout"] == "sent 0 0\n", result
    assert box.request("GET", "/healthz")[0] == 200
    assert box.proc.poll() is None
    assert "ignored signal 15" in box.log.read_text()
    started = time.monotonic()
    os.kill(box.inner_pid, signal.SIGTERM)  # what `docker stop` does, from outside
    assert box.proc.wait(timeout=10) == 0
    assert time.monotonic() - started < 5


def test_orphans_are_reaped_while_the_snippet_runs(make_init_sandbox):
    # Every `( sleep 0.01 & )` leaves an orphan, which the server inherits as
    # PID 1. Unreaped, the zombies count against the process limit until fork
    # fails, and the run hangs until its timeout.
    box = make_init_sandbox(SANDBOX_MAX_PROCS=str(_tasks_of(SANDBOX_UID) + 60))
    result = box.run("bash", "for i in $(seq 300); do ( sleep 0.01 & ); done; echo finished", timeout=60)
    assert result["timed_out"] is False, result
    assert (result["exit_code"], result["stdout"]) == (0, "finished\n"), result
    assert "fork" not in result["stderr"]
    assert result["duration"] < 40


ESCAPE = (
    "import os, time\n"
    "if os.fork() == 0:\n"
    "    os.setsid()  # leaves the snippet's process group and session\n"
    "    with open('escapee.tmp', 'w') as f:\n"
    "        f.write(str(os.getpid()))\n"
    "    os.rename('escapee.tmp', 'escapee.pid')\n"
    "    time.sleep(300)\n"
    "    os._exit(0)\n"
    "while not os.path.exists('escapee.pid'):\n"
    "    time.sleep(0.01)\n"
    "print('started')\n"
)


def test_docker_exec_processes_survive_runs_but_escapees_do_not(make_init_sandbox):
    box = make_init_sandbox()
    probe = box.exec_inside(["sleep", "60"])  # started like a health check or `docker exec`
    try:
        time.sleep(0.5)
        assert probe.poll() is None
        assert box.run("bash", "echo plain")["stdout"] == "plain\n"
        time.sleep(0.3)
        assert probe.poll() is None, "a run that left nothing behind killed a docker exec process"
        # Background jobs die with the snippet's process group, but busy ones
        # take a moment to be scheduled and die; they are not strays.
        result = box.run("bash", "for i in $(seq 80); do ( while :; do :; done ) & done; echo started")
        assert result["stdout"] == "started\n"
        time.sleep(0.3)
        assert probe.poll() is None, "background jobs that were still dying were taken for strays"
        assert box.run("python", ESCAPE)["stdout"] == "started\n"
        pid = int((box.workdir / "escapee.pid").read_text())
        assert box.run("bash", f"test -d /proc/{pid} && echo alive || echo gone")["stdout"] == "gone\n"
    finally:
        probe.kill()
        probe.wait()


def test_big_leftovers_in_tmp_and_shm_are_removed_small_ones_kept(make_init_sandbox):
    # Both are tmpfs: their files use the container's memory until deleted.
    box = make_init_sandbox()
    tmp, shm = box.tmpdir, box.shmdir
    code = (
        "head -c 40000000 /dev/zero > $TMPDIR/big.bin\n"
        "mkdir -p $TMPDIR/locked/in && touch $TMPDIR/locked/in/f && chmod 000 $TMPDIR/locked/in $TMPDIR/locked\n"
        f"head -c 1000000 /dev/zero > {shm}/small.bin\n"
        "echo ok\n"
    )
    result = box.run("bash", code)
    assert result["stdout"] == "ok\n", result
    assert f"[sandbox] Emptied {tmp} (" in result["stderr"]
    assert str(shm) not in result["stderr"]
    listing = box.run("bash", f"ls -A $TMPDIR | grep -v '^exec-'; echo ---; ls -A {shm}")["stdout"]
    assert listing == "---\nsmall.bin\n"
    assert box.run("bash", "echo kept > $TMPDIR/note.txt")["stderr"] == ""
    assert box.run("bash", "cat $TMPDIR/note.txt")["stdout"] == "kept\n"
    result = box.run("bash", f"head -c 40000000 /dev/zero > {shm}/big.bin; echo ok")
    assert result["stdout"] == "ok\n"
    assert f"[sandbox] Emptied {shm} (" in result["stderr"]
    assert box.run("bash", f"ls -A {shm}")["stdout"] == ""


def test_writing_stops_at_the_disk_floor_and_cleaning_up_works(make_init_sandbox):
    # A 64 MB workspace that must keep 24 MB free: deterministic, unlike the host disk.
    box = make_init_sandbox(workspace_mb=64, SANDBOX_MIN_FREE_MB="24")
    fill = (
        "import time\n"
        "with open('fill.bin', 'wb') as f:\n"
        "    for _ in range(15):\n"
        "        f.write(b'x' * 4_000_000); f.flush(); time.sleep(0.1)\n"
        "print('wrote everything')\n"
    )
    result = box.run("python", fill, timeout=60)
    assert result["exit_code"] == -signal.SIGKILL and result["timed_out"] is False, result
    assert "wrote everything" not in result["stdout"]
    assert "[sandbox] Stopped: the server's disk is almost full" in result["stderr"]
    size = int(box.run("bash", "stat -c %s fill.bin")["stdout"])
    assert 40 * MIB <= size < 60_000_000
    result = box.run("bash", "rm fill.bin && echo removed")  # starts below the floor: warned, not refused
    assert (result["exit_code"], result["stdout"]) == (0, "removed\n")
    assert result["stderr"].startswith("[sandbox] Warning: the server's disk is almost full")
    assert wait_until(lambda: box.run("bash", "echo fine")["stderr"] == "", 5)
