"""SSRF guard: address forms, host names, DNS answers, redirects, download limits."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from app.tools.base import ToolError
from app.tools.netguard import Blocked, check_host_name, check_ip, resolve_public
from app.tools.web import Fetcher, MAX_DOWNLOAD_BYTES, fetch_url

BLOCKED_ADDRESSES = [
    "127.0.0.1", "127.8.9.1", "0.0.0.0", "0.1.2.3",                       # loopback, "this network"
    "10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.1",            # private
    "169.254.169.254", "169.254.0.1",                                      # link-local, cloud metadata
    "100.64.0.1", "100.127.255.254",                                       # CGNAT
    "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",        # multicast, reserved, broadcast
    "192.0.0.8", "192.0.2.1", "198.18.0.1", "198.51.100.7", "203.0.113.9", "192.88.99.1",
    "::", "::1", "fe80::1", "fc00::1", "fd12:3456::1", "fec0::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:192.168.0.1",   # IPv4-mapped
    "::127.0.0.1", "::10.1.2.3",                                           # IPv4-compatible (deprecated)
    "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b:1::1",                   # NAT64 to internal
    "2002:7f00:1::", "2002:a00:1::", "2001::1", "2001:db8::1", "100::1",   # 6to4, Teredo, documentation, discard
]
ALLOWED_ADDRESSES = ["8.8.8.8", "1.1.1.1", "93.184.215.14", "172.15.0.1", "172.32.0.1", "100.63.255.255",
                     "2606:4700:4700::1111", "2a00:1450:4001:81b::200e", "::ffff:8.8.8.8", "64:ff9b::808:808"]


@pytest.mark.parametrize("addr", BLOCKED_ADDRESSES)
def test_internal_addresses_are_refused(addr):
    with pytest.raises(Blocked):
        check_ip(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ALLOWED_ADDRESSES)
def test_public_addresses_are_allowed(addr):
    check_ip(ipaddress.ip_address(addr))


@pytest.mark.parametrize("host", ["llm", "webui", "sandbox", "searxng", "agent", "caddy", "localhost", "LLM",
                                  "localhost.", "foo.localhost", "db.internal", "host.docker.internal",
                                  "printer.local", "nas.lan", "myai-llm-1", "2130706433", "0x7f000001", ""])
def test_internal_host_names_are_refused_before_dns(host):
    with pytest.raises(Blocked):
        check_host_name(host)


def resolver_for(table: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("name not known")
        return table[host]
    return resolve


async def test_resolution_checks_every_answer():
    table = {"good.example": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
             "rebind.example": ["93.184.215.14", "10.0.0.7"],          # one internal answer poisons the name
             "inside.example": ["192.168.1.20"], "meta.example": ["169.254.169.254"],
             "mapped.example": ["::ffff:127.0.0.1"], "empty.example": [], "odd.example": ["not-an-ip"]}
    resolve = resolver_for(table)
    assert await resolve_public("good.example", 443, resolve) == "93.184.215.14"
    for host in ("rebind.example", "inside.example", "meta.example", "mapped.example", "empty.example",
                 "odd.example", "missing.example"):
        with pytest.raises(Blocked):
            await resolve_public(host, 80, resolve)


@pytest.mark.parametrize("literal", ["127.0.0.1", "[::1]", "10.1.1.1", "[::ffff:10.0.0.1]", "169.254.169.254"])
async def test_ip_literals_are_checked_without_dns(literal):
    async def never(host, port):
        raise AssertionError("DNS must not be used for IP literals")
    with pytest.raises(Blocked):
        await resolve_public(literal, 80, never)


# ---- the fetcher on top of the guard ----

HTML = b"<html><head><title>T</title></head><body><p>" + b"Readable words. " * 40 + b"</p></body></html>"


def make_fetcher(handler, table=None):
    table = table or {"public.example": ["93.184.215.14"], "other.example": ["93.184.215.99"]}
    return Fetcher(resolver=resolver_for(table), transport=httpx.MockTransport(handler))


async def test_connects_to_the_checked_address_and_keeps_host_and_sni():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=HTML)

    f = make_fetcher(handler)
    text = await fetch_url(f, "https://public.example/page?x=1", 6000)
    assert "Readable words." in text and text.splitlines()[0] == "T"
    req = seen[0]
    assert req.url.host == "93.184.215.14" and req.url.path == "/page" and req.url.query == b"x=1"
    assert req.headers["host"] == "public.example"
    assert req.extensions["sni_hostname"] == "public.example"
    await f.aclose()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8080/", "http://llm:8080/v1/models", "http://sandbox:8070/exec",
    "http://webui:8080/", "http://searxng:8080/search?q=x", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://2130706433/", "http://0x7f.0.0.1/", "http://127.1/",
    "http://017700000001/", "http://172.17.0.1/", "http://user:pw@public.example/", "http://public.example@10.0.0.1/",
    "ftp://public.example/file", "file:///etc/passwd", "gopher://public.example/", "javascript:alert(1)",
    "http://", "not a url", "http://inside.example/",
])
async def test_forbidden_urls_never_reach_the_network(url):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"request was sent to {request.url}")

    table = {"public.example": ["93.184.215.14"], "inside.example": ["10.0.0.5"],
             # What a libc resolver answers for numeric shorthands:
             "0x7f.0.0.1": ["127.0.0.1"], "127.1": ["127.0.0.1"]}
    f = make_fetcher(handler, table)
    with pytest.raises(ToolError):
        await f.fetch(url)
    await f.aclose()


async def test_redirect_to_a_private_address_is_refused():
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    f = make_fetcher(handler)
    with pytest.raises(ToolError, match="refused"):
        await f.fetch("http://public.example/start")
    assert hops == ["public.example"]              # the internal hop was never contacted
    await f.aclose()


async def test_redirect_to_an_internal_name_is_refused_after_a_public_hop():
    table = {"public.example": ["93.184.215.14"], "other.example": ["93.184.215.99"], "evil.example": ["10.0.0.9"]}
    targets = {"public.example": "https://other.example/next", "other.example": "http://evil.example/admin"}
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.headers["host"])
        return httpx.Response(307, headers={"location": targets[request.headers["host"]]})

    f = make_fetcher(handler, table)
    with pytest.raises(ToolError, match="refused"):
        await f.fetch("http://public.example/")
    assert hops == ["public.example", "other.example"]
    await f.aclose()


async def test_relative_redirects_are_followed_and_rechecked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"arrived at " + request.url.path.encode())

    f = make_fetcher(handler)
    page = await f.fetch("http://public.example/a")
    assert page.body == b"arrived at /b" and page.url == "http://public.example/b"
    await f.aclose()


async def test_at_most_five_redirects():
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"location": f"/hop{count}"})

    f = make_fetcher(handler)
    with pytest.raises(ToolError, match="redirects"):
        await f.fetch("http://public.example/")
    assert count == 6                               # the first request plus five redirects
    await f.aclose()


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "application/octet-stream", "video/mp4", ""])
async def test_non_text_content_is_refused(ctype):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": ctype} if ctype else {}, content=b"\x00\x01")

    f = make_fetcher(handler)
    with pytest.raises(ToolError, match="not a text page"):
        await f.fetch("http://public.example/file")
    await f.aclose()


async def test_download_is_capped_at_two_megabytes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"a" * (MAX_DOWNLOAD_BYTES + 500_000))

    f = make_fetcher(handler)
    page = await f.fetch("http://public.example/huge")
    assert len(page.body) == MAX_DOWNLOAD_BYTES and page.truncated
    await f.aclose()


async def test_http_errors_and_empty_pages_are_tool_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/missing":
            return httpx.Response(404, headers={"content-type": "text/html"}, content=b"nope")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><script>x()</script></html>")

    f = make_fetcher(handler)
    with pytest.raises(ToolError, match="HTTP 404"):
        await f.fetch("http://public.example/missing")
    with pytest.raises(ToolError, match="no readable text"):
        await fetch_url(f, "http://public.example/empty", 6000)
    await f.aclose()


async def test_fetch_result_respects_max_chars():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"word " * 5000)

    f = make_fetcher(handler)
    text = await fetch_url(f, "http://public.example/long", 800)
    assert len(text) < 900 and "omitted" in text
    await f.aclose()
