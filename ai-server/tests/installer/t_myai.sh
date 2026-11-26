#!/usr/bin/env bash
# The myai command: backup, restore, bench, models, and the small commands.
# Docker is a fake that logs what it is asked to do.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and bin/myai

compose_log() { grep '^docker compose' "$FAKE_LOG" | sed 's/.*--env-file [^ ]* //'; }
backup_files() { find "$APP/backups" -maxdepth 1 -name 'myai-*.tar.gz' | sort; }

# An installed app and the myai functions, with no terminal to ask on.
myai_ready() {
  fake_install
  load_myai
  have_tty() { return 1; }
}

# ------------------------------------------------------------------ backup ---

test_backup_holds_settings_and_data_but_no_models() {
  local file list
  myai_ready
  ln -s ./notes.txt "$APP/data/workspace/link"
  expect_ok cmd_backup
  assert_eq "$(backup_files | wc -l)" 1 "number of archives"
  file=$(backup_files)
  assert_mode "$file" 600
  assert_mode "$APP/backups" 700
  list=$("$REAL_TAR" -tzf "$file")
  assert_contains "$list" ".env"
  assert_contains "$list" "data/webui/webui.db"
  assert_contains "$list" "data/agent/agent.db"
  assert_contains "$list" "data/workspace/notes.txt"
  assert_not_contains "$list" "models" "the archive listing"
  assert_not_contains "$list" ".gguf" "the archive listing"
  # A link inside the workspace still points where it pointed.
  mkdir "$T/unpacked"
  "$REAL_TAR" -xzf "$file" -C "$T/unpacked"
  assert_eq "$(readlink "$T/unpacked/data/workspace/link")" "./notes.txt" "target of a symbolic link"
  assert_eq "$(cat "$T/unpacked/.env")" "$(cat "$APP/.env")"
  # The chat and the agent were stopped for the copy and started again.
  assert_call_order "stop webui agent" "up -d --no-deps webui agent"
  assert_eq "$(find "$APP/backups" -name '*.tmp' | wc -l)" 0 "leftover temporary files"
  assert_contains "$OUT" "Backup written: $file"
}

test_backup_trap_restarts_services_after_a_failed_archive() {
  myai_ready
  : >"$FAKE_DIR/tar_fail" # tar fails as on a full disk
  expect_fail cmd_backup
  assert_contains "$OUT" "The backup could not be written"
  assert_contains "$OUT" "df -h"
  assert_called "stop webui agent"
  assert_called "up -d --no-deps webui agent"
  assert_call_order "stop webui agent" "up -d --no-deps webui agent"
  assert_eq "$(backup_files | wc -l)" 0 "no archive is left behind"
  assert_eq "$(find "$APP/backups" -name '*.tmp' | wc -l)" 0 "no half-written file is left behind"
}

test_backup_trap_restarts_services_when_the_command_dies() {
  myai_ready
  # Something goes wrong in the middle that nobody planned for.
  backup_archive() {
    printf 'half' >"$1"
    exit 3
  }
  expect_fail cmd_backup
  assert_call_order "stop webui agent" "up -d --no-deps webui agent"
  assert_eq "$(find "$APP/backups" -type f | wc -l)" 0 "files left in backups/"
}

test_backup_restarts_only_what_was_running() {
  myai_ready
  echo "exited none 0" >"$FAKE_DIR/state_webui"
  expect_ok cmd_backup
  assert_called "stop agent"
  assert_called "up -d --no-deps agent"
  assert_not_called "webui agent"
  assert_not_called "up -d --no-deps webui"
}

test_backup_of_a_stopped_server_starts_nothing() {
  myai_ready
  echo "exited none 0" >"$FAKE_DIR/state_webui"
  echo "exited none 0" >"$FAKE_DIR/state_agent"
  expect_ok cmd_backup
  assert_eq "$(backup_files | wc -l)" 1
  assert_eq "$(compose_log | grep -c -e '^stop' -e '^up' || true)" 0 "stop or up calls"
}

test_backup_keeps_the_newest_seven() {
  local i
  myai_ready
  for i in 1 2 3 4 5 6 7 8 9; do
    echo old >"$APP/backups/myai-2026010$i-031500.tar.gz"
  done
  echo "not a backup of ours" >"$APP/backups/notes.txt"
  expect_ok cmd_backup
  assert_eq "$(backup_files | wc -l)" 7 "number of archives kept"
  assert_no_file "$APP/backups/myai-20260101-031500.tar.gz"
  assert_no_file "$APP/backups/myai-20260103-031500.tar.gz"
  assert_file "$APP/backups/myai-20260104-031500.tar.gz"
  assert_file "$APP/backups/myai-20260109-031500.tar.gz"
  assert_file "$APP/backups/notes.txt"
  # The one just made is among them.
  [ "$(backup_files | tail -n 1)" != "$APP/backups/myai-20260109-031500.tar.gz" ] || fail "the new archive is missing"
}

test_backup_works_with_a_data_folder_elsewhere() {
  local file
  myai_ready
  mv "$APP/data" "$T/elsewhere"
  env_set DATA_DIR "$T/elsewhere"
  expect_ok cmd_backup
  file=$(backup_files)
  assert_contains "$("$REAL_TAR" -tzf "$file")" "data/webui/webui.db"
  assert_not_contains "$("$REAL_TAR" -tzf "$file")" "elsewhere"
}

# ----------------------------------------------------------------- restore ---

test_restore_brings_back_settings_and_data() {
  local archive model
  myai_ready
  model="$APP/data/models/$(env_get MODEL_FILE)"
  expect_ok cmd_backup
  archive=$(backup_files)
  # Later: things changed.
  echo "changed chats" >"$APP/data/webui/webui.db"
  rm "$APP/data/workspace/notes.txt"
  echo "newer file" >"$APP/data/workspace/newer.txt"
  env_set LLM_THREADS 2
  sleep 1 # a different second, so the safety backup gets its own name
  : >"$FAKE_LOG"
  ASSUME_YES=1
  expect_ok cmd_restore "$archive"
  assert_eq "$(cat "$APP/data/webui/webui.db")" "chat database"
  assert_eq "$(cat "$APP/data/workspace/notes.txt")" "a file"
  assert_no_file "$APP/data/workspace/newer.txt"
  assert_eq "$(env_get LLM_THREADS)" 6
  assert_eq "$(env_get DATA_DIR)" "$APP/data"
  assert_mode "$APP/.env" 600
  assert_file "$model"
  # Everything was stopped for it and started again; the state from before was saved first.
  assert_call_order "down --remove-orphans" "up -d --remove-orphans"
  assert_eq "$(backup_files | wc -l)" 2 "archives: the one restored and the safety copy"
  assert_contains "$("$REAL_TAR" -xzOf "$(backup_files | tail -n 1)" data/webui/webui.db)" "changed chats"
  assert_contains "$OUT" "Restored"
}

test_restore_accepts_a_bare_file_name() {
  local archive
  myai_ready
  expect_ok cmd_backup
  archive=$(basename "$(backup_files)")
  echo "changed" >"$APP/data/webui/webui.db"
  sleep 1
  ASSUME_YES=1
  expect_ok cmd_restore "$archive"
  assert_eq "$(cat "$APP/data/webui/webui.db")" "chat database"
}

test_restore_asks_first_and_changes_nothing_without_a_yes() {
  local archive
  myai_ready
  expect_ok cmd_backup
  archive=$(backup_files)
  echo "changed chats" >"$APP/data/webui/webui.db"
  : >"$FAKE_LOG"
  expect_fail cmd_restore "$archive"
  assert_contains "$OUT" "Nothing was changed"
  assert_contains "$OUT" "add --yes"
  assert_eq "$(cat "$APP/data/webui/webui.db")" "changed chats"
  assert_not_called "docker compose"
}

test_restore_refuses_archives_it_did_not_make() {
  myai_ready
  ASSUME_YES=1
  mkdir -p "$T/evil/etc" "$T/evil/data"
  echo x >"$T/evil/etc/passwd"
  echo x >"$T/evil/.env"
  "$REAL_TAR" -czf "$T/foreign.tar.gz" -C "$T/evil" .env etc/passwd
  expect_fail cmd_restore "$T/foreign.tar.gz"
  assert_contains "$OUT" "was not made by \"myai backup\""
  "$REAL_TAR" -czf "$T/noenv.tar.gz" -C "$T/evil" data
  expect_fail cmd_restore "$T/noenv.tar.gz"
  assert_contains "$OUT" "was not made by"
  echo "this is not an archive" >"$T/junk.tar.gz"
  expect_fail cmd_restore "$T/junk.tar.gz"
  assert_contains "$OUT" "cannot be read as a backup"
  expect_fail cmd_restore "$T/missing.tar.gz"
  assert_contains "$OUT" "There is no file"
  expect_fail cmd_restore
  assert_contains "$OUT" "Which backup?"
  assert_eq "$(cat "$APP/data/webui/webui.db")" "chat database" "nothing was touched"
  assert_not_called "down"
}

test_restore_without_the_model_file_does_not_start_the_services() {
  local archive
  myai_ready
  expect_ok cmd_backup
  archive=$(backup_files)
  rm "$APP/data/models/"*.gguf
  sleep 1
  : >"$FAKE_LOG"
  ASSUME_YES=1
  expect_ok cmd_restore "$archive"
  assert_contains "$OUT" "uses a model file that is not on this server"
  assert_not_called "up -d --remove-orphans"
}

# ------------------------------------------------------------------- bench ---

pick_from() { printf '%s\n' "$@" | bench_pick; }

test_bench_pick_smallest_within_ten_percent_of_the_best() {
  myai_ready
  assert_eq "$(pick_from "4 5.0" "5 5.6" "6 6.0" "7 6.1" "8 6.2")" 5 "5.6 is within 10% of 6.2, 5.0 is not"
  assert_eq "$(pick_from "4 6.0" "5 6.1" "6 6.1" "7 6.0" "8 5.9")" 4 "all about the same: the fewest threads"
  assert_eq "$(pick_from "4 3.0" "5 3.8" "6 4.6" "7 5.3" "8 6.2")" 8 "speed keeps rising: the most threads"
  assert_eq "$(pick_from "4 4.0" "5 5.0" "6 6.5" "7 6.4" "8 5.1")" 6 "a peak in the middle"
  assert_eq "$(pick_from "4 9.0" "5 10.0")" 4 "exactly 90% counts as within 10%"
  assert_eq "$(pick_from "4 8.99" "5 10.0")" 5 "just under 90% does not"
  assert_eq "$(pick_from "4 0" "5 0" "6 5.5" "7 0")" 6 "failed measurements are ignored"
  assert_eq "$(pick_from "8 6.2" "4 6.0" "6 6.1")" 4 "the order of the lines does not matter"
  assert_eq "$(pick_from "1 2.5")" 1 "a single measurement"
  if pick_from "4 0" "5 0" >/dev/null; then fail "no usable measurement must fail"; fi
  if printf '' | bench_pick >/dev/null; then fail "no measurement at all must fail"; fi
}

bench_speeds() { # speed for 4, 5, 6, 7 and 8 threads
  local n=4 speed
  for speed in "$@"; do
    echo "$speed" >"$FAKE_DIR/bench_$n"
    n=$((n + 1))
  done
}

test_bench_measures_each_thread_count_and_keeps_the_pick() {
  myai_ready
  bench_speeds 5.0 5.6 6.0 6.1 6.2
  ASSUME_YES=1
  expect_ok cmd_bench
  assert_eq "$(env_get LLM_THREADS)" 5 "the thread count that was kept"
  assert_eq "$(env_get LLM_THREADS_BATCH)" 8 "the batch threads are not touched"
  assert_contains "$OUT" "      4   5.0 tokens per second"
  assert_contains "$OUT" "      8   6.2 tokens per second"
  assert_contains "$OUT" "Kept: 5 threads (it was 6)"
  assert_contains "$OUT" "CPU credits"
  assert_contains "$OUT" "within 10%"
  # One restart of the model server for each of 4..8, and one for the result.
  assert_eq "$(compose_log | grep -c '^up -d --no-deps llm')" 6 "restarts of the model server"
}

test_bench_range_starts_lower_on_small_servers() {
  myai_ready
  fake_machine 2 8000000
  echo 2.0 >"$FAKE_DIR/bench_1"
  echo 3.5 >"$FAKE_DIR/bench_2"
  ASSUME_YES=1
  expect_ok cmd_bench
  assert_eq "$(env_get LLM_THREADS)" 2
  assert_contains "$OUT" "with 1 to 2 threads"
}

test_bench_skips_a_failed_measurement() {
  myai_ready
  bench_speeds 5.0 5.6 6.0 6.1 6.2
  # With 5 threads the gateway does not answer.
  eval "real_$(declare -f bench_measure)"
  bench_measure() {
    if [ "$(env_get LLM_THREADS)" = 5 ]; then return 1; fi
    real_bench_measure
  }
  ASSUME_YES=1
  expect_ok cmd_bench
  assert_contains "$OUT" "      5   failed"
  assert_eq "$(env_get LLM_THREADS)" 6 "5 failed, so 6 (6.0, within 10% of 6.2) is the pick"
}

test_bench_puts_the_old_value_back_when_nothing_works() {
  myai_ready
  echo "error HTTP 502 no model" >"$FAKE_DIR/chat_reply"
  ASSUME_YES=1
  expect_fail cmd_bench
  assert_contains "$OUT" "No measurement worked"
  assert_contains "$OUT" "Putting back 6 threads"
  assert_eq "$(env_get LLM_THREADS)" 6
  assert_eq "$(compose_log | tail -n 1)" "up -d --no-deps llm" "the model server is restarted with the old value"
}

test_bench_puts_the_old_value_back_when_interrupted() {
  myai_ready
  bench_measure() { exit 130; } # Ctrl+C in the middle of the first measurement
  ASSUME_YES=1
  expect_fail cmd_bench
  assert_eq "$(env_get LLM_THREADS)" 6
}

test_bench_needs_a_running_model_and_a_yes() {
  myai_ready
  bench_speeds 5.0 5.6 6.0 6.1 6.2
  expect_fail cmd_bench
  assert_contains "$OUT" "Nothing was changed"
  assert_contains "$OUT" "myai bench --yes"
  assert_eq "$(env_get LLM_THREADS)" 6
  ASSUME_YES=1
  echo "running starting 0" >"$FAKE_DIR/state_llm"
  expect_fail cmd_bench
  assert_contains "$OUT" "The model must be running"
  rm "$FAKE_DIR/state_llm"
  : >"$APP/.stopped"
  expect_fail cmd_bench
  assert_contains "$OUT" "The model must be running"
  assert_not_called "up -d"
}

# ------------------------------------------------------------------ models ---

# A sparse file with the catalogue's size counts as "downloaded".
have_model() { # ID
  local file bytes
  file="$APP/data/models/$(catalog_field "$1" 4)"
  bytes=$(catalog_field "$1" 5)
  printf 'GGUF' >"$file"
  python3 -c 'import os, sys; os.truncate(sys.argv[1], int(sys.argv[2]))' "$file" "$bytes"
}

test_model_list_marks_current_and_downloaded() {
  myai_ready
  have_model fast
  expect_ok cmd_model list
  assert_contains "$OUT" "* fast"
  assert_contains "$OUT" "  hauhau"
  [[ $OUT =~ \*\ fast\ +2\.7\ GB\ +needs\ +6\ GB\ memory\ +downloaded ]] || fail "the line for fast is wrong: $OUT"
  [[ $OUT =~ hauhau\ +21\.2\ GB\ +needs\ 30\ GB\ memory\ +not\ downloaded ]] || fail "the line for hauhau is wrong: $OUT"
  assert_contains "$OUT" "myai model use <id>"
}

test_model_use_switches_to_a_downloaded_model() {
  myai_ready
  have_model classic
  expect_ok cmd_model use classic
  assert_eq "$(env_get MODEL_ID)" classic
  assert_eq "$(env_get MODEL_FILE)" "Huihui-Qwen3-30B-A3B-Instruct-2507-abliterated.Q4_K_M.gguf"
  assert_called "up -d --no-deps llm"
  assert_not_called "curl"
  assert_contains "$OUT" "Now using the model 'classic'"
}

test_model_use_goes_back_when_the_new_model_does_not_load() {
  myai_ready
  have_model classic
  # The first check sees a healthy server; after the switch it has crashed.
  printf 'running healthy 0\nexited none 0\n' >"$FAKE_DIR/state_llm"
  expect_fail cmd_model use classic
  assert_contains "$OUT" "could not be loaded"
  assert_contains "$OUT" "too little memory"
  assert_eq "$(env_get MODEL_ID)" fast "the previous model is set again"
  assert_eq "$(env_get MODEL_FILE)" "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
  assert_eq "$(compose_log | grep -c '^up -d --no-deps llm')" 2 "the model server was started again with the old model"
}

test_model_use_on_a_stopped_server_only_changes_the_setting() {
  myai_ready
  have_model classic
  : >"$APP/.stopped"
  expect_ok cmd_model use classic
  assert_eq "$(env_get MODEL_ID)" classic
  assert_not_called "up -d"
  assert_contains "$OUT" "myai start"
}

test_model_use_downloads_first_when_needed() {
  myai_ready
  hf_site
  MODELS_CONF="$T/models.conf"
  echo "tiny|1|$REPO|$FILE|$SIZE|A tiny test model" >"$MODELS_CONF"
  expect_ok cmd_model use tiny
  cmp -s "$(served_file)" "$APP/data/models/$FILE" || fail "the model file was not downloaded"
  assert_eq "$(env_get MODEL_ID)" tiny
  assert_eq "$(env_get MODEL_FILE)" "$FILE"
  assert_contains "$OUT" "Checksum correct"
}

test_model_use_refuses_unknown_names_and_full_disks() {
  myai_ready
  expect_fail cmd_model use gpt9
  assert_contains "$OUT" "There is no model called 'gpt9'"
  expect_fail cmd_model use
  assert_contains "$OUT" "Which model?"
  expect_fail cmd_model frobnicate
  assert_contains "$OUT" "Unknown: myai model frobnicate"
  echo 1000000 >"$FAKE_DIR/df_avail_kb" # 1 GB free, hauhau needs 21 GB
  expect_fail cmd_model use hauhau
  assert_contains "$OUT" "Not enough free disk space"
  assert_eq "$(env_get MODEL_ID)" fast
  assert_not_called "resolve/main"
}

test_model_use_warns_when_memory_is_small() {
  myai_ready
  fake_machine 4 8000000
  have_model classic
  expect_fail cmd_model use classic
  assert_contains "$OUT" "meant for servers with at least 28 GB"
  assert_contains "$OUT" "Nothing was changed"
  assert_eq "$(env_get MODEL_ID)" fast
}

test_model_custom_downloads_and_switches() {
  myai_ready
  hf_site
  expect_ok cmd_model custom "$REPO" "$FILE"
  cmp -s "$(served_file)" "$APP/data/models/$FILE" || fail "the model file was not downloaded"
  assert_eq "$(env_get MODEL_ID)" custom
  assert_eq "$(env_get MODEL_FILE)" "$FILE"
  expect_ok cmd_model list
  assert_contains "$OUT" "* custom   $FILE"
}

test_model_custom_checks_the_names() {
  myai_ready
  expect_fail cmd_model custom
  assert_contains "$OUT" "Say: myai model custom"
  expect_fail cmd_model custom "no-slash" "x.gguf"
  assert_contains "$OUT" "not a Hugging Face repository name"
  expect_fail cmd_model custom "https://huggingface.co/a/b" "x.gguf"
  expect_fail cmd_model custom "a/b" "x.bin"
  assert_contains "$OUT" "must end in .gguf"
  expect_fail cmd_model custom "a/b" "../../etc/x.gguf"
  expect_fail cmd_model custom "a/b" "sub/../x.gguf"
  assert_not_called "curl"
}

# ------------------------------------------------------- think, telegram ---

test_think_sets_the_mode_and_restarts_the_agent() {
  myai_ready
  expect_ok cmd_think on
  assert_eq "$(env_get AGENT_THINKING)" on
  assert_called "up -d --no-deps agent"
  expect_ok cmd_think auto
  assert_eq "$(env_get AGENT_THINKING)" auto
  expect_fail cmd_think sometimes
  assert_contains "$OUT" "myai think on, myai think off or myai think auto"
  expect_fail cmd_think
  assert_eq "$(env_get AGENT_THINKING)" auto
}

test_think_on_a_stopped_server_restarts_nothing() {
  myai_ready
  : >"$APP/.stopped"
  expect_ok cmd_think off
  assert_eq "$(env_get AGENT_THINKING)" off
  assert_not_called "up -d"
  assert_contains "$OUT" "takes effect when the services are started"
}

test_telegram_set_stores_a_valid_token_without_showing_it() {
  local token="123456789:AAEabcdefghijklmnopqrstuvwxyz_0123-456"
  myai_ready
  expect_ok cmd_telegram set "$token"
  assert_eq "$(env_get TELEGRAM_BOT_TOKEN)" "$token"
  assert_not_contains "$OUT" "$token" "the output"
  assert_contains "$OUT" "/pair 12345678"
  assert_called "up -d --no-deps agent"
}

test_telegram_set_refuses_things_that_are_not_tokens() {
  local bad
  myai_ready
  for bad in "hello" "123:short" "abc:AAEabcdefghijklmnopqrstuvwxyz0123456789" "123456789:has spaces in the long part here ok"; do
    expect_fail cmd_telegram set "$bad"
    assert_contains "$OUT" "does not look like a bot token"
  done
  assert_eq "$(env_get TELEGRAM_BOT_TOKEN)" ""
  # No token given and no terminal to ask on.
  expect_fail cmd_telegram set
  assert_contains "$OUT" "myai telegram set <token>"
  assert_not_called "up -d"
}

test_telegram_explains_the_steps() {
  myai_ready
  expect_ok cmd_telegram
  assert_contains "$OUT" "No Telegram bot is connected yet"
  assert_contains "$OUT" "@BotFather"
  assert_contains "$OUT" "myai telegram set"
  assert_contains "$OUT" "/pair 12345678"
  env_set TELEGRAM_BOT_TOKEN "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456789"
  expect_ok cmd_telegram
  assert_contains "$OUT" "A Telegram bot token is stored"
  assert_not_contains "$OUT" "AAEabc" "the output"
}

# ---------------------------------------------------- start, stop, status ---

test_stop_then_start() {
  myai_ready
  expect_ok cmd_stop
  assert_called "compose --project-directory $APP -f $APP/docker-compose.yml --env-file $APP/.env stop"
  assert_file "$APP/.stopped"
  assert_contains "$OUT" "until you run: myai start"
  expect_ok cmd_start
  assert_no_file "$APP/.stopped"
  assert_called "up -d --remove-orphans"
  assert_contains "$OUT" "Ready."
}

test_start_without_a_model_file_explains() {
  myai_ready
  rm "$APP/data/models/"*.gguf
  expect_fail cmd_start
  assert_contains "$OUT" "The model file is missing"
  assert_contains "$OUT" "myai model use"
  assert_not_called "up -d"
}

test_start_reports_a_crashing_model_server() {
  myai_ready
  echo "restarting none 6" >"$FAKE_DIR/state_llm"
  expect_fail cmd_start
  assert_contains "$OUT" "stopped or keeps crashing"
  assert_contains "$OUT" "myai logs llm"
}

test_restart_one_service_or_all() {
  myai_ready
  expect_ok cmd_restart webui
  assert_called "up -d --force-recreate --no-deps webui"
  assert_contains "$OUT" "Restarted: webui"
  expect_ok cmd_restart
  assert_called "up -d --force-recreate --remove-orphans"
  expect_fail cmd_restart database
  assert_contains "$OUT" "There is no service called 'database'"
  assert_contains "$OUT" "llm agent sandbox webui searxng caddy"
}

test_status_says_what_runs() {
  myai_ready
  expect_ok cmd_status
  assert_contains "$OUT" "Address:   https://srv123456.hstgr.cloud"
  assert_contains "$OUT" "Model:     fast"
  assert_contains "$OUT" "llm       ready"
  assert_contains "$OUT" "caddy     ready"
  assert_contains "$OUT" "Everything is running."
  echo "running unhealthy 0" >"$FAKE_DIR/state_webui"
  echo "running starting 0" >"$FAKE_DIR/state_llm"
  expect_ok cmd_status
  assert_contains "$OUT" "webui     NOT WORKING (it does not answer)"
  assert_contains "$OUT" "llm       starting, not ready yet"
  assert_contains "$OUT" "2 service(s) are not ready"
  assert_contains "$OUT" "myai doctor"
  : >"$APP/.stopped"
  expect_ok cmd_status
  assert_contains "$OUT" "was stopped with \"myai stop\""
}

test_logs_checks_the_service_name() {
  myai_ready
  expect_fail cmd_logs nginx
  assert_contains "$OUT" "There is no service called 'nginx'"
  expect_ok cmd_logs caddy
  assert_called "logs --tail 200 caddy"
}

test_link_and_creds_show_the_secrets() {
  myai_ready
  expect_ok cmd_link
  assert_contains "$OUT" "https://srv123456.hstgr.cloud/enter/0123456789abcdef0123456789abcdef"
  assert_contains "$OUT" "[QR code for https://srv123456.hstgr.cloud/enter/0123456789abcdef0123456789abcdef]"
  expect_ok cmd_creds
  assert_contains "$OUT" "E-mail:    owner@example.com"
  assert_contains "$OUT" "Password:  FirstPassword12345678"
  assert_contains "$OUT" "https://srv123456.hstgr.cloud/agent/"
  assert_contains "$OUT" "/pair 12345678"
}

# -------------------------------------------------------------------- heal ---

test_heal_leaves_a_stopped_server_alone() {
  myai_ready
  : >"$APP/.stopped"
  expect_ok cmd_heal
  assert_eq "$(cat "$FAKE_LOG")" "" "no docker call"
  assert_eq "$OUT" ""
}

test_heal_brings_services_up_and_restarts_unhealthy_ones() {
  myai_ready
  echo "running unhealthy 0" >"$FAKE_DIR/state_webui"
  expect_ok cmd_heal
  assert_called "up -d --remove-orphans"
  assert_called "restart webui"
  assert_not_called "restart llm"
  assert_not_called "restart agent"
  assert_contains "$OUT" "webui does not answer; restarting it"
}

test_heal_is_quiet_when_all_is_well() {
  myai_ready
  expect_ok cmd_heal
  assert_eq "$OUT" ""
  assert_called "up -d --remove-orphans"
  assert_not_called " restart "
}

test_heal_does_not_restart_a_model_that_is_still_loading() {
  myai_ready
  echo "running starting 0" >"$FAKE_DIR/state_llm"
  expect_ok cmd_heal
  assert_not_called " restart "
}

test_heal_steps_aside_for_another_command() {
  myai_ready
  # A backup (or an update) holds the lock.
  (
    flock 9
    sleep 2
  ) 9>"$APP/.lock" &
  sleep 0.3
  expect_ok cmd_heal
  assert_eq "$(cat "$FAKE_LOG")" "" "no docker call while another command works"
  # Other commands say so instead of waiting silently.
  expect_fail cmd_stop
  assert_contains "$OUT" "Another myai command is still working"
  wait
}

test_heal_does_nothing_without_a_model_file() {
  myai_ready
  rm "$APP/data/models/"*.gguf
  expect_ok cmd_heal
  assert_not_called "up -d"
}

# --------------------------------------------------------------- uninstall ---

installed_system_files() {
  mkdir -p "$MYAI_SYSROOT/etc/systemd/system"
  for unit in myai-backup.service myai-backup.timer myai-heal.service myai-heal.timer; do
    echo "[Unit]" >"$MYAI_SYSROOT/etc/systemd/system/$unit"
  done
  ln -s "$APP/bin/myai" "$MYAI_SYSROOT/usr/local/bin/myai"
  echo "secrets" >"$MYAI_SYSROOT/root/myai-credentials.txt"
}

test_uninstall_keeps_data_by_default() {
  myai_ready
  installed_system_files
  ASSUME_YES=1
  expect_ok cmd_uninstall
  assert_called "systemctl disable --now myai-backup.timer myai-heal.timer"
  assert_called "down --remove-orphans --volumes --rmi local"
  assert_eq "$(find "$MYAI_SYSROOT/etc/systemd/system" -name 'myai-*' | wc -l)" 0 "unit files left"
  assert_no_file "$MYAI_SYSROOT/usr/local/bin/myai"
  assert_file "$APP/.env"
  assert_file "$APP/data/webui/webui.db"
  assert_file "$MYAI_SYSROOT/root/myai-credentials.txt"
  assert_contains "$OUT" "Kept: $APP"
  assert_contains "$OUT" "Left as they are: Docker"
}

test_uninstall_purge_deletes_everything_of_myai() {
  myai_ready
  installed_system_files
  ASSUME_YES=1
  expect_ok cmd_uninstall --purge
  assert_no_file "$APP"
  assert_no_file "$MYAI_SYSROOT/root/myai-credentials.txt"
  assert_no_file "$MYAI_SYSROOT/usr/local/bin/myai"
  [ -d "$T" ] || fail "the folder above the app must still exist"
}

test_uninstall_asks_first() {
  myai_ready
  installed_system_files
  expect_fail cmd_uninstall --purge
  assert_contains "$OUT" "Nothing was removed"
  assert_file "$APP/.env"
  assert_file "$MYAI_SYSROOT/usr/local/bin/myai"
  assert_not_called "down"
}

# ------------------------------------------------------- the command itself ---

run_myai() { MYAI_DIR="$APP" bash "$AI_DIR/bin/myai" "$@"; }

test_main_help_needs_neither_root_nor_an_installation() {
  echo 1000 >"$FAKE_DIR/uid"
  expect_ok run_myai help
  assert_contains "$OUT" "myai model use <id>"
  expect_ok run_myai
  assert_contains "$OUT" "myai status"
  expect_ok run_myai --help
}

test_main_refuses_without_root_or_installation() {
  fake_install
  echo 1000 >"$FAKE_DIR/uid"
  expect_fail run_myai status
  assert_contains "$OUT" "myai must be run as root"
  assert_contains "$OUT" "sudo myai status"
  echo 0 >"$FAKE_DIR/uid"
  rm "$APP/.env"
  expect_fail run_myai status
  assert_contains "$OUT" "myai is not set up in $APP"
}

test_main_unknown_command_points_to_help() {
  fake_install
  expect_fail run_myai dance
  assert_contains "$OUT" "Unknown command: dance"
  assert_contains "$OUT" "myai help"
}

test_main_runs_commands_and_takes_yes_anywhere() {
  fake_install
  expect_ok run_myai status
  assert_contains "$OUT" "Everything is running."
  expect_ok run_myai think on
  expect_ok run_myai link
  assert_contains "$OUT" "/enter/0123456789abcdef0123456789abcdef"
  # --yes before or after the command
  echo "tiny|1|a/b|tiny.gguf|8|Tiny" >"$APP/config/models.conf"
  printf 'GGUFtiny' >"$APP/data/models/tiny.gguf"
  fake_machine 1 500000 # far too little memory: myai asks before switching
  expect_fail run_myai model use tiny </dev/null
  expect_ok run_myai model use tiny --yes
  expect_ok run_myai --yes model use tiny
}

test_main_finds_its_folder_through_the_symlink() {
  fake_install
  ln -s "$APP/bin/myai" "$T/bin/myai"
  expect_ok env -u MYAI_DIR myai status
  assert_contains "$OUT" "Everything is running."
}

test_update_hands_over_to_the_installed_installer() {
  myai_ready
  cat >"$APP/install.sh" <<'STUB'
#!/usr/bin/env bash
echo "installer got: $*"
STUB
  expect_ok cmd_update
  assert_eq "$OUT" "installer got: --update --yes --dir $APP"
  rm "$APP/install.sh"
  expect_fail cmd_update
  assert_contains "$OUT" "The installer is missing"
}

# ------------------------------------------------------------------ doctor ---

doctor_ready() {
  myai_ready
  export MYAI_DOCTOR_SAMPLE_SECS=0
  echo 200 >"$FAKE_DIR/https_code"
  echo "myai-caddy-1" >"$FAKE_DIR/ps_ours_80"
  echo "myai-caddy-1" >"$FAKE_DIR/ps_ours_443"
  echo 300000000 >"$FAKE_DIR/df_avail_kb"
  echo old >"$APP/backups/myai-$(date +%Y%m%d)-031500.tar.gz"
}

test_doctor_all_good() {
  doctor_ready
  expect_ok cmd_doctor
  assert_contains "$OUT" "[ok]       DNS: srv123456.hstgr.cloud points to this server (203.0.113.10)."
  assert_contains "$OUT" "[ok]       Port 443 is served by the myai proxy."
  assert_contains "$OUT" "[ok]       HTTPS answers with a valid certificate (status 200)."
  assert_contains "$OUT" "[ok]       Service llm: ready."
  assert_contains "$OUT" "[ok]       Model file:"
  assert_contains "$OUT" "[ok]       Memory: 7.6 GB free of 31.3 GB."
  assert_contains "$OUT" "[ok]       Disk:"
  assert_contains "$OUT" "[ok]       CPU: steal 0.0% (normal)"
  assert_contains "$OUT" "[ok]       Timers:"
  assert_contains "$OUT" "[ok]       Newest backup:"
  assert_contains "$OUT" "All checks passed."
  assert_not_contains "$OUT" "PROBLEM" "the report"
}

test_doctor_names_problems_with_advice() {
  doctor_ready
  echo "198.51.100.77" >"$FAKE_DIR/dig_A_srv123456.hstgr.cloud"
  echo "2001:db8::77" >"$FAKE_DIR/dig_AAAA_srv123456.hstgr.cloud"
  rm "$FAKE_DIR/ps_ours_80"
  echo 'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=1,fd=6))' >"$FAKE_DIR/ss_tcp_80"
  echo 60 >"$FAKE_DIR/https_rc"
  echo 000 >"$FAKE_DIR/https_code"
  echo "running unhealthy 0" >"$FAKE_DIR/state_agent"
  echo "running starting 0" >"$FAKE_DIR/state_llm"
  rm "$APP/data/models/"*.gguf
  sed -i -e 's/^MemAvailable:.*/MemAvailable:    300000 kB/' -e 's/^SwapFree:.*/SwapFree:        1000000 kB/' \
    "$MYAI_SYSROOT/proc/meminfo"
  echo 2000000 >"$FAKE_DIR/df_avail_kb"
  rm "$APP/backups/"*
  # 30% of the CPU time is taken away between two readings.
  cpu_sample() {
    if [ -e "$T/second-reading" ]; then
      echo "11000 400 8500"
    else
      : >"$T/second-reading"
      echo "10000 100 8000"
    fi
  }
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  The name srv123456.hstgr.cloud points to 198.51.100.77, but this server's public address is 203.0.113.10."
  assert_contains "$OUT" "also has an IPv6 address (2001:db8::77) that is not on this server"
  assert_contains "$OUT" "[PROBLEM]  Port 80 is used by another program (nginx)"
  assert_contains "$OUT" "[PROBLEM]  The certificate for srv123456.hstgr.cloud is not valid (yet)."
  assert_contains "$OUT" "myai logs caddy"
  assert_contains "$OUT" "[PROBLEM]  Service agent: NOT WORKING (it does not answer)."
  assert_contains "$OUT" "[look]     Service llm is still starting."
  assert_contains "$OUT" "[PROBLEM]  The model file is missing or damaged"
  assert_contains "$OUT" "[PROBLEM]  Memory is almost full"
  assert_contains "$OUT" "More than half of the swap space is in use"
  assert_contains "$OUT" "[PROBLEM]  The disk is almost full: 2.0 GB free."
  assert_contains "$OUT" "[PROBLEM]  The provider is holding this server back: 30.0% of the CPU time"
  assert_contains "$OUT" "CPU credits are used up"
  assert_contains "$OUT" "[look]     There is no backup yet."
  assert_contains "$OUT" "problem(s)"
}

test_doctor_localhost_and_stopped() {
  doctor_ready
  env_set SITE_ADDRESS localhost
  env_set CADDY_TLS_LINE "tls internal"
  : >"$APP/.stopped"
  expect_ok cmd_doctor
  assert_contains "$OUT" "The address is \"localhost\""
  assert_contains "$OUT" "HTTPS answers (self-signed certificate, as configured)."
  assert_contains "$OUT" "myai was stopped with \"myai stop\""
  assert_called "-k https://localhost/"
}

test_doctor_certificate_end_date() {
  doctor_ready
  # A certificate that ends in 5 days: the fake s_client output is fed to the real openssl.
  "$REAL_OPENSSL" req -x509 -newkey rsa:2048 -nodes -keyout "$T/key.pem" -out "$FAKE_DIR/s_client_out" \
    -days 5 -subj "/CN=srv123456.hstgr.cloud" >/dev/null 2>&1 || skip "openssl cannot make a test certificate"
  expect_ok cmd_doctor
  [[ $OUT == *"The certificate ends in "[45]" days and was not renewed yet."* ]] || fail "no warning about the end date: $OUT"
}
