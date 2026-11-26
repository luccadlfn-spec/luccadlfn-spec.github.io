#!/usr/bin/env bash
# The settings file (.env): reading, writing, generating, keeping.
# shellcheck disable=SC2154  # AI_DIR, T, APP, ENV_FILE, ... come from run.sh, helpers.sh and install.sh

CONTRACT_KEYS="SITE_ADDRESS CADDY_TLS_LINE GATE_TOKEN TZ DATA_DIR
LLAMA_IMAGE WEBUI_IMAGE SEARXNG_IMAGE CADDY_IMAGE
MODEL_ID MODEL_FILE MODEL_ALIAS LLM_CTX LLM_PARALLEL LLM_THREADS LLM_THREADS_BATCH
LLM_CACHE_RAM_MIB LLM_THINK_BUDGET LLM_LOAD_MODE LLM_TEMPERATURE LLM_TOP_P LLM_TOP_K LLM_MIN_P
LLM_PRESENCE_PENALTY LLM_API_KEY AGENT_API_KEY SANDBOX_TOKEN SEARXNG_SECRET WEBUI_SECRET_KEY
WEBUI_ADMIN_EMAIL WEBUI_ADMIN_PASSWORD WEBUI_ADMIN_NAME DASHBOARD_PASSWORD
AGENT_THINKING AGENT_MAX_STEPS AGENT_CTX_TOKENS AGENT_MAX_OUTPUT_TOKENS CPU_GOVERNOR CPU_BASELINE
BACKGROUND_REST_RATIO MIN_SCHEDULE_INTERVAL_MIN MAX_SCHEDULES ENABLE_RUN_CODE TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_IDS PAIRING_CODE"
SECRET_KEYS="LLM_API_KEY AGENT_API_KEY SANDBOX_TOKEN SEARXNG_SECRET WEBUI_SECRET_KEY"

# What the installer does before it writes the settings.
config_ready() {
  check_cpu >/dev/null
  select_model
  collect_answers
  mkdir -p "$APP"
}

# One installer run up to and including the settings, in its own shell.
run_config() {
  (
    load_installer "$@"
    config_ready
    write_config
  )
}

test_env_set_get_roundtrip() {
  load_installer
  mkdir -p "$APP"
  env_set A plain
  env_set B "tls internal"
  env_set C 'x&y\z/1|2'
  env_set D ""
  env_set E 'ghcr.io/ggml-org/llama.cpp@sha256:0123abcd'
  assert_eq "$(env_get A)" plain
  assert_eq "$(env_get B)" "tls internal"
  assert_eq "$(env_get C)" 'x&y\z/1|2'
  assert_eq "$(env_get D)" ""
  assert_eq "$(env_get E)" 'ghcr.io/ggml-org/llama.cpp@sha256:0123abcd'
  assert_contains "$(cat "$ENV_FILE")" "B='tls internal'" "a value with a space is single-quoted"
  assert_contains "$(cat "$ENV_FILE")" "E=ghcr.io/ggml-org/llama.cpp@sha256:0123abcd"
  assert_mode "$ENV_FILE" 600
  if env_get MISSING >/dev/null; then fail "env_get must fail for a missing key"; fi
  env_has A || fail "env_has A"
  if env_has MISSING; then fail "env_has MISSING"; fi
}

test_env_set_replaces_one_line_and_keeps_the_rest() {
  load_installer
  mkdir -p "$APP"
  printf '# a comment\nA=1\nX=keep me\n\nB=2\n' >"$ENV_FILE"
  chmod 644 "$ENV_FILE"
  env_set A 9
  assert_eq "$(cat "$ENV_FILE")" $'# a comment\nA=9\nX=keep me\n\nB=2' "file after replacing A"
  env_set NEW value
  assert_eq "$(tail -n 1 "$ENV_FILE")" "NEW=value" "a new key is appended"
  assert_mode "$ENV_FILE" 600
  # A key that is a prefix of another must not match it.
  printf 'LLM_THREADS_BATCH=8\nLLM_THREADS=6\n' >"$ENV_FILE"
  env_set LLM_THREADS 4
  assert_eq "$(cat "$ENV_FILE")" $'LLM_THREADS_BATCH=8\nLLM_THREADS=4'
  assert_eq "$(env_get LLM_THREADS)" 4
  assert_eq "$(env_get LLM_THREADS_BATCH)" 8
}

test_env_get_reads_quoted_and_odd_values() {
  load_installer
  mkdir -p "$APP"
  printf 'Q1="double quoted"\nQ2=%s\nQ3=a=b=c\nQ4=crlf\r\nQ5=\n' "'single quoted'" >"$ENV_FILE"
  assert_eq "$(env_get Q1)" "double quoted"
  assert_eq "$(env_get Q2)" "single quoted"
  assert_eq "$(env_get Q3)" "a=b=c"
  assert_eq "$(env_get Q4)" "crlf"
  assert_eq "$(env_get Q5)" ""
}

test_env_set_refuses_values_it_cannot_store() {
  load_installer
  mkdir -p "$APP"
  expect_fail env_set BAD "it's"
  assert_contains "$OUT" "cannot be stored"
  expect_fail env_set BAD $'two\nlines'
  expect_fail env_set "BAD KEY" x
  assert_no_file "$ENV_FILE"
}

test_config_fresh_file_has_every_key_and_fresh_secrets() {
  local key out seen="" value
  run_config >"$T/out" 2>&1 || fail "write_config failed: $(cat "$T/out")"
  load_installer
  out=$(cat "$T/out")
  assert_file "$ENV_FILE"
  assert_mode "$ENV_FILE" 600
  for key in $CONTRACT_KEYS; do
    env_has "$key" || fail "the settings file lacks $key"
  done
  if grep -E '=@[a-z0-9_]+$' "$ENV_FILE"; then fail "a placeholder was left in the settings file"; fi

  [[ $(env_get GATE_TOKEN) =~ ^[0-9a-f]{32}$ ]] || fail "GATE_TOKEN is not 32 hex characters"
  for key in $SECRET_KEYS; do
    value=$(env_get "$key")
    [[ $value =~ ^[0-9a-f]{64}$ ]] || fail "$key is not 64 hex characters"
    assert_not_contains "$seen" "$value" "secrets must all differ; $key"
    assert_not_contains "$out" "$value" "the installer output must not show $key; output"
    seen+=" $value"
  done
  value=$(env_get WEBUI_ADMIN_PASSWORD)
  [[ $value =~ ^[A-Za-z0-9]{16,}$ ]] || fail "the admin password must be 16+ letters and digits, got '$value'"
  assert_eq "$(env_get DASHBOARD_PASSWORD)" "$value" "DASHBOARD_PASSWORD equals the admin password"
  assert_not_contains "$out" "$value" "the password is not shown while writing the settings; output"
  [[ $(env_get PAIRING_CODE) =~ ^[0-9]{8}$ ]] || fail "PAIRING_CODE is not 8 digits"

  assert_eq "$(env_get SITE_ADDRESS)" "srv123456.hstgr.cloud"
  assert_eq "$(env_get CADDY_TLS_LINE)" ""
  assert_eq "$(env_get DATA_DIR)" "$APP/data"
  assert_eq "$(env_get MODEL_ID)" hauhau
  assert_eq "$(env_get MODEL_FILE)" "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
  assert_eq "$(env_get MODEL_ALIAS)" assistant
  assert_eq "$(env_get LLM_THREADS)" 6
  assert_eq "$(env_get LLM_THREADS_BATCH)" 8
  assert_eq "$(env_get LLM_CTX)" 32768
  assert_eq "$(env_get AGENT_CTX_TOKENS)" 14000
  assert_eq "$(env_get CPU_GOVERNOR)" on "governor on a .hstgr.cloud machine"
  assert_eq "$(env_get WEBUI_ADMIN_EMAIL)" "owner@example.com"
  assert_eq "$(env_get TELEGRAM_BOT_TOKEN)" ""
  [ -n "$(env_get TZ)" ] || fail "TZ is empty"
  assert_contains "$(env_get LLAMA_IMAGE)" "llama.cpp"
}

test_config_time_zone_comes_from_the_system() {
  echo "Europe/Vilnius" >"$FAKE_DIR/tz"
  run_config >/dev/null 2>&1
  load_installer
  assert_eq "$(env_get TZ)" "Europe/Vilnius"
}

test_config_covers_every_key_of_env_example() {
  local key
  [ -f "$AI_DIR/.env.example" ] || skip ".env.example is not there yet"
  run_config >/dev/null 2>&1
  load_installer
  while IFS= read -r key; do
    env_has "$key" || fail "$key is documented in .env.example but not written by the installer"
  done < <(grep -o -E '^[A-Z][A-Z0-9_]*=' "$AI_DIR/.env.example" | tr -d '=')
}

test_config_real_compose_accepts_the_generated_file() {
  [ -n "$REAL_DOCKER" ] || skip "docker is not installed"
  [ -f "$AI_DIR/docker-compose.yml" ] || skip "docker-compose.yml is not there yet"
  "$REAL_DOCKER" compose version >/dev/null 2>&1 || skip "docker compose is not available"
  run_config >/dev/null 2>&1
  expect_ok "$REAL_DOCKER" compose --project-directory "$AI_DIR" -f "$AI_DIR/docker-compose.yml" \
    --env-file "$APP/.env" config -q
}

test_config_rerun_keeps_the_file_byte_for_byte() {
  run_config >/dev/null 2>&1
  cp -p "$APP/.env" "$T/first"
  expect_ok run_config
  cmp -s "$T/first" "$APP/.env" || fail "a second run changed the settings file:
$(diff "$T/first" "$APP/.env")"
  assert_contains "$OUT" "Kept $APP/.env as it is"
  assert_mode "$APP/.env" 600
}

test_config_rerun_adds_only_what_is_missing() {
  local key before after
  run_config >/dev/null 2>&1
  cp "$APP/.env" "$T/first"
  # The owner tuned a value, added a line of their own, and two keys are gone.
  sed -i -e '/^AGENT_MAX_STEPS=/d' -e '/^PAIRING_CODE=/d' -e 's/^LLM_THREADS=.*/LLM_THREADS=4/' "$APP/.env"
  printf '# my own note\nMY_OWN_SETTING=1' >>"$APP/.env" # no newline at the end, on purpose
  expect_ok run_config
  load_installer
  assert_eq "$(env_get AGENT_MAX_STEPS)" 12 "the missing key is back"
  [[ $(env_get PAIRING_CODE) =~ ^[0-9]{8}$ ]] || fail "PAIRING_CODE was not added again"
  assert_eq "$(env_get LLM_THREADS)" 4 "the tuned value stays"
  assert_eq "$(env_get MY_OWN_SETTING)" 1 "the owner's own line stays"
  grep -qx '# my own note' "$APP/.env" || fail "the owner's comment is gone"
  for key in $SECRET_KEYS GATE_TOKEN WEBUI_ADMIN_PASSWORD DASHBOARD_PASSWORD SITE_ADDRESS; do
    before=$(env_get "$key" "$T/first")
    after=$(env_get "$key")
    assert_eq "$after" "$before" "$key after a second run"
  done
  assert_eq "$(grep -c '^MY_OWN_SETTING=' "$APP/.env")" 1
  assert_eq "$(grep -c '^AGENT_MAX_STEPS=' "$APP/.env")" 1
  assert_contains "$OUT" "added 2 new setting(s)"
}

test_config_rerun_with_options_changes_only_those() {
  local key
  echo "203.0.113.10" >"$FAKE_DIR/dig_A_chat.example.com"
  run_config >/dev/null 2>&1
  cp "$APP/.env" "$T/first"
  expect_ok run_config --domain chat.example.com --model fast --governor off --admin-email me@example.com
  load_installer
  assert_eq "$(env_get SITE_ADDRESS)" "chat.example.com"
  assert_eq "$(env_get CADDY_TLS_LINE)" ""
  assert_eq "$(env_get MODEL_ID)" fast
  assert_eq "$(env_get MODEL_FILE)" "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
  assert_eq "$(env_get CPU_GOVERNOR)" off
  assert_eq "$(env_get WEBUI_ADMIN_EMAIL)" "me@example.com"
  for key in $SECRET_KEYS GATE_TOKEN WEBUI_ADMIN_PASSWORD PAIRING_CODE LLM_THREADS; do
    assert_eq "$(env_get "$key")" "$(env_get "$key" "$T/first")" "$key"
  done
  # Same number of lines: values were replaced in place, nothing was added.
  assert_eq "$(wc -l <"$APP/.env")" "$(wc -l <"$T/first")" "number of lines"
}

test_config_rerun_tls_internal_option() {
  run_config >/dev/null 2>&1
  run_config --tls-internal >/dev/null 2>&1
  load_installer
  assert_eq "$(env_get CADDY_TLS_LINE)" "tls internal"
  assert_eq "$(env_get SITE_ADDRESS)" "srv123456.hstgr.cloud" "the address stays"
}

test_config_admin_email_option_on_first_run() {
  run_config --admin-email me@example.com >/dev/null 2>&1
  load_installer
  assert_eq "$(env_get WEBUI_ADMIN_EMAIL)" "me@example.com"
}

test_config_low_memory_server_gets_smaller_windows() {
  fake_machine 4 8000000
  run_config >/dev/null 2>&1
  load_installer
  assert_eq "$(env_get MODEL_ID)" fast
  assert_eq "$(env_get LLM_CTX)" 16384
  assert_eq "$(env_get AGENT_CTX_TOKENS)" 6000
  assert_eq "$(env_get LLM_CACHE_RAM_MIB)" 512
  assert_eq "$(env_get LLM_THREADS)" 3
  assert_eq "$(env_get LLM_THREADS_BATCH)" 4
  # The agent's budget must fit into one of the model's slots.
  [ "$(env_get AGENT_CTX_TOKENS)" -lt $(($(env_get LLM_CTX) / $(env_get LLM_PARALLEL))) ] || fail "agent budget too large"
}

test_config_dry_run_writes_nothing_and_hides_secrets() {
  expect_ok run_config --dry-run
  assert_no_file "$APP/.env"
  assert_contains "$OUT" "would write: $APP/.env (mode 600)"
  assert_contains "$OUT" "GATE_TOKEN=<hidden>"
  assert_contains "$OUT" "LLM_API_KEY=<hidden>"
  assert_contains "$OUT" "SITE_ADDRESS=srv123456.hstgr.cloud"
  assert_contains "$OUT" "TELEGRAM_BOT_TOKEN="
}

test_config_warns_when_agent_budget_exceeds_a_slot() {
  run_config >/dev/null 2>&1
  sed -i 's/^AGENT_CTX_TOKENS=.*/AGENT_CTX_TOKENS=20000/' "$APP/.env"
  expect_ok run_config
  assert_contains "$OUT" "AGENT_CTX_TOKENS (20000) must be smaller"
}
