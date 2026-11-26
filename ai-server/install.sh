#!/usr/bin/env bash
# myai installer: a private web chat and an always-on agent on one server,
# served by a local model. Nothing leaves the machine.
#
#   curl -fsSL <raw url>/ai-server/install.sh | bash
#   curl -fsSL <raw url>/ai-server/install.sh | bash -s -- --domain chat.example.com
#
# Safe to run again: the settings file, the secrets and all data are kept.
#
# Everything is inside functions and main() is called on the last line, so a
# download that was cut off cannot run half an installation. Tests load the
# functions without running anything: MYAI_SOURCE_ONLY=1 source install.sh

set -euo pipefail

MYAI_DEFAULT_REPO="luccadlfn-spec/luccadlfn-spec.github.io"
MYAI_DEFAULT_BRANCH="ccr-f9008ee5-63plj0"

# Test hook: a directory that stands in for "/" (see bin/myai-lib.sh).
: "${MYAI_SYSROOT:=}"

STEP_TOTAL=11
STEP_NO=0
STEP_NAME="getting ready"
SRC_DIR=""
STAGE_DIR=""
FAILED_AT=""
APT_UPDATED=0
STACK_STARTED=0
SELFTEST_FAILED=0
SPEED_TEXT=""
SITE_CHOSEN=0
SITE_ADDRESS=""
SITE_REASON=""
CADDY_TLS_LINE=""
PUBLIC_IPV4=""
LLAMA_UPDATE_TAG="" # with --update: the tag whose newer build is tried
LLAMA_TRY=""        # the model server build this run tries (repo@sha256:...)
REPLACED_LLAMA=""   # the build that a newer one replaced
REPLACED_IMAGES=()  # image versions that this update replaced
HARDEN_FAILED=()
# -g: also global when this file is sourced from inside a function (the tests).
declare -gA PLAN=()

usage() {
  cat <<'EOF'
myai installer

  install.sh [options]

Options:
  --domain <host>        address of this server, for example chat.example.com
                         (default: worked out automatically)
  --model <id>           model to install: hauhau, heretic, huihui, classic or fast
                         (default: chosen by how much memory the server has)
  --admin-email <e>      your login name for the chat page (default: owner@example.com).
                         A real address is also the contact for this server's
                         HTTPS certificate at Let's Encrypt and ZeroSSL, so that
                         the second can step in when the first fails.
                         myai itself never sends e-mail.
  --governor <on|off>    protect the CPU credits of a Hostinger server
                         (default: on when this is a Hostinger server)
  --tls-internal         use a self-signed certificate (for testing)
  --no-harden            leave swap, firewall, fail2ban and automatic updates alone
  --skip-model-download  do not download the model file
  --dir <path>           where to install (default: /opt/myai)
  --yes                  ask no questions, use the defaults
  --dry-run              show what would be done and change nothing
  --update               fetch the newest app files and restart (what "myai update" runs)
  --help                 show this text

The same settings as environment variables: MYAI_DOMAIN, MYAI_MODEL,
MYAI_ADMIN_EMAIL, MYAI_GOVERNOR, MYAI_DIR, and with the value 1:
MYAI_TLS_INTERNAL, MYAI_NO_HARDEN, MYAI_SKIP_MODEL_DOWNLOAD, MYAI_YES,
MYAI_DRY_RUN. MYAI_REPO and MYAI_REF choose where the app files come from.

It is safe to run the installer again. It keeps your settings and data.
EOF
}

# ------------------------------------------------------------- bootstrap ---
# Used before the shared library is loaded.

boot_fail() {
  local line
  printf '\nStopped: %s\n' "$1" >&2
  shift
  for line in "$@"; do printf '  %s\n' "$line" >&2; done
  exit 1
}

# The directory this script file is in. Prints nothing when the script
# arrives on stdin (curl | bash); bash then reports a made-up file name, so
# the file must also look like this script.
script_dir() {
  local src=${BASH_SOURCE[0]:-}
  if [ -n "$src" ] && [ -f "$src" ] && grep -q '^MYAI_DEFAULT_REPO=' "$src" 2>/dev/null; then
    (cd "$(dirname "$src")" && pwd -P)
  fi
}

# fetch_app_files DIR: download the repository as a tarball and unpack only
# its ai-server/ part into DIR. With MYAI_REF set, only that ref is tried;
# otherwise the branch this script was written on, then main.
fetch_app_files() {
  local dir=$1 repo refs ref url tarball
  repo=${MYAI_REPO:-$MYAI_DEFAULT_REPO}
  [[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
    boot_fail "MYAI_REPO must look like owner/name, not '$repo'."
  if [ -n "${MYAI_REF:-}" ]; then
    [[ $MYAI_REF =~ ^[A-Za-z0-9_./-]+$ ]] || boot_fail "MYAI_REF contains characters that are not allowed."
    refs=$MYAI_REF
  else
    refs="$MYAI_DEFAULT_BRANCH main"
  fi
  tarball="$dir/app.tar.gz"
  for ref in $refs; do
    url="https://codeload.github.com/$repo/tar.gz/$ref"
    if curl -fsSL --retry 3 --retry-delay 3 --connect-timeout 20 -o "$tarball" "$url" 2>/dev/null &&
      tar -xzf "$tarball" -C "$dir" --no-same-owner --strip-components=2 \
        --wildcards '*/ai-server/*' 2>/dev/null &&
      [ -f "$dir/docker-compose.yml" ] && [ -f "$dir/bin/myai-lib.sh" ] && [ -f "$dir/install.sh" ]; then
      rm -f "$tarball"
      FETCHED_REF=$ref
      return 0
    fi
  done
  rm -f "$tarball"
  return 1
}

# Decide where the app files come from. Next to this script when it runs from
# a copy of the repository; otherwise they are downloaded to a temporary
# directory and the installer in that download takes over, so the installer,
# the compose file and the model list always belong to the same version.
locate_source() {
  local here stage
  here=$(script_dir)
  if [ -n "${MYAI_STAGED:-}" ]; then
    SRC_DIR=$here
    STAGE_DIR=$MYAI_STAGED
    return 0
  fi
  if [ "$PRE_UPDATE" != 1 ] && [ -n "$here" ] && [ -f "$here/docker-compose.yml" ] &&
    [ -f "$here/bin/myai-lib.sh" ]; then
    SRC_DIR=$here
    return 0
  fi
  if [ "$PRE_UPDATE" = 1 ] && [ -n "$here" ] && [ -e "$here/../.git" ]; then
    boot_fail "This copy of myai is a git checkout, so it is updated with git." \
      "Run: git -C '$here' pull   and then: bash '$here/install.sh'"
  fi
  command -v curl >/dev/null 2>&1 || boot_fail "The program 'curl' is missing." "Install it: apt-get install -y curl"
  command -v tar >/dev/null 2>&1 || boot_fail "The program 'tar' is missing." "Install it: apt-get install -y tar"
  printf 'Downloading the app files ...\n'
  stage=$(mktemp -d "${TMPDIR:-/tmp}/myai-stage.XXXXXX")
  if ! fetch_app_files "$stage"; then
    rm -rf "$stage"
    boot_fail "The app files could not be downloaded from GitHub." \
      "Check the internet connection: curl -I https://codeload.github.com" \
      "Then run the same command again."
  fi
  export MYAI_STAGED=$stage
  export MYAI_REF=${MYAI_REF:-$FETCHED_REF}
  exec bash "$stage/install.sh" "$@"
}

# --------------------------------------------------------------- options ---

truthy() {
  case "${1:-}" in
    1 | true | TRUE | yes | YES | on | ON) return 0 ;;
  esac
  return 1
}

# Options that matter before the library is loaded.
prescan() {
  local arg
  PRE_UPDATE=0
  for arg in "$@"; do
    case "$arg" in
      -h | --help)
        usage
        exit 0
        ;;
      --update) PRE_UPDATE=1 ;;
    esac
  done
}

parse_args() {
  local arg args=() ids
  OPT_DOMAIN=${MYAI_DOMAIN:-}
  OPT_MODEL=${MYAI_MODEL:-}
  OPT_ADMIN_EMAIL=${MYAI_ADMIN_EMAIL:-}
  OPT_GOVERNOR=${MYAI_GOVERNOR:-}
  OPT_TLS_INTERNAL=0
  OPT_NO_HARDEN=0
  OPT_SKIP_MODEL=0
  OPT_UPDATE=0
  APP_DIR=${MYAI_DIR:-/opt/myai}
  if truthy "${MYAI_TLS_INTERNAL:-}"; then OPT_TLS_INTERNAL=1; fi
  if truthy "${MYAI_NO_HARDEN:-}"; then OPT_NO_HARDEN=1; fi
  if truthy "${MYAI_SKIP_MODEL_DOWNLOAD:-}"; then OPT_SKIP_MODEL=1; fi
  if truthy "${MYAI_YES:-}"; then ASSUME_YES=1; fi
  if truthy "${MYAI_DRY_RUN:-}"; then DRY_RUN=1; fi

  # "--name=value" is the same as "--name value".
  for arg in "$@"; do
    case "$arg" in
      --*=*) args+=("${arg%%=*}" "${arg#*=}") ;;
      *) args+=("$arg") ;;
    esac
  done
  set -- ${args[@]+"${args[@]}"}

  while [ $# -gt 0 ]; do
    case "$1" in
      --domain | --model | --admin-email | --governor | --dir)
        if [ $# -lt 2 ] || [ -z "$2" ]; then
          die "The option $1 needs a value." "Example: $1 $(option_example "$1")"
        fi
        case "$1" in
          --domain) OPT_DOMAIN=$2 ;;
          --model) OPT_MODEL=$2 ;;
          --admin-email) OPT_ADMIN_EMAIL=$2 ;;
          --governor) OPT_GOVERNOR=$2 ;;
          --dir) APP_DIR=$2 ;;
        esac
        shift 2
        ;;
      --tls-internal)
        OPT_TLS_INTERNAL=1
        shift
        ;;
      --no-harden)
        OPT_NO_HARDEN=1
        shift
        ;;
      --skip-model-download)
        OPT_SKIP_MODEL=1
        shift
        ;;
      --update)
        OPT_UPDATE=1
        shift
        ;;
      -y | --yes)
        ASSUME_YES=1
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      *)
        die "Unknown option: $1" "See the options with: install.sh --help"
        ;;
    esac
  done

  OPT_DOMAIN=${OPT_DOMAIN,,}
  if [ -n "$OPT_DOMAIN" ] && ! valid_hostname "$OPT_DOMAIN"; then
    die "'$OPT_DOMAIN' is not a valid address for --domain." \
      "Give only the name, without https:// and without a path. Example: --domain chat.example.com"
  fi
  if [ -n "$OPT_ADMIN_EMAIL" ] && ! valid_email "$OPT_ADMIN_EMAIL"; then
    die "'$OPT_ADMIN_EMAIL' does not look like an e-mail address." "Example: --admin-email you@example.com"
  fi
  case "$OPT_GOVERNOR" in
    '' | on | off) ;;
    *) die "--governor must be on or off, not '$OPT_GOVERNOR'." ;;
  esac
  if [[ ! $APP_DIR =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] || [[ "$APP_DIR/" == */./* ]] ||
    [[ "$APP_DIR/" == */../* ]]; then
    die "'$APP_DIR' cannot be used with --dir." \
      "Use a full path made of letters, digits, dots and dashes, at least two levels deep." \
      "Example: --dir /opt/myai"
  fi
  # With --update the hardening of the first install is left as it is.
  if [ "$OPT_UPDATE" = 1 ]; then OPT_NO_HARDEN=1; fi
  # How other myai commands name this run while it holds the lock. They wait
  # for an update, but not for an installation, which can take an hour.
  if [ "$OPT_UPDATE" = 1 ]; then MYAI_LOCK_NAME="myai update"; else MYAI_LOCK_NAME="the installer"; fi

  ENV_FILE="$APP_DIR/.env"
  MODELS_CONF="$SRC_DIR/config/models.conf"
  LOG_FILE="$MYAI_SYSROOT/var/log/myai-install.log"
  # Advice when a model cannot be downloaded. The myai command that the
  # library suggests does not exist yet during the first installation.
  ids=$(catalog_ids | paste -sd' ' -) || ids=""
  MODEL_CHOICE_HINT="Choose another model (${ids:-see config/models.conf}) and run the install command again with it at the end:  ... | bash -s -- --model <id>"
}

option_example() {
  case "$1" in
    --domain) echo "chat.example.com" ;;
    --model) echo "hauhau" ;;
    --admin-email) echo "you@example.com" ;;
    --governor) echo "on" ;;
    --dir) echo "/opt/myai" ;;
  esac
}

# ------------------------------------------------------ progress, errors ---

step() {
  STEP_NO=$((STEP_NO + 1))
  STEP_NAME=$1
  printf '\n[%d/%d] %s\n' "$STEP_NO" "$STEP_TOTAL" "$1"
}

on_exit() {
  local rc=$?
  case "$STAGE_DIR" in
    */myai-stage.*)
      if [ "$STAGE_DIR" -ef "$SRC_DIR" ]; then rm -rf "$STAGE_DIR"; fi
      ;;
  esac
  if [ "$rc" -eq 0 ]; then return 0; fi
  case "$rc" in
    129 | 130 | 143) # the terminal closed, Ctrl+C, or stopped from outside: nothing failed
      if [ "$rc" = 129 ]; then
        printf '\nStopped in step [%s/%s] %s: the terminal was closed.\n' "$STEP_NO" "$STEP_TOTAL" "$STEP_NAME" >&2
      else
        printf '\nStopped as asked, in step [%s/%s] %s.\n' "$STEP_NO" "$STEP_TOTAL" "$STEP_NAME" >&2
      fi
      printf '  Nothing you already have is lost. To go on, run the same command again;\n' >&2
      ;;
    *)
      if [ "${MYAI_DIE_SHOWN:-0}" != 1 ]; then
        printf '\nStopped: something failed in step [%s/%s] %s.\n' "$STEP_NO" "$STEP_TOTAL" "$STEP_NAME" >&2
        if [ -n "$FAILED_AT" ]; then printf '  The command that failed: %s\n' "$FAILED_AT" >&2; fi
      fi
      printf '  Nothing you already have is lost. After fixing the problem, run the same command again;\n' >&2
      ;;
  esac
  printf '  finished steps are quick the second time.\n' >&2
}

# run_logged CMD...: like run, but the command's own chatter goes to the log
# file. When it fails, its last lines are shown.
run_logged() {
  local rc
  if [ "$DRY_RUN" = 1 ]; then
    printf '  would run: %s\n' "$(quote_cmd "$@")"
    return 0
  fi
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  printf '\n$ %s\n' "$(quote_cmd "$@")" >>"$LOG_FILE" 2>/dev/null || true
  if "$@" >>"$LOG_FILE" 2>&1; then return 0; else rc=$?; fi
  warn "This command failed (exit code $rc): $(quote_cmd "$@")"
  printf '  Its last messages (everything is in %s):\n' "$LOG_FILE" >&2
  tail -n 12 "$LOG_FILE" 2>/dev/null | sed 's/^/      | /' >&2 || true
  return "$rc"
}

# docker compose for this project; with --dry-run only shown.
run_dc() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  would run: docker compose %s   (in %s)\n' "$(quote_cmd "$@")" "$APP_DIR"
    return 0
  fi
  dc "$@"
}

# problem "what is wrong" ["what to do" ...]: a failed check. Stops the
# installer. With --dry-run it is reported and the run goes on, so that the
# whole plan can be seen.
problem() {
  local line
  if [ "$DRY_RUN" != 1 ]; then die "$@"; fi
  PREFLIGHT_PROBLEMS=$((PREFLIGHT_PROBLEMS + 1))
  printf '  PROBLEM (a real run stops here): %s\n' "$1"
  shift
  for line in "$@"; do printf '      %s\n' "$line"; done
}

# ------------------------------------------------------- step 1: checks ---

check_root() {
  if [ "$(id -u)" -eq 0 ]; then return 0; fi
  problem "This installer must run as the user root." \
    "On Hostinger: open the browser terminal in hPanel (it is root already) and paste the command there." \
    "Elsewhere: run   sudo -i   first, then paste the command again."
}

os_field() { # KEY FILE
  awk -F= -v k="$1" '
    $1 == k && !found { v = substr($0, length(k) + 2); gsub(/^["\047]|["\047]$/, "", v); print v; found = 1 }' "$2"
}

read_os() {
  local file="$MYAI_SYSROOT/etc/os-release"
  OS_ID=""
  OS_VERSION=""
  OS_CODENAME=""
  OS_NAME="an unknown system"
  [ -r "$file" ] || return 0
  OS_ID=$(os_field ID "$file")
  OS_VERSION=$(os_field VERSION_ID "$file")
  OS_CODENAME=$(os_field VERSION_CODENAME "$file")
  OS_NAME=$(os_field PRETTY_NAME "$file")
  [ -n "$OS_NAME" ] || OS_NAME="$OS_ID $OS_VERSION"
}

check_os() {
  local arch
  read_os
  case "$OS_ID:$OS_VERSION" in
    ubuntu:22.04 | ubuntu:24.04 | debian:12)
      note "System: $OS_NAME"
      ;;
    ubuntu:2[5-9].* | ubuntu:[3-9][0-9].* | debian:1[3-9])
      note "System: $OS_NAME"
      warn "This version was not tested (tested: Ubuntu 22.04, Ubuntu 24.04, Debian 12). Trying anyway."
      ;;
    *)
      problem "This installer works on Ubuntu 22.04, Ubuntu 24.04 and Debian 12. This server runs $OS_NAME." \
        "On Hostinger: hPanel, VPS, OS & Panel, Operating System, choose Ubuntu 24.04." \
        "Careful: changing the operating system erases everything on the server."
      ;;
  esac
  arch=$(uname -m)
  if [ "$arch" != x86_64 ]; then
    problem "This installer needs a 64-bit Intel or AMD processor (x86_64). This server has: $arch." \
      "Use a server with an Intel or AMD processor."
  fi
}

check_cpu() {
  VCPUS=$(hw_vcpus)
  if hw_has_avx2; then
    note "Processor: $VCPUS vCPUs, AVX2 available"
  else
    note "Processor: $VCPUS vCPUs"
    problem "The processor of this server lacks AVX2, which the model needs to run at a usable speed." \
      "Use a newer server. Every current Hostinger KVM plan has AVX2."
  fi
  if [ "$VCPUS" -lt 4 ]; then
    warn "Only $VCPUS vCPUs. It will work, but answers will be slow. 8 vCPUs are recommended."
  fi
}

# Sets MODEL_ID, MODEL_FILE and, for a catalogue model, MODEL_BYTES. Order: --model, then what the settings file already says,
# then the first catalogue entry that fits the memory.
select_model() {
  local existing_file ids min
  RAM_KB=$(hw_ram_kb) || RAM_KB=0
  MODEL_ID=""
  MODEL_FILE=""
  MODEL_BYTES=""
  MODEL_WHY=""
  ids=$(catalog_ids | paste -sd' ' -)

  if [ -n "$OPT_MODEL" ]; then
    if catalog_field "$OPT_MODEL" 1 >/dev/null; then
      MODEL_ID=$OPT_MODEL
      MODEL_WHY="chosen with --model"
    else
      problem "There is no model called '$OPT_MODEL'." "Choose one of: $ids"
    fi
  fi
  if [ -z "$MODEL_ID" ] && [ -z "$OPT_MODEL" ] && existing_file=$(env_get MODEL_FILE) && [ -n "$existing_file" ]; then
    MODEL_ID=$(env_get MODEL_ID) || MODEL_ID=custom
    MODEL_FILE=$existing_file
    MODEL_WHY="kept from the existing settings"
    # Download details are only known when the file is the catalogue's file.
    if [ "$(catalog_field "$MODEL_ID" 4 2>/dev/null)" = "$MODEL_FILE" ]; then
      MODEL_BYTES=$(catalog_field "$MODEL_ID" 5)
    fi
    return 0
  fi
  if [ -z "$MODEL_ID" ]; then
    if MODEL_ID=$(catalog_pick_for_ram "$RAM_KB"); then
      MODEL_WHY="chosen for $(fmt_ram "$RAM_KB") of memory"
    else
      problem "This server has $(fmt_ram "$RAM_KB") of memory. The smallest model needs about 6 GB." \
        "Use a server with more memory. 32 GB is recommended."
      MODEL_ID=$(catalog_ids | tail -n 1)
      MODEL_WHY="the smallest one"
    fi
  fi
  MODEL_FILE=$(catalog_field "$MODEL_ID" 4)
  MODEL_BYTES=$(catalog_field "$MODEL_ID" 5)
  min=$(catalog_field "$MODEL_ID" 2)
  if ! ram_fits "$RAM_KB" "$min"; then
    warn "The model '$MODEL_ID' is meant for servers with at least $min GB of memory; this one has $(fmt_ram "$RAM_KB"). It may be very slow or fail to start."
  fi
}

check_memory() {
  select_model
  note "Memory: $(fmt_ram "$RAM_KB")"
  note "Model: $MODEL_ID ($MODEL_WHY)${MODEL_BYTES:+, $(fmt_gb "$MODEL_BYTES")}"
}

# Free space needed: what is left of the model download, room for the
# container images and data (15 GB the first time), and the swap file.
check_disk() {
  local dir=$APP_DIR data models avail_kb need have_part=0 left=0 base swap=0
  data=$(env_get DATA_DIR) || data="$APP_DIR/data"
  models="$data/models"
  if [ "$OPT_SKIP_MODEL" != 1 ] && [ -n "$MODEL_BYTES" ] && ! model_file_complete "$models/$MODEL_FILE" "$MODEL_BYTES"; then
    have_part=$(file_size "$models/$MODEL_FILE.part") || have_part=0
    left=$((MODEL_BYTES - have_part))
    if [ "$left" -lt 0 ]; then left=$MODEL_BYTES; fi
  fi
  if [ -f "$ENV_FILE" ]; then base=3000000000; else base=15000000000; fi
  if [ "$OPT_NO_HARDEN" != 1 ] && [ ! -e "$MYAI_SYSROOT/swapfile" ] &&
    [ -z "$(swapon --show --noheadings 2>/dev/null || true)" ]; then
    swap=8589934592
  fi
  need=$((left + base + swap))
  while [ ! -d "$dir" ]; do dir=$(dirname "$dir"); done
  avail_kb=$(df -Pk "$dir" 2>/dev/null | awk 'NR == 2 { print $4 }') || avail_kb=""
  if [[ ! $avail_kb =~ ^[0-9]+$ ]]; then
    warn "Could not find out how much disk space is free. Continuing."
    return 0
  fi
  if [ $((avail_kb * 1024)) -lt "$need" ]; then
    problem "Not enough free disk space: $(fmt_gb $((avail_kb * 1024))) free, about $(fmt_gb "$need") needed." \
      "That is $(fmt_gb "$left") for the model, $(fmt_gb "$base") for programs and data, $(fmt_gb "$swap") for swap." \
      "Free some space (see what is large: du -xh / | sort -h | tail -20) or use a smaller model: --model fast"
  else
    note "Disk: $(fmt_gb $((avail_kb * 1024))) free, about $(fmt_gb "$need") needed"
  fi
}

# Adds a line to the advice of check_ports unless it is there already: one
# program usually holds both ports.
add_advice() {
  local line
  for line in ${advice[@]+"${advice[@]}"}; do
    if [ "$line" = "$1" ]; then return 0; fi
  done
  advice+=("$1")
}

check_ports() {
  local spec proto port users holder busy=() advice=() traefik=0 unit
  if ! command -v ss >/dev/null 2>&1; then
    warn "Could not check whether ports 80 and 443 are free (the tool 'ss' is missing)."
    return 0
  fi
  for spec in tcp:80 tcp:443 udp:443; do
    proto=${spec%%:*}
    port=${spec##*:}
    if port_is_ours "$port" "$proto"; then continue; fi
    users=$(port_users "$proto" "$port" | paste -sd' ' -) || users=""
    holder=$(port_container "$port" "$proto") || holder=""
    if [ -z "$users" ] && [ -z "$holder" ]; then continue; fi
    if [ -n "$holder" ]; then
      busy+=("Port $port ($proto) is used by the Docker container $holder.")
      case "${holder,,}" in
        *traefik*) traefik=1 ;;
      esac
      add_advice "  docker stop ${holder%% *} && docker update --restart=no ${holder%% *}"
    else
      busy+=("Port $port ($proto) is used by the program: $users")
      for unit in nginx apache2 caddy lighttpd haproxy; do
        case " $users " in
          *" $unit "*) add_advice "  systemctl disable --now $unit" ;;
        esac
      done
    fi
  done
  if [ ${#busy[@]} -eq 0 ]; then
    note "Ports 80 and 443 are free (or already used by myai)"
    return 0
  fi
  if [ "$traefik" = 1 ]; then
    busy+=("That is Traefik, the proxy which Hostinger's Docker template and one-click apps set up."
      "myai brings its own proxy, and two proxies cannot share ports 80 and 443."
      "Remove that project in hPanel (VPS, Docker Manager), or stop it here:")
  else
    busy+=("myai needs ports 80 and 443 for its own web proxy. Stop the other program first:")
  fi
  if [ ${#advice[@]} -eq 0 ]; then advice=("  (find it with: ss -ltnp | grep -E ':(80|443) ')"); fi
  problem "Ports 80 and 443 must be free." "${busy[@]}" "${advice[@]}" "Then run the installer again."
}

# Two installers at the same time would replace each other's files (the
# Hostinger post-install script can still be working when the install
# command is pasted into the browser terminal as well).
check_not_installing() {
  if ! lock_is_held; then return 0; fi
  read_lock_holder
  if [ "$LOCK_WHAT" = "the installer" ]; then
    problem "The installer is already running (process $LOCK_PID, since $LOCK_SINCE)." \
      "Wait until it has finished; there is no need to start it twice." \
      "If the Hostinger post-install script started it, watch it with: tail -f /root/myai-install.log"
  fi
}

preflight() {
  PREFLIGHT_PROBLEMS=0
  check_root
  check_not_installing
  check_os
  check_cpu
  check_memory
  check_disk
  check_ports
  if [ "$PREFLIGHT_PROBLEMS" -gt 0 ]; then
    note "(dry run) $PREFLIGHT_PROBLEMS check(s) above would stop a real run."
  fi
}

# Questions are asked once, here, before the long steps. Without a terminal,
# or with --yes, the defaults are used.
collect_answers() {
  local answer tries=0
  ADMIN_EMAIL=$OPT_ADMIN_EMAIL
  if [ -n "$ADMIN_EMAIL" ] || env_has WEBUI_ADMIN_EMAIL; then return 0; fi
  ADMIN_EMAIL="owner@example.com"
  if ! can_ask; then return 0; fi
  printf '\n  One question. Your answer is the login name for the chat page. A real\n' >/dev/tty
  printf "  address is also given to Let's Encrypt and ZeroSSL, the free services that\n" >/dev/tty
  printf '  issue the HTTPS certificate, as its contact. myai itself never sends e-mail.\n' >/dev/tty
  while [ "$tries" -lt 3 ]; do
    tries=$((tries + 1))
    answer=$(ask "  E-mail address for your login (press Enter for owner@example.com): " "owner@example.com")
    if valid_email "$answer"; then
      ADMIN_EMAIL=$answer
      return 0
    fi
    printf '  That does not look like an e-mail address. Try again.\n' >/dev/tty
  done
  note "Using owner@example.com."
}

# ----------------------------------------------------- step 2: packages ---

apt_update() {
  local try
  if [ "$APT_UPDATED" = 1 ]; then return 0; fi
  for try in 1 2 3 4 5 6; do
    if run_logged env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update -q; then
      APT_UPDATED=1
      return 0
    fi
    if [ "$try" -lt 6 ]; then
      note "The package system is busy or unreachable. Trying again in 20 seconds (try $try of 6)."
      sleep 20
    fi
  done
  return 1
}

apt_install() {
  apt_update || return 1
  # NEEDRESTART_SUSPEND: Ubuntu 22.04 would otherwise stop and ask which
  # services to restart.
  run_logged env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get \
    -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
    install -y --no-install-recommends "$@"
}

install_packages() {
  local missing=()
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  [ -s /etc/ssl/certs/ca-certificates.crt ] || missing+=(ca-certificates)
  command -v jq >/dev/null 2>&1 || missing+=(jq)
  command -v openssl >/dev/null 2>&1 || missing+=(openssl)
  command -v tar >/dev/null 2>&1 || missing+=(tar)
  command -v dig >/dev/null 2>&1 || missing+=(dnsutils)
  command -v ss >/dev/null 2>&1 || missing+=(iproute2)
  if [ ${#missing[@]} -eq 0 ]; then
    note "The basic tools are already installed."
  else
    note "Installing: ${missing[*]} (a minute or two)"
    apt_install "${missing[@]}" ||
      die "The basic tools could not be installed (${missing[*]})." \
        "Another update may be running: wait five minutes and run the same command again." \
        "On Ubuntu 22.04, jq needs the 'universe' software source: add-apt-repository -y universe"
  fi
  # The QR code is a convenience; go on without it.
  if ! command -v qrencode >/dev/null 2>&1; then
    note "Installing qrencode, which shows your entry link as a QR code."
    apt_install qrencode || warn "The QR code tool could not be installed. The link is still shown as text."
  fi
}

# ------------------------------------------------------- step 3: Docker ---

docker_ready() { command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }

docker_major() {
  local v
  v=$(docker version --format '{{.Server.Version}}' 2>/dev/null) || return 1
  v=${v%%.*}
  [[ $v =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$v"
}

wait_for_docker() {
  local tries=0
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  while [ "$tries" -lt 30 ]; do
    if docker info >/dev/null 2>&1; then return 0; fi
    tries=$((tries + 1))
    sleep 2
  done
  return 1
}

# Add Docker's own package source, unless one is configured already (a second
# entry with a different key file makes every apt command fail).
setup_docker_repo() {
  local etc="$MYAI_SYSROOT/etc/apt"
  if grep -rqs 'download\.docker\.com' "$etc/sources.list" "$etc/sources.list.d" 2>/dev/null; then
    return 0
  fi
  run install -m 0755 -d "$etc/keyrings" || return 1
  run curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" -o "$etc/keyrings/docker.asc" || return 1
  run chmod a+r "$etc/keyrings/docker.asc" || return 1
  write_file "$etc/sources.list.d/docker.list" 644 <<EOF || return 1
deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$OS_ID $OS_CODENAME stable
EOF
  APT_UPDATED=0
}

install_docker() {
  local major path
  path=$(command -v docker 2>/dev/null) || path=""
  case "$path" in
    /snap/*)
      die "Docker is installed as a snap package, which cannot use the folder $APP_DIR." \
        "Remove it (snap remove docker) and run the installer again; it installs the regular Docker."
      ;;
  esac

  if ! docker_ready; then
    if [ -z "$path" ] || dpkg -s docker-ce >/dev/null 2>&1; then
      note "Installing Docker from Docker's official package source (two or three minutes)."
      setup_docker_repo || die "Docker's package source could not be added." \
        "Check the connection: curl -I https://download.docker.com   Then run the same command again."
      apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin ||
        die "Docker could not be installed." "Run the same command again. The details are in $LOG_FILE"
    elif dpkg -s docker.io >/dev/null 2>&1; then
      note "Docker from the system's own packages is installed; adding its Compose plugin."
      apt_install docker-compose-v2 docker-buildx ||
        die "The Compose plugin for this Docker is not available." \
          "Replace it with Docker's official version: apt-get remove -y docker.io   then run the installer again."
    else
      die "Docker is installed in a way this installer does not know, and 'docker compose' is missing." \
        "Install the Compose plugin (https://docs.docker.com/compose/install/linux/) and run the installer again."
    fi
  fi

  if has_systemd || [ "$DRY_RUN" = 1 ]; then
    run_logged systemctl enable --now docker || warn "Could not switch on the Docker service."
  fi
  wait_for_docker || die "Docker is installed but does not answer." \
    "Look at its messages: journalctl -u docker -n 30   Then run the same command again."
  if [ "$DRY_RUN" = 1 ] && ! docker_ready; then return 0; fi

  # The compose file needs network features of Docker 28 or newer.
  major=$(docker_major) || major=0
  if [ "$major" -lt 28 ]; then
    if dpkg -s docker-ce >/dev/null 2>&1; then
      note "Docker $major is too old (28 or newer is needed). Updating it; running containers restart briefly."
      setup_docker_repo || die "Docker's package source could not be added."
      apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin ||
        die "Docker could not be updated." "Run the same command again. The details are in $LOG_FILE"
      wait_for_docker || die "Docker does not answer after the update." "Look at: journalctl -u docker -n 30"
    else
      die "This Docker (version $major) is too old; version 28 or newer is needed." \
        "Replace it with Docker's official version: apt-get remove -y docker.io   then run the installer again."
    fi
  fi
  note "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?') with Compose is ready."
}

# ---------------------------------------------------- step 4: hardening ---
# Each item is safe to repeat. They run inside "if", where bash does not
# stop at a failing command, so every command that matters is checked by hand.

harden_item() { # NAME FUNCTION
  local name=$1
  shift
  if "$@"; then return 0; fi
  warn "Could not finish: $name. The installation continues without it."
  HARDEN_FAILED+=("$name")
}

# The port or ports the SSH server listens on, one per line.
ssh_ports() {
  local ports="" extra
  ports=$(sshd -T 2>/dev/null | awk 'tolower($1) == "port" { print $2 }') || ports=""
  # The port of the current SSH session, in case it is not in the file.
  extra=$(awk '{ print $4 }' <<<"${SSH_CONNECTION:-}")
  ports=$(printf '%s\n%s\n' "$ports" "$extra" | grep -E '^[0-9]+$' | sort -un) || ports=""
  [ -n "$ports" ] || return 1
  printf '%s\n' "$ports"
}

harden_swap() {
  local swapfile="$MYAI_SYSROOT/swapfile" active
  active=$(swapon --show --noheadings 2>/dev/null) || active=""
  if [ -n "$active" ]; then
    note "Swap: already there, left as it is."
  elif [ -e "$swapfile" ]; then
    note "Swap: switching on the existing $swapfile."
    run swapon "$swapfile" || return 1
  else
    note "Swap: creating an 8 GB swap file (reserve memory on disk)."
    if ! run fallocate -l 8G "$swapfile"; then
      run dd if=/dev/zero of="$swapfile" bs=1M count=8192 status=none || {
        rm -f "$swapfile"
        return 1
      }
    fi
    run chmod 600 "$swapfile" || return 1
    if ! run_logged mkswap "$swapfile" || ! run swapon "$swapfile"; then
      rm -f "$swapfile"
      return 1
    fi
    append_line_once "$MYAI_SYSROOT/etc/fstab" "/swapfile none swap sw 0 0" || return 1
  fi
  write_file_if_changed "$MYAI_SYSROOT/etc/sysctl.d/90-myai.conf" 644 <<'EOF' || return 1
# Written by the myai installer: use swap only when memory is really short.
vm.swappiness = 10
EOF
  run sysctl -q -w vm.swappiness=10 || return 1
}

# The SSH port is allowed before the firewall is switched on, and the
# firewall is not switched on unless every rule was accepted.
harden_firewall() {
  local ports port
  if ! command -v ufw >/dev/null 2>&1; then
    apt_install ufw || return 1
  fi
  if ! ports=$(ssh_ports); then
    ports=22
    note "Firewall: could not read the SSH port from the SSH server; keeping port 22 open."
  fi
  for port in $ports; do
    run_logged ufw allow "$port/tcp" || return 1
  done
  run_logged ufw allow 80/tcp || return 1
  run_logged ufw allow 443/tcp || return 1
  run_logged ufw allow 443/udp || return 1
  run_logged ufw default deny incoming || return 1
  run_logged ufw --force enable || return 1
  note "Firewall: on. Open ports: SSH ($(printf '%s' "$ports" | paste -sd, -)), 80 and 443."
}

harden_fail2ban() {
  local ports
  if ! command -v fail2ban-client >/dev/null 2>&1; then
    # python3-systemd lets it read the SSH log on systems without /var/log/auth.log.
    apt_install fail2ban python3-systemd || return 1
  fi
  ports=$(ssh_ports | paste -sd, -) || ports=""
  [ -n "$ports" ] || ports=22
  write_file_if_changed "$MYAI_SYSROOT/etc/fail2ban/jail.d/myai-sshd.local" 644 <<EOF || return 1
# Written by the myai installer: block addresses that keep guessing SSH passwords.
[sshd]
enabled = true
port = $ports
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
  run_logged systemctl enable fail2ban || return 1
  if [ "$FILE_CHANGED" = 1 ]; then
    run_logged systemctl restart fail2ban || return 1
  else
    run_logged systemctl start fail2ban || return 1
  fi
  note "fail2ban: on (5 wrong SSH logins block an address for an hour)."
}

harden_auto_updates() {
  if ! command -v unattended-upgrade >/dev/null 2>&1; then
    apt_install unattended-upgrades || return 1
  fi
  write_file_if_changed "$MYAI_SYSROOT/etc/apt/apt.conf.d/52myai-auto-updates" 644 <<'EOF' || return 1
// Written by the myai installer: install security updates automatically and,
// when an update needs it, restart the server at 04:30.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
  note "Automatic security updates: on (restart at 04:30 when one is needed)."
}

# docker_log_settings FILE: prints Docker's daemon.json with log rotation
# added. Every other key, and log options that are already set, stay.
#   returns 0: the printed content should be written
#           1: nothing to do
#           2: the file was left alone (the reason is printed)
docker_log_settings() {
  local file=$1 cur='{}' new driver
  if [ -s "$file" ]; then
    if ! cur=$(jq -e 'if type == "object" then . else error("not an object") end' "$file" 2>/dev/null); then
      warn "$file is not valid JSON; left unchanged."
      return 2
    fi
    driver=$(jq -r '."log-driver" // "json-file"' <<<"$cur")
    case "$driver" in
      json-file | local) ;;
      *)
        # To stderr: stdout carries the new file content.
        note "Docker logs: the log driver '$driver' is configured; left unchanged." >&2
        return 2
        ;;
    esac
  fi
  new=$(jq '."log-opts" = ({"max-size": "10m", "max-file": "3"} + (."log-opts" // {}))' <<<"$cur") || return 2
  if jq -e -n --argjson a "$cur" --argjson b "$new" '$a == $b' >/dev/null 2>&1; then
    return 1
  fi
  printf '%s\n' "$new"
}

harden_docker_logs() {
  local file="$MYAI_SYSROOT/etc/docker/daemon.json" new rc
  if new=$(docker_log_settings "$file"); then
    if [ -f "$file" ] && [ ! -e "$file.before-myai" ]; then
      run cp -p "$file" "$file.before-myai" || return 1
    fi
    printf '%s\n' "$new" | write_file "$file" 644 || return 1
    note "Docker logs: size limit added (10 MB, 3 files per container). Restarting Docker."
    if has_systemd || [ "$DRY_RUN" = 1 ]; then
      run_logged systemctl restart docker || return 1
    fi
    wait_for_docker || return 1
  else
    rc=$?
    if [ "$rc" -eq 1 ]; then note "Docker logs: size limit already set."; fi
  fi
  return 0
}

harden() {
  if [ "$OPT_NO_HARDEN" = 1 ]; then
    if [ "$OPT_UPDATE" = 1 ]; then
      note "Skipped during an update (what the first installation set up stays)."
    else
      note "Skipped (--no-harden)."
    fi
    return 0
  fi
  harden_item "the swap file" harden_swap
  harden_item "the firewall" harden_firewall
  harden_item "fail2ban" harden_fail2ban
  harden_item "automatic security updates" harden_auto_updates
  harden_item "the size limit for Docker logs" harden_docker_logs
  note "Not changed: how you log in with SSH. Advice: add an SSH key and then switch off"
  note "password logins (on Hostinger: hPanel, VPS, Settings, SSH keys)."
}

# ------------------------------------------------ step 5: configuration ---

# install_app_files: copy the app from SRC_DIR to APP_DIR. The settings file,
# the data and the backups in APP_DIR are never touched.
install_app_files() {
  local src=$SRC_DIR dst=$APP_DIR sub
  if [ -d "$dst" ] && [ "$src" -ef "$dst" ]; then
    note "The app files are already in $dst."
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    note "would copy the app files from $src to $dst"
    note "  (never touching $dst/.env, $dst/data or $dst/backups)"
    return 0
  fi
  mkdir -p "$dst"
  chmod 755 "$dst"
  # Old code goes first, so that files removed in a newer version do not
  # stay behind in the image builds.
  for sub in agent sandbox; do
    if [ -d "$src/$sub" ]; then rm -rf "${dst:?}/$sub"; fi
  done
  # --mode: every file readable by all, whatever the umask of the copy it
  # comes from. The services read these files as other users (the agent and
  # the sandbox as 10001, SearXNG its settings as 977), and the archive's
  # "./" entry would otherwise give $dst the mode of a 700 download folder.
  tar -C "$src" --anchored --exclude=./.env --exclude=./data --exclude=./backups --exclude=./.git \
    --exclude=./.lock --exclude=./.stopped --exclude=./.replaced-images \
    --no-anchored --exclude=__pycache__ --exclude=.pytest_cache \
    --mode=u+rwX,go+rX,go-w -cf - . |
    tar -C "$dst" --no-same-owner -xf -
  chmod 755 "$dst/bin/myai" "$dst/install.sh"
  note "App files copied to $dst."
}

install_cli() {
  run ln -sfn "$APP_DIR/bin/myai" "$MYAI_SYSROOT/usr/local/bin/myai"
}

system_timezone() {
  local tz=""
  tz=$(timedatectl show -p Timezone --value 2>/dev/null) || tz=""
  if [ -z "$tz" ] && [ -r "$MYAI_SYSROOT/etc/timezone" ]; then
    tz=$(awk 'NR == 1' "$MYAI_SYSROOT/etc/timezone")
  fi
  if [ -z "$tz" ]; then
    tz=$(readlink "$MYAI_SYSROOT/etc/localtime" 2>/dev/null) || tz=""
    tz=${tz#*/zoneinfo/}
  fi
  if [[ ! $tz =~ ^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$ ]]; then tz=UTC; fi
  printf '%s\n' "$tz"
}

new_secret() { # BYTES -> 2 x BYTES hex characters
  if [ "$DRY_RUN" = 1 ]; then
    printf '<generated>\n'
    return 0
  fi
  openssl rand -hex "$1"
}

# 20 characters without look-alikes (no 0/O, 1/l/I), easy to type on a phone.
new_password() {
  local pw=""
  if [ "$DRY_RUN" = 1 ]; then
    printf '<generated>\n'
    return 0
  fi
  while [ ${#pw} -lt 20 ]; do
    pw+=$(openssl rand -base64 48 | tr -dc 'a-km-zA-HJ-NP-Z2-9')
  done
  printf '%s\n' "${pw:0:20}"
}

new_pairing_code() {
  local hex
  if [ "$DRY_RUN" = 1 ]; then
    printf '<generated>\n'
    return 0
  fi
  hex=$(openssl rand -hex 4)
  printf '%08d\n' $((16#$hex % 100000000))
}

# The login e-mail of the chat page, as this run sets it.
admin_email_now() {
  local e=${PLAN[WEBUI_ADMIN_EMAIL]:-${ADMIN_EMAIL:-}}
  if [ -z "$e" ]; then e=$(env_get WEBUI_ADMIN_EMAIL) || e=""; fi
  printf '%s\n' "${e:-owner@example.com}"
}

# CADDY_TLS_LINE for a public name. With a real e-mail address as the
# contact, Caddy can also use ZeroSSL when Let's Encrypt fails; that matters
# most for names ending in sslip.io, which share one weekly Let's Encrypt
# limit with every other sslip.io user. A placeholder address would make
# Let's Encrypt refuse to issue anything, so then the line stays empty.
public_tls_line() { # EMAIL
  if usable_contact_email "$1"; then printf 'tls %s\n' "$1"; fi
}

# Sets SITE_ADDRESS, CADDY_TLS_LINE and SITE_REASON:
#   1. --domain;
#   2. the machine's own full name, when public DNS points it at this machine;
#   3. <public address with dashes>.sslip.io, a free name for any address;
#   4. localhost with a self-signed certificate when there is no public address.
choose_site_address() {
  local fqdn self_signed=0
  PUBLIC_IPV4=$(detect_public_ipv4) || PUBLIC_IPV4=""
  if [ -n "$OPT_DOMAIN" ]; then
    SITE_ADDRESS=$OPT_DOMAIN
    SITE_REASON="given with --domain"
    if [ "$SITE_ADDRESS" = localhost ] || is_ipv4 "$SITE_ADDRESS"; then
      # No public certificate authority signs these.
      self_signed=1
    elif [ -n "$PUBLIC_IPV4" ] && ! dns_points_to "$SITE_ADDRESS" "$PUBLIC_IPV4"; then
      warn "$SITE_ADDRESS does not point to this server yet (its public address is $PUBLIC_IPV4)."
      warn "Create an A record for it at your domain provider; the certificate is issued once that works."
    fi
  elif [ -z "$PUBLIC_IPV4" ]; then
    SITE_ADDRESS=localhost
    self_signed=1
    SITE_REASON="no public address was found, so only this machine can reach it"
  else
    fqdn=$(machine_fqdn)
    if [[ $fqdn == *.* ]] && valid_hostname "$fqdn" && dns_points_to "$fqdn" "$PUBLIC_IPV4"; then
      SITE_ADDRESS=$fqdn
      SITE_REASON="the name of this server, which public DNS points here"
    else
      SITE_ADDRESS="${PUBLIC_IPV4//./-}.sslip.io"
      SITE_REASON="a free name from sslip.io that leads to this server's address $PUBLIC_IPV4"
    fi
  fi
  if [ "$OPT_TLS_INTERNAL" = 1 ]; then self_signed=1; fi
  if [ "$self_signed" = 1 ]; then
    CADDY_TLS_LINE="tls internal"
  else
    CADDY_TLS_LINE=$(public_tls_line "$(admin_email_now)")
  fi
  SITE_CHOSEN=1
}

ensure_site() {
  if [ "$SITE_CHOSEN" != 1 ]; then choose_site_address; fi
}

default_governor() {
  local fqdn
  case "$OPT_GOVERNOR" in
    on | off)
      printf '%s\n' "$OPT_GOVERNOR"
      return 0
      ;;
  esac
  fqdn=$(machine_fqdn)
  case "$fqdn" in
    *.hstgr.cloud) echo on ;;
    *) echo off ;;
  esac
}

# The settings file, in the order of .env.example. "@name" marks a value
# that is worked out or generated; anything else is the default, unless
# .env.example in the app files gives another one.
ENV_TEMPLATE='# Settings for myai, written by install.sh. Every key is explained in the
# file .env.example next to this one. After a change by hand run: myai restart

SITE_ADDRESS=@site
CADDY_TLS_LINE=@tls
GATE_TOKEN=@hex16
TZ=@tz
DATA_DIR=@datadir

LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server
WEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.13.5
SEARXNG_IMAGE=docker.io/searxng/searxng:2026.11.23-a7163d091
CADDY_IMAGE=caddy:2.11.9-alpine

MODEL_ID=@model_id
MODEL_FILE=@model_file
MODEL_ALIAS=assistant
LLM_CTX=@ctx
LLM_PARALLEL=2
LLM_THREADS=@threads
LLM_THREADS_BATCH=@threads_batch
LLM_CACHE_RAM_MIB=@cache_ram
LLM_THINK_BUDGET=2048
LLM_LOAD_MODE=mmap+mlock
LLM_TEMPERATURE=0.7
LLM_TOP_P=0.8
LLM_TOP_K=20
LLM_MIN_P=0
LLM_PRESENCE_PENALTY=1.0

LLM_API_KEY=@hex32
AGENT_API_KEY=@hex32
SANDBOX_TOKEN=@hex32
SEARXNG_SECRET=@hex32
WEBUI_SECRET_KEY=@hex32
WEBUI_ADMIN_EMAIL=@email
WEBUI_ADMIN_PASSWORD=@password
WEBUI_ADMIN_NAME=Owner
DASHBOARD_PASSWORD=@same_password

AGENT_THINKING=auto
AGENT_MAX_STEPS=12
AGENT_CTX_TOKENS=@agent_ctx
AGENT_MAX_OUTPUT_TOKENS=1500
CPU_GOVERNOR=@governor
CPU_BASELINE=0.60
BACKGROUND_REST_RATIO=0.5
MIN_SCHEDULE_INTERVAL_MIN=15
MAX_SCHEDULES=50
ENABLE_RUN_CODE=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_IDS=
PAIRING_CODE=@pairing'

# A default from .env.example in the app files, else the given fallback.
example_default() { # KEY FALLBACK
  local v
  v=$(env_get "$1" "$SRC_DIR/.env.example" 2>/dev/null) || v=""
  if [ -n "$v" ]; then printf '%s\n' "$v"; else printf '%s\n' "$2"; fi
}

# Servers with less than 12 GB of memory get a smaller context window and
# prompt cache; the defaults are sized for 32 GB.
low_memory() { [ "${RAM_KB:-0}" -gt 0 ] && [ "$RAM_KB" -lt 12582912 ]; }

# new_env_value KEY TEMPLATE: sets VALUE for a key the settings file does not
# have yet. Not called through $(...) because it also records choices.
new_env_value() {
  local key=$1 tmpl=$2
  case "$tmpl" in
    @site)
      ensure_site
      VALUE=$SITE_ADDRESS
      ;;
    @tls)
      ensure_site
      VALUE=$CADDY_TLS_LINE
      ;;
    @hex16) VALUE=$(new_secret 16) ;;
    @hex32) VALUE=$(new_secret 32) ;;
    @password) VALUE=$(new_password) ;;
    @same_password) VALUE=${PLAN[WEBUI_ADMIN_PASSWORD]:-} ;;
    @pairing) VALUE=$(new_pairing_code) ;;
    @tz) VALUE=$(system_timezone) ;;
    @datadir) VALUE="$APP_DIR/data" ;;
    @model_id) VALUE=$MODEL_ID ;;
    @model_file) VALUE=$MODEL_FILE ;;
    @threads) VALUE=$(compute_threads "$VCPUS") ;;
    @threads_batch) VALUE=$VCPUS ;;
    @ctx) if low_memory; then VALUE=16384; else VALUE=$(example_default "$key" 32768); fi ;;
    @cache_ram) if low_memory; then VALUE=512; else VALUE=$(example_default "$key" 2048); fi ;;
    @agent_ctx) if low_memory; then VALUE=6000; else VALUE=$(example_default "$key" 14000); fi ;;
    @email) VALUE=${ADMIN_EMAIL:-owner@example.com} ;;
    @governor) VALUE=$(default_governor) ;;
    *) VALUE=$(example_default "$key" "$tmpl") ;;
  esac
}

# True once Open WebUI has its database, and with it the first login: it
# reads WEBUI_ADMIN_EMAIL and WEBUI_ADMIN_PASSWORD only before that.
chat_has_its_login() {
  local data=${PLAN[DATA_DIR]:-}
  if [ -z "$data" ]; then data=$(env_get DATA_DIR) || data="$APP_DIR/data"; fi
  [ -f "$data/webui/webui.db" ]
}

# Change one setting that was asked for on the command line.
override_setting() { # KEY VALUE
  if [ "${PLAN[$1]:-}" = "$2" ]; then return 0; fi
  env_set "$1" "$2"
  PLAN[$1]=$2
}

# write_config: create the settings file, or complete an existing one.
# An existing file keeps every line it has: only keys that are missing are
# added, and only options given on the command line change a value.
write_config() {
  local line key tmpl quoted existing=0 content="" shown="" added="" added_shown="" extra
  local email_changed=0 tls contact
  if [ -f "$ENV_FILE" ]; then
    existing=1
    secure_env_file
  fi
  PLAN=()

  while IFS= read -r line; do
    case "$line" in
      [A-Z]*=*)
        key=${line%%=*}
        tmpl=${line#*=}
        if VALUE=$(env_get "$key"); then
          PLAN[$key]=$VALUE
          continue
        fi
        new_env_value "$key" "$tmpl"
        PLAN[$key]=$VALUE
        quoted=$(env_quote "$VALUE") || die "The value for $key contains a quote character or a line break."
        content+="$key=$quoted"$'\n'
        added+="$key=$quoted"$'\n'
        if is_secret_key "$key" && [ -n "$VALUE" ]; then quoted="<hidden>"; fi
        shown+="$key=$quoted"$'\n'
        added_shown+="$key=$quoted"$'\n'
        ;;
      *)
        content+="$line"$'\n'
        shown+="$line"$'\n'
        ;;
    esac
  done <<<"$ENV_TEMPLATE"

  # Keys that a newer .env.example documents and this installer does not know.
  if [ -f "$SRC_DIR/.env.example" ]; then
    while IFS= read -r line; do
      key=${line%%=*}
      if [ -n "${PLAN[$key]+set}" ] || env_has "$key"; then continue; fi
      VALUE=$(env_get "$key" "$SRC_DIR/.env.example") || VALUE=""
      PLAN[$key]=$VALUE
      extra="$key=$(env_quote "$VALUE")"
      content+="$extra"$'\n'
      shown+="$extra"$'\n'
      added+="$extra"$'\n'
      added_shown+="$extra"$'\n'
    done < <(grep -E '^[A-Z][A-Z0-9_]*=' "$SRC_DIR/.env.example" || true)
  fi

  if [ "$existing" = 0 ]; then
    if [ "$DRY_RUN" = 1 ]; then
      printf '%s' "$shown" | write_file "$ENV_FILE" 600
    else
      printf '%s' "$content" | write_file "$ENV_FILE" 600
      note "Settings written to $ENV_FILE (only root can read it)."
    fi
  elif [ -n "$added" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      note "would add these missing settings to $ENV_FILE (everything else in it stays):"
      printf '%s' "$added_shown" | sed 's/^/      | /'
    else
      {
        cat "$ENV_FILE"
        # Start on a new line even when the file does not end with one.
        if [ -n "$(tail -c 1 "$ENV_FILE")" ]; then printf '\n'; fi
        printf '\n# Added by the installer on %s\n%s' "$(date +%F)" "$added"
      } | write_file "$ENV_FILE" 600
      note "Kept $ENV_FILE and added $(printf '%s' "$added" | grep -c '') new setting(s)."
    fi
  else
    note "Kept $ENV_FILE as it is."
  fi

  # Options given on the command line win over what the file says.
  if [ "$existing" = 1 ]; then
    if [ -n "$OPT_ADMIN_EMAIL" ] && [ "${PLAN[WEBUI_ADMIN_EMAIL]:-}" != "$OPT_ADMIN_EMAIL" ]; then
      if chat_has_its_login; then
        # Open WebUI reads the setting only while it has no database. The
        # credentials sheet shows this value, so it must stay the login
        # that really works.
        warn "The chat page already has its login, ${PLAN[WEBUI_ADMIN_EMAIL]:-?}. --admin-email cannot change it any more, so it stays."
        warn "To change the login: log in to the chat page, open the Admin Panel, Users, and edit your user."
      else
        override_setting WEBUI_ADMIN_EMAIL "$OPT_ADMIN_EMAIL"
        email_changed=1
      fi
    fi
    if [ -n "$OPT_DOMAIN" ]; then
      ensure_site
      override_setting SITE_ADDRESS "$SITE_ADDRESS"
      # The new address gets the certificate it needs, but a contact address
      # chosen before stays.
      if tls_is_self_signed "$CADDY_TLS_LINE" || ! tls_contact "${PLAN[CADDY_TLS_LINE]:-}" >/dev/null; then
        override_setting CADDY_TLS_LINE "$CADDY_TLS_LINE"
      fi
    elif [ "$OPT_TLS_INTERNAL" = 1 ]; then
      override_setting CADDY_TLS_LINE "tls internal"
    elif [ "$email_changed" = 1 ] &&
      { [ -z "${PLAN[CADDY_TLS_LINE]:-}" ] || tls_contact "${PLAN[CADDY_TLS_LINE]}" >/dev/null; }; then
      # The contact for the certificate follows the login address.
      override_setting CADDY_TLS_LINE "$(public_tls_line "$OPT_ADMIN_EMAIL")"
    fi
    if [ -n "$OPT_MODEL" ] && [ -n "$MODEL_FILE" ]; then
      override_setting MODEL_ID "$MODEL_ID"
      override_setting MODEL_FILE "$MODEL_FILE"
    fi
    if [ -n "$OPT_GOVERNOR" ]; then override_setting CPU_GOVERNOR "$OPT_GOVERNOR"; fi
  fi

  note "Address: ${PLAN[SITE_ADDRESS]}${SITE_REASON:+ ($SITE_REASON)}"
  tls=${PLAN[CADDY_TLS_LINE]:-}
  if tls_is_self_signed "$tls"; then
    note "Certificate: self-signed (browsers show a warning the first time)"
  elif contact=$(tls_contact "$tls"); then
    note "Certificate: from Let's Encrypt, or ZeroSSL when that fails (contact address: $contact)"
  elif [ -z "$tls" ]; then
    note "Certificate: from Let's Encrypt"
  else
    note "Certificate: as set in CADDY_TLS_LINE ($tls)"
  fi
  note "Threads: ${PLAN[LLM_THREADS]} for writing answers, ${PLAN[LLM_THREADS_BATCH]} for reading prompts ($VCPUS vCPUs)"
  note "CPU-credit protection (governor): ${PLAN[CPU_GOVERNOR]}"
  check_plan
}

# Things in the final settings that would not work.
check_plan() {
  local ctx=${PLAN[LLM_CTX]:-0} par=${PLAN[LLM_PARALLEL]:-1} agent=${PLAN[AGENT_CTX_TOKENS]:-0} site records
  if [[ $ctx =~ ^[0-9]+$ ]] && [[ $par =~ ^[1-9][0-9]*$ ]] && [[ $agent =~ ^[0-9]+$ ]] &&
    [ "$agent" -ge $((ctx / par)) ]; then
    warn "AGENT_CTX_TOKENS ($agent) must be smaller than LLM_CTX / LLM_PARALLEL ($((ctx / par))). Lower it in $ENV_FILE."
  fi
  # A name with an IPv6 record that leads elsewhere makes the certificate fail.
  site=${PLAN[SITE_ADDRESS]:-}
  if ! tls_is_self_signed "${PLAN[CADDY_TLS_LINE]:-}" && [ -n "$site" ] && command -v dig >/dev/null 2>&1; then
    records=$(dns_records AAAA "$site" | paste -sd' ' -) || records=""
    if [ -n "$records" ] && ! ip -6 -o addr show scope global 2>/dev/null | grep -qF -- "${records%% *}"; then
      warn "$site also has an IPv6 address ($records) that is not on this server. The certificate may fail until that DNS record (type AAAA) is removed or corrected."
    fi
  fi
}

# An image version that an update replaced. It is also written down in the
# app folder, so that an update which stops half-way (and leaves the new
# version in the settings) still gets it removed by the next run that works.
remember_replaced_image() { # IMAGE
  REPLACED_IMAGES+=("$1")
  if [ "$DRY_RUN" != 1 ]; then printf '%s\n' "$1" >>"$APP_DIR/.replaced-images"; fi
}

# With --update, the images follow the versions the new app files were
# tested with. Open WebUI, SearXNG and Caddy have fixed versions, which the
# settings take at once; the replaced versions are removed once the update
# has worked. The model server image is a moving tag instead: its newer
# build is only tried (start_llm), and the settings keep the build that
# worked until the newer one has loaded the model.
refresh_images() {
  local key new
  [ "$OPT_UPDATE" = 1 ] || return 0
  [ -f "$SRC_DIR/.env.example" ] || return 0
  for key in WEBUI_IMAGE SEARXNG_IMAGE CADDY_IMAGE; do
    new=$(env_get "$key" "$SRC_DIR/.env.example") || continue
    if [ -n "$new" ] && [ "${PLAN[$key]:-}" != "$new" ]; then
      note "$key: ${PLAN[$key]:-(not set)} -> $new"
      if [ -n "${PLAN[$key]:-}" ]; then remember_replaced_image "${PLAN[$key]}"; fi
      override_setting "$key" "$new"
    fi
  done
  new=$(env_get LLAMA_IMAGE "$SRC_DIR/.env.example") || new=""
  case "${PLAN[LLAMA_IMAGE]:-}" in
    *@sha256:*) LLAMA_UPDATE_TAG=$new ;;
  esac
}

configure() {
  # The lock comes first: another installer, an update or a backup may be
  # using the app files that are replaced next. From here on the 5-minute
  # repair and other myai commands wait for this run.
  if [ "$DRY_RUN" != 1 ]; then
    mkdir -p "$APP_DIR"
    acquire_lock
  fi
  install_app_files
  install_cli
  write_config
  refresh_images
}

# -------------------------------------------------- step 6: directories ---

make_dirs() {
  local data=${PLAN[DATA_DIR]}
  make_data_dirs "$data"
  run install -d -m 700 -o root -g root "$APP_DIR/backups"
  note "Data folder: $data"
}

# ------------------------------------------------- step 7: model download ---

fetch_model() {
  local dir="${PLAN[DATA_DIR]}/models" file=${PLAN[MODEL_FILE]} id=${PLAN[MODEL_ID]} repo bytes
  if [ "$OPT_SKIP_MODEL" = 1 ]; then
    note "Skipped (--skip-model-download). The model file belongs here: $dir/$file"
    return 0
  fi
  if [ "$(catalog_field "$id" 4 2>/dev/null)" = "$file" ]; then
    repo=$(catalog_field "$id" 3)
    bytes=$(catalog_field "$id" 5)
    download_model "$repo" "$file" "$bytes" "$dir"
  elif file_is_gguf "$dir/$file"; then
    note "Using your own model file: $dir/$file"
  else
    warn "The model file $file is not in the catalogue, so it cannot be downloaded here."
    warn "Download it with: myai model custom <huggingface repository> $file"
  fi
}

# ---------------------------------------------------------- step 8: start ---

# Sets LLAMA_TRY to the exact model server build (repo@sha256:...) that this
# run tries, or to "" when it keeps the one in the settings. That is the
# build behind the moving tag on a first installation, and the newer build
# of the new app files' tag with --update. Restarts later stay on the build
# that was tested here.
pick_llama_build() {
  local current=${PLAN[LLAMA_IMAGE]} tag digest
  LLAMA_TRY=""
  case "$current" in
    *@sha256:*) tag=$LLAMA_UPDATE_TAG ;;
    *) tag=$current ;;
  esac
  [ -n "$tag" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    if [ "$tag" != "$current" ]; then note "would download $tag, to try its newest build"; fi
    note "would write the exact build of $tag into LLAMA_IMAGE in $ENV_FILE once the model has loaded with it"
    return 0
  fi
  if [ "$tag" != "$current" ]; then
    note "Downloading the newest build of the model server, to try it."
    if ! run_logged docker pull "$tag"; then
      warn "The newest build of the model server could not be downloaded. The current one stays."
      return 0
    fi
  fi
  if digest=$(image_digest_ref "$tag"); then
    if [ "$digest" != "$current" ]; then LLAMA_TRY=$digest; fi
  elif [ "$tag" = "$current" ]; then
    warn "Could not read the exact build of $tag; LLAMA_IMAGE keeps the moving tag."
  else
    warn "Could not read the exact build of $tag. The model server stays on the current one."
  fi
}

llm_failure_help() { # RC
  if [ "$1" -eq 1 ]; then
    printf 'The model server was not ready after 30 minutes.\n'
  else
    printf 'The model server stopped or keeps restarting.\n'
  fi
}

# Start the model server and wait until the model is loaded. A build in
# LLAMA_TRY is given to compose for this one start; the settings record it
# only once the model has loaded with it. Until then they keep the build that
# worked: if this run is stopped, the 5-minute repair goes back to that one,
# and the next run can still go back to it.
start_llm() {
  local data=${PLAN[DATA_DIR]} file=${PLAN[MODEL_FILE]} current=${PLAN[LLAMA_IMAGE]} rc=0
  note "Starting the model server. Loading the model into memory takes a few minutes."
  # rc: 0 loaded, 1 not ready in time, 2 stopped or crashing, 3 not started.
  if [ -n "$LLAMA_TRY" ]; then
    LLAMA_IMAGE=$LLAMA_TRY run_dc up -d llm || rc=3
  else
    run_dc up -d llm || rc=3
  fi
  if [ "$rc" -eq 0 ]; then wait_healthy llm 1800 "the model to load" || rc=$?; fi
  if [ "$rc" -eq 0 ]; then
    if [ -n "$LLAMA_TRY" ]; then
      env_set LLAMA_IMAGE "$LLAMA_TRY"
      PLAN[LLAMA_IMAGE]=$LLAMA_TRY
      note "Model server build recorded: ${LLAMA_TRY##*@}"
      if [[ $current == *@sha256:* ]]; then REPLACED_LLAMA=$current; fi
    fi
    note "The model is loaded."
    return 0
  fi

  # A newer build that fails: back to the build that worked, if there is one.
  if [ -n "$LLAMA_TRY" ] && [[ $current == *@sha256:* ]]; then
    dc logs --tail 20 llm 2>&1 | sed 's/^/      | /' >&2 || true
    warn "The newer model server build did not load the model (its messages are above). Going back to the build that worked."
    rc=0
    run_dc up -d llm || rc=3
    if [ "$rc" -eq 0 ]; then wait_healthy llm 1800 "the model to load" || rc=$?; fi
    if [ "$rc" -eq 0 ]; then
      note "The model is loaded, with the model server build from before."
      return 0
    fi
    warn "The build from before does not load the model now either, so the newer build is not the cause."
  fi
  if [ "$rc" -eq 3 ]; then
    die "The model server could not be started." "The message above says why; more with: myai logs llm"
  fi
  dc logs --tail 20 llm 2>&1 | sed 's/^/      | /' >&2 || true
  die "$(llm_failure_help "$rc")" \
    "Its last messages are above; more with: myai logs llm" \
    "Usual cause: too little free memory. A smaller model: run the install command again with --model fast at the end." \
    "If the messages say that the model file cannot be read, delete it and run the installer again to download it afresh:" \
    "  rm $data/models/$file"
}

start_stack() {
  local data=${PLAN[DATA_DIR]} file=${PLAN[MODEL_FILE]} rc=0 what
  if [ "$DRY_RUN" != 1 ] && ! file_is_gguf "$data/models/$file"; then
    warn "The model file is not there yet, so the services are not started."
    note "When $data/models/$file exists, run: myai start"
    return 0
  fi

  run_dc config -q || die "Docker Compose does not accept the settings in $ENV_FILE." \
    "The message above names the setting. Correct it and run the same command again."
  note "Downloading the container images (a few GB the first time)."
  run_dc pull --ignore-buildable || die "The container images could not be downloaded." \
    "This is usually a short network problem. Run the same command again."
  pick_llama_build
  note "Building the agent and the sandbox."
  run_dc build agent sandbox || die "Building the agent or the sandbox failed." \
    "The messages above show where. Run the same command again; if it fails the same way, report those messages."

  start_llm

  note "Starting the other services."
  run_dc up -d --remove-orphans || die "The services could not be started." "See: myai status   and: myai logs"
  for what in agent webui; do
    rc=0
    wait_healthy "$what" 600 "the $what service" || rc=$?
    if [ "$rc" -ne 0 ]; then
      die "The service '$what' did not become ready." "See its messages: myai logs $what" \
        "Then run the same command again."
    fi
  done
  if [ "$DRY_RUN" != 1 ]; then rm -f "$APP_DIR/.stopped"; fi
  STACK_STARTED=1
  note "All services are running."
}

# ------------------------------------------------------ step 9: self-test ---

self_test() {
  local out speed reading tokens secs workspace="${PLAN[DATA_DIR]}/workspace" proof
  proof="$workspace/myai-selftest.txt"
  if [ "$DRY_RUN" = 1 ]; then
    note "would send one short chat request through the agent gateway and report tokens per second"
    note "would give the agent one small task that must use a tool (write a file) and check the result"
    return 0
  fi
  if [ "$STACK_STARTED" != 1 ]; then
    note "Skipped: the services are not running."
    return 0
  fi

  note "Test 1 of 2: a short chat through the gateway."
  out=$(gateway_chat "Count from 1 to 20, separated by spaces." 64 900) || true
  out=${out##*$'\n'}
  case "$out" in
    "ok "*)
      read -r _ speed reading tokens secs <<<"$out" || true
      note "Passed: $tokens tokens in $secs seconds."
      SPEED_TEXT="writes about $speed tokens per second"
      if [ "${reading%.*}" -gt 0 ] 2>/dev/null; then SPEED_TEXT+=", reads about $reading tokens per second"; fi
      note "Speed: $SPEED_TEXT."
      ;;
    *)
      SELFTEST_FAILED=1
      warn "The chat test failed: ${out#error }"
      ;;
  esac

  note "Test 2 of 2: an agent task that has to use a tool. This can take a few minutes."
  rm -f "$proof"
  out=$(agent_task "Use the write_file tool to create the file myai-selftest.txt with the content OK. Then answer with the single word: done" 900) || true
  out=${out##*$'\n'}
  # The file in the shared workspace proves that the tool really ran.
  if [ -f "$proof" ]; then
    secs=""
    case "$out" in
      "ok "*) read -r _ _ secs _ <<<"$out" || true ;;
    esac
    note "Passed: the agent used a tool and wrote a file${secs:+ in $secs seconds}."
    rm -f "$proof"
  else
    SELFTEST_FAILED=1
    case "$out" in
      "ok 0 "*) warn "The agent answered, but without using the tool it was asked to use." ;;
      "ok "*) warn "The agent used a tool (${out##* }), but the test file did not appear in ${workspace}." ;;
      *) warn "The agent test failed: ${out#error }" ;;
    esac
  fi

  if [ "$SELFTEST_FAILED" = 1 ]; then
    warn "A test failed. The installation is complete and stays as it is."
    warn "Look for the cause with: myai doctor   and: myai logs agent"
  fi
}

# Free KB on the disk that holds DIR; empty when unknown.
free_kb() {
  df -Pk "${1:-/}" 2>/dev/null | awk 'NR == 2 && $4 ~ /^[0-9]+$/ { print $4 }'
}

# Once the services run, remove the image versions that this update
# replaced (Open WebUI alone is about 6 GB per version, and an update every
# few weeks would fill the disk over a year), and old builds of the agent
# and the sandbox. Only those: never other images on this server. The model
# server build from before stays when the test failed with the new one.
remove_replaced_images() {
  local img key in_use list="$APP_DIR/.replaced-images" removed=0 root before after
  [ "$STACK_STARTED" = 1 ] || return 0
  if [ -n "$REPLACED_LLAMA" ] && [ "$SELFTEST_FAILED" != 1 ]; then REPLACED_IMAGES+=("$REPLACED_LLAMA"); fi
  if [ "$DRY_RUN" = 1 ]; then
    for img in ${REPLACED_IMAGES[@]+"${REPLACED_IMAGES[@]}"}; do note "would remove the replaced image $img"; done
    if [ "$OPT_UPDATE" = 1 ]; then
      note "would remove old builds of the agent and the sandbox (images of this project only)"
    fi
    return 0
  fi
  # Versions an earlier update replaced but could not remove any more.
  if [ -f "$list" ]; then
    while IFS= read -r img; do
      if [ -n "$img" ]; then REPLACED_IMAGES+=("$img"); fi
    done <"$list"
  fi
  root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || root=""
  before=$(free_kb "${root:-/}")
  for img in ${REPLACED_IMAGES[@]+"${REPLACED_IMAGES[@]}"}; do
    # Never one that the settings use now.
    in_use=0
    for key in LLAMA_IMAGE WEBUI_IMAGE SEARXNG_IMAGE CADDY_IMAGE; do
      if [ "${PLAN[$key]:-}" = "$img" ]; then in_use=1; fi
    done
    [ "$in_use" = 0 ] || continue
    # Fails when a container still uses it: then it stays.
    if docker image rm "$img" >/dev/null 2>&1; then removed=$((removed + 1)); fi
  done
  rm -f -- "$list"
  # Dangling builds of this compose project, nothing else.
  docker image prune -f --filter "label=com.docker.compose.project=myai" >/dev/null 2>&1 || true
  after=$(free_kb "${root:-/}")
  if [[ $before =~ ^[0-9]+$ ]] && [[ $after =~ ^[0-9]+$ ]] && [ $((after - before)) -gt 100000 ]; then
    note "Removed container images that are no longer used: $(fmt_gb $(((after - before) * 1024))) of disk space freed."
  elif [ "$removed" -gt 0 ]; then
    note "Removed container images that are no longer used."
  fi
}

# ------------------------------------------- step 10: command and timers ---

install_timers() {
  # Called inside "if", where set -e does not apply: every step is checked.
  # A oneshot service has no time limit of its own. Both get one, so that a
  # command that hangs cannot hold the myai lock until the next reboot; the
  # repair's limit is shorter than the 15 minutes other commands wait for it.
  local dir="$MYAI_SYSROOT/etc/systemd/system" changed=0
  write_file_if_changed "$dir/myai-backup.service" 644 <<'EOF' || return 1
[Unit]
Description=myai: daily backup of settings and data
After=docker.service

[Service]
Type=oneshot
Environment=HOME=/root
ExecStart=/usr/local/bin/myai backup
TimeoutStartSec=2h
EOF
  changed=$((changed + FILE_CHANGED))
  write_file_if_changed "$dir/myai-backup.timer" 644 <<'EOF' || return 1
[Unit]
Description=myai: daily backup at 03:15

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
  changed=$((changed + FILE_CHANGED))
  write_file_if_changed "$dir/myai-heal.service" 644 <<'EOF' || return 1
[Unit]
Description=myai: keep the services running
After=docker.service

[Service]
Type=oneshot
Environment=HOME=/root
ExecStart=/usr/local/bin/myai heal
TimeoutStartSec=10min
EOF
  changed=$((changed + FILE_CHANGED))
  write_file_if_changed "$dir/myai-heal.timer" 644 <<'EOF' || return 1
[Unit]
Description=myai: check the services every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  changed=$((changed + FILE_CHANGED))

  if ! has_systemd && [ "$DRY_RUN" != 1 ]; then
    warn "systemd is not running here, so the daily backup and the 5-minute check are not scheduled."
    return 0
  fi
  if [ "$changed" -gt 0 ]; then run_logged systemctl daemon-reload || return 1; fi
  run_logged systemctl enable --now myai-backup.timer myai-heal.timer || return 1
}

install_command_and_timers() {
  install_cli
  note "Command installed: myai   (try: myai help)"
  if install_timers; then
    note "Scheduled: a backup every night at 03:15 (the newest 7 are kept in $APP_DIR/backups)"
    note "Scheduled: a check every 5 minutes that restarts a service that stopped working"
  else
    warn "The timers could not be set up. Backups and the 5-minute check are not scheduled."
  fi
}

# -------------------------------------------------------- step 11: summary ---

final_summary() {
  local file="$MYAI_SYSROOT/root/myai-credentials.txt" text link
  if [ "$DRY_RUN" = 1 ]; then
    note "would print the entry link (with a QR code), the login and the next steps"
    note "would write: $file (mode 600) with the same details"
    printf '\nDry run finished. Nothing was changed.\n'
    return 0
  fi
  text=$(credentials_text)
  link=$(entry_link)
  {
    printf 'myai on %s, written on %s\n\n%s\n' "${PLAN[SITE_ADDRESS]}" "$(date '+%F %H:%M')" "$text"
    if [ -n "$SPEED_TEXT" ]; then printf '\nSpeed measured at installation: the model %s.\n' "$SPEED_TEXT"; fi
  } | write_file "$file" 600

  printf '\n============================================================\n'
  if [ "$OPT_UPDATE" = 1 ]; then
    printf ' The update is finished.\n'
  elif [ "$STACK_STARTED" = 1 ]; then
    printf ' Your private AI server is ready.\n'
  else
    printf ' Installed, but not started yet (see the warning above).\n'
  fi
  printf '============================================================\n\n'
  printf '%s\n' "$text"
  printf '\nThe link from point 1 as a QR code (point your phone camera at it):\n'
  print_qr "$link"
  if ! tls_is_self_signed "${PLAN[CADDY_TLS_LINE]:-}"; then
    printf '\nThe certificate is fetched in the first minute. If the browser warns, wait and reload.\n'
  fi
  if [ -n "$SPEED_TEXT" ]; then
    printf '\nSpeed measured just now: the model %s.\n' "$SPEED_TEXT"
  fi
  if [ ${#HARDEN_FAILED[@]} -gt 0 ]; then
    printf '\nNot set up (see the warnings above): %s\n' "$(printf '%s; ' "${HARDEN_FAILED[@]}")"
  fi
  if [ "$SELFTEST_FAILED" = 1 ]; then
    printf '\nOne of the tests failed. Start with: myai doctor\n'
  fi
  cat <<EOF

Three commands to know:
  myai status    is everything running?
  myai doctor    checks the address, certificate, memory and disk, with advice
  myai bench     finds the best number of threads (the chat pauses for 10 to 15 minutes)

All of the above is saved in /root/myai-credentials.txt (only root can read it).
Show it again any time with: myai creds
EOF
}

# -------------------------------------------------------------------- main ---

main() {
  prescan "$@"
  locate_source "$@"
  # shellcheck source=bin/myai-lib.sh
  . "$SRC_DIR/bin/myai-lib.sh"
  parse_args "$@"
  PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
  trap on_exit EXIT
  trap 'FAILED_AT="$BASH_COMMAND"' ERR
  # Without these, on_exit sees the status 0 after Ctrl+C or a closed
  # terminal and says nothing.
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  set -E

  if [ "$DRY_RUN" = 1 ]; then
    say "myai installer: DRY RUN, nothing is changed. Lines with \"would\" are what a real run does;"
    say "the other lines are what it reports."
  elif [ "$OPT_UPDATE" = 1 ]; then
    say "myai: updating the app in $APP_DIR. Settings and data are kept."
  else
    say "myai installer. This sets up a private AI chat and agent on this server."
    say "It takes 15 to 60 minutes; most of that is downloading the model. Keep this window open."
    say "If it closes or the connection drops, run the same command again: it continues where it stopped."
  fi

  step "Checking this server"
  preflight
  collect_answers

  step "Installing basic tools"
  install_packages

  step "Docker"
  install_docker

  step "Protecting the server (swap, firewall, automatic updates)"
  harden

  step "App files and settings"
  configure

  step "Data folders"
  make_dirs

  step "Model file"
  fetch_model

  step "Starting the services"
  start_stack

  step "Testing"
  self_test
  remove_replaced_images

  step "The myai command, nightly backups, automatic repair"
  install_command_and_timers

  step "Done"
  final_summary
}

if [ "${MYAI_SOURCE_ONLY:-0}" != 1 ]; then main "$@"; exit $?; fi
