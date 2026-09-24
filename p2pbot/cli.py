"""Command line interface (SPEC section 12).

``run.py`` is the only entry point an operator needs::

    python run.py bot                       # long-poll bot + scheduler
    python run.py tick                      # one cycle: due jobs, compute, publish
    python run.py parser --scenario pln     # one market fetch pass
    python run.py rates  --scenario uah     # print the computed prices
    python run.py publish --dry-run         # create/update the advertisements
    python run.py verify-config             # validate .env accounts and every blueprint

Design notes
------------
* :func:`main` is a pure function of ``(argv, env, services)``: it returns the process exit
  code (``0`` ok, ``1`` configuration error, ``2`` runtime error) instead of exiting, so the
  whole CLI is testable without spawning a process. ``run.py`` turns that int into
  ``SystemExit``.
* An explicit ``env`` mapping replaces the ``.env`` file entirely (hermetic tests); with
  ``env=None`` the process environment is overlaid on ``.env`` as SPEC section 3 defines.
* ``services`` is the frozen :class:`~p2pbot.services.BotServices` façade. It is imported
  lazily, so ``--help``, usage errors and :func:`build_parser` never need the venue or
  Telegram layers. Passing an explicit façade therefore also keeps the CLI usable while
  ``p2pbot.exchanges``/``p2pbot.telegram`` are still landing.
* A price is printed as ``PAIR PLATFORM price source [clamped]`` and a publish attempt as
  ``ACCOUNT PAIR status price adv_no|error`` - one line per row, always.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .blueprint import Blueprint, load_blueprint
from .config import Settings, find_blueprints, load_settings
from .constants import VERSION
from .errors import BotError, ConfigError, EngineError
from .logging_setup import get_logger, setup_logging
from .market import MarketParser, MarketStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .services import BotServices

__all__ = ["build_parser", "main"]

_log = get_logger(__name__)

#: Venue names as an operator writes them; anything unknown falls back to a capitalisation.
PLATFORM_LABELS: Mapping[str, str] = {
    "binance": "Binance",
    "okx": "OKX",
    "bybit": "ByBit",
}


# ---------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The ``run.py`` parser: one subcommand per SPEC section 12 command."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "P2P advertisement manager for Binance/OKX/ByBit: compute prices from the "
            "active scenario, push them to the venues and inspect the results."
        ),
    )
    parser.add_argument("--version", action="version", version=f"p2p-ad-bot {VERSION}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    _add_command(subparsers, "bot", "long-poll the Telegram bot and run the scheduler",
                 scenario=True)
    _add_command(subparsers, "tick", "run the due jobs, then compute and publish",
                 scenario=True, dry_run=True)
    _add_command(subparsers, "parser", "run one competitor-market fetch pass",
                 scenario=True, dry_run=True)
    _add_command(subparsers, "rates", "print the computed advertisement prices",
                 scenario=True)
    _add_command(subparsers, "publish", "create or update the advertisements",
                 scenario=True, dry_run=True)
    _add_command(subparsers, "verify-config", "validate .env accounts and every blueprint")
    return parser


def _add_command(
    subparsers: Any,
    name: str,
    help_text: str,
    *,
    scenario: bool = False,
    dry_run: bool = False,
) -> argparse.ArgumentParser:
    command = subparsers.add_parser(name, help=help_text, description=help_text)
    if scenario:
        command.add_argument(
            "--scenario",
            metavar="NAME",
            default=None,
            help="blueprint to activate (default: the scenario the bot last activated)",
        )
    if dry_run:
        command.add_argument(
            "--dry-run",
            action="store_true",
            help="report what would be pushed without calling a venue or writing state",
        )
    return command


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------
def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    services: "BotServices | None" = None,
) -> int:
    """Run one CLI command.

    Args:
        argv: argument vector without the program name; ``None`` reads :data:`sys.argv`.
        env: environment mapping; ``None`` reads the process environment over ``.env``.
        services: façade to drive; ``None`` builds one with ``build_services(settings)``.

    Returns:
        ``0`` on success, ``1`` for a configuration/scenario fault, ``2`` for a runtime
        fault. Usage errors are reported by :mod:`argparse` and exit ``2`` as well.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse already printed usage/help/version
        return int(exc.code or 0)

    try:
        return _dispatch(args, env, services)
    except (ConfigError, EngineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BotError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary: one short line, never a traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _dispatch(
    args: argparse.Namespace,
    env: Mapping[str, str] | None,
    services: "BotServices | None",
) -> int:
    settings = _load_settings(env)
    setup_logging(settings.log_path, settings.log_level)

    command = args.command
    scenario = getattr(args, "scenario", None)
    dry_run = bool(getattr(args, "dry_run", False))

    if command == "verify-config":
        return _verify_config(settings)
    if command == "bot":
        return _run_bot(settings, services, scenario)

    resolved = _resolve_services(settings, services)
    if command == "rates":
        return _print_rates(resolved, scenario)
    if command == "parser":
        return _run_parser(resolved, scenario, dry_run)
    if command == "tick":
        return _run_tick(resolved, scenario, dry_run)
    return _run_publish(resolved, scenario, dry_run)


def _load_settings(env: Mapping[str, str] | None) -> Settings:
    """Settings for this run; an explicit ``env`` mapping suppresses the ``.env`` file."""
    return load_settings(env_path=".env", env=env, dotenv=env is None)


def _resolve_services(settings: Settings, services: "BotServices | None") -> "BotServices":
    """The façade to drive: the injected one, or a freshly wired :class:`BotServices`."""
    if services is not None:
        return services
    from .services import build_services  # lazy: usage/help never needs the venue layer

    return build_services(settings)


def _select_scenario(
    services: "BotServices", name: str | None, *, align_job: bool
) -> Blueprint:
    """Resolve the working scenario, activating ``name`` when the operator asked for one.

    Activating reloads and validates the blueprint, persists the choice and re-aligns the
    ``parser`` scheduler job (interval or cron) with it; without ``--scenario`` the
    scenario the bot last activated is used as-is.
    """
    if name:
        blueprint = services.scenarios.activate(name)
        align_job = True
    else:
        blueprint = services.scenarios.blueprint()
    if align_job:
        from .services import configure_parser_job  # lazy, same reason as build_services

        configure_parser_job(services, blueprint)
    else:
        _log.debug("scenario %s loaded without touching the parser job", blueprint.name)
    return blueprint


def _attach_plan(services: "BotServices", blueprint: Blueprint) -> None:
    """Give the publisher the active blueprint so ad specs carry its amounts."""
    setter = getattr(getattr(services, "publisher", None), "set_blueprint", None)
    if callable(setter):
        setter(blueprint)


# ---------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------
def _print_rates(services: "BotServices", scenario: str | None) -> int:
    """``rates``: print the computed price of every pair/platform, publishing nothing."""
    blueprint = _select_scenario(services, scenario, align_job=False)
    ads = services.compute()
    print(f"scenario {blueprint.name} ({blueprint.fiat}, {blueprint.strategy})")
    for ad in ads:
        line = f"{ad.pair} {_platform_label(ad.platform)} {ad.price} {ad.source}"
        if ad.clamped:
            line += " [clamped]"
        print(line)
    if not ads:
        print("no prices computed")
    return 0


def _run_publish(services: "BotServices", scenario: str | None, dry_run: bool) -> int:
    """``publish``: recompute every price and create/update the advertisements."""
    blueprint = _select_scenario(services, scenario, align_job=False)
    _attach_plan(services, blueprint)
    results = services.refresh_prices(dry_run=dry_run)
    problems = tuple(getattr(services, "last_problems", ()) or ())
    _print_results(results, problems)
    return _publish_exit_code(results, problems)


def _run_tick(services: "BotServices", scenario: str | None, dry_run: bool) -> int:
    """``tick``: run the due scheduler jobs, then one compute + publish cycle."""
    blueprint = _select_scenario(services, scenario, align_job=True)
    _attach_plan(services, blueprint)
    ran = services.scheduler.run_due()
    print("jobs: " + (", ".join(job.name for job in ran) if ran else "nothing due"))
    results = services.refresh_prices(dry_run=dry_run)
    problems = tuple(getattr(services, "last_problems", ()) or ())
    _print_results(results, problems)
    return _publish_exit_code(results, problems)


def _run_parser(services: "BotServices", scenario: str | None, dry_run: bool) -> int:
    """``parser``: one competitor fetch pass; ``--dry-run`` keeps the market store intact."""
    blueprint = _select_scenario(services, scenario, align_job=True)
    if dry_run:
        scratch = MarketStore(data=services.market.as_dict())
        results = MarketParser(services.adapters, scratch, services.clock).run_once(blueprint)
    else:
        results = services.run_parser()
    for row in results:
        middle = "-" if row.middle is None else str(row.middle)
        line = (
            f"{_platform_label(row.platform)} {row.pair} fetched={row.fetched} "
            f"kept={row.kept} middle={middle}"
        )
        if row.error:
            line += f" error={row.error}"
        print(line)
    if not results:
        print("no market prices to fetch for this scenario")
    return 0


def _run_bot(
    settings: Settings, services: "BotServices | None", scenario: str | None
) -> int:
    """``bot``: long-poll Telegram and run the scheduler until interrupted."""
    try:
        warnings = settings.validate(require_telegram=True)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        _log.warning(warning)

    resolved = _resolve_services(settings, services)
    blueprint = _select_scenario(resolved, scenario, align_job=True)
    _attach_plan(resolved, blueprint)

    from .telegram.api import TelegramAPI  # lazy: that package lands independently
    from .telegram.bot import BotRunner

    api = TelegramAPI(settings.telegram_api_base, settings.telegram_bot_token or "")
    runner = BotRunner(resolved, api)
    attach = getattr(runner, "attach_scheduler", None)
    if callable(attach):
        attach(resolved.scheduler)
    print(
        f"bot: scenario {blueprint.name} ({blueprint.strategy}), "
        f"polling {settings.telegram_api_base}"
    )
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("bot: stopped")
    return 0


def _verify_config(settings: Settings) -> int:
    """``verify-config``: validate the ``.env`` accounts and every blueprint in the dir."""
    problems: list[str] = []
    try:
        problems.extend(settings.validate())
    except ConfigError as exc:
        problems.append(str(exc))

    blueprints = find_blueprints(settings.scenarios_dir)
    if not blueprints:
        problems.append(f"no blueprint files in {settings.scenarios_dir}")
    for path in blueprints:
        try:
            blueprint = load_blueprint(path)
        except (ConfigError, OSError, TypeError, ValueError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        unknown = sorted(
            {
                account_id
                for plan in blueprint.pairs
                for account_id in plan.accounts
                if not settings.has_account(account_id)
            }
        )
        if unknown:
            problems.append(f"{path}: accounts missing from .env: {', '.join(unknown)}")
        print(
            f"ok {path.name}: {blueprint.name} ({blueprint.fiat}, {blueprint.strategy}), "
            f"{len(blueprint.pairs)} pair(s)"
        )

    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s) found", file=sys.stderr)
        return 1
    print(f"config ok: {len(settings.accounts)} account(s), {len(blueprints)} blueprint(s)")
    return 0


# ---------------------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------------------
def _platform_label(platform: Any) -> str:
    key = str(platform).strip().lower()
    return PLATFORM_LABELS.get(key, key.capitalize())


def _format_result(result: Any) -> str:
    """``ACCOUNT PAIR status price adv_no|error`` - one line per publish attempt."""
    price = "-" if result.price is None else str(result.price)
    tail = result.error or result.adv_no or "-"
    return f"{result.account_id} {result.pair} {result.status} {price} {tail}"


def _print_results(results: Sequence[Any], problems: Sequence[str] = ()) -> None:
    if not results:
        if problems:
            print(f"nothing to push: {problems[0]}")
        else:
            print("no advertisements to publish")
    else:
        for result in results:
            print(_format_result(result))
        if problems:
            print(f"skipped {len(problems)}: {problems[0]}")


def _publish_exit_code(results: Sequence[Any], problems: Sequence[str]) -> int:
    """``0`` when something was pushed (or nothing needed pushing), ``1`` when blocked.

    The tolerant engine path never raises, so an unattended ``publish``/``tick`` must signal
    "problems prevented every advertisement" through its exit code instead of a silent 0.
    """
    if results or not problems:
        return 0
    return 1
