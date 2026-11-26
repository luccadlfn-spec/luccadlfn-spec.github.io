#!/usr/bin/env bash
# Step 1 (checks), step 2 (packages) and step 3 (Docker).
# shellcheck disable=SC2154  # variables come from run.sh, helpers.sh and install.sh

SS_DOCKER_PROXY='LISTEN 0      4096         0.0.0.0:80        0.0.0.0:*    users:(("docker-proxy",pid=1234,fd=7))'
SS_NGINX='LISTEN 0      511          0.0.0.0:80        0.0.0.0:*    users:(("nginx",pid=812,fd=6),("nginx",pid=811,fd=6))
LISTEN 0      511             [::]:80           [::]:*    users:(("nginx",pid=812,fd=7),("nginx",pid=811,fd=7))'

test_preflight_supported_systems() {
  load_installer
  fake_os ubuntu 24.04 noble "Ubuntu 24.04.5 LTS"
  expect_ok check_os
  assert_contains "$OUT" "System: Ubuntu 24.04.5 LTS"
  assert_not_contains "$OUT" "Warning"
  fake_os ubuntu 22.04 jammy "Ubuntu 22.04.5 LTS"
  expect_ok check_os
  fake_os debian 12 bookworm "Debian GNU/Linux 12 (bookworm)"
  expect_ok check_os
  assert_not_contains "$OUT" "Warning"
}

test_preflight_newer_system_is_tried_with_a_warning() {
  load_installer
  fake_os ubuntu 26.04 resolute "Ubuntu 26.04 LTS"
  expect_ok check_os
  assert_contains "$OUT" "was not tested"
  fake_os debian 13 trixie "Debian GNU/Linux 13 (trixie)"
  expect_ok check_os
  assert_contains "$OUT" "was not tested"
}

test_preflight_other_systems_stop_with_advice() {
  local spec
  load_installer
  for spec in "ubuntu 20.04 focal" "debian 11 bullseye" "centos 9 x" "almalinux 9.4 x" "fedora 41 x"; do
    # shellcheck disable=SC2086  # three words on purpose
    fake_os $spec "Some System"
    expect_fail check_os
    assert_contains "$OUT" "works on Ubuntu 22.04, Ubuntu 24.04 and Debian 12"
    assert_contains "$OUT" "This server runs Some System"
  done
  rm "$MYAI_SYSROOT/etc/os-release"
  expect_fail check_os
  assert_contains "$OUT" "an unknown system"
}

test_preflight_needs_x86_64() {
  echo "aarch64" >"$FAKE_DIR/uname_m"
  load_installer
  expect_fail check_os
  assert_contains "$OUT" "x86_64"
  assert_contains "$OUT" "aarch64"
}

test_preflight_needs_root() {
  echo 1000 >"$FAKE_DIR/uid"
  load_installer
  expect_fail check_root
  assert_contains "$OUT" "must run as the user root"
  assert_contains "$OUT" "sudo -i"
}

test_preflight_stops_when_the_installer_is_already_running() {
  load_installer
  mkdir -p "$APP"
  expect_ok check_not_installing
  # The post-install script is still at work when the command is pasted too.
  hold_lock 2 "the installer"
  expect_fail check_not_installing
  assert_contains "$OUT" "The installer is already running (process "
  assert_contains "$OUT" "there is no need to start it twice"
  assert_contains "$OUT" "tail -f /root/myai-install.log"
  wait
  # A myai command does not stop the checks: step 5 waits for it.
  hold_lock 2 "myai backup"
  expect_ok check_not_installing
  wait
}

test_preflight_needs_avx2() {
  fake_machine 8 32862000 "fpu sse sse2 avx"
  load_installer
  expect_fail check_cpu
  assert_contains "$OUT" "lacks AVX2"
}

test_preflight_few_vcpus_only_warn() {
  fake_machine 2 32862000
  load_installer
  expect_ok check_cpu
  assert_contains "$OUT" "Only 2 vCPUs"
}

test_preflight_disk_space_for_model_and_the_rest() {
  : >"$MYAI_SYSROOT/swapfile" # swap exists, so no extra 8 GB
  load_installer --model fast
  select_model
  # fast is 2.7 GB; with 15 GB for the rest that is 17.7 GB.
  echo 17000000 >"$FAKE_DIR/df_avail_kb" # 17.4 GB
  expect_fail check_disk
  assert_contains "$OUT" "Not enough free disk space"
  assert_contains "$OUT" "--model fast"
  echo 18000000 >"$FAKE_DIR/df_avail_kb" # 18.4 GB
  expect_ok check_disk
  assert_contains "$OUT" "about 17.7 GB needed"
}

test_preflight_disk_counts_the_swap_file_too() {
  load_installer --model fast
  select_model
  echo 18000000 >"$FAKE_DIR/df_avail_kb"
  expect_fail check_disk # 2.7 + 15 + 8.6 GB
  (
    load_installer --model fast --no-harden
    select_model
    expect_ok check_disk
  )
}

test_preflight_disk_needs_little_when_everything_is_there() {
  local models="$APP/data/models"
  load_installer --model fast
  select_model
  mkdir -p "$models"
  : >"$MYAI_SYSROOT/swapfile"
  echo "DATA_DIR=$APP/data" >"$APP/.env"
  # The model file is complete (a sparse file of the right size).
  printf 'GGUF' >"$models/$MODEL_FILE"
  truncate_to "$models/$MODEL_FILE" "$MODEL_BYTES"
  echo 3500000 >"$FAKE_DIR/df_avail_kb"
  expect_ok check_disk
  assert_contains "$OUT" "about 3.0 GB needed"
}

# A sparse file of SIZE bytes (no disk space is used).
truncate_to() { python3 -c 'import os, sys; os.truncate(sys.argv[1], int(sys.argv[2]))' "$1" "$2"; }

test_preflight_disk_counts_only_the_rest_of_a_partial_download() {
  local models="$APP/data/models"
  load_installer --model fast --no-harden
  select_model
  mkdir -p "$models"
  printf 'GGUF' >"$models/$MODEL_FILE.part"
  truncate_to "$models/$MODEL_FILE.part" 2000000000
  echo 16000000 >"$FAKE_DIR/df_avail_kb" # 16.4 GB: enough for 0.7 GB + 15 GB
  expect_ok check_disk
  assert_contains "$OUT" "about 15.7 GB needed"
}

test_preflight_ports_free() {
  load_installer
  expect_ok check_ports
  assert_contains "$OUT" "Ports 80 and 443 are free"
}

test_preflight_ports_traefik_is_named_and_explained() {
  printf '%s\n' "$SS_DOCKER_PROXY" >"$FAKE_DIR/ss_tcp_80"
  printf '%s\n' "${SS_DOCKER_PROXY//:80 /:443 }" >"$FAKE_DIR/ss_tcp_443"
  echo "traefik-traefik-1 (traefik:v3.2)" >"$FAKE_DIR/ps_any_80"
  echo "traefik-traefik-1 (traefik:v3.2)" >"$FAKE_DIR/ps_any_443"
  load_installer
  expect_fail check_ports
  assert_contains "$OUT" "Ports 80 and 443 must be free"
  assert_contains "$OUT" "Port 80 (tcp) is used by the Docker container traefik-traefik-1 (traefik:v3.2)"
  assert_contains "$OUT" "That is Traefik"
  assert_contains "$OUT" "Hostinger"
  assert_contains "$OUT" "docker stop traefik-traefik-1 && docker update --restart=no traefik-traefik-1"
  assert_eq "$(grep -c 'docker stop traefik-traefik-1' <<<"$OUT")" 1 "the advice for both ports is given once"
  assert_contains "$OUT" "Then run the installer again"
}

test_preflight_ports_other_program_is_named() {
  printf '%s\n' "$SS_NGINX" >"$FAKE_DIR/ss_tcp_80"
  printf '%s\n' "${SS_NGINX//:80 /:443 }" >"$FAKE_DIR/ss_tcp_443"
  load_installer
  expect_fail check_ports
  assert_contains "$OUT" "Port 80 (tcp) is used by the program: nginx"
  assert_contains "$OUT" "Port 443 (tcp) is used by the program: nginx"
  assert_contains "$OUT" "systemctl disable --now nginx"
  assert_eq "$(grep -c 'systemctl disable --now nginx' <<<"$OUT")" 1 "the advice for both ports is given once"
  assert_not_contains "$OUT" "Traefik"
}

test_preflight_ports_unknown_program() {
  echo 'LISTEN 0 128 0.0.0.0:443 0.0.0.0:*' >"$FAKE_DIR/ss_tcp_443"
  load_installer
  expect_fail check_ports
  assert_contains "$OUT" "Port 443 (tcp) is used by the program: ?"
  assert_contains "$OUT" "ss -ltnp"
}

test_preflight_ports_own_proxy_is_fine_on_a_second_run() {
  local port
  for port in 80 443; do
    printf '%s\n' "${SS_DOCKER_PROXY//:80 /:$port }" >"$FAKE_DIR/ss_tcp_$port"
    echo "myai-caddy-1" >"$FAKE_DIR/ps_ours_$port"
    echo "myai-caddy-1 (caddy:2.11.9-alpine)" >"$FAKE_DIR/ps_any_$port"
  done
  load_installer
  expect_ok check_ports
}

test_preflight_port_users_reads_ss_output() {
  load_installer
  printf '%s\n' "$SS_NGINX" >"$FAKE_DIR/ss_tcp_80"
  assert_eq "$(port_users tcp 80)" "nginx"
  printf '%s\n' "$SS_DOCKER_PROXY" >"$FAKE_DIR/ss_tcp_80"
  assert_eq "$(port_users tcp 80)" "docker-proxy"
  echo 'UNCONN 0 0 0.0.0.0:443 0.0.0.0:* users:(("caddy",pid=5,fd=9))' >"$FAKE_DIR/ss_udp_443"
  assert_eq "$(port_users udp 443)" "caddy"
  assert_eq "$(port_users tcp 443)" ""
}

test_preflight_dry_run_reports_problems_and_goes_on() {
  echo 1000 >"$FAKE_DIR/uid"
  fake_os centos 9 x "CentOS Stream 9"
  printf '%s\n' "$SS_NGINX" >"$FAKE_DIR/ss_tcp_80"
  echo 300000000 >"$FAKE_DIR/df_avail_kb"
  load_installer --dry-run
  expect_ok preflight
  assert_contains "$OUT" "PROBLEM (a real run stops here): This installer must run as the user root."
  assert_contains "$OUT" "PROBLEM (a real run stops here): Ports 80 and 443 must be free."
  assert_contains "$OUT" "3 check(s) above would stop a real run"
}

test_preflight_all_checks_pass_on_the_target_machine() {
  : >"$MYAI_SYSROOT/swapfile"
  echo 300000000 >"$FAKE_DIR/df_avail_kb"
  load_installer
  expect_ok preflight
  assert_contains "$OUT" "Processor: 8 vCPUs, AVX2 available"
  assert_contains "$OUT" "Memory: 31.3 GB"
  assert_contains "$OUT" "Model: hauhau (chosen for 31.3 GB of memory), 21.2 GB"
  assert_not_contains "$OUT" "Warning"
}

# ------------------------------------------------------------- packages ---

test_packages_nothing_to_install_when_all_is_there() {
  load_installer
  expect_ok install_packages
  assert_contains "$OUT" "already installed"
  assert_not_called "apt-get"
}

test_packages_installs_what_is_missing() {
  rm "$T/bin/dig" "$T/bin/ss" "$T/bin/qrencode"
  load_installer
  expect_ok install_packages
  assert_called "apt-get -o DPkg::Lock::Timeout=600 update -q"
  assert_called "install -y --no-install-recommends dnsutils iproute2"
  assert_called "install -y --no-install-recommends qrencode"
  assert_eq "$(grep -c 'update -q' "$FAKE_LOG")" 1 "the package lists are refreshed once"
}

test_packages_failure_stops_with_advice_and_shows_the_log() {
  rm "$T/bin/dig"
  load_installer
  # update works, install fails
  cat >"$T/bin/apt-get" <<'APT'
#!/usr/bin/env bash
printf 'apt-get %s\n' "$*" >>"$FAKE_LOG"
case "$*" in *" install "*) echo "E: Unable to locate package dnsutils"; exit 100 ;; esac
exit 0
APT
  chmod 755 "$T/bin/apt-get"
  expect_fail install_packages
  assert_contains "$OUT" "The basic tools could not be installed (dnsutils)"
  assert_contains "$OUT" "E: Unable to locate package dnsutils" "the last lines of the log are shown"
  assert_contains "$OUT" "run the same command again"
  assert_contains "$(cat "$MYAI_SYSROOT/var/log/myai-install.log")" "Unable to locate package"
}

test_packages_missing_qr_tool_is_not_fatal() {
  rm "$T/bin/qrencode"
  echo 100 >"$FAKE_DIR/rc_apt-get"
  load_installer
  # The package lists count as already refreshed.
  APT_UPDATED=1 expect_ok install_packages
  assert_contains "$OUT" "The link is still shown as text"
}

# --------------------------------------------------------------- Docker ---

test_docker_already_there_is_left_alone() {
  load_installer
  read_os
  expect_ok install_docker
  assert_contains "$OUT" "Docker 29.0.0 with Compose is ready"
  assert_not_called "apt-get"
  assert_no_file "$MYAI_SYSROOT/etc/apt/sources.list.d/docker.list"
}

test_docker_too_old_is_updated_from_dockers_source() {
  echo "27.3.1" >"$FAKE_DIR/docker_version"
  : >"$FAKE_DIR/dpkg_docker-ce"
  load_installer
  read_os
  expect_ok install_docker
  assert_contains "$OUT" "Docker 27 is too old"
  assert_called "install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
  assert_eq "$(cat "$MYAI_SYSROOT/etc/apt/sources.list.d/docker.list")" \
    "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable"
  assert_file "$MYAI_SYSROOT/etc/apt/keyrings/docker.asc"
}

test_docker_existing_package_source_is_not_added_twice() {
  echo "27.3.1" >"$FAKE_DIR/docker_version"
  : >"$FAKE_DIR/dpkg_docker-ce"
  mkdir -p "$MYAI_SYSROOT/etc/apt/sources.list.d"
  echo "deb [signed-by=/usr/share/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" \
    >"$MYAI_SYSROOT/etc/apt/sources.list.d/docker-ce.list"
  load_installer
  read_os
  expect_ok install_docker
  assert_no_file "$MYAI_SYSROOT/etc/apt/sources.list.d/docker.list"
  assert_no_file "$MYAI_SYSROOT/etc/apt/keyrings/docker.asc"
}

test_docker_too_old_from_the_distribution_stops_with_advice() {
  echo "24.0.7" >"$FAKE_DIR/docker_version"
  load_installer
  read_os
  expect_fail install_docker
  assert_contains "$OUT" "is too old"
  assert_contains "$OUT" "apt-get remove -y docker.io"
}

test_docker_debian_uses_the_debian_source() {
  echo "20.10.24" >"$FAKE_DIR/docker_version"
  : >"$FAKE_DIR/dpkg_docker-ce"
  fake_os debian 12 bookworm "Debian GNU/Linux 12 (bookworm)"
  load_installer
  read_os
  expect_ok install_docker
  assert_contains "$(cat "$MYAI_SYSROOT/etc/apt/sources.list.d/docker.list")" \
    "https://download.docker.com/linux/debian bookworm stable"
}
