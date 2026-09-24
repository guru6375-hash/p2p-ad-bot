"""Smoke harness for the Telegram layer: real bot loop against a local stub Bot API.

Run from the project root::

    python scripts/telegram_stub_smoke.py

The harness starts a ``http.server.ThreadingHTTPServer`` that implements
``/bot<token>/getMe``, ``/getUpdates``, ``/sendMessage`` and ``/setMyCommands``, records
every ``sendMessage`` body, and feeds a scripted update sequence (a foreign user with a
document, a foreign ``/setbase``, then the owner's commands, the owner's document, and
``/version``). It then drives the *real* :class:`p2pbot.telegram.bot.BotRunner` poll loop
- one poll iteration per scripted update - against a hand-written stub façade that
implements exactly the SPEC 11.5 contract, and prints the transcript.

It proves, from the server side (recorded ``sendMessage`` bodies, not from internal
state): no reply was ever sent for the foreign user, no reply was ever sent for an
attachment (including the owner's), and the owner's commands were answered.

It also prints three focused verifications: the rate limiter rejecting the 21st message
inside the window, a non-private chat being refused, and the bot token staying redacted
in ``{"ok": false}`` error messages.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p2pbot import constants  # noqa: E402
from p2pbot.errors import (  # noqa: E402
    ConfigError,
    MissingCapError,
    MissingRateError,
    TelegramError,
    TransportError,
)
from p2pbot.models import ComputedAd, MarketSnapshot, Pair, utcnow  # noqa: E402
from p2pbot.telegram import BotRunner, TelegramAPI, Update  # noqa: E402
from p2pbot.telegram.handlers import Dispatcher  # noqa: E402
from p2pbot.telegram.security import AccessController, Decision, RateLimiter  # noqa: E402

TOKEN = "111111:SMOKE-TOKEN-abcdef"
#: Tokens the stub server treats as failures (never printed unredacted).
BAD_TOKEN_HTTP_401 = "999999:BADTOKEN-deadbeef"
BAD_TOKEN_OK_FALSE = "999999:SOFTFAIL-cafebabe"
CONFLICT_TOKEN = "999999:CONFLICT-badc0de"
#: Token whose requests the stub answers by dropping the connection (a transport fault).
ABORT_TOKEN = "999999:ABORT-noresponse"

OWNER_ID = 424242
FOREIGN_ID = 999000111
PAIR = Pair.parse("UAH/USDT")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Print one PASS/FAIL line and remember failures for the exit code."""
    print(f"{'PASS' if ok else 'FAIL'} {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)
    return ok


# --------------------------------------------------------------------------- stub API


class StubState:
    """Recorded server-side state: queued updates, sent messages, received calls."""

    def __init__(self, updates: Sequence[Mapping[str, Any]]) -> None:
        self.updates: list[Mapping[str, Any]] = list(updates)
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.commands: list[dict[str, Any]] = []

    def next_batch(self) -> list[Mapping[str, Any]]:
        """Serve exactly one queued update per call; ``[]`` once the script is drained."""
        return [self.updates.pop(0)] if self.updates else []


def _make_handler(state: StubState) -> type[BaseHTTPRequestHandler]:
    class StubHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # silence stderr noise
            return

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            method = self.path.rsplit("/", 1)[-1].split("?")[0]
            state.calls.append(f"POST {_redact_path(self.path)} -> {len(raw)} bytes")
            if not isinstance(payload, dict):
                payload = {}

            if BAD_TOKEN_HTTP_401 in self.path:
                self._json(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
            elif ABORT_TOKEN in self.path:
                self.close_connection = True  # drop the socket: the client sees a transport fault
            elif CONFLICT_TOKEN in self.path:
                self._json(
                    409,
                    {
                        "ok": False,
                        "error_code": 409,
                        "description": "Conflict: terminated by other getUpdates request",
                    },
                )
            elif BAD_TOKEN_OK_FALSE in self.path:
                # A misbehaving server that echoes the request path back (token included):
                # the client must still redact it.
                self._json(
                    200,
                    {
                        "ok": False,
                        "error_code": 401,
                        "description": f"Unauthorized for bot{BAD_TOKEN_OK_FALSE}",
                    },
                )
            elif method == "getMe":
                self._json(200, {"ok": True, "result": {"id": 7, "is_bot": True, "username": "smoke_bot"}})
            elif method == "getUpdates":
                self._json(200, {"ok": True, "result": state.next_batch()})
            elif method == "sendMessage":
                state.sent.append(payload)
                self._json(
                    200,
                    {
                        "ok": True,
                        "result": {
                            "message_id": len(state.sent),
                            "chat": {"id": payload.get("chat_id")},
                            "text": payload.get("text", ""),
                        },
                    },
                )
            elif method == "setMyCommands":
                state.commands.append(payload)
                self._json(200, {"ok": True, "result": True})
            else:
                self._json(404, {"ok": False, "error_code": 404, "description": f"unknown method {method}"})

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return StubHandler


def _redact_path(path: str) -> str:
    for token in (TOKEN, BAD_TOKEN_HTTP_401, BAD_TOKEN_OK_FALSE, CONFLICT_TOKEN, ABORT_TOKEN):
        path = path.replace(token, constants.REDACTED)
    return path


def _message(
    update_id: int,
    user_id: int,
    *,
    text: str | None = None,
    chat_type: str = "private",
    attachments: Sequence[tuple[str, Mapping[str, Any]]] = (),
) -> dict[str, Any]:
    """Build a raw Telegram update payload (as the Bot API would deliver it)."""
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": 1758700000,
        "chat": {"id": user_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "smoke"},
    }
    if text is not None:
        message["text"] = text
    for key, value in attachments:
        message[key] = value
    return {"update_id": update_id, "message": message}


#: ``(payload, sender_kind, carries_attachment)`` - the scripted conversation.
SCRIPT: tuple[tuple[dict[str, Any], str, bool], ...] = (
    (
        _message(1, FOREIGN_ID, attachments=(("document", {"file_id": "BQ-FOREIGN", "file_name": "virus.exe"}),)),
        "foreign",
        True,
    ),
    (_message(2, FOREIGN_ID, text="/setbase UAH/USDT 99.00"), "foreign", False),
    (_message(3, OWNER_ID, text="/setbase UAH/USDT 47.00"), "owner", False),
    (_message(4, OWNER_ID, text="/setcap UAH/USDT 46.50"), "owner", False),
    (_message(5, OWNER_ID, text="/rates"), "owner", False),
    (_message(6, OWNER_ID, text="/status"), "owner", False),
    (
        _message(7, OWNER_ID, attachments=(("document", {"file_id": "BQ-OWNER", "file_name": "rates.csv"}),)),
        "owner",
        True,
    ),
    (_message(8, OWNER_ID, text="/version"), "owner", False),
)


# ------------------------------------------------------------------- stub façade (§11.5)


@dataclass(frozen=True)
class PublishResult:
    account_id: str
    platform: str
    pair: str
    status: str
    price: Decimal | None = None
    adv_no: str | None = None
    error: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class RateRow:
    pair: str
    base: Decimal | None
    cap: Decimal | None


@dataclass(frozen=True)
class MarketRow:
    platform: str
    pair: str
    middle: Decimal | None
    filtered: int
    fetched_at: datetime | None


@dataclass(frozen=True)
class JobRow:
    name: str
    next_run_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class StatusSnapshot:
    version: str
    scenario: str | None
    fiat: str | None
    strategy: str | None
    rates: tuple[RateRow, ...]
    prices: tuple[ComputedAd, ...]
    market: tuple[MarketRow, ...]
    jobs: tuple[JobRow, ...]
    last_publish: tuple[PublishResult, ...]
    engine_error: str | None = None
    engine_problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketFetchResult:
    platform: str
    pair: str
    fetched: int
    kept: int
    middle: Decimal | None
    error: str | None


class StubRateStore:
    """Dict-backed RateStore stand-in used when ``p2pbot.rates`` is not importable yet."""

    def __init__(self, data: Mapping[str, Any] | None = None, path: Path | None = None) -> None:
        self._base: dict[str, Decimal] = {}
        self._cap: dict[str, Decimal] = {}
        self.path = path
        data = data or {}
        for symbol, rate in (data.get("base") or {}).items():
            self._base[symbol] = Decimal(str(rate))
        for symbol, rate in (data.get("cap") or {}).items():
            self._cap[symbol] = Decimal(str(rate))

    def set_base(self, pair: Pair | str, rate: Decimal) -> None:
        self._write(self._base, pair, rate)

    def set_cap(self, pair: Pair | str, rate: Decimal) -> None:
        self._write(self._cap, pair, rate)

    def _write(self, target: dict[str, Decimal], pair: Pair | str, rate: Decimal) -> None:
        key = Pair.parse(pair).symbol
        value = Decimal(rate)
        if value <= 0:
            raise ValueError(f"rate for {key} must be greater than zero")
        target[key] = value

    def base(self, pair: Pair | str) -> Decimal | None:
        return self._base.get(Pair.parse(pair).symbol)

    def cap(self, pair: Pair | str) -> Decimal | None:
        return self._cap.get(Pair.parse(pair).symbol)

    def clear(self, pair: Pair | str) -> None:
        key = Pair.parse(pair).symbol
        self._base.pop(key, None)
        self._cap.pop(key, None)

    def pairs(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._base) | set(self._cap)))

    def save(self) -> None:
        return None


def _build_rate_store() -> tuple[Any, str]:
    """Use the real RateStore when it exists, otherwise the local stub."""
    try:
        from p2pbot.rates import RateStore
    except ImportError:
        return StubRateStore(), "local stub (p2pbot.rates is not importable yet)"
    return RateStore(), "real p2pbot.rates.RateStore"


@dataclass
class StubSettings:
    owner_id: int
    scenarios_dir: str = "scenarios"
    state_path: str = "var/state.json"

    def redacted(self) -> dict[str, str]:
        return {"owner_id": str(self.owner_id)}


class StubBlueprint:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fiat = "UAH"
        self.strategy = "fixed_spread"
        self.pairs = (PAIR, Pair.parse("UAH/USDC"))


class StubScenarios:
    def __init__(self, active: str = "uah") -> None:
        self._active = active

    def available(self) -> tuple[str, ...]:
        return ("pln", "uah")

    def active_name(self) -> str | None:
        return self._active

    def activate(self, name: str) -> StubBlueprint:
        if name not in self.available():
            raise ConfigError(f"unknown scenario {name!r}")
        self._active = name
        return self.blueprint()

    def blueprint(self) -> StubBlueprint:
        return StubBlueprint(self._active)

    def reload(self) -> StubBlueprint:
        return self.blueprint()


@dataclass(frozen=True)
class StubJob:
    name: str
    interval_minutes: int | None
    next_run_at: datetime | None
    cron: str | None = None
    last_error: str | None = None


class StubScheduler:
    """Minimal Scheduler: exposes ``jobs``/``next_run``/``describe``/``run_due``."""

    def __init__(self, clock: Any) -> None:
        self._clock = clock
        self.runs = 0
        self.last_error: str | None = None

    @property
    def jobs(self) -> tuple[StubJob, ...]:
        return (StubJob("parser", constants.DEFAULT_PARSER_INTERVAL_MINUTES, self.next_run()),)

    def next_run(self, name: str | None = None) -> datetime | None:
        return self._clock() + timedelta(minutes=constants.DEFAULT_PARSER_INTERVAL_MINUTES)

    def describe(self) -> str:
        return f"parser every {constants.DEFAULT_PARSER_INTERVAL_MINUTES}m"

    def run_due(self, now: datetime) -> tuple[StubJob, ...]:
        self.runs += 1
        return ()


class StubMarket:
    def __init__(self, snapshots: Mapping[tuple[str, str], MarketSnapshot] | None = None) -> None:
        self._data = dict(snapshots or {})

    def get(self, platform: str, pair: Pair | str) -> MarketSnapshot | None:
        return self._data.get((platform, Pair.parse(pair).symbol))

    def middle(self, platform: str, pair: Pair | str) -> Decimal | None:
        snapshot = self.get(platform, pair)
        return None if snapshot is None else snapshot.middle


class StubServices:
    """Hand-written stand-in for ``p2pbot.services.BotServices`` (SPEC 11.5 contract)."""

    def __init__(self) -> None:
        clock = utcnow
        self.clock = clock
        now = clock()
        self.settings = StubSettings(owner_id=OWNER_ID)
        self.version = "1.0.0-smoke"
        self.rates, self.rates_kind = _build_rate_store()
        self.market = StubMarket(
            {
                ("binance", PAIR.symbol): MarketSnapshot(
                    platform="binance",
                    pair=PAIR,
                    middle=Decimal("46.90"),
                    filtered=(),
                    fetched_at=now - timedelta(minutes=5),
                ),
                ("okx", PAIR.symbol): MarketSnapshot(
                    platform="okx",
                    pair=PAIR,
                    middle=Decimal("47.10"),
                    filtered=(),
                    fetched_at=now - timedelta(minutes=40),
                ),
            }
        )
        self.scheduler = StubScheduler(clock)
        self.scenarios = StubScenarios()
        self.publish_calls: list[bool] = []
        #: Engine-skipped (pair, platform) problems, surfaced as ``engine_problems`` /
        #: ``last_problems`` exactly like the real façade does.
        self.problems: tuple[str, ...] = ()
        self.last_problems: tuple[str, ...] = ()
        self._last_publish: tuple[PublishResult, ...] = (
            PublishResult(
                account_id="Binance#1",
                platform="binance",
                pair=PAIR.symbol,
                status="created",
                price=Decimal("46.50"),
                adv_no="9001",
            ),
        )

    # ----- façade methods -----------------------------------------------------------

    def _prices(self) -> tuple[ComputedAd, ...]:
        base = self.rates.base(PAIR)
        cap = self.rates.cap(PAIR)
        if base is None or cap is None:
            return ()
        price = cap if base > cap else base
        return tuple(
            ComputedAd(
                pair=PAIR,
                platform=platform,
                price=price,
                source="base_rate",
                cap=cap,
                accounts=(f"{platform.capitalize()}#1",),
                base=base,
                clamped=base > cap,
            )
            for platform in constants.PLATFORMS
        )

    def refresh_prices(self, *, dry_run: bool = False) -> tuple[PublishResult, ...]:
        self.publish_calls.append(dry_run)
        if self.rates.base(PAIR) is None:
            # Mirrors the real façade: a missing rate is a hard EngineError, not a no-op.
            raise MissingRateError(f"no base_rate stored for {PAIR.symbol}")
        if self.rates.cap(PAIR) is None:
            raise MissingCapError(f"no cap_rate stored for {PAIR.symbol}")
        results = tuple(
            PublishResult(
                account_id=ad.accounts[0],
                platform=ad.platform,
                pair=ad.pair.symbol,
                status="dry_run" if dry_run else "created",
                price=ad.price,
                adv_no=None if dry_run else "9001",
                dry_run=dry_run,
            )
            for ad in self._prices()
        )
        self._last_publish = results
        self.last_problems = self.problems
        return results

    def set_active(self, active: bool, pairs: Sequence[str] | None = None) -> tuple[PublishResult, ...]:
        wanted = {Pair.parse(item).symbol for item in pairs} if pairs else {PAIR.symbol}
        self.last_problems = self.problems
        return tuple(
            PublishResult(
                account_id=ad.accounts[0],
                platform=ad.platform,
                pair=ad.pair.symbol,
                status="updated" if active else "skipped",
                price=ad.price,
                adv_no="9001",
            )
            for ad in self._prices()
            if ad.pair.symbol in wanted
        )

    def run_parser(self, pairs: Sequence[str] | None = None) -> tuple[MarketFetchResult, ...]:
        return (
            MarketFetchResult("binance", PAIR.symbol, 120, 12, Decimal("46.90"), None),
            MarketFetchResult("okx", PAIR.symbol, 80, 0, None, "TransportError: okx unreachable"),
        )

    def snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(
            version=self.version,
            scenario=self.scenarios.active_name(),
            fiat="UAH",
            strategy="fixed_spread",
            rates=(RateRow(PAIR.symbol, self.rates.base(PAIR), self.rates.cap(PAIR)),),
            prices=self._prices(),
            market=(
                MarketRow("binance", PAIR.symbol, Decimal("46.90"), 12, self.clock() - timedelta(minutes=5)),
                MarketRow("okx", PAIR.symbol, Decimal("47.10"), 4, self.clock() - timedelta(minutes=40)),
            ),
            jobs=(JobRow("parser", self.scheduler.next_run(), None),),
            last_publish=self._last_publish,
            engine_problems=self.problems,
        )


#: Engine skip problems used by the "venue has no competitor data" cases (SPEC 7.1).
PROBLEMS = (
    "UAH/USDC okx: MissingMarketDataError: no market middle available for UAH/USDC on okx",
    "UAH/USDC bybit: MissingMarketDataError: copy source UAH/USDC on binance unavailable",
)


class NoPricedAds(StubServices):
    """Façade whose every entry is skipped by the engine: zero results, two problems."""

    def __init__(self) -> None:
        super().__init__()
        self.rates.set_base(PAIR, Decimal("47.00"))
        self.rates.set_cap(PAIR, Decimal("46.50"))
        self.problems = PROBLEMS

    def _prices(self) -> tuple[ComputedAd, ...]:
        return ()


class RecordingAccess(AccessController):
    """AccessController that remembers every decision so the harness can print them."""

    def __init__(self, owner_id: int | None, limiter: RateLimiter | None = None) -> None:
        super().__init__(owner_id, limiter)
        self.decisions: list[tuple[int, Decision]] = []

    def authorize(self, update: Update) -> Decision:
        decision = super().authorize(update)
        self.decisions.append((update.update_id, decision))
        return decision


# -------------------------------------------------------------------------- transcript


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("smoke.telegram")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("  log: %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _print_text(prefix: str, text: str, indent: str = "    ") -> None:
    """Print (possibly multi-line) reply text with a stable indentation."""
    print(prefix + text.replace("\n", "\n" + indent))


def _run_updates(state: StubState, runner: BotRunner) -> list[dict[str, Any]]:
    """Drive the real poll loop once per scripted update; return per-update transcripts."""
    rows: list[dict[str, Any]] = []
    for payload, sender, attachment in SCRIPT:
        before = len(state.sent)
        iterations = runner.run_forever(max_iterations=1)
        rows.append(
            {
                "update_id": payload["update_id"],
                "sender": sender,
                "attachment": attachment,
                "iterations": iterations,
                "replies": list(state.sent[before:]),
            }
        )
    return rows


def main() -> int:
    state = StubState([payload for payload, _, _ in SCRIPT])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    api = TelegramAPI(f"http://127.0.0.1:{port}", TOKEN)
    services = StubServices()
    access = RecordingAccess(OWNER_ID)
    logger = _make_logger()
    runner = BotRunner(services, api, access=access, logger=logger)
    runner.attach_scheduler(services.scheduler)

    _banner("stub Telegram Bot API")
    print(f"server        : http://127.0.0.1:{port} (token {constants.REDACTED})")
    print(f"api           : {api!r}")
    identity = api.get_me()
    print(f"getMe         : id={identity.get('id')} username={identity.get('username')}")
    registered = runner.register_commands()
    print(f"setMyCommands : registered={registered} count={len(constants.TELEGRAM_COMMANDS)}")
    print(f"rate store    : {services.rates_kind}")

    _banner("polling: one real run_forever(max_iterations=1) per scripted update")
    rows = _run_updates(state, runner)
    decisions = dict(access.decisions)

    for row in rows:
        update_id = row["update_id"]
        decision = decisions.get(update_id)
        print(f"update {update_id}: sender={row['sender']} attachment={'yes' if row['attachment'] else 'no'}")
        if decision is None:
            print("  decision: <none - authorize() was not called>")
        else:
            verdict = "allowed" if decision.allow else "rejected"
            print(
                f"  decision: {verdict} reason={decision.reason} silent={'yes' if decision.silent else 'no'}"
            )
        print(f"  outbound: {len(row['replies'])} sendMessage")

    before_drain = len(state.sent)
    drained = runner.run_forever(max_iterations=1)
    print()
    print(
        f"drain poll    : iterations={drained} queued_updates_left={len(state.updates)} "
        f"replies={len(state.sent) - before_drain}"
    )
    print(f"scheduler     : run_due called {services.scheduler.runs} times (once per poll iteration)")
    print(f"next offset   : {runner.offset}  (max update_id + 1)")

    _banner("outbound sendMessage bodies (as recorded by the stub server)")
    for index, body in enumerate(state.sent, start=1):
        print(f"[{index}] chat_id={body.get('chat_id')}")
        _print_text("", str(body.get("text", "")))

    _banner("checks")
    owner_replies = 0
    for row in rows:
        update_id = row["update_id"]
        if row["sender"] == "foreign" or row["attachment"]:
            check(f"update {update_id}: zero replies (sender={row['sender']}, attachment={row['attachment']})", row["replies"] == [])
        else:
            ok = len(row["replies"]) == 1 and row["replies"][0].get("chat_id") == OWNER_ID
            owner_replies += 1 if ok else 0
            check(f"update {update_id}: owner command answered once", ok)
    check(
        "every answer went to the owner's private chat only",
        all(body.get("chat_id") == OWNER_ID for body in state.sent),
    )
    check(
        "foreign /setbase was ignored (base rate is still the owner's value)",
        services.rates.base(PAIR) == Decimal("47.00") and services.rates.cap(PAIR) == Decimal("46.50"),
        f"base={services.rates.base(PAIR)} cap={services.rates.cap(PAIR)}",
    )
    check("setMyCommands reached the stub", len(state.commands) == 1 and len(state.commands[0]["commands"]) == len(constants.TELEGRAM_COMMANDS))
    check("owner commands answered", owner_replies == sum(1 for r in rows if r["sender"] == "owner" and not r["attachment"]))
    check("scheduler ran once per poll iteration", services.scheduler.runs == len(rows) + 1, f"runs={services.scheduler.runs}")
    leaked = sorted({"p2pbot.services", "p2pbot.publisher", "p2pbot.engine"} & set(sys.modules))
    check("the telegram layer never imported services/publisher/engine", leaked == [], f"imported={leaked}")
    by_id = {row["update_id"]: row for row in rows}
    setbase_text = str(by_id[3]["replies"][0].get("text", ""))
    setcap_text = str(by_id[4]["replies"][0].get("text", ""))
    check(
        "/setbase with no cap: value stored and the refresh degrades gracefully",
        setbase_text.startswith("base_rate UAH/USDT = 47.00")
        and "ads not updated: MissingCapError" in setbase_text,
    )
    check(
        "/setcap states the cap rule and pushes prices immediately",
        "can never exceed the cap" in setcap_text and "publish: 3 ads" in setcap_text,
    )

    _banner("probe: injected Transport (p2pbot.exchanges.base) instead of urllib")
    try:
        from p2pbot.exchanges.base import HttpResponse
    except ImportError as exc:
        print(f"skipped: p2pbot.exchanges.base is not importable right now ({exc})")
    else:

        class RecordingTransport:
            """Transport stub: replays scripted updates and records every outbound call."""

            def __init__(self, updates: Sequence[Mapping[str, Any]]) -> None:
                self.requests: list[Any] = []
                self._updates = list(updates)

            def send(self, request: Any, *, timeout: float = 15.0) -> Any:
                self.requests.append(request)
                method = request.url.rsplit("/", 1)[-1]
                if method == "getUpdates":
                    batch = [self._updates.pop(0)] if self._updates else []
                    result: Any = batch
                elif method == "sendMessage":
                    result = {"message_id": 1, "text": dict(request.json_body).get("text", "")}
                else:
                    result = True
                return HttpResponse(status=200, body=json.dumps({"ok": True, "result": result}).encode("utf-8"))

        transport = RecordingTransport([_message(77, OWNER_ID, text="/version")])
        probe_api = TelegramAPI("http://127.0.0.1:1", TOKEN, transport=transport)
        probe_runner = BotRunner(
            StubServices(),
            probe_api,
            access=RecordingAccess(OWNER_ID),
            logger=logger,
        )
        iterations = probe_runner.run_forever(max_iterations=1)
        methods = [request.url.rsplit("/", 1)[-1] for request in transport.requests]
        print(f"no socket used : base_url={probe_api.base_url!r} (never contacted)")
        print(f"transport calls: {methods}")
        sent = [request for request in transport.requests if request.url.rsplit("/", 1)[-1] == "sendMessage"]
        check(
            "injected Transport is used verbatim (HttpRequest/HttpResponse reused)",
            iterations == 1
            and methods == ["getUpdates", "sendMessage"]
            and dict(sent[0].json_body).get("chat_id") == OWNER_ID,
        )

        class FailingTransport:
            """Transport that fails the way ``UrllibTransport`` does, URL (token) included."""

            def send(self, request: Any, *, timeout: float = 15.0) -> Any:
                raise TransportError(f"POST {request.url} failed: connection reset by peer")

        try:
            TelegramAPI("http://127.0.0.1:1", TOKEN, transport=FailingTransport()).get_updates()
            text = "<no exception raised>"
        except TransportError as exc:
            text = f"{type(exc).__name__}: {exc}"
        print(f"transport fault: {text}")
        check("token redacted in injected-transport failures", TOKEN not in text and constants.REDACTED in text)

    _banner("verification 1: command surface (Dispatcher + stub façade, no Telegram I/O)")
    surface_services = StubServices()
    surface_services.rates.set_base(PAIR, Decimal("47.00"))
    surface_services.rates.set_cap(PAIR, Decimal("46.50"))
    dispatcher = Dispatcher(surface_services, logger=logger)
    print(f"registered commands: {', '.join(dispatcher.commands)}")

    def dispatch(text: str, update_id: int = 700) -> str:
        result = dispatcher.dispatch(Update.from_payload(_message(update_id, OWNER_ID, text=text)))
        return "" if result is None else result.text

    for command_text in (
        "/help",
        "/rates",
        "/scenarios",
        "/scenario uah",
        "/parse",
        "/publish --dry",
        "/pause UAH/USDT",
        "/resume",
        "/setbase UAH/USDT",
        "/setbase UAH/USDT abc",
        "/setbase nonsense 47.00",
        "/setcap UAH/USDT 46.50",
        "/publish --bogus",
        "/nope",
        "hello there",
    ):
        reply = dispatch(command_text, update_id=700 + len(command_text))
        head = reply.split("\n")[0]
        extra = "" if reply.count("\n") == 0 else f" (+{reply.count(chr(10))} more lines)"
        print(f"{command_text!r} -> {head}{extra}")
    print("/status ->")
    _print_text("", dispatch("/status"), indent="  ")

    check("/help lists the commands", "/setbase" in dispatch("/help") and "/publish" in dispatch("/help"))
    check("/setbase malformed (arg count) -> usage", dispatch("/setbase UAH/USDT") == "Usage: /setbase <PAIR> <RATE>")
    check("/setbase malformed (rate) -> usage", dispatch("/setbase UAH/USDT abc") == "Usage: /setbase <PAIR> <RATE>")
    check("/setbase malformed (pair) -> usage", dispatch("/setbase nonsense 47.00") == "Usage: /setbase <PAIR> <RATE>")
    check(
        "/setcap states the cap can never be exceeded",
        "can never exceed the cap" in dispatch("/setcap UAH/USDT 46.50"),
    )
    check(
        "/publish --dry maps to refresh_prices(dry_run=True)",
        True in surface_services.publish_calls and "[dry-run]" in dispatch("/publish --dry"),
    )
    check("/publish --bogus -> usage", dispatch("/publish --bogus") == "Usage: /publish [--dry]")
    check("/parse renders per-platform rows incl. errors", "error=TransportError" in dispatch("/parse"))
    check("/pause and /resume reach set_active", dispatch("/pause").startswith("pause") and dispatch("/resume").startswith("resume"))
    check("/scenario unknown -> ConfigError text", dispatch("/scenario nope").startswith("ConfigError: unknown scenario"))
    check(
        "unknown command and free text -> help hint",
        dispatch("/nope") == dispatch("hello there") == "Unknown command. Send /help for the command list.",
    )
    check("/status renders scenario, rates, prices, market, jobs and last publish", all(
        marker in dispatch("/status")
        for marker in ("scenario: uah", "rates:", "prices:", "market:", "jobs:", "last publish:", "[clamped]")
    ))

    failing_services = StubServices()

    def _boom() -> Any:
        raise MissingRateError("no base_rate for UAH/USDT")

    failing_services.snapshot = _boom  # type: ignore[method-assign]
    failure_reply = Dispatcher(failing_services, logger=logger).dispatch(
        Update.from_payload(_message(701, OWNER_ID, text="/status"))
    )
    assert failure_reply is not None
    print(f"façade fault  -> {failure_reply.text}")
    check(
        "façade exception becomes 'TypeName: message' (no traceback)",
        failure_reply.text == "MissingRateError: no base_rate for UAH/USDT",
    )

    _banner("verification 2: a rate change pushes prices immediately (stub façade)")
    rate_services = StubServices()
    rate_dispatcher = Dispatcher(rate_services, logger=logger)

    def rate_dispatch(text: str, update_id: int) -> str:
        result = rate_dispatcher.dispatch(
            Update.from_payload(_message(update_id, OWNER_ID, text=text))
        )
        return "" if result is None else result.text

    print("no cap stored yet: /setbase UAH/USDT 47.00 ->")
    no_cap_reply = rate_dispatch("/setbase UAH/USDT 47.00", 801)
    _print_text("", no_cap_reply, indent="  ")
    check(
        "no cap: base rate stored, refresh degrades gracefully",
        rate_services.rates.base(PAIR) == Decimal("47.00")
        and "ads not updated: MissingCapError" in no_cap_reply,
    )

    print("/setcap UAH/USDT 46.50 ->")
    cap_reply = rate_dispatch("/setcap UAH/USDT 46.50", 802)
    _print_text("", cap_reply, indent="  ")
    check(
        "cap stored, cap rule stated, prices pushed at once",
        rate_services.rates.cap(PAIR) == Decimal("46.50")
        and "can never exceed the cap" in cap_reply
        and "publish: 3 ads" in cap_reply,
    )

    print("/setbase UAH/USDT 45.00 (cap now stored) ->")
    base_reply = rate_dispatch("/setbase UAH/USDT 45.00", 803)
    _print_text("", base_reply, indent="  ")
    check(
        "/setbase refreshes too",
        rate_services.rates.base(PAIR) == Decimal("45.00")
        and "publish: 3 ads" in base_reply
        and rate_services.publish_calls[-1] is False,
    )

    class PartiallyFailing(StubServices):
        """Façade whose first account fails: the reply must show it and keep the rest."""

        def refresh_prices(self, *, dry_run: bool = False) -> tuple[PublishResult, ...]:
            results = super().refresh_prices(dry_run=dry_run)
            broken = replace(results[0], status="error", error="ApiError: rate rejected by venue")
            return (broken, *results[1:])

    failing_services = PartiallyFailing()
    failing_dispatcher = Dispatcher(failing_services, logger=logger)
    failing_dispatcher.dispatch(Update.from_payload(_message(804, OWNER_ID, text="/setbase UAH/USDT 47.00")))
    failing = failing_dispatcher.dispatch(
        Update.from_payload(_message(805, OWNER_ID, text="/setcap UAH/USDT 46.50"))
    )
    assert failing is not None
    print("/setcap with one venue failing ->")
    _print_text("", failing.text, indent="  ")
    check(
        "per-account publish errors are reported, not swallowed",
        "publish: 3 ads (1 error)" in failing.text
        and "ApiError: rate rejected by venue" in failing.text,
    )

    _banner("verification 3: engine skip problems are rendered")
    clean_services = StubServices()
    clean_services.rates.set_base(PAIR, Decimal("47.00"))
    clean_services.rates.set_cap(PAIR, Decimal("46.50"))
    clean_dispatcher = Dispatcher(clean_services, logger=logger)

    def clean_dispatch(text: str, update_id: int) -> str:
        result = clean_dispatcher.dispatch(
            Update.from_payload(_message(update_id, OWNER_ID, text=text))
        )
        return "" if result is None else result.text

    clean_replies = {
        text: clean_dispatch(text, 900 + index)
        for index, text in enumerate(("/status", "/rates", "/publish"))
    }
    print(f"no problems: /publish -> {clean_replies['/publish'].splitlines()[-1]!r}")
    check(
        "no problems -> no 'skipped'/'nothing to push' text anywhere",
        all("skipped" not in text and "nothing to push" not in text for text in clean_replies.values()),
    )

    problem_services = StubServices()
    problem_services.rates.set_base(PAIR, Decimal("47.00"))
    problem_services.rates.set_cap(PAIR, Decimal("46.50"))
    problem_services.problems = PROBLEMS
    problem_dispatcher = Dispatcher(problem_services, logger=logger)

    def problem_dispatch(text: str, update_id: int) -> str:
        result = problem_dispatcher.dispatch(
            Update.from_payload(_message(update_id, OWNER_ID, text=text))
        )
        return "" if result is None else result.text

    print("/publish with 3 pushed results and 2 skipped entries ->")
    publish_reply = problem_dispatch("/publish", 910)
    _print_text("", publish_reply, indent="  ")
    publish_lines = publish_reply.split("\n")
    check(
        "results line followed by exactly one 'skipped 2:' line",
        publish_lines[0] == "publish:"
        and sum(1 for line in publish_lines if line.startswith("  created ")) == 3
        and sum(1 for line in publish_lines if line.startswith("skipped ")) == 1
        and publish_lines[-1] == f"skipped 2: {PROBLEMS[0]}",
    )

    print("/status with the same 2 skipped entries ->")
    status_reply = problem_dispatch("/status", 911)
    _print_text("", status_reply, indent="  ")
    check(
        "/status appends a 'skipped 2:' block listing both problems",
        "skipped 2:" in status_reply
        and f"  {PROBLEMS[0]}" in status_reply
        and f"  {PROBLEMS[1]}" in status_reply,
    )

    rates_reply = problem_dispatch("/rates", 912)
    check(
        "/rates appends the same block",
        "skipped 2:" in rates_reply and f"  {PROBLEMS[0]}" in rates_reply,
    )

    paused_reply = problem_dispatch("/pause", 913)
    check(
        "/pause appends the single 'skipped 2:' line too",
        paused_reply.split("\n")[-1] == f"skipped 2: {PROBLEMS[0]}",
    )

    many_services = StubServices()
    many_services.rates.set_base(PAIR, Decimal("47.00"))
    many_services.rates.set_cap(PAIR, Decimal("46.50"))
    many_services.problems = tuple(
        f"UAH/USDC okx: MissingMarketDataError: no market middle (entry {index})" for index in range(12)
    )
    many_reply = Dispatcher(many_services, logger=logger).dispatch(
        Update.from_payload(_message(914, OWNER_ID, text="/rates"))
    )
    assert many_reply is not None
    many_lines = many_reply.text.split("\n")
    print("/rates with 12 skipped entries (tail) ->")
    for line in many_lines[-4:]:
        print(f"  {line}")
    check(
        "block truncates after 10 lines with '... and K more'",
        "skipped 12:" in many_reply.text
        and sum(1 for line in many_lines if line.startswith("  UAH/USDC")) == 10
        and many_lines[-1] == "  ... and 2 more",
    )

    nothing_services = NoPricedAds()
    nothing_reply = Dispatcher(nothing_services, logger=logger).dispatch(
        Update.from_payload(_message(915, OWNER_ID, text="/publish"))
    )
    assert nothing_reply is not None
    print("/publish with 0 results and 2 skipped entries ->")
    _print_text("", nothing_reply.text, indent="  ")
    check(
        "zero results + problems -> explicit 'nothing to push' wording",
        nothing_reply.text == f"publish:\nnothing to push: {PROBLEMS[0]}",
    )

    _banner("verification 4: rate limiter (sliding window, 20/60s)")
    fake_now = [0.0]
    limiter = RateLimiter(constants.RATE_LIMIT_MAX_MESSAGES, constants.RATE_LIMIT_WINDOW_SECONDS, clock=lambda: fake_now[0])
    outcomes = [limiter.check(OWNER_ID) for _ in range(constants.RATE_LIMIT_MAX_MESSAGES + 1)]
    print(f"checks 1..{constants.RATE_LIMIT_MAX_MESSAGES} within the window : all allowed={all(outcomes[:-1])}")
    print(
        f"check {len(outcomes)} at t=0s          : allowed={outcomes[-1]} "
        f"(rejected calls do not consume the window: in_window={limiter.allow_count(OWNER_ID)})"
    )
    check(f"21st message inside {constants.RATE_LIMIT_WINDOW_SECONDS}s is rejected", outcomes[-1] is False)
    fake_now[0] = 59.0
    still_blocked = limiter.check(OWNER_ID)
    fake_now[0] = 60.0
    freed = limiter.check(OWNER_ID)
    print(f"check at t=59s                : allowed={still_blocked} (still inside the window)")
    print(f"check at t=60s                : allowed={freed} (oldest hit aged out)")
    check("window slides: still blocked at 59s, allowed again at 60s", still_blocked is False and freed is True)

    print()
    print("through the real policy path (AccessController + RateLimiter, 21 owner updates):")
    limited_access = AccessController(OWNER_ID, limiter=RateLimiter(20, 60, clock=lambda: 0.0))
    last = None
    for index in range(21):
        last = limited_access.authorize(Update.from_payload(_message(500 + index, OWNER_ID, text="/status")))
    assert last is not None
    print(
        f"  update #21 -> allow={last.allow} reason={last.reason} silent={last.silent} "
        "(not silent: the owner gets the rate-limit notice)"
    )
    check("21st owner update is refused with reason=rate-limit", last.allow is False and last.reason == "rate-limit")

    _banner("verification 5: non-private chat refused")
    group_update = Update.from_payload(_message(900, OWNER_ID, text="/status", chat_type="supergroup"))
    group_decision = AccessController(OWNER_ID).authorize(group_update)
    print(
        f"owner /status in a supergroup -> allow={group_decision.allow} "
        f"reason={group_decision.reason} silent={group_decision.silent}"
    )
    check(
        "group chat from the owner is refused",
        group_decision.allow is False and group_decision.reason == "non-private-chat",
    )

    _banner("verification 6: token stays redacted in error messages")
    probes = (
        ("HTTP 401 + ok:false", BAD_TOKEN_HTTP_401, False),
        ("HTTP 200 + ok:false, stub echoes the token", BAD_TOKEN_OK_FALSE, True),
    )
    for label, bad_token, expect_marker in probes:
        bad_api = TelegramAPI(f"http://127.0.0.1:{port}", bad_token)
        try:
            bad_api.get_me()
            text = "<no exception raised>"
        except TelegramError as exc:
            text = f"{type(exc).__name__}: {exc}"
        print(f"{label}: {text}")
        check(
            f"{label}: description surfaced and token redacted",
            bad_token not in text
            and "Unauthorized" in text
            and (constants.REDACTED in text) is expect_marker,
        )
    print(f"api repr      : {api!r}")

    _banner("verification 7: poll failures -> logged guidance + exponential backoff")
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    capture_logger = logging.getLogger("smoke.guidance")
    capture_logger.handlers.clear()
    capture_logger.setLevel(logging.INFO)
    capture_logger.propagate = False
    capture_logger.addHandler(_Capture())

    for label, failing_token, expected in (
        ("401", BAD_TOKEN_HTTP_401, "401 Unauthorized"),
        ("409", CONFLICT_TOKEN, "409 Conflict"),
    ):
        captured.clear()
        failing_runner = BotRunner(
            StubServices(),
            TelegramAPI(f"http://127.0.0.1:{port}", failing_token),
            access=RecordingAccess(OWNER_ID),
            logger=capture_logger,
        )
        failing_runner.run_forever(max_iterations=1, sleep=lambda _seconds: None)
        print(f"{label}: {captured[-1] if captured else '<nothing logged>'}")
        check(f"HTTP {label} is logged with explicit guidance", any(expected in line for line in captured))

    sleeps: list[float] = []
    dead_runner = BotRunner(
        StubServices(),
        TelegramAPI(f"http://127.0.0.1:{port}", ABORT_TOKEN),
        access=RecordingAccess(OWNER_ID),
        logger=capture_logger,
    )
    iterations = dead_runner.run_forever(max_iterations=6, sleep=sleeps.append)
    print(f"dropped socket: iterations={iterations} sleeps={sleeps}")
    print(f"  last poll error: {captured[-1][:80]}...")
    check(
        "transport fault: 6 polls, backoff 1s->2s->4s->8s->16s->30s, no crash",
        iterations == 6 and sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0],
    )

    _banner("summary")
    print(f"updates handled : {len(rows)} (+1 drain poll)")
    print(f"sendMessage     : {len(state.sent)} total, 0 for foreign senders, 0 for attachments")
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    if FAILURES:
        print(f"FAILED checks: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
