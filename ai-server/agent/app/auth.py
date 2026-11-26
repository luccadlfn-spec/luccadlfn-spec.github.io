"""Authentication for the HTTP API: the bearer key, dashboard sessions, login rate limit.

Sessions are stateless signed cookies. The signing key is derived from a random
secret kept in the database, the dashboard password and a counter, so sessions
survive restarts, die when the password changes, and can all be revoked at once
(logout bumps the counter).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Callable

from .config import Settings
from .db import Database

SESSION_COOKIE = "myai_agent_session"
SESSION_SECONDS = 30 * 24 * 3600
LOGIN_FAILURES = 5              # per client ...
LOGIN_WINDOW = 300.0            # ... within this many seconds
GLOBAL_FAILURES = 30            # across all clients, in case the client address is forged
MAX_TRACKED_CLIENTS = 2000


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


class Auth:
    def __init__(self, settings: Settings, db: Database, clock: Callable[[], float] = time.time):
        self.s, self.db, self._clock = settings, db, clock
        self._failures: dict[str, list[float]] = {}
        self._global: list[float] = []
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

    def password_ok(self, password: str) -> bool:
        return self.password_enabled and isinstance(password, str) and _same(password, self.s.dashboard_password)

    def retry_after(self, client: str) -> int:
        """Seconds until this client may try again; 0 when it may try now."""
        now = self._clock()
        self._global = [t for t in self._global if now - t < LOGIN_WINDOW]
        mine = [t for t in self._failures.get(client, []) if now - t < LOGIN_WINDOW]
        if mine:
            self._failures[client] = mine
        else:
            self._failures.pop(client, None)
        if len(mine) >= LOGIN_FAILURES:
            return int(LOGIN_WINDOW - (now - mine[0])) + 1
        if len(self._global) >= GLOBAL_FAILURES:
            return int(LOGIN_WINDOW - (now - self._global[0])) + 1
        return 0

    def record_failure(self, client: str) -> None:
        now = self._clock()
        if len(self._failures) >= MAX_TRACKED_CLIENTS and client not in self._failures:
            self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(client, []).append(now)
        self._global.append(now)

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
        if not expires.isdigit() or int(expires) < self._clock():
            return False
        good = hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()
        return _same(sig, good)

    def revoke_sessions(self) -> None:
        self.db.kv_set("session_epoch", int(self.db.kv_get("session_epoch", 0)) + 1)
