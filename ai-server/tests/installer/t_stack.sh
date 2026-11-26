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

test_app_files_are_readable_by_the_services_whatever_the_umask() {
  load_installer
  # A private clone (umask 077), or the 700 folder a download is unpacked in.
  (
    umask 077
    make_source_tree "$T/src"
    mkdir -p "$T/src/config/searxng"
    echo "use_default_settings: true" >"$T/src/config/searxng/settings.yml"
    chmod 700 "$T/src/bin/myai"
  )
  assert_mode "$T/src" 700
  SRC_DIR="$T/src"
  expect_ok install_app_files
  assert_mode "$APP" 755
  # SearXNG reads its settings as uid 977; the agent and sandbox images run as 10001.
  assert_mode "$APP/config/searxng/settings.yml" 644
  assert_mode "$APP/agent/app" 755
  assert_mode "$APP/agent/app/new.py" 644
  assert_mode "$APP/bin/myai" 755
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

test_bootstrap_curl_pipe_bash_runs_the_downloaded_installer() {
  local out
  make_github_tarball ccr-f9008ee5-63plj0
  echo 380000000 >"$FAKE_DIR/df_avail_kb"
  # As in the README: the script arrives on stdin, so there is no checkout
  # next to it and it downloads the app files, then hands over to them.
  out=$(bash -s -- --dry-run --yes --dir "$APP" <"$AI_DIR/install.sh" 2>&1) || fail "the piped installer failed: $out"
  assert_contains "$out" "Downloading the app files"
  assert_contains "$out" "would copy the app files from $TMPDIR/myai-stage."
  assert_contains "$out" "Dry run finished. Nothing was changed."
  assert_called "https://codeload.github.com/luccadlfn-spec/luccadlfn-spec.github.io/tar.gz/ccr-f9008ee5-63plj0"
  assert_eq "$(find "$TMPDIR" -maxdepth 1 -name 'myai-stage.*' | wc -l)" 0 "download folders left behind"
}

test_update_fetches_the_new_files_and_keeps_the_settings() {
  local out
  (installed >/dev/null 2>&1)
  cp -p "$APP/.env" "$T/env.before"
  make_github_tarball ccr-f9008ee5-63plj0
  echo 380000000 >"$FAKE_DIR/df_avail_kb"
  # What "myai update" runs, from the installed copy.
  out=$(bash "$APP/install.sh" --update --dry-run --yes --dir "$APP" 2>&1) || fail "the update failed: $out"
  assert_contains "$out" "Downloading the app files"
  assert_contains "$out" "Skipped during an update"
  assert_contains "$out" "Kept $APP/.env as it is."
  assert_contains "$out" "Dry run finished. Nothing was changed."
  assert_called "tar.gz/ccr-f9008ee5-63plj0"
  cmp -s "$T/env.before" "$APP/.env" || fail "a dry-run update changed the settings"
  assert_eq "$(find "$TMPDIR" -maxdepth 1 -name 'myai-stage.*' | wc -l)" 0 "download folders left behind"
}

test_bootstrap_refuses_odd_repo_names() {
  load_installer
  mkdir -p "$T/stage"
  export MYAI_REPO='someone/fork; rm -rf /'
  expect_fail fetch_app_files "$T/stage"
  assert_contains "$OUT" "MYAI_REPO must look like owner/name"
  assert_not_called "curl"
}

# ------------------------------------------------------- one at a time ---

# An installer shell just before step 5, with build files of a first run.
before_configure() {
  load_installer
  check_cpu >/dev/null
  select_model
  collect_answers
  mkdir -p "$APP/agent"
  echo "build file in use" >"$APP/agent/marker"
}

test_second_installer_stops_before_touching_the_app_files() {
  before_configure
  hold_lock 3 "the installer"
  expect_fail configure
  assert_contains "$OUT" "The installer is still running (process "
  assert_contains "$OUT" "tail -f /root/myai-install.log"
  assert_not_contains "$OUT" "App files copied" "the output"
  assert_eq "$(cat "$APP/agent/marker")" "build file in use" "the first installer's build files"
  assert_no_file "$APP/.env"
  wait
}

test_installer_waits_for_a_myai_command_before_copying() {
  local tries=0
  before_configure
  # A backup holds the lock. Just before it lets go, it looks whether the
  # app files were replaced while it worked.
  (
    exec 9>>"$APP/.lock"
    flock 9
    printf '%s 03:15 myai backup\n' "$BASHPID" >"$APP/.lock"
    : >"$T/holding"
    sleep 2
    if [ -f "$APP/agent/marker" ]; then echo untouched >"$T/seen"; else echo replaced >"$T/seen"; fi
  ) &
  while [ ! -e "$T/holding" ]; do
    tries=$((tries + 1))
    [ "$tries" -lt 200 ] || fail "the backup did not start"
    sleep 0.05
  done
  expect_ok configure
  wait
  assert_eq "$(cat "$T/seen")" untouched "the app files while the backup held the lock"
  assert_contains "$OUT" "Waiting for myai backup (process "
  assert_contains "$OUT" "App files copied"
  assert_no_file "$APP/agent/marker"
  assert_contains "$(cat "$APP/.lock")" " the installer"
}

# --------------------------------------------------------- when it stops ---

test_ctrl_c_says_where_it_stopped_and_how_to_go_on() {
  local rc=0
  echo 380000000 >"$FAKE_DIR/df_avail_kb"
  # "sshd -T" is slow this time, so Ctrl+C arrives while step 4 waits for it.
  printf '#!/usr/bin/env bash\nexec sleep 30\n' >"$T/bin/sshd"
  python3 - "$AI_DIR/install.sh" "$APP" "$T/out" <<'PY' || rc=$?
import os, signal, subprocess, sys, time
script, app, out = sys.argv[1:4]
with open(out, "wb") as sink:
    p = subprocess.Popen(["bash", script, "--dry-run", "--yes", "--dir", app], stdout=sink,
                         stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 30
    while time.time() < deadline and b"[4/11]" not in open(out, "rb").read():
        time.sleep(0.1)
    time.sleep(0.5)
    os.killpg(p.pid, signal.SIGINT)  # what Ctrl+C in a terminal does
    sys.exit(p.wait(timeout=30))
PY
  assert_eq "$rc" 130 "exit code after Ctrl+C"
  assert_contains "$(cat "$T/out")" "Stopped as asked, in step [4/11] Protecting the server"
  assert_contains "$(cat "$T/out")" "To go on, run the same command again"
  assert_not_contains "$(cat "$T/out")" "something failed" "the output"
}

test_unforeseen_failure_names_the_step_and_the_command() {
  local out rc=0
  # A shell of its own: inside "if" or "||" bash would switch off both
  # set -e and the ERR trap, for subshells too.
  cat >"$T/fails.sh" <<'SCRIPT'
export MYAI_SOURCE_ONLY=1
. "$AI_DIR/bin/myai-lib.sh"
. "$AI_DIR/install.sh"
trap on_exit EXIT
trap 'FAILED_AT="$BASH_COMMAND"' ERR
set -eE
STEP_NO=6
STEP_NAME="Data folders"
cat /no/such/file 2>/dev/null
echo "not reached"
SCRIPT
  out=$(bash "$T/fails.sh" 2>&1) || rc=$?
  assert_eq "$rc" 1 "exit code"
  assert_not_contains "$out" "not reached" "the output"
  assert_contains "$out" "Stopped: something failed in step [6/11] Data folders."
  assert_contains "$out" "The command that failed: cat /no/such/file"
  assert_contains "$out" "After fixing the problem, run the same command again"
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

TAG="ghcr.io/ggml-org/llama.cpp:server"
OLD_BUILD="ghcr.io/ggml-org/llama.cpp@sha256:0000000000000000000000000000000000000000000000000000000000000000"

test_stack_llama_build_is_recorded_once_the_model_has_loaded() {
  installed
  with_model_file
  printf 'ghcr.io/other/thing@sha256:9999\nghcr.io/ggml-org/llama.cpp@%s\n' "$DIGEST" >"$FAKE_DIR/repodigests"
  assert_eq "${PLAN[LLAMA_IMAGE]}" "$TAG"
  : >"$FAKE_LOG"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  # The exact build was tried while the settings still named the tag ...
  assert_called "compose up: LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp@$DIGEST given for this call; the settings file says LLAMA_IMAGE=$TAG"
  # ... and recorded once the model had loaded.
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
  assert_eq "${PLAN[LLAMA_IMAGE]}" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
  assert_contains "$(cat "$T/out")" "Model server build recorded: sha256:1111"
  assert_eq "$REPLACED_LLAMA" "" "no build was replaced on a first installation"
  # Already exact: nothing is fetched or tried again.
  : >"$FAKE_LOG"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  assert_not_called "docker pull"
  assert_not_called "docker image inspect"
  assert_not_called "given for this call"
}

test_stack_llama_build_unknown_keeps_the_tag() {
  installed
  with_model_file
  : >"$FAKE_DIR/repodigests"
  expect_ok start_stack
  assert_contains "$OUT" "keeps the moving tag"
  assert_eq "$(env_get LLAMA_IMAGE)" "$TAG"
  assert_not_called "given for this call"
}

test_stack_first_build_that_fails_is_not_recorded() {
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  echo "exited none 0" >"$FAKE_DIR/state_llm"
  expect_fail start_stack
  # The tag stays, so the next run fetches the newest build again.
  assert_eq "$(env_get LLAMA_IMAGE)" "$TAG"
  assert_not_contains "$OUT" "Going back" "the output"
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
  assert_contains "$OUT" "If the messages say that the model file cannot be read"
  assert_not_called "up -d --remove-orphans"
}

test_stack_start_stops_when_compose_cannot_start_the_model_server() {
  installed
  with_model_file
  echo 1 >"$FAKE_DIR/rc_up"
  expect_fail start_stack
  assert_contains "$OUT" "The model server could not be started."
  assert_not_contains "$OUT" "model file" "the advice"
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

# ---------------------------------------------------------------- update ---

# A server installed with an exact model server build, about to be updated.
pinned_install() {
  installed
  with_model_file
  env_set LLAMA_IMAGE "$OLD_BUILD"
  echo "${PLAN[DATA_DIR]}/workspace/myai-selftest.txt" >"$FAKE_DIR/task_proof"
}

# One "myai update" run, from the settings to the end of the tests.
update_run() {
  load_installer --update "$@"
  check_cpu >/dev/null
  select_model
  collect_answers
  write_config >/dev/null 2>&1
  refresh_images
  start_stack
  self_test
  remove_replaced_images
}

test_stack_update_keeps_the_build_that_worked_in_the_settings() {
  pinned_install
  (
    load_installer --update
    check_cpu >/dev/null
    select_model
    collect_answers
    write_config >/dev/null 2>&1
    refresh_images >/dev/null
    assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD" "LLAMA_IMAGE before anything was downloaded"
    assert_eq "$LLAMA_UPDATE_TAG" "$TAG" "the tag whose newer build is tried"
  )
}

test_stack_update_tries_the_newer_build_and_records_it() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_called "docker pull $TAG"
  # While the newer build loaded the model, the settings named the old one.
  assert_called "compose up: LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp@$DIGEST given for this call; the settings file says LLAMA_IMAGE=$OLD_BUILD"
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
  assert_contains "$(cat "$T/out")" "All services are running"
}

test_stack_update_goes_back_to_the_old_model_server_build() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # The new build crashes; after going back the server is healthy.
  printf 'exited none 0\nrunning healthy 0\n' >"$FAKE_DIR/state_llm"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "Going back to the build that worked"
  assert_contains "$(cat "$T/out")" "with the model server build from before"
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD" "LLAMA_IMAGE after the failed try"
  assert_contains "$(cat "$T/out")" "All services are running"
  assert_not_called "docker image rm $OLD_BUILD"
}

test_stack_update_stopped_early_can_still_go_back() {
  # The first try stops while the images download ("run the same command
  # again"); the second one finds a newer build that does not work.
  pinned_install
  echo 1 >"$FAKE_DIR/rc_pull"
  if (update_run) >"$T/out" 2>&1; then fail "the first try should stop"; fi
  assert_contains "$(cat "$T/out")" "The container images could not be downloaded"
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD" "LLAMA_IMAGE after the first try"
  rm "$FAKE_DIR/rc_pull"
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  printf 'exited none 0\nrunning healthy 0\n' >"$FAKE_DIR/state_llm"
  (update_run) >"$T/out" 2>&1 || fail "the second try failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "Going back to the build that worked"
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD" "LLAMA_IMAGE after the second try"
  assert_not_contains "$(cat "$T/out")" "rm $APP/data/models" "the advice"
}

test_stack_update_when_neither_build_loads_the_model() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  echo "exited none 0" >"$FAKE_DIR/state_llm"
  if (update_run) >"$T/out" 2>&1; then fail "the update should stop"; fi
  assert_contains "$(cat "$T/out")" "Going back to the build that worked"
  assert_contains "$(cat "$T/out")" "so the newer build is not the cause"
  assert_contains "$(cat "$T/out")" "too little free memory"
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD"
}

test_stack_update_without_the_newer_build_stays_on_the_current_one() {
  pinned_install
  echo 1 >"$FAKE_DIR/rc_docker_pull"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "could not be downloaded. The current one stays."
  assert_not_called "given for this call"
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD"
  assert_contains "$(cat "$T/out")" "All services are running"
}

test_stack_update_removes_the_image_versions_it_replaced() {
  local new_webui
  pinned_install
  env_set WEBUI_IMAGE "ghcr.io/open-webui/open-webui:v0.0.1"
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  new_webui=$(env_get WEBUI_IMAGE)
  assert_called "docker image rm ghcr.io/open-webui/open-webui:v0.0.1"
  assert_called "docker image rm $OLD_BUILD"
  # Old builds of the agent and the sandbox: this project's images only, never "-a".
  assert_called "docker image prune -f --filter label=com.docker.compose.project=myai"
  assert_not_called "prune -a"
  assert_not_called "--all"
  assert_contains "$(cat "$T/out")" "Removed container images that are no longer used"
  # What runs now stays, and the old build went only after the tests passed.
  assert_not_called "docker image rm $new_webui"
  assert_not_called "docker image rm ghcr.io/ggml-org/llama.cpp@$DIGEST"
  assert_call_order "exec -T agent python" "docker image rm $OLD_BUILD"
  assert_no_file "$APP/.replaced-images"
}

test_stack_update_stopped_early_still_removes_the_old_versions_later() {
  pinned_install
  env_set WEBUI_IMAGE "ghcr.io/open-webui/open-webui:v0.0.1"
  # The first try stops while the images download, after the settings already
  # name the new version; the old one is written down.
  echo 1 >"$FAKE_DIR/rc_pull"
  if (update_run) >"$T/out" 2>&1; then fail "the first try should stop"; fi
  assert_contains "$(cat "$APP/.replaced-images")" "ghcr.io/open-webui/open-webui:v0.0.1"
  # The next run that works removes it, though it did not replace it itself.
  rm "$FAKE_DIR/rc_pull"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the second try failed: $(cat "$T/out")"
  assert_called "docker image rm ghcr.io/open-webui/open-webui:v0.0.1"
  assert_no_file "$APP/.replaced-images"
}

test_stack_never_removes_an_image_the_settings_use() {
  pinned_install
  # Written down by an earlier update, but in use again now.
  env_get WEBUI_IMAGE >"$APP/.replaced-images"
  echo "caddy:2.0.0-alpine" >>"$APP/.replaced-images"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_not_called "docker image rm $(env_get WEBUI_IMAGE)"
  assert_called "docker image rm caddy:2.0.0-alpine"
}

test_stack_update_keeps_the_old_model_build_when_a_test_failed() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  echo "error HTTP 502 upstream not ready" >"$FAKE_DIR/chat_reply"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "The chat test failed"
  assert_not_called "docker image rm $OLD_BUILD"
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST" "the new build loaded the model"
}

test_stack_run_without_update_removes_no_image() {
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  : >"$FAKE_LOG"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  remove_replaced_images >>"$T/out" 2>&1
  assert_not_called "docker image rm"
  assert_not_contains "$(cat "$T/out")" "Removed the container images" "the output"
}

test_stack_update_dry_run_only_lists_what_it_would_remove() {
  local out
  pinned_install
  env_set WEBUI_IMAGE "ghcr.io/open-webui/open-webui:v0.0.1"
  cp -p "$APP/.env" "$T/env.before"
  : >"$FAKE_LOG"
  out=$( (update_run --dry-run) 2>&1) || fail "the dry run failed: $out"
  assert_contains "$out" "would download $TAG, to try its newest build"
  assert_contains "$out" "would remove the replaced image ghcr.io/open-webui/open-webui:v0.0.1"
  assert_not_called "docker image rm"
  assert_not_called "docker pull"
  cmp -s "$T/env.before" "$APP/.env" || fail "a dry run changed the settings"
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
  assert_contains "$(cat "$T/out")" "Passed: the agent used a tool and wrote a file in 20.0 seconds"
  assert_eq "$SPEED_TEXT" "writes about 6.20 tokens per second, reads about 61.00 tokens per second"
  assert_no_file "${PLAN[DATA_DIR]}/workspace/myai-selftest.txt"
  assert_called "exec -T agent python -c"
}

test_selftest_agent_that_used_no_tool_fails_without_undoing_anything() {
  installed
  with_model_file
  STACK_STARTED=1
  # The task finished, but no tool ran and no file was written.
  echo "ok 0 12.0 -" >"$FAKE_DIR/task_reply"
  self_test >"$T/out" 2>&1 || fail "a failed self-test must not stop the installer"
  assert_eq "$SELFTEST_FAILED" 1
  assert_contains "$(cat "$T/out")" "without using the tool it was asked to use"
  assert_contains "$(cat "$T/out")" "The installation is complete and stays as it is"
  assert_file "$APP/.env"
}

test_selftest_tool_that_wrote_no_file_is_named() {
  installed
  with_model_file
  STACK_STARTED=1
  # A tool ran (the fake agent says so) but the file is not in the workspace.
  echo "ok 2 30.5 list_files,read_file" >"$FAKE_DIR/task_reply"
  self_test >"$T/out" 2>&1 || fail "a failed self-test must not stop the installer"
  assert_eq "$SELFTEST_FAILED" 1
  assert_contains "$(cat "$T/out")" "The agent used a tool (list_files,read_file), but the test file did not appear in ${PLAN[DATA_DIR]}/workspace"
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
  # A hanging command must not hold the lock for ever; the repair ends well
  # before the 15 minutes that other commands wait for it.
  assert_contains "$(cat "$units/myai-heal.service")" "TimeoutStartSec=10min"
  assert_contains "$(cat "$units/myai-backup.service")" "TimeoutStartSec=2h"
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

test_summary_with_a_certificate_contact_is_not_called_self_signed() {
  installed --admin-email owner@myai-test-domain.net
  assert_eq "$(env_get CADDY_TLS_LINE)" "tls owner@myai-test-domain.net"
  STACK_STARTED=1
  final_summary >"$T/out" 2>&1
  assert_not_contains "$(cat "$T/out")" "self-signed" "the summary"
  assert_contains "$(cat "$T/out")" "The certificate is fetched in the first minute"
  assert_not_contains "$(cat "$MYAI_SYSROOT/root/myai-credentials.txt")" "self-signed" "the credentials file"
}

test_summary_mentions_what_was_not_done() {
  installed --tls-internal
  STACK_STARTED=0
  # shellcheck disable=SC2034  # read by final_summary
  HARDEN_FAILED=("the firewall")
  SELFTEST_FAILED=1
  final_summary >"$T/out" 2>&1
  assert_contains "$(cat "$T/out")" "Installed, but not started yet"
  assert_contains "$(cat "$T/out")" "Not set up (see the warnings above): the firewall"
  assert_contains "$(cat "$T/out")" "self-signed certificate"
  assert_contains "$(cat "$T/out")" "Start with: myai doctor"
}
