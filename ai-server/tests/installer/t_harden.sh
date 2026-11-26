#!/usr/bin/env bash
# Hardening: firewall, swap, fail2ban, automatic updates, Docker log rotation.
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and install.sh

count_calls() { grep -cF -- "$1" "$FAKE_LOG" || true; }

# ------------------------------------------------------------- firewall ---

test_harden_firewall_uses_the_real_ssh_port() {
  printf 'port 2222\naddressfamily any\nlistenaddress 0.0.0.0\npermitrootlogin yes\npasswordauthentication yes\n' \
    >"$FAKE_DIR/sshd_T"
  load_installer
  expect_ok harden_firewall
  assert_called "sshd -T"
  assert_called "ufw allow 2222/tcp"
  assert_not_called "ufw allow 22/tcp"
  assert_called "ufw allow 80/tcp"
  assert_called "ufw allow 443/tcp"
  assert_called "ufw allow 443/udp"
  assert_called "ufw default deny incoming"
  assert_called "ufw --force enable"
  # SSH is allowed before anything is denied or switched on.
  assert_call_order "ufw allow 2222/tcp" "ufw default deny incoming"
  assert_call_order "ufw allow 443/udp" "ufw default deny incoming"
  assert_call_order "ufw default deny incoming" "ufw --force enable"
  assert_contains "$OUT" "SSH (2222), 80 and 443"
}

test_harden_firewall_allows_every_ssh_port() {
  printf 'port 22\nport 2222\n' >"$FAKE_DIR/sshd_T"
  load_installer
  expect_ok harden_firewall
  assert_called "ufw allow 22/tcp"
  assert_called "ufw allow 2222/tcp"
}

test_harden_firewall_also_allows_the_port_of_this_session() {
  load_installer
  export SSH_CONNECTION="198.51.100.1 50000 203.0.113.10 2200"
  expect_ok harden_firewall
  assert_called "ufw allow 22/tcp"
  assert_called "ufw allow 2200/tcp"
}

test_harden_firewall_keeps_22_open_when_the_port_is_unknown() {
  rm "$FAKE_DIR/sshd_T"
  load_installer
  expect_ok harden_firewall
  assert_called "ufw allow 22/tcp"
  assert_contains "$OUT" "keeping port 22 open"
}

test_harden_firewall_is_not_switched_on_after_a_failed_rule() {
  : >"$FAKE_DIR/ufw_fail_allow"
  load_installer
  expect_fail harden_firewall
  assert_not_called "ufw --force enable"
  assert_not_called "ufw default deny incoming"
  # ... and the installer goes on, remembering what is missing.
  HARDEN_FAILED=()
  harden_item "the firewall" harden_firewall >"$T/out" 2>&1
  assert_eq "${HARDEN_FAILED[*]}" "the firewall"
  assert_contains "$(cat "$T/out")" "The installation continues without it"
  assert_not_called "ufw --force enable"
}

test_harden_firewall_installs_ufw_first_when_missing() {
  rm "$T/bin/ufw"
  load_installer
  # The fake apt-get installs nothing, so the function cannot finish;
  # what matters is that it asked for the package and enabled nothing.
  expect_fail harden_firewall
  assert_called "install -y --no-install-recommends ufw"
}

test_harden_never_touches_the_ssh_configuration() {
  mkdir -p "$MYAI_SYSROOT/etc/ssh"
  echo "PasswordAuthentication yes" >"$MYAI_SYSROOT/etc/ssh/sshd_config"
  cp -p "$MYAI_SYSROOT/etc/ssh/sshd_config" "$T/sshd_config.before"
  : >"$MYAI_SYSROOT/etc/fstab"
  load_installer
  expect_ok harden
  cmp -s "$T/sshd_config.before" "$MYAI_SYSROOT/etc/ssh/sshd_config" || fail "sshd_config was changed"
  assert_eq "$(grep '^sshd ' "$FAKE_LOG" | sort -u)" "sshd -T" "the only way sshd is called"
  assert_not_called "systemctl restart ssh"
  assert_not_called "systemctl reload ssh"
  assert_contains "$OUT" "Not changed: how you log in with SSH"
}

# ----------------------------------------------------------------- swap ---

test_harden_swap_is_created_when_there_is_none() {
  : >"$MYAI_SYSROOT/etc/fstab"
  load_installer
  expect_ok harden_swap
  assert_called "fallocate -l 8G $MYAI_SYSROOT/swapfile"
  assert_called "mkswap $MYAI_SYSROOT/swapfile"
  assert_called "swapon $MYAI_SYSROOT/swapfile"
  assert_called "sysctl -q -w vm.swappiness=10"
  assert_mode "$MYAI_SYSROOT/swapfile" 600
  assert_eq "$(cat "$MYAI_SYSROOT/etc/fstab")" "/swapfile none swap sw 0 0"
  assert_contains "$(cat "$MYAI_SYSROOT/etc/sysctl.d/90-myai.conf")" "vm.swappiness = 10"
  # A second run must not create it again or add a second fstab line.
  : >"$FAKE_LOG"
  expect_ok harden_swap
  assert_not_called "fallocate"
  assert_not_called "mkswap"
  assert_eq "$(grep -c swap "$MYAI_SYSROOT/etc/fstab")" 1 "fstab lines"
}

test_harden_swap_is_left_alone_when_there_is_some() {
  echo "/dev/vda2 partition 4G 0B -2" >"$FAKE_DIR/swapon_show"
  load_installer
  expect_ok harden_swap
  assert_not_called "fallocate"
  assert_not_called "mkswap"
  assert_no_file "$MYAI_SYSROOT/swapfile"
  assert_called "sysctl -q -w vm.swappiness=10"
  assert_contains "$OUT" "already there"
}

test_harden_swap_failure_removes_the_file() {
  : >"$MYAI_SYSROOT/etc/fstab"
  echo 255 >"$FAKE_DIR/rc_swapon" # a container, or a file system that cannot hold swap
  load_installer
  expect_fail harden_swap
  assert_no_file "$MYAI_SYSROOT/swapfile"
  assert_eq "$(cat "$MYAI_SYSROOT/etc/fstab")" "" "fstab is untouched"
}

# ---------------------------------------------- fail2ban and auto updates ---

test_harden_fail2ban_watches_the_real_ssh_port() {
  printf 'port 2222\n' >"$FAKE_DIR/sshd_T"
  load_installer
  expect_ok harden_fail2ban
  local jail="$MYAI_SYSROOT/etc/fail2ban/jail.d/myai-sshd.local"
  assert_called "install -y --no-install-recommends fail2ban python3-systemd"
  assert_contains "$(cat "$jail")" "[sshd]"
  assert_contains "$(cat "$jail")" "enabled = true"
  assert_contains "$(cat "$jail")" "port = 2222"
  assert_contains "$(cat "$jail")" "backend = systemd"
  assert_called "systemctl enable fail2ban"
  assert_called "systemctl restart fail2ban"
  # Second run: nothing changed, so no restart.
  : >"$FAKE_LOG"
  expect_ok harden_fail2ban
  assert_not_called "systemctl restart fail2ban"
  assert_called "systemctl start fail2ban"
}

test_harden_automatic_updates_reboot_at_0430() {
  load_installer
  expect_ok harden_auto_updates
  local conf="$MYAI_SYSROOT/etc/apt/apt.conf.d/52myai-auto-updates"
  assert_called "install -y --no-install-recommends unattended-upgrades"
  assert_contains "$(cat "$conf")" 'APT::Periodic::Unattended-Upgrade "1";'
  assert_contains "$(cat "$conf")" 'Unattended-Upgrade::Automatic-Reboot "true";'
  assert_contains "$(cat "$conf")" 'Unattended-Upgrade::Automatic-Reboot-Time "04:30";'
  assert_mode "$conf" 644
}

test_harden_skipped_with_no_harden() {
  load_installer --no-harden
  expect_ok harden
  assert_contains "$OUT" "Skipped (--no-harden)"
  assert_eq "$(cat "$FAKE_LOG")" "" "no command at all"
  assert_no_file "$MYAI_SYSROOT/etc/docker/daemon.json"
}

test_harden_one_failing_item_does_not_stop_the_others() {
  : >"$MYAI_SYSROOT/etc/fstab"
  : >"$FAKE_DIR/ufw_fail_allow"
  load_installer
  harden >"$T/out" 2>&1
  assert_eq "${HARDEN_FAILED[*]}" "the firewall"
  assert_file "$MYAI_SYSROOT/etc/apt/apt.conf.d/52myai-auto-updates"
  assert_file "$MYAI_SYSROOT/etc/fail2ban/jail.d/myai-sshd.local"
  assert_file "$MYAI_SYSROOT/etc/docker/daemon.json"
}

# --------------------------------------------------- Docker log rotation ---

DAEMON_JSON_FULL='{
  "dns": ["1.1.1.1", "9.9.9.9"],
  "features": {"buildkit": true},
  "log-opts": {"labels": "prod", "max-size": "50m"},
  "default-address-pools": [{"base": "172.30.0.0/16", "size": 24}],
  "live-restore": true
}'

test_harden_docker_logs_file_is_created_when_missing() {
  local file="$MYAI_SYSROOT/etc/docker/daemon.json"
  load_installer
  expect_ok harden_docker_logs
  assert_eq "$(jq -c . "$file")" '{"log-opts":{"max-size":"10m","max-file":"3"}}'
  assert_mode "$file" 644
  assert_called "systemctl restart docker"
  assert_no_file "$file.before-myai"
}

test_harden_docker_logs_merge_keeps_existing_keys() {
  local file="$MYAI_SYSROOT/etc/docker/daemon.json"
  mkdir -p "$(dirname "$file")"
  printf '%s\n' "$DAEMON_JSON_FULL" >"$file"
  load_installer
  expect_ok harden_docker_logs
  # Everything that was there is still there, unchanged.
  assert_eq "$(jq -c 'del(."log-opts")' "$file")" "$(jq -c 'del(."log-opts")' <<<"$DAEMON_JSON_FULL")" "the other keys"
  assert_eq "$(jq -r '."log-opts".labels' "$file")" "prod" "an existing log option"
  assert_eq "$(jq -r '."log-opts"."max-size"' "$file")" "50m" "a size the owner chose is not overwritten"
  assert_eq "$(jq -r '."log-opts"."max-file"' "$file")" "3" "the missing option is added"
  assert_eq "$(jq -r 'keys_unsorted | join(",")' "$file")" "dns,features,log-opts,default-address-pools,live-restore" "key order"
  # The original is kept next to it.
  assert_eq "$(cat "$file.before-myai")" "$DAEMON_JSON_FULL"
  assert_eq "$(count_calls "systemctl restart docker")" 1 "Docker restarts"
}

test_harden_docker_logs_no_restart_when_nothing_changes() {
  local file="$MYAI_SYSROOT/etc/docker/daemon.json"
  mkdir -p "$(dirname "$file")"
  printf '{"log-driver":"json-file","log-opts":{"max-file":"5","max-size":"20m"},"dns":["1.1.1.1"]}\n' >"$file"
  cp -p "$file" "$T/before"
  load_installer
  expect_ok harden_docker_logs
  cmp -s "$T/before" "$file" || fail "the file was rewritten although nothing had to change"
  assert_not_called "systemctl restart docker"
  assert_no_file "$file.before-myai"
  assert_contains "$OUT" "already set"
}

test_harden_docker_logs_second_run_is_a_no_op() {
  load_installer
  expect_ok harden_docker_logs
  expect_ok harden_docker_logs
  assert_eq "$(count_calls "systemctl restart docker")" 1 "Docker restarts over two runs"
}

test_harden_docker_logs_leaves_a_broken_file_alone() {
  local file="$MYAI_SYSROOT/etc/docker/daemon.json" content
  mkdir -p "$(dirname "$file")"
  load_installer
  for content in '{"dns": ["1.1.1.1"' '[1, 2]' '"text"'; do
    printf '%s\n' "$content" >"$file"
    expect_ok harden_docker_logs
    assert_eq "$(cat "$file")" "$content" "the file content"
  done
  assert_not_called "systemctl restart docker"
}

test_harden_docker_logs_leaves_another_log_driver_alone() {
  local file="$MYAI_SYSROOT/etc/docker/daemon.json"
  mkdir -p "$(dirname "$file")"
  printf '{"log-driver":"journald"}\n' >"$file"
  load_installer
  expect_ok harden_docker_logs
  assert_eq "$(cat "$file")" '{"log-driver":"journald"}'
  assert_contains "$OUT" "log driver 'journald'"
  assert_not_called "systemctl restart docker"
  # The "local" driver takes the same options.
  printf '{"log-driver":"local"}\n' >"$file"
  expect_ok harden_docker_logs
  assert_eq "$(jq -c . "$file")" '{"log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}'
}

test_harden_docker_logs_dry_run_changes_nothing() {
  local file="$MYAI_SYSROOT/etc/docker/daemon.json"
  mkdir -p "$(dirname "$file")"
  printf '{"dns":["1.1.1.1"]}\n' >"$file"
  load_installer --dry-run
  expect_ok harden_docker_logs
  assert_eq "$(cat "$file")" '{"dns":["1.1.1.1"]}'
  assert_contains "$OUT" "would write: $file"
  assert_contains "$OUT" "would run: systemctl restart docker"
  assert_eq "$(cat "$FAKE_LOG")" "" "no command in a dry run"
}
