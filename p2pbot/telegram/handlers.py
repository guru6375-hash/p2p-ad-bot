"""Command router for the owner-only bot: text in, reply text out.

Every handler is a pure function of ``(context, args)`` and performs **no Telegram I/O**;
the runner is responsible for sending :class:`HandlerResult.text` (and its buttons). That
split is what makes the whole command surface testable without a socket.

The router talks to the duck-typed façade and nothing else - it never imports
``p2pbot.services``/``publisher``. Façade faults are reported back to the owner as
``<ExceptionType>: <message>``; a traceback never reaches Telegram and no secret is echoed.

Commands:

* ``/getads [<PAIR>|all] [--offline] [--details]`` - the buy ads per account and pair.
* ``/setrate`` - answers with the buttons **UAH** and **PLN**. After a press the next plain
  message is the rate (``43.50``, or ``43.50 --dry`` for a preview):

  - UAH: ``services.set_uah_rate`` reprices each account's online buy UAH/USDT ads as a
    ladder ``rate``, ``rate - STEP``, ... and its UAH/USDC ads the same ladder one STEP
    lower (``uah_config.STEP``);
  - PLN: ``services.set_pln_rate`` sets every online buy PLN/USDT and PLN/USDC ad to the
    rate.

  ``/setrate uah 43.50`` / ``/setrate pln 3.85`` skip the buttons. A waiting prompt
  expires after :data:`PENDING_RATE_SECONDS`; any command (or ``/cancel``) drops it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .. import constants
from ..models import Pair, parse_decimal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .api import CallbackQuery, Update

__all__ = [
    "Dispatcher",
    "HandlerResult",
    "HELP_TEXT",
    "MARKETS",
    "PENDING_RATE_SECONDS",
    "command",
]

_LOGGER = logging.getLogger(__name__)

_USAGE_SETRATE = "Usage: /setrate, or /setrate <uah|pln> <RATE> [--dry]"
_USAGE_GETADS = "Usage: /getads [<PAIR>|all] [--offline] [--details]"
#: What ``/getads`` shows without a pair, in this order: the UAH and PLN pairs.
_GETADS_DEFAULT_PAIRS = ("UAH/USDT", "UAH/USDC", "PLN/USDT", "PLN/USDC")

#: ``/setrate`` markets: key -> (button label, façade method).
MARKETS: Mapping[str, tuple[str, str]] = {
    "uah": ("🇺🇦 UAH", "set_uah_rate"),
    "pln": ("🇵🇱 PLN", "set_pln_rate"),
}
#: ``callback_data`` prefix of the ``/setrate`` buttons.
_SETRATE_CALLBACK = "setrate:"
#: How long a pressed market waits for its rate message.
PENDING_RATE_SECONDS = 300.0

#: How many notes a reply lists before summarising the rest.
_MAX_PROBLEM_LINES = 10

#: Shown for an unknown command or for free text.
HELP_HINT = "Unknown command. Send /help for the command list."


def _build_help() -> str:
    lines = [
        "P2P ad manager (owner only). Files and attachments are never accepted.",
        "commands:",
    ]
    lines.extend(f"/{name} - {description}" for name, description in constants.TELEGRAM_COMMANDS)
    lines += [
        "",
        "/setrate → tap UAH or PLN → send the rate (e.g. 43.50; add --dry to preview).",
        "UAH: USDT = rate, USDC one STEP lower, each next ad one STEP lower.",
        "PLN: every PLN/USDT and PLN/USDC buy ad = rate.",
    ]
    return "\n".join(lines)


HELP_TEXT = _build_help()


@dataclass(frozen=True)
class HandlerResult:
    """The reply for one update; ``silent`` suppresses the reply entirely.

    ``buttons`` are inline-keyboard rows of ``(label, callback_data)`` under the reply;
    ``edit`` (for a button press) replaces the text of the message that carried the button,
    which also removes its buttons.
    """

    text: str
    silent: bool = False
    buttons: Sequence[Sequence[tuple[str, str]]] | None = None
    edit: str | None = None


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


def _fmt(value: Any) -> str:
    """Render an optional Decimal-like value, ``-`` when absent."""
    return "-" if value is None else str(value)


def _problem_list(raw: Any) -> tuple[str, ...]:
    """Normalise a ``problems`` value (never raises)."""
    if not raw:
        return ()
    if isinstance(raw, str):
        return (raw,)
    try:
        return tuple(str(item) for item in raw)
    except TypeError:  # a stub façade may hand us something odd
        return ()


def _parse_rate(args: Sequence[str]) -> tuple[Decimal, bool] | None:
    """``<RATE> [--dry]`` -> ``(rate, dry_run)``; ``None`` when it is not that."""
    dry_run = "--dry" in args
    values = [arg for arg in args if arg != "--dry"]
    if len(values) != 1 or len(args) - len(values) > 1:
        return None
    try:
        rate = parse_decimal(values[0].replace(",", "."), "rate")
    except ValueError:
        return None
    if rate <= 0:
        return None
    return rate, dry_run


def _setrate_buttons() -> list[list[tuple[str, str]]]:
    return [[(label, f"{_SETRATE_CALLBACK}{key}") for key, (label, _) in MARKETS.items()]]


def _rate_prompt(ctx: _Context, market: str) -> str:
    if market == "uah":
        steps = getattr(ctx.services, "uah_steps", None) or {}
        ladder = " · ".join(f"{name.capitalize()} {step}" for name, step in steps.items())
        return "\n".join(
            [
                "🇺🇦 Send the UAH rate, e.g. 43.50",
                "USDT = rate, USDC one STEP lower, each next ad one STEP lower"
                + (f" (STEP {ladder})." if ladder else "."),
                "Add --dry to preview · /cancel to stop.",
            ]
        )
    return "\n".join(
        [
            "🇵🇱 Send the PLN rate, e.g. 3.85",
            "Every online PLN/USDT and PLN/USDC buy ad gets this rate.",
            "Add --dry to preview · /cancel to stop.",
        ]
    )


def _apply_rate(ctx: _Context, market: str, rate: Decimal, dry_run: bool) -> HandlerResult:
    """Run the market's façade method and render its report."""
    method = getattr(ctx.services, MARKETS[market][1])
    report = method(rate, dry_run=dry_run)
    name = market.upper()
    header = (
        f"🧪 Preview · {name} rate {rate} · nothing sent"
        if dry_run
        else f"✅ {name} rate {rate} applied"
    )
    return HandlerResult("\n".join([header, *_edit_report_lines(report, dry_run=dry_run)]))


# ----- commands ----------------------------------------------------------------------


@command("start")
@command("help")
def _cmd_help(ctx: _Context, args: list[str]) -> HandlerResult:
    return HandlerResult(HELP_TEXT)


@command("setrate")
def _cmd_setrate(ctx: _Context, args: list[str]) -> HandlerResult:
    if not args:
        return HandlerResult("💱 Set rate · pick a market", buttons=_setrate_buttons())
    market = args[0].lower()
    parsed = _parse_rate(args[1:]) if market in MARKETS else None
    if parsed is None:
        return HandlerResult(_USAGE_SETRATE)
    return _apply_rate(ctx, market, *parsed)


@command("cancel")
def _cmd_cancel(ctx: _Context, args: list[str]) -> HandlerResult:
    # the dispatcher drops a waiting /setrate before any command runs
    return HandlerResult("Nothing is waiting for a rate.")


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
    skipped = tuple(getattr(report, "skipped", ()) or ())
    if skipped:
        lines += ["", f"⏭ Skipped ({len(skipped)}) · another ad already has this rate"]
        for edit in skipped:
            lines.append(
                f"   {getattr(edit, 'account_id', '?')} {getattr(edit, 'pair', '?')} "
                f"adv {getattr(edit, 'adv_no', '?')}"
            )
    problems = _problem_list(getattr(report, "problems", ()))
    if problems:
        lines += ["", f"⚠️ Notes ({len(problems)})"]
        lines += [f"   {item}" for item in problems[:_MAX_PROBLEM_LINES]]
        if len(problems) > _MAX_PROBLEM_LINES:
            lines.append(f"   ... and {len(problems) - _MAX_PROBLEM_LINES} more")
    edited = len(results) - len(failed)
    done = "would be updated" if dry_run else "updated"
    summary = f"{edited} {done} · {len(failed)} failed · {len(unchanged)} already at rate"
    if skipped:
        summary += f" · {len(skipped)} skipped"
    lines += ["", summary]
    return lines


class Dispatcher:
    """Routes one update to a registered command handler.

    It also remembers, per chat, which ``/setrate`` market button was pressed, so the next
    plain message of that chat is read as the rate.
    """

    def __init__(
        self,
        services: Any,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._services = services
        self._logger = logger if logger is not None else _LOGGER
        self._ctx = _Context(services=services, logger=self._logger)
        self._clock = clock
        #: chat id -> (market, when the button was pressed)
        self._pending: dict[int | None, tuple[str, float]] = {}

    @property
    def commands(self) -> tuple[str, ...]:
        """Registered command names, sorted (diagnostics)."""
        return tuple(sorted(_COMMANDS))

    def pending(self, chat_id: int | None) -> str | None:
        """The market waiting for a rate in ``chat_id`` (``None`` when nothing waits)."""
        entry = self._pending.get(chat_id)
        if entry is None or self._clock() - entry[1] > PENDING_RATE_SECONDS:
            return None
        return entry[0]

    def dispatch(self, update: "Update") -> HandlerResult | None:
        """Return the reply for ``update``; ``None`` when there is nothing to answer."""
        callback = getattr(update, "callback", None)
        if callback is not None:
            return self._guard("button", lambda: self._press(callback))
        message = getattr(update, "message", None)
        if message is None:
            return None
        text = (getattr(message, "text", "") or "").strip()
        if not text:
            return None
        chat_id = getattr(message, "chat_id", None)
        if not text.startswith("/"):
            return self._guard("rate", lambda: self._rate_message(chat_id, text))
        head, _, rest = text.partition(" ")
        name = head[1:].split("@", 1)[0].lower()
        waiting = self._pending.pop(chat_id, None)
        handler = _COMMANDS.get(name)
        if handler is None:
            return HandlerResult(HELP_HINT)
        if name == "cancel" and waiting is not None:
            return HandlerResult(f"Cancelled: {waiting[0].upper()} rate not changed.")
        return self._guard(f"/{name}", lambda: handler(self._ctx, rest.split()))

    def _press(self, callback: "CallbackQuery") -> HandlerResult:
        data = getattr(callback, "data", "") or ""
        market = data[len(_SETRATE_CALLBACK):] if data.startswith(_SETRATE_CALLBACK) else ""
        if market not in MARKETS:
            return HandlerResult("This button is no longer used. Send /setrate.")
        message = getattr(callback, "message", None)
        chat_id = getattr(message, "chat_id", None)
        self._pending[chat_id] = (market, self._clock())
        return HandlerResult(
            _rate_prompt(self._ctx, market), edit=f"💱 Set rate · {MARKETS[market][0]}"
        )

    def _rate_message(self, chat_id: int | None, text: str) -> HandlerResult:
        entry = self._pending.get(chat_id)
        if entry is None:
            return HandlerResult(HELP_HINT)
        market, since = entry
        if self._clock() - since > PENDING_RATE_SECONDS:
            del self._pending[chat_id]
            return HandlerResult("⌛ That rate prompt expired. Send /setrate again.")
        parsed = _parse_rate(text.split())
        if parsed is None:
            return HandlerResult(
                f"❓ Not a rate: {text[:40]!r}. Send a number like "
                f"{'43.50' if market == 'uah' else '3.85'}, or /cancel."
            )
        del self._pending[chat_id]
        return _apply_rate(self._ctx, market, *parsed)

    def _guard(self, what: str, run: Callable[[], HandlerResult]) -> HandlerResult:
        try:
            return run()
        except Exception as exc:  # every façade fault becomes text; never a traceback
            self._logger.warning("%s failed: %s: %s", what, type(exc).__name__, exc)
            return HandlerResult(f"{type(exc).__name__}: {exc}")
