"""Command line interface (SPEC section 12).

``run.py`` is the only entry point an operator needs::

    python run.py bot                       # long-poll the Telegram bot
    python run.py verify-config             # validate .env accounts and uah_config.py

The ``parser``, ``rates`` and ``pln-edits`` commands were moved to ``archive/``.

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
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .config import Settings, load_settings
from .constants import TELEGRAM_COMMANDS, VERSION
from .errors import BotError, ConfigError, EngineError
from .logging_setup import get_logger, setup_logging
from .uah_config import describe_steps, load_uah_steps

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .services import BotServices

__all__ = ["build_parser", "main"]

_log = get_logger(__name__)


# ---------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The ``run.py`` parser: one subcommand per SPEC section 12 command."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "P2P advertisement manager for Binance/OKX/ByBit: a Telegram bot that lists "
            "your buy ads and sets their rate."
        ),
    )
    parser.add_argument("--version", action="version", version=f"p2p-ad-bot {VERSION}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    _add_command(subparsers, "bot", "long-poll the Telegram bot (/getads, /setrate)")
    _add_command(subparsers, "verify-config", "validate .env accounts and uah_config.py")
    return parser


def _add_command(subparsers: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    return subparsers.add_parser(name, help=help_text, description=help_text)


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

    if args.command == "verify-config":
        return _verify_config(settings)
    return _run_bot(settings, services)


def _load_settings(env: Mapping[str, str] | None) -> Settings:
    """Settings for this run; an explicit ``env`` mapping suppresses the ``.env`` file."""
    return load_settings(env_path=".env", env=env, dotenv=env is None)


def _resolve_services(settings: Settings, services: "BotServices | None") -> "BotServices":
    """The façade to drive: the injected one, or a freshly wired :class:`BotServices`."""
    if services is not None:
        return services
    from .services import build_services  # lazy: usage/help never needs the venue layer

    return build_services(settings)


# ---------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------
def _run_bot(settings: Settings, services: "BotServices | None") -> int:
    """``bot``: long-poll Telegram until interrupted."""
    try:
        warnings = settings.validate(require_telegram=True)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        _log.warning(warning)

    resolved = _resolve_services(settings, services)

    from .telegram.api import TelegramAPI  # lazy: that package lands independently
    from .telegram.bot import BotRunner

    api = TelegramAPI(settings.telegram_api_base, settings.telegram_bot_token or "")
    runner = BotRunner(resolved, api)
    print(f"bot: polling {settings.telegram_api_base}")
    # publish the command menu Telegram shows when the owner types "/"
    register = getattr(runner, "register_commands", None)
    if callable(register):
        published = register()
        print(
            f"bot: command menu {'published' if published else 'NOT published (see the log)'}"
            f" ({len(TELEGRAM_COMMANDS)} commands)"
        )
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("bot: stopped")
    return 0


def _verify_config(settings: Settings) -> int:
    """``verify-config``: validate ``.env`` and ``uah_config.py``."""
    problems: list[str] = []
    try:
        problems.extend(settings.validate())
    except ConfigError as exc:
        problems.append(str(exc))

    try:
        steps = load_uah_steps()
    except ConfigError as exc:
        problems.append(str(exc))
    else:
        print(f"ok uah_config.py: STEP {describe_steps(steps)}")

    try:
        disabled = settings.disabled_platforms
    except ConfigError as exc:
        problems.append(str(exc))
    else:
        if disabled:
            print(f"disabled exchanges: {', '.join(sorted(disabled))} (DISABLED_EXCHANGES)")

    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s) found", file=sys.stderr)
        return 1
    print(f"config ok: {len(settings.accounts)} account(s)")
    return 0
