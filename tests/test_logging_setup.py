"""Logging setup: idempotency, file handler lifecycle and secret redaction."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from p2pbot.config import load_settings
from p2pbot.logging_setup import LOGGER_NAME, get_logger, redact_secrets, setup_logging


def _detach_dead_handlers(logger: logging.Logger) -> None:
    """Drop handlers bound to a stream a previous test's capture already closed.

    ``setup_logging`` adds a ``StreamHandler`` bound to whatever ``sys.stderr`` is at that
    moment; a handler created while another test held pytest's capture keeps a reference to
    the closed stream, and flushing it later raises ``ValueError: I/O operation on closed
    file``. A closed handler can never be useful, so the fixture repairs the logger.
    """
    for handler in list(logger.handlers):
        try:
            handler.flush()
        except ValueError:
            logger.removeHandler(handler)
            handler.close()


@pytest.fixture
def clean_logger():
    """Give a test a pristine ``p2pbot`` logger and restore it afterwards."""
    logger = logging.getLogger(LOGGER_NAME)
    _detach_dead_handlers(logger)
    handlers_before = list(logger.handlers)
    level_before = logger.level
    yield logger
    for handler in list(logger.handlers):
        if handler not in handlers_before:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(level_before)


def _lines(path: Path, needle: str) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if needle in line]


def test_setup_logging_is_idempotent(clean_logger: logging.Logger, tmp_path: Path) -> None:
    """Calling it twice must not duplicate handlers (a record is written once)."""
    log_path = tmp_path / "bot.log"
    setup_logging(log_path, "INFO")
    setup_logging(log_path, "INFO")
    setup_logging(log_path, "DEBUG")
    clean_logger.info("recorded-once")
    assert len(_lines(log_path, "recorded-once")) == 1
    assert clean_logger.level == logging.DEBUG


def test_setup_logging_falls_back_for_an_unknown_level(
    clean_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        setup_logging(None, "LOUD")
        assert clean_logger.level == logging.INFO
    assert "unknown log level 'LOUD'" in caplog.text


@pytest.mark.parametrize(
    ("level", "expected"), [("debug", logging.DEBUG), ("WARNING", logging.WARNING), ("", logging.INFO)]
)
def test_setup_logging_resolves_level_names(
    clean_logger: logging.Logger, level: str, expected: int
) -> None:
    setup_logging(None, level)
    assert clean_logger.level == expected


def test_setup_logging_writes_to_the_configured_file(
    clean_logger: logging.Logger, tmp_path: Path
) -> None:
    log_path = tmp_path / "nested" / "bot.log"
    logger = setup_logging(log_path, "INFO")
    logger.info("hello from the test")
    assert len(_lines(log_path, "hello from the test")) == 1
    assert "INFO" in log_path.read_text(encoding="utf-8")


def test_file_handler_follows_the_path_setting(clean_logger: logging.Logger, tmp_path: Path) -> None:
    first = tmp_path / "one.log"
    second = tmp_path / "two.log"
    logger = setup_logging(first, "INFO")
    logger.info("in-the-first")

    setup_logging(second, "INFO")
    logger.info("in-the-second")

    assert len(_lines(first, "in-the-first")) == 1
    assert _lines(first, "in-the-second") == []
    assert len(_lines(second, "in-the-second")) == 1


def test_unopenable_log_file_falls_back_to_console(
    clean_logger: logging.Logger, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "a-directory"
    directory.mkdir()
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        setup_logging(directory, "INFO")  # opening a directory as a file fails
        clean_logger.info("still-logged")
    assert "cannot open log file" in caplog.text
    assert "still-logged" in caplog.text


def test_get_logger_prefixes_children(clean_logger: logging.Logger) -> None:
    assert get_logger("engine").name == "p2pbot.engine"
    assert get_logger("telegram.bot").name == "p2pbot.telegram.bot"
    assert get_logger("").name == LOGGER_NAME
    assert get_logger("   ").name == LOGGER_NAME
    assert get_logger(LOGGER_NAME).name == LOGGER_NAME
    assert get_logger(f"{LOGGER_NAME}.market").name == "p2pbot.market"


# -- redaction -------------------------------------------------------------------------
def test_redact_secrets_masks_every_configured_secret(settings) -> None:
    text = (
        "token=123456:TEST-TOKEN key=binance-key-1 cookie=binance-secret-1 owner=4242"
    )
    redacted = redact_secrets(text, settings)
    assert "TEST-TOKEN" not in redacted
    assert "binance-key-1" not in redacted
    assert "binance-secret-1" not in redacted
    assert "owner=4242" in redacted
    assert redacted.count("***") == 3


def test_redact_secrets_scans_the_environment_without_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BINANCE_1_API_KEY", "env-secret-value")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "4242")
    redacted = redact_secrets("key=env-secret-value owner=4242")
    assert "env-secret-value" not in redacted
    assert "owner=4242" in redacted


def test_redact_secrets_ignores_tiny_values_and_empty_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OKX_1_PASSPHRASE", "x")
    assert redact_secrets("text with x in it") == "text with x in it"
    assert redact_secrets("") == ""


def test_redact_secrets_handles_overlapping_values() -> None:
    settings = load_settings(
        env_path=None,
        env={"BINANCE_1_API_KEY": "abcdef", "BINANCE_1_SECRET_KEY": "abc"},
        dotenv=False,
    )
    redacted = redact_secrets("value=abcdef", settings)
    assert redacted == "value=***"


def test_redacted_settings_never_expose_credential_values(settings) -> None:
    assert "binance-key-1" not in str(settings.redacted())
