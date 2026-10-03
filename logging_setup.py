"""Logging configuration: rotating files, console output, secret redaction."""
from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import Config

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


class RedactingFormatter(logging.Formatter):
    """Formatter that removes secret values from the final text, tracebacks included."""

    def __init__(self, fmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        unique = {s for s in secrets if s and len(s) >= 4}
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "[redacted]")
        return text


class SilencePrivilegedIntentWarning(logging.Filter):
    """Silences only the warning 'Privileged message content intent is missing'."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.WARNING and "Privileged message content intent is missing" in record.getMessage():
            return False
        return True


def setup_logging(config: Config) -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    level = getattr(logging, config.log_level, logging.INFO)
    log_dir = Path(config.log_dir)
    if not log_dir.is_absolute():
        log_dir = Path(__file__).resolve().parent / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = RedactingFormatter(LOG_FORMAT, config.secrets())
    intent_filter = SilencePrivilegedIntentWarning()

    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(intent_filter)

    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception as exc:
            logging.getLogger().debug("stderr reconfigure skipped: %s", exc)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(intent_filter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console)

    # Libraries can log payloads or session identifiers at low levels. Keep
    # them at a level that never dumps raw protocol data.
    logging.getLogger("discord.gateway").setLevel(max(level, logging.WARNING))
    logging.getLogger("discord.http").setLevel(max(level, logging.INFO))
    logging.getLogger("lavalink").setLevel(max(level, logging.INFO))
    logging.getLogger("aiohttp").setLevel(max(level, logging.WARNING))

    from utils.timing import install_http_rate_limit_filter

    install_http_rate_limit_filter()
