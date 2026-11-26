"""Checks of docker-compose.yml, the Caddyfile and .env.example: the parts of
the stack around the sandbox that review findings were about.

They need the docker CLI with the compose plugin; the Caddy checks also need
a running Docker daemon and the Caddy image named in .env.example already
present. Otherwise they are skipped. Nothing is pulled, and Caddy is
published on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from conftest import free_port

APP = Path(__file__).resolve().parents[2]
COMPOSE, CADDYFILE, ENV_EXAMPLE = APP / "docker-compose.yml", APP / "Caddyfile", APP / ".env.example"
SITE = "srv123456.hstgr.cloud"
GATE = "0123456789abcdef0123456789abcdef"
FILLED = {  # values .env.example leaves empty on purpose
    "GATE_TOKEN": GATE, "LLM_API_KEY": "1" * 64, "AGENT_API_KEY": "2" * 64, "SANDBOX_TOKEN": "3" * 64,
    "SEARXNG_SECRET": "4" * 64, "WEBUI_SECRET_KEY": "5" * 64, "WEBUI_ADMIN_PASSWORD": "Example-Password-1",
    "DASHBOARD_PASSWORD": "Example-Password-1", "PAIRING_CODE": "12345678",
}  # fmt: skip


def _example() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = re.match(r"([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match[1]] = match[2]
    return values


def _seconds(duration: str) -> float:
    """A Go duration as compose prints it ("5m0s", "15s") in seconds."""
    units = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", duration))


def _docker_ok(*args: str) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", *args], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="module")
def compose(tmp_path_factory) -> dict:
    """The stack as `docker compose config` renders it with a filled .env."""
    if not _docker_ok("compose", "version"):
        pytest.skip("needs the docker CLI with the compose plugin")
    values = _example()
    values.update(FILLED)
    values["DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    env_file = tmp_path_factory.mktemp("compose") / "test.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    env = {key: value for key, value in os.environ.items() if key not in values}
    out = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(env_file),
                          "config", "--format", "json"], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stderr == "", out.stderr  # no warnings either
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def caddy_image() -> str:
    image = _example()["CADDY_IMAGE"]
    if not _docker_ok("image", "inspect", image):
        pytest.skip(f"needs a Docker daemon with {image} present (tests do not pull images)")
    return image


def test_only_caddy_publishes_ports(compose):
    assert {name for name, service in compose["services"].items() if service.get("ports")} == {"caddy"}


def test_webui_keeps_the_embedding_model_of_its_image(compose):
    # The image keeps its embedding model under /app/backend/data/cache. A
    # bind mount on /app/backend/data hid it: the first start then tried to
    # download it from Hugging Face, and document search failed without it.
    webui = compose["services"]["webui"]
    assert webui["environment"]["DATA_DIR"] == "/data"
    targets = [volume["target"] for volume in webui["volumes"]]
    assert "/data" in targets
    assert not [t for t in targets if t == "/app/backend/data" or t.startswith("/app/backend/data/")]
    health = webui["healthcheck"]
    assert "test" not in health  # the image's own test is kept
    assert _seconds(health["start_period"]) >= 300


def test_searxng_names_this_server_as_its_contact(compose):
    # Without a contact SearXNG does not load Wikipedia, Wikidata and
    # OpenStreetMap. The value goes into User-Agent headers: never an e-mail.
    environment = compose["services"]["searxng"]["environment"]
    assert environment["SEARXNG_CONTACT_URL"] == f"https://{SITE}/"
    assert "@" not in environment["SEARXNG_CONTACT_URL"]


def test_searxng_config_dir_is_a_named_volume(compose):
    # The image declares /etc/searxng a volume. Left unnamed, every new
    # container gets a new anonymous volume and each "docker compose down"
    # (myai restore does one) leaves the old one behind.
    mounts = {volume["target"]: volume for volume in compose["services"]["searxng"]["volumes"]}
    assert mounts["/etc/searxng"]["type"] == "volume"
    assert mounts["/etc/searxng"]["source"] == "searxng_config"


def test_agent_gets_the_model_servers_thinking_budget(compose):
    # The agent leaves room for as many thinking tokens as the model server
    # allows. Without the same value, a budget raised in .env lets the model
    # think past the agent's max_tokens, and that whole call is wasted.
    services = compose["services"]
    assert services["agent"]["environment"]["LLM_THINK_BUDGET"] == services["llm"]["environment"]["LLAMA_ARG_THINK_BUDGET"]


def test_sandbox_health_check_is_cheap_works_and_carries_no_environment(compose, sandbox):
    health = compose["services"]["sandbox"]["healthcheck"]
    test = health["test"]
    assert test[:3] == ["CMD", "/usr/bin/env", "-i"]  # no SANDBOX_TOKEN in the probe's environment
    assert not [part for part in test if "python" in part]  # Python took 9-15 s on a busy CPU
    assert _seconds(health["timeout"]) >= 15 and health["retries"] >= 5
    if not (os.path.exists("/usr/bin/env") and os.path.exists("/bin/bash")):
        pytest.skip("needs /usr/bin/env and /bin/bash to run the probe here")
    # Compose prints "$" as "$$" so that its output stays a valid compose file.
    probe = [part.replace("$$", "$").replace("127.0.0.1/8070", f"127.0.0.1/{sandbox.port}") for part in test[1:]]
    assert subprocess.run(probe, capture_output=True, timeout=15).returncode == 0
    closed = free_port()
    probe = [part.replace(f"127.0.0.1/{sandbox.port}", f"127.0.0.1/{closed}") for part in probe]
    assert subprocess.run(probe, capture_output=True, timeout=15).returncode != 0


def _adapt(image: str, tls_line: str, site: str = "203-0-113-10.sslip.io") -> dict:
    out = subprocess.run(["docker", "run", "--rm", "-e", f"SITE_ADDRESS={site}", "-e", f"GATE_TOKEN={GATE}",
                          "-e", f"CADDY_TLS_LINE={tls_line}", "-v", f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
                          image, "caddy", "adapt", "--config", "/etc/caddy/Caddyfile"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_certificate_authorities_are_as_documented(caddy_image):
    # .env.example and the Caddyfile say: no second authority by default
    # (ZeroSSL needs an account e-mail), and "tls <e-mail>" adds ZeroSSL.
    def issuers(tls_line: str) -> list[str]:
        policies = _adapt(caddy_image, tls_line)["apps"].get("tls", {}).get("automation", {}).get("policies", [])
        return [issuer.get("ca", issuer["module"]) for policy in policies for issuer in policy.get("issuers", [])]

    assert issuers("") == []  # Caddy's defaults: Let's Encrypt, plus ZeroSSL only with an e-mail
    assert "https://acme.zerossl.com/v2/DV90" in issuers("tls owner@mail.example.net")


def test_a_bare_ip_address_gets_a_certificate_without_sni(caddy_image, tmp_path):
    # Clients send no server name for an IP address; Caddy then needs
    # default_sni, or every handshake ends in "tlsv1 alert internal error".
    port = free_port()
    name = f"sandbox-test-caddy-{port}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "SITE_ADDRESS=127.0.0.1",
                    "-e", f"GATE_TOKEN={GATE}", "-e", "CADDY_TLS_LINE=tls internal", "-p", f"127.0.0.1:{port}:443",
                    "-v", f"{CADDYFILE}:/etc/caddy/Caddyfile:ro", caddy_image],
                   check=True, capture_output=True, timeout=120)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # "tls internal" is self-signed
    try:
        reply, error, deadline = b"", None, time.monotonic() + 30
        while time.monotonic() < deadline and not reply:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                    with context.wrap_socket(raw, server_hostname=None) as tls:  # no SNI
                        tls.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                        while chunk := tls.recv(65536):
                            reply += chunk
            except OSError as exc:  # not listening yet, or the handshake failed
                error = exc
                time.sleep(0.5)
        assert reply.startswith(b"HTTP/1.1 200"), error
        assert b"Private server" in reply
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
