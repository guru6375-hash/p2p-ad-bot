"""Configuration: ``.env`` parsing, venue-account discovery and :class:`Settings`.

The module performs no network I/O. A missing ``.env`` is not an error: every key has a
documented default (SPEC section 3), and a process-environment variable always wins over
the value found in the file. Accounts are discovered generically from
``<PLATFORM>_<INDEX>_<FIELD>`` keys, so adding a second Binance account needs nothing but
two more lines in ``.env``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .constants import PLATFORMS, REDACTED, SECRET_FIELDS
from .errors import ConfigError
from .models import Account, AccountRef

__all__ = [
    "ACCOUNT_KEY_RE",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_LOG_PATH",
    "DEFAULT_MARKET_PATH",
    "DEFAULT_ADS_PATH",
    "DEFAULT_SCENARIOS_DIR",
    "DEFAULT_STATE_PATH",
    "DEFAULT_TELEGRAM_API_BASE",
    "LOG_LEVEL_NAMES",
    "OPTIONAL_CREDENTIALS",
    "REQUIRED_CREDENTIALS",
    "Settings",
    "find_blueprints",
    "is_account_key",
    "is_secret_key",
    "load_dotenv",
    "load_settings",
    "parse_accounts",
]

#: ``<PLATFORM>_<INDEX>_<FIELD>`` is the only key shape that declares a venue account.
ACCOUNT_KEY_RE = re.compile(r"^([A-Za-z]+)_(\d+)_([A-Z0-9_]+)$")

#: ``export KEY=VALUE`` prefixes are tolerated in ``.env``.
_EXPORT_RE = re.compile(r"^export\s+")

#: A Telegram user id must be a plain positive-ish decimal integer.
_INTEGER_RE = re.compile(r"^[0-9]+$")

#: Every level name :mod:`logging` understands, used by :meth:`Settings.validate`.
LOG_LEVEL_NAMES: frozenset[str] = frozenset(logging.getLevelNamesMapping())

DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_STATE_PATH = "var/state.json"
DEFAULT_MARKET_PATH = "var/market.json"
DEFAULT_ADS_PATH = "var/ads.json"
DEFAULT_SCENARIOS_DIR = "scenarios"
DEFAULT_LOG_PATH = "var/bot.log"
DEFAULT_LOG_LEVEL = "INFO"

#: Credentials a venue needs before an advertisement can be managed (SPEC section 3).
REQUIRED_CREDENTIALS: Mapping[str, tuple[str, ...]] = {
    "binance": ("API_KEY", "SECRET_KEY"),
    "okx": ("API_KEY", "SECRET_KEY", "PASSPHRASE"),
    "bybit": ("API_KEY", "SECRET_KEY"),
}

#: Credentials that are only needed by some venues/flows; absent is fine.
OPTIONAL_CREDENTIALS: Mapping[str, tuple[str, ...]] = {
    "binance": ("SESSION_COOKIE", "CSRF_TOKEN"),
    "okx": ("SESSION_COOKIE", "CSRF_TOKEN"),
    "bybit": (),
}


def load_dotenv(path: str | Path) -> dict[str, str]:
    """Parse a ``.env`` file into a mapping.

    ``KEY=VALUE`` lines only: blank lines and ``#`` comments are ignored, a leading
    ``export `` is tolerated, surrounding single/double quotes are stripped and values are
    never interpolated. A missing file yields an empty mapping.
    """
    env_path = Path(path)
    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read env file {env_path}: {exc}") from exc

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = _EXPORT_RE.sub("", line).strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        values[key] = _unquote(value.strip())
    return values


def is_account_key(key: str) -> bool:
    """True when ``key`` declares a venue account (``<PLATFORM>_<INDEX>_<FIELD>``).

    The field name must contain a letter, matching every credential name in use
    (``API_KEY``, ``SECRET_KEY``, ``PASSPHRASE``, ``SESSION_COOKIE``, ``CSRF_TOKEN``).
    Ambient variables such as Chromium's ``EFC_11724_1592913036`` match the raw shape but
    are not credentials, and treating them as accounts would poison the redactor.
    """
    match = ACCOUNT_KEY_RE.match(key.strip().upper())
    return bool(match and any(character.isalpha() for character in match.group(3)))


def parse_accounts(env: Mapping[str, str]) -> dict[str, Account]:
    """Discover venue accounts in ``env``, keyed by display id (``Binance#1``).

    Keys that do not look like an account are ignored; an account key with an invalid
    index (``BINANCE_0_API_KEY``) raises :class:`ConfigError`. Credential field names are
    stored uppercase.
    """
    collected: dict[str, dict[str, str]] = {}
    refs: dict[str, AccountRef] = {}
    for raw_key, raw_value in env.items():
        key = str(raw_key).strip().upper()
        match = ACCOUNT_KEY_RE.match(key)
        if not match or not is_account_key(key):
            continue
        platform, index_text, field_name = match.groups()
        if platform.lower() not in PLATFORMS:
            # Ambient variables can match the key shape (Windows ships e.g.
            # ``PROCESSOR_1_LEVEL``, Adobe ships ``EFC_11724_1592913036``); only supported
            # venues may declare an account, so an unrelated variable can never invent a
            # phantom account that would then fail validation. A typo in a venue name
            # surfaces as a missing account when a blueprint is validated instead.
            continue
        try:
            ref = AccountRef.parse(f"{platform}#{int(index_text)}")
        except ConfigError as exc:
            raise ConfigError(f"invalid account key {raw_key!r}: {exc}") from exc
        collected.setdefault(ref.id, {})[field_name] = str(raw_value).strip()
        refs[ref.id] = ref
    return {
        account_id: Account(ref=refs[account_id], credentials=credentials)
        for account_id, credentials in collected.items()
    }


def is_secret_key(key: str) -> bool:
    """True when the env key holds a value that must never be logged."""
    upper = key.strip().upper()
    if is_account_key(upper):
        return True
    return any(field in upper for field in SECRET_FIELDS)


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration.

    ``raw`` is the *complete* merged mapping (process environment overlaid on the ``.env``
    file) with uppercase keys, so optional switches the loader does not interpret itself
    (``SCENARIO``, ``REFRESH_INTERVAL_MINUTES``, …) stay reachable. :meth:`redacted` masks
    the credential-shaped entries of it.
    """

    telegram_bot_token: str | None
    telegram_owner_id: int | None
    telegram_api_base: str
    state_path: Path
    market_path: Path
    ads_path: Path
    scenarios_dir: Path
    log_path: Path | None
    log_level: str
    accounts: Mapping[str, Account]
    raw: Mapping[str, str]

    def account(self, account_id: str) -> Account:
        """The account with this id, e.g. ``"Binance#1"``."""
        account = self.accounts.get(account_id)
        if account is not None:
            return account
        try:
            canonical = AccountRef.parse(account_id).id
        except ConfigError:
            canonical = account_id
        account = self.accounts.get(canonical)
        if account is not None:
            return account
        known = ", ".join(sorted(self.accounts)) or "none"
        raise ConfigError(f"unknown account {account_id!r}; known accounts: {known}")

    def accounts_for(self, platform: str) -> tuple[Account, ...]:
        """Every account of ``platform``, ordered by index."""
        wanted = platform.strip().lower()
        matching = [account for account in self.accounts.values() if account.platform == wanted]
        return tuple(sorted(matching, key=lambda account: account.ref.index))

    def has_account(self, account_id: str) -> bool:
        """True when ``account_id`` was discovered."""
        try:
            self.account(account_id)
        except ConfigError:
            return False
        return True

    def redacted(self) -> dict[str, str]:
        """``raw`` with every credential value replaced by ``***``."""
        return {
            key: (REDACTED if is_secret_key(key) else value)
            for key, value in self.raw.items()
        }

    def validate_accounts(self) -> tuple[str, ...]:
        """Check the discovered accounts; returns warnings, raises :class:`ConfigError`.

        Hard faults: an unknown platform prefix and a missing required credential.
        """
        warnings: list[str] = []
        for account_id in sorted(self.accounts):
            account = self.accounts[account_id]
            platform = account.platform
            if platform not in PLATFORMS:
                raise ConfigError(
                    f"account {account_id} uses unknown platform {platform!r}; "
                    f"known platforms: {', '.join(PLATFORMS)}"
                )
            known = REQUIRED_CREDENTIALS[platform] + OPTIONAL_CREDENTIALS[platform]
            for field in sorted(account.credentials):
                if field not in known:
                    warnings.append(
                        f"account {account_id} declares unrecognised credential field {field} "
                        f"for platform {platform}"
                    )
            for field in REQUIRED_CREDENTIALS[platform]:
                if not account.credential(field):
                    raise ConfigError(f"account {account_id} is missing required credential {field}")
            for field in OPTIONAL_CREDENTIALS[platform]:
                if field in account.credentials and not account.credential(field):
                    warnings.append(f"account {account_id} declares {field} but leaves it empty")
        return tuple(warnings)

    def validate(self, require_telegram: bool = False) -> tuple[str, ...]:
        """Validate the whole configuration; returns human-readable warnings.

        Raises :class:`ConfigError` for hard faults: an invalid ``TELEGRAM_OWNER_ID``, a
        discovered account with an unknown platform or a missing required credential, and
        — when ``require_telegram`` is set — a missing bot token or owner id.
        """
        warnings: list[str] = []
        owner_raw = str(self.raw.get("TELEGRAM_OWNER_ID", "") or "").strip()
        if self.telegram_owner_id is None and owner_raw:
            raise ConfigError(f"TELEGRAM_OWNER_ID must be a positive integer, got {owner_raw!r}")
        if require_telegram:
            if not self.telegram_bot_token:
                raise ConfigError("TELEGRAM_BOT_TOKEN is required to run the Telegram bot")
            if self.telegram_owner_id is None:
                raise ConfigError("TELEGRAM_OWNER_ID is required to run the Telegram bot")
        elif not self.telegram_bot_token:
            warnings.append("TELEGRAM_BOT_TOKEN is not set: the Telegram commands are unavailable")

        warnings.extend(self.validate_accounts())

        if self.log_level.upper() not in LOG_LEVEL_NAMES:
            warnings.append(f"LOG_LEVEL {self.log_level!r} is not a known logging level; INFO is used")
        return tuple(warnings)


def load_settings(
    env_path: str | Path | None = ".env",
    env: Mapping[str, str] | None = None,
    dotenv: bool = True,
) -> Settings:
    """Build :class:`Settings` from the process environment and a ``.env`` file.

    Args:
        env_path: file to read; ignored when ``dotenv`` is false or the path is ``None``.
        env: process environment to use instead of :data:`os.environ` (tests/CLI).
        dotenv: set false to read the process environment only.
    """
    file_values: dict[str, str] = {}
    if dotenv and env_path is not None:
        file_values = load_dotenv(env_path)
    process_values = dict(os.environ if env is None else env)

    merged: dict[str, str] = {}
    for source in (file_values, process_values):
        for key, value in source.items():
            merged[str(key).strip().upper()] = str(value)

    accounts = parse_accounts(merged)
    raw: dict[str, str] = dict(merged)

    token = _optional(merged, "TELEGRAM_BOT_TOKEN")
    owner = _parse_owner(merged.get("TELEGRAM_OWNER_ID"))
    api_base = _optional(merged, "TELEGRAM_API_BASE") or DEFAULT_TELEGRAM_API_BASE
    state_path = Path(_optional(merged, "STATE_PATH") or DEFAULT_STATE_PATH)
    market_path = Path(_optional(merged, "MARKET_PATH") or DEFAULT_MARKET_PATH)
    ads_path = Path(_optional(merged, "ADS_PATH") or DEFAULT_ADS_PATH)
    scenarios_dir = Path(_optional(merged, "SCENARIOS_DIR") or DEFAULT_SCENARIOS_DIR)
    log_path = _parse_log_path(merged.get("LOG_PATH"))
    log_level = (_optional(merged, "LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper()

    return Settings(
        telegram_bot_token=token,
        telegram_owner_id=owner,
        telegram_api_base=api_base,
        state_path=state_path,
        market_path=market_path,
        ads_path=ads_path,
        scenarios_dir=scenarios_dir,
        log_path=log_path,
        log_level=log_level,
        accounts=accounts,
        raw=raw,
    )


def find_blueprints(directory: str | Path) -> tuple[Path, ...]:
    """Every ``*.json`` blueprint in ``directory``, sorted by name."""
    try:
        return tuple(sorted(Path(directory).glob("*.json")))
    except OSError as exc:
        raise ConfigError(f"cannot list blueprints in {directory}: {exc}") from exc


def _optional(env: Mapping[str, str], key: str) -> str | None:
    value = str(env.get(key, "") or "").strip()
    return value or None


def _parse_owner(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if not _INTEGER_RE.match(text) or int(text) <= 0:
        raise ConfigError(f"TELEGRAM_OWNER_ID must be a positive integer, got {text!r}")
    return int(text)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_log_path(raw: str | None) -> Path | None:
    """``LOG_PATH``: absent -> ``var/bot.log``; present but empty -> console only."""
    if raw is None:
        return Path(DEFAULT_LOG_PATH)
    text = str(raw).strip()
    return Path(text) if text else None
