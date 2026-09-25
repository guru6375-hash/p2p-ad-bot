"""The size-1 PLN edit queue: producer pricing + live-ad matching, consumer ``edit_ad`` calls."""

from __future__ import annotations

import queue
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from p2pbot import edit_queue
from p2pbot.blueprint import load_blueprint
from p2pbot.cli import main
from p2pbot.cron_config import load_cron_config
from p2pbot.edit_queue import (
    EDITED_SIDE,
    QUEUE_SIZE,
    UAH_USDC,
    UAH_USDT,
    AdEdit,
    EditQueueReport,
    run_pln_edit_queue,
    run_uah_rate_queue,
)
from p2pbot.errors import ConfigError
from p2pbot.market import MarketFetchResult
from p2pbot.models import ComputedAd, OwnAd, OwnAdsResult, Pair, PublishResult

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
PLN_USDT = Pair.parse("PLN/USDT")
PLN_USDC = Pair.parse("PLN/USDC")


def _computed(pair: str, platform: str, price: str, accounts: tuple[str, ...]) -> ComputedAd:
    return ComputedAd(
        pair=Pair.parse(pair),
        platform=platform,
        price=Decimal(price),
        source="market_middle",
        cap=Decimal("10"),
        accounts=accounts,
    )


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


class _Engine:
    def __init__(self, ads=(), problems=(), error: BaseException | None = None) -> None:
        self.ads, self.problems, self.error = tuple(ads), tuple(problems), error

    def compute_with_problems(self):
        if self.error is not None:
            raise self.error
        return self.ads, self.problems


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


class _Parser:
    def __init__(self, rows=()) -> None:
        self.rows = tuple(rows)
        self.calls: list[tuple[str, ...]] = []

    def run_once(self, blueprint, pairs=None):
        self.calls.append(tuple(pairs))
        return self.rows


@pytest.fixture
def pln():
    return load_blueprint(SCENARIOS / "pln.json")


def test_queue_holds_a_single_edit_of_buy_ads_only() -> None:
    assert QUEUE_SIZE == 1
    assert EDITED_SIDE == "buy"


def test_each_online_buy_ad_of_a_pln_pair_is_edited_and_sell_ads_are_left_alone(pln) -> None:
    engine = _Engine(
        [
            _computed("PLN/USDT", "binance", "3.83", ("Binance#1",)),
            _computed("PLN/USDC", "binance", "3.74", ("Binance#1",)),
            _computed("PLN/USDT", "bybit", "3.83", ("Bybit#1",)),
        ]
    )
    listings = [
        OwnAdsResult(
            "Binance#1",
            "binance",
            ads=(
                _live("Binance#1", "1"),  # PLN/USDT buy, online -> edited
                _live("Binance#1", "2"),  # a second one of the same pair -> edited too
                _live("Binance#1", "3", side="sell"),  # a sell ad -> never touched
                _live("Binance#1", "4", pair=Pair.parse("EUR/USDT")),  # another pair
                _live("Binance#1", "5", pair=PLN_USDC, status="offline"),  # not online
            ),
        ),
        OwnAdsResult(
            "Bybit#1",
            "bybit",
            ads=(
                _live("Bybit#1", "6", price=Decimal("3.83")),  # already there -> unchanged
                _live("Bybit#1", "7", price=Decimal("3.83"), price_floating_ratio=Decimal("99")),
            ),
        ),
    ]
    publisher = _Publisher(listings)

    report = run_pln_edit_queue(pln, engine, publisher)

    assert [(e.account_id, e.adv_no, e.side, e.price) for e in report.queued] == [
        ("Binance#1", "1", "buy", Decimal("3.83")),
        ("Binance#1", "2", "buy", Decimal("3.83")),
        ("Bybit#1", "7", "buy", Decimal("3.83")),  # floating -> made fixed at the target
    ]
    assert [e.adv_no for e in report.unchanged] == ["6"]
    assert publisher.edits == [
        ("Binance#1", "PLN/USDT", "1", Decimal("3.83"), False),
        ("Binance#1", "PLN/USDT", "2", Decimal("3.83"), False),
        ("Bybit#1", "PLN/USDT", "7", Decimal("3.83"), False),
    ]
    assert "3" not in {edit[2] for edit in publisher.edits}  # the sell ad
    assert [result.status for result in report.results] == ["updated"] * 3
    assert report.problems == ("Binance#1 PLN/USDC: no online buy ad to edit (1 not online)",)
    assert publisher.listed == [["Binance#1", "Bybit#1"]]  # one listing per account
    assert publisher.threads == {threading.current_thread().name}  # consumer = caller


def test_only_pln_pairs_are_considered(pln) -> None:
    engine = _Engine(
        [
            _computed("UAH/USDT", "binance", "47.00", ("Binance#1",)),
            _computed("PLN/USDT", "binance", "3.83", ("Binance#1",)),
        ],
        problems=["UAH/USDC binance: MissingCapError: x", "PLN/USDC okx: MissingCapError: y"],
    )
    listing = OwnAdsResult(
        "Binance#1",
        "binance",
        ads=(_live("Binance#1", "1"), _live("Binance#1", "9", pair=Pair.parse("UAH/USDT"))),
    )
    publisher = _Publisher([listing])

    report = run_pln_edit_queue(pln, engine, publisher)

    assert [edit.adv_no for edit in report.queued] == ["1"]
    assert report.problems == ("PLN/USDC okx: MissingCapError: y",)


def test_a_scenario_without_pln_pairs_does_nothing(base_rate_blueprint) -> None:
    publisher = _Publisher()

    report = run_pln_edit_queue(base_rate_blueprint, _Engine(), publisher, parser=_Parser())

    assert report == EditQueueReport((), (), (), ("scenario base_rate has no PLN pairs",))
    assert publisher.listed == []


def test_the_parser_fetches_only_the_pln_pairs_and_its_failures_are_reported(pln) -> None:
    parser = _Parser(
        [
            MarketFetchResult("binance", PLN_USDT, 20, 2, Decimal("3.83")),
            MarketFetchResult("okx", PLN_USDC, 0, 0, None, error="no ads"),
        ]
    )

    report = run_pln_edit_queue(pln, _Engine(), _Publisher(), parser=parser)

    assert parser.calls == [("PLN/USDT", "PLN/USDC")]
    assert report.problems == ("okx PLN/USDC market: no ads",)


def test_an_unlistable_account_is_reported_and_the_others_still_edited(pln) -> None:
    engine = _Engine([_computed("PLN/USDT", "okx", "4.16", ("Okx#1",)),
                      _computed("PLN/USDT", "binance", "3.83", ("Binance#1",))])
    listings = [
        OwnAdsResult("Okx#1", "okx", error="ApiError: HTTP 404"),
        OwnAdsResult("Binance#1", "binance", ads=(_live("Binance#1", "1"),)),
    ]

    report = run_pln_edit_queue(pln, engine, _Publisher(listings))

    assert [edit.account_id for edit in report.queued] == ["Binance#1"]
    assert report.problems == ("Okx#1: cannot list ads: ApiError: HTTP 404",)


def test_dry_run_is_passed_to_every_edit(pln) -> None:
    engine = _Engine([_computed("PLN/USDT", "binance", "3.83", ("Binance#1",))])
    publisher = _Publisher([OwnAdsResult("Binance#1", "binance", ads=(_live("Binance#1", "1"),))])

    report = run_pln_edit_queue(pln, engine, publisher, dry_run=True)

    assert publisher.edits[0][-1] is True
    assert report.results[0].status == "dry_run"


def test_a_failing_producer_still_releases_the_consumer(pln) -> None:
    report = run_pln_edit_queue(pln, _Engine(error=ValueError("boom")), _Publisher())

    assert report.queued == () and report.results == ()
    assert report.problems == ("producer stopped: ValueError: boom",)


def test_a_failing_edit_is_reported_and_the_queue_keeps_draining(pln) -> None:
    engine = _Engine([_computed("PLN/USDT", "binance", "3.83", ("Binance#1",))])
    listing = OwnAdsResult(
        "Binance#1", "binance", ads=(_live("Binance#1", "1"), _live("Binance#1", "2"))
    )
    publisher = _Publisher([listing], failing_adv_no="1")

    report = run_pln_edit_queue(pln, engine, publisher)

    assert [edit[2] for edit in publisher.edits] == ["2"]
    assert report.problems == (
        "Binance#1 PLN/USDT buy adv 1 -> 3.83 (market_middle): RuntimeError: bug",
    )


def test_the_producer_never_runs_more_than_one_edit_ahead(pln, monkeypatch) -> None:
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
    engine = _Engine([_computed("PLN/USDT", "binance", "3.83", ("Binance#1",))])
    ads = tuple(_live("Binance#1", str(index)) for index in range(5))
    report = run_pln_edit_queue(pln, engine, _SlowPublisher([OwnAdsResult("Binance#1", "binance", ads=ads)]))

    assert len(report.results) == 5
    assert max(sizes) == 1  # never more than one edit waiting
    for index in range(3):  # edit i is taken before edit i+2 can be put
        assert events.index(f"edit:{index}") < events.index(f"put:{index + 2}")


# -- CLI ---------------------------------------------------------------------------------
def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "TELEGRAM_OWNER_ID": "4242",
        "STATE_PATH": str(tmp_path / "var" / "state.json"),
        "MARKET_PATH": str(tmp_path / "var" / "market.json"),
        "ADS_PATH": str(tmp_path / "var" / "ads.json"),
        "SCENARIOS_DIR": str(SCENARIOS),
        "LOG_PATH": "",
    }


def test_cli_pln_edits_prints_the_report(pln, tmp_path: Path, capsys, monkeypatch) -> None:
    edit = AdEdit("Binance#1", "binance", PLN_USDT, "1", Decimal("3.83"), "market_middle", "buy")
    same = AdEdit("Bybit#1", "bybit", PLN_USDT, "6", Decimal("3.83"), "copy:Binance", "sell")
    result = PublishResult("Binance#1", "binance", PLN_USDT, "updated", price=Decimal("3.83"), adv_no="1")
    calls: list[dict] = []

    def fake_run(blueprint, engine, publisher, *, parser, dry_run, pairs):
        calls.append(
            {"blueprint": blueprint.name, "engine": engine, "dry_run": dry_run, "pairs": pairs}
        )
        return EditQueueReport((edit,), (same,), (result,), ("Okx#1: cannot list ads: 404",))

    monkeypatch.setattr(edit_queue, "run_pln_edit_queue", fake_run)
    services = SimpleNamespace(
        scenarios=SimpleNamespace(blueprint=lambda: pln),
        engine_factory=lambda blueprint: "engine",
        publisher=SimpleNamespace(set_blueprint=lambda blueprint: None),
        parser="parser",
        cron=load_cron_config(pairs=["USDT"]),
    )

    assert main(["pln-edits", "--dry-run"], env=_env(tmp_path), services=services) == 0

    assert calls == [
        {"blueprint": "pln", "engine": "engine", "dry_run": True, "pairs": (PLN_USDT,)}
    ]
    assert capsys.readouterr().out.splitlines() == [
        "queued    Binance#1 PLN/USDT buy adv 1 -> 3.83 (market_middle)",
        "unchanged Bybit#1 PLN/USDT sell adv 6 -> 3.83 (copy:Binance)",
        "edited    Binance#1 PLN/USDT updated 3.83 1",
        "problem   Okx#1: cannot list ads: 404",
    ]


@pytest.mark.parametrize(
    ("report", "code", "last_line"),
    [
        (EditQueueReport((), (), (), ()), 0, "no PLN ads to edit"),
        (EditQueueReport((), (), (), ("PLN/USDT binance: MissingCapError: x",)), 1,
         "problem   PLN/USDT binance: MissingCapError: x"),
    ],
)
def test_cli_pln_edits_exit_code(pln, tmp_path, capsys, monkeypatch, report, code, last_line) -> None:
    monkeypatch.setattr(edit_queue, "run_pln_edit_queue", lambda *args, **kwargs: report)
    services = SimpleNamespace(
        scenarios=SimpleNamespace(blueprint=lambda: pln),
        engine_factory=lambda blueprint: None,
        publisher=None,
        parser=None,
    )

    assert main(["pln-edits"], env=_env(tmp_path), services=services) == code
    assert capsys.readouterr().out.splitlines()[-1] == last_line


# -- pair selection (cron_config.PAIRS) -------------------------------------------------
def test_only_the_selected_pairs_are_parsed_and_edited(pln) -> None:
    engine = _Engine(
        [
            _computed("PLN/USDT", "binance", "3.83", ("Binance#1",)),
            _computed("PLN/USDC", "binance", "3.74", ("Binance#1",)),
        ],
        problems=["PLN/USDC okx: MissingCapError: x"],
    )
    listing = OwnAdsResult(
        "Binance#1", "binance", ads=(_live("Binance#1", "1"), _live("Binance#1", "2", pair=PLN_USDC))
    )
    parser = _Parser()

    report = run_pln_edit_queue(pln, engine, _Publisher([listing]), parser=parser, pairs=[PLN_USDC])

    assert parser.calls == [("PLN/USDC",)]
    assert [(edit.adv_no, edit.pair) for edit in report.queued] == [("2", PLN_USDC)]
    assert report.problems == ("PLN/USDC okx: MissingCapError: x",)


def test_a_selected_pair_missing_from_the_scenario_is_reported(pln) -> None:
    engine = _Engine([_computed("PLN/USDT", "binance", "3.83", ("Binance#1",))])
    listing = OwnAdsResult("Binance#1", "binance", ads=(_live("Binance#1", "1"),))

    report = run_pln_edit_queue(pln, engine, _Publisher([listing]), pairs=["PLN/USDT", "PLN/BTC"])

    assert [edit.adv_no for edit in report.queued] == ["1"]
    assert report.problems == ("PLN/BTC is not a PLN pair of scenario pln",)


def test_no_selected_pair_in_the_scenario_does_nothing(pln) -> None:
    publisher = _Publisher()

    report = run_pln_edit_queue(pln, _Engine(), publisher, parser=_Parser(), pairs=["PLN/BTC"])

    assert report == EditQueueReport((), (), (), ("PLN/BTC is not a PLN pair of scenario pln",))
    assert publisher.listed == []


# -- /setrate: the UAH price ladders ------------------------------------------------------
STEPS = {"binance": Decimal("0.25"), "okx": Decimal("0.01"), "bybit": Decimal("0.01")}
CAPS = {"UAH/USDT": Decimal("50.00"), "UAH/USDC": Decimal("50.00")}


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
    report = run_uah_rate_queue("45.00", _Publisher([_binance1()]), steps=STEPS, caps=CAPS)

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

    report = run_uah_rate_queue("45.25", _Publisher([listing]), steps=STEPS, caps=CAPS)

    assert list(_ladder(report, UAH_USDT).values()) == [
        Decimal("45.25"), Decimal("45.24"), Decimal("45.23"), Decimal("45.22"), Decimal("45.21"),
    ]
    assert _ladder(report, UAH_USDC) == {"u1": Decimal("45.24")}


def test_the_ladder_never_exceeds_the_stored_cap() -> None:
    caps = {"UAH/USDT": Decimal("44.90"), "UAH/USDC": Decimal("44.60")}

    report = run_uah_rate_queue("45.00", _Publisher([_binance1()]), steps=STEPS, caps=caps)

    assert list(_ladder(report, UAH_USDT).values()) == [Decimal("44.90"), Decimal("44.65"), Decimal("44.40")]
    assert list(_ladder(report, UAH_USDC).values()) == [Decimal("44.60"), Decimal("44.35"), Decimal("44.10")]
    assert all(edit.price <= caps[edit.pair.symbol] for edit in report.queued + report.unchanged)
    assert {edit.source.split(" #")[0] for edit in report.queued} == {
        "setrate capped at 44.90", "setrate capped at 44.60",
    }


def test_the_uah_pairs_need_no_cap_and_a_stored_one_still_limits() -> None:
    caps = {"UAH/USDT": Decimal("44.90")}  # no UAH/USDC cap at all

    report = run_uah_rate_queue("45.00", _Publisher([_binance1()]), steps=STEPS, caps=caps)

    assert list(_ladder(report, UAH_USDT).values()) == [Decimal("44.90"), Decimal("44.65"), Decimal("44.40")]
    assert list(_ladder(report, UAH_USDC).values()) == [Decimal("44.75"), Decimal("44.50"), Decimal("44.25")]
    assert report.problems == ()


def test_no_caps_at_all_prices_the_ladders_from_the_rate() -> None:
    report = run_uah_rate_queue("45.00", _Publisher([_binance1()]), steps=STEPS, caps={})

    assert list(_ladder(report, UAH_USDT).values())[0] == Decimal("45.00")
    assert list(_ladder(report, UAH_USDC).values())[0] == Decimal("44.75")
    assert report.problems == ()


def test_edits_are_queued_so_no_ad_passes_through_a_neighbours_price() -> None:
    # moving up: the top ad first; moving down: the bottom ad first
    up = OwnAdsResult("Bybit#1", "bybit", ads=(_uah("Bybit#1", "a", "45.00"), _uah("Bybit#1", "b", "44.99")))
    down = OwnAdsResult("Okx#1", "okx", ads=(_uah("Okx#1", "c", "46.00"), _uah("Okx#1", "d", "45.99")))

    rising = run_uah_rate_queue("45.50", _Publisher([up]), steps=STEPS, caps=CAPS)
    falling = run_uah_rate_queue("45.50", _Publisher([down]), steps=STEPS, caps=CAPS)

    assert [edit.adv_no for edit in rising.queued] == ["a", "b"]
    assert [edit.adv_no for edit in falling.queued] == ["d", "c"]


def test_ads_already_on_their_rung_are_unchanged() -> None:
    listing = OwnAdsResult(
        "Bybit#1", "bybit", ads=(_uah("Bybit#1", "a", "45.25"), _uah("Bybit#1", "b", "45.00"))
    )

    report = run_uah_rate_queue("45.25", _Publisher([listing]), steps=STEPS, caps=CAPS)

    assert [edit.adv_no for edit in report.unchanged] == ["a"]
    assert [(edit.adv_no, edit.price) for edit in report.queued] == [("b", Decimal("45.24"))]


def test_setrate_reports_missing_offline_and_unlistable_ads() -> None:
    listings = [
        OwnAdsResult("Binance#1", "binance", ads=(_uah("Binance#1", "b1", "45", status="offline"),)),
        OwnAdsResult("Okx#1", "okx", error="ApiError: HTTP 404"),
    ]

    report = run_uah_rate_queue("47.00", _Publisher(listings), steps=STEPS, caps=CAPS)

    assert report.queued == ()
    assert report.problems == (
        "Okx#1: cannot list ads: ApiError: HTTP 404",
        "Binance#1 UAH/USDT: no online buy ad to edit (1 not online)",
        "Binance#1 UAH/USDC: no online buy ad to edit",
    )


def test_setrate_quantizes_the_rate_and_passes_dry_run_on() -> None:
    publisher = _Publisher([OwnAdsResult("Binance#1", "binance", ads=(_uah("Binance#1", "b1", "46"),))])

    report = run_uah_rate_queue("47.005", publisher, steps=STEPS, caps=CAPS, dry_run=True)

    assert report.queued[0].price == Decimal("47.01")
    assert publisher.edits[0][-1] is True
    assert report.results[0].status == "dry_run"


def test_setrate_reports_a_missing_step_and_non_positive_rungs() -> None:
    listings = [
        OwnAdsResult("Binance#1", "binance", ads=(_uah("Binance#1", "b1", "1"), _uah("Binance#1", "b2", "0.9"))),
        OwnAdsResult("Okx#1", "okx", ads=(_uah("Okx#1", "o1", "1"),)),
    ]

    report = run_uah_rate_queue(
        "0.25", _Publisher(listings), steps={"binance": Decimal("0.25")}, caps=CAPS,
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
        run_uah_rate_queue(rate, publisher, steps=STEPS, caps=CAPS)
    assert publisher.listed == []
