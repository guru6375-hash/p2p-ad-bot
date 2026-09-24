"""Command router for the owner-only bot: text in, reply text out.

Every handler is a pure function of ``(context, args)`` and performs **no Telegram I/O**;
the runner is responsible for sending :class:`HandlerResult.text`. That split is what
makes the whole command surface testable without a socket.

The router talks to the frozen façade (SPEC 11.5) and nothing else - it never imports
``p2pbot.services``/``publisher``/``engine``. Façade faults (missing rates or caps,
unknown scenario, transport failures) are reported back to the owner as
``<ExceptionType>: <message>``; a traceback never reaches Telegram and no secret is
echoed.

``/setbase`` and ``/setcap`` push prices immediately (``refresh_prices()``) because a
lowered cap must bite at once - the hard rule is that an advertisement price never
exceeds ``cap_rate``. When that refresh cannot run (no cap stored yet, no active
scenario, missing market data, venue failure) the rate stays written and the reply says
``ads not updated: <TypeName>: <message>``.

A venue with no competitor data for a pair is a *skip*, not a fault: the engine reports
those ``(pair, platform)`` entries in ``StatusSnapshot.engine_problems`` (and in
``services.last_problems`` after a refresh). ``/status`` and ``/rates`` append a
``skipped <N>:`` block for them, ``/publish``/``/pause``/``/resume`` append one
``skipped <N>: <first>`` line, and when nothing at all could be pushed the reply says
``nothing to push: <first>`` instead of a bare ``no results``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .. import constants
from ..errors import ConfigError, EngineError, ExchangeError
from ..models import Pair, parse_decimal, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .api import Update

__all__ = [
    "Dispatcher",
    "HandlerResult",
    "HELP_TEXT",
    "command",
    "render_results",
    "render_snapshot",
]

_LOGGER = logging.getLogger(__name__)

_USAGE_SETBASE = "Usage: /setbase <PAIR> <RATE>"
_USAGE_SETCAP = "Usage: /setcap <PAIR> <RATE>"
_USAGE_SCENARIO = "Usage: /scenario <NAME>"
_USAGE_PARSE = "Usage: /parse [<PAIR>]"
_USAGE_PUBLISH = "Usage: /publish [--dry]"
_USAGE_PAUSE = "Usage: /pause [<PAIR>]"
_USAGE_RESUME = "Usage: /resume [<PAIR>]"

_CAP_RULE = (
    "advertisement prices can never exceed the cap: the engine clamps every computed "
    "price to it and the publisher re-asserts it before each request."
)

#: How many skipped-entry problems a reply lists before summarising the rest.
_MAX_PROBLEM_LINES = 10

#: Shown for an unknown command or for free text.
HELP_HINT = "Unknown command. Send /help for the command list."


def _build_help() -> str:
    lines = [
        "P2P ad manager (owner only). Files and attachments are never accepted.",
        "commands:",
    ]
    lines.extend(f"/{name} - {description}" for name, description in constants.TELEGRAM_COMMANDS)
    return "\n".join(lines)


HELP_TEXT = _build_help()


@dataclass(frozen=True)
class HandlerResult:
    """The reply for one update; ``silent`` suppresses the reply entirely."""

    text: str
    silent: bool = False


@dataclass(frozen=True)
class _Context:
    """What a handler is allowed to see: the façade, the start time and a logger."""

    services: Any
    started_at: datetime
    logger: logging.Logger

    def now(self) -> datetime:
        clock = getattr(self.services, "clock", None)
        return clock() if callable(clock) else utcnow()


_Handler = Callable[[_Context, list[str]], HandlerResult]

#: Command name (no slash, lowercase) -> handler. Populated by :func:`command`.
_COMMANDS: dict[str, _Handler] = {}


def command(name: str) -> Callable[[_Handler], _Handler]:
    """Register a handler for ``name``; several names may share one handler."""

    def decorate(func: _Handler) -> _Handler:
        _COMMANDS[name] = func
        return func

    return decorate


# ----- formatting helpers (shared by /status, /rates, /parse, /publish) --------------


def _fmt(value: Any) -> str:
    """Render an optional Decimal-like value, ``-`` when absent."""
    return "-" if value is None else str(value)


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _format_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def _rate_lines(rows: Iterable[Any]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"  {getattr(row, 'pair', '?')} base {_fmt(getattr(row, 'base', None))} "
            f"cap {_fmt(getattr(row, 'cap', None))}"
        )
    return lines


def _price_lines(rows: Iterable[Any]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        line = (
            f"  {getattr(row, 'pair', '?')} {getattr(row, 'platform', '?')} "
            f"{_fmt(getattr(row, 'price', None))} {getattr(row, 'source', '?')}"
        )
        if getattr(row, "clamped", False):
            line += " [clamped]"
        lines.append(line)
    return lines


def _market_lines(rows: Iterable[Any], now: datetime) -> list[str]:
    lines: list[str] = []
    for row in rows:
        fetched = _as_datetime(getattr(row, "fetched_at", None))
        if fetched is None:
            age = "never"
        else:
            elapsed = now - fetched
            age = f"{_format_duration(elapsed)} ago"
            if elapsed > timedelta(minutes=constants.DEFAULT_PARSER_INTERVAL_MINUTES):
                age += " [stale]"
        lines.append(
            f"  {getattr(row, 'platform', '?')} {getattr(row, 'pair', '?')} "
            f"middle {_fmt(getattr(row, 'middle', None))} "
            f"filtered {getattr(row, 'filtered', 0)} fetched {age}"
        )
    return lines


def _job_lines(rows: Iterable[Any], now: datetime) -> list[str]:
    lines: list[str] = []
    for row in rows:
        next_run = _as_datetime(getattr(row, "next_run_at", None))
        when = "never" if next_run is None else f"{next_run.isoformat()} (in {_format_duration(next_run - now)})"
        line = f"  {getattr(row, 'name', '?')} next_run {when}"
        error = getattr(row, "last_error", None)
        if error:
            line += f" last_error {error}"
        lines.append(line)
    return lines


def _result_lines(results: Iterable[Any]) -> list[str]:
    lines: list[str] = []
    for result in results:
        parts = [
            str(getattr(result, "status", "?")),
            str(getattr(result, "account_id", "?")),
            str(getattr(result, "pair", "?")),
            _fmt(getattr(result, "price", None)),
        ]
        adv_no = getattr(result, "adv_no", None)
        if adv_no:
            parts.append(f"adv {adv_no}")
        if getattr(result, "dry_run", False):
            parts.append("[dry-run]")
        error = getattr(result, "error", None)
        if error:
            parts.append(f"error: {error}")
        lines.append("  " + " ".join(parts))
    return lines


def _parse_lines(results: Iterable[Any]) -> list[str]:
    lines: list[str] = []
    for item in results:
        line = (
            f"  {getattr(item, 'platform', '?')} {getattr(item, 'pair', '?')} "
            f"fetched={getattr(item, 'fetched', 0)} kept={getattr(item, 'kept', 0)} "
            f"middle={_fmt(getattr(item, 'middle', None))}"
        )
        error = getattr(item, "error", None)
        if error:
            line += f" error={error}"
        lines.append(line)
    return lines


def _problem_list(raw: Any) -> tuple[str, ...]:
    """Normalise an ``engine_problems``/``last_problems`` value (never raises)."""
    if not raw:
        return ()
    if isinstance(raw, str):
        return (raw,)
    try:
        return tuple(str(item) for item in raw)
    except TypeError:  # a not-yet-final façade may hand us something odd
        return ()


def _problem_lines(problems: Any) -> list[str]:
    """``skipped <N>:`` block (at most ``_MAX_PROBLEM_LINES`` entries); [] when empty."""
    items = _problem_list(problems)
    if not items:
        return []
    lines = [f"skipped {len(items)}:"]
    lines += [f"  {item}" for item in items[:_MAX_PROBLEM_LINES]]
    if len(items) > _MAX_PROBLEM_LINES:
        lines.append(f"  ... and {len(items) - _MAX_PROBLEM_LINES} more")
    return lines


def _last_problems(ctx: "_Context") -> tuple[str, ...]:
    """Skipped-entry problems of the last façade operation (absent on old façades)."""
    return _problem_list(getattr(ctx.services, "last_problems", ()))


def render_results(results: Iterable[Any], problems: Any = ()) -> str:
    """Render publish results, one line per entry (used by /publish, /pause, /resume).

    ``problems`` are the engine's skipped ``(pair, platform)`` entries: they are appended
    as one ``skipped <N>: <first>`` line, and when nothing at all was pushed they replace
    the bare ``no results`` with the reason (``nothing to push: <first>``).
    """
    items = _problem_list(problems)
    lines = _result_lines(tuple(results or ()))
    if not lines:
        return f"nothing to push: {items[0]}" if items else "no results"
    if items:
        lines.append(f"skipped {len(items)}: {items[0]}")
    return "\n".join(lines)


def render_snapshot(snapshot: Any, *, now: datetime | None = None) -> str:
    """Render a ``StatusSnapshot``: scenario, rates, prices, market, jobs, last publish.

    Reads the frozen façade shape through ``getattr`` so a not-yet-final implementation
    degrades to ``-``/``none`` instead of raising, and never prints configuration or
    credentials - only pair names, prices and job names.
    """
    moment = now or utcnow()
    lines = [
        f"version: {getattr(snapshot, 'version', constants.VERSION)}",
        f"scenario: {getattr(snapshot, 'scenario', None) or 'none'} "
        f"fiat: {getattr(snapshot, 'fiat', None) or '-'} "
        f"strategy: {getattr(snapshot, 'strategy', None) or '-'}",
        "rates:",
    ]
    lines += _rate_lines(getattr(snapshot, "rates", ()) or ()) or ["  none"]
    lines.append("prices:")
    lines += _price_lines(getattr(snapshot, "prices", ()) or ()) or ["  none"]
    lines.append("market:")
    lines += _market_lines(getattr(snapshot, "market", ()) or (), moment) or ["  none"]
    lines.append("jobs:")
    lines += _job_lines(getattr(snapshot, "jobs", ()) or (), moment) or ["  none"]
    lines.append("last publish:")
    lines += _result_lines(getattr(snapshot, "last_publish", ()) or ()) or ["  none"]
    lines += _problem_lines(getattr(snapshot, "engine_problems", ()))
    return "\n".join(lines)


def _lookup(store: Any, name: str, pair: Pair) -> Decimal | None:
    """Read a base/cap rate; a store that raises for a missing rate reports ``None``."""
    getter = getattr(store, name, None)
    if not callable(getter):
        return None
    try:
        value = getter(pair)
    except Exception:  # a missing rate must not break the command that just wrote one
        return None
    return value if isinstance(value, Decimal) else None


def _parse_pair_rate(args: list[str], usage: str) -> tuple[Pair, Decimal] | None:
    """Parse ``<PAIR> <RATE>``; ``None`` (with a log line) when the input is malformed."""
    try:
        pair = Pair.parse(args[0])
        rate = parse_decimal(args[1], "rate")
    except ValueError as exc:  # ConfigError/RateError are ValueErrors
        _LOGGER.warning("malformed command (%s): %s", usage, exc)
        return None
    if rate <= 0:
        _LOGGER.warning("malformed command (%s): rate must be greater than zero", usage)
        return None
    return pair, rate


def _refresh_after_rate_change(ctx: _Context) -> str:
    """Push the new rates to the venues at once: a lowered cap must bite immediately.

    Returns one short line: the number of pushed ads, or why the refresh could not run.
    A refresh fault (no cap stored, no active scenario, missing market data, venue
    failure) never invalidates the rate that was just written.
    """
    try:
        results = tuple(ctx.services.refresh_prices())
    except (EngineError, ConfigError, ExchangeError) as exc:
        ctx.logger.warning("price refresh after a rate change failed: %s: %s", type(exc).__name__, exc)
        return f"ads not updated: {type(exc).__name__}: {exc}"
    problems = _last_problems(ctx)
    if not results:
        return f"nothing to push: {problems[0]}" if problems else "publish: 0 ads"
    errors = [result for result in results if getattr(result, "status", "") == "error"]
    lines = [f"publish: {len(results)} ads" + (f" ({len(errors)} error)" if errors else "")]
    if errors:
        lines.append(render_results(errors))
    if problems:
        lines.append(f"skipped {len(problems)}: {problems[0]}")
    return "\n".join(lines)


# ----- commands ----------------------------------------------------------------------


@command("start")
@command("help")
def _cmd_help(ctx: _Context, args: list[str]) -> HandlerResult:
    return HandlerResult(HELP_TEXT)


@command("setbase")
def _cmd_setbase(ctx: _Context, args: list[str]) -> HandlerResult:
    if len(args) != 2:
        return HandlerResult(_USAGE_SETBASE)
    parsed = _parse_pair_rate(args, _USAGE_SETBASE)
    if parsed is None:
        return HandlerResult(_USAGE_SETBASE)
    pair, rate = parsed
    ctx.services.rates.set_base(pair, rate)
    ctx.services.rates.save()
    lines = [f"base_rate {pair.symbol} = {rate}"]
    cap = _lookup(ctx.services.rates, "cap", pair)
    if cap is not None and rate > cap:
        lines.append(f"WARNING: base_rate is above cap {cap}; ads are clamped to the cap.")
    lines.append(_refresh_after_rate_change(ctx))
    return HandlerResult("\n".join(lines))


@command("setcap")
def _cmd_setcap(ctx: _Context, args: list[str]) -> HandlerResult:
    if len(args) != 2:
        return HandlerResult(_USAGE_SETCAP)
    parsed = _parse_pair_rate(args, _USAGE_SETCAP)
    if parsed is None:
        return HandlerResult(_USAGE_SETCAP)
    pair, rate = parsed
    ctx.services.rates.set_cap(pair, rate)
    ctx.services.rates.save()
    lines = [f"cap_rate {pair.symbol} = {rate}", _CAP_RULE]
    base = _lookup(ctx.services.rates, "base", pair)
    if base is not None:
        note = "" if base <= rate else "  (base_rate is currently above the cap)"
        lines.append(f"base_rate {pair.symbol} = {base}{note}")
    lines.append(_refresh_after_rate_change(ctx))
    return HandlerResult("\n".join(lines))


@command("rates")
def _cmd_rates(ctx: _Context, args: list[str]) -> HandlerResult:
    snapshot = ctx.services.snapshot()
    lines = ["rates:"]
    lines += _rate_lines(getattr(snapshot, "rates", ()) or ()) or ["  none"]
    lines.append("prices:")
    lines += _price_lines(getattr(snapshot, "prices", ()) or ()) or ["  none"]
    lines += _problem_lines(getattr(snapshot, "engine_problems", ()))
    return HandlerResult("\n".join(lines))


@command("scenarios")
def _cmd_scenarios(ctx: _Context, args: list[str]) -> HandlerResult:
    names = tuple(ctx.services.scenarios.available())
    if not names:
        return HandlerResult("scenarios: none available")
    return HandlerResult(
        f"scenarios: {', '.join(names)}\nactive: {ctx.services.scenarios.active_name() or 'none'}"
    )


@command("scenario")
def _cmd_scenario(ctx: _Context, args: list[str]) -> HandlerResult:
    if len(args) != 1:
        return HandlerResult(_USAGE_SCENARIO)
    blueprint = ctx.services.scenarios.activate(args[0])
    pairs = getattr(blueprint, "pairs", ()) or ()
    return HandlerResult(
        f"scenario '{args[0]}' activated: "
        f"fiat={getattr(blueprint, 'fiat', '?')} "
        f"strategy={getattr(blueprint, 'strategy', '?')} "
        f"pairs={len(tuple(pairs))}"
    )


@command("parse")
def _cmd_parse(ctx: _Context, args: list[str]) -> HandlerResult:
    if len(args) > 1:
        return HandlerResult(_USAGE_PARSE)
    pairs = None if not args else [Pair.parse(args[0]).symbol]
    lines = _parse_lines(ctx.services.run_parser(pairs=pairs) or ())
    return HandlerResult("\n".join(lines) if lines else "parse: nothing to fetch")


@command("publish")
def _cmd_publish(ctx: _Context, args: list[str]) -> HandlerResult:
    dry_run = False
    for arg in args:
        if arg != "--dry":
            return HandlerResult(_USAGE_PUBLISH)
        dry_run = True
    results = ctx.services.refresh_prices(dry_run=dry_run)
    header = "publish (dry run):" if dry_run else "publish:"
    return HandlerResult(f"{header}\n{render_results(results, _last_problems(ctx))}")


def _set_active(ctx: _Context, args: list[str], *, active: bool, usage: str) -> HandlerResult:
    if len(args) > 1:
        return HandlerResult(usage)
    pairs = None if not args else [Pair.parse(args[0]).symbol]
    results = ctx.services.set_active(active, pairs=pairs)
    scope = pairs[0] if pairs else "all pairs"
    verb = "resume" if active else "pause"
    return HandlerResult(f"{verb} {scope}:\n{render_results(results, _last_problems(ctx))}")


@command("pause")
def _cmd_pause(ctx: _Context, args: list[str]) -> HandlerResult:
    return _set_active(ctx, args, active=False, usage=_USAGE_PAUSE)


@command("resume")
def _cmd_resume(ctx: _Context, args: list[str]) -> HandlerResult:
    return _set_active(ctx, args, active=True, usage=_USAGE_RESUME)


@command("status")
def _cmd_status(ctx: _Context, args: list[str]) -> HandlerResult:
    return HandlerResult(render_snapshot(ctx.services.snapshot(), now=ctx.now()))


@command("version")
def _cmd_version(ctx: _Context, args: list[str]) -> HandlerResult:
    version = getattr(ctx.services, "version", constants.VERSION)
    now = _as_datetime(ctx.now()) or utcnow()
    started = _as_datetime(ctx.started_at) or now
    return HandlerResult(f"p2pbot {version}, uptime {_format_duration(now - started)}")


class Dispatcher:
    """Routes one update to a registered command handler."""

    def __init__(
        self,
        services: Any,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._services = services
        self._logger = logger if logger is not None else _LOGGER
        service_clock = getattr(services, "clock", None)
        chooser = clock if callable(clock) else (service_clock if callable(service_clock) else None)
        started = _as_datetime(chooser()) if chooser is not None else None
        self._ctx = _Context(
            services=services,
            started_at=started if started is not None else utcnow(),
            logger=self._logger,
        )

    @property
    def commands(self) -> tuple[str, ...]:
        """Registered command names, sorted (diagnostics)."""
        return tuple(sorted(_COMMANDS))

    def dispatch(self, update: "Update") -> HandlerResult | None:
        """Return the reply for ``update``; ``None`` when there is nothing to answer."""
        message = getattr(update, "message", None)
        if message is None:
            return None
        text = (getattr(message, "text", "") or "").strip()
        if not text:
            return None
        name = ""
        args: list[str] = []
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            name = head[1:].split("@", 1)[0].lower()
            args = rest.split()
        handler = _COMMANDS.get(name)
        if handler is None:
            return HandlerResult(HELP_HINT)
        try:
            return handler(self._ctx, args)
        except Exception as exc:  # every façade fault becomes text; never a traceback
            self._logger.warning("command /%s failed: %s: %s", name, type(exc).__name__, exc)
            return HandlerResult(f"{type(exc).__name__}: {exc}")
