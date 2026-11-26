#!/usr/bin/env bash
# Static checks: syntax, shellcheck, file modes, the catalogue.
# shellcheck disable=SC2154  # AI_DIR, TESTS_DIR, T, ... come from run.sh and helpers.sh

MYAI_SCRIPTS="install.sh bin/myai bin/myai-lib.sh hostinger/post-install.sh extras/claude-local.sh"

test_lint_bash_syntax() {
  local f
  for f in $MYAI_SCRIPTS; do
    bash -n "$AI_DIR/$f" || fail "syntax error in $f"
  done
  for f in "$TESTS_DIR"/*.sh "$TESTS_DIR"/fakes/*; do
    bash -n "$f" || fail "syntax error in $f"
  done
}

test_lint_shellcheck_clean() {
  local sc=${SHELLCHECK:-}
  if [ -z "$sc" ]; then
    sc=$(PATH=$ORIG_PATH command -v shellcheck) || skip "shellcheck is not installed (pip install shellcheck-py)"
  fi
  # shellcheck disable=SC2086  # a list of file names
  (cd "$AI_DIR" && "$sc" -x $MYAI_SCRIPTS) || fail "shellcheck reported problems in the scripts"
  (cd "$TESTS_DIR" && "$sc" -x run.sh helpers.sh t_*.sh fakes/*) || fail "shellcheck reported problems in the tests"
}

test_lint_scripts_are_executable() {
  local f
  for f in install.sh bin/myai hostinger/post-install.sh extras/claude-local.sh; do
    [ -x "$AI_DIR/$f" ] || fail "$f is not executable"
    assert_eq "$(head -c 2 "$AI_DIR/$f")" "#!" "first two bytes of $f"
  done
}

test_lint_strict_mode_and_guarded_main() {
  local f
  for f in install.sh bin/myai; do
    grep -qx 'set -euo pipefail' "$AI_DIR/$f" || fail "$f does not set -euo pipefail"
    # main and exit on one line: bash never reads past it, even when the
    # file is replaced while it runs (myai update).
    # shellcheck disable=SC2016  # the expected text, not to be expanded
    assert_eq "$(tail -n 1 "$AI_DIR/$f")" \
      'if [ "${MYAI_SOURCE_ONLY:-0}" != 1 ]; then main "$@"; exit $?; fi' "last line of $f"
  done
}

test_lint_post_install_fits_hostinger_limit() {
  local size
  size=$(wc -c <"$AI_DIR/hostinger/post-install.sh")
  [ "$size" -lt 49152 ] || fail "hostinger/post-install.sh is $size bytes (limit 48 KB)"
}

test_lint_catalogue_is_well_formed() {
  local ids line n=0
  load_installer
  ids=$(catalog_ids | paste -sd' ' -)
  assert_eq "$ids" "hauhau heretic huihui classic fast" "catalogue ids, in order"
  while IFS= read -r line; do
    n=$((n + 1))
    [[ $line =~ ^[a-z0-9]+\|[0-9]+\|[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\|[A-Za-z0-9._-]+\.gguf\|[0-9]+\|.+$ ]] ||
      fail "catalogue line $n is malformed: $line"
  done < <(catalog_lines)
  assert_eq "$n" 5 "number of catalogue entries"
  assert_eq "$(catalog_field hauhau 5)" 21166758016 "size of hauhau"
  assert_eq "$(catalog_field fast 4)" "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf" "file of fast"
  assert_eq "$(catalog_field classic 2)" 28 "memory for classic"
  expect_fail catalog_field nosuchmodel 1
}

test_lint_installer_help_needs_nothing() {
  local out
  out=$(PATH=/usr/bin:/bin bash "$AI_DIR/install.sh" --help)
  assert_contains "$out" "--dry-run"
  assert_contains "$out" "--skip-model-download"
  # A real admin e-mail leaves the machine as the certificate contact: say so.
  assert_contains "$out" "HTTPS certificate at Let's Encrypt and ZeroSSL"
  out=$(PATH=/usr/bin:/bin bash "$AI_DIR/bin/myai" help)
  assert_contains "$out" "myai doctor"
  assert_contains "$out" "myai uninstall"
}
