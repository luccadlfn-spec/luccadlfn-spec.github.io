#!/usr/bin/env bash
# The model server (llama.cpp): does a build know every setting that
# docker-compose.yml gives it, did the system stop it because the memory ran
# out, and does it hold a second copy of the model? Docker, the kernel log and
# /proc are fakes; the help text is the real one of a llama.cpp server build.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh, t_stack.sh, install.sh and bin/myai

HELP_TEXT="$TESTS_DIR/llama-server-help.txt"

# The file the fake docker reads the help text of IMAGE from.
help_of() { printf '%s/llama_help_%s\n' "$FAKE_DIR" "${1//[^A-Za-z0-9]/_}"; }

# The real help text without the variable NAME: a build that does not know it.
help_without() { grep -vF "(env: $1)" "$HELP_TEXT"; }

# The real help text without the line of the option --threads-batch: a build
# that does not know it. "--threads-batch-draft" and the other lines stay.
help_without_threads_batch() { sed '/^-tb, *--threads-batch /d' "$HELP_TEXT"; }

# grow_file FILE BYTES: FILE gets that size without using disk space; its
# first bytes ("GGUF") stay.
grow_file() { python3 -c 'import sys; open(sys.argv[1], "r+b").truncate(int(sys.argv[2]))' "$1" "$2"; }

# fake_llm_process ANON_KB FILE_KB [SWAP_KB]: the model server runs as
# process 4242 with that much memory in RAM (and in swap), in a container
# made at 03:00 UTC. The lines and their order are those of Linux 6.
fake_llm_process() {
  echo "4242 false 0 2026-11-26T03:00:00.000000000Z" >"$FAKE_DIR/facts_llm"
  mkdir -p "$MYAI_SYSROOT/proc/4242"
  printf 'Name:\tllama-server\nVmRSS:\t%s kB\nRssAnon:\t%s kB\nRssFile:\t%s kB\nRssShmem:\t0 kB\nVmSwap:\t%s kB\n' \
    "$(($1 + $2))" "$1" "$2" "${3:-0}" >"$MYAI_SYSROOT/proc/4242/status"
}

# What the kernel logs when the whole machine runs out of memory and it stops
# a program (Linux 6). A container with a memory limit gets "Memory cgroup out
# of memory: Killed process ..." instead.
oom_lines() { # PID NAME
  printf '%s invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0\n' "$2"
  printf 'oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=docker-0123.scope,mems_allowed=0,global_oom,task_memcg=/system.slice/docker-0123.scope,task=%s,pid=%s,uid=0\n' "$2" "$1"
  printf 'Out of memory: Killed process %s (%s) total-vm:40123456kB, anon-rss:17012345kB, file-rss:20123456kB, shmem-rss:0kB, UID:0 pgtables:80000kB oom_score_adj:0\n' "$1" "$2"
}

# ----------------------------------------------- which settings a build knows ---

test_llm_settings_are_read_from_the_compose_file() {
  local name
  load_installer
  assert_eq "$(compose_llm_settings "$AI_DIR/docker-compose.yml" | paste -sd' ' -)" \
    "LLAMA_ARG_MODEL LLAMA_ARG_ALIAS LLAMA_ARG_HOST LLAMA_ARG_PORT LLAMA_API_KEY LLAMA_ARG_CTX_SIZE LLAMA_ARG_N_PARALLEL LLAMA_ARG_THREADS LLAMA_ARG_LOAD_MODE LLAMA_ARG_REPACK LLAMA_ARG_NO_REPACK LLAMA_ARG_CACHE_RAM LLAMA_ARG_CTX_CHECKPOINTS LLAMA_ARG_UI LLAMA_ARG_REASONING LLAMA_ARG_THINK_BUDGET LLAMA_ARG_N_PREDICT LLAMA_ARG_TEMPERATURE LLAMA_ARG_TOP_P LLAMA_ARG_TOP_K LLAMA_ARG_MIN_P LLAMA_ARG_PRESENCE_PENALTY LLAMA_ARG_TIMEOUT --threads-batch" \
    "the settings of service llm"
  # Every one of them can be explained to the owner when it is lost.
  for name in $(compose_llm_settings "$AI_DIR/docker-compose.yml"); do
    case "$(llama_setting_meaning "$name")" in
      *"a setting from docker-compose.yml"*) fail "no explanation for $name in llama_setting_meaning" ;;
    esac
  done
  # Only service llm counts; comments do not; the list form of a compose file works too.
  cat >"$T/compose.yml" <<'YML'
services:
  agent:
    environment:
      LLAMA_ARG_OF_THE_AGENT: "1"
  llm:
    # LLAMA_ARG_IN_A_COMMENT: "1"
    environment:
      - LLAMA_ARG_ONE=1
      # - LLAMA_ARG_COMMENTED_OUT=1
      - LLAMA_API_KEY=${KEY}
    command:
      - --flag-one
      - "${N:-8}"
      - --flag-two=3
    healthcheck:
      test: ["CMD", "curl", "--fail", "http://127.0.0.1:8080/health"]
networks:
  llm: {}
YML
  assert_eq "$(compose_llm_settings "$T/compose.yml" | paste -sd' ' -)" "LLAMA_ARG_ONE LLAMA_API_KEY --flag-one --flag-two"
}

test_llm_build_that_knows_every_setting_is_used() {
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  : >"$FAKE_LOG"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "The model server build knows all the settings that myai gives it."
  assert_not_contains "$(cat "$T/out")" "Warning" "the output"
  # The build that is about to start is asked, without network and without a download.
  assert_called "docker run --rm --init --pull never --network none --name myai-llama-help-"
  assert_called "ghcr.io/ggml-org/llama.cpp@$DIGEST --help"
  assert_call_order "ghcr.io/ggml-org/llama.cpp@$DIGEST --help" "given for this call"
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
}

test_llm_first_install_names_lost_settings_and_goes_on() {
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # A newer llama.cpp that renamed the repacking switch and dropped one more name.
  sed -e 's/(env: LLAMA_ARG_REPACK)/(env: LLAMA_ARG_WEIGHT_REPACK)/' -e '/(env: LLAMA_ARG_TEMPERATURE)/d' \
    "$HELP_TEXT" >"$FAKE_DIR/llama_help"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "The model server build does not know 2 of the settings that myai gives it. It ignores them without a message:"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_REPACK: the switch that keeps a single copy of the model in memory. Without it the model needs about twice its size in memory"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_TEMPERATURE: how the model picks its words (LLM_TEMPERATURE)."
  assert_contains "$(cat "$T/out")" "myai goes on with this build. To see this list again: myai doctor"
  # Named once, under the new name of the switch.
  assert_eq "$(grep -c 'REPACK' "$T/out")" 1 "lines about repacking"
  # There is no other build to go back to: this one is tried and recorded.
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
  assert_contains "$(cat "$T/out")" "All services are running"
}

test_llm_update_keeps_the_build_that_knows_the_settings() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # The newest build would lose the repacking switch; the current one knows it.
  help_without LLAMA_ARG_REPACK >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_called "docker pull $TAG"
  assert_contains "$(cat "$T/out")" "The newest build of the model server does not know 1 of the settings that myai gives it, and would ignore them:"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_REPACK: the switch that keeps a single copy of the model in memory."
  assert_contains "$(cat "$T/out")" "So the model server stays on the build that works. The next \"myai update\" tries the newest build again."
  # It was never started, so it could not be recorded.
  assert_not_called "given for this call"
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD" "LLAMA_IMAGE after the update"
  assert_called "$OLD_BUILD --help"
  assert_contains "$(cat "$T/out")" "The current build knows all the settings that myai gives it."
  assert_not_called "docker image rm $OLD_BUILD"
  # Unused, it does not stay on the disk: its exact name and the moving tag go.
  assert_called "docker image rm ghcr.io/ggml-org/llama.cpp@$DIGEST $TAG"
  assert_contains "$(cat "$T/out")" "All services are running"
}

test_llm_update_names_what_the_current_build_lacks_too() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # The newest build would lose the repacking switch; the current one lacks LLAMA_ARG_UI.
  help_without LLAMA_ARG_REPACK >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  help_without LLAMA_ARG_UI >"$(help_of "$OLD_BUILD")"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "So the model server stays on the build that works."
  assert_contains "$(cat "$T/out")" "The current build does not know 1 of the settings that myai gives it. It ignores them without a message:"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_UI: that the model server's own chat page is off."
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD"
}

test_llm_update_tries_a_build_that_loses_nothing_new() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # Neither build knows LLAMA_ARG_UI: the newer one loses nothing that works now.
  help_without LLAMA_ARG_UI >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  help_without LLAMA_ARG_UI >"$(help_of "$OLD_BUILD")"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_called "compose up: LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp@$DIGEST given for this call"
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST"
  assert_not_contains "$(cat "$T/out")" "stays on the build that works" "the output"
  assert_contains "$(cat "$T/out")" "The model server build does not know 1 of the settings that myai gives it."
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_UI: that the model server's own chat page is off. Nothing outside Docker can reach that page, so this one does no harm."
}

test_llm_update_tries_a_build_that_loses_only_smaller_settings() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # The current build knows every setting. The newest one lost two that cannot
  # stop the model server, fill the memory or let others use it: it is tried
  # all the same, so that its fixes are not missed.
  grep -vE '\(env: LLAMA_ARG_(UI|TEMPERATURE)\)' "$HELP_TEXT" >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  : >"$FAKE_LOG"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_called "compose up: LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp@$DIGEST given for this call"
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST" "LLAMA_IMAGE after the update"
  assert_not_contains "$(cat "$T/out")" "stays on the build that works" "the output"
  assert_contains "$(cat "$T/out")" "The model server build does not know 2 of the settings that myai gives it. It ignores them without a message:"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_TEMPERATURE: how the model picks its words (LLM_TEMPERATURE)."
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_UI: that the model server's own chat page is off."
  assert_not_called "docker image rm ghcr.io/ggml-org/llama.cpp@$DIGEST"
}

test_llm_update_tries_a_build_that_mends_the_current_one() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # The current build lacks the repacking switch, so it holds the model twice.
  # The newest one knows the switch and lost only the harmless LLAMA_ARG_UI.
  help_without LLAMA_ARG_REPACK >"$(help_of "$OLD_BUILD")"
  help_without LLAMA_ARG_UI >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST" "LLAMA_IMAGE after the update"
  assert_not_contains "$(cat "$T/out")" "stays on the build that works" "the output"
  assert_not_contains "$(cat "$T/out")" "REPACK" "the output"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_UI: that the model server's own chat page is off."
}

test_llm_update_keeps_the_current_build_when_only_the_newer_one_can_be_asked() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  help_without LLAMA_ARG_CACHE_RAM >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  : >"$(help_of "$OLD_BUILD")"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_CACHE_RAM: the memory limit for remembered conversations (LLM_CACHE_RAM_MIB)."
  assert_contains "$(cat "$T/out")" "So the model server stays on the build that works."
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD"
  # Nothing is claimed about the current build, which could not be asked.
  assert_not_contains "$(cat "$T/out")" "knows all the settings" "the output"
}

test_llm_old_build_knows_the_switch_by_its_old_name() {
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  # llama.cpp b6049 to b7375 know the repacking switch only as LLAMA_ARG_NO_REPACK.
  sed 's/(env: LLAMA_ARG_REPACK)/(env: LLAMA_ARG_NO_REPACK)/' "$HELP_TEXT" >"$FAKE_DIR/llama_help"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "The model server build knows all the settings that myai gives it."
  # Without the old name in the compose file, such a build would lose the switch.
  sed -i '/LLAMA_ARG_NO_REPACK/d' "$APP/docker-compose.yml"
  start_stack >"$T/out" 2>&1 || fail "start_stack failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "- LLAMA_ARG_REPACK: the switch that keeps a single copy of the model in memory."
}

test_llm_first_install_stops_on_an_unknown_option() {
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  help_without_threads_batch >"$FAKE_DIR/llama_help"
  grep -q -- '--threads-batch-draft' "$FAKE_DIR/llama_help" || fail "the test help text lost --threads-batch-draft"
  : >"$FAKE_LOG"
  # llama-server stops at once on an option it does not know, and there is no
  # other build to go back to: the installer stops before it starts anything.
  expect_fail start_stack
  assert_contains "$OUT" "Stopped: This build of the model server does not know 1 of the options that myai gives it, so it cannot start:"
  assert_contains "$OUT" "- --threads-batch: how many CPU threads read prompts (LLM_THREADS_BATCH)."
  assert_contains "$OUT" "A newer myai may know the new names: myai update"
  assert_not_contains "$OUT" "without a message" "the output"
  assert_not_contains "$OUT" "goes on" "the output"
  assert_not_called "up -d"
  assert_eq "$(env_get LLAMA_IMAGE)" "$TAG"
}

test_llm_update_keeps_the_build_that_knows_the_options() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  help_without_threads_batch >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  (update_run) >"$T/out" 2>&1 || fail "the update failed: $(cat "$T/out")"
  assert_contains "$(cat "$T/out")" "The newest build of the model server does not know 1 of the options that myai gives it, so it would not start:"
  assert_contains "$(cat "$T/out")" "- --threads-batch: how many CPU threads read prompts (LLM_THREADS_BATCH)."
  assert_not_contains "$(cat "$T/out")" "ignore" "the output"
  assert_contains "$(cat "$T/out")" "So the model server stays on the build that works."
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD" "LLAMA_IMAGE after the update"
  assert_contains "$(cat "$T/out")" "All services are running"
}

test_llm_update_stops_when_no_build_knows_an_option() {
  pinned_install
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  help_without_threads_batch >"$(help_of "ghcr.io/ggml-org/llama.cpp@$DIGEST")"
  help_without_threads_batch >"$(help_of "$OLD_BUILD")"
  : >"$FAKE_LOG"
  expect_fail update_run
  assert_contains "$OUT" "Stopped: This build of the model server does not know 1 of the options that myai gives it, so it cannot start:"
  assert_not_called "up -d"
  assert_eq "$(env_get LLAMA_IMAGE)" "$OLD_BUILD"
}

test_llm_options_are_not_judged_without_option_lines() {
  load_installer
  # A help text in which no line introduces an option cannot tell which
  # options a build knows: none of them is called unknown.
  sed 's/^-/ -/' "$HELP_TEXT" >"$FAKE_DIR/llama_help"
  assert_eq "$(llama_lost_settings "$TAG" "$AI_DIR/docker-compose.yml")" "" "settings lost, by a help text without option lines"
  # With option lines, a missing one is named.
  help_without_threads_batch >"$FAKE_DIR/llama_help"
  assert_eq "$(llama_lost_settings "$TAG" "$AI_DIR/docker-compose.yml")" "--threads-batch" "settings lost, by a help text without --threads-batch"
}

test_llm_help_of_a_build_that_hangs_is_dropped() {
  local rc out name
  load_installer
  # docker still waits when the minute is up: "timeout" gives up (124), or
  # stops docker by force 10 seconds later (137). By then docker has printed
  # part of the help text, and the container may still run.
  cat >"$T/bin/timeout" <<'FAKE'
#!/usr/bin/env bash
printf 'timeout %s\n' "$*" >>"$FAKE_LOG"
printf -- '-t,    --threads N\n                                        (env: LLAMA_ARG_THREADS)\n'
exit "$(cat "$FAKE_DIR/rc_timeout")"
FAKE
  chmod 755 "$T/bin/timeout"
  for rc in 124 137; do
    echo "$rc" >"$FAKE_DIR/rc_timeout"
    : >"$FAKE_LOG"
    out=$(llama_help "$TAG")
    assert_eq "$out" "" "the help text of a build stopped half-way (exit $rc)"
    assert_called "timeout -k 10 60 docker run --rm --init --pull never --network none --name myai-llama-help-"
    name=$(grep -oE 'myai-llama-help-[0-9]+-[0-9]+' "$FAKE_LOG" | head -n 1)
    assert_called "docker rm -f $name"
    if llama_lost_settings "$TAG" "$AI_DIR/docker-compose.yml" >/dev/null; then
      fail "a build stopped half-way must count as one that could not say (exit $rc)"
    fi
  done
}

test_llm_settings_check_is_skipped_when_the_build_cannot_say() {
  local how
  installed
  with_model_file
  echo "ghcr.io/ggml-org/llama.cpp@$DIGEST" >"$FAKE_DIR/repodigests"
  for how in empty other-format no-image; do
    case "$how" in
      empty) : >"$FAKE_DIR/llama_help" ;;
      other-format) sed 's/(env: /[variable: /' "$HELP_TEXT" >"$FAKE_DIR/llama_help" ;;
      no-image) rm -f "$FAKE_DIR/llama_help" ;;
    esac
    env_set LLAMA_IMAGE "$TAG"
    PLAN[LLAMA_IMAGE]=$TAG
    start_stack >"$T/out" 2>&1 || fail "start_stack failed ($how): $(cat "$T/out")"
    assert_contains "$(cat "$T/out")" "Skipped one check: the model server program could not say which settings it knows." "the output ($how)"
    assert_not_contains "$(cat "$T/out")" "Warning" "the output ($how)"
    assert_eq "$(env_get LLAMA_IMAGE)" "ghcr.io/ggml-org/llama.cpp@$DIGEST" "LLAMA_IMAGE ($how)"
  done
}

test_llm_settings_check_in_a_dry_run_only_says_so() {
  local out
  pinned_install
  : >"$FAKE_LOG"
  out=$( (update_run --dry-run) 2>&1) || fail "the dry run failed: $out"
  assert_contains "$out" "would check that the model server build knows every setting in docker-compose.yml"
  assert_contains "$out" "would check that the model server did not run out of memory and holds the model only once"
  assert_not_called "docker run"
  assert_not_called "journalctl"
}

test_doctor_names_settings_the_model_server_ignores() {
  doctor_ready
  env_set LLAMA_IMAGE "$OLD_BUILD"
  help_without LLAMA_ARG_REPACK >"$(help_of "$OLD_BUILD")"
  : >"$FAKE_LOG"
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  The model server ignores 1 of the settings that myai gives it:"
  assert_contains "$OUT" "- LLAMA_ARG_REPACK: the switch that keeps a single copy of the model in memory."
  assert_contains "$OUT" "A newer myai may know the new names: myai update"
  assert_called "docker run --rm --init --pull never --network none --name myai-llama-help-"
  assert_called "$OLD_BUILD --help"
  # Only harmless ones lost: something to look at, not a problem.
  help_without LLAMA_ARG_UI >"$(help_of "$OLD_BUILD")"
  expect_ok cmd_doctor
  assert_contains "$OUT" "[look]     The model server ignores 1 of the settings that myai gives it:"
  # A build that cannot be asked.
  : >"$(help_of "$OLD_BUILD")"
  expect_ok cmd_doctor
  assert_contains "$OUT" "[look]     Could not check which settings the model server knows: its program could not say."
}

test_doctor_names_an_option_the_model_server_does_not_know() {
  doctor_ready
  env_set LLAMA_IMAGE "$OLD_BUILD"
  help_without_threads_batch >"$(help_of "$OLD_BUILD")"
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  This build of the model server does not know 1 of the options that myai gives it, so it cannot start:"
  assert_contains "$OUT" "- --threads-batch: how many CPU threads read prompts (LLM_THREADS_BATCH)."
  assert_contains "$OUT" "A newer myai may know the new names: myai update"
  assert_not_contains "$OUT" "ignores" "the report"
  # Together with a variable it does not know: each under its own headline.
  help_without_threads_batch | grep -vF "(env: LLAMA_ARG_UI)" >"$(help_of "$OLD_BUILD")"
  expect_fail cmd_doctor
  assert_contains "$OUT" "of the options that myai gives it, so it cannot start:"
  assert_contains "$OUT" "[look]     The model server ignores 1 of the settings that myai gives it:"
  assert_contains "$OUT" "- LLAMA_ARG_UI:"
}

# ------------------------------------------------------------ out of memory ---

test_doctor_reports_out_of_memory_stops_with_advice() {
  local since
  doctor_ready
  env_set MODEL_ID hauhau
  fake_llm_process 650000 2450000
  echo "4242 false 2 2026-11-26T03:00:00.000000000Z" >"$FAKE_DIR/facts_llm"
  {
    oom_lines 4100 llama-server
    echo "Memory cgroup out of memory: Killed process 4180 (llama-server) total-vm:40123456kB, anon-rss:17012345kB, file-rss:88kB, shmem-rss:0kB, UID:0 pgtables:80000kB oom_score_adj:0"
    oom_lines 977 python3
  } >"$FAKE_DIR/journal_kernel"
  : >"$FAKE_LOG"
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  The system stopped the model server 2 time(s) because the memory ran out."
  assert_contains "$OUT" "- a smaller model: myai model use classic   (or the much weaker: myai model use fast)"
  assert_contains "$OUT" "set LLM_CTX=16384, AGENT_CTX_TOKENS=6000"
  assert_contains "$OUT" "and LLM_CACHE_RAM_MIB=512 (the values for small servers), then run: myai restart"
  assert_contains "$OUT" "ignores the switch that keeps a single copy"
  assert_not_contains "$OUT" "has restarted" "the report"
  # Only kills since the container was made, in the kernel log of every boot
  # since then.
  since=$(date -d 2026-11-26T03:00:00Z +%s)
  assert_called "journalctl -q --no-pager -o cat _TRANSPORT=kernel --since @$since"
  # The advice fits the model in use.
  env_set MODEL_ID classic
  expect_fail cmd_doctor
  assert_contains "$OUT" "- a smaller model: myai model use fast   (much weaker)"
  env_set MODEL_ID fast
  expect_fail cmd_doctor
  assert_not_contains "$OUT" "a smaller model" "the advice for the smallest model"
}

test_doctor_counts_out_of_memory_stops_from_before_a_restart() {
  doctor_ready
  env_set MODEL_ID hauhau
  fake_llm_process 650000 2450000
  # The server was restarted since. The container stays (made at 03:00) and
  # Docker has forgotten the stop: only the kernel log of the boot before
  # knows about it.
  oom_lines 4100 llama-server >"$FAKE_DIR/journal_kernel_before"
  : >"$FAKE_DIR/journal_kernel"
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  The system stopped the model server 1 time(s) because the memory ran out."
  assert_not_called "--dmesg"
}

test_doctor_out_of_memory_seen_by_docker_alone() {
  doctor_ready
  # Between two tries: the container waits to be started again, and the
  # kernel log cannot be read.
  echo "restarting none 1" >"$FAKE_DIR/state_llm"
  echo "0 true 1 2026-11-26T03:00:00.000000000Z" >"$FAKE_DIR/facts_llm"
  rm "$T/bin/journalctl"
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  The system stopped the model server 1 time(s) because the memory ran out."
}

test_doctor_restarts_without_memory_trouble() {
  doctor_ready
  echo "4242 false 3 2026-11-26T03:00:00.000000000Z" >"$FAKE_DIR/facts_llm"
  oom_lines 977 python3 >"$FAKE_DIR/journal_kernel"
  expect_ok cmd_doctor
  assert_contains "$OUT" "[look]     The model server has restarted 3 time(s) since it was set up."
  assert_contains "$OUT" "its messages say why: myai logs llm"
  assert_not_contains "$OUT" "memory ran out" "the report"
}

# ------------------------------------------------- one copy of the model, or two ---

# The line between one copy and two (model_held_twice), against measured
# figures and the catalogue.
test_llm_memory_line_fits_the_measurements_and_the_catalogue() {
  local f4b=2497281120 fmoe=4230976032 id size kv kv_min state buffers settings ctx cache anon fresh
  load_installer
  # Measured on the stand-in llama-server (llama.cpp master f5e6915c2) with the
  # environment of docker-compose.yml, a context of 4096 and no prompt cache.
  # RssAnon and RssFile in kB, then the model file in bytes.
  model_held_twice 3129552 2435440 $f4b 4096 0 || fail "4B dense with repacking: not noticed"
  model_held_twice 3557676 4142308 $fmoe 4096 0 || fail "mixture of experts with repacking: not noticed"
  if model_held_twice 651076 2449200 $f4b 4096 0; then fail "4B dense without repacking: false alarm"; fi
  if model_held_twice 250056 4144356 $fmoe 4096 0; then fail "mixture of experts without repacking: false alarm"; fi
  # Not mapped (load mode "none"): the one copy of the model is in RssAnon.
  if model_held_twice 3084012 15908 $f4b 4096 0; then fail "4B without mmap: false alarm"; fi
  if model_held_twice 3445892 15920 $f4b 4096 0; then fail "4B without mmap, repacked while loading: false alarm"; fi
  # The figures measured before (GB): repacking on, then load mode "mlock" without mmap.
  model_held_twice $((3130000000 / 1024)) $((2430000000 / 1024)) $f4b 4096 0 || fail "4B dense, earlier figures: not noticed"
  model_held_twice $((3440000000 / 1024)) $((4140000000 / 1024)) $fmoe 4096 0 || fail "mixture of experts, earlier figures: not noticed"
  if model_held_twice $((3440000000 / 1024)) $((20000000 / 1024)) $f4b 4096 0; then fail "4B without mmap, earlier figures: false alarm"; fi

  # The catalogue at its default settings never raises a false alarm. Generous
  # estimates of what is rightly in RssAnon besides the model: context memory
  # (98 KB per token for "classic", which has full attention in all 48 layers;
  # at most 40 KB for the hybrid Qwen3.5 and Qwen3.6 models), their recurrent
  # state with 8 snapshots per slot (at most 1.2 GB), a full prompt cache,
  # 0.8 GB of working buffers and 0.3 GB for the rest. RssFile is the file.
  for id in hauhau heretic huihui classic fast; do
    size=$(catalog_field "$id" 5)
    kv=40000 kv_min=20000 state=1200000000 buffers=800000000
    if [ "$id" = classic ]; then kv=98000 kv_min=98000 state=0; fi
    # 32 GB servers: LLM_CTX 32768, LLM_CACHE_RAM_MIB 2048; small ones: 16384 and 512.
    for settings in "32768 2048" "16384 512"; do
      read -r ctx cache <<<"$settings"
      anon=$(((ctx * kv + state + cache * 1048576 + buffers + 300000000) / 1024))
      if model_held_twice "$anon" $((size / 1024)) "$size" "$ctx" "$cache"; then
        fail "$id at a context of $ctx and a $cache MiB prompt cache: false alarm"
      fi
    done
    # With repacking on, the big models are noticed even with an empty prompt
    # cache and the least context memory: a copy of 70% of the model. (For the
    # mixture of experts the copy was 80%, 72% of it in the CPU_REPACK buffers
    # that CPUs without AMX use.) For "fast" a copy is smaller than a full
    # prompt cache, so it cannot be told apart; it fits in memory anyway.
    if [ "$id" != fast ]; then
      fresh=$(((32768 * kv_min + 300000000) / 1024 + size * 7 / 10 / 1024))
      model_held_twice "$fresh" $((size / 1024)) "$size" 32768 2048 || fail "$id with repacking: not noticed"
    fi
  done
}

test_doctor_notices_a_second_copy_of_the_model() {
  local model
  doctor_ready
  env_set LLM_CTX 4096
  env_set LLM_CACHE_RAM_MIB 0
  model="$APP/data/models/$(env_get MODEL_FILE)"
  grow_file "$model" 2497281120
  # Measured with repacking on.
  fake_llm_process 3129552 2435440
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  The model server uses 5.7 GB of memory for a 2.5 GB model file: it holds a second copy of the model."
  assert_contains "$OUT" "llama.cpp makes that copy (\"repacking\") when it ignores the switch that myai sets."
  assert_contains "$OUT" "myai update"
  # Measured with repacking off.
  fake_llm_process 651076 2449200
  expect_ok cmd_doctor
  assert_contains "$OUT" "[ok]       Model server memory: 3.2 GB for a 2.5 GB model file."
  assert_not_contains "$OUT" "second copy" "the report"
}

test_doctor_counts_the_part_of_the_copy_in_swap() {
  doctor_ready
  # The default model (21.2 GB) at the default settings, repacked: a copy of
  # 72% (15.2 GB) plus 1 GB. The model file is locked in RAM, so on a 32 GB
  # server 4.5 GB of the copy went to swap.
  grow_file "$APP/data/models/$(env_get MODEL_FILE)" 21166758016
  env_set LLM_CTX 32768
  env_set LLM_CACHE_RAM_MIB 2048
  fake_llm_process 11464908 20670662 4394531
  expect_fail cmd_doctor
  assert_contains "$OUT" "[PROBLEM]  The model server uses 37.4 GB of memory (4.5 GB of it in swap) for a 21.2 GB model file: it holds a second copy of the model."
  # One copy, a little of the rest in swap.
  fake_llm_process 2900000 20670662 300000
  expect_ok cmd_doctor
  assert_contains "$OUT" "[ok]       Model server memory: 24.4 GB (0.3 GB of it in swap) for a 21.2 GB model file."
  # A few MB in swap are not worth a word.
  fake_llm_process 2900000 20670662 20000
  expect_ok cmd_doctor
  assert_contains "$OUT" "[ok]       Model server memory: 24.2 GB for a 21.2 GB model file."
}

test_doctor_memory_check_keeps_quiet_when_it_cannot_tell() {
  local model
  doctor_ready
  env_set LLM_CTX 4096
  env_set LLM_CACHE_RAM_MIB 0
  model="$APP/data/models/$(env_get MODEL_FILE)"
  grow_file "$model" 2497281120
  fake_llm_process 3129552 2435440
  # Still loading the model.
  echo "running starting 0" >"$FAKE_DIR/state_llm"
  expect_ok cmd_doctor
  assert_not_contains "$OUT" "Model server memory" "the report while loading"
  assert_not_contains "$OUT" "second copy" "the report while loading"
  # A prompt cache without a limit: nothing to compare with.
  echo "running healthy 0" >"$FAKE_DIR/state_llm"
  env_set LLM_CACHE_RAM_MIB -1
  expect_ok cmd_doctor
  assert_not_contains "$OUT" "Model server memory" "the report with an unlimited prompt cache"
  # The numbers cannot be read.
  env_set LLM_CACHE_RAM_MIB 0
  rm "$MYAI_SYSROOT/proc/4242/status"
  expect_ok cmd_doctor
  assert_not_contains "$OUT" "Model server memory" "the report without /proc"
  # Stopped by the owner: no memory lines at all, not even old kills.
  oom_lines 4100 llama-server >"$FAKE_DIR/journal_kernel"
  : >"$APP/.stopped"
  expect_ok cmd_doctor
  assert_not_contains "$OUT" "memory ran out" "the report of a stopped server"
}

# ---------------------------------------------- the installer's self-test ---

selftest_ready() {
  installed
  with_model_file
  # shellcheck disable=SC2034  # read by self_test
  STACK_STARTED=1
  echo "${PLAN[DATA_DIR]}/workspace/myai-selftest.txt" >"$FAKE_DIR/task_proof"
}

test_selftest_names_out_of_memory_stops() {
  selftest_ready
  fake_llm_process 650000 2450000
  oom_lines 4100 llama-server >"$FAKE_DIR/journal_kernel"
  self_test >"$T/out" 2>&1 || fail "a failed self-test must not stop the installer"
  assert_contains "$(cat "$T/out")" "Passed: 40 tokens"
  assert_contains "$(cat "$T/out")" "The system stopped the model server 1 time(s) because the memory ran out. What to do: myai doctor"
  assert_eq "$SELFTEST_FAILED" 1
  assert_contains "$(cat "$T/out")" "A test failed."
}

test_selftest_names_a_second_copy_of_the_model() {
  selftest_ready
  PLAN[LLM_CTX]=4096
  PLAN[LLM_CACHE_RAM_MIB]=0
  grow_file "${PLAN[DATA_DIR]}/models/${PLAN[MODEL_FILE]}" 4230976032
  # Measured for the mixture of experts with repacking on.
  fake_llm_process 3557676 4142308
  self_test >"$T/out" 2>&1 || fail "a failed self-test must not stop the installer"
  assert_contains "$(cat "$T/out")" "The model server holds a second copy of the model in memory, so it can run out of memory later. What to do: myai doctor"
  assert_eq "$SELFTEST_FAILED" 1
  # The same with part of the copy in swap.
  SELFTEST_FAILED=0
  fake_llm_process 1557676 4142308 2000000
  self_test >"$T/out" 2>&1 || fail "a failed self-test must not stop the installer"
  assert_contains "$(cat "$T/out")" "The model server holds a second copy of the model in memory"
  assert_eq "$SELFTEST_FAILED" 1
  # With repacking off: nothing to say.
  SELFTEST_FAILED=0
  fake_llm_process 250056 4144356
  self_test >"$T/out" 2>&1
  assert_not_contains "$(cat "$T/out")" "memory" "the self-test"
  assert_eq "$SELFTEST_FAILED" 0
}

test_stack_start_names_out_of_memory_as_the_cause() {
  installed
  with_model_file
  echo "exited none 0" >"$FAKE_DIR/state_llm"
  echo "0 true 4 2026-11-26T03:00:00.000000000Z" >"$FAKE_DIR/facts_llm"
  expect_fail start_stack
  assert_contains "$OUT" "Stopped: The model server ran out of memory: the system stopped it 1 time(s)."
  assert_contains "$OUT" "--model fast"
}
