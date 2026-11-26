#!/usr/bin/env bash
# Getting the app files, starting the services, the self-test, timers and
# the final summary. Docker is a fake that logs what it is asked to do.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and install.sh

DIGEST="sha256:1111111111111111111111111111111111111111111111111111111111111111"

# An installer shell at the point after "settings": app files copied to
# $APP, settings written, PLAN filled in.
installed() {
  load_installer "$@"
  check_cpu >/dev/null
  select_model
  collect_answers
  install_app_files >/dev/null
  write_config >/dev/null 2>&1
}

with_model_file() {
  mkdir -p "${PLAN[DATA_DIR]}/models" "${PLAN[DATA_DIR]}/workspace"
  printf 'GGUFfake' >"${PLAN[DATA_DIR]}/models/${PLAN[MODEL_FILE]}"
}

compose_calls() { grep '^docker compose' "$FAKE_LOG" | sed 's/.*--env-file [^ ]* //'; }

# ------------------------------------------------------------ app files ---

make_source_tree() { # DIR
  mkdir -p "$1"/{bin,config,agent/app,sandbox}
  echo "services: {}" >"$1/docker-compose.yml"
  echo "#!/bin/sh" >"$1/install.sh"
  echo "#!/bin/sh" >"$1/bin/myai"
  echo "# lib" >"$1/bin/myai-lib.sh"
  echo "new code" >"$1/agent/app/new.py"
  echo "catalogue" >"$1/config/models.conf"
}

test_app_files_copy_never_touches_settings_data_or_backups() {
  load_installer
  make_source_tree "$T/src"
  # Things a developer's checkout might contain and that must not be copied.
  echo "SECRET_FROM_A_DEV_MACHINE=1" >"$T/src/.env"
  mkdir -p "$T/src/data/webui" "$T/src/backups" "$T/src/agent/app/__pycache__"
  echo "dev data" >"$T/src/data/webui/dev.db"
  echo "x" >"$T/src/agent/app/__pycache__/new.cpython-312.pyc"
  # What the installed app already has.
  mkdir -p "$APP"/{data/webui,backups,agent/app,config}
  echo "REAL=1" >"$APP/.env"
  echo "real chats" >"$APP/data/webui/webui.db"
  echo "a backup" >"$APP/backups/myai-1.tar.gz"
  echo "stale" >"$APP/agent/app/removed_upstream.py"
  echo "own" >"$APP/config/own.conf"
  SRC_DIR="$T/src"
  expect_ok install_app_files
  assert_eq "$(cat "$APP/.env")" "REAL=1" "the settings file"
  assert_eq "$(cat "$APP/data/webui/webui.db")" "real chats"
  assert_no_file "$APP/data/webui/dev.db"
  assert_eq "$(cat "$APP/backups/myai-1.tar.gz")" "a backup"
  assert_no_file "$APP/agent/app/removed_upstream.py"
  assert_eq "$(cat "$APP/agent/app/new.py")" "new code"
  assert_no_file "$APP/agent/app/__pycache__"
  assert_eq "$(cat "$APP/config/own.conf")" "own" "a file the owner added to config/"
  [ -x "$APP/bin/myai" ] || fail "bin/myai is not executable"
  assert_file "$APP/docker-compose.yml"
}

test_app_files_same_directory_is_left_alone() {
  load_installer
  make_source_tree "$APP"
  SRC_DIR="$APP"
  expect_ok install_app_files
  assert_contains "$OUT" "already in $APP"
  assert_eq "$(cat "$APP/agent/app/new.py")" "new code"
}

# Pack the repository the way GitHub does: one top folder, the website next
# to ai-server/.
make_github_tarball() { # REF
  local top="$T/pack/luccadlfn-spec.github.io-$1"
  mkdir -p "$top"
  cp -r "$AI_DIR" "$top/ai-server"
  echo "<html>the website</html>" >"$top/index.html"
  mkdir -p "$top/cw"
  echo "x" >"$top/cw/page.html"
  "$REAL_TAR" -czf "$FAKE_DIR/codeload_$1.tar.gz" -C "$T/pack" "luccadlfn-spec.github.io-$1"
}

test_bootstrap_fetch_installs_only_the_ai_server_part() {
  load_installer
  make_github_tarball ccr-f9008ee5-63plj0
  mkdir -p "$T/stage"
  fetch_app_files "$T/stage" || fail "fetch_app_files failed"
  assert_eq "$FETCHED_REF" "ccr-f9008ee5-63plj0"
  assert_file "$T/stage/docker-compose.yml"
  assert_file "$T/stage/install.sh"
  assert_file "$T/stage/bin/myai-lib.sh"
  assert_file "$T/stage/config/models.conf"
  assert_no_file "$T/stage/index.html"
  assert_no_file "$T/stage/cw"
  assert_no_file "$T/stage/ai-server"
  assert_no_file "$T/stage/app.tar.gz"
  assert_called "https://codeload.github.com/luccadlfn-spec/luccadlfn-spec.github.io/tar.gz/ccr-f9008ee5-63plj0"
}

test_bootstrap_falls_back_to_main() {
  load_installer
  make_github_tarball main
  mkdir -p "$T/stage"
  fetch_app_files "$T/stage" || fail "fetch_app_files failed"
  assert_eq "$FETCHED_REF" main
  assert_call_order "tar.gz/ccr-f9008ee5-63plj0" "tar.gz/main"
}

test_bootstrap_explicit_ref_is_the_only_one_tried() {
  load_installer
  make_github_tarball main
  mkdir -p "$T/stage"
  export MYAI_REF=v9 MYAI_REPO=someone/fork
  if fetch_app_files "$T/stage"; then fail "a missing MYAI_REF must not fall back to another branch"; fi
  assert_called "https://codeload.github.com/someone/fork/tar.gz/v9"
  assert_not_called "tar.gz/main"
  assert_not_called "tar.gz/ccr-"
}

test_bootstrap_refuses_odd_repo_names() {
  load_installer
  mkdir -p "$T/stage"
  export MYAI_REPO='someone/fork; rm -rf /'
  expect_fail fetch_app_files "$T/stage"
  assert_contains "$OUT" "MYAI_REPO must look like owner/name"
  assert_not_called "curl"
}

# ----------------------------------------------------------------- start ---

test_stack_service_states() {
  load_installer
  mkdir -p "$APP"
  : >"$APP/.env"
  local line want
  while IFS='|' read -r line want; do
    echo "$line" >"$FAKE_DIR/state_llm"
    assert_eq "$(svc_state llm)" "$want" "state for '$line'"
  done <<'STATES'
running healthy 0|healthy
running starting 0|starting
running starting 5|failing
running unhealthy 0|unhealthy
running none 0|running
exited none 0|exited
restarting none 1|restarting
restarting none 7|failing
created none 0|created
STATES
  : >"$FAKE_DIR/missing_llm"
  assert_eq "$(svc_state llm)" missing
}

test_stack_wait_healthy_waits_for_the_model() {
  load_installer
  mkdir -p "$APP"
  : >"$APP/.env"
  printf 'running starting 0\nrunning starting 0\nrunning healthy 0\n' >"$FAKE_DIR/state_llm"
  wait_healthy llm 30 "the model" || fail "wait_healthy should succeed"
  assert_eq "$(grep -c 'docker inspect' "$FAKE_LOG")" 3 "number of polls"
}

test_stack_wait_healthy_notices_a_crash_and_a_timeout() {
  local rc=0
  load_installer
  mkdir -p "$APP"
  : >"$APP/.env"
  echo "exited none 0" >"$FAKE_DIR/state_llm"
  wait_healthy llm 30 || rc=$?
  assert_eq "$rc" 2 "a stopped service"
  echo "restarting none 9" >"$FAKE_DIR/state_llm"
  rc=0
  wait_healthy llm 30 || rc=$?
  assert_eq "$rc" 2 "a service that keeps crashing"
  echo "running starting 0" >"$FAKE_DIR/state_llm"
  rc=0
  MYAI_POLL_SECS=0.2 wait_healthy llm 1 >"$T/out" || rc=$?
  assert_eq "$rc" 1 "a timeout"
}

test_stack_wait_healthy_prints_progress() {
  load_installer
  mkdir -p "$APP"
  : >"$APP/.env"
  echo "running starting 0" >"$FAKE_DIR/state_llm"
  MYAI_POLL_SECS=0.3 MYAI_PROGRESS_SECS=1 wait_healthy llm 3 "the model to load" >"$T/out" || true
  assert_contains "$(cat "$T/out")" "waiting for the model to load ... 1 s so far"
}

test_stack_image_names() {
  load_installer
  assert_eq "$(image_repo ghcr.io/ggml-org/llama.cpp:server)" "ghcr.io/ggml-org/llama.cpp"
  assert_eq "$(image_repo "ghcr.io/ggml-org/llama.cpp@$DIGEST")" "ghcr.io/ggml-org/llama.cpp"
  assert_eq "$(image_repo caddy:2.11.9-alpine)" "caddy"
  assert_eq "$(image_repo docker.io/library/caddy:2)" "caddy"
  assert_eq "$(image_repo docker.io/searxng/searxng:2026.11.23-a7163d091)" "searxng/searxng"
  assert_eq "$(image_repo localhost:5000/team/img)" "localhost:5000/team/img" "a registry port is not a tag"
  assert_eq "$(image_repo localhost:5000/team/img:v1)" "localhost:5000/team/img"
}

test_stack_llama_build_is_recorded() {
  installed
  printf 'ghcr.io/other/thing@sha256:9999\nghcr.io/ggml-org/llama.cpp@%s\n' "$DIGEST" >"$FAKE_DIR/repodigests"
  assert_eq "${PLAN[LLAMA_IMAGE]}" "ghcr.io/ggml-org/llama.cpp:server"
  expect_ok pin_llama_image
  pin_llama_image >/dev/null
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
  assert_eq "${PLAN[LLAMA_IMAGE]}" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
  # Already exact: nothing is asked of Docker again.
  : >"$FAKE_LOG"
  pin_llama_image
  assert_not_called "docker image inspect"
}

test_stack_llama_build_unknown_keeps_the_tag() {
  installed
  : >"$FAKE_DIR/repodigests"
  expect_ok pin_llama_image
  assert_contains "$OUT" "keeps the moving tag"
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp:server"
}

test_stack_start_order_and_result() {
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  : >"$APP/.stopped"
  : >"$FAKE_LOG"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  assert_eq "$(compose_calls | grep -v -e '^ps ' -e '^logs ')" "config -q
pull --ignore-buildable
build agent sandbox
up -d llm
up -d --remove-orphans" "docker compose calls, in order"
  assert_eq "$STACK_STARTED" 1
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST" "the pulled build is recorded"
  assert_no_file "$APP/.stopped"
  assert_contains "$(cat "$T/out")" "All services are running"
  # compose was pointed at the right project
  assert_called "docker compose --project-directory $APP -f $APP/docker-compose.yml --env-file $APP/.env up -d llm"
}

test_stack_is_not_started_without_the_model_file() {
  installed --skip-model-download
  : >"$FAKE_LOG"
  expect_ok start_stack
  assert_contains "$OUT" "The model file is not there yet"
  assert_contains "$OUT" "myai start"
  assert_not_called "up -d"
  expect_ok fetch_model
  assert_contains "$OUT" "Skipped (--skip-model-download)"
}

test_stack_start_stops_clearly_when_the_model_server_crashes() {
  installed
  with_model_file
  echo "exited none 0" >"$FAKE_DIR/state_llm"
  : >"$FAKE_LOG"
  expect_fail start_stack
  assert_contains "$OUT" "The model server stopped or keeps restarting"
  assert_contains "$OUT" "myai logs llm"
  assert_contains "$OUT" "--model fast"
  assert_not_called "up -d --remove-orphans"
}

test_stack_start_stops_when_images_cannot_be_pulled() {
  installed
  with_model_file
  echo 1 >"$FAKE_DIR/rc_pull"
  expect_fail start_stack
  assert_contains "$OUT" "The container images could not be downloaded"
  assert_contains "$OUT" "Run the same command again"
  assert_not_called "up -d"
}

test_stack_update_goes_back_to_the_old_model_server_build() {
  local old="ghcr.io/ggml-org/llama.cpp@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  installed
  with_model_file
  env_set LLAMA_IMAGE "$old"
  # A second run with --update: the tag is tried again, the old build is remembered.
  (
    load_installer --update
    check_cpu >/dev/null
    select_model
    collect_answers
    write_config >/dev/null 2>&1
    refresh_images >/dev/null
    assert_eq "$OLD_LLAMA_IMAGE" "$old"
    assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp:server"
    echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
    # The new build crashes; after going back the server is healthy.
    printf 'exited none 0\nrunning healthy 0\n' >"$FAKE_DIR/state_llm"
    start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
    assert_contains "$(cat "$T/out")" "Going back to the build that worked"
    assert_eq "$(env_get LLAMA_IMAGE)" "$old" "LLAMA_IMAGE after the failed update"
    assert_eq "$STACK_STARTED" 1
  )
}

test_stack_update_follows_the_image_versions_of_the_new_files() {
  installed
  env_set WEBUI_IMAGE "ghcr.io/open-webui/open-webui:v0.0.1"
  (
    load_installer --update
    check_cpu >/dev/null
    select_model
    collect_answers
    write_config >/dev/null 2>&1
    [ -f "$SRC_DIR/.env.example" ] || skip ".env.example is not there yet"
    refresh_images >"$T/out"
    assert_eq "$(env_get WEBUI_IMAGE)" "$(env_get WEBUI_IMAGE "$SRC_DIR/.env.example")"
    assert_contains "$(cat "$T/out")" "WEBUI_IMAGE: ghcr.io/open-webui/open-webui:v0.0.1 ->"
  )
  # Without --update the owner's value stays.
  env_set WEBUI_IMAGE "ghcr.io/open-webui/open-webui:v0.0.1"
  (
    load_installer
    check_cpu >/dev/null
    select_model
    collect_answers
    write_config >/dev/null 2>&1
    refresh_images
    assert_eq "$(env_get WEBUI_IMAGE)" "ghcr.io/open-webui/open-webui:v0.0.1"
  )
}

# -------------------------------------------------------------- self-test ---

test_selftest_passes_and_reports_speed() {
  installed
  with_model_file
  STACK_STARTED=1
  echo "${PLAN[DATA_DIR]}/workspace/myai-selftest.txt" >"$FAKE_DIR/task_proof"
  self_test >"$T/out" 2>&1
  assert_eq "$SELFTEST_FAILED" 0
  assert_contains "$(cat "$T/out")" "Passed: 40 tokens in 7.1 seconds"
  assert_contains "$(cat "$T/out")" "Passed: the agent used a tool and wrote a file"
  assert_eq "$SPEED_TEXT" "writes about 6.20 tokens per second, reads about 61.00 tokens per second"
  assert_no_file "${PLAN[DATA_DIR]}/workspace/myai-selftest.txt"
  assert_called "exec -T agent python -c"
}

test_selftest_agent_that_used_no_tool_fails_without_undoing_anything() {
  installed
  with_model_file
  STACK_STARTED=1
  # The fake agent answers "ok" but writes no file.
  self_test >"$T/out" 2>&1 || fail "a failed self-test must not stop the installer"
  assert_eq "$SELFTEST_FAILED" 1
  assert_contains "$(cat "$T/out")" "did not use the tool"
  assert_contains "$(cat "$T/out")" "The installation is complete and stays as it is"
  assert_file "$APP/.env"
}

test_selftest_chat_error_is_reported() {
  installed
  with_model_file
  STACK_STARTED=1
  echo "error HTTP 502 upstream not ready" >"$FAKE_DIR/chat_reply"
  echo "error the task did not finish in 900 seconds" >"$FAKE_DIR/task_reply"
  self_test >"$T/out" 2>&1 || fail "a failed self-test must not stop the installer"
  assert_eq "$SELFTEST_FAILED" 1
  assert_contains "$(cat "$T/out")" "The chat test failed: HTTP 502 upstream not ready"
  assert_contains "$(cat "$T/out")" "The agent test failed: the task did not finish in 900 seconds"
  assert_eq "$SPEED_TEXT" ""
}

test_selftest_skipped_when_nothing_runs() {
  installed
  STACK_STARTED=0
  expect_ok self_test
  assert_contains "$OUT" "Skipped: the services are not running"
  assert_not_called "exec -T agent"
}

# ------------------------------------------------- dirs, command, timers ---

test_dirs_are_created_with_the_right_owners() {
  installed
  : >"$FAKE_LOG"
  expect_ok make_dirs
  local d
  for d in models webui agent workspace caddy/data caddy/config; do
    [ -d "$APP/data/$d" ] || fail "missing directory data/$d"
  done
  [ -d "$APP/backups" ] || fail "missing directory backups"
  assert_mode "$APP/data/agent" 750
  assert_mode "$APP/data/workspace" 750
  assert_mode "$APP/data/models" 755
  assert_mode "$APP/backups" 700
  assert_called "install -d -m 750 -o 10001 -g 10001 $APP/data/agent $APP/data/workspace"
  assert_called "install -d -m 755 -o root -g root $APP/data $APP/data/models"
  if [ "$("$REAL_ID" -u)" -eq 0 ]; then
    assert_eq "$(stat -c %u:%g "$APP/data/workspace")" "10001:10001" "owner of the workspace"
    assert_eq "$(stat -c %u:%g "$APP/data/agent")" "10001:10001" "owner of the agent data"
    assert_eq "$(stat -c %u "$APP/data/models")" 0 "owner of the models"
  fi
}

test_command_link_and_timers() {
  installed
  : >"$FAKE_LOG"
  expect_ok install_command_and_timers
  local units="$MYAI_SYSROOT/etc/systemd/system"
  assert_eq "$(readlink "$MYAI_SYSROOT/usr/local/bin/myai")" "$APP/bin/myai"
  assert_contains "$(cat "$units/myai-backup.timer")" "OnCalendar=*-*-* 03:15:00"
  assert_contains "$(cat "$units/myai-backup.timer")" "Persistent=true"
  assert_contains "$(cat "$units/myai-backup.service")" "ExecStart=/usr/local/bin/myai backup"
  assert_contains "$(cat "$units/myai-heal.timer")" "OnUnitActiveSec=5min"
  assert_contains "$(cat "$units/myai-heal.service")" "ExecStart=/usr/local/bin/myai heal"
  assert_called "systemctl daemon-reload"
  assert_called "systemctl enable --now myai-backup.timer myai-heal.timer"
  # Again: the unit files are the same, so systemd is not reloaded.
  : >"$FAKE_LOG"
  expect_ok install_command_and_timers
  assert_not_called "systemctl daemon-reload"
  assert_called "systemctl enable --now myai-backup.timer myai-heal.timer"
}

test_timers_without_systemd_only_warn() {
  installed
  rmdir "$MYAI_SYSROOT/run/systemd/system"
  expect_ok install_command_and_timers
  assert_contains "$OUT" "systemd is not running here"
  assert_not_called "systemctl"
}

test_summary_shows_and_saves_the_credentials() {
  installed
  STACK_STARTED=1
  SPEED_TEXT="writes about 6.20 tokens per second"
  final_summary >"$T/out" 2>&1
  local out file="$MYAI_SYSROOT/root/myai-credentials.txt" link
  out=$(cat "$T/out")
  link="https://srv123456.hstgr.cloud/enter/$(env_get GATE_TOKEN)"
  assert_file "$file"
  assert_mode "$file" 600
  assert_contains "$(cat "$file")" "$link"
  assert_contains "$(cat "$file")" "Password:  $(env_get WEBUI_ADMIN_PASSWORD)"
  assert_contains "$(cat "$file")" "/pair $(env_get PAIRING_CODE)"
  assert_contains "$(cat "$file")" "writes about 6.20 tokens per second"
  assert_contains "$out" "Your private AI server is ready"
  assert_contains "$out" "$link"
  assert_contains "$out" "[QR code for $link]"
  assert_contains "$out" "E-mail:    owner@example.com"
  assert_contains "$out" "https://srv123456.hstgr.cloud/agent/"
  assert_contains "$out" "myai status"
  assert_contains "$out" "myai doctor"
  assert_contains "$out" "myai bench"
  assert_contains "$out" "/root/myai-credentials.txt"
}

test_summary_mentions_what_was_not_done() {
  installed --tls-internal
  STACK_STARTED=0
  HARDEN_FAILED=("the firewall")
  SELFTEST_FAILED=1
  final_summary >"$T/out" 2>&1
  assert_contains "$(cat "$T/out")" "Installed, but not started yet"
  assert_contains "$(cat "$T/out")" "Not set up (see the warnings above): the firewall"
  assert_contains "$(cat "$T/out")" "self-signed certificate"
  assert_contains "$(cat "$T/out")" "Start with: myai doctor"
}
