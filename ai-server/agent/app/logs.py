"""Logging setup, including a filter that keeps configured secrets out of the log."""

from __future__ import annotations

import logging

from .config import Settings


class Redact(logging.Filter):
    """Last line of defence: no configured secret is ever written to the log."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if any(s in message for s in self._secrets):
            for s in self._secrets:
                message = message.replace(s, "<redacted>")
            record.msg, record.args = message, None
        return True


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "courlan"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    secrets = settings.secrets()
    if secrets:
        redact = Redact(secrets)
        for handler in logging.getLogger().handlers:
            handler.addFilter(redact)
