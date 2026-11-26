#!/usr/bin/env bash
# Hardware figures: thread counts, CPU readings, formatting.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and install.sh

test_hw_thread_arithmetic() {
  load_installer
  # max(2, round(0.75 * vCPU)), never more than there are
  assert_eq "$(compute_threads 2)" 2 "2 vCPU"
  assert_eq "$(compute_threads 4)" 3 "4 vCPU"
  assert_eq "$(compute_threads 8)" 6 "8 vCPU"
  assert_eq "$(compute_threads 16)" 12 "16 vCPU"
  assert_eq "$(compute_threads 1)" 1 "1 vCPU"
  assert_eq "$(compute_threads 3)" 2 "3 vCPU"
  assert_eq "$(compute_threads 6)" 5 "6 vCPU (4.5 rounds up)"
  assert_eq "$(compute_threads 12)" 9 "12 vCPU"
  assert_eq "$(compute_threads 32)" 24 "32 vCPU"
}

test_hw_threads_in_the_settings_for_each_size() {
  local n want
  for n in 2 4 8 16; do
    case "$n" in
      2) want=2 ;; 4) want=3 ;; 8) want=6 ;; 16) want=12 ;;
    esac
    (
      APP="$T/app$n"
      fake_machine "$n" 32862000
      load_installer
      check_cpu >/dev/null 2>&1
      select_model
      collect_answers
      mkdir -p "$APP"
      write_config >/dev/null 2>&1
      assert_eq "$(env_get LLM_THREADS)" "$want" "LLM_THREADS with $n vCPUs"
      assert_eq "$(env_get LLM_THREADS_BATCH)" "$n" "LLM_THREADS_BATCH with $n vCPUs"
    )
  done
}

test_hw_counts_vcpus_and_finds_avx2() {
  load_installer
  assert_eq "$(hw_vcpus)" 8
  hw_has_avx2 || fail "avx2 is in the fake cpuinfo"
  fake_machine 3 8000000 "fpu sse sse2 avx"
  assert_eq "$(hw_vcpus)" 3
  if hw_has_avx2; then fail "this fake CPU has no avx2"; fi
  # avx2 must be a whole word, not part of another flag
  fake_machine 2 8000000 "fpu avx avx2_fake xavx2"
  if hw_has_avx2; then fail "avx2_fake is not avx2"; fi
  assert_eq "$(hw_ram_kb)" 8000000
}

test_hw_cpu_steal_and_busy_percentages() {
  load_installer
  # total steal idle: 1000 ticks passed, 250 stolen, 500 idle -> 25% steal, 25% busy
  assert_eq "$(cpu_percentages "10000 100 8000" "11000 350 8500")" "25.0 25.0"
  assert_eq "$(cpu_percentages "10000 100 8000" "11000 100 9000")" "0.0 0.0" "an idle second"
  assert_eq "$(cpu_percentages "10000 100 8000" "10000 100 8000")" "0.0 0.0" "no time passed"
  # the fake /proc/stat: user 1000 nice 0 system 500 idle 8000 iowait 100 irq 0 softirq 50 steal 20
  assert_eq "$(cpu_sample)" "9670 20 8100"
}

test_hw_formatting() {
  load_installer
  assert_eq "$(fmt_gb 21166758016)" "21.2 GB"
  assert_eq "$(fmt_gb 0)" "0.0 GB"
  assert_eq "$(fmt_ram 32862000)" "31.3 GB"
  assert_eq "$(fmt_duration 45)" "45 s"
  assert_eq "$(fmt_duration 135)" "2 min 15 s"
  assert_eq "$(fmt_duration 1800)" "30 min 00 s"
  assert_eq "$(quote_cmd ufw allow 22/tcp)" "ufw allow 22/tcp"
  assert_eq "$(quote_cmd echo "two words" "it's")" "echo 'two words' 'it'\\''s'"
}

test_hw_memory_fit_rule() {
  load_installer
  ram_fits 32862000 30 || fail "31.3 GiB fits a 30 GB model"
  ram_fits 29300000 30 || fail "a server sold as 30 GB fits a 30 GB model"
  if ram_fits 28700000 30; then fail "27.4 GiB does not fit a 30 GB model"; fi
  ram_fits 5900000 6 || fail "a 6 GB server fits the small model"
  if ram_fits 3900000 6; then fail "a 4 GB server does not"; fi
}
