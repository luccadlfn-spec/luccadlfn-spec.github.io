#!/usr/bin/env bash
# claude-local.sh: run the Claude Code command-line tool against the local
# model of this server.
#
# EXPERIMENTAL, AND NOT SUPPORTED BY ANTHROPIC. Anthropic does not support
# using Claude Code with models other than Claude. What you get is your local
# model inside Claude Code's tooling, not Claude, and it can stop working
# with any update of Claude Code or of the model server. Nothing else in
# myai depends on this script.
#
# It installs nothing and changes nothing on the server. It must run on the
# server itself, as root: the model server has no published port, and this
# script talks to its container directly.

set -euo pipefail

SELF=$(readlink -f "${BASH_SOURCE[0]}")
APP_DIR=${MYAI_DIR:-$(dirname "$(dirname "$SELF")")}
ENV_FILE="$APP_DIR/.env"

# shellcheck source-path=SCRIPTDIR
# shellcheck source=../bin/myai-lib.sh
. "$(dirname "$SELF")/../bin/myai-lib.sh"

usage() {
  cat <<'EOF'
claude-local.sh: Claude Code with the local model. Experimental, and not
supported by Anthropic.

  extras/claude-local.sh --check          test the connection to the model
  extras/claude-local.sh "your question"  ask once; the answer is printed
  extras/claude-local.sh "question" --tools "Read"
                                          further options go to claude as they are

What you need:
  1. Run it on the server itself, as root (for example in the browser terminal).
  2. The Claude Code program "claude" must be installed. This script does not
     install it. Anthropic's installer: curl -fsSL https://claude.ai/install.sh | bash
  3. The model must be running: myai status

What it does: it starts "claude --bare -p" with these settings, for this
one command only:
  ANTHROPIC_BASE_URL   the model server's container (its Anthropic-style API)
  ANTHROPIC_API_KEY    the internal key of the model server
  model name           the local alias (not a "claude-..." name)
  CLAUDE_CONFIG_DIR    /root/.claude-myai-local, apart from a normal Claude Code setup
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1   no update checks or usage reports

Good to know:
  - It is slow. Claude Code sends a few thousand tokens before your question;
    on a CPU the first answer takes a minute or more.
  - While it runs, the web chat and the agent share the model with it.
  - Your question and the files Claude Code reads go only to the local model.
EOF
}

# The model server's address inside Docker. Its network has no route to the
# internet and no published port, but the server itself can reach it.
llm_url() {
  local id ip
  if [ -n "${MYAI_LLM_URL:-}" ]; then
    printf '%s\n' "${MYAI_LLM_URL%/}"
    return 0
  fi
  id=$(dc ps -q llm 2>/dev/null | awk 'NR == 1') || id=""
  [ -n "$id" ] || return 1
  ip=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}' "$id" 2>/dev/null |
    awk 'NF { print; exit }') || ip=""
  is_ipv4 "$ip" || return 1
  printf 'http://%s:8080\n' "$ip"
}

# One tiny request to the Anthropic-style endpoint. The key is passed on
# stdin so that it does not show up in the process list.
check_connection() { # URL KEY MODEL
  local out
  out=$(printf 'header = "x-api-key: %s"\n' "$2" | curl -sS -m 300 -K - \
    -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
    -d "{\"model\":\"$3\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}]}" \
    "$1/v1/messages" 2>&1) || die "The model server did not answer at $1." "curl said: $out"
  case "$out" in
    *'"type":"message"'*) say "The connection works: the model answered through $1/v1/messages" ;;
    *) die "The model server answered, but not as expected." "It said: ${out:0:300}" ;;
  esac
}

main() {
  local url key alias ctx parallel window
  case "${1:-}" in
    '' | -h | --help)
      usage
      exit 0
      ;;
  esac
  [ "$(id -u)" -eq 0 ] || die "Run this as root on the server itself." "Try: sudo $0 --check"
  [ -f "$ENV_FILE" ] || die "myai is not set up in $APP_DIR (no settings file)." "Install myai first."
  key=$(env_get LLM_API_KEY) || key=""
  [ -n "$key" ] || die "LLM_API_KEY is missing in $ENV_FILE." "Run the myai installer again; it keeps your data."
  alias=$(env_get MODEL_ALIAS) || alias=""
  alias=${alias:-assistant}
  case "$alias" in
    claude-*) die "MODEL_ALIAS in $ENV_FILE starts with \"claude-\"." \
      "Claude Code then assumes a real Claude model. Set it back to: assistant" ;;
  esac
  url=$(llm_url) || die "The model server is not running." "Start it: myai start   and check: myai status"
  curl -fs -m 10 -o /dev/null "$url/health" ||
    die "The model server at $url is not ready." "It may still be loading the model. Check: myai status"

  if [ "$1" = --check ]; then
    check_connection "$url" "$key" "$alias"
    exit 0
  fi

  command -v claude >/dev/null 2>&1 ||
    die "The program \"claude\" (Claude Code) is not installed. This script does not install it." \
      "If you want to try it: curl -fsSL https://claude.ai/install.sh | bash" \
      "Then run this command again."

  # Each request gets one of the model's slots: tell Claude Code how much
  # text fits, or it assumes a far larger window.
  ctx=$(env_get LLM_CTX) || ctx=32768
  parallel=$(env_get LLM_PARALLEL) || parallel=2
  window=16384
  if [[ $ctx =~ ^[0-9]+$ ]] && [[ $parallel =~ ^[1-9][0-9]*$ ]]; then window=$((ctx / parallel)); fi

  export ANTHROPIC_BASE_URL=$url
  export ANTHROPIC_API_KEY=$key
  export ANTHROPIC_MODEL=$alias
  export ANTHROPIC_DEFAULT_HAIKU_MODEL=$alias
  export ANTHROPIC_DEFAULT_SONNET_MODEL=$alias
  export ANTHROPIC_DEFAULT_OPUS_MODEL=$alias
  export CLAUDE_CODE_SUBAGENT_MODEL=$alias
  export CLAUDE_CONFIG_DIR=${CLAUDE_LOCAL_CONFIG_DIR:-/root/.claude-myai-local}
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  # Keeps the start of every request identical, so the model server can
  # reuse what it already read.
  export CLAUDE_CODE_ATTRIBUTION_HEADER=0
  export CLAUDE_CODE_MAX_CONTEXT_TOKENS=$window
  export CLAUDE_CODE_MAX_OUTPUT_TOKENS=4096
  # A login to Anthropic must not be used for the local model.
  unset ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
  mkdir -p "$CLAUDE_CONFIG_DIR"
  chmod 700 "$CLAUDE_CONFIG_DIR"

  printf 'Experimental: Claude Code with the local model "%s". Not supported by Anthropic.\n' "$alias" >&2
  printf 'The first answer can take a minute or more.\n\n' >&2
  exec claude --bare --model "$alias" -p "$@"
}

main "$@"
