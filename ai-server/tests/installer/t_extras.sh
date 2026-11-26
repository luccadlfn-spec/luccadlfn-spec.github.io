#!/usr/bin/env bash
# hostinger/post-install.sh and extras/claude-local.sh. Nothing here reaches
# the network: curl is the fake, and the model server is the local file server.
# shellcheck disable=SC2154  # variables come from run.sh and helpers.sh

# ------------------------------------------------------ post-install.sh ---

# The installer that post-install.sh downloads for REF: a stub that says how
# it was started and ends with STATUS.
fake_raw_installer() { # REF STATUS
  cat >"$FAKE_DIR/raw_$1_install.sh" <<STUB
echo "installer started with: \$* (MYAI_REF=\$MYAI_REF)"
exit $2
STUB
}

run_post_install() {
  local rc=0
  bash "$AI_DIR/hostinger/post-install.sh" >"$T/post.out" 2>&1 || rc=$?
  return "$rc"
}

test_post_install_runs_the_installer_without_questions_and_logs_it() {
  local rc=0 log="$MYAI_SYSROOT/root/myai-install.log"
  fake_raw_installer ccr-f9008ee5-63plj0 0
  run_post_install || rc=$?
  assert_eq "$rc" 0 "exit code"
  assert_eq "$(cat "$T/post.out")" "" "output outside the log"
  assert_file "$log"
  assert_mode "$log" 600
  assert_contains "$(cat "$log")" "Installer downloaded from https://raw.githubusercontent.com/luccadlfn-spec/luccadlfn-spec.github.io/ccr-f9008ee5-63plj0/ai-server/install.sh"
  assert_contains "$(cat "$log")" "installer started with: --yes (MYAI_REF=ccr-f9008ee5-63plj0)"
  assert_contains "$(cat "$log")" "finished with status 0"
}

test_post_install_falls_back_to_main_and_passes_on_a_failure() {
  local rc=0 log="$MYAI_SYSROOT/root/myai-install.log"
  fake_raw_installer main 4 # only main exists; the installer fails
  run_post_install || rc=$?
  assert_eq "$rc" 4 "exit code of the installer"
  assert_call_order "/ccr-f9008ee5-63plj0/ai-server/install.sh" "/main/ai-server/install.sh"
  assert_contains "$(cat "$log")" "(MYAI_REF=main)"
  assert_contains "$(cat "$log")" "finished with status 4"
}

test_post_install_explains_when_nothing_can_be_downloaded() {
  local rc=0 log="$MYAI_SYSROOT/root/myai-install.log"
  run_post_install || rc=$?
  assert_eq "$rc" 1 "exit code"
  assert_contains "$(cat "$log")" "The installer could not be downloaded. Run it by hand"
  # The branch the app files are on, which is tried first.
  assert_contains "$(cat "$log")" "curl -fsSL https://raw.githubusercontent.com/luccadlfn-spec/luccadlfn-spec.github.io/ccr-f9008ee5-63plj0/ai-server/install.sh | bash"
}

# ------------------------------------------------------ claude-local.sh ---

# An installed myai whose model server is the local file server, and a fake
# "claude" that prints what it was started with.
claude_local_ready() {
  mkdir -p "$APP" "$T/llm/v1"
  printf 'LLM_API_KEY=testkey123\nMODEL_ALIAS=assistant\nLLM_CTX=32768\nLLM_PARALLEL=2\n' >"$APP/.env"
  echo '{"status":"ok"}' >"$T/llm/health"
  echo '{"id":"1","type":"message","role":"assistant","content":[{"type":"text","text":"OK"}]}' >"$T/llm/v1/messages"
  start_fileserver "$T/llm"
  export MYAI_LLM_URL="http://127.0.0.1:$HF_PORT" CLAUDE_LOCAL_CONFIG_DIR="$T/claude-config"
  cat >"$T/bin/claude" <<'FAKE'
#!/usr/bin/env bash
if [ "${1:-}" = --help ]; then
  # $FAKE_DIR/claude_old: a version from before --restricted.
  if [ -e "${FAKE_DIR:?}/claude_old" ]; then echo "  --bare   Minimal mode"; else echo "  --restricted   Restricted mode"; fi
  exit 0
fi
printf 'claude args:'
printf ' [%s]' "$@"
printf '\n'
for v in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
  CLAUDE_CODE_SUBAGENT_MODEL CLAUDE_CONFIG_DIR CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC \
  CLAUDE_CODE_MAX_CONTEXT_TOKENS DISABLE_AUTO_COMPACT ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN; do
  printf '%s=%s\n' "$v" "${!v-<unset>}"
done
FAKE
  chmod 755 "$T/bin/claude"
}

run_claude_local() { MYAI_DIR="$APP" bash "$AI_DIR/extras/claude-local.sh" "$@"; }

test_claude_local_without_arguments_explains_itself() {
  expect_ok run_claude_local
  assert_contains "$OUT" "Experimental, and not"
  assert_contains "$OUT" "supported by Anthropic"
  assert_contains "$OUT" "does not"
  assert_contains "$OUT" "install it"
  assert_contains "$OUT" "--check"
}

test_claude_local_runs_claude_with_the_local_model_only() {
  claude_local_ready
  # A login to Anthropic in the caller's environment must not be passed on.
  ANTHROPIC_AUTH_TOKEN=from-a-login CLAUDE_CODE_OAUTH_TOKEN=from-a-login \
    expect_ok run_claude_local "What is 2+2?" --tools Read
  # As root: no command-running tools, no settings files, nothing that would ask.
  assert_contains "$OUT" "claude args: [--bare] [--restricted] [--strict-mcp-config] [--permission-mode] [dontAsk] [--model] [assistant] [-p] [What is 2+2?] [--tools] [Read]"
  assert_contains "$OUT" "ANTHROPIC_BASE_URL=http://127.0.0.1:$HF_PORT"
  assert_contains "$OUT" "ANTHROPIC_API_KEY=testkey123"
  assert_contains "$OUT" "ANTHROPIC_MODEL=assistant"
  assert_contains "$OUT" "ANTHROPIC_DEFAULT_HAIKU_MODEL=assistant"
  assert_contains "$OUT" "CLAUDE_CODE_SUBAGENT_MODEL=assistant"
  assert_contains "$OUT" "CLAUDE_CONFIG_DIR=$T/claude-config"
  assert_contains "$OUT" "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
  assert_contains "$OUT" "CLAUDE_CODE_MAX_CONTEXT_TOKENS=16384" "one slot: LLM_CTX / LLM_PARALLEL"
  assert_contains "$OUT" "DISABLE_AUTO_COMPACT=1"
  assert_contains "$OUT" "ANTHROPIC_AUTH_TOKEN=<unset>"
  assert_contains "$OUT" "CLAUDE_CODE_OAUTH_TOKEN=<unset>"
  assert_contains "$OUT" "Not supported by Anthropic"
  assert_mode "$T/claude-config" 700
  assert_contains "$(cat "$T/http.log")" "GET /health"
}

test_claude_local_refuses_what_would_let_the_model_act_as_root() {
  local opts
  claude_local_ready
  while IFS= read -r opts; do
    # shellcheck disable=SC2086  # each line is a list of words
    expect_fail run_claude_local "Clean up this server" $opts
    assert_contains "$OUT" "Refused: "
    assert_contains "$OUT" "run commands or change files on this server with full power"
    assert_not_contains "$OUT" "claude args:" "the output for: $opts"
  done <<'OPTIONS'
--dangerously-skip-permissions
--allow-dangerously-skip-permissions
--allowedTools Bash
--allowed-tools=Edit
--permission-mode bypassPermissions
--permission-mode=acceptEdits
--permission-mode auto
--settings /root/permissive.json
--setting-sources=user,project
--mcp-config /root/mcp.json
--plugin-dir /root/plugin
--agents /root/agents.json
--permission-prompt-tool mcp__x__approve
--tools Bash
--tools Read,Bash
--tools Read Write
--tools=default
--output-format json --tools Grep WebFetch
OPTIONS
  assert_contains "$OUT" "Refused: --tools WebFetch"
  assert_eq "$(grep -c "POST /v1/messages" "$T/http.log" || true)" 0 "requests to the model"
}

test_claude_local_lets_the_model_read() {
  claude_local_ready
  expect_ok run_claude_local "Summarise the notes" --tools Read,Grep --tools Glob --permission-mode plan \
    --output-format json --add-dir /srv/notes
  assert_contains "$OUT" "[-p] [Summarise the notes] [--tools] [Read,Grep] [--tools] [Glob] [--permission-mode] [plan] [--output-format] [json] [--add-dir] [/srv/notes]"
  expect_ok run_claude_local "Only answer" --tools ""
  assert_contains "$OUT" "[--tools] []"
}

test_claude_local_needs_a_claude_that_knows_restricted() {
  claude_local_ready
  : >"$FAKE_DIR/claude_old"
  expect_fail run_claude_local "What is 2+2?"
  assert_contains "$OUT" "does not know --restricted"
  assert_contains "$OUT" "claude update"
  assert_not_contains "$OUT" "claude args:" "the output"
}

test_claude_local_usage_explains_the_limits_as_root() {
  expect_ok run_claude_local --help
  assert_contains "$OUT" "It runs as root, so the model may only read files."
  assert_contains "$OUT" "--restricted --strict-mcp-config --permission-mode dontAsk"
}

test_claude_local_check_sends_the_key_as_a_header() {
  claude_local_ready
  expect_ok run_claude_local --check
  assert_contains "$OUT" "The connection works"
  assert_contains "$(cat "$T/http.log")" "POST /v1/messages range=- x-api-key=testkey123"
  echo '{"error":{"message":"Invalid API Key"}}' >"$T/llm/v1/messages"
  expect_fail run_claude_local --check
  assert_contains "$OUT" "answered, but not as expected"
}

test_claude_local_refuses_what_cannot_work() {
  claude_local_ready
  echo 1000 >"$FAKE_DIR/uid"
  expect_fail run_claude_local --check
  assert_contains "$OUT" "Run this as root"
  echo 0 >"$FAKE_DIR/uid"
  sed -i 's/^MODEL_ALIAS=.*/MODEL_ALIAS=claude-local/' "$APP/.env"
  expect_fail run_claude_local --check
  assert_contains "$OUT" "starts with \"claude-\""
  sed -i -e 's/^MODEL_ALIAS=.*/MODEL_ALIAS=assistant/' -e 's/^LLM_API_KEY=.*/LLM_API_KEY=/' "$APP/.env"
  expect_fail run_claude_local --check
  assert_contains "$OUT" "LLM_API_KEY is missing"
  sed -i 's/^LLM_API_KEY=.*/LLM_API_KEY=testkey123/' "$APP/.env"
  rm "$T/bin/claude"
  expect_fail run_claude_local "a question"
  assert_contains "$OUT" "is not installed. This script does not install it."
  rm "$APP/.env"
  expect_fail run_claude_local --check
  assert_contains "$OUT" "myai is not set up in $APP"
}

test_claude_local_finds_the_model_server_container() {
  claude_local_ready
  unset MYAI_LLM_URL
  echo "172.30.0.9" >"$FAKE_DIR/ip_llm"
  # The fake curl reaches no 172.30 address, so the check of the server fails;
  # what matters is the address it was looking for.
  expect_fail run_claude_local --check
  assert_contains "$OUT" "The model server at http://172.30.0.9:8080 is not ready."
  : >"$FAKE_DIR/missing_llm"
  expect_fail run_claude_local --check
  assert_contains "$OUT" "The model server is not running."
}
