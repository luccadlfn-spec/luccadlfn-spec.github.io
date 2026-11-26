"""SSRF guard for fetch_url.

The agent reads untrusted pages, and a page can ask it to fetch anything. This
module decides which destinations are allowed: public internet addresses only.
The caller connects to the exact address checked here, so a second DNS answer
cannot swap in an internal one (no rebinding gap).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]


class Blocked(Exception):
    """The destination is not allowed. The message is safe to show to the model."""


# Names that only make sense inside the compose networks or on the host.
BLOCKED_HOSTS = frozenset({
    "llm", "agent", "sandbox", "webui", "searxng", "caddy", "localhost",
    "host.docker.internal", "gateway.docker.internal", "metadata.google.internal",
})
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".intranet", ".corp")

_V4_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)]
_V6_BLOCKED = [ipaddress.ip_network(n) for n in (
    "::/128", "::1/128", "::/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32",
    "2002::/16", "3fff::/20", "5f00::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
)]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def check_ip(ip: IPAddress) -> None:
    """Raise Blocked unless `ip` is an ordinary public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:                 # ::ffff:a.b.c.d
            return check_ip(ip.ipv4_mapped)
        if ip in _NAT64:                               # 64:ff9b::a.b.c.d reaches a.b.c.d
            return check_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        blocked = _V6_BLOCKED
    else:
        blocked = _V4_BLOCKED
    if (any(ip in net for net in blocked) or ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified or not ip.is_global):
        raise Blocked(f"refused: {ip} is not a public internet address")


def check_host_name(host: str) -> None:
    """Refuse names that can only resolve inside the stack, before any DNS lookup."""
    name = host.rstrip(".").lower()
    if not name:
        raise Blocked("the URL has no host")
    if name in BLOCKED_HOSTS or name.endswith(BLOCKED_SUFFIXES):
        raise Blocked(f"refused: host '{name}' is internal")
    if "." not in name:
        # Single-label names are compose service or container names, or numeric
        # tricks such as "2130706433"; nothing public is reached that way.
        raise Blocked(f"refused: '{name}' is not a public host name")


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


async def resolve_public(host: str, port: int, resolver: Resolver = system_resolver) -> str:
    """Return one checked address for `host`. Every address the name has must be public."""
    literal = host.strip("[]")
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        ip = None
    if ip is not None:
        check_ip(ip)
        return str(ip)
    check_host_name(host)
    try:
        addresses = await asyncio.wait_for(resolver(host.rstrip("."), port), timeout=10)
    except Blocked:
        raise
    except Exception as exc:
        raise Blocked(f"could not resolve host '{host}' ({type(exc).__name__})") from None
    if not addresses:
        raise Blocked(f"host '{host}' has no address")
    parsed = []
    for addr in addresses[:16]:
        try:
            # A zone id ("fe80::1%eth0") would make ip_address fail; strip it and let check_ip refuse.
            parsed.append(ipaddress.ip_address(addr.split("%", 1)[0]))
        except ValueError:
            raise Blocked(f"host '{host}' resolved to something that is not an address") from None
    for ip in parsed:
        check_ip(ip)       # one internal answer poisons the whole name
    v4 = [ip for ip in parsed if ip.version == 4]
    return str((v4 or parsed)[0])
