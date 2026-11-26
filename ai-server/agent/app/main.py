"""Entry point: `uvicorn app.main:app`."""

from __future__ import annotations

import logging
import sqlite3

from .api import create_app
from .config import Settings
from .logs import configure_logging
from .services import Services

log = logging.getLogger("app")

_settings = Settings.from_env()
configure_logging(_settings)
try:
    _services = Services(_settings)
except (sqlite3.Error, OSError) as exc:
    # The usual cause: the data directory is not writable by this container's user (uid 10001).
    log.error("cannot open the database at %s (%s). Check that the data directory exists "
              "and belongs to uid 10001.", _settings.db_path, exc)
    raise SystemExit(1) from None
app = create_app(_services)
