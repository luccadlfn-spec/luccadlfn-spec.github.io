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
                                          let it read files in this folder;
                                          further options go to claude as they are

It runs as root, so the model may only read files. Claude Code always starts
with --restricted --strict-mcp-config --permission-mode dontAsk: no tool that
runs commands or fetches web pages, no settings files of yours, nothing that
would have to ask first. Options that give it more (--allowedTools,
--dangerously-skip-permissions, --permission-mode acceptEdits, --settings,
--mcp-config, plugins, agents, or tools other than Read, Glob and Grep) are
refused: as root, the local model, or text planted in a web page that the
agent saved, could otherwise run commands on this server with full power.
For more than reading, set Claude Code up under an ordinary user account; this
script does not do that.

What you need:
  1. Run it on the server itself, as root (for example in the browser terminal).
  2. The Claude Code program "claude" must be installed, in a version that
     knows --restricted. This script does not install it. Anthropic's
     installer: curl -fsSL https://claude.ai/install.sh | bash
  3. The model must be running: myai status

What it does: it starts "claude --bare --restricted -p" with these settings,
for this one command only:
  ANTHROPIC_BASE_URL   the model server's container (its Anthropic-style API)
  ANTHROPIC_API_KEY    the internal key of the model server
  model name           the local alias (not a "claude-..." name)
  CLAUDE_CONFIG_DIR    /root/.claude-myai-local, apart from a normal Claude Code setup
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1   no update checks or usage reports

Good to know:
  - It is slow. Claude Code sends about 1,100 tokens of its own instructions
    with every question; on a CPU the first answer takes a minute or more.
  - Claude Code prints a line with "unrecognized_model". That is expected:
    it does not know the local model's name.
  - One request may hold about LLM_CTX / LLM_PARALLEL tokens (16384 with the
    default settings). A task that reads many files outgrows that and stops
    with an error; give it smaller tasks.
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

refuse_option() { # OPTION
  die "Refused: $1. Claude Code runs as root here, and this would let the local model run commands or change files on this server with full power." \
    "As root it may only read files: --tools with Read, Glob or Grep." \
    "For more, set Claude Code up under an ordinary user account; this script does not do that."
}

# --tools values: names separated by commas or spaces; "" means none.
check_tools() { # VALUE
  local names=() tool
  IFS=', ' read -r -a names <<<"$1"
  for tool in ${names[@]+"${names[@]}"}; do
    case "$tool" in
      Read | Glob | Grep) ;;
      *) refuse_option "--tools $tool" ;;
    esac
  done
}

# Refuses the options that would give the model more than reading: tools,
# permissions, settings files, MCP servers, plugins and agents.
check_options() {
  local args=("$@") n=$# i=0 arg name value has_value
  while [ "$i" -lt "$n" ]; do
    arg=${args[$i]}
    i=$((i + 1))
    case "$arg" in
      --*=*)
        name=${arg%%=*}
        value=${arg#*=}
        has_value=1
        ;;
      *)
        name=$arg
        value=""
        has_value=0
        ;;
    esac
    case "$name" in
      --dangerously-skip-permissions | --allow-dangerously-skip-permissions | --allowedTools | --allowed-tools | \
        --settings | --setting-sources | --mcp-config | --plugin-dir | --plugin-url | --agents | --agent | \
        --permission-prompt-tool)
        refuse_option "$name"
        ;;
      --permission-mode)
        if [ "$has_value" = 0 ]; then
          value=${args[$i]:-}
          i=$((i + 1))
        fi
        case "$value" in
          default | dontAsk | plan | manual) ;;
          *) refuse_option "--permission-mode $value" ;;
        esac
        ;;
      --tools)
        if [ "$has_value" = 1 ]; then check_tools "$value"; fi
        # Its values follow as words of their own, up to the next option.
        while [ "$i" -lt "$n" ] && [[ ${args[$i]} != -* ]]; do
          check_tools "${args[$i]}"
          i=$((i + 1))
        done
        ;;
    esac
  done
}

# One tiny request to the Anthropic-style endpoint. The key is passed on
# stdin so that it does not show up in the process list.
check_connection() { # URL KEY MODEL
  local out
  out=$(printf 'header = "x-api-key: %s"\n' "$2" | curl -sS -m 300 --noproxy '*' -K - \
    -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
    -d "{\"model\":\"$3\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}]}" \
    "$1/v1/messages" 2>&1) || die "The model server did not answer at $1." "curl said: $out"
  case "$out" in
    *'"type":"message"'*) say "The connection works: the model answered through $1/v1/messages" ;;
    *) die "The model server answered, but not as expected." "It said: ${out:0:300}" ;;
  esac
}

main() {
  local url key alias ctx parallel window help
  case "${1:-}" in
    '' | -h | --help)
      usage
      exit 0
      ;;
  esac
  [ "$(id -u)" -eq 0 ] || die "Run this as root on the server itself." "Try: sudo $0 --check"
  check_options "$@"
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
  # --noproxy: a proxy, if one is set, cannot reach a container's address.
  curl -fs -m 10 --noproxy '*' -o /dev/null "$url/health" ||
    die "The model server at $url is not ready." "It may still be loading the model. Check: myai status"

  if [ "$1" = --check ]; then
    check_connection "$url" "$key" "$alias"
    exit 0
  fi

  command -v claude >/dev/null 2>&1 ||
    die "The program \"claude\" (Claude Code) is not installed. This script does not install it." \
      "If you want to try it: curl -fsSL https://claude.ai/install.sh | bash" \
      "Then run this command again."
  # Without --restricted the model could get tools that run commands as root.
  help=$(claude --help 2>/dev/null) || help=""
  [[ $help == *--restricted* ]] ||
    die "This version of Claude Code does not know --restricted, which keeps the local model to reading files." \
      "Update it with: claude update   Then run this command again."

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
  # Claude Code's automatic summary needs a far larger window than one slot.
  # With this one it summarised even a new one-line question before answering
  # (measured: 92 s of 99 s). A run that outgrows the window stops instead.
  export DISABLE_AUTO_COMPACT=1
  # Leave out request fields that only Anthropic's own API knows.
  export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
  # Reading a long request on a CPU can take longer than the default 10 minutes.
  export API_TIMEOUT_MS=1800000
  # A login to Anthropic must not be used for the local model.
  unset ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
  mkdir -p "$CLAUDE_CONFIG_DIR"
  chmod 700 "$CLAUDE_CONFIG_DIR"

  printf 'Experimental: Claude Code with the local model "%s". Not supported by Anthropic.\n' "$alias" >&2
  printf 'The first answer can take a minute or more. A line about "unrecognized_model" is expected.\n\n' >&2
  # As root: no tools that run commands or fetch pages, no settings files or
  # MCP servers that could allow more, and whatever would ask is refused.
  exec claude --bare --restricted --strict-mcp-config --permission-mode dontAsk --model "$alias" -p "$@"
}

main "$@"
