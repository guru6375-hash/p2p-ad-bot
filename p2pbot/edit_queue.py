"""Size-1 producer/consumer queues that reprice live **buy** advertisements.

:func:`run_edit_queue` is the shared machinery: a **producer** (a background thread) puts
:class:`AdEdit` items on a ``queue.Queue(maxsize=1)`` and the **consumer** applies them.
Two producers exist, both behind Telegram ``/setrate``:

* :func:`run_uah_rate_queue` (UAH): every enabled account's own ads are read and its online
  buy ``UAH/USDT`` ads are queued as a descending ladder ``rate``, ``rate - STEP``,
  ``rate - 2*STEP``, ... (``uah_config.STEP[exchange]``); its ``UAH/USDC`` ads the same
  ladder one STEP lower.
* :func:`run_pln_rate_queue` (PLN): every enabled account's online buy ``PLN/USDT`` and
  ``PLN/USDC`` ads are queued at the one rate given.

Sell ads are never touched. Before anything is queued the target price is compared with
the live prices: an ad already at its fixed target price is reported as unchanged, and for
the flat PLN rate only one ad per account and pair takes the price - when another ad of that
pair already has it (or gets it), the rest are skipped, because the venues refuse two of
your ads at the same price (Bybit 90043).

``put`` blocks while an item is waiting, so at most one edit is ever pending. The
**consumer** (the calling thread) takes one item at a time and applies it through
:meth:`AdPublisher.edit_ad`, which re-reads the ad and changes only its price: the ad keeps
its own limits, amount and payment methods (a floating ad becomes a fixed one), and
anything unsafe is refused.

Every venue read of the producer happens before its first ``put``, so the producer and the
consumer never use the adapters at the same time.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from .constants import DEFAULT_PRICE_TICK, PRICE_TICK, SIDE_BUY
from .errors import ConfigError
from .logging_setup import get_logger
from .models import OwnAd, OwnAdsResult, Pair, PublishResult, parse_decimal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .publisher import AdPublisher

__all__ = [
    "QUEUE_SIZE",
    "PLN",
    "EDITED_SIDE",
    "UAH_USDT",
    "UAH_USDC",
    "PLN_USDT",
    "PLN_USDC",
    "AdEdit",
    "EditQueueReport",
    "run_edit_queue",
    "run_pln_rate_queue",
    "run_uah_rate_queue",
]

_log = get_logger(__name__)

#: Capacity of the edit queue: the producer never runs more than one edit ahead.
QUEUE_SIZE = 1
PLN = "PLN"
#: The only trade type the queues edit: sell ads are left alone.
EDITED_SIDE = SIDE_BUY
#: The pairs ``/setrate`` reprices: USDT at the rate, USDC at the rate minus the STEP.
UAH_USDT = Pair.parse("UAH/USDT")
UAH_USDC = Pair.parse("UAH/USDC")
#: The pairs the PLN ``/setrate`` sets, both to the same rate.
PLN_USDT = Pair.parse("PLN/USDT")
PLN_USDC = Pair.parse("PLN/USDC")
#: End-of-stream marker the producer always puts last, even when it fails.
_DONE = object()


@dataclass(frozen=True)
class AdEdit:
    """One queued edit: set the live ad ``adv_no`` of ``account_id`` to ``price``.

    ``side`` is the ad's own trade type, reported for information: ``edit_ad`` always keeps it.
    """

    account_id: str
    platform: str
    pair: Pair
    adv_no: str
    price: Decimal
    source: str
    side: str = ""

    def describe(self) -> str:
        side = f" {self.side}" if self.side else ""
        return (
            f"{self.account_id} {self.pair.symbol}{side} adv {self.adv_no} -> {self.price} "
            f"({self.source})"
        )


@dataclass(frozen=True)
class EditQueueReport:
    """What one run queued, how each edit went, and what was skipped or failed."""

    queued: tuple[AdEdit, ...]
    unchanged: tuple[AdEdit, ...]
    results: tuple[PublishResult, ...]
    problems: tuple[str, ...]
    #: left at their price: another ad of the same account and pair holds the target price
    skipped: tuple[AdEdit, ...] = ()


class EditProducer:
    """What a producer function gets: the queue (through :meth:`offer`) and the report lists."""

    def __init__(self, edits: "queue.Queue[Any]") -> None:
        self._edits = edits
        self.queued: list[AdEdit] = []
        self.unchanged: list[AdEdit] = []
        self.skipped: list[AdEdit] = []
        self.problems: list[str] = []

    def listings(
        self, publisher: "AdPublisher", account_ids: "Iterable[str] | None"
    ) -> dict[str, OwnAdsResult]:
        """The live ads per account (``None``: every configured account); failures reported."""
        ids = None if account_ids is None else list(account_ids)
        found: dict[str, OwnAdsResult] = {}
        for listing in publisher.fetch_own_ads(ids):
            found[listing.account_id] = listing
            if not listing.ok:
                self.problems.append(f"{listing.account_id}: cannot list ads: {listing.error}")
        return found

    def offer_pair(self, listing: OwnAdsResult, pair: Pair, price: Decimal, source: str) -> None:
        """Set one online buy ad of ``pair`` in ``listing`` to ``price``; skip the others.

        A venue refuses a second ad of yours at the same price, so only one ad per account
        and pair can hold ``price``: the ad already at it (then nothing is sent), otherwise
        the highest-priced one. Every other ad keeps its price and is reported as skipped.
        """
        ads = sorted(
            self._online_buy_ads(listing, pair),
            key=lambda ad: (ad.price is None, -(ad.price or Decimal(0)), ad.adv_no),
        )
        holder = next((ad for ad in ads if self._at_price(ad, price)), ads[0] if ads else None)
        for live in ads:
            edit = self._edit(listing, pair, live, price, source)
            if live is holder:
                self.offer(edit, live)
            else:
                self.skipped.append(edit)
                _log.info("skipped %s: another ad already has that price", edit.describe())

    @staticmethod
    def _at_price(live: OwnAd, price: Decimal) -> bool:
        """True when ``live`` already shows the fixed ``price`` (a floating ad never is)."""
        return live.price == price and live.price_floating_ratio is None

    def offer_ladder(
        self, listing: OwnAdsResult, pair: Pair, start: Decimal, step: Decimal, source: str
    ) -> None:
        """Queue the online buy ads of ``pair`` as a descending price ladder.

        The ads are ranked by their current price, highest first (ties by ``adv_no``), and
        the n-th gets ``start - (n - 1) * step``, so every ad keeps its rank and each price
        is one ``step`` below the previous one. The edits are queued ads-moving-up first
        (highest target first) and then ads-moving-down (lowest target first), so no edit
        passes through a neighbour's price, which the venues refuse. A non-positive rung is
        reported instead of queued.
        """
        ranked = sorted(
            self._online_buy_ads(listing, pair),
            key=lambda ad: (ad.price is None, -(ad.price or Decimal(0)), ad.adv_no),
        )
        rungs: list[tuple[AdEdit, OwnAd]] = []
        for index, live in enumerate(ranked):
            price = start - step * index
            if price <= 0:
                self.problems.append(
                    f"{listing.account_id} {pair.symbol} ad {index + 1} ({live.adv_no}): price "
                    f"{price} is not positive"
                )
                continue
            rungs.append(
                (self._edit(listing, pair, live, price, f"{source} #{index + 1}"), live)
            )
        rising = [rung for rung in rungs if rung[1].price is None or rung[0].price > rung[1].price]
        falling = [rung for rung in rungs if rung not in rising]
        for edit, live in sorted(rising, key=lambda rung: -rung[0].price):
            self.offer(edit, live)
        for edit, live in sorted(falling, key=lambda rung: rung[0].price):
            self.offer(edit, live)

    def _online_buy_ads(self, listing: OwnAdsResult, pair: Pair) -> list[OwnAd]:
        """The online buy ads of ``pair``; reports it when there are none."""
        related = [live for live in listing.ads if live.pair == pair and live.side == EDITED_SIDE]
        online = [live for live in related if live.active]
        if not online:
            offline = f" ({len(related)} not online)" if related else ""
            self.problems.append(
                f"{listing.account_id} {pair.symbol}: no online {EDITED_SIDE} ad to edit{offline}"
            )
        return online

    @staticmethod
    def _edit(
        listing: OwnAdsResult, pair: Pair, live: OwnAd, price: Decimal, source: str
    ) -> AdEdit:
        return AdEdit(
            listing.account_id, listing.platform, pair, live.adv_no, price, source, live.side
        )

    def offer(self, edit: AdEdit, live: OwnAd) -> None:
        """Queue ``edit`` (blocking while one is waiting), or record it as unchanged."""
        if self._at_price(live, edit.price):
            self.unchanged.append(edit)
            return
        self._edits.put(edit)  # blocks while the previous edit is still waiting
        self.queued.append(edit)
        _log.info("queued edit %s", edit.describe())


def run_edit_queue(
    produce: Callable[[EditProducer], None],
    publisher: "AdPublisher",
    *,
    dry_run: bool = False,
    name: str = "edit-producer",
    problems: Iterable[str] = (),
) -> EditQueueReport:
    """Run ``produce`` in a producer thread and consume its edits in the calling thread.

    The consumer applies each edit with :meth:`AdPublisher.edit_ad` (``dry_run`` asks it for
    a dry run: ads are read, nothing is sent). ``problems`` are reported first. Every venue
    read of a producer must happen before its first :meth:`EditProducer.offer`, so the
    producer and the consumer never use the adapters at the same time. Returns when the
    producer is done and the queue is drained.
    """
    edits: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_SIZE)
    producer = EditProducer(edits)
    producer.problems.extend(problems)
    results: list[PublishResult] = []

    def target() -> None:
        try:
            produce(producer)
        except Exception as exc:  # the consumer must still be told to stop
            _log.exception("%s failed", name)
            producer.problems.append(f"producer stopped: {type(exc).__name__}: {exc}")
        finally:
            edits.put(_DONE)

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    _consume(publisher, edits, dry_run, results, producer.problems)
    thread.join()
    return EditQueueReport(
        tuple(producer.queued),
        tuple(producer.unchanged),
        tuple(results),
        tuple(producer.problems),
        tuple(producer.skipped),
    )


def run_uah_rate_queue(
    rate: Decimal | str | int,
    publisher: "AdPublisher",
    *,
    steps: Mapping[str, Decimal],
    dry_run: bool = False,
    account_ids: "Iterable[str] | None" = None,
) -> EditQueueReport:
    """``/setrate``: reprice the online buy UAH ads of every account as descending ladders.

    Per account and exchange step (``steps[exchange]``), the ``UAH/USDT`` ads get ``rate``,
    ``rate - step``, ``rate - 2*step``, ... and the ``UAH/USDC`` ads start one step lower:
    ``rate - step``, ``rate - 2*step``, ... (see :meth:`EditProducer.offer_ladder`).

    ``rate`` is quantized to the UAH tick. Every enabled account is read (or only
    ``account_ids``). Raises :class:`ConfigError` for a rate that is not a positive number.
    """
    top = _positive_rate(rate, UAH_USDT.fiat)

    def produce(out: EditProducer) -> None:
        for listing in out.listings(publisher, account_ids).values():
            if not listing.ok:
                continue  # already reported
            step = steps.get(listing.platform)
            if step is None:
                out.problems.append(f"{listing.account_id}: no STEP for {listing.platform}")
                continue
            for pair, start in ((UAH_USDT, top), (UAH_USDC, top - step)):
                out.offer_ladder(listing, pair, start, step, "setrate")

    return run_edit_queue(produce, publisher, dry_run=dry_run, name="uah-rate-producer")


def run_pln_rate_queue(
    rate: Decimal | str | int,
    publisher: "AdPublisher",
    *,
    dry_run: bool = False,
    account_ids: "Iterable[str] | None" = None,
) -> EditQueueReport:
    """``/setrate`` PLN: set every online buy ``PLN/USDT`` and ``PLN/USDC`` ad to ``rate``.

    ``rate`` is quantized to the PLN tick. Every enabled account is read (or only
    ``account_ids``). Raises :class:`ConfigError` for a rate that is not a positive number.
    """
    price = _positive_rate(rate, PLN)

    def produce(out: EditProducer) -> None:
        for listing in out.listings(publisher, account_ids).values():
            if not listing.ok:
                continue  # already reported
            for pair in (PLN_USDT, PLN_USDC):
                out.offer_pair(listing, pair, price, "setrate")

    return run_edit_queue(produce, publisher, dry_run=dry_run, name="pln-rate-producer")


def _positive_rate(rate: Decimal | str | int, fiat: str) -> Decimal:
    """``rate`` quantized (half up) to the fiat's price tick; refused unless positive."""
    tick = PRICE_TICK.get(fiat, DEFAULT_PRICE_TICK)
    price = parse_decimal(rate, "rate").quantize(tick, rounding=ROUND_HALF_UP)
    if price <= 0:
        raise ConfigError(f"rate must be positive, got {rate}")
    return price


def _consume(
    publisher: "AdPublisher",
    edits: "queue.Queue[Any]",
    dry_run: bool,
    results: list[PublishResult],
    problems: list[str],
) -> None:
    """Consumer loop: apply each queued edit with ``edit_ad`` until the producer is done."""
    while True:
        item = edits.get()
        try:
            if item is _DONE:
                return
            try:
                results.append(
                    publisher.edit_ad(
                        item.account_id, item.pair, item.adv_no, price=item.price, dry_run=dry_run
                    )
                )
            except Exception as exc:  # keep draining, or the producer would block forever
                _log.exception("edit failed: %s", item.describe())
                problems.append(f"{item.describe()}: {type(exc).__name__}: {exc}")
        finally:
            edits.task_done()
