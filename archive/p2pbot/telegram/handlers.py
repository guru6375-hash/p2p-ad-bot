"""Command router for the owner-only bot: text in, reply text out.

Every handler is a pure function of ``(context, args)`` and performs **no Telegram I/O**;
the runner is responsible for sending :class:`HandlerResult.text`. That split is what
makes the whole command surface testable without a socket.

The router talks to the frozen façade (SPEC 11.5) and nothing else - it never imports
``p2pbot.services``/``publisher``/``engine``. Façade faults (missing rates or caps,
unknown scenario, transport failures) are reported back to the owner as
``<ExceptionType>: <message>``; a traceback never reaches Telegram and no secret is
echoed.

``/setbase`` and ``/setcap`` only store the rate. ``/setrate <RATE> [--dry]`` is the one
command that edits live ads: through ``services.set_uah_rate`` it reprices each account's
online buy UAH/USDT ads as a ladder ``rate``, ``rate - STEP``, ``rate - 2*STEP``, ... and its
UAH/USDC ads the same ladder one STEP lower (a cap is optional; a stored one still limits),
and replies with one line per edit plus the problems.

A venue with no competitor data for a pair is a *skip*, not a fault: the engine reports
those ``(pair, platform)`` entries in ``StatusSnapshot.engine_problems``, and ``/rates``
appends a ``skipped <N>:`` block for them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .. import constants
from ..models import Pair, parse_decimal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .api import Update

__all__ = [
    "Dispatcher",
    "HandlerResult",
    "HELP_TEXT",
    "command",
]

_LOGGER = logging.getLogger(__name__)

_USAGE_SETBASE = "Usage: /setbase <PAIR> <RATE>"
_USAGE_SETCAP = "Usage: /setcap <PAIR> <RATE>"
_USAGE_SCENARIO = "Usage: /scenario <NAME>"
_USAGE_SETRATE = "Usage: /setrate <RATE> [--dry]"
_USAGE_GETADS = "Usage: /getads [<PAIR>|all] [--offline] [--details]"
#: What ``/getads`` shows without a pair, in this order: the UAH and PLN pairs.
_GETADS_DEFAULT_PAIRS = ("UAH/USDT", "UAH/USDC", "PLN/USDT", "PLN/USDC")

_CAP_RULE = (
    "advertisement prices can never exceed the cap: the engine clamps every computed "
    "price to it and every ad edit re-asserts it before the request."
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
    """What a handler is allowed to see: the façade and a logger."""

    services: Any
    logger: logging.Logger


_Handler = Callable[[_Context, list[str]], HandlerResult]

#: Command name (no slash, lowercase) -> handler. Populated by :func:`command`.
_COMMANDS: dict[str, _Handler] = {}


def command(name: str) -> Callable[[_Handler], _Handler]:
    """Register a handler for ``name``; several names may share one handler."""

    def decorate(func: _Handler) -> _Handler:
        _COMMANDS[name] = func
        return func

    return decorate


# ----- formatting helpers (/rates) ----------------------------------------------------


def _fmt(value: Any) -> str:
    """Render an optional Decimal-like value, ``-`` when absent."""
    return "-" if value is None else str(value)


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


def _problem_list(raw: Any) -> tuple[str, ...]:
    """Normalise an ``engine_problems`` value (never raises)."""
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


@command("setrate")
def _cmd_setrate(ctx: _Context, args: list[str]) -> HandlerResult:
    dry_run = "--dry" in args
    values = [arg for arg in args if arg != "--dry"]
    if len(values) != 1 or len(args) - len(values) > 1:
        return HandlerResult(_USAGE_SETRATE)
    try:
        rate = parse_decimal(values[0], "rate")
    except ValueError:
        return HandlerResult(_USAGE_SETRATE)
    if rate <= 0:
        return HandlerResult(_USAGE_SETRATE)
    report = ctx.services.set_uah_rate(rate, dry_run=dry_run)
    header = (
        f"🧪 Preview · rate {rate} · nothing sent" if dry_run else f"✅ Rate {rate} applied"
    )
    return HandlerResult("\n".join([header, *_edit_report_lines(report, dry_run=dry_run)]))


def _pair_order(symbol: str) -> tuple[int, str]:
    """The default pairs first, in their listed order, then the rest alphabetically."""
    if symbol in _GETADS_DEFAULT_PAIRS:
        return (_GETADS_DEFAULT_PAIRS.index(symbol), "")
    return (len(_GETADS_DEFAULT_PAIRS), symbol)


@command("getads")
def _cmd_getads(ctx: _Context, args: list[str]) -> HandlerResult:
    """Buy ads per account and pair, rates highest first (``--offline``, ``--details``)."""
    flags = {arg for arg in args if arg.startswith("--")}
    values = [arg for arg in args if not arg.startswith("--")]
    if len(values) > 1 or not flags <= {"--offline", "--details"} or len(flags) != sum(
        1 for arg in args if arg.startswith("--")
    ):
        return HandlerResult(_USAGE_GETADS)
    offline, details = "--offline" in flags, "--details" in flags
    if not values:
        wanted: tuple[str, ...] | None = _GETADS_DEFAULT_PAIRS
    elif values[0].lower() == "all":
        wanted = None
    else:
        try:
            wanted = (Pair.parse(values[0]).symbol,)
        except ValueError:
            return HandlerResult(_USAGE_GETADS)
    scope = "all pairs" if wanted is None else ", ".join(wanted)
    lines = [f"📊 Buy ads · {scope}" + (" · incl. offline" if offline else "")]
    for listing in ctx.services.get_own_ads():
        account = getattr(listing, "account_id", "?")
        lines.append("")
        error = getattr(listing, "error", None)
        if error:
            lines.append(f"⚠️ {account}: cannot list ads ({error})")
            continue
        by_pair: dict[str, list[Any]] = {}
        for ad in getattr(listing, "ads", ()) or ():
            pair = str(getattr(ad, "pair", ""))
            if getattr(ad, "side", "") != "buy" or (wanted is not None and pair not in wanted):
                continue
            if offline or getattr(ad, "active", False):
                by_pair.setdefault(pair, []).append(ad)
        if not by_pair:
            lines.append(f"🏦 {account}: no buy ads")
            continue
        lines.append(f"🏦 {account}")
        for pair in sorted(by_pair, key=_pair_order):
            ads = sorted(by_pair[pair], key=lambda ad: -(ad.price or 0))
            if details:
                lines.append(f"   {pair}")
                for ad in ads:
                    state = "" if ad.active else " (off)"
                    lines.append(
                        f"      {_fmt(ad.price)}{state} | {_fmt(ad.min_amount)}–{_fmt(ad.max_amount)}"
                        f" | left {_fmt(ad.quantity)} | adv {ad.adv_no}"
                    )
            else:
                rates = " · ".join(
                    _fmt(ad.price) + ("" if ad.active else " (off)") for ad in ads
                )
                lines.append(f"   {pair}: {rates}")
    return HandlerResult("\n".join(lines))


def _edit_report_lines(report: Any, *, dry_run: bool = False) -> list[str]:
    """The resulting rates per account and pair, then failures, notes and a summary."""
    results = tuple(getattr(report, "results", ()) or ())
    unchanged = tuple(getattr(report, "unchanged", ()) or ())
    rates: dict[str, dict[str, list[Any]]] = {}
    failed: list[Any] = []
    for result in results:
        if getattr(result, "status", "") == "error":
            failed.append(result)
            continue
        account = str(getattr(result, "account_id", "?"))
        rates.setdefault(account, {}).setdefault(str(getattr(result, "pair", "?")), []).append(
            getattr(result, "price", None)
        )
    for edit in unchanged:
        account = str(getattr(edit, "account_id", "?"))
        rates.setdefault(account, {}).setdefault(str(getattr(edit, "pair", "?")), []).append(
            getattr(edit, "price", None)
        )
    lines: list[str] = []
    for account, pairs in rates.items():
        lines += ["", f"🏦 {account}"]
        for pair in sorted(pairs, key=_pair_order):
            prices = sorted(pairs[pair], key=lambda price: -(price or 0))
            lines.append(f"   {pair}: " + " · ".join(_fmt(price) for price in prices))
    if failed:
        lines += ["", f"❌ Failed ({len(failed)})"]
        for result in failed:
            lines.append(
                f"   {getattr(result, 'account_id', '?')} {getattr(result, 'pair', '?')} → "
                f"{_fmt(getattr(result, 'price', None))}: {getattr(result, 'error', '?')}"
            )
    problems = _problem_list(getattr(report, "problems", ()))
    if problems:
        lines += ["", f"⚠️ Notes ({len(problems)})"]
        lines += [f"   {item}" for item in problems[:_MAX_PROBLEM_LINES]]
        if len(problems) > _MAX_PROBLEM_LINES:
            lines.append(f"   ... and {len(problems) - _MAX_PROBLEM_LINES} more")
    edited = len(results) - len(failed)
    done = "would be updated" if dry_run else "updated"
    lines += ["", f"{edited} {done} · {len(failed)} failed · {len(unchanged)} already at rate"]
    return lines


class Dispatcher:
    """Routes one update to a registered command handler."""

    def __init__(
        self,
        services: Any,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._services = services
        self._logger = logger if logger is not None else _LOGGER
        self._ctx = _Context(services=services, logger=self._logger)

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
