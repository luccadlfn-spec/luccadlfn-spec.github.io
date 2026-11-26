#!/usr/bin/env bash
# Which model is installed: by memory, by --model, or kept from an earlier run.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and install.sh

# RAM figures as /proc/meminfo reports them (a little less than the nominal size).
pick_for() { # RAM_KB -> model id
  (
    fake_machine 8 "$1"
    load_installer
    select_model >/dev/null 2>&1
    printf '%s\n' "$MODEL_ID"
  )
}

test_model_by_memory() {
  assert_eq "$(pick_for 32862000)" hauhau "a 32 GiB server"
  assert_eq "$(pick_for 30650000)" hauhau "a server sold as 32 GB, counted in decimal"
  assert_eq "$(pick_for 30790000)" hauhau "a 30 GiB server"
  assert_eq "$(pick_for 28700000)" classic "a 28 GiB server: the first entry that fits"
  assert_eq "$(pick_for 24500000)" fast "a 24 GiB server"
  assert_eq "$(pick_for 16200000)" fast "a 16 GiB server"
  assert_eq "$(pick_for 8000000)" fast "an 8 GiB server"
  assert_eq "$(pick_for 5900000)" fast "a 6 GiB server"
}

test_model_fills_in_file_and_size() {
  load_installer
  select_model
  assert_eq "$MODEL_ID" hauhau
  assert_eq "$MODEL_FILE" "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
  assert_eq "$MODEL_BYTES" 21166758016
}

test_model_too_little_memory_stops() {
  fake_machine 2 3900000
  load_installer
  expect_fail select_model
  assert_contains "$OUT" "The smallest model needs about 6 GB"
}

test_model_option_wins_over_memory() {
  fake_machine 8 16200000
  load_installer --model heretic
  select_model >"$T/out" 2>&1
  assert_eq "$MODEL_ID" heretic
  assert_eq "$MODEL_FILE" "Qwen3.6-35B-A3B-uncensored-heretic-Q4_K_M.gguf"
  assert_eq "$MODEL_BYTES" 21233608512
  assert_contains "$(cat "$T/out")" "meant for servers with at least 30 GB" "a warning about memory"
}

test_model_option_small_model_on_a_big_server() {
  load_installer --model fast
  select_model >"$T/out" 2>&1
  assert_eq "$MODEL_ID" fast
  assert_eq "$(cat "$T/out")" "" "no warning"
}

test_model_unknown_option_stops_and_lists_choices() {
  load_installer --model gpt9
  expect_fail select_model
  assert_contains "$OUT" "There is no model called 'gpt9'"
  assert_contains "$OUT" "hauhau heretic huihui classic fast"
}

test_model_existing_settings_are_kept() {
  mkdir -p "$APP"
  printf 'MODEL_ID=classic\nMODEL_FILE=Huihui-Qwen3-30B-A3B-Instruct-2507-abliterated.Q4_K_M.gguf\n' >"$APP/.env"
  load_installer
  select_model
  assert_eq "$MODEL_ID" classic "not switched to hauhau although the memory would allow it"
  assert_eq "$MODEL_BYTES" 18556687008
  (
    load_installer --model fast
    select_model
    assert_eq "$MODEL_ID" fast "--model still wins"
  )
}

test_model_existing_custom_file_is_kept() {
  mkdir -p "$APP"
  printf 'MODEL_ID=custom\nMODEL_FILE=My-Own.gguf\n' >"$APP/.env"
  load_installer
  select_model
  assert_eq "$MODEL_ID" custom
  assert_eq "$MODEL_FILE" "My-Own.gguf"
  assert_eq "$MODEL_BYTES" "" "no size is known for a file that is not in the catalogue"
}

test_model_catalogue_lookup_ignores_comments_and_blank_lines() {
  load_installer
  MODELS_CONF="$T/models.conf"
  printf '# comment\n\n  # indented comment\nbig|30|o/r|big.gguf|100|Big one\nsmall|6|o/s|small.gguf|10|Small one\n' >"$MODELS_CONF"
  assert_eq "$(catalog_ids | paste -sd' ' -)" "big small"
  assert_eq "$(catalog_field small 3)" "o/s"
  assert_eq "$(catalog_field big 6)" "Big one"
  assert_eq "$(catalog_pick_for_ram 32862000)" big
  assert_eq "$(catalog_pick_for_ram 8000000)" small
  if catalog_pick_for_ram 1000000 >/dev/null; then fail "nothing fits 1 GB"; fi
}
