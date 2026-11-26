"""Authentication for the HTTP API: the bearer key, dashboard sessions, login rate limit.

Sessions are stateless signed cookies. The signing key is derived from a random
secret kept in the database, the dashboard password and a counter, so sessions
survive restarts, die when the password changes, and can all be revoked at once
(logout bumps the counter).

The login limit counts failures per client. The client is the TCP peer, except
for the reverse proxy (Caddy), whose X-Forwarded-For names the real client: any
other container that can reach the agent (the sandbox runs model-written code)
could otherwise pick a fresh name for every guess.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import socket
import time
from typing import Any, Awaitable, Callable

from fastapi import Request

from .config import Settings
from .db import Database

SESSION_COOKIE = "myai_agent_session"
SESSION_SECONDS = 30 * 24 * 3600
LOGIN_FAILURES = 5              # per client ...
LOGIN_WINDOW = 300.0            # ... within this many seconds
GLOBAL_FAILURES = 30            # across all clients within the window; above it, logins slow down
GLOBAL_SPACING = 5.0            # to one attempt per this many seconds (a full stop would lock the owner out)
MAX_TRACKED_CLIENTS = 2000
PROXY_REFRESH = 10.0            # an unknown peer looks the proxy's address up again, at most this often

Resolve = Callable[[str], Awaitable[set[str]]]


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


def _address(text: str) -> str:
    """A canonical address, or the text itself when it is not one."""
    try:
        ip = ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return text.strip()
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def client_bucket(address: str) -> str:
    """The rate-limit key for an address. IPv6 is grouped by /64, which one host usually owns whole."""
    try:
        ip = ipaddress.ip_address(_address(address))
    except ValueError:
        return address[:64] or "unknown"
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


async def _resolve(name: str) -> set[str]:
    try:
        ipaddress.ip_address(name.strip("[]"))
        return {_address(name)}
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(name, None, type=socket.SOCK_STREAM)
    return {_address(info[4][0]) for info in infos}


class ProxyTrust:
    """Knows the reverse proxy's addresses. Compose service names are resolved through
    Docker's DNS; the address changes when the container is recreated, so a peer that
    is not known triggers a fresh lookup (at most every PROXY_REFRESH seconds)."""

    def __init__(self, names: tuple[str, ...], resolve: Resolve = _resolve,
                 clock: Callable[[], float] = time.monotonic):
        self._names = tuple(n for n in names if n)
        self._resolve, self._clock = resolve, clock
        self._addresses: frozenset[str] = frozenset()
        self._checked: float | None = None

    async def is_proxy(self, peer: str) -> bool:
        if not peer or not self._names:
            return False
        peer = _address(peer)
        if peer in self._addresses:
            return True
        now = self._clock()
        if self._checked is None or now - self._checked >= PROXY_REFRESH:
            self._checked = now
            found: set[str] = set()
            for name in self._names:
                try:
                    found |= await asyncio.wait_for(self._resolve(name), 2.0)
                except Exception:          # not resolvable right now (or ever, outside compose)
                    continue
            self._addresses = frozenset(found)
        return peer in self._addresses


class Auth:
    def __init__(self, settings: Settings, db: Database, clock: Callable[[], float] = time.time):
        self.s, self.db, self._clock = settings, db, clock
        self.proxies = ProxyTrust(settings.trusted_proxies)
        self._failures: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._last_spaced = float("-inf")
        secret = db.kv_get("session_secret")
        if not isinstance(secret, str) or len(secret) < 32:
            secret = secrets.token_hex(32)
            db.kv_set("session_secret", secret)
        self._secret = secret

    # ---- bearer ----
    def bearer_ok(self, header: str | None) -> bool:
        key = self.s.agent_api_key
        if not key or not header:
            return False                 # an empty key never matches: no key, no API
        scheme, _, value = header.partition(" ")
        return scheme.lower() == "bearer" and _same(value.strip(), key)

    # ---- password login ----
    @property
    def password_enabled(self) -> bool:
        return bool(self.s.dashboard_password)

    def password_ok(self, password: Any) -> bool:
        return self.password_enabled and isinstance(password, str) and _same(password, self.s.dashboard_password)

    async def client_key(self, request: Request) -> str:
        """Who is calling, for the login limit: the TCP peer, or the client the proxy names."""
        peer = request.client.host if request.client else ""
        if await self.proxies.is_proxy(peer):
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                peer = forwarded.rsplit(",", 1)[-1].strip()       # the entry the proxy itself added
        return client_bucket(peer)

    def check_login(self, client: str, password: Any) -> tuple[bool, int]:
        """(password right, seconds to wait before trying again).

        Deliberately synchronous: the limit check, the comparison and the bookkeeping
        happen as one step, so attempts sent at the same moment cannot all pass the
        check before any of them is counted.
        """
        now = self._clock()
        self._global = [t for t in self._global if now - t < LOGIN_WINDOW]
        mine = [t for t in self._failures.get(client, []) if now - t < LOGIN_WINDOW]
        if mine:
            self._failures[client] = mine
        else:
            self._failures.pop(client, None)
        if len(mine) >= LOGIN_FAILURES:
            return False, int(LOGIN_WINDOW - (now - mine[0])) + 1
        if len(self._global) >= GLOBAL_FAILURES:
            # Many failures from many clients: slow every attempt down instead of refusing
            # all of them, which would let anyone keep the owner out.
            since = now - self._last_spaced
            if since < GLOBAL_SPACING:
                return False, max(1, int(GLOBAL_SPACING - since + 0.999))
            self._last_spaced = now
        if self.password_ok(password):
            return True, 0
        if len(self._failures) >= MAX_TRACKED_CLIENTS and client not in self._failures:
            self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(client, []).append(now)
        self._global.append(now)
        return False, 0

    # ---- sessions ----
    def _key(self) -> bytes:
        epoch = self.db.kv_get("session_epoch", 0)
        material = f"{epoch}|{hashlib.sha256(self.s.dashboard_password.encode()).hexdigest()}"
        return hmac.new(bytes.fromhex(self._secret), material.encode(), hashlib.sha256).digest()

    def new_session(self) -> str:
        payload = f"{int(self._clock()) + SESSION_SECONDS}.{secrets.token_hex(8)}"
        sig = hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def session_ok(self, cookie: str | None) -> bool:
        if not cookie or not self.password_enabled or len(cookie) > 200:
            return False
        payload, _, sig = cookie.rpartition(".")
        expires = payload.split(".", 1)[0]
        # isascii: str.isdigit() also accepts characters such as "²" that int() refuses.
        if not (expires.isascii() and expires.isdigit() and len(expires) <= 12) or int(expires) < self._clock():
            return False
        good = hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()
        return _same(sig, good)

    def revoke_sessions(self) -> None:
        self.db.kv_set("session_epoch", int(self.db.kv_get("session_epoch", 0)) + 1)
