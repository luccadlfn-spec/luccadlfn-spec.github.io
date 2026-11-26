"""Tests for sandbox/server.py. Run with:  python -m pytest sandbox/tests

They start the real server on a free loopback port, so they need Linux, bash
and nothing else (no Docker, no network).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

from conftest import TOKEN, process_alive, wait_until

MIB = 1024 * 1024


# --------------------------------------------------------------------------
# plain HTTP behaviour
# --------------------------------------------------------------------------


def test_healthz_needs_no_token(sandbox):
    status, headers, payload = sandbox.request("GET", "/healthz")
    assert status == 200
    assert payload == {"ok": True}
    assert headers["Content-Type"] == "application/json"
    assert "Python" not in headers.get("Server", "")


def test_unknown_paths_and_wrong_methods(sandbox):
    assert sandbox.request("GET", "/")[0] == 404
    assert sandbox.request("GET", "/exec")[0] == 405
    assert sandbox.request("POST", "/healthz", body=b"{}")[0] == 405
    assert sandbox.request("POST", "/nope", body=b"{}", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 404


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer wrong",
        f"Bearer {TOKEN[:-1]}",
        f"Bearer {TOKEN}x",
        f"Basic {TOKEN}",
        TOKEN,
        f"Bearer {TOKEN} extra",
    ],
)
def test_exec_refuses_a_bad_or_missing_token(sandbox, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    status, response_headers, payload = sandbox.request(
        "POST", "/exec", body={"language": "python", "code": "open('should-not-run', 'w')"}, headers=headers
    )
    assert status == 401
    assert response_headers.get("WWW-Authenticate") == "Bearer"
    assert "error" in payload
    assert not (sandbox.workdir / "should-not-run").exists()


def test_bearer_scheme_is_case_insensitive(sandbox):
    status, _, payload = sandbox.request(
        "POST", "/exec", body={"language": "bash", "code": "echo ok"}, headers={"Authorization": f"bearer {TOKEN}"}
    )
    assert status == 200 and payload["stdout"] == "ok\n"


def test_exec_from_the_sandbox_itself_is_refused_even_with_the_token(make_sandbox):
    # Docker hands the token to every process it starts in the container
    # (health checks included), so a snippet can find it. Requests from the
    # container itself are therefore refused; the agent calls from elsewhere.
    box = make_sandbox(SANDBOX_ALLOW_LOCAL_EXEC="")
    for token in (TOKEN, None):
        status, _, payload = box.post_exec({"language": "bash", "code": "touch from-inside"}, token=token)
        assert status == 403
        assert "inside the sandbox" in payload["error"]
    assert not (box.workdir / "from-inside").exists()
    assert box.request("GET", "/healthz")[0] == 200  # the health check is a local request too


def test_same_host_unit(server_module):
    same = server_module._same_host
    assert same("127.0.0.1", "127.0.0.1")
    assert same("127.0.0.5", "172.20.0.3")  # any loopback address
    assert same("::1", "::1")
    assert same("::ffff:127.0.0.1", "::ffff:172.20.0.3")
    assert same("172.20.0.3", "172.20.0.3")  # the container's own address on its network
    assert same("not an address", "172.20.0.3")  # unknown: refuse
    assert not same("172.20.0.2", "172.20.0.3")  # the agent's container
    assert not same("::ffff:172.20.0.2", "::ffff:172.20.0.3")


def test_no_configured_token_means_nobody_gets_in(make_sandbox):
    box = make_sandbox(SANDBOX_TOKEN="")
    assert box.request("GET", "/healthz")[0] == 200
    for authorization in ("Bearer ", "Bearer", "Bearer x", ""):
        status, _, _ = box.request(
            "POST", "/exec", body={"language": "bash", "code": "echo hi"}, headers={"Authorization": authorization}
        )
        assert status == 401


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'"text"',
        b"null",
        b"[" * 100_000,
        {"code": "print(1)"},
        {"language": "ruby", "code": "puts 1"},
        {"language": "Python", "code": "print(1)"},
        {"language": ["python"], "code": "print(1)"},
        {"language": "python"},
        {"language": "python", "code": ""},
        {"language": "python", "code": "   \n"},
        {"language": "python", "code": 5},
        {"language": "python", "code": None},
        {"language": "python", "code": "print(1)", "timeout": "5"},
        {"language": "python", "code": "print(1)", "timeout": True},
        {"language": "python", "code": "print(1)", "timeout": 0},
        {"language": "python", "code": "print(1)", "timeout": -3},
        {"language": "python", "code": "print(1)", "timeout": [5]},
        b'{"language": "python", "code": "print(1)", "timeout": NaN}',
        b'{"language": "python", "code": "print(1)", "timeout": -Infinity}',
        b'{"language": "python", "code": "\\ud800"}',
    ],
)
def test_bad_input_is_a_400(sandbox, body):
    status, _, payload = sandbox.post_exec(body)
    assert status == 400, payload
    assert isinstance(payload["error"], str) and payload["error"]


def test_missing_or_chunked_length_is_a_400(sandbox):
    head = f"POST /exec HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
    assert sandbox.raw((head + "\r\n").encode()).startswith(b"HTTP/1.1 400 ")
    chunked = head + "Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    assert sandbox.raw(chunked.encode()).startswith(b"HTTP/1.1 400 ")
    negative = head + "Content-Length: -5\r\n\r\n"
    assert sandbox.raw(negative.encode()).startswith(b"HTTP/1.1 400 ")


def test_body_over_one_megabyte_is_a_413(sandbox):
    body = json.dumps({"language": "python", "code": "#" * (MIB + 1)}).encode()
    status, _, payload = sandbox.post_exec(body)
    assert status == 413
    assert "error" in payload
    # Without a token the size does not matter: still 401.
    assert sandbox.post_exec(body, token=None)[0] == 401
    # A declared length that is absurd is refused without reading it.
    head = f"POST /exec HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {'9' * 5000}\r\n\r\n"
    assert sandbox.raw(head.encode()).startswith(b"HTTP/1.1 413 ")
    assert sandbox.request("GET", "/healthz")[0] == 200


def test_body_of_exactly_one_megabyte_is_accepted(sandbox):
    frame = len(json.dumps({"language": "python", "code": ""}))
    prefix = "print('fits')\n"
    code = prefix + "#" * (MIB - frame - len(prefix) - 1)  # the newline takes two bytes in JSON
    body = json.dumps({"language": "python", "code": code}).encode()
    assert len(body) == MIB
    status, _, payload = sandbox.post_exec(body)
    assert status == 200, payload
    assert payload["stdout"] == "fits\n"


# --------------------------------------------------------------------------
# running code
# --------------------------------------------------------------------------


def test_python_result_fields(sandbox):
    result = sandbox.run("python", "import sys\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit(3)")
    assert set(result) == {"exit_code", "stdout", "stderr", "timed_out", "duration"}
    assert result["exit_code"] == 3
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"
    assert result["timed_out"] is False
    assert isinstance(result["duration"], float) and 0 <= result["duration"] < 30


def test_python_traceback_names_a_real_file(sandbox):
    result = sandbox.run("python", "x = 1\nraise ValueError('boom')")
    assert result["exit_code"] == 1
    assert "ValueError: boom" in result["stderr"]
    assert "main.py" in result["stderr"] and "line 2" in result["stderr"]


def test_bash_result_fields(sandbox):
    result = sandbox.run("bash", "echo out; echo err >&2; exit 4")
    assert (result["exit_code"], result["stdout"], result["stderr"], result["timed_out"]) == (4, "out\n", "err\n", False)


def test_bash_is_really_bash(sandbox):
    result = sandbox.run("bash", 'a=(one two three); echo "${a[1]} ${#a[@]} $BASH_VERSION"')
    assert result["exit_code"] == 0
    assert result["stdout"].startswith("two 3 ")


def test_death_by_signal_gives_a_negative_exit_code(sandbox):
    result = sandbox.run("bash", "kill -TERM $$")
    assert result["exit_code"] == -signal.SIGTERM
    assert result["timed_out"] is False


def test_runs_in_the_workspace_and_files_persist(sandbox):
    result = sandbox.run("python", "import os\nprint(os.getcwd())\nopen('kept.txt', 'w').write('hello')")
    assert result["stdout"].strip() == str(sandbox.workdir)
    assert (sandbox.workdir / "kept.txt").read_text() == "hello"
    assert sandbox.run("bash", "cat kept.txt")["stdout"] == "hello"


def test_workspace_modules_can_be_imported(sandbox):
    (sandbox.workdir / "helper_mod.py").write_text("VALUE = 41\n")
    assert sandbox.run("python", "import helper_mod\nprint(helper_mod.VALUE + 1)")["stdout"] == "42\n"
    assert not (sandbox.workdir / "__pycache__").exists()


def test_environment_is_minimal_and_has_no_token(sandbox):
    result = sandbox.run("python", "import json, os\nprint(json.dumps(dict(os.environ)))")
    env = json.loads(result["stdout"])
    assert TOKEN not in result["stdout"]
    assert "SANDBOX_TOKEN" not in env
    assert "CANARY_FROM_SERVER_ENV" not in env
    assert env["HOME"] == str(sandbox.tmpdir)
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert set(env) <= {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH", "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING", "MPLBACKEND", "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS", "TZ", "PYTHONNOUSERSITE", "SANDBOX_WORKSPACE",
    }  # fmt: skip
    # Entries in PYTHONPATH come before the standard library: never the workspace.
    assert str(sandbox.workdir) not in env["PYTHONPATH"].split(os.pathsep)
    assert env["PYTHONNOUSERSITE"] == "1"
    bash_env = sandbox.run("bash", "env")["stdout"]
    assert TOKEN not in bash_env and "CANARY" not in bash_env


def test_time_zone_is_passed_on(make_sandbox):
    box = make_sandbox(TZ="Asia/Tokyo")
    assert box.run("bash", "echo $TZ")["stdout"] == "Asia/Tokyo\n"


def test_stdin_is_empty_and_inherits_no_server_sockets(sandbox):
    result = sandbox.run("python", "import os, sys\nprint(repr(sys.stdin.read()))\nprint(sorted(os.listdir('/proc/self/fd')))")
    lines = result["stdout"].splitlines()
    assert lines[0] == "''"
    # 0, 1, 2 plus the descriptor listdir itself uses: the listening socket is not inherited.
    assert len(json.loads(lines[1].replace("'", '"'))) <= 4


def test_script_temp_files_are_removed(sandbox):
    sandbox.run("python", "print(1)")
    sandbox.run("bash", "echo 1")
    assert [p.name for p in sandbox.tmpdir.iterdir() if p.name.startswith("exec-")] == []


def test_unicode_round_trip_and_invalid_bytes(sandbox):
    assert sandbox.run("python", "print('héllo ✓ \U0001f600')")["stdout"] == "héllo ✓ \U0001f600\n"
    result = sandbox.run("bash", "printf 'a\\xff\\xfeb'")
    assert result["exit_code"] == 0
    assert result["stdout"] == "a��b"


def test_output_printed_before_a_timeout_is_kept(sandbox):
    result = sandbox.run("python", "import time\nprint('before the hang')\ntime.sleep(60)", timeout=1)
    assert result["timed_out"] is True
    assert result["stdout"] == "before the hang\n"


# --------------------------------------------------------------------------
# timeouts and process cleanup
# --------------------------------------------------------------------------

FORK_TREE = """
import os, time
def note():
    with open("tree.pids", "a") as handle:
        handle.write(f"{os.getpid()}\\n")
for _ in range(3):
    os.fork()
note()
print("forked", flush=True)
time.sleep(300)
"""


def test_timeout_kills_the_whole_process_tree(sandbox):
    pid_file = sandbox.workdir / "tree.pids"
    pid_file.unlink(missing_ok=True)
    started = time.monotonic()
    result = sandbox.run("python", FORK_TREE, timeout=2)
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert result["exit_code"] == -signal.SIGKILL
    assert 1.9 <= result["duration"] < 6
    assert elapsed < 10
    assert result["stdout"].count("forked") == 8
    pids = {int(line) for line in pid_file.read_text().split()}
    assert len(pids) == 8
    assert wait_until(lambda: not any(process_alive(pid) for pid in pids), 5), "part of the tree survived"


def test_timeout_kills_a_bash_pipeline_with_background_jobs(sandbox):
    pid_file = sandbox.workdir / "bash.pids"
    pid_file.unlink(missing_ok=True)
    code = "sleep 300 & echo $! >> bash.pids\n(sleep 300 & echo $! >> bash.pids; sleep 300) & echo $! >> bash.pids\nyes | sleep 300"
    result = sandbox.run("bash", code, timeout=1)
    assert result["timed_out"] is True
    pids = {int(line) for line in pid_file.read_text().split()}
    assert len(pids) == 3
    assert wait_until(lambda: not any(process_alive(pid) for pid in pids), 5)


def test_background_jobs_neither_delay_the_answer_nor_survive(sandbox):
    pid_file = sandbox.workdir / "bg.pid"
    pid_file.unlink(missing_ok=True)
    started = time.monotonic()
    result = sandbox.run("bash", "sleep 120 & echo $! > bg.pid; echo done", timeout=60)
    assert time.monotonic() - started < 10
    assert (result["exit_code"], result["stdout"], result["timed_out"]) == (0, "done\n", False)
    pid = int(pid_file.read_text())
    assert wait_until(lambda: not process_alive(pid), 5)


def test_timeout_above_the_maximum_is_clamped_not_refused(sandbox):
    result = sandbox.run("bash", "echo ok", timeout=100_000)
    assert result["stdout"] == "ok\n"
    status, _, payload = sandbox.post_exec(b'{"language": "bash", "code": "echo big", "timeout": 1e999}')
    assert status == 400  # infinity is not a number of seconds
    status, _, payload = sandbox.post_exec(b'{"language": "bash", "code": "echo big", "timeout": ' + b"9" * 400 + b"}")
    assert status == 200 and payload["stdout"] == "big\n"


def test_fractional_timeout(sandbox):
    result = sandbox.run("bash", "sleep 30", timeout=0.3)
    assert result["timed_out"] is True
    assert 0.25 <= result["duration"] < 5


# --------------------------------------------------------------------------
# output limits
# --------------------------------------------------------------------------


def test_long_output_keeps_head_and_tail_and_marks_the_cut(sandbox):
    code = (
        "import sys\n"
        "sys.stdout.write('HEAD' + 'a' * 600000 + 'TAIL')\n"
        "sys.stderr.write('EHEAD' + 'b' * 300000 + 'ETAIL')\n"
    )
    result = sandbox.run("python", code)
    assert result["exit_code"] == 0
    for stream, head, tail, total in (("stdout", "HEAD", "TAIL", 600008), ("stderr", "EHEAD", "ETAIL", 300010)):
        text = result[stream]
        assert len(text) <= 20000
        assert len(text) > 19000
        assert text.startswith(head) and text.endswith(tail)
        assert f"[... output truncated: {total} bytes in total, middle omitted ...]" in text


def test_output_at_the_limit_is_untouched(sandbox):
    result = sandbox.run("python", "import sys\nsys.stdout.write('x' * 20000)")
    assert result["stdout"] == "x" * 20000
    result = sandbox.run("python", "import sys\nsys.stdout.write('x' * 20001)")
    assert len(result["stdout"]) <= 20000 and "truncated" in result["stdout"]


def test_huge_output_does_not_block_or_exhaust_memory(sandbox):
    started = time.monotonic()
    result = sandbox.run("bash", "head -c 200000000 /dev/zero | tr '\\0' 'z'; echo; echo END", timeout=120, http_timeout=150)
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert len(result["stdout"]) <= 20000
    assert result["stdout"].endswith("END\n")
    assert time.monotonic() - started < 100


def test_output_buffer_unit(server_module):
    buffer = server_module.OutputBuffer()
    assert buffer.text() == ""
    buffer.add(b"hello ")
    buffer.add("wörld".encode())
    assert buffer.text() == "hello wörld"

    big = server_module.OutputBuffer()
    for _ in range(2000):
        big.add(b"0123456789" * 1000)  # 20 MB in total
    assert len(big.head) + len(big.tail) <= 3 * server_module._KEEP_BYTES
    text = big.text()
    assert len(text) <= server_module.OUTPUT_LIMIT
    assert text.startswith("0123456789") and text.endswith("0123456789")
    assert "20000000 bytes in total" in text

    wide = server_module.OutputBuffer()
    wide.add(("\U0001f600" * 30000).encode())  # 4 bytes per character
    text = wide.text()
    assert len(text) <= server_module.OUTPUT_LIMIT
    assert text.startswith("\U0001f600") and text.endswith("\U0001f600")

    split = server_module.OutputBuffer()
    for byte in "ééé".encode():
        split.add(bytes([byte]))  # multi-byte characters arriving one byte at a time
    assert split.text() == "ééé"


def test_parse_request_unit(server_module):
    parse = server_module.parse_request
    assert parse(b'{"language": "python", "code": "print(1)"}') == ("python", "print(1)", 60.0)
    assert parse(b'{"language": "bash", "code": "ls", "timeout": 5}') == ("bash", "ls", 5.0)
    assert parse(b'{"language": "bash", "code": "ls", "timeout": 2.5}') == ("bash", "ls", 2.5)
    assert parse(b'{"language": "bash", "code": "ls", "timeout": null}')[2] == 60.0
    assert parse(b'{"language": "bash", "code": "ls", "timeout": 301}')[2] == 300.0
    assert parse(b'{"language": "bash", "code": "ls", "timeout": 1e9}')[2] == 300.0
    assert parse(b'{"language": "bash", "code": "ls", "unknown": 1}')[0] == "bash"
    with pytest.raises(ValueError):
        parse(b'{"language": "sh", "code": "ls"}')


def test_stop_requested_unit(server_module):
    stop, info = server_module.stop_requested, types.SimpleNamespace
    assert stop(info(si_pid=4321, si_code=0), as_init=False)  # outside a container: any sender
    assert stop(info(si_pid=0, si_code=0), as_init=True)  # kill() from outside the container (docker stop)
    assert stop(info(si_pid=0, si_code=-6), as_init=True)  # tgkill() from outside
    assert not stop(info(si_pid=57, si_code=0), as_init=True)  # kill -TERM 1 from a snippet
    assert not stop(info(si_pid=0, si_code=-1), as_init=True)  # rt_sigqueueinfo() with a made-up sender


def test_memory_note_unit(server_module):
    note = server_module._memory_note
    assert note(3, 3, True) is None  # the counter shows no memory kill: a snippet's own kill -9
    assert "ran out of" in note(3, 4, False)  # a child died of it even though the program exited normally
    assert "most likely" in note(None, None, True)  # no counter: say what probably happened
    assert note(None, None, False) is None


def test_server_notes_stay_within_the_output_limit(server_module):
    big = server_module.OutputBuffer()
    big.add(b"x" * 100_000)
    text = server_module.with_notes(big, ["[sandbox] before"], ["[sandbox] after"])
    assert len(text) <= server_module.OUTPUT_LIMIT
    assert text.startswith("[sandbox] before\nxxx") and text.endswith("xxx\n[sandbox] after\n")
    assert "output truncated: 100000 bytes" in text
    small = server_module.OutputBuffer()
    small.add(b"no newline")
    assert server_module.with_notes(small, [], ["[sandbox] note"]) == "no newline\n[sandbox] note\n"
    assert server_module.with_notes(server_module.OutputBuffer(), [], []) == ""


def test_empty_dir_unit(server_module, tmp_path):
    top, outside = tmp_path / "scratch", tmp_path / "outside"
    top.mkdir()
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (top / "file").write_text("x")
    (top / "link-to-outside").symlink_to(outside)  # removed, its target untouched
    deep = top
    for _ in range(1100):  # deeper than Python's recursion limit
        deep = deep / "d"
        deep.mkdir()
    (deep / "bottom").write_text("y")
    locked = top / "locked" / "inner"
    locked.mkdir(parents=True)
    (locked / "f").write_text("z")
    locked.chmod(0)
    locked.parent.chmod(0)
    server_module._empty_dir(str(top))
    assert list(top.iterdir()) == []
    assert (outside / "keep.txt").read_text() == "keep"


def test_default_disk_floor_unit(server_module, tmp_path):
    guard = server_module.DiskGuard(str(tmp_path))
    st = os.statvfs(tmp_path)
    assert guard.enabled
    assert guard.floor == max(5 * 1024**3, st.f_blocks * st.f_frsize // 20)
    assert server_module.DiskGuard(str(tmp_path / "missing")).enabled is False


# --------------------------------------------------------------------------
# resource limits
# --------------------------------------------------------------------------


def test_file_size_limit(make_sandbox):
    box = make_sandbox(SANDBOX_MAX_FILE_MB="1")
    result = box.run("bash", 'head -c 3000000 /dev/zero > big.bin; echo "rc=$?"')
    assert "rc=0" not in result["stdout"]
    assert (box.workdir / "big.bin").stat().st_size <= MIB
    result = box.run("python", "open('big2.bin', 'wb').write(b'x' * 3000000)")
    assert result["exit_code"] != 0
    assert "File too large" in result["stderr"]
    assert (box.workdir / "big2.bin").stat().st_size <= MIB
    assert box.run("python", "open('small.bin', 'wb').write(b'x' * 500000)\nprint('ok')")["stdout"] == "ok\n"


def test_default_file_size_limit_is_100_mb(sandbox):
    result = sandbox.run("bash", "ulimit -f; ulimit -n; ulimit -c")
    blocks, files, core = result["stdout"].split()
    assert int(blocks) * 1024 == 100 * MIB  # bash reports 1024-byte blocks
    assert int(files) == 256
    assert int(core) == 0


def test_open_files_limit(make_sandbox):
    box = make_sandbox(SANDBOX_MAX_OPEN_FILES="32")
    code = (
        "import os\n"
        "held = []\n"
        "try:\n"
        "    while len(held) < 1000:\n"
        "        held.append(os.open('/dev/null', os.O_RDONLY))\n"
        "except OSError as error:\n"
        "    print(len(held), error.errno)\n"
    )
    count, errno_value = box.run("python", code)["stdout"].split()
    assert int(count) < 32
    assert int(errno_value) == 24  # EMFILE


def test_process_limit_reaches_the_snippet(make_sandbox):
    box = make_sandbox(SANDBOX_MAX_PROCS="77")
    assert box.run("bash", "ulimit -u")["stdout"].strip() == "77"


def test_cpu_time_limit_is_applied_by_the_bootstrap(server_module):
    # 1 CPU second soft (SIGXCPU), +5 hard (SIGKILL); a busy loop must not outlive that.
    argv = [sys.executable, "-I", "-S", "-c", server_module._BOOTSTRAP, "1", str(10**9), "64", "0",
            sys.executable, "-c", "while True: pass"]  # fmt: skip
    started = time.monotonic()
    proc = subprocess.run(argv, timeout=60)
    assert proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL)
    assert time.monotonic() - started < 30


def test_cpu_limit_follows_the_timeout(sandbox):
    assert sandbox.run("bash", "ulimit -t", timeout=7)["stdout"].strip() == "12"


def test_falls_back_to_an_in_memory_script_when_tmp_is_unusable(make_sandbox, tmp_path):
    box = make_sandbox(SANDBOX_TMPDIR=str(tmp_path / "does-not-exist"))
    result = box.run("python", "print(__file__)\nprint(6 * 7)")
    assert result["exit_code"] == 0, result
    assert result["stdout"].splitlines()[0].startswith("/proc/self/fd/")
    assert result["stdout"].splitlines()[1] == "42"
    assert box.run("bash", "echo from-bash; exit 5")["exit_code"] == 5
    assert box.run("bash", "sleep 30", timeout=0.5)["timed_out"] is True


def test_missing_workspace_runs_in_tmp_and_says_so(make_sandbox, tmp_path):
    # A 500 would reach the model only as "the sandbox refused the request".
    box = make_sandbox(SANDBOX_WORKDIR=str(tmp_path / "no-such-workspace"))
    result = box.run("bash", "pwd; echo hi")
    assert result["exit_code"] == 0
    assert result["stdout"] == f"{box.tmpdir}\nhi\n"
    assert result["stderr"].startswith("[sandbox] The workspace ")
    assert "cannot be used" in result["stderr"] and str(box.tmpdir) in result["stderr"]
    assert box.request("GET", "/healthz")[0] == 200


# --------------------------------------------------------------------------
# the workspace: permissions, imports, disk space
# --------------------------------------------------------------------------


def test_a_chmod_of_the_workspace_is_repaired(make_unprivileged_sandbox):
    # `chmod -R 644 .` takes the x bit off the workspace itself; without the
    # repair no later run could even start in it (each was a 500).
    box = make_unprivileged_sandbox()
    result = box.run("bash", "echo data > keep.txt; chmod -R 644 . ; echo done")
    assert "done" in result["stdout"]
    result = box.run("bash", "pwd; cat keep.txt; touch new.txt && echo wrote")
    assert result["exit_code"] == 0, result
    assert result["stdout"] == f"{box.workdir}\ndata\nwrote\n"
    assert "[sandbox] The workspace" in result["stderr"] and "given back" in result["stderr"]
    assert box.workdir.stat().st_mode & 0o700 == 0o700
    # Once repaired, runs are quiet again.
    assert box.run("bash", "echo quiet")["stderr"] == ""


def test_a_workspace_that_cannot_be_entered_falls_back_to_tmp(make_unprivileged_sandbox):
    if os.geteuid() != 0:
        pytest.skip("needs root to give the workspace to another owner")
    box = make_unprivileged_sandbox()
    os.chown(box.workdir, 0, 0)  # for example after a restore by root
    box.workdir.chmod(0o700)
    result = box.run("bash", "pwd; echo still-runs")
    assert result["exit_code"] == 0
    assert result["stdout"] == f"{box.tmpdir}\nstill-runs\n"
    assert "cannot be used (permission denied)" in result["stderr"]


PLANTED = {
    "sitecustomize.py": "print('PLANTED sitecustomize')\n",
    "usercustomize.py": "print('PLANTED usercustomize')\n",
    "planted.pth": "import sys; print('PLANTED pth')\n",
    "email.py": "raise ImportError('PLANTED email.py')\n",
    "json.py": "raise ImportError('PLANTED json.py')\n",
    "random.py": "raise ImportError('PLANTED random.py')\n",
    "types.py": "raise ImportError('PLANTED types.py')\n",
}


def test_files_in_the_workspace_cannot_hijack_later_python_runs(make_sandbox):
    box = make_sandbox()
    for name, text in PLANTED.items():
        (box.workdir / name).write_text(text)
    # HOME is the temp directory: a per-user site directory there would be
    # loaded by every later run as well.
    user_site = box.tmpdir / ".local" / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
    user_site.mkdir(parents=True)
    (user_site / "usercustomize.py").write_text("print('PLANTED user site')\n")
    (user_site / "planted.pth").write_text("import sys; print('PLANTED user pth')\n")
    (box.workdir / "helper_mod.py").write_text("VALUE = 41\n")
    code = "import json, random, tempfile, types, urllib.request\nimport helper_mod\nprint('answer', 6 * 7, helper_mod.VALUE + 1)"
    result = box.run("python", code)
    assert result["exit_code"] == 0, result
    assert result["stdout"] == "answer 42 42\n"
    assert "PLANTED" not in result["stdout"] + result["stderr"]
    # Python started from bash, in another directory, finds workspace modules too.
    result = box.run("bash", "cd /; python3 -c 'import helper_mod, urllib.request; print(helper_mod.VALUE)'")
    assert (result["exit_code"], result["stdout"]) == (0, "41\n"), result
    assert "PLANTED" not in result["stdout"] + result["stderr"]


def test_a_nearly_full_disk_stops_writing_but_not_cleaning_up(make_sandbox, tmp_path):
    # A floor far above what is free: for the guard the disk is already full.
    st = os.statvfs(tmp_path)
    box = make_sandbox(SANDBOX_MIN_FREE_MB=str(st.f_bavail * st.f_frsize // MIB + 100_000))
    result = box.run("bash", "echo hi")
    assert (result["exit_code"], result["stdout"]) == (0, "hi\n")
    assert result["stderr"].startswith("[sandbox] Warning: the server's disk is almost full")
    (box.workdir / "old.bin").write_bytes(b"x" * 2_000_000)
    result = box.run("bash", "rm old.bin && echo removed")
    assert (result["exit_code"], result["stdout"]) == (0, "removed\n")
    fill = (
        "import os, time\n"
        "with open('fill.bin', 'wb') as f:\n"
        "    for _ in range(30):\n"
        "        f.write(b'x' * 4_000_000); f.flush(); os.fsync(f.fileno()); time.sleep(0.03)\n"
        "print('wrote everything')\n"
    )
    try:
        result = box.run("python", fill, timeout=60)
        written = (box.workdir / "fill.bin").stat().st_size
    finally:
        (box.workdir / "fill.bin").unlink(missing_ok=True)
    assert result["exit_code"] == -signal.SIGKILL and result["timed_out"] is False, result
    assert "wrote everything" not in result["stdout"]
    assert "[sandbox] Stopped: the server's disk is almost full" in result["stderr"]
    assert written < 100_000_000


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------


def _in_threads(count: int, target) -> list:
    results: list = [None] * count

    def work(index: int) -> None:
        try:
            results[index] = target(index)
        except BaseException as error:  # surface failures in the main thread
            results[index] = error

    threads = [threading.Thread(target=work, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def test_one_run_at_a_time_and_the_second_request_waits(sandbox):
    log = sandbox.workdir / "serial.log"
    log.unlink(missing_ok=True)
    code = (
        "import time\n"
        "open('serial.log', 'a').write(f'start {time.time()}\\n')\n"
        "time.sleep(0.7)\n"
        "open('serial.log', 'a').write(f'end {time.time()}\\n')\n"
    )
    started = time.monotonic()
    results = _in_threads(3, lambda _: sandbox.run("python", code))
    elapsed = time.monotonic() - started
    assert all(result["exit_code"] == 0 for result in results)
    events = sorted((float(when), what) for what, when in (line.split() for line in log.read_text().splitlines()))
    assert [what for _, what in events] == ["start", "end"] * 3, "runs overlapped"
    assert elapsed >= 2.1


def test_parallel_requests_each_get_their_own_answer(sandbox):
    results = _in_threads(6, lambda index: sandbox.run("bash", f"echo request-{index}"))
    assert [result["stdout"] for result in results] == [f"request-{index}\n" for index in range(6)]


def test_healthz_answers_while_code_is_running(sandbox):
    worker = threading.Thread(target=lambda: sandbox.run("bash", "sleep 2"))
    worker.start()
    try:
        time.sleep(0.3)
        started = time.monotonic()
        assert sandbox.request("GET", "/healthz", timeout=5)[0] == 200
        assert time.monotonic() - started < 1.5
    finally:
        worker.join(30)


def test_too_many_waiting_requests_get_a_503(sandbox, server_module):
    marker = sandbox.workdir / "busy.marker"
    marker.unlink(missing_ok=True)
    holder = threading.Thread(target=lambda: sandbox.run("bash", "touch busy.marker; sleep 2"))
    holder.start()
    try:
        assert wait_until(marker.exists, 10)
        extra = server_module.MAX_PENDING  # one more than fits next to the running one
        statuses = _in_threads(extra, lambda _: sandbox.post_exec({"language": "bash", "code": "echo hi"})[0])
    finally:
        holder.join(30)
    assert sorted(statuses) == [200] * (extra - 1) + [503]


def _raw_exec(box, code: str, timeout: float = 120) -> socket.socket:
    body = json.dumps({"language": "bash", "code": code, "timeout": timeout}).encode()
    head = f"POST /exec HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n"
    sock = socket.create_connection(("127.0.0.1", box.port), timeout=10)
    sock.sendall(head.encode() + body)
    return sock


def test_client_hangup_stops_the_run_and_frees_the_sandbox(sandbox):
    pid_file = sandbox.workdir / "hang.pid"
    pid_file.unlink(missing_ok=True)
    sock = _raw_exec(sandbox, "echo $$ > hang.pid; sleep 120")
    try:
        assert wait_until(lambda: pid_file.exists() and pid_file.read_text().strip(), 10)
        pid = int(pid_file.read_text())
        assert process_alive(pid)
    finally:
        sock.close()
    assert wait_until(lambda: not process_alive(pid), 5), "the run kept going after the client left"
    started = time.monotonic()
    assert sandbox.run("bash", "echo next")["stdout"] == "next\n"
    assert time.monotonic() - started < 5


def test_a_waiting_request_whose_client_left_is_never_run(sandbox):
    marker = sandbox.workdir / "abandoned.marker"
    busy = sandbox.workdir / "busy2.marker"
    for path in (marker, busy):
        path.unlink(missing_ok=True)
    holder = threading.Thread(target=lambda: sandbox.run("bash", "touch busy2.marker; sleep 1.5"))
    holder.start()
    try:
        assert wait_until(busy.exists, 10)
        sock = _raw_exec(sandbox, "touch abandoned.marker")
        time.sleep(0.4)  # it is now queued behind the running snippet
        sock.close()
    finally:
        holder.join(30)
    assert sandbox.run("bash", "echo after")["stdout"] == "after\n"  # the queue has drained
    assert not marker.exists()


def test_half_sent_request_does_not_block_others(sandbox):
    head = f"POST /exec HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 500\r\n\r\n" + '{"language":'
    with socket.create_connection(("127.0.0.1", sandbox.port), timeout=10) as stalled:
        stalled.sendall(head.encode())
        assert sandbox.run("bash", "echo fine")["stdout"] == "fine\n"


# --------------------------------------------------------------------------
# shutdown
# --------------------------------------------------------------------------


def test_sigterm_stops_the_server_and_the_running_snippet(make_sandbox):
    box = make_sandbox()
    pid_file = box.workdir / "term.pid"
    sock = _raw_exec(box, "echo $$ > term.pid; sleep 120")
    try:
        assert wait_until(lambda: pid_file.exists() and pid_file.read_text().strip(), 10)
        pid = int(pid_file.read_text())
        started = time.monotonic()
        box.proc.send_signal(signal.SIGTERM)
        assert box.proc.wait(timeout=10) == 0
        assert time.monotonic() - started < 5
        assert wait_until(lambda: not process_alive(pid), 5)
    finally:
        sock.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root is exempt from RLIMIT_NPROC; covered by the container test")
def test_fork_bomb_is_contained(make_sandbox):
    # The limit counts all processes of this user, so allow what already runs plus a little.
    mine = sum(1 for name in os.listdir("/proc") if name.isdigit() and os.stat(f"/proc/{name}").st_uid == os.getuid())
    box = make_sandbox(SANDBOX_MAX_PROCS=str(mine + 150))
    result = box.run("bash", ":(){ :|:& };:\nsleep 3", timeout=10)
    assert result["exit_code"] is not None
    assert box.run("bash", "echo alive")["stdout"] == "alive\n"
