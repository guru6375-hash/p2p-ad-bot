"""The size-1 edit queues behind ``/setrate``: the UAH ladder and the flat PLN rate."""

from __future__ import annotations

import queue
import threading
from decimal import Decimal

import pytest

from p2pbot import edit_queue
from p2pbot.edit_queue import (
    EDITED_SIDE,
    PLN_USDC,
    PLN_USDT,
    QUEUE_SIZE,
    UAH_USDC,
    UAH_USDT,
    AdEdit,
    EditQueueReport,
    run_edit_queue,
    run_pln_rate_queue,
    run_uah_rate_queue,
)
from p2pbot.errors import ConfigError
from p2pbot.models import OwnAd, OwnAdsResult, Pair, PublishResult


def _live(account_id: str, adv_no: str, pair: Pair = PLN_USDT, **fields) -> OwnAd:
    values = dict(
        platform=account_id.split("#")[0].lower(),
        account_id=account_id,
        adv_no=adv_no,
        pair=pair,
        side="buy",
        status="online",
        price=Decimal("3.90"),
    )
    values.update(fields)
    return OwnAd(**values)


class _Publisher:
    """Lists fixed live ads and records every ``edit_ad`` call (in the consumer thread)."""

    def __init__(self, listings=(), *, failing_adv_no: str | None = None) -> None:
        self.listings = {listing.account_id: listing for listing in listings}
        self.failing_adv_no = failing_adv_no
        self.listed: list[list[str]] = []
        self.edits: list[tuple] = []
        self.threads: set[str] = set()

    def fetch_own_ads(self, account_ids=None):
        if account_ids is None:  # every configured account
            self.listed.append(None)
            return tuple(self.listings.values())
        self.listed.append(list(account_ids))
        return tuple(
            self.listings.get(account_id, OwnAdsResult(account_id, "", error="unknown account"))
            for account_id in account_ids
        )

    def edit_ad(self, account_id, pair, adv_no, *, price, dry_run):
        self.threads.add(threading.current_thread().name)
        if adv_no == self.failing_adv_no:
            raise RuntimeError("bug")
        self.edits.append((account_id, pair.symbol, adv_no, price, dry_run))
        status = "dry_run" if dry_run else "updated"
        return PublishResult(account_id, "binance", pair, status, price=price, adv_no=adv_no, dry_run=dry_run)


def test_queue_holds_a_single_edit_of_buy_ads_only() -> None:
    assert QUEUE_SIZE == 1
    assert EDITED_SIDE == "buy"


# -- PLN: one flat rate ----------------------------------------------------------------
def test_pln_sets_every_online_buy_ad_of_both_pairs_to_the_rate() -> None:
    listings = [
        OwnAdsResult(
            "Binance#1",
            "binance",
            ads=(
                _live("Binance#1", "t1", price=Decimal("3.80")),
                _live("Binance#1", "c1", pair=PLN_USDC, price=Decimal("3.79")),
                _live("Binance#1", "s1", side="sell"),  # a sell ad: never touched
                _live("Binance#1", "o1", status="offline"),  # offline: left alone
                _live("Binance#1", "u1", pair=UAH_USDT, price=Decimal("43.00")),  # another fiat
            ),
        ),
        OwnAdsResult("Bybit#1", "bybit", ads=(_live("Bybit#1", "y1", price=Decimal("3.70")),)),
    ]
    publisher = _Publisher(listings)

    report = run_pln_rate_queue("3.855", publisher)

    assert publisher.listed == [None]  # every enabled account
    assert publisher.edits == [
        ("Binance#1", "PLN/USDT", "t1", Decimal("3.86"), False),
        ("Binance#1", "PLN/USDC", "c1", Decimal("3.86"), False),
        ("Bybit#1", "PLN/USDT", "y1", Decimal("3.86"), False),
    ]
    assert publisher.threads == {threading.main_thread().name}  # the consumer is the caller
    assert {edit.source for edit in report.queued} == {"setrate"}
    assert report.problems == ("Bybit#1 PLN/USDC: no online buy ad to edit",)


def test_pln_ads_already_at_the_rate_are_unchanged_and_dry_run_is_passed_on() -> None:
    listing = OwnAdsResult(
        "Binance#2",
        "binance",
        ads=(
            _live("Binance#2", "t1", price=Decimal("3.85")),
            _live("Binance#2", "c1", pair=PLN_USDC, price=Decimal("3.80")),
        ),
    )
    publisher = _Publisher([listing])

    report = run_pln_rate_queue("3.85", publisher, dry_run=True)

    assert [edit.adv_no for edit in report.unchanged] == ["t1"]
    assert publisher.edits == [("Binance#2", "PLN/USDC", "c1", Decimal("3.85"), True)]
    assert report.results[0].status == "dry_run"


def test_pln_reports_an_unlistable_account_and_edits_the_others() -> None:
    listings = [
        OwnAdsResult("Okx#1", "okx", error="ApiError: HTTP 404"),
        OwnAdsResult(
            "Binance#1",
            "binance",
            ads=(_live("Binance#1", "t1"), _live("Binance#1", "c1", pair=PLN_USDC)),
        ),
    ]

    report = run_pln_rate_queue("3.85", _Publisher(listings))

    assert len(report.results) == 2
    assert report.problems == ("Okx#1: cannot list ads: ApiError: HTTP 404",)


def test_pln_sets_one_ad_per_pair_and_skips_the_rest_instead_of_failing() -> None:
    # Bybit refuses a second ad of yours at the same price (90043), so it is never sent
    listing = OwnAdsResult(
        "Bybit#1",
        "bybit",
        ads=(
            _live("Bybit#1", "t-low", price=Decimal("3.74")),
            _live("Bybit#1", "t-high", price=Decimal("3.75")),
            _live("Bybit#1", "c-low", pair=PLN_USDC, price=Decimal("3.73")),
            _live("Bybit#1", "c-at", pair=PLN_USDC, price=Decimal("3.21")),
        ),
    )
    publisher = _Publisher([listing])

    report = run_pln_rate_queue("3.21", publisher)

    # USDT: the highest-priced ad takes the rate; USDC: one already has it, nothing is sent
    assert publisher.edits == [("Bybit#1", "PLN/USDT", "t-high", Decimal("3.21"), False)]
    assert [edit.adv_no for edit in report.unchanged] == ["c-at"]
    assert [edit.adv_no for edit in report.skipped] == ["t-low", "c-low"]
    assert report.problems == ()


def test_a_floating_ad_at_the_price_does_not_count_as_holding_it() -> None:
    listing = OwnAdsResult(
        "Binance#1",
        "binance",
        ads=(_live("Binance#1", "f", price=Decimal("3.21"), price_floating_ratio=Decimal("91")),),
    )
    publisher = _Publisher([listing])

    report = run_pln_rate_queue("3.21", publisher)

    assert [edit[2] for edit in publisher.edits] == ["f"]
    assert report.unchanged == () and report.skipped == ()


@pytest.mark.parametrize("rate", ["0", "-1", "abc", 3.85])
def test_pln_refuses_a_rate_that_is_not_a_positive_number(rate: object) -> None:
    publisher = _Publisher([OwnAdsResult("Binance#1", "binance", ads=(_live("Binance#1", "t1"),))])

    with pytest.raises(ConfigError):
        run_pln_rate_queue(rate, publisher)
    assert publisher.listed == []


# -- the queue itself --------------------------------------------------------------------
def test_a_failing_producer_still_releases_the_consumer() -> None:
    def produce(out) -> None:
        raise ValueError("boom")

    report = run_edit_queue(produce, _Publisher())

    assert report.queued == () and report.results == ()
    assert report.problems == ("producer stopped: ValueError: boom",)


def _offer_each(listing: OwnAdsResult, price: str):
    """A producer that queues every ad of ``listing`` at ``price`` (no pricing rules)."""

    def produce(out) -> None:
        for live in listing.ads:
            out.offer(AdEdit(listing.account_id, "binance", live.pair, live.adv_no, Decimal(price), "setrate", "buy"), live)

    return produce


def test_a_failing_edit_is_reported_and_the_queue_keeps_draining() -> None:
    listing = OwnAdsResult(
        "Binance#1", "binance", ads=(_live("Binance#1", "1"), _live("Binance#1", "2"))
    )
    publisher = _Publisher([listing], failing_adv_no="1")

    report = run_edit_queue(_offer_each(listing, "3.83"), publisher)

    assert [edit[2] for edit in publisher.edits] == ["2"]
    assert "Binance#1 PLN/USDT buy adv 1 -> 3.83 (setrate): RuntimeError: bug" in report.problems


def test_the_producer_never_runs_more_than_one_edit_ahead(monkeypatch) -> None:
    events: list[str] = []
    sizes: list[int] = []

    class _RecordingQueue(queue.Queue):
        def put(self, item, block=True, timeout=None):
            super().put(item, block, timeout)
            sizes.append(self.qsize())
            if isinstance(item, AdEdit):
                events.append(f"put:{item.adv_no}")

    class _SlowPublisher(_Publisher):
        def edit_ad(self, account_id, pair, adv_no, **kwargs):
            events.append(f"edit:{adv_no}")
            return super().edit_ad(account_id, pair, adv_no, **kwargs)

    monkeypatch.setattr(edit_queue.queue, "Queue", _RecordingQueue)
    listing = OwnAdsResult("Binance#1", "binance", ads=tuple(_live("Binance#1", str(index)) for index in range(5)))
    report = run_edit_queue(_offer_each(listing, "3.83"), _SlowPublisher([listing]))

    assert len(report.results) == 5
    assert max(sizes) == 1  # never more than one edit waiting
    for index in range(3):  # edit i is taken before edit i+2 can be put
        assert events.index(f"edit:{index}") < events.index(f"put:{index + 2}")


# -- UAH: the STEP ladder ---------------------------------------------------------------
STEPS = {"binance": Decimal("0.25"), "okx": Decimal("0.01"), "bybit": Decimal("0.01")}


def _uah(account_id: str, adv_no: str, price: str, pair: Pair = UAH_USDT, **fields) -> OwnAd:
    return _live(account_id, adv_no, pair=pair, price=Decimal(price), **fields)


def _binance1() -> OwnAdsResult:
    """The live Binance#1 shape: 3 USDT + 3 USDC buy ads, plus ads the ladder must ignore."""
    return OwnAdsResult(
        "Binance#1",
        "binance",
        ads=(
            _uah("Binance#1", "t1", "44.75"),
            _uah("Binance#1", "t2", "44.50"),
            _uah("Binance#1", "t3", "45.05"),
            _uah("Binance#1", "c1", "44.75", pair=UAH_USDC),
            _uah("Binance#1", "c2", "44.50", pair=UAH_USDC),
            _uah("Binance#1", "c3", "45.05", pair=UAH_USDC),
            _uah("Binance#1", "s1", "54.50", side="sell"),  # a sell ad: never touched
            _uah("Binance#1", "o1", "40.00", status="offline"),  # offline: not in the ladder
            _uah("Binance#1", "p1", "3.80", pair=Pair.parse("PLN/USDT")),  # another fiat
        ),
    )


def _ladder(report: EditQueueReport, pair: Pair) -> dict[str, Decimal]:
    prices = {edit.adv_no: edit.price for edit in report.queued + report.unchanged if edit.pair == pair}
    return dict(sorted(prices.items(), key=lambda item: -item[1]))


def test_setrate_builds_a_step_ladder_per_pair_usdc_one_step_below_usdt() -> None:
    report = run_uah_rate_queue("45.00", _Publisher([_binance1()]), steps=STEPS)

    # ads keep their rank (by current price): t3 (45.05) > t1 (44.75) > t2 (44.50)
    assert _ladder(report, UAH_USDT) == {
        "t3": Decimal("45.00"), "t1": Decimal("44.75"), "t2": Decimal("44.50"),
    }
    assert _ladder(report, UAH_USDC) == {
        "c3": Decimal("44.75"), "c1": Decimal("44.50"), "c2": Decimal("44.25"),
    }
    assert {edit.source for edit in report.queued} == {"setrate #1", "setrate #2", "setrate #3"}
    assert report.problems == ()


def test_the_ladder_uses_each_exchange_step_and_scales_to_any_number_of_ads() -> None:
    ads = tuple(_uah("Bybit#1", f"y{index}", f"45.{index:02d}") for index in range(5))
    listing = OwnAdsResult("Bybit#1", "bybit", ads=ads + (_uah("Bybit#1", "u1", "45.10", pair=UAH_USDC),))

    report = run_uah_rate_queue("45.25", _Publisher([listing]), steps=STEPS)

    assert list(_ladder(report, UAH_USDT).values()) == [
        Decimal("45.25"), Decimal("45.24"), Decimal("45.23"), Decimal("45.22"), Decimal("45.21"),
    ]
    assert _ladder(report, UAH_USDC) == {"u1": Decimal("45.24")}


def test_the_ladders_start_at_the_rate_there_is_no_cap() -> None:
    report = run_uah_rate_queue("45.00", _Publisher([_binance1()]), steps=STEPS)

    assert list(_ladder(report, UAH_USDT).values())[0] == Decimal("45.00")
    assert list(_ladder(report, UAH_USDC).values())[0] == Decimal("44.75")
    assert report.problems == ()


def test_edits_are_queued_so_no_ad_passes_through_a_neighbours_price() -> None:
    # moving up: the top ad first; moving down: the bottom ad first
    up = OwnAdsResult("Bybit#1", "bybit", ads=(_uah("Bybit#1", "a", "45.00"), _uah("Bybit#1", "b", "44.99")))
    down = OwnAdsResult("Okx#1", "okx", ads=(_uah("Okx#1", "c", "46.00"), _uah("Okx#1", "d", "45.99")))

    rising = run_uah_rate_queue("45.50", _Publisher([up]), steps=STEPS)
    falling = run_uah_rate_queue("45.50", _Publisher([down]), steps=STEPS)

    assert [edit.adv_no for edit in rising.queued] == ["a", "b"]
    assert [edit.adv_no for edit in falling.queued] == ["d", "c"]


def test_ads_already_on_their_rung_are_unchanged() -> None:
    listing = OwnAdsResult(
        "Bybit#1", "bybit", ads=(_uah("Bybit#1", "a", "45.25"), _uah("Bybit#1", "b", "45.00"))
    )

    report = run_uah_rate_queue("45.25", _Publisher([listing]), steps=STEPS)

    assert [edit.adv_no for edit in report.unchanged] == ["a"]
    assert [(edit.adv_no, edit.price) for edit in report.queued] == [("b", Decimal("45.24"))]


def test_setrate_reports_missing_offline_and_unlistable_ads() -> None:
    listings = [
        OwnAdsResult("Binance#1", "binance", ads=(_uah("Binance#1", "b1", "45", status="offline"),)),
        OwnAdsResult("Okx#1", "okx", error="ApiError: HTTP 404"),
    ]

    report = run_uah_rate_queue("47.00", _Publisher(listings), steps=STEPS)

    assert report.queued == ()
    assert report.problems == (
        "Okx#1: cannot list ads: ApiError: HTTP 404",
        "Binance#1 UAH/USDT: no online buy ad to edit (1 not online)",
        "Binance#1 UAH/USDC: no online buy ad to edit",
    )


def test_setrate_quantizes_the_rate_and_passes_dry_run_on() -> None:
    publisher = _Publisher([OwnAdsResult("Binance#1", "binance", ads=(_uah("Binance#1", "b1", "46"),))])

    report = run_uah_rate_queue("47.005", publisher, steps=STEPS, dry_run=True)

    assert report.queued[0].price == Decimal("47.01")
    assert publisher.edits[0][-1] is True
    assert report.results[0].status == "dry_run"


def test_setrate_reports_a_missing_step_and_non_positive_rungs() -> None:
    listings = [
        OwnAdsResult("Binance#1", "binance", ads=(_uah("Binance#1", "b1", "1"), _uah("Binance#1", "b2", "0.9"))),
        OwnAdsResult("Okx#1", "okx", ads=(_uah("Okx#1", "o1", "1"),)),
    ]

    report = run_uah_rate_queue(
        "0.25", _Publisher(listings), steps={"binance": Decimal("0.25")},
        account_ids=["Binance#1", "Okx#1"],
    )

    assert [(edit.adv_no, edit.price) for edit in report.queued] == [("b1", Decimal("0.25"))]
    assert report.problems == (
        "Binance#1 UAH/USDT ad 2 (b2): price 0.00 is not positive",
        "Binance#1 UAH/USDC: no online buy ad to edit",
        "Okx#1: no STEP for okx",
    )


@pytest.mark.parametrize("rate", ["0", "-1", "abc", 47.0])
def test_setrate_refuses_a_rate_that_is_not_a_positive_number(rate: object) -> None:
    publisher = _Publisher([_binance1()])

    with pytest.raises(ConfigError):
        run_uah_rate_queue(rate, publisher, steps=STEPS)
    assert publisher.listed == []
