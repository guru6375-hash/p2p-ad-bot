"""Logging configuration for the bot, plus the secret-redaction helper.

One logger object (``p2pbot``) is configured by :func:`setup_logging`, which is
idempotent: calling it twice never duplicates a handler. A file handler is added only
when a log path is supplied, a stream handler is always present.

:func:`redact_secrets` masks every credential value known to a :class:`~p2pbot.config.Settings`
instance (or, when no settings are supplied, every credential-looking value of the process
environment) so no operator secret can reach the log or a Telegram message.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from .config import Settings, is_secret_key
from .constants import REDACTED

LOGGER_NAME = "p2pbot"
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Marker attributes stamped on the handlers we own, used to stay idempotent.
_STREAM_MARKER = "_p2pbot_stream_handler"
_FILE_MARKER = "_p2pbot_file_path"

#: Shortest credential value worth masking (see :func:`_secret_values`).
_MIN_MASK_LENGTH = 2


def setup_logging(log_path: Path | None = None, level: str = "INFO") -> logging.Logger:
    """Configure and return the ``p2pbot`` logger.

    Args:
        log_path: file to log to; ``None`` keeps console-only logging.
        level: level name (``DEBUG``/``INFO``/…); unknown names fall back to ``INFO``.

    Repeated calls are idempotent: the stream handler is added once and the file handler
    is only replaced when the requested path differs from the attached one.
    """
    logger = logging.getLogger(LOGGER_NAME)
    _ensure_stream_handler(logger)

    resolved_level = _resolve_level(level)
    if resolved_level is None:
        resolved_level = logging.INFO
        logger.warning("unknown log level %r; falling back to INFO", level)
    logger.setLevel(resolved_level)

    if log_path is not None:
        _ensure_file_handler(logger, Path(log_path))
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``p2pbot`` logger, e.g. ``get_logger("engine")``."""
    if not name or not name.strip():
        return logging.getLogger(LOGGER_NAME)
    child = name.strip()
    if child == LOGGER_NAME or child.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(child)
    return logging.getLogger(f"{LOGGER_NAME}.{child}")


def redact_secrets(text: str, settings: Settings | None = None) -> str:
    """Replace every known credential value inside ``text`` with ``***``.

    When ``settings`` is omitted the process environment is scanned for credential-shaped
    keys, so the helper is safe to call from anywhere (including before configuration).
    """
    if not text:
        return text
    redacted = text
    for secret in _secret_values(settings):
        if secret in redacted:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def _resolve_level(level: str) -> int | None:
    name = (level or "").strip().upper()
    if not name:
        return None
    return logging.getLevelNamesMapping().get(name)


def _ensure_stream_handler(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        if getattr(handler, _STREAM_MARKER, False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    setattr(handler, _STREAM_MARKER, True)
    logger.addHandler(handler)


def _ensure_file_handler(logger: logging.Logger, path: Path) -> None:
    resolved = Path(path)
    for handler in list(logger.handlers):
        attached = getattr(handler, _FILE_MARKER, None)
        if attached is None:
            continue
        if Path(attached) == resolved:
            return
        logger.removeHandler(handler)
        handler.close()
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(resolved, encoding="utf-8")
    except OSError as exc:
        logger.warning("cannot open log file %s: %s (logging to the console only)", resolved, exc)
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    setattr(handler, _FILE_MARKER, str(resolved))
    logger.addHandler(handler)


def _secret_values(settings: Settings | None) -> list[str]:
    """Every credential value to mask, longest first so overlaps cannot leak a suffix.

    Values shorter than two characters are skipped: replacing a one-character value would
    rewrite unrelated text (timestamps, ids) without protecting anything.
    """
    source: Iterable[tuple[str, str]] = (
        settings.raw.items() if settings is not None else os.environ.items()
    )
    values: set[str] = set()
    for key, value in source:
        if len(value) >= _MIN_MASK_LENGTH and is_secret_key(key):
            values.add(value)
    if settings is not None:
        for account in settings.accounts.values():
            for value in account.credentials.values():
                if len(value) >= _MIN_MASK_LENGTH:
                    values.add(value)
    return sorted(values, key=len, reverse=True)


__all__ = ["get_logger", "redact_secrets", "setup_logging"]
