#!/usr/bin/env bash
# The model download: continuing, checking size, file type and checksum.
# A small local web server stands in for Hugging Face.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and install.sh

REPO="someone/Some-Model-GGUF"
FILE="some-model.Q4_K_M.gguf"

# hf_site [CONTENT_FILE]: build the fake site in $T/hf and start the server.
# Sets SIZE and SHA of the model file it serves.
hf_site() {
  local dir="$T/hf/$REPO/resolve/main" api="$T/hf/api/models/$REPO/tree"
  mkdir -p "$dir" "$api" "$T/models"
  if [ -n "${1:-}" ]; then
    cp "$1" "$dir/$FILE"
  else
    {
      printf 'GGUF'
      head -c 300000 /dev/urandom
    } >"$dir/$FILE"
  fi
  SIZE=$(stat -c %s "$dir/$FILE")
  SHA=$(sha256sum "$dir/$FILE" | awk '{ print $1 }')
  hf_listing "$SHA"
  start_fileserver "$T/hf"
}

# hf_listing SHA: what the Hugging Face API would list for the repository.
hf_listing() {
  cat >"$T/hf/api/models/$REPO/tree/main" <<JSON
[{"type":"file","oid":"aaaa","size":1519,"path":".gitattributes"},
 {"type":"file","oid":"bbbb","size":$SIZE,"lfs":{"oid":"$1","size":$SIZE,"pointerSize":135},"path":"$FILE"},
 {"type":"file","oid":"cccc","size":4000,"path":"README.md"}]
JSON
}

served_file() { printf '%s\n' "$T/hf/$REPO/resolve/main/$FILE"; }

test_download_fresh_file_is_checked_and_renamed() {
  hf_site
  load_installer
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_file "$T/models/$FILE"
  assert_no_file "$T/models/$FILE.part"
  cmp -s "$(served_file)" "$T/models/$FILE" || fail "the downloaded file differs from the original"
  assert_mode "$T/models/$FILE" 644
  assert_contains "$OUT" "Checksum correct"
  assert_contains "$(cat "$T/http.log")" "GET /$REPO/resolve/main/$FILE range=-" "a plain request for the file"
  assert_contains "$(cat "$T/http.log")" "GET /api/models/$REPO/tree/main" "the checksum was looked up"
}

test_download_continues_a_partial_file() {
  hf_site
  load_installer
  head -c 100000 "$(served_file)" >"$T/models/$FILE.part"
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  cmp -s "$(served_file)" "$T/models/$FILE" || fail "the continued file differs from the original"
  assert_contains "$(cat "$T/http.log")" "range=bytes=100000-" "only the missing part was requested"
  assert_not_contains "$(cat "$T/http.log")" "$FILE range=-" "requests"
  assert_contains "$OUT" "Continuing an earlier download"
  assert_no_file "$T/models/$FILE.part"
}

test_download_retries_by_itself_when_the_connection_drops() {
  hf_site
  load_installer
  : >"$(served_file).flaky" # the first transfer breaks off half-way
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  cmp -s "$(served_file)" "$T/models/$FILE" || fail "the file differs after a broken connection"
  assert_contains "$OUT" "it continues where it stopped"
  assert_contains "$(cat "$T/http.log")" "range=bytes=150002-" "the second try asked for the second half"
}

test_download_prints_progress_lines_and_leaves_no_process_behind() {
  local before after
  hf_site
  load_installer
  : >"$(served_file).slow"
  before=$(pgrep -c -f "sleep $MYAI_PROGRESS_SECS" || true)
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "downloaded 0.0 GB of 0.0 GB (" "a progress line"
  cmp -s "$(served_file)" "$T/models/$FILE" || fail "the file differs"
  # The helper that prints the progress lines must be gone.
  sleep 1.2
  after=$(pgrep -c -f "sleep $MYAI_PROGRESS_SECS" || true)
  [ "$after" -le "$before" ] || fail "a progress helper is still running"
}

test_download_a_complete_part_file_is_not_fetched_again() {
  hf_site
  load_installer
  cp "$(served_file)" "$T/models/$FILE.part"
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_file "$T/models/$FILE"
  assert_not_contains "$(cat "$T/http.log")" "/resolve/main/" "requests"
}

test_download_skips_a_file_that_is_already_complete() {
  hf_site
  load_installer
  cp "$(served_file)" "$T/models/$FILE"
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "already here"
  assert_eq "$(cat "$T/http.log")" "" "no request at all"
}

test_download_size_mismatch_is_fatal() {
  hf_site
  # Hugging Face does not say the size (its API is down), so only the
  # downloaded file itself can be checked.
  rm "$T/hf/api/models/$REPO/tree/main"
  load_installer
  expect_fail download_model "$REPO" "$FILE" $((SIZE + 1)) "$T/models"
  assert_contains "$OUT" "wrong size ($SIZE bytes, expected $((SIZE + 1)))"
  assert_contains "$OUT" "Run the same command again"
  assert_no_file "$T/models/$FILE"
  assert_no_file "$T/models/$FILE.part"
}

test_download_replaced_model_stops_before_downloading() {
  hf_site
  load_installer
  # A partial download of the old upload, from an earlier try.
  head -c 1000 "$(served_file)" >"$T/models/$FILE.part"
  # The catalogue has the size of the file that the publisher has replaced.
  expect_fail download_model "$REPO" "$FILE" $((SIZE - 1000)) "$T/models"
  assert_contains "$OUT" "The publisher has replaced the model file $FILE: Hugging Face lists it with $SIZE bytes, the model list of myai with $((SIZE - 1000))."
  assert_contains "$OUT" "nothing was downloaded"
  # The installer's advice: myai does not exist yet during the first installation.
  assert_contains "$OUT" "bash -s -- --model <id>"
  assert_contains "$OUT" "hauhau heretic huihui classic fast"
  assert_not_contains "$OUT" "myai model" "the installer's advice"
  assert_not_contains "$(cat "$T/http.log")" "/resolve/main/" "requests for the file"
  assert_no_file "$T/models/$FILE.part"
  assert_no_file "$T/models/$FILE"
}

test_download_oversized_part_file_is_started_again() {
  hf_site
  load_installer
  head -c $((SIZE + 5000)) /dev/urandom >"$T/models/$FILE.part"
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  cmp -s "$(served_file)" "$T/models/$FILE" || fail "the file differs"
  assert_contains "$(cat "$T/http.log")" "$FILE range=-" "downloaded from the start"
}

test_download_bad_magic_is_fatal() {
  # Something in between answers with a web page of exactly the right size.
  printf '<html><body>Access denied by a proxy</body></html>' >"$T/page"
  hf_site "$T/page"
  load_installer
  expect_fail download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "not a model file"
  assert_no_file "$T/models/$FILE"
  assert_no_file "$T/models/$FILE.part"
}

test_download_hash_mismatch_is_fatal() {
  hf_site
  hf_listing "0000000000000000000000000000000000000000000000000000000000000000"
  load_installer
  expect_fail download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "checksum does not match"
  assert_no_file "$T/models/$FILE"
  assert_no_file "$T/models/$FILE.part"
}

test_download_hash_unavailable_only_warns() {
  hf_site
  rm "$T/hf/api/models/$REPO/tree/main" # the API answers 404
  load_installer
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "Warning: Hugging Face did not report a checksum"
  assert_file "$T/models/$FILE"
  cmp -s "$(served_file)" "$T/models/$FILE" || fail "the file differs"
}

test_download_hash_missing_from_listing_only_warns() {
  hf_site
  # The listing knows the file but has no checksum for it.
  printf '[{"type":"file","size":%s,"path":"%s"}]' "$SIZE" "$FILE" >"$T/hf/api/models/$REPO/tree/main"
  load_installer
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "did not report a checksum"
  assert_file "$T/models/$FILE"
}

test_download_garbage_from_the_api_only_warns() {
  hf_site
  printf '{"error":"Too many requests"}' >"$T/hf/api/models/$REPO/tree/main"
  load_installer
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "did not report a checksum"
  printf 'not json at all' >"$T/hf/api/models/$REPO/tree/main"
  rm "$T/models/$FILE"
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_file "$T/models/$FILE"
}

test_download_missing_file_stops_at_once() {
  hf_site
  rm "$(served_file)"
  load_installer
  expect_fail download_model "$REPO" "$FILE" 300004 "$T/models"
  assert_contains "$OUT" "the server answered 404"
  # From the installer: the myai command does not exist yet at this point.
  assert_contains "$OUT" "bash -s -- --model <id>"
  assert_not_contains "$OUT" "myai model" "the installer's advice"
  assert_eq "$(grep -c "/resolve/main/" "$T/http.log")" 1 "number of tries for a missing file"
}

test_download_gives_up_after_repeated_failures_and_keeps_the_part() {
  load_installer
  mkdir -p "$T/models"
  export MYAI_HF_BASE="http://127.0.0.1:1" # nothing listens there
  printf 'GGUFpartial' >"$T/models/$FILE.part"
  expect_fail download_model "$REPO" "$FILE" 5000 "$T/models"
  assert_contains "$OUT" "failed 4 times in a row"
  assert_contains "$OUT" "Run the same command again to continue"
  assert_file "$T/models/$FILE.part"
  assert_eq "$(cat "$T/models/$FILE.part")" "GGUFpartial" "the partial file is untouched"
}

test_download_model_outside_the_catalogue_uses_the_reported_size() {
  hf_site
  load_installer
  expect_ok download_model "$REPO" "$FILE" "" "$T/models"
  assert_file "$T/models/$FILE"
  assert_contains "$OUT" "Checksum correct"
  # ... and it is recognised as complete the next time.
  : >"$T/http.log"
  expect_ok download_model "$REPO" "$FILE" "" "$T/models"
  assert_not_contains "$(cat "$T/http.log")" "/resolve/main/" "requests on the second run"
}

test_download_file_in_a_subfolder() {
  local sub="$T/hf/$REPO/resolve/main/Q4"
  hf_site
  mkdir -p "$sub"
  cp "$(served_file)" "$sub/inner.gguf"
  # The listing of the subfolder is a second API address.
  rm "$T/hf/api/models/$REPO/tree/main"
  mkdir -p "$T/hf/api/models/$REPO/tree/main"
  printf '[{"type":"file","size":%s,"lfs":{"oid":"%s","size":%s},"path":"Q4/inner.gguf"}]' "$SIZE" "$SHA" "$SIZE" \
    >"$T/hf/api/models/$REPO/tree/main/Q4"
  load_installer
  expect_ok download_model "$REPO" "Q4/inner.gguf" "" "$T/models"
  assert_file "$T/models/inner.gguf"
  assert_contains "$OUT" "Checksum correct"
}

test_download_dry_run_fetches_nothing() {
  hf_site
  load_installer --dry-run
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  assert_contains "$OUT" "would download $MYAI_HF_BASE/$REPO/resolve/main/$FILE"
  assert_no_file "$T/models/$FILE"
  assert_no_file "$T/models/$FILE.part"
  assert_eq "$(cat "$T/http.log")" "" "no request in a dry run"
}

test_download_replaces_a_damaged_final_file() {
  hf_site
  load_installer
  printf 'not a model' >"$T/models/$FILE"
  expect_ok download_model "$REPO" "$FILE" "$SIZE" "$T/models"
  cmp -s "$(served_file)" "$T/models/$FILE" || fail "the damaged file was not replaced"
}

test_download_gguf_magic_check() {
  load_installer
  printf 'GGUF\x03\x00\x00\x00rest' >"$T/good"
  printf 'GGU' >"$T/short"
  printf '\x00\x00\x00\x00GGUF' >"$T/zeros"
  : >"$T/empty"
  file_is_gguf "$T/good" || fail "a file starting with GGUF"
  for f in short zeros empty missing; do
    if file_is_gguf "$T/$f" 2>/dev/null; then fail "$f must not count as a GGUF file"; fi
  done
}
