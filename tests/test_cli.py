"""CLI: the ``bot`` and ``verify-config`` subcommands, output and exit codes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from p2pbot.cli import build_parser, main
from p2pbot.constants import TELEGRAM_COMMANDS

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeFacade:
    """Duck-typed stand-in for ``BotServices``: the CLI only hands it to the runner."""


def _env(tmp_path: Path, **overrides) -> dict[str, str]:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "TELEGRAM_OWNER_ID": "4242",
        "STATE_PATH": str(tmp_path / "var" / "state.json"),
        "MARKET_PATH": str(tmp_path / "var" / "market.json"),
        "ADS_PATH": str(tmp_path / "var" / "ads.json"),
        "LOG_PATH": "",
        "BINANCE_1_API_KEY": "k1",
        "BINANCE_1_SECRET_KEY": "s1",
        "BINANCE_2_API_KEY": "k2",
        "BINANCE_2_SECRET_KEY": "s2",
        "OKX_1_API_KEY": "ok",
        "OKX_1_SECRET_KEY": "os",
        "OKX_1_PASSPHRASE": "op",
        "BYBIT_1_API_KEY": "bk",
        "BYBIT_1_SECRET_KEY": "bs",
    }
    env.update({key.upper(): str(value) for key, value in overrides.items()})
    return env


# -- argument parsing ------------------------------------------------------------------
def test_parser_exposes_only_bot_and_verify_config() -> None:
    for name in ("bot", "verify-config"):
        assert build_parser().parse_args([name]).command == name
    for archived in ("parser", "rates", "pln-edits", "publish", "tick"):
        with pytest.raises(SystemExit):
            build_parser().parse_args([archived])


def test_missing_or_unknown_command_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([], env={}) == 2
    assert main(["nope"], env={}) == 2
    assert main(["--version"], env={}) == 0
    assert main(["--help"], env={}) == 0
    captured = capsys.readouterr()
    assert "p2p-ad-bot" in captured.out


# -- verify-config ---------------------------------------------------------------------
def test_verify_config_accepts_a_good_setup(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["verify-config"], env=_env(tmp_path)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["ok uah_config.py: STEP binance 0.25, bybit 0.01", "config ok: 4 account(s)"]


def test_verify_config_reports_invalid_settings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env = _env(tmp_path, TELEGRAM_OWNER_ID="not-a-number")
    assert main(["verify-config"], env=env) == 1
    assert "TELEGRAM_OWNER_ID must be a positive integer" in capsys.readouterr().err


def test_verify_config_reports_settings_that_raise(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env = _env(tmp_path, OKX_1_SECRET_KEY="", OKX_1_PASSPHRASE="")
    assert main(["verify-config"], env=env) == 1
    err = capsys.readouterr().err
    assert "Okx#1 is missing required credential" in err
    assert "problem(s) found" in err


def test_env_mapping_is_honoured_instead_of_the_dotenv_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An explicit ``env`` mapping is used verbatim: no ``.env`` file is read."""
    env = {"LOG_PATH": "", "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN", "TELEGRAM_OWNER_ID": "4242"}
    assert main(["verify-config"], env=env) == 0
    assert "config ok: 0 account(s)" in capsys.readouterr().out


# -- bot -------------------------------------------------------------------------------
def test_bot_requires_telegram_credentials(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["TELEGRAM_BOT_TOKEN"]
    assert main(["bot"], env=env) == 1
    assert "TELEGRAM_BOT_TOKEN is required to run the Telegram bot" in capsys.readouterr().err

    env = _env(tmp_path)
    del env["TELEGRAM_OWNER_ID"]
    assert main(["bot"], env=env) == 1
    assert "TELEGRAM_OWNER_ID is required to run the Telegram bot" in capsys.readouterr().err


def test_bot_publishes_the_menu_and_starts_the_runner(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from p2pbot.telegram import bot as bot_module

    recorded: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, services, api):
            recorded["services"] = services
            recorded["api"] = api

        def register_commands(self) -> bool:
            recorded["registered"] = True
            return True

        def run_forever(self, **kwargs) -> int:
            recorded["ran"] = True
            return 0

    monkeypatch.setattr(bot_module, "BotRunner", _FakeRunner)
    facade = FakeFacade()
    # LOG_LEVEL is unknown, so validate() reports a warning the bot path must log
    assert main(["bot"], env=_env(tmp_path, LOG_LEVEL="LOUD"), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "bot: polling https://api.telegram.org",
        f"bot: command menu published ({len(TELEGRAM_COMMANDS)} commands)",
    ]
    assert recorded["registered"] is True  # the "/" menu is published before polling
    assert recorded["ran"] is True
    assert recorded["services"] is facade


def test_bot_stops_cleanly_on_keyboard_interrupt(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from p2pbot.telegram import bot as bot_module

    class _InterruptingRunner:
        def __init__(self, services, api) -> None:
            pass

        def run_forever(self, **kwargs) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr(bot_module, "BotRunner", _InterruptingRunner)
    facade = FakeFacade()
    assert main(["bot"], env=_env(tmp_path), services=facade) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "bot: stopped"


# -- run.py ----------------------------------------------------------------------------
def test_run_py_entrypoint_reports_the_version() -> None:
    completed = subprocess.run(
        [sys.executable, "run.py", "--version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0
    assert "p2p-ad-bot" in (completed.stdout + completed.stderr)


def test_run_py_entrypoint_rejects_an_unknown_command() -> None:
    completed = subprocess.run(
        [sys.executable, "run.py", "nope"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "invalid choice" in (completed.stderr + completed.stdout)


def test_bot_reports_a_command_menu_that_could_not_be_published(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from p2pbot.telegram import bot as bot_module

    class _UnpublishedRunner:
        def __init__(self, services, api):
            pass

        def register_commands(self) -> bool:
            return False

        def run_forever(self, **kwargs) -> int:
            return 0

    monkeypatch.setattr(bot_module, "BotRunner", _UnpublishedRunner)
    facade = FakeFacade()

    assert main(["bot"], env=_env(tmp_path), services=facade) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"bot: command menu NOT published (see the log) ({len(TELEGRAM_COMMANDS)} commands)"
    )
