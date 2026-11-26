#!/usr/bin/env bash
# Tests for install.sh and bin/myai. Nothing here touches the real system:
# every test runs in its own temporary directory, with fake commands (docker,
# ufw, systemctl, apt-get, sshd, dig, ...) in front of PATH and a fake "/".
#
#   tests/installer/run.sh            run everything
#   tests/installer/run.sh download   run the tests whose name contains "download"
#   tests/installer/run.sh -v ...     also show the output of passing tests
#
# Needs: bash, jq, curl, tar, openssl, python3. shellcheck is used when it is
# installed (pip install shellcheck-py provides it); set SHELLCHECK to its path.

set -uo pipefail

TESTS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
AI_DIR=$(cd "$TESTS_DIR/../.." && pwd -P)
ORIG_PATH=$PATH
VERBOSE=0
PATTERN=""
for arg in "$@"; do
  case "$arg" in
    -v) VERBOSE=1 ;;
    *) PATTERN=$arg ;;
  esac
done

for tool in jq curl tar openssl python3 awk sed; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "The tests need the program '$tool'." >&2
    exit 2
  }
done
REAL_CURL=$(command -v curl)
REAL_TAR=$(command -v tar)
REAL_DF=$(command -v df)
REAL_UNAME=$(command -v uname)
REAL_ID=$(command -v id)
REAL_INSTALL=$(command -v install)
REAL_DOCKER=$(command -v docker || true)
export REAL_CURL REAL_TAR REAL_DF REAL_UNAME REAL_ID REAL_INSTALL REAL_DOCKER TESTS_DIR AI_DIR ORIG_PATH

REAL_OPENSSL=$(command -v openssl)
export REAL_OPENSSL

TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/myai-tests.XXXXXX")
export TEST_TMP
trap 'rm -rf "$TEST_TMP"' EXIT

# During a test, PATH holds only the fake commands and this short list of
# harmless real tools. Whatever else is installed here (docker, systemctl,
# apt-get, ufw, ...) cannot be reached, even if a fake were missing.
#
# Some systems put a wrapper in front of a tool (a guard around rm, for
# example) that looks for the real tool further down PATH. Such a wrapper
# stays in front: its directory is kept on PATH when it holds nothing else,
# and the real tool is linked behind it.
SYSBIN="$TEST_TMP/sysbin"
SYSPATH=""
mkdir -p "$SYSBIN"
for tool in bash sh awk sed grep cat head tail tr cut sort uniq wc stat mktemp mkdir rmdir rm cp mv chmod \
  chown ln ls dirname basename readlink date sleep env paste od sha256sum jq python3 flock touch tee find \
  cmp diff pgrep gzip nproc true false; do
  mapfile -t found < <(type -aP "$tool" 2>/dev/null)
  [ ${#found[@]} -gt 0 ] || continue
  dir=$(dirname "${found[0]}")
  if [ ${#found[@]} -gt 1 ] && [ "$(find "$dir" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 ]; then
    case ":$SYSPATH:" in
      *":$dir:"*) ;;
      *) SYSPATH="${SYSPATH:+$SYSPATH:}$dir" ;;
    esac
    ln -s "${found[1]}" "$SYSBIN/$tool"
  else
    ln -s "${found[0]}" "$SYSBIN/$tool"
  fi
done
SYSPATH="${SYSPATH:+$SYSPATH:}$SYSBIN"
export SYSBIN SYSPATH

# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for file in "$TESTS_DIR"/t_*.sh; do
  # shellcheck source=/dev/null
  . "$file"
done

passed=0
failed=0
skipped=0
failed_names=()
for name in $(declare -F | awk '{ print $3 }' | grep '^test_' | sort); do
  if [ -n "$PATTERN" ] && [[ $name != *"$PATTERN"* ]]; then continue; fi
  log="$TEST_TMP/$name.log"
  (
    set -euo pipefail
    sandbox
    "$name"
  ) >"$log" 2>&1
  rc=$?
  case "$rc" in
    0)
      passed=$((passed + 1))
      printf 'ok    %s\n' "$name"
      if [ "$VERBOSE" = 1 ]; then sed 's/^/        /' "$log"; fi
      ;;
    77)
      skipped=$((skipped + 1))
      printf 'skip  %s  (%s)\n' "$name" "$(tail -n 1 "$log")"
      ;;
    *)
      failed=$((failed + 1))
      failed_names+=("$name")
      printf 'FAIL  %s  (exit %s)\n' "$name" "$rc"
      sed 's/^/        /' "$log"
      ;;
  esac
done

printf '\n%d passed, %d failed, %d skipped\n' "$passed" "$failed" "$skipped"
if [ "$failed" -gt 0 ]; then
  printf 'Failed: %s\n' "${failed_names[*]}"
  exit 1
fi
