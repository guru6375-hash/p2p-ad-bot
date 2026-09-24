"""CLI: every subcommand, --scenario/--dry-run, output format and exit codes (SPEC 12)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot.blueprint import Blueprint, load_blueprint, load_blueprint_by_name
from p2pbot.cli import build_parser, main
from p2pbot.errors import ConfigError, ExchangeError, MissingCapError, TransportError
from p2pbot.market import MarketFetchResult, MarketStore, build_snapshot
from p2pbot.models import ComputedAd, Pair, PublishResult
from p2pbot.scheduler import Job, Scheduler

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def _computed(
    *,
    pair: str = "UAH/USDT",
    platform: str = "binance",
    price: str = "47.00",
    source: str = "base_rate",
    clamped: bool = False,
) -> ComputedAd:
    return ComputedAd(
        pair=Pair.parse(pair),
        platform=platform,
        price=Decimal(price),
        source=source,
        cap=Decimal("47.20"),
        accounts=("Binance#1",),
        clamped=clamped,
    )


class _StubAdapter:
    def __init__(self, platform: str, ads=()) -> None:
        self.platform = platform
        self.ads = tuple(ads)

    def search_ads(self, pair, *, filters=None, **kwargs):
        return build_snapshot(self.platform, pair, self.ads, filters)


class _FakePublisher:
    def __init__(self) -> None:
        self.blueprints: list[object] = []

    def set_blueprint(self, blueprint) -> None:
        self.blueprints.append(blueprint)


class _FakeScenarios:
    def __init__(self, blueprint: Blueprint | None) -> None:
        self._blueprint = blueprint
        self.activated: list[str] = []

    def available(self) -> tuple[str, ...]:
        return ("pln", "uah")

    def active_name(self) -> str | None:
        return None if self._blueprint is None else self._blueprint.name

    def activate(self, name: str) -> Blueprint:
        blueprint = load_blueprint_by_name(name, SCENARIOS)
        self.activated.append(name)
        self._blueprint = blueprint
        return blueprint

    def blueprint(self) -> Blueprint:
        if self._blueprint is None:
            raise ConfigError("no active scenario; available: pln, uah")
        return self._blueprint

    def reload(self) -> Blueprint:
        return self.blueprint()


class _StubScheduler:
    """Scheduler double reporting a fixed set of due jobs (no rescheduling)."""

    def __init__(self, due: tuple[str, ...] = ("parser",)) -> None:
        self._due = due
        self.ran: list[datetime | None] = []
        self.registered: list[object] = []

    def jobs(self) -> tuple:
        return tuple(self.registered)

    def add(self, job):
        self.registered.append(job)
        return job

    def remove(self, name):
        self.registered = [job for job in self.registered if job.name != name]
        return None

    def get(self, name):
        for job in self.registered:
            if job.name == name:
                return job
        raise KeyError(name)

    def run_due(self, now=None):
        self.ran.append(now)
        return tuple(type("Ran", (), {"name": name})() for name in self._due)


class FakeFacade:
    """Duck-typed stand-in for ``BotServices`` covering everything the CLI touches."""

    def __init__(
        self,
        blueprint: Blueprint | None,
        *,
        ads: tuple[ComputedAd, ...] = (),
        publish: tuple[PublishResult, ...] = (),
        parser_rows: tuple[MarketFetchResult, ...] = (),
        clock: datetime | None = None,
        scheduler=None,
    ) -> None:
        self.scenarios = _FakeScenarios(blueprint)
        self.scheduler = scheduler or Scheduler(lambda: clock or datetime(2026, 1, 1, tzinfo=UTC))
        self.clock = lambda: clock or datetime(2026, 1, 1, tzinfo=UTC)
        self.adapters = {
            "binance": _StubAdapter("binance"),
            "okx": _StubAdapter("okx", ads=()),
            "bybit": _StubAdapter("bybit", ads=()),
        }
        self.market = MarketStore()
        self.publisher = _FakePublisher()
        self.ads = ads
        self.publish_results = publish
        self.parser_rows = parser_rows
        self.compute_error: BaseException | None = None
        self.publish_error: BaseException | None = None
        self.parser_error: BaseException | None = None
        self.refresh_calls: list[bool] = []
        self.compute_calls = 0

    @property
    def scenario_name(self) -> str | None:
        return self.scenarios._blueprint.name if self.scenarios._blueprint else None

    def compute(self) -> tuple[ComputedAd, ...]:
        self.compute_calls += 1
        if self.compute_error is not None:
            raise self.compute_error
        return self.ads

    def refresh_prices(self, *, dry_run: bool = False) -> tuple[PublishResult, ...]:
        self.refresh_calls.append(dry_run)
        if self.publish_error is not None:
            raise self.publish_error
        return self.publish_results

        self.last_problems: tuple[str, ...] = ()

    def run_parser(self, pairs=None):
        if self.parser_error is not None:
            raise self.parser_error
        return self.parser_rows


def _env(tmp_path: Path, **overrides) -> dict[str, str]:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "TELEGRAM_OWNER_ID": "4242",
        "STATE_PATH": str(tmp_path / "var" / "state.json"),
        "MARKET_PATH": str(tmp_path / "var" / "market.json"),
        "ADS_PATH": str(tmp_path / "var" / "ads.json"),
        "SCENARIOS_DIR": str(SCENARIOS),
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


def _scenarios_copy(tmp_path: Path) -> Path:
    target = tmp_path / "scenarios"
    target.mkdir()
    for name in ("uah.json", "pln.json"):
        (target / name).write_text((SCENARIOS / name).read_text(encoding="utf-8"), encoding="utf-8")
    return target


# -- argument parsing ------------------------------------------------------------------
def test_parser_exposes_every_documented_command() -> None:
    for name in ("bot", "tick", "parser", "rates", "publish", "verify-config"):
        assert build_parser().parse_args([name]).command == name
    assert build_parser().parse_args(["rates", "--scenario", "uah"]).scenario == "uah"
    assert build_parser().parse_args(["rates"]).scenario is None
    assert build_parser().parse_args(["publish"]).dry_run is False
    assert build_parser().parse_args(["publish", "--dry-run"]).dry_run is True


def test_missing_or_unknown_command_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([], env={}) == 2
    assert main(["nope"], env={}) == 2
    assert main(["--version"], env={}) == 0
    assert main(["--help"], env={}) == 0
    captured = capsys.readouterr()
    assert "p2p-ad-bot" in captured.out


# -- rates -----------------------------------------------------------------------------
def test_rates_prints_the_computed_prices(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(
        load_blueprint(SCENARIOS / "uah.json"),
        ads=(
            _computed(price="47.00"),
            _computed(pair="UAH/USDC", price="46.75", source="base_rate_minus_spread"),
            _computed(platform="okx", pair="UAH/USDC", price="46.50", clamped=True),
        ),
    )
    assert main(["rates"], env=_env(tmp_path), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "scenario uah (UAH, fixed_spread)"
    assert out[1] == "UAH/USDT Binance 47.00 base_rate"
    assert out[2] == "UAH/USDC Binance 46.75 base_rate_minus_spread"
    assert out[3] == "UAH/USDC OKX 46.50 base_rate [clamped]"


def test_rates_without_prices(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "pln.json"))
    assert main(["rates"], env=_env(tmp_path), services=facade) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "no prices computed"


def test_rates_activates_the_requested_scenario(tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    assert main(["rates", "--scenario", "pln"], env=_env(tmp_path), services=facade) == 0
    assert facade.scenarios.activated == ["pln"]
    assert facade.scenario_name == "pln"


def test_a_configuration_fault_exits_one(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    facade.compute_error = MissingCapError("no cap_rate stored for UAH/USDT")
    assert main(["rates"], env=_env(tmp_path), services=facade) == 1
    assert "error: no cap_rate stored for UAH/USDT" in capsys.readouterr().err


def test_a_runtime_fault_exits_two(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    facade.compute_error = ExchangeError("venue exploded")
    assert main(["rates"], env=_env(tmp_path), services=facade) == 2
    assert "error: ExchangeError: venue exploded" in capsys.readouterr().err

    unexpected = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    unexpected.compute_error = RuntimeError("boom")
    assert main(["rates"], env=_env(tmp_path), services=unexpected) == 2
    assert "error: RuntimeError: boom" in capsys.readouterr().err


def test_no_active_scenario_is_a_configuration_fault(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    facade = FakeFacade(None)
    assert main(["rates"], env=_env(tmp_path), services=facade) == 1
    assert "no active scenario" in capsys.readouterr().err


# -- publish ---------------------------------------------------------------------------
def test_publish_prints_one_line_per_attempt(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(
        load_blueprint(SCENARIOS / "uah.json"),
        publish=(
            PublishResult(
                account_id="Binance#1",
                platform="binance",
                pair=Pair.parse("UAH/USDT"),
                status="created",
                price=Decimal("47.00"),
                adv_no="2048",
            ),
            PublishResult(
                account_id="Okx#1",
                platform="okx",
                pair=Pair.parse("UAH/USDT"),
                status="error",
                price=Decimal("47.00"),
                error="TransportError: reset",
            ),
        ),
    )
    assert main(["publish"], env=_env(tmp_path), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "Binance#1 UAH/USDT created 47.00 2048",
        "Okx#1 UAH/USDT error 47.00 TransportError: reset",
    ]
    assert facade.refresh_calls == [False]
    assert facade.publisher.blueprints == [facade.scenarios.blueprint()]


def test_publish_dry_run_is_forwarded(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(
        load_blueprint(SCENARIOS / "uah.json"),
        publish=(
            PublishResult(
                account_id="Binance#1",
                platform="binance",
                pair=Pair.parse("UAH/USDT"),
                status="dry_run",
                price=Decimal("47.00"),
                dry_run=True,
            ),
        ),
    )
    assert main(["publish", "--dry-run"], env=_env(tmp_path), services=facade) == 0
    assert facade.refresh_calls == [True]
    assert capsys.readouterr().out.splitlines() == ["Binance#1 UAH/USDT dry_run 47.00 -"]


def test_publish_without_attempts(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    assert main(["publish"], env=_env(tmp_path), services=facade) == 0
    assert capsys.readouterr().out.splitlines() == ["no advertisements to publish"]


def test_publish_reports_a_facade_fault(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    facade.publish_error = TransportError("all venues down")
    assert main(["publish"], env=_env(tmp_path), services=facade) == 2
    assert "error: TransportError: all venues down" in capsys.readouterr().err


# -- tick ------------------------------------------------------------------------------
def test_tick_runs_the_job_then_publishes(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    scheduler = _StubScheduler(due=("parser",))
    facade = FakeFacade(
        load_blueprint(SCENARIOS / "pln.json"),
        publish=(
            PublishResult(
                account_id="Binance#1",
                platform="binance",
                pair=Pair.parse("PLN/USDT"),
                status="updated",
                price=Decimal("4.31"),
                adv_no="9",
            ),
        ),
        scheduler=scheduler,
    )
    assert main(["tick"], env=_env(tmp_path), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "jobs: parser"
    assert out[1] == "Binance#1 PLN/USDT updated 4.31 9"
    assert len(scheduler.ran) == 1


def test_tick_with_nothing_due_still_publishes(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))  # parser disabled -> job removed
    assert main(["tick"], env=_env(tmp_path), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "jobs: nothing due"
    assert out[1] == "no advertisements to publish"


def test_a_scenario_alignment_leaves_the_parser_job_scheduled(tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"), clock=datetime(2026, 1, 1, tzinfo=UTC))
    facade.scheduler.add(Job.every("parser", lambda: None, 25))
    assert main(["tick", "--scenario", "pln"], env=_env(tmp_path), services=facade) == 0
    job = facade.scheduler.get("parser")
    assert job.next_run_at is not None
    assert facade.scheduler.due(datetime(2026, 1, 1, 0, 25, tzinfo=UTC)) == (job,)


def test_tick_reports_the_jobs_that_ran(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    scheduler = _StubScheduler(due=("parser", "prices"))
    facade = FakeFacade(load_blueprint(SCENARIOS / "pln.json"), scheduler=scheduler)
    assert main(["tick"], env=_env(tmp_path), services=facade) == 0
    assert capsys.readouterr().out.splitlines()[0] == "jobs: parser, prices"


def test_tick_dry_run_is_forwarded(tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "pln.json"))
    assert main(["tick", "--dry-run"], env=_env(tmp_path), services=facade) == 0
    assert facade.refresh_calls[-1] is True


# -- parser ----------------------------------------------------------------------------
def test_parser_prints_each_venue(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(
        load_blueprint(SCENARIOS / "pln.json"),
        parser_rows=(
            MarketFetchResult(
                platform="binance",
                pair=Pair.parse("PLN/USDT"),
                fetched=3,
                kept=1,
                middle=Decimal("3.84"),
            ),
            MarketFetchResult(
                platform="okx", pair=Pair.parse("PLN/USDC"), error="ApiError: no ads"
            ),
        ),
    )
    assert main(["parser"], env=_env(tmp_path), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Binance PLN/USDT fetched=3 kept=1 middle=3.84"
    assert out[1] == "OKX PLN/USDC fetched=0 kept=0 middle=- error=ApiError: no ads"


def test_parser_without_targets(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    assert main(["parser"], env=_env(tmp_path), services=facade) == 0
    assert capsys.readouterr().out.splitlines() == ["no market prices to fetch for this scenario"]


def test_parser_dry_run_keeps_the_market_store_untouched(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from p2pbot.models import Pair as _Pair

    facade = FakeFacade(load_blueprint(SCENARIOS / "pln.json"))
    facade.market.put(
        build_snapshot("binance", _Pair.parse("PLN/USDT"), [], fetched_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    before = facade.market.as_dict()

    assert main(["parser", "--dry-run"], env=_env(tmp_path), services=facade) == 0

    assert facade.market.as_dict() == before  # scratch store only
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 4
    assert all("fetched=0 kept=0 middle=-" in line for line in out)
    assert out[0].startswith("Binance PLN/USDT")


def test_parser_reports_a_transport_failure(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    facade = FakeFacade(load_blueprint(SCENARIOS / "pln.json"))
    facade.parser_error = TransportError("dns failure")
    assert main(["parser"], env=_env(tmp_path), services=facade) == 2
    assert "error: TransportError: dns failure" in capsys.readouterr().err


# -- verify-config ---------------------------------------------------------------------
def test_verify_config_accepts_a_good_setup(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    scenarios = _scenarios_copy(tmp_path)
    assert main(["verify-config"], env=_env(tmp_path, SCENARIOS_DIR=str(scenarios))) == 0
    out = capsys.readouterr().out
    assert "ok pln.json: pln (PLN, market_middle), 2 pair(s)" in out
    assert "ok uah.json: uah (UAH, fixed_spread), 2 pair(s)" in out
    assert "config ok: 4 account(s), 2 blueprint(s)" in out


def test_verify_config_reports_a_broken_blueprint(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scenarios = _scenarios_copy(tmp_path)
    (scenarios / "broken.json").write_text("{not json", encoding="utf-8")
    assert main(["verify-config"], env=_env(tmp_path, SCENARIOS_DIR=str(scenarios))) == 1
    captured = capsys.readouterr()
    assert "is not valid JSON" in captured.err
    assert "1 problem(s) found" in captured.err
    assert "ok uah.json" in captured.out


def test_verify_config_reports_a_missing_scenarios_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["verify-config"], env=_env(tmp_path, SCENARIOS_DIR=str(tmp_path / "nope"))) == 1
    assert "no blueprint files in" in capsys.readouterr().err


def test_verify_config_reports_accounts_missing_from_env(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scenarios = _scenarios_copy(tmp_path)
    env = _env(tmp_path, SCENARIOS_DIR=str(scenarios))
    for key in list(env):
        if key.startswith("BYBIT"):
            del env[key]
    assert main(["verify-config"], env=env) == 1
    err = capsys.readouterr().err
    assert "accounts missing from .env: Bybit#1" in err


def test_verify_config_reports_invalid_settings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scenarios = _scenarios_copy(tmp_path)
    env = _env(tmp_path, SCENARIOS_DIR=str(scenarios), TELEGRAM_OWNER_ID="not-a-number")
    assert main(["verify-config"], env=env) == 1
    assert "TELEGRAM_OWNER_ID must be a positive integer" in capsys.readouterr().err


def test_verify_config_does_not_need_the_façade(tmp_path: Path) -> None:
    """No services/build_services call is made for a pure validation run."""
    scenarios = _scenarios_copy(tmp_path)
    assert main(["verify-config"], env=_env(tmp_path, SCENARIOS_DIR=str(scenarios))) == 0


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


def test_bot_starts_the_runner_and_attaches_the_scheduler(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from p2pbot.telegram import bot as bot_module

    recorded: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, services, api):
            recorded["services"] = services
            recorded["api"] = api

        def attach_scheduler(self, scheduler) -> None:
            recorded["scheduler"] = scheduler

        def run_forever(self, **kwargs) -> int:
            recorded["ran"] = True
            return 0

    monkeypatch.setattr(bot_module, "BotRunner", _FakeRunner)
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
    # LOG_LEVEL is unknown, so validate() reports a warning the bot path must log
    assert main(["bot"], env=_env(tmp_path, LOG_LEVEL="LOUD"), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["bot: scenario uah (fixed_spread), polling https://api.telegram.org"]
    assert recorded["ran"] is True
    assert recorded["scheduler"] is facade.scheduler
    assert recorded["services"] is facade


def test_bot_stops_cleanly_on_keyboard_interrupt(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from p2pbot.telegram import bot as bot_module

    class _InterruptingRunner:
        def __init__(self, services, api) -> None:
            pass

        def attach_scheduler(self, scheduler) -> None:
            pass

        def run_forever(self, **kwargs) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr(bot_module, "BotRunner", _InterruptingRunner)
    facade = FakeFacade(load_blueprint(SCENARIOS / "uah.json"))
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


def test_env_mapping_is_honoured_instead_of_the_dotenv_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An explicit ``env`` mapping is used verbatim: no ``.env`` file is read."""
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "bare.json").write_text(
        json.dumps(
            {
                "version": 1,
                "name": "bare",
                "fiat": "UAH",
                "strategy": "fixed_spread",
                "pairs": [{"pair": "UAH/USDT", "anchor": True}],
            }
        ),
        encoding="utf-8",
    )
    env = {
        "SCENARIOS_DIR": str(scenarios),
        "LOG_PATH": "",
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "TELEGRAM_OWNER_ID": "4242",
    }
    assert main(["verify-config"], env=env) == 0
    assert "config ok: 0 account(s), 1 blueprint(s)" in capsys.readouterr().out


# -- wiring without an injected façade -------------------------------------------------
def test_rates_builds_the_real_façade_when_none_is_injected(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """No injected services -> the CLI wires build_services itself (no venue call)."""
    env = _env(tmp_path, SCENARIO="uah")
    assert main(["rates"], env=env) == 1
    err = capsys.readouterr().err
    assert "error: no base_rate stored for UAH/USDT" in err


def test_publish_signals_blocked_entries_through_its_exit_code(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Missing rates are skipped entries; with nothing pushed the command must exit 1."""
    env = _env(tmp_path, SCENARIO="uah")
    assert main(["publish", "--dry-run"], env=env) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "nothing to push: UAH/USDT binance: MissingRateError: no base_rate stored for "
        "UAH/USDT (needed by platform binance)"
    ]


def test_tick_signals_blocked_entries_through_its_exit_code(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env = _env(tmp_path, SCENARIO="uah")
    assert main(["tick"], env=env) == 1
    out = capsys.readouterr().out.splitlines()
    # uah has the parser disabled, so aligning the scenario removes the job
    assert out[0] == "jobs: nothing due"
    assert out[1].startswith("nothing to push: UAH/USDT binance: MissingRateError")


def test_publish_lists_the_skipped_entries_next_to_the_results(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    facade = FakeFacade(
        load_blueprint(SCENARIOS / "uah.json"),
        publish=(
            PublishResult(
                account_id="Binance#1",
                platform="binance",
                pair=Pair.parse("UAH/USDT"),
                status="updated",
                price=Decimal("47.00"),
                adv_no="7",
            ),
        ),
    )
    facade.last_problems = ("UAH/USDC okx: MissingMarketDataError: no data",)
    assert main(["publish"], env=_env(tmp_path), services=facade) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Binance#1 UAH/USDT updated 47.00 7"
    assert out[1] == "skipped 1: UAH/USDC okx: MissingMarketDataError: no data"


def test_verify_config_reports_settings_that_raise(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scenarios = _scenarios_copy(tmp_path)
    env = _env(tmp_path, SCENARIOS_DIR=str(scenarios), OKX_1_SECRET_KEY="", OKX_1_PASSPHRASE="")
    assert main(["verify-config"], env=env) == 1
    err = capsys.readouterr().err
    assert "Okx#1 is missing required credential" in err
    assert "problem(s) found" in err
