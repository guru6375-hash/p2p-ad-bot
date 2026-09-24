"""Configuration: .env parsing, account discovery, Settings validation (SPEC section 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from p2pbot.config import (
    DEFAULT_ADS_PATH,
    DEFAULT_LOG_PATH,
    DEFAULT_MARKET_PATH,
    DEFAULT_SCENARIOS_DIR,
    DEFAULT_STATE_PATH,
    DEFAULT_TELEGRAM_API_BASE,
    Settings,
    find_blueprints,
    is_account_key,
    is_secret_key,
    load_dotenv,
    load_settings,
    parse_accounts,
)
from p2pbot.errors import ConfigError
from p2pbot.models import Account, AccountRef


# -- load_dotenv -----------------------------------------------------------------------
def test_load_dotenv_parses_the_supported_syntax(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\ufeff# comment line\n"
        "\n"
        "PLAIN=value\n"
        "SPACED =  padded value  \n"
        "SINGLE='quoted value'\n"
        'DOUBLE="double value"\n'
        "export EXPORTED=exported\n"
        "EMPTY=\n"
        "NO_EQUALS_LINE\n"
        "=novalue\n"
        "INTERPOLATED=${PLAIN}/suffix\n",
        encoding="utf-8",
    )
    values = load_dotenv(env_file)
    assert values["PLAIN"] == "value"
    assert values["SPACED"] == "padded value"
    assert values["SINGLE"] == "quoted value"
    assert values["DOUBLE"] == "double value"
    assert values["EXPORTED"] == "exported"
    assert values["EMPTY"] == ""
    assert values["INTERPOLATED"] == "${PLAIN}/suffix"
    assert "NO_EQUALS_LINE" not in values
    assert "" not in values


def test_load_dotenv_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "absent.env") == {}


# -- account discovery -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("BINANCE_1_API_KEY", True),
        ("okx_2_passphrase", True),
        ("BYBIT_10_SESSION_COOKIE", True),
        ("EFC_11724_1592913036", False),
        ("TELEGRAM_OWNER_ID", False),
        ("BINANCE_API_KEY", False),
        ("BINANCE_1_", False),
        ("1_BINANCE_API_KEY", False),
    ],
)
def test_is_account_key(key: str, expected: bool) -> None:
    assert is_account_key(key) is expected


def test_parse_accounts_discovers_every_platform_with_uppercase_fields() -> None:
    accounts = parse_accounts(
        {
            "BINANCE_1_API_KEY": "bk1",
            "binance_1_secret_key": "bs1",
            "BINANCE_2_API_KEY": "bk2",
            "BINANCE_2_SECRET_KEY": "bs2",
            "OKX_1_API_KEY": "ok1",
            "OKX_1_SECRET_KEY": "os1",
            "OKX_1_PASSPHRASE": "op1",
            "BYBIT_1_API_KEY": "bb1",
            "BYBIT_1_SECRET_KEY": "byb1",
            "TELEGRAM_BOT_TOKEN": "token",
            "PATH": "/usr/bin",
        }
    )
    assert sorted(accounts) == ["Binance#1", "Binance#2", "Bybit#1", "Okx#1"]
    assert accounts["Binance#1"].credentials == {"API_KEY": "bk1", "SECRET_KEY": "bs1"}
    assert accounts["Binance#1"].platform == "binance"
    assert accounts["Okx#1"].credential("passphrase") == "op1"
    assert accounts["Bybit#1"].ref.index == 1


def test_parse_accounts_ignores_ambient_variables() -> None:
    accounts = parse_accounts({"EFC_11724_1592913036": "1", "PROCESSOR_1_LEVEL": "7"})
    assert accounts == {}


def test_parse_accounts_rejects_index_zero() -> None:
    with pytest.raises(ConfigError, match="invalid account key"):
        parse_accounts({"BINANCE_0_API_KEY": "k"})


# -- Settings construction and defaults ------------------------------------------------
def test_load_settings_uses_documented_defaults(tmp_path: Path) -> None:
    settings = load_settings(env_path=None, env={}, dotenv=False)
    assert settings.telegram_bot_token is None
    assert settings.telegram_owner_id is None
    assert settings.telegram_api_base == DEFAULT_TELEGRAM_API_BASE
    assert settings.state_path == Path(DEFAULT_STATE_PATH)
    assert settings.market_path == Path(DEFAULT_MARKET_PATH)
    assert settings.ads_path == Path(DEFAULT_ADS_PATH)
    assert settings.scenarios_dir == Path(DEFAULT_SCENARIOS_DIR)
    assert settings.log_path == Path(DEFAULT_LOG_PATH)
    assert settings.log_level == "INFO"
    assert settings.accounts == {}


def test_load_settings_reads_env_file_and_process_env_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_OWNER_ID=111\n"
        "LOG_LEVEL=debug\n"
        "BINANCE_1_API_KEY=from-file\n"
        "BINANCE_1_SECRET_KEY=file-secret\n",
        encoding="utf-8",
    )
    settings = load_settings(
        env_path=env_file,
        env={"TELEGRAM_OWNER_ID": "222", "BINANCE_1_API_KEY": "from-process"},
        dotenv=True,
    )
    assert settings.telegram_owner_id == 222
    assert settings.accounts["Binance#1"].credential("api_key") == "from-process"
    assert settings.accounts["Binance#1"].credential("secret_key") == "file-secret"
    assert settings.log_level == "DEBUG"


def test_load_settings_keeps_uninterpreted_switches_and_uppercases_keys() -> None:
    settings = load_settings(
        env_path=None,
        env={"scenario": "uah", "REFRESH_INTERVAL_MINUTES": "10", "custom": "kept"},
        dotenv=False,
    )
    assert settings.raw["SCENARIO"] == "uah"
    assert settings.raw["REFRESH_INTERVAL_MINUTES"] == "10"
    assert settings.raw["CUSTOM"] == "kept"


def test_load_settings_explicit_path_overrides() -> None:
    settings = load_settings(
        env_path=None,
        env={
            "STATE_PATH": "run/rates.json",
            "MARKET_PATH": "run/market.json",
            "ADS_PATH": "run/ads.json",
            "SCENARIOS_DIR": "plans",
            "TELEGRAM_API_BASE": "http://127.0.0.1:8081/",
        },
        dotenv=False,
    )
    assert settings.state_path == Path("run/rates.json")
    assert settings.market_path == Path("run/market.json")
    assert settings.ads_path == Path("run/ads.json")
    assert settings.scenarios_dir == Path("plans")
    assert settings.telegram_api_base == "http://127.0.0.1:8081/"


def test_empty_log_path_means_console_only() -> None:
    settings = load_settings(env_path=None, env={"LOG_PATH": ""}, dotenv=False)
    assert settings.log_path is None
    settings = load_settings(env_path=None, env={"LOG_PATH": "   "}, dotenv=False)
    assert settings.log_path is None


@pytest.mark.parametrize("raw_owner", ["abc", "-5", "0", "1.5", "+7"])
def test_load_settings_rejects_invalid_owner_id(raw_owner: str) -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_OWNER_ID must be a positive integer"):
        load_settings(env_path=None, env={"TELEGRAM_OWNER_ID": raw_owner}, dotenv=False)


def test_blank_owner_id_is_treated_as_unset() -> None:
    settings = load_settings(env_path=None, env={"TELEGRAM_OWNER_ID": "   "}, dotenv=False)
    assert settings.telegram_owner_id is None
    assert settings.validate()  # only the missing-token warning


# -- account access ---------------------------------------------------------------------
def test_account_lookup_accepts_any_casing(settings: Settings) -> None:
    assert settings.account("Binance#1").id == "Binance#1"
    assert settings.account("binance#1").id == "Binance#1"
    assert settings.has_account("BINANCE#1") is True
    assert settings.has_account("Binance#9") is False


def test_account_lookup_error_lists_known_accounts(settings: Settings) -> None:
    with pytest.raises(ConfigError) as excinfo:
        settings.account("Kraken#1")
    message = str(excinfo.value)
    assert "Kraken#1" in message
    assert "Binance#1" in message


def test_accounts_for_platform_is_ordered_by_index(env_factory) -> None:
    settings = load_settings(
        env_path=None,
        env=env_factory(
            **{
                "BINANCE_3_API_KEY": "k3",
                "BINANCE_3_SECRET_KEY": "s3",
                "BINANCE_1_API_KEY": "k1",
                "BINANCE_1_SECRET_KEY": "s1",
            }
        ),
        dotenv=False,
    )
    assert [account.id for account in settings.accounts_for("Binance")] == [
        "Binance#1",
        "Binance#2",
        "Binance#3",
    ]
    assert settings.accounts_for("bybit")[0].id == "Bybit#1"
    assert settings.accounts_for("kraken") == ()


# -- validation ------------------------------------------------------------------------
def test_validate_warns_about_missing_token_but_does_not_fail(settings: Settings) -> None:
    warnings = load_settings(env_path=None, env={}, dotenv=False).validate()
    assert any("TELEGRAM_BOT_TOKEN is not set" in warning for warning in warnings)


def test_validate_requires_telegram_credentials_when_asked() -> None:
    without_token = load_settings(env_path=None, env={"TELEGRAM_OWNER_ID": "5"}, dotenv=False)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN is required"):
        without_token.validate(require_telegram=True)

    without_owner = load_settings(env_path=None, env={"TELEGRAM_BOT_TOKEN": "t"}, dotenv=False)
    with pytest.raises(ConfigError, match="TELEGRAM_OWNER_ID is required"):
        without_owner.validate(require_telegram=True)


def test_validate_catches_owner_id_smuggled_into_raw() -> None:
    settings = Settings(
        telegram_bot_token="t",
        telegram_owner_id=None,
        telegram_api_base=DEFAULT_TELEGRAM_API_BASE,
        state_path=Path(DEFAULT_STATE_PATH),
        market_path=Path(DEFAULT_MARKET_PATH),
        ads_path=Path(DEFAULT_ADS_PATH),
        scenarios_dir=Path(DEFAULT_SCENARIOS_DIR),
        log_path=None,
        log_level="INFO",
        accounts={},
        raw={"TELEGRAM_OWNER_ID": "not-a-number"},
    )
    with pytest.raises(ConfigError, match="must be a positive integer"):
        settings.validate()


def test_unknown_platform_keys_are_never_discovered_as_accounts() -> None:
    """Regression: ambient env keys must not invent accounts (start-up killer)."""
    assert parse_accounts({"KRAKEN_1_API_KEY": "k", "KRAKEN_1_SECRET_KEY": "s"}) == {}
    assert parse_accounts({"EFC_11724_1592913036": "1", "PROCESSOR_1_LEVEL": "7"}) == {}
    discovered = parse_accounts({"KRAKEN_1_API_KEY": "k", "BINANCE_1_API_KEY": "b"})
    assert sorted(discovered) == ["Binance#1"]


def test_validate_accounts_still_rejects_a_programmatic_unknown_platform() -> None:
    settings = Settings(
        telegram_bot_token=None,
        telegram_owner_id=None,
        telegram_api_base=DEFAULT_TELEGRAM_API_BASE,
        state_path=Path(DEFAULT_STATE_PATH),
        market_path=Path(DEFAULT_MARKET_PATH),
        ads_path=Path(DEFAULT_ADS_PATH),
        scenarios_dir=Path(DEFAULT_SCENARIOS_DIR),
        log_path=None,
        log_level="INFO",
        accounts={
            "Kraken#1": Account(ref=AccountRef(platform="kraken", index=1), credentials={"API_KEY": "k"})
        },
        raw={},
    )
    with pytest.raises(ConfigError, match="unknown platform 'kraken'"):
        settings.validate_accounts()


def test_validate_accounts_rejects_missing_required_credential() -> None:
    settings = load_settings(
        env_path=None,
        env={"OKX_1_API_KEY": "k", "OKX_1_SECRET_KEY": "s"},
        dotenv=False,
    )
    with pytest.raises(ConfigError, match="account Okx#1 is missing required credential PASSPHRASE"):
        settings.validate_accounts()


def test_validate_accounts_warns_on_unknown_and_empty_optional_fields() -> None:
    settings = load_settings(
        env_path=None,
        env={
            "BINANCE_1_API_KEY": "k",
            "BINANCE_1_SECRET_KEY": "s",
            "BINANCE_1_WEBHOOK_URL": "https://example.invalid/hook",
            "BINANCE_1_CSRF_TOKEN": "",
        },
        dotenv=False,
    )
    warnings = settings.validate_accounts()
    assert any("unrecognised credential field WEBHOOK_URL" in warning for warning in warnings)
    assert any("declares CSRF_TOKEN but leaves it empty" in warning for warning in warnings)


def test_validate_warns_on_unknown_log_level() -> None:
    settings = load_settings(env_path=None, env={"LOG_LEVEL": "LOUD"}, dotenv=False)
    assert any("LOG_LEVEL 'LOUD' is not a known logging level" in warning for warning in settings.validate())


def test_validate_accepts_the_shipped_account_shape(settings: Settings) -> None:
    assert settings.validate() == ()


# -- secret handling -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("BINANCE_1_API_KEY", True),
        ("TELEGRAM_BOT_TOKEN", True),
        ("OKX_1_PASSPHRASE", True),
        ("TELEGRAM_OWNER_ID", False),
        ("LOG_LEVEL", False),
    ],
)
def test_is_secret_key(key: str, expected: bool) -> None:
    assert is_secret_key(key) is expected


def test_settings_redacted_masks_credentials_and_keeps_plain_values(settings: Settings) -> None:
    redacted = settings.redacted()
    assert redacted["BINANCE_1_API_KEY"] == "***"
    assert redacted["TELEGRAM_BOT_TOKEN"] == "***"
    assert redacted["TELEGRAM_OWNER_ID"] == "4242"
    assert "binance-key-1" not in json.dumps(redacted)


# -- blueprint discovery ---------------------------------------------------------------
def test_find_blueprints_returns_sorted_json_files(tmp_path: Path) -> None:
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert [path.name for path in find_blueprints(tmp_path)] == ["a.json", "b.json"]


def test_find_blueprints_handles_a_missing_directory(tmp_path: Path) -> None:
    assert find_blueprints(tmp_path / "nope") == ()
