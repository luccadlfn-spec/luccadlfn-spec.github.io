#!/usr/bin/env bash
# myai-lib.sh: functions shared by install.sh and bin/myai.
#
# This file is sourced, never run. Loading it changes no shell option and
# does nothing by itself. The caller sets APP_DIR (where the app lives, for
# example /opt/myai) before calling anything that touches files or Docker.

: "${DRY_RUN:=0}"
: "${ASSUME_YES:=0}"
# Test hook: a directory that stands in for "/" for the system files this code
# reads and writes (/proc, /etc, ...). Empty on a real server.
: "${MYAI_SYSROOT:=}"
# Test hook: where model files and their checksums are fetched from.
: "${MYAI_HF_BASE:=https://huggingface.co}"
# Seconds between progress lines, between health polls, between download tries.
: "${MYAI_PROGRESS_SECS:=15}"
: "${MYAI_POLL_SECS:=5}"
: "${MYAI_RETRY_SECS:=10}"
: "${MYAI_DOWNLOAD_TRIES:=8}"

MYAI_SERVICES="llm agent sandbox webui searxng caddy"

# ------------------------------------------------------------------ output ---

say()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '  Warning: %s\n' "$*" >&2; }

# die "what went wrong" ["what to do next" ...]
die() {
  local line
  printf '\nStopped: %s\n' "$1" >&2
  shift
  for line in "$@"; do printf '  %s\n' "$line" >&2; done
  # shellcheck disable=SC2034  # read by the exit handler of the script that sourced this file
  MYAI_DIE_SHOWN=1
  exit 1
}

# 21166758016 -> "21.2 GB" (decimal, as download pages show it)
fmt_gb() { awk -v b="${1:-0}" 'BEGIN { printf "%.1f GB", b / 1000000000 }'; }

# 135 -> "2 min 15 s"
fmt_duration() {
  local s=${1:-0}
  if [ "$s" -lt 60 ]; then
    printf '%d s' "$s"
  else
    printf '%d min %02d s' $((s / 60)) $((s % 60))
  fi
}

# Quote a command line for display only.
quote_cmd() {
  local arg out=""
  for arg in "$@"; do
    if [[ $arg =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then
      out+="$arg "
    else
      out+="'${arg//\'/\'\\\'\'}' "
    fi
  done
  printf '%s' "${out% }"
}

# ----------------------------------------------- doing or only showing ---

# run CMD...: run it, or with --dry-run only print it.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  would run: %s\n' "$(quote_cmd "$@")"
    return 0
  fi
  "$@"
}

# write_file PATH MODE: content from stdin. Written through a temporary file
# so a crash never leaves half a file behind.
write_file() {
  local path=$1 mode=$2 tmp
  if [ "$DRY_RUN" = 1 ]; then
    printf '  would write: %s (mode %s)\n' "$path" "$mode"
    sed 's/^/      | /'
    return 0
  fi
  mkdir -p "$(dirname "$path")" || return 1
  tmp=$(mktemp "$path.XXXXXX") || return 1
  if cat >"$tmp" && chmod "$mode" "$tmp" && mv -f "$tmp" "$path"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# Same, but leaves a file alone that already has this content. Sets
# FILE_CHANGED to 1 when the file was (or would be) written, else 0.
write_file_if_changed() {
  local path=$1 mode=$2 content
  content=$(cat)
  FILE_CHANGED=0
  if [ -f "$path" ] && [ "$content" = "$(cat "$path")" ]; then
    return 0
  fi
  # shellcheck disable=SC2034  # read by the caller
  FILE_CHANGED=1
  printf '%s\n' "$content" | write_file "$path" "$mode"
}

# Add LINE to FILE unless the file already has exactly that line.
append_line_once() {
  local file=$1 line=$2
  if [ -f "$file" ] && grep -qxF -- "$line" "$file"; then
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '  would add to %s: %s\n' "$file" "$line"
    return 0
  fi
  printf '%s\n' "$line" >>"$file"
}

# ----------------------------------------------------------------- asking ---

# True when a question can be put to a person: not --yes, not --dry-run, and
# a terminal exists (the script itself may arrive on stdin from curl).
have_tty() { { : </dev/tty; } 2>/dev/null; }

can_ask() {
  [ "$ASSUME_YES" != 1 ] && [ "$DRY_RUN" != 1 ] && have_tty
}

# ask "question " "default": prints the answer. Gives up after five minutes
# and uses the default, so an unattended run never hangs.
ask() {
  local reply=""
  printf '%s' "$1" >/dev/tty
  IFS= read -r -t 300 reply </dev/tty || reply=""
  printf '%s\n' "${reply:-$2}"
}

# confirm "question": true for yes. --yes answers yes; without a terminal the
# answer is no.
confirm() {
  local answer
  if [ "$ASSUME_YES" = 1 ]; then return 0; fi
  if ! can_ask; then return 1; fi
  answer=$(ask "$1 [y/N] " n)
  case "$answer" in
    y | Y | yes | Yes | YES) return 0 ;;
  esac
  return 1
}

# ------------------------------------------------------- the settings file ---

env_file() { printf '%s\n' "${ENV_FILE:-$APP_DIR/.env}"; }

# Keys whose values are never printed.
is_secret_key() {
  case "$1" in
    *_KEY | *_TOKEN | *_SECRET | *_PASSWORD | PAIRING_CODE) return 0 ;;
  esac
  return 1
}

# env_get KEY [FILE]: prints the value. Fails when the key is not in the file.
# The file is parsed, never executed.
env_get() {
  local key=$1 file=${2:-} line val
  [ -n "$file" ] || file=$(env_file)
  [ -f "$file" ] || return 1
  line=$(grep -m1 -E "^${key}=" "$file") || return 1
  val=${line#*=}
  val=${val%$'\r'}
  case "$val" in
    \'*\')
      val=${val#\'}
      val=${val%\'}
      ;;
    \"*\")
      val=${val#\"}
      val=${val%\"}
      ;;
  esac
  printf '%s\n' "$val"
}

env_has() { env_get "$1" "${2:-}" >/dev/null 2>&1; }

# How a value is written in the file: as it is when it is made of harmless
# characters, otherwise in single quotes (Docker Compose reads both).
env_quote() {
  local value=$1
  case "$value" in
    *$'\n'* | *\'*) return 1 ;;
  esac
  if [[ $value =~ ^[A-Za-z0-9_.,:@/+=%~-]*$ ]]; then
    printf '%s\n' "$value"
  else
    printf "'%s'\n" "$value"
  fi
}

# env_set KEY VALUE [FILE]: replace the key's line, or add one. Every other
# line stays as it is. The file keeps mode 600.
env_set() {
  local key=$1 value=$2 file=${3:-} quoted tmp
  [ -n "$file" ] || file=$(env_file)
  case "$key" in
    '' | *[!A-Za-z0-9_]*) die "Internal error: '$key' is not a valid settings name." ;;
  esac
  quoted=$(env_quote "$value") ||
    die "The value for $key cannot be stored: it contains a quote character or a line break." \
      "Use a value without those characters."
  if [ "$DRY_RUN" = 1 ]; then
    if is_secret_key "$key"; then
      note "would set $key=<hidden> in $file"
    else
      note "would set $key=$quoted in $file"
    fi
    return 0
  fi
  if [ ! -f "$file" ]; then
    (umask 077 && : >"$file")
  fi
  tmp=$(mktemp "$file.XXXXXX")
  # The value travels through the environment so that awk does not interpret
  # backslashes or "&" in it.
  if ! K=$key V=$quoted awk '
      BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0 }
      index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
      { print }
      END { if (!done) print k "=" v }' "$file" >"$tmp"; then
    rm -f "$tmp"
    die "Could not update $file." "Check that the disk is not full: df -h"
  fi
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

# -------------------------------------------------------- model catalogue ---
# config/models.conf: id|min_ram_gb|hf_repo|file|bytes|description

catalog_file() { printf '%s\n' "${MODELS_CONF:-$APP_DIR/config/models.conf}"; }

catalog_lines() {
  local file
  file=$(catalog_file)
  [ -f "$file" ] || return 1
  grep -v -E '^[[:space:]]*(#|$)' "$file" || true
}

# catalog_field ID N: field N (1-6) of the entry. Fails for an unknown id.
catalog_field() {
  catalog_lines | awk -F'|' -v id="$1" -v n="$2" '
    $1 == id && !found { print $n; found = 1 }
    END { exit !found }'
}

catalog_ids() { catalog_lines | awk -F'|' '{ print $1 }'; }

# True when a server with RAM_KB of memory can run a model that asks for
# MIN_GB. A "32 GB" server reports a little less than 32 GiB (the kernel keeps
# some, and some providers count in decimal GB), so 93% of the figure counts.
ram_fits() {
  awk -v kb="$1" -v gb="$2" 'BEGIN { exit !(kb >= gb * 1048576 * 0.93) }'
}

# catalog_pick_for_ram RAM_KB: the first entry, in file order, that fits.
catalog_pick_for_ram() {
  catalog_lines | awk -F'|' -v kb="$1" '
    !found && kb >= $2 * 1048576 * 0.93 { print $1; found = 1 }
    END { exit !found }'
}

# --------------------------------------------------------------- hardware ---

hw_vcpus() {
  local n
  n=$(grep -c -E '^processor[[:space:]]*:' "$MYAI_SYSROOT/proc/cpuinfo" 2>/dev/null) || n=0
  if [ "$n" -lt 1 ]; then
    n=$(nproc 2>/dev/null) || n=1
  fi
  printf '%s\n' "$n"
}

hw_has_avx2() { grep -qw avx2 "$MYAI_SYSROOT/proc/cpuinfo" 2>/dev/null; }

hw_ram_kb() {
  awk '$1 == "MemTotal:" { print $2; found = 1 } END { exit !found }' "$MYAI_SYSROOT/proc/meminfo"
}

fmt_ram() { awk -v kb="${1:-0}" 'BEGIN { printf "%.1f GB", kb / 1048576 }'; }

# Threads for generating text: three quarters of the vCPUs, rounded, at least
# 2, never more than there are. The rest stays free for the other services.
compute_threads() {
  local n=$1 t
  t=$(((3 * n + 2) / 4))
  if [ "$t" -lt 2 ]; then t=2; fi
  if [ "$t" -gt "$n" ]; then t=$n; fi
  printf '%s\n' "$t"
}

# True on a machine that is run by systemd (not inside a plain container).
has_systemd() { [ -d "$MYAI_SYSROOT/run/systemd/system" ]; }

# One reading of the "cpu" line of /proc/stat: "total steal idle" in ticks.
cpu_sample() {
  awk '$1 == "cpu" { print $2 + $3 + $4 + $5 + $6 + $7 + $8 + $9, $9, $5 + $6; exit }' \
    "$MYAI_SYSROOT/proc/stat"
}

# cpu_percentages "total steal idle" "total steal idle": prints "steal busy"
# in percent for the time between two readings.
cpu_percentages() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    split(a, x, " "); split(b, y, " ")
    dt = y[1] - x[1]
    if (dt <= 0) { print "0.0 0.0"; exit }
    steal = (y[2] - x[2]) * 100 / dt
    busy = (dt - (y[3] - x[3]) - (y[2] - x[2])) * 100 / dt
    if (busy < 0) busy = 0
    printf "%.1f %.1f\n", steal, busy
  }'
}

# ---------------------------------------------------------------- network ---

valid_hostname() {
  [[ ${1:-} =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]
}

valid_email() {
  [[ ${1:-} =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]
}

is_ipv4() { [[ ${1:-} =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

# True for an IPv4 address that can be reached from the internet (not
# private, loopback, link-local, carrier NAT, multicast or reserved).
is_public_ipv4() {
  local a b c d
  [[ ${1:-} =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  a=$((10#${BASH_REMATCH[1]}))
  b=$((10#${BASH_REMATCH[2]}))
  c=$((10#${BASH_REMATCH[3]}))
  d=$((10#${BASH_REMATCH[4]}))
  if [ "$a" -gt 255 ] || [ "$b" -gt 255 ] || [ "$c" -gt 255 ] || [ "$d" -gt 255 ]; then return 1; fi
  if [ "$a" -eq 0 ] || [ "$a" -eq 10 ] || [ "$a" -eq 127 ] || [ "$a" -ge 224 ]; then return 1; fi
  if [ "$a" -eq 100 ] && [ "$b" -ge 64 ] && [ "$b" -le 127 ]; then return 1; fi
  if [ "$a" -eq 169 ] && [ "$b" -eq 254 ]; then return 1; fi
  if [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ]; then return 1; fi
  if [ "$a" -eq 192 ] && [ "$b" -eq 168 ]; then return 1; fi
  return 0
}

# The full name of this machine, lower case.
machine_fqdn() {
  local name
  name=$(hostname -f 2>/dev/null) || name=$(hostname 2>/dev/null) || name=""
  printf '%s\n' "${name,,}"
}

# This machine's public IPv4 address. Fails when none can be found.
#   1. the address it uses to reach the internet, when that one is public;
#   2. otherwise (behind NAT) the address a public resolver sees.
detect_public_ipv4() {
  local ip
  ip=$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }') || ip=""
  if is_public_ipv4 "$ip"; then
    printf '%s\n' "$ip"
    return 0
  fi
  ip=$(dig +short +time=3 +tries=1 TXT CH whoami.cloudflare @1.1.1.1 2>/dev/null |
    tr -d '"' | awk 'NR == 1') || ip=""
  if is_public_ipv4 "$ip"; then
    printf '%s\n' "$ip"
    return 0
  fi
  ip=$(curl -4 -fsS --max-time 8 https://icanhazip.com 2>/dev/null | tr -d '[:space:]') || ip=""
  if is_public_ipv4 "$ip"; then
    printf '%s\n' "$ip"
    return 0
  fi
  return 1
}

# The addresses public DNS has for NAME. Asks 1.1.1.1 directly: the machine's
# own resolver would answer from /etc/hosts.
dns_records() { # TYPE NAME
  dig +short +time=3 +tries=2 "$1" "$2" @1.1.1.1 2>/dev/null | grep -E '^[0-9a-fA-F:.]+$' || true
}

# True when public DNS says NAME is at the IPv4 address IP.
dns_points_to() {
  dns_records A "$1" | grep -qxF -- "$2"
}

# port_users tcp|udp PORT: the programs listening there, one per line
# ("?" when the name cannot be read).
port_users() {
  local flags=-Hltnp
  if [ "$1" = udp ]; then flags=-Hlunp; fi
  ss "$flags" "sport = :$2" 2>/dev/null | awk '
    {
      if (match($0, /users:\(\("[^"]+"/)) print substr($0, RSTART + 9, RLENGTH - 10)
      else print "?"
    }' | sort -u
}

# The Docker container that publishes a port, as "name (image)".
port_container() { # PORT PROTO
  command -v docker >/dev/null 2>&1 || return 0
  docker ps --filter "publish=$1/$2" --format '{{.Names}} ({{.Image}})' 2>/dev/null | awk 'NR == 1'
}

# True when the port is held by this stack's own proxy (the installer run again, or a health check).
port_is_ours() { # PORT PROTO
  command -v docker >/dev/null 2>&1 || return 1
  docker ps --filter "publish=$1/$2" --filter "label=com.docker.compose.project=myai" \
    --format '{{.Names}}' 2>/dev/null | grep -q .
}

# --------------------------------------------------------- Docker Compose ---

dc() {
  docker compose --project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml" \
    --env-file "$(env_file)" "$@"
}

valid_service() {
  case " $MYAI_SERVICES " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# svc_state SERVICE: one word.
#   healthy | starting | unhealthy   (the service has a health check)
#   running                          (it has none)
#   failing                          (it keeps crashing and being restarted)
#   exited | created | paused | dead | restarting | missing
svc_state() {
  local id out status health restarts
  id=$(dc ps -aq "$1" 2>/dev/null | awk 'NR == 1') || id=""
  if [ -z "$id" ]; then
    echo missing
    return 0
  fi
  out=$(docker inspect --format \
    '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}}' \
    "$id" 2>/dev/null) || out=""
  read -r status health restarts <<<"$out" || true
  case "${status:-missing}" in
    running)
      case "${health:-none}" in
        healthy | unhealthy) echo "$health" ;;
        starting)
          if [ "${restarts:-0}" -ge 3 ]; then echo failing; else echo starting; fi
          ;;
        *) echo running ;;
      esac
      ;;
    restarting)
      if [ "${restarts:-0}" -ge 3 ]; then echo failing; else echo restarting; fi
      ;;
    *) echo "${status:-missing}" ;;
  esac
}

svc_is_up() {
  case "$(svc_state "$1")" in
    healthy | starting | unhealthy | running) return 0 ;;
  esac
  return 1
}

# wait_healthy SERVICE SECONDS [what to call it]
# Returns 0 when healthy, 1 on timeout, 2 when the service stopped or keeps
# crashing. Prints a line every MYAI_PROGRESS_SECS seconds.
wait_healthy() {
  local svc=$1 limit=$2 what=${3:-$1} began=$SECONDS shown=0 elapsed state
  if [ "$DRY_RUN" = 1 ]; then
    note "would wait for $what (at most $(fmt_duration "$limit"))"
    return 0
  fi
  while :; do
    state=$(svc_state "$svc")
    case "$state" in
      healthy | running) return 0 ;;
      exited | dead | failing) return 2 ;;
    esac
    elapsed=$((SECONDS - began))
    if [ "$elapsed" -ge "$limit" ]; then return 1; fi
    if [ $((elapsed - shown)) -ge "$MYAI_PROGRESS_SECS" ]; then
      note "waiting for $what ... $(fmt_duration "$elapsed") so far"
      shown=$elapsed
    fi
    sleep "$MYAI_POLL_SECS"
  done
}

# "ghcr.io/x/y:tag" -> "ghcr.io/x/y"; Docker Hub prefixes are dropped because
# Docker leaves them out of the names it reports.
image_repo() {
  local ref=$1 last
  ref=${ref%%@*}
  last=${ref##*/}
  case "$last" in
    *:*) ref=${ref%:*} ;;
  esac
  ref=${ref#index.docker.io/}
  ref=${ref#docker.io/}
  ref=${ref#library/}
  printf '%s\n' "$ref"
}

# image_digest_ref IMAGE: prints "repo@sha256:..." for a pulled image. Fails
# when Docker knows no digest for it (a locally built image, for example).
image_digest_ref() {
  local ref=$1 repo line
  repo=$(image_repo "$ref")
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    if [ "$(image_repo "$line")" = "$repo" ]; then
      printf '%s\n' "$line"
      return 0
    fi
  done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" 2>/dev/null)
  return 1
}

# ------------------------------------------------- talking to the gateway ---
# The agent service is the only way to the model, and it publishes no port.
# These probes therefore run inside the agent container (it has Python and
# already holds AGENT_API_KEY, so no secret appears on a command line).

GATEWAY_CHAT_PY='
import json, os, sys, time, urllib.error, urllib.request
prompt, max_tokens, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
body = {"model": "assistant", "stream": False, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}]}
req = urllib.request.Request(
    "http://127.0.0.1:8090/v1/chat/completions", data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ.get("AGENT_API_KEY", "")})
began = time.time()
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
except urllib.error.HTTPError as exc:
    detail = exc.read(300).decode("utf-8", "replace").replace("\n", " ")
    print("error HTTP %s %s" % (exc.code, detail))
    sys.exit(1)
except Exception as exc:
    print("error " + str(exc).replace("\n", " ")[:300])
    sys.exit(1)
took = time.time() - began
timings = data.get("timings") or {}
usage = data.get("usage") or {}
tokens = timings.get("predicted_n") or usage.get("completion_tokens") or 0
speed = timings.get("predicted_per_second") or (tokens / took if took > 0 else 0)
if not tokens:
    print("error the model returned no text")
    sys.exit(1)
print("ok %.2f %.2f %d %.1f" % (speed, timings.get("prompt_per_second") or 0, tokens, took))
'

# gateway_chat PROMPT MAX_TOKENS [TIMEOUT_SECONDS]
# Prints "ok <tokens/s generating> <tokens/s reading> <tokens> <seconds>" or
# "error <reason>".
gateway_chat() {
  dc exec -T agent python -c "$GATEWAY_CHAT_PY" "$1" "$2" "${3:-900}" 2>&1
}

AGENT_TASK_PY='
import json, os, sys, time, urllib.error, urllib.request
prompt, limit = sys.argv[1], float(sys.argv[2])
base = "http://127.0.0.1:8090/agent/api"
head = {"Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ.get("AGENT_API_KEY", "")}
def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=head, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)
began = time.time()
try:
    task_id = call("POST", "/tasks", {"prompt": prompt})["id"]
    while True:
        task = call("GET", "/tasks/%s" % task_id)
        if task.get("status") in ("done", "error", "cancelled"):
            break
        if time.time() - began > limit:
            print("error the task did not finish in %d seconds" % limit)
            sys.exit(1)
        time.sleep(3)
except urllib.error.HTTPError as exc:
    print("error HTTP %s %s" % (exc.code, exc.read(300).decode("utf-8", "replace").replace("\n", " ")))
    sys.exit(1)
except Exception as exc:
    print("error " + str(exc).replace("\n", " ")[:300])
    sys.exit(1)
if task.get("status") != "done":
    print("error the task ended as %s: %s" % (task.get("status"), str(task.get("error") or "")[:200]))
    sys.exit(1)
steps = task.get("steps") or []
tools = sum(1 for s in steps if isinstance(s, dict) and s.get("t") == "tool")
print("ok %d %.1f" % (tools, time.time() - began))
'

# agent_task PROMPT [TIMEOUT_SECONDS]: runs one agent task to the end.
# Prints "ok <tool calls logged> <seconds>" or "error <reason>".
agent_task() {
  dc exec -T agent python -c "$AGENT_TASK_PY" "$1" "${2:-900}" 2>&1
}

# ---------------------------------------------------------- model download ---

file_size() { stat -c %s -- "$1" 2>/dev/null; }

# A GGUF model file starts with the four bytes "GGUF".
file_is_gguf() {
  [ -f "$1" ] && [ "$(head -c 4 -- "$1" | od -An -tx1 | tr -d ' \n')" = 47475546 ]
}

# model_file_complete PATH [BYTES]: the file exists, is a GGUF file and, when
# BYTES is known, has that size.
model_file_complete() {
  local path=$1 want=${2:-}
  file_is_gguf "$path" || return 1
  if [ -n "$want" ] && [ "$(file_size "$path")" != "$want" ]; then return 1; fi
  return 0
}

# hf_file_info REPO FILE: prints "<bytes> <sha256>" as Hugging Face reports
# them ("-" for a part it does not report). Fails when the answer cannot be
# fetched or does not list the file.
hf_file_info() {
  local repo=$1 file=$2 dir url json out
  dir=$(dirname "$file")
  url="$MYAI_HF_BASE/api/models/$repo/tree/main"
  if [ "$dir" != "." ]; then url="$url/$dir"; fi
  json=$(curl -fsSL --connect-timeout 15 --max-time 60 "$url" 2>/dev/null) || return 1
  out=$(printf '%s' "$json" | jq -r --arg f "$file" '
    if type == "array" then .[] else empty end
    | select(type == "object" and .path == $f)
    | "\(.size // "-") \(.lfs.oid // "-")"' 2>/dev/null | awk 'NR == 1') || return 1
  [ -n "$out" ] || return 1
  printf '%s\n' "$out"
}

download_progress() { # PART WANT
  local have
  have=$(file_size "$1") || have=0
  if [ -n "$2" ] && [ "$2" -gt 0 ]; then
    note "downloaded $(fmt_gb "$have") of $(fmt_gb "$2") ($((have * 100 / $2))%)"
  else
    note "downloaded $(fmt_gb "$have") so far"
  fi
}

# fetch_resumable URL PART WANT: one curl run that continues PART where it
# stopped. Returns curl's exit code and leaves the HTTP status in
# FETCH_HTTP_CODE and curl's error text in FETCH_ERROR. curl's own progress meter is replaced by plain lines,
# which also read well in a log file.
fetch_resumable() {
  local url=$1 part=$2 want=$3 codefile errfile ticker rc=0
  codefile=$(mktemp)
  errfile=$(mktemp)
  (
    while sleep "$MYAI_PROGRESS_SECS" >/dev/null 2>&1; do
      kill -0 "$$" 2>/dev/null || exit 0
      download_progress "$part" "$want"
    done
  ) &
  ticker=$!
  # Give up on a connection that moves less than 1 kB/s for two minutes; the
  # caller then tries again and continues from the same place.
  curl -fL -sS -C - --connect-timeout 30 --speed-limit 1024 --speed-time 120 \
    -o "$part" -w '%{http_code}' "$url" >"$codefile" 2>"$errfile" || rc=$?
  kill "$ticker" 2>/dev/null || true
  wait "$ticker" 2>/dev/null || true
  FETCH_HTTP_CODE=$(cat "$codefile" 2>/dev/null) || FETCH_HTTP_CODE=""
  # curl's own words for what went wrong, for the message to the user.
  FETCH_ERROR=$(awk 'NF { line = $0 } END { sub(/^curl: \([0-9]+\) /, "", line); print line }' "$errfile" 2>/dev/null) || FETCH_ERROR=""
  rm -f "$codefile" "$errfile"
  return "$rc"
}

# download_verified URL DEST BYTES SHA256
# BYTES and SHA256 may be empty when unknown. Downloads to DEST.part,
# continues an earlier partial download, checks size, file type and (when a
# checksum is known) SHA-256, and only then gives the file its final name.
# A failed check is fatal and removes the bad file.
download_verified() {
  local url=$1 dest=$2 want=$3 sha=$4 part="$2.part" have before tries=0 rc got name
  name=$(basename "$dest")

  if [ -e "$dest" ]; then
    if model_file_complete "$dest" "$want"; then
      note "Already downloaded: $name"
      return 0
    fi
    # Not something this code wrote (it renames only complete files), so it
    # cannot be continued safely.
    warn "$name exists but is incomplete or not a model file. Downloading it again."
    rm -f -- "$dest"
  fi

  if [ -f "$part" ] && [ -n "$want" ]; then
    have=$(file_size "$part") || have=0
    if [ "$have" -gt "$want" ]; then rm -f -- "$part"; fi
  fi
  if [ -f "$part" ]; then
    note "Continuing an earlier download ($(fmt_gb "$(file_size "$part")") already here)."
  fi

  while :; do
    before=$(file_size "$part") || before=0
    if [ -n "$want" ] && [ "$before" = "$want" ]; then break; fi
    rc=0
    fetch_resumable "$url" "$part" "$want" || rc=$?
    if [ "$rc" -eq 0 ]; then break; fi
    case "$FETCH_HTTP_CODE" in
      416) break ;; # nothing left to fetch: the partial file is already whole
      401 | 403 | 404)
        die "The model file could not be downloaded (the server answered $FETCH_HTTP_CODE)." \
          "Address: $url" \
          "The file may have been renamed or removed by its publisher." \
          "Choose another model: myai model list"
        ;;
    esac
    have=$(file_size "$part") || have=0
    if [ "$have" -gt "$before" ]; then tries=0; fi # it moved forward: keep going
    tries=$((tries + 1))
    if [ "$tries" -ge "$MYAI_DOWNLOAD_TRIES" ]; then
      die "The download failed $tries times in a row (${FETCH_ERROR:-curl error $rc})." \
        "What is here so far is kept. Run the same command again to continue." \
        "If it keeps failing, check the internet connection: curl -I https://huggingface.co"
    fi
    note "The download stopped (${FETCH_ERROR:-curl error $rc}). Trying again in $MYAI_RETRY_SECS seconds; it continues where it stopped."
    sleep "$MYAI_RETRY_SECS"
  done

  have=$(file_size "$part") || have=0
  if [ -n "$want" ] && [ "$have" != "$want" ]; then
    rm -f -- "$part"
    die "The downloaded file has the wrong size ($have bytes, expected $want)." \
      "It was removed. The model was probably replaced by its publisher." \
      "Run the same command again. If the size is wrong again, use another model: myai model list"
  fi
  if ! file_is_gguf "$part"; then
    rm -f -- "$part"
    die "The downloaded file is not a model file (it does not start with GGUF)." \
      "It was removed. Something between this server and Hugging Face returned a different page." \
      "Run the same command again in a few minutes."
  fi
  if [ -n "$sha" ]; then
    note "Checking the file against its SHA-256 checksum. This takes a minute or two."
    got=$(sha256sum -- "$part" | awk '{ print $1 }')
    if [ "$got" != "$sha" ]; then
      rm -f -- "$part"
      die "The downloaded file is damaged: its checksum does not match the one Hugging Face reports." \
        "It was removed. Run the same command again to download it afresh."
    fi
    note "Checksum correct."
  else
    warn "Hugging Face did not report a checksum for this file, so that check was skipped. Size and file type are correct."
  fi
  chmod 644 "$part"
  mv -f -- "$part" "$dest"
}

# download_model HF_REPO FILE BYTES MODELS_DIR
# BYTES may be empty (a model that is not in the catalogue); the size
# Hugging Face reports is used then.
download_model() {
  local repo=$1 file=$2 want=$3 dir=$4 dest info size="" sha="" url
  dest="$dir/$(basename "$file")"
  url="$MYAI_HF_BASE/$repo/resolve/main/$file?download=true"

  if model_file_complete "$dest" "$want"; then
    note "The model file is already here: $dest"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    note "would download $url"
    note "  to $dest${want:+ ($(fmt_gb "$want"))}, continuing a partial download if there is one,"
    note "  then check its size, that it is a GGUF file, and its SHA-256 checksum"
    return 0
  fi

  if info=$(hf_file_info "$repo" "$file"); then
    read -r size sha <<<"$info" || true
    [[ $size =~ ^[0-9]+$ ]] || size=""
    [[ $sha =~ ^[0-9a-f]{64}$ ]] || sha=""
  fi
  if [ -z "$want" ]; then want=$size; fi

  mkdir -p "$dir"
  note "Downloading $(basename "$file")${want:+ ($(fmt_gb "$want"))} from Hugging Face."
  note "A progress line appears every $MYAI_PROGRESS_SECS seconds. If this stops, run the same command again: it continues."
  download_verified "$url" "$dest" "$want" "$sha"
  note "Model file ready: $dest"
}

# ------------------------------------------------------ links and secrets ---

entry_link() {
  printf 'https://%s/enter/%s\n' "$(env_get SITE_ADDRESS)" "$(env_get GATE_TOKEN)"
}

# A QR code in the terminal, when the qrencode tool is installed.
print_qr() {
  if command -v qrencode >/dev/null 2>&1; then
    qrencode -t ANSIUTF8 -m 2 -- "$1" 2>/dev/null || true
  fi
}

# The text of the credentials sheet, built from the settings file.
credentials_text() {
  local site tls email password dash code
  site=$(env_get SITE_ADDRESS) || site="?"
  tls=$(env_get CADDY_TLS_LINE) || tls=""
  email=$(env_get WEBUI_ADMIN_EMAIL) || email="?"
  password=$(env_get WEBUI_ADMIN_PASSWORD) || password="?"
  dash=$(env_get DASHBOARD_PASSWORD) || dash=""
  code=$(env_get PAIRING_CODE) || code=""
  cat <<EOF
1. Open this link once on each phone or computer you want to use.
   It is your key to the server, so keep it to yourself:

     $(entry_link)

2. Log in to the chat with:

     E-mail:    $email
     Password:  $password

   (This is the first password. If you change it in the chat page, the new
   one counts there.)

3. The agent dashboard (tasks, schedules, notes):

     https://$site/agent/
     Password:  ${dash:-(password login is switched off)}

4. Telegram (optional):
     a. In Telegram, write to @BotFather, send /newbot and copy the token it gives you.
     b. On this server run:   myai telegram set
     c. Send your new bot this message:   /pair $code
EOF
  if [ -n "$tls" ]; then
    cat <<EOF

Note: this server uses a self-signed certificate. Your browser will show a
warning the first time; choose "Advanced" and continue.
EOF
  fi
}

# ------------------------------------------------------------------- lock ---

# One myai command that changes things at a time. The lock is released when
# the process (and anything it starts) ends.
acquire_lock() {
  local mode=${1:-wait}
  exec 9>"$APP_DIR/.lock"
  if flock -n 9; then return 0; fi
  if [ "$mode" = try ]; then return 1; fi
  die "Another myai command is still working (a backup, an update or a benchmark)." \
    "Wait a few minutes and try again."
}
