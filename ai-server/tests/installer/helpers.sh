#!/usr/bin/env bash
# Helpers for the installer tests: a throwaway sandbox for each test, fake
# commands in front of PATH, and a few assertions. Sourced by run.sh.

# shellcheck disable=SC2034,SC2153  # variables set here are used by the test files; AI_DIR etc. come from run.sh

# ---------------------------------------------------------------- sandbox ---

# sandbox: a fresh directory $T with
#   $T/root   stands in for "/" (MYAI_SYSROOT): /etc, /proc, /root, ...
#   $T/bin    the fake commands, first on PATH
#   $T/fake   canned answers for the fake commands (FAKE_DIR)
#   $T/calls.log  every call a fake command received (FAKE_LOG)
#   $T/app    where the app is "installed" (APP)
# The machine it describes: Ubuntu 24.04, 8 vCPUs with AVX2, 32 GB.
sandbox() {
  T=$(mktemp -d "$TEST_TMP/t.XXXXXX")
  APP="$T/app"
  export MYAI_SYSROOT="$T/root" FAKE_DIR="$T/fake" FAKE_LOG="$T/calls.log"
  mkdir -p "$MYAI_SYSROOT"/{etc,proc,root,run/systemd/system,usr/local/bin,var/log} "$FAKE_DIR" "$T/bin"
  cp "$TESTS_DIR"/fakes/* "$T/bin/"
  chmod 755 "$T/bin"/*
  : >"$FAKE_LOG"
  export PATH="$T/bin:$SYSPATH"
  export MYAI_POLL_SECS=0 MYAI_RETRY_SECS=0 MYAI_PROGRESS_SECS=1 MYAI_DOWNLOAD_TRIES=4
  export MYAI_HF_BASE="http://127.0.0.1:9"
  unset MYAI_DOMAIN MYAI_MODEL MYAI_ADMIN_EMAIL MYAI_GOVERNOR MYAI_TLS_INTERNAL MYAI_NO_HARDEN \
    MYAI_SKIP_MODEL_DOWNLOAD MYAI_YES MYAI_DRY_RUN MYAI_DIR MYAI_REPO MYAI_REF MYAI_STAGED SSH_CONNECTION
  fake_os ubuntu 24.04 noble "Ubuntu 24.04.5 LTS"
  fake_machine 8 32862000
  # A public address and a Hostinger-style name that points to it.
  echo "203.0.113.10" >"$FAKE_DIR/ip_src"
  echo "srv123456.hstgr.cloud" >"$FAKE_DIR/fqdn"
  echo "203.0.113.10" >"$FAKE_DIR/dig_A_srv123456.hstgr.cloud"
  echo "port 22" >"$FAKE_DIR/sshd_T"
  # The tests behave the same whoever runs them: "id -u" says root.
  echo 0 >"$FAKE_DIR/uid"
  export TMPDIR="$T/tmp"
  mkdir -p "$TMPDIR"
}

fake_os() { # ID VERSION CODENAME PRETTY_NAME
  cat >"$MYAI_SYSROOT/etc/os-release" <<OS
PRETTY_NAME="$4"
NAME="x"
VERSION_ID="$2"
VERSION_CODENAME=$3
ID=$1
OS
}

fake_machine() { # VCPUS RAM_KB [CPU FLAGS]
  local n=$1 ram=$2 flags=${3:-fpu sse sse2 avx avx2 fma} i=0
  : >"$MYAI_SYSROOT/proc/cpuinfo"
  while [ "$i" -lt "$n" ]; do
    printf 'processor\t: %s\nmodel name\t: Fake CPU\nflags\t\t: %s\n\n' "$i" "$flags" >>"$MYAI_SYSROOT/proc/cpuinfo"
    i=$((i + 1))
  done
  cat >"$MYAI_SYSROOT/proc/meminfo" <<MEM
MemTotal:       $ram kB
MemFree:         1000000 kB
MemAvailable:    8000000 kB
SwapTotal:       8388604 kB
SwapFree:        8388604 kB
MEM
  echo "cpu  1000 0 500 8000 100 0 50 20 0 0" >"$MYAI_SYSROOT/proc/stat"
}

# Load the library and the installer's functions, and set what main() sets.
# Arguments are installer options, as on its command line.
load_installer() {
  export MYAI_SOURCE_ONLY=1
  # shellcheck source=/dev/null
  . "$AI_DIR/bin/myai-lib.sh"
  # shellcheck source=/dev/null
  . "$AI_DIR/install.sh"
  SRC_DIR=$AI_DIR
  # --yes: a test must never stop to ask, even when run from a terminal.
  parse_args --yes --dir "$APP" "$@"
}

# The same for the myai command, working on $APP.
load_myai() {
  export MYAI_SOURCE_ONLY=1 MYAI_DIR="$APP"
  # shellcheck source=/dev/null
  . "$AI_DIR/bin/myai"
}

# A minimal installed app in $APP: compose file, settings, data folders.
fake_install() {
  mkdir -p "$APP/bin" "$APP/config" "$APP/backups" "$APP/data"/{models,webui,agent,workspace,caddy/data}
  cp "$AI_DIR/bin/myai" "$AI_DIR/bin/myai-lib.sh" "$APP/bin/"
  cp "$AI_DIR/config/models.conf" "$APP/config/"
  echo "services: {}" >"$APP/docker-compose.yml"
  cat >"$APP/.env" <<ENV
SITE_ADDRESS=srv123456.hstgr.cloud
CADDY_TLS_LINE=
GATE_TOKEN=0123456789abcdef0123456789abcdef
DATA_DIR=$APP/data
MODEL_ID=fast
MODEL_FILE=Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf
MODEL_ALIAS=assistant
LLM_THREADS=6
LLM_THREADS_BATCH=8
AGENT_THINKING=auto
WEBUI_ADMIN_EMAIL=owner@example.com
WEBUI_ADMIN_PASSWORD=FirstPassword12345678
DASHBOARD_PASSWORD=FirstPassword12345678
TELEGRAM_BOT_TOKEN=
PAIRING_CODE=12345678
ENV
  chmod 600 "$APP/.env"
  printf 'GGUFfake model' >"$APP/data/models/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
  echo "chat database" >"$APP/data/webui/webui.db"
  echo "agent database" >"$APP/data/agent/agent.db"
  echo "a file" >"$APP/data/workspace/notes.txt"
}

# Start the local file server on $1; sets HF_PORT and MYAI_HF_BASE. The
# server is stopped when the test's subshell ends.
start_fileserver() {
  local root=$1 tries=0
  : >"$T/http.log"
  python3 "$TESTS_DIR/fileserver.py" "$root" "$T/http.log" >"$T/http.port" 2>"$T/http.err" &
  FILESERVER_PID=$!
  trap 'kill "$FILESERVER_PID" 2>/dev/null || true' EXIT
  while [ ! -s "$T/http.port" ]; do
    tries=$((tries + 1))
    [ "$tries" -lt 100 ] || fail "the test file server did not start: $(cat "$T/http.err")"
    sleep 0.05
  done
  HF_PORT=$(cat "$T/http.port")
  export MYAI_HF_BASE="http://127.0.0.1:$HF_PORT"
}

# hold_lock SECONDS NAME: another process holds the myai lock of $APP for
# SECONDS and names itself NAME in the lock file, as acquire_lock does.
# Returns once the lock is held. $APP must exist.
hold_lock() {
  local tries=0
  rm -f "$T/holding"
  (
    exec 9>>"$APP/.lock"
    flock 9
    printf '%s 10:02 %s\n' "$BASHPID" "$2" >"$APP/.lock"
    : >"$T/holding"
    sleep "$1"
  ) &
  while [ ! -e "$T/holding" ]; do
    tries=$((tries + 1))
    [ "$tries" -lt 200 ] || fail "the process that should hold the lock did not start"
    sleep 0.05
  done
}

# ------------------------------------------------------------- assertions ---

fail() {
  printf 'ASSERTION FAILED: %s\n' "$*" >&2
  exit 1
}

skip() {
  printf 'SKIPPED: %s\n' "$*"
  exit 77
}

assert_eq() { # ACTUAL EXPECTED [WHAT]
  [ "$1" = "$2" ] || fail "${3:-value}: expected '$2', got '$1'"
}

assert_contains() { # TEXT PART [WHAT]
  case "$1" in
    *"$2"*) ;;
    *) fail "${3:-text} does not contain '$2'. It is: $1" ;;
  esac
}

assert_not_contains() { # TEXT PART [WHAT]
  case "$1" in
    *"$2"*) fail "${3:-text} contains '$2'. It is: $1" ;;
  esac
}

assert_file() { [ -f "$1" ] || fail "file missing: $1"; }
assert_no_file() { [ ! -e "$1" ] || fail "file should not exist: $1"; }

assert_mode() { # FILE MODE
  assert_eq "$(stat -c %a "$1")" "$2" "mode of $1"
}

# A fake command was called with this text in its arguments.
assert_called() {
  grep -qF -- "$1" "$FAKE_LOG" || fail "expected call not found: '$1'. Calls were:
$(cat "$FAKE_LOG")"
}

assert_not_called() {
  if grep -qF -- "$1" "$FAKE_LOG"; then
    fail "unexpected call: '$1'. Calls were:
$(cat "$FAKE_LOG")"
  fi
}

# The first call containing A came before the first call containing B.
assert_call_order() {
  local a b
  a=$(grep -nF -- "$1" "$FAKE_LOG" | head -n 1 | cut -d: -f1)
  b=$(grep -nF -- "$2" "$FAKE_LOG" | head -n 1 | cut -d: -f1)
  [ -n "$a" ] || fail "call not found: '$1'"
  [ -n "$b" ] || fail "call not found: '$2'"
  [ "$a" -lt "$b" ] || fail "'$1' (call $a) should come before '$2' (call $b)"
}

# expect_fail CMD...: runs CMD in a subshell, expects it to fail, and leaves
# its output in OUT.
expect_fail() {
  if OUT=$( ("$@") 2>&1); then
    fail "expected a failure, but it succeeded: $*
output: $OUT"
  fi
}

# expect_ok CMD...: runs CMD in a subshell, expects success, output in OUT.
expect_ok() {
  if ! OUT=$( ("$@") 2>&1); then
    fail "expected success, but it failed: $*
output: $OUT"
  fi
}
