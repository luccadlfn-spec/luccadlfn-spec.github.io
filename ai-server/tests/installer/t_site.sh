#!/usr/bin/env bash
# Choosing the address of the server (SITE_ADDRESS), and small network helpers.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and install.sh

test_site_1_domain_option_wins() {
  echo "203.0.113.10" >"$FAKE_DIR/dig_A_chat.example.com"
  load_installer --domain Chat.Example.com
  choose_site_address >"$T/out" 2>&1
  assert_eq "$SITE_ADDRESS" "chat.example.com" "the name is lower-cased"
  assert_eq "$CADDY_TLS_LINE" ""
  assert_eq "$(cat "$T/out")" "" "no warning when DNS is right"
}

test_site_1_domain_that_does_not_point_here_warns() {
  echo "198.51.100.7" >"$FAKE_DIR/dig_A_chat.example.com"
  load_installer --domain chat.example.com
  choose_site_address >"$T/out" 2>&1
  assert_eq "$SITE_ADDRESS" "chat.example.com"
  assert_contains "$(cat "$T/out")" "does not point to this server yet"
  assert_contains "$(cat "$T/out")" "203.0.113.10"
}

test_site_2_machine_name_when_public_dns_points_here() {
  load_installer
  choose_site_address
  assert_eq "$SITE_ADDRESS" "srv123456.hstgr.cloud"
  assert_eq "$CADDY_TLS_LINE" ""
  assert_called "dig +short +time=3 +tries=2 A srv123456.hstgr.cloud @1.1.1.1"
}

test_site_3_sslip_when_the_name_is_not_in_public_dns() {
  rm "$FAKE_DIR/dig_A_srv123456.hstgr.cloud"
  load_installer
  choose_site_address
  assert_eq "$SITE_ADDRESS" "203-0-113-10.sslip.io"
  assert_eq "$CADDY_TLS_LINE" ""
}

test_site_3_sslip_when_the_name_points_elsewhere() {
  echo "198.51.100.99" >"$FAKE_DIR/dig_A_srv123456.hstgr.cloud"
  load_installer
  choose_site_address
  assert_eq "$SITE_ADDRESS" "203-0-113-10.sslip.io"
}

test_site_3_sslip_when_the_machine_has_no_full_name() {
  echo "myserver" >"$FAKE_DIR/fqdn"
  echo "203.0.113.10" >"$FAKE_DIR/dig_A_myserver"
  load_installer
  choose_site_address
  assert_eq "$SITE_ADDRESS" "203-0-113-10.sslip.io" "a name without a dot is not used"
}

test_site_4_localhost_without_a_public_address() {
  echo "10.0.0.5" >"$FAKE_DIR/ip_src"
  load_installer
  choose_site_address
  assert_eq "$SITE_ADDRESS" "localhost"
  assert_eq "$CADDY_TLS_LINE" "tls internal"
}

test_site_public_address_behind_nat_is_found_through_dns() {
  echo "10.0.0.5" >"$FAKE_DIR/ip_src"
  echo '"198.51.100.20"' >"$FAKE_DIR/dig_whoami"
  rm "$FAKE_DIR/dig_A_srv123456.hstgr.cloud"
  load_installer
  choose_site_address
  assert_eq "$PUBLIC_IPV4" "198.51.100.20"
  assert_eq "$SITE_ADDRESS" "198-51-100-20.sslip.io"
}

test_site_public_address_last_resort_is_https() {
  rm "$FAKE_DIR/ip_src"
  echo "198.51.100.30" >"$FAKE_DIR/curl_myip"
  load_installer
  assert_eq "$(detect_public_ipv4)" "198.51.100.30"
}

test_site_tls_internal_option() {
  load_installer --tls-internal
  choose_site_address
  assert_eq "$SITE_ADDRESS" "srv123456.hstgr.cloud"
  assert_eq "$CADDY_TLS_LINE" "tls internal"
}

test_site_localhost_or_bare_ip_as_domain_gets_self_signed() {
  load_installer --domain localhost
  choose_site_address
  assert_eq "$CADDY_TLS_LINE" "tls internal"
  (
    load_installer --domain 203.0.113.10
    choose_site_address
    assert_eq "$SITE_ADDRESS" "203.0.113.10"
    assert_eq "$CADDY_TLS_LINE" "tls internal"
  )
}

test_site_bad_domain_is_refused() {
  expect_fail load_installer --domain "https://chat.example.com/"
  assert_contains "$OUT" "not a valid address"
  expect_fail load_installer --domain "bad name"
  expect_fail load_installer --domain
  assert_contains "$OUT" "needs a value"
}

test_site_governor_follows_the_machine_name() {
  load_installer
  assert_eq "$(default_governor)" on ".hstgr.cloud machine"
  echo "box.example.net" >"$FAKE_DIR/fqdn"
  assert_eq "$(default_governor)" off "other machine"
  (
    load_installer --governor on
    assert_eq "$(default_governor)" on "--governor on"
  )
  echo "srv1.hstgr.cloud" >"$FAKE_DIR/fqdn"
  (
    load_installer --governor off
    assert_eq "$(default_governor)" off "--governor off on a Hostinger machine"
  )
  expect_fail load_installer --governor maybe
}

test_net_public_ipv4_table() {
  local ip
  load_installer
  for ip in 203.0.113.10 8.8.8.8 100.63.255.255 100.128.0.1 172.15.0.1 172.32.0.1 192.167.1.1 1.0.0.1 223.255.255.254; do
    is_public_ipv4 "$ip" || fail "$ip should count as public"
  done
  for ip in 10.0.0.1 127.0.0.1 0.0.0.0 169.254.169.254 172.16.0.1 172.31.255.255 192.168.1.1 100.64.0.1 \
    100.127.255.255 224.0.0.1 255.255.255.255 256.1.1.1 1.2.3 1.2.3.4.5 "" abc 1.2.3.x; do
    if is_public_ipv4 "$ip"; then fail "'$ip' should not count as public"; fi
  done
  # Leading zeros must not be read as octal.
  is_public_ipv4 8.08.8.09 || fail "8.08.8.09"
}

test_net_validators() {
  local v
  load_installer
  for v in chat.example.com a.b localhost srv123456.hstgr.cloud 203-0-113-10.sslip.io x; do
    valid_hostname "$v" || fail "'$v' should be a valid host name"
  done
  for v in "" "-a.b" "a.b-" "a b" "http://a.b" "a/b" "a_b.c" "a\$b"; do
    if valid_hostname "$v"; then fail "'$v' should not be a valid host name"; fi
  done
  for v in me@example.com first.last+tag@sub.example.org; do
    valid_email "$v" || fail "'$v' should be a valid e-mail"
  done
  for v in "" me me@ @example.com "me@example" "me @example.com" "me@exa mple.com" "me'@example.com"; do
    if valid_email "$v"; then fail "'$v' should not be a valid e-mail"; fi
  done
  expect_fail load_installer --admin-email nonsense
  assert_contains "$OUT" "does not look like an e-mail address"
}

test_options_can_come_from_the_environment() {
  load_installer
  ASSUME_YES=0
  DRY_RUN=0
  # As hostinger/post-install.sh passes them: no options on the command line.
  MYAI_DOMAIN=Chat.Example.com MYAI_MODEL=fast MYAI_ADMIN_EMAIL=me@example.com MYAI_GOVERNOR=off \
    MYAI_TLS_INTERNAL=1 MYAI_NO_HARDEN=1 MYAI_SKIP_MODEL_DOWNLOAD=1 MYAI_YES=1 MYAI_DRY_RUN=1 \
    MYAI_DIR="$T/elsewhere" parse_args
  assert_eq "$OPT_DOMAIN" chat.example.com
  assert_eq "$OPT_MODEL" fast
  assert_eq "$OPT_ADMIN_EMAIL" me@example.com
  assert_eq "$OPT_GOVERNOR" off
  assert_eq "$OPT_TLS_INTERNAL" 1 "MYAI_TLS_INTERNAL"
  assert_eq "$OPT_NO_HARDEN" 1 "MYAI_NO_HARDEN"
  assert_eq "$OPT_SKIP_MODEL" 1 "MYAI_SKIP_MODEL_DOWNLOAD"
  assert_eq "$ASSUME_YES" 1 "MYAI_YES"
  assert_eq "$DRY_RUN" 1 "MYAI_DRY_RUN"
  assert_eq "$APP_DIR" "$T/elsewhere" "MYAI_DIR"
  # The command line wins.
  MYAI_MODEL=fast MYAI_DIR="$T/elsewhere" parse_args --model hauhau --dir "$APP"
  assert_eq "$OPT_MODEL" hauhau
  assert_eq "$APP_DIR" "$APP"
  MYAI_GOVERNOR=maybe expect_fail parse_args
  assert_contains "$OUT" "--governor must be on or off"
}

test_net_install_dir_must_be_sane() {
  local d
  for d in / /opt relative/path "/opt/my ai" "/opt/\$x" /opt/.. /opt/../etc /opt/./myai; do
    # load_installer passes its own --dir first; the one given here comes last and wins.
    expect_fail load_installer --dir "$d"
    assert_contains "$OUT" "cannot be used with --dir"
  done
}
