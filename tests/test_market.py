"""Market filtering (strict thresholds), middle price, snapshot store and parser (SPEC 8)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from p2pbot.constants import BINANCE_FILTERS, FILTERS_BY_PLATFORM, OKX_FILTERS
from p2pbot.errors import ConfigError, MissingMarketDataError, TransportError
from p2pbot.market import (
    MarketFetchResult,
    MarketParser,
    MarketStore,
    build_snapshot,
    default_filters_for,
    enabled_plans,
    filter_ads,
    middle_price,
    snapshot_key,
)
from p2pbot.models import CompetitorAd, Filters, MarketSnapshot, Pair

from conftest import make_ad, make_snapshot

UAH_USDT = Pair.parse("UAH/USDT")


# -- filter_ads: Binance strict boundaries ---------------------------------------------
@pytest.mark.parametrize(
    ("field", "value", "kept"),
    [
        ("month_order_count", "499", False),
        ("month_order_count", "500", False),
        ("month_order_count", "500.0001", True),
        ("month_order_count", "501", True),
        ("positive_rate", "0.96", False),
        ("positive_rate", "0.97", False),
        ("positive_rate", "0.9701", True),
        ("positive_rate", "0.971", True),
        ("month_finish_rate", "0.93", False),
        ("month_finish_rate", "0.94", False),
        ("month_finish_rate", "0.9401", True),
        ("month_finish_rate", "0.941", True),
    ],
)
def test_binance_thresholds_are_strict(field: str, value: str, kept: bool) -> None:
    ad = make_ad(**{field: value})
    assert filter_ads([ad], "binance", BINANCE_FILTERS) == ((ad,) if kept else ())


def test_binance_exactly_at_every_threshold_is_excluded() -> None:
    boundary = make_ad(month_order_count="500", positive_rate="0.97", month_finish_rate="0.94")
    assert filter_ads([boundary], "binance", BINANCE_FILTERS) == ()
    just_above = make_ad(month_order_count="501", positive_rate="0.971", month_finish_rate="0.941")
    assert filter_ads([just_above], "binance", BINANCE_FILTERS) == (just_above,)


@pytest.mark.parametrize("user_type", ["user", "", "  ", "vip"])
def test_non_merchants_are_excluded(user_type: str) -> None:
    assert filter_ads([make_ad(user_type=user_type)], "binance", BINANCE_FILTERS) == ()


def test_merchant_check_is_case_insensitive() -> None:
    ad = make_ad(user_type="Merchant")
    assert filter_ads([ad], "binance", BINANCE_FILTERS) == (ad,)


@pytest.mark.parametrize("field", ["month_order_count", "positive_rate", "month_finish_rate"])
def test_a_missing_metric_fails_closed(field: str) -> None:
    assert filter_ads([make_ad(**{field: None})], "binance", BINANCE_FILTERS) == ()


def test_no_filter_configured_keeps_every_ad() -> None:
    ads = [make_ad(user_type="user", month_order_count=None), make_ad(adv_no="adv-2")]
    assert filter_ads(ads, "binance", None) == tuple(ads)


def test_user_type_none_disables_the_merchant_check() -> None:
    ad = make_ad(user_type="user")
    assert filter_ads([ad], "binance", Filters(user_type=None)) == (ad,)


def test_okx_filter_is_merchant_only() -> None:
    merchant = make_ad(platform="okx", month_order_count=None, positive_rate=None, month_finish_rate=None)
    user = make_ad(platform="okx", user_type="user")
    assert filter_ads([merchant, user], "okx", OKX_FILTERS) == (merchant,)


def test_thresholds_are_not_applied_to_non_binance_venues() -> None:
    """OKX publishes no order-count metrics, so the numeric checks must not run there."""
    ad = make_ad(platform="okx", month_order_count="10", positive_rate="0.5", month_finish_rate="0.5")
    assert filter_ads([ad], "okx", BINANCE_FILTERS) == (ad,)


def test_filter_platform_is_case_insensitive() -> None:
    ad = make_ad()
    assert filter_ads([ad], "BINANCE", BINANCE_FILTERS) == (ad,)
    assert filter_ads([make_ad(month_order_count="1")], "BINANCE", BINANCE_FILTERS) == ()


# -- middle_price ----------------------------------------------------------------------
def test_middle_price_of_empty_range_is_none() -> None:
    assert middle_price([]) is None


def test_middle_price_is_the_quantized_mean_of_the_extremes() -> None:
    assert middle_price([Decimal("46.90"), Decimal("47.10")]) == Decimal("47.00")
    assert middle_price([Decimal("47.10"), Decimal("46.90")]) == Decimal("47.00")
    assert middle_price([Decimal("47.10")]) == Decimal("47.10")


def test_middle_price_rounds_half_up_on_the_fiat_tick() -> None:
    # (46.90 + 47.15) / 2 = 47.025 -> 47.03 on the 0.01 tick
    assert middle_price([Decimal("46.90"), Decimal("47.15")]) == Decimal("47.03")
    assert middle_price([Decimal("4.314"), Decimal("4.315")], "PLN") == Decimal("4.31")
    assert isinstance(middle_price([Decimal("1")]), Decimal)


def test_middle_price_uses_the_default_tick_for_unknown_fiats() -> None:
    assert middle_price([Decimal("1.005"), Decimal("1.005")], "XYZ") == Decimal("1.01")


# -- build_snapshot --------------------------------------------------------------------
def test_build_snapshot_filters_and_records_metadata() -> None:
    kept = make_ad(adv_no="kept")
    dropped = make_ad(adv_no="dropped", month_order_count="10")
    moment = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    snapshot = build_snapshot("BINANCE", "uah/usdt", [kept, dropped], BINANCE_FILTERS, moment)
    assert snapshot.platform == "binance"
    assert snapshot.pair == UAH_USDT
    assert snapshot.ads == (kept, dropped)
    assert snapshot.filtered == (kept,)
    assert snapshot.middle == kept.price
    assert snapshot.fetched_at == moment


def test_build_snapshot_of_an_empty_or_fully_filtered_market_has_no_middle() -> None:
    snapshot = build_snapshot("binance", UAH_USDT, [], BINANCE_FILTERS)
    assert snapshot.middle is None
    assert snapshot.filtered == ()
    assert snapshot.fetched_at is not None
    assert snapshot.fetched_at.tzinfo is not None

    filtered_out = build_snapshot("binance", UAH_USDT, [make_ad(user_type="user")], BINANCE_FILTERS)
    assert filtered_out.middle is None
    assert filtered_out.ads


# -- MarketStore -----------------------------------------------------------------------
def test_store_put_get_and_items() -> None:
    store = MarketStore()
    snapshot = make_snapshot(platform="binance", prices=("46.90", "47.10"))
    store.put(snapshot)
    assert store.get("BINANCE", "uah/usdt") is snapshot
    assert store.items() == (snapshot,)
    assert store.middle("binance", UAH_USDT) == Decimal("47.00")
    assert store.get("okx", UAH_USDT) is None
    assert store.middle("okx", UAH_USDT) is None


def test_store_items_are_sorted_by_platform_then_pair() -> None:
    store = MarketStore()
    store.put(make_snapshot(platform="okx", pair="UAH/USDT"))
    store.put(make_snapshot(platform="binance", pair="UAH/USDC"))
    assert [(s.platform, s.pair.symbol) for s in store.items()] == [
        ("binance", "UAH/USDC"),
        ("okx", "UAH/USDT"),
    ]


def test_store_recomputes_a_missing_middle_from_filtered_ads() -> None:
    ad = make_ad(price="47.02")
    snapshot = MarketSnapshot(platform="binance", pair=UAH_USDT, filtered=(ad,), middle=None)
    store = MarketStore()
    store.put(snapshot)
    assert store.middle("binance", UAH_USDT) == Decimal("47.02")

    empty = MarketSnapshot(platform="okx", pair=UAH_USDT, filtered=(), middle=None)
    store.put(empty)
    assert store.middle("okx", UAH_USDT) is None


def test_store_as_dict_and_from_dict_round_trip() -> None:
    store = MarketStore()
    store.put(make_snapshot(platform="binance", prices=("46.90", "47.10")))
    store.put(make_snapshot(platform="okx", pair="UAH/USDC", prices=("46.75",)))
    payload = json.loads(json.dumps(store.as_dict()))
    restored = MarketStore.from_dict(payload)
    assert restored.as_dict() == store.as_dict()
    assert restored.middle("okx", "UAH/USDC") == Decimal("46.75")


def test_store_accepts_a_bare_sequence_of_snapshots() -> None:
    payload = MarketStore().as_dict()["snapshots"]
    payload.append(make_snapshot(platform="bybit").to_dict())
    store = MarketStore(data=payload)
    assert [snapshot.platform for snapshot in store.items()] == ["bybit"]


def test_store_rejects_malformed_payloads() -> None:
    with pytest.raises(ConfigError, match="must be a mapping or a sequence of snapshots"):
        MarketStore(data="binance")
    with pytest.raises(ConfigError, match="invalid market snapshot payload"):
        MarketStore(data=[{"platform": "binance"}])
    with pytest.raises(ConfigError, match="invalid market snapshot payload"):
        MarketStore(data=[1, 2])


def test_store_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "market.json"
    store = MarketStore(path=path)
    store.put(make_snapshot(platform="binance", prices=("46.90", "47.10")))
    store.save()
    assert path.is_file()
    assert list(tmp_path.glob("**/*.tmp")) == []

    loaded = MarketStore.load(path)
    assert loaded.middle("binance", UAH_USDT) == Decimal("47.00")
    assert loaded.as_dict() == store.as_dict()

    rewritable = MarketStore.load(path)
    rewritable.put(make_snapshot(platform="okx", prices=("46.99",)))
    rewritable.save()
    assert MarketStore.load(path).middle("okx", UAH_USDT) == Decimal("46.99")


def test_store_save_without_path_and_load_without_file(tmp_path: Path) -> None:
    store = MarketStore()
    store.put(make_snapshot())
    store.save()
    assert list(tmp_path.iterdir()) == []
    assert MarketStore.load(tmp_path / "absent.json").items() == ()
    assert MarketStore.load(None).items() == ()


def test_store_load_of_a_corrupt_file_degrades_to_empty(tmp_path: Path, caplog) -> None:
    """A damaged cache must never stop the bot: warn, stay bound to the path, start empty."""
    path = tmp_path / "market.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = MarketStore.load(path)
    assert store.items() == ()
    assert store.path == path
    assert "is unreadable" in caplog.text


@pytest.mark.parametrize("payload", ['"just a string"', "42", "true"])
def test_store_load_of_a_non_snapshot_payload_degrades_to_empty(
    tmp_path: Path, caplog, payload: str
) -> None:
    path = tmp_path / "market.json"
    path.write_text(payload, encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = MarketStore.load(path)
    assert store.items() == ()
    assert store.path == path
    assert "instead of an object/list" in caplog.text


def test_store_load_of_a_list_of_junk_entries_degrades_to_empty(
    tmp_path: Path, caplog
) -> None:
    """Valid JSON whose entries are not snapshots degrades too: one rule, no exceptions."""
    path = tmp_path / "market.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = MarketStore.load(path)
    assert store.items() == ()
    assert store.path == path
    assert "holds invalid snapshot entries" in caplog.text


def test_a_degraded_store_repairs_the_file_when_it_is_saved(tmp_path: Path) -> None:
    path = tmp_path / "market.json"
    path.write_text("{not json", encoding="utf-8")
    store = MarketStore.load(path)
    store.put(make_snapshot(platform="binance", prices=("46.90", "47.10")))
    store.save()
    assert MarketStore.load(path).middle("binance", UAH_USDT) == Decimal("47.00")


def test_store_load_of_a_valid_file_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "market.json"
    seed = MarketStore(path=path)
    seed.put(make_snapshot(platform="okx", prices=("46.99",)))
    seed.save()
    loaded = MarketStore.load(path)
    assert loaded.items() == seed.items()
    assert loaded.middle("okx", UAH_USDT) == Decimal("46.99")


def test_snapshot_key_is_canonical() -> None:
    assert snapshot_key("BINANCE", "uah/usdc") == ("binance", "UAH/USDC")


# -- filters resolution ----------------------------------------------------------------
def test_default_filters_follow_the_blueprint_then_the_hardcoded_policy(
    pln_blueprint, uah_blueprint
) -> None:
    assert default_filters_for("binance", uah_blueprint.pair("UAH/USDT")) == BINANCE_FILTERS
    assert default_filters_for("okx", uah_blueprint.pair("UAH/USDT")) == OKX_FILTERS
    assert default_filters_for("bybit", uah_blueprint.pair("UAH/USDT")) == Filters(user_type="merchant")
    assert default_filters_for("binance", pln_blueprint.pair("PLN/USDT")) == BINANCE_FILTERS


def test_default_filters_fall_back_to_constants_for_a_plan_without_filters() -> None:
    plan = SimpleNamespace()
    assert default_filters_for("binance", plan) == FILTERS_BY_PLATFORM["binance"]
    assert default_filters_for("okx", plan) == FILTERS_BY_PLATFORM["okx"]
    assert default_filters_for("bybit", plan) == Filters(user_type="merchant")


def test_default_filters_accept_a_mapping_override() -> None:
    plan = SimpleNamespace(filters={"binance": {"min_month_order_count": "900"}})
    resolved = default_filters_for("binance", plan)
    assert resolved.min_month_order_count == Decimal("900")
    assert default_filters_for("okx", plan) == OKX_FILTERS


def test_enabled_plans_skips_disabled_entries() -> None:
    enabled = SimpleNamespace(enabled=True, pair="A")
    disabled = SimpleNamespace(enabled=False, pair="B")
    plan = SimpleNamespace(pairs=(disabled, enabled))
    assert enabled_plans(plan) == (enabled,)


# -- MarketParser ----------------------------------------------------------------------
class _StubAdapter:
    """Adapter stand-in: applies the real filter to scripted raw ads."""

    def __init__(self, platform: str, ads=(), error: BaseException | None = None) -> None:
        self.platform = platform
        self.ads = tuple(ads)
        self.error = error
        self.calls: list[tuple[Pair, Filters | None]] = []

    def search_ads(self, pair, *, filters=None, **kwargs) -> MarketSnapshot:
        self.calls.append((pair, filters))
        if self.error is not None:
            raise self.error
        return build_snapshot(
            self.platform,
            pair,
            self.ads,
            filters,
            fetched_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )


def _parser(adapters, store=None, clock=None) -> MarketParser:
    market = store if store is not None else MarketStore()
    return MarketParser(adapters, market, clock)


def test_parser_fetches_market_middle_platforms_only(pln_blueprint) -> None:
    binance = _StubAdapter("binance", [make_ad(platform="binance", pair="PLN/USDT", price="4.31")])
    okx = _StubAdapter("okx", [make_ad(platform="okx", pair="PLN/USDT", price="4.33")])
    store = MarketStore()
    parser = _parser({"binance": binance, "okx": okx}, store)

    results = parser.run_once(pln_blueprint)

    assert [(r.platform, r.pair.symbol) for r in results] == [
        ("binance", "PLN/USDT"),
        ("okx", "PLN/USDT"),
        ("binance", "PLN/USDC"),
        ("okx", "PLN/USDC"),
    ]
    assert all(r.ok for r in results)
    assert results[0].fetched == 1
    assert results[0].kept == 1
    assert results[0].middle == Decimal("4.31")
    assert store.middle("binance", "PLN/USDT") == Decimal("4.31")
    assert parser.market is store
    assert set(parser.adapters) == {"binance", "okx"}


def test_parser_narrows_to_requested_pairs(pln_blueprint) -> None:
    binance = _StubAdapter("binance", [make_ad(platform="binance", pair="PLN/USDT", price="4.31")])
    okx = _StubAdapter("okx", [make_ad(platform="okx", pair="PLN/USDT", price="4.31")])
    results = _parser({"binance": binance, "okx": okx}).run_once(pln_blueprint, pairs=["pln/usdc"])
    assert [(r.platform, r.pair.symbol) for r in results] == [
        ("binance", "PLN/USDC"),
        ("okx", "PLN/USDC"),
    ]


def test_parser_applies_the_resolved_filters(pln_blueprint) -> None:
    binance = _StubAdapter("binance", [make_ad(platform="binance", pair="PLN/USDT", user_type="user")])
    _parser({"binance": binance}).run_once(pln_blueprint, pairs=["PLN/USDT"])
    _pair, filters = binance.calls[0]
    assert filters == BINANCE_FILTERS


def test_parser_captures_a_missing_adapter_without_aborting_the_pass(pln_blueprint) -> None:
    binance = _StubAdapter("binance", [make_ad(platform="binance", pair="PLN/USDT")])
    store = MarketStore()
    results = _parser({"binance": binance}, store).run_once(pln_blueprint, pairs=["PLN/USDT"])

    assert [r.platform for r in results] == ["binance", "okx"]
    okx_result = results[1]
    assert okx_result.ok is False
    assert "no adapter registered for platform 'okx'" in okx_result.error
    assert okx_result.fetched == 0
    assert store.middle("binance", "PLN/USDT") is not None


def test_parser_captures_a_transport_failure_and_leaves_the_snapshot_untouched(
    pln_blueprint,
) -> None:
    binance = _StubAdapter("binance", error=TransportError("connection reset"))
    store = MarketStore()
    parser = _parser({"binance": binance, "okx": _StubAdapter("okx")}, store)

    results = parser.run_once(pln_blueprint, pairs=["PLN/USDT"])

    assert results[0].error == "TransportError: connection reset"
    assert results[0].middle is None
    assert store.get("binance", "PLN/USDT") is None
    assert results[1].ok is True  # the healthy venue still ran


def test_parser_run_once_on_a_blueprint_without_market_sources(uah_blueprint) -> None:
    parser = _parser({"binance": _StubAdapter("binance")})
    assert parser.run_once(uah_blueprint) == ()
    assert parser.middle_for(uah_blueprint, "UAH/USDT", "binance") is None


def test_parser_middle_for_reads_the_store(pln_blueprint) -> None:
    store = MarketStore()
    store.put(make_snapshot(platform="binance", pair="PLN/USDT", prices=("4.30", "4.34")))
    parser = _parser({}, store)
    assert parser.middle_for(pln_blueprint, "PLN/USDT", "binance") == Decimal("4.32")
    assert parser.middle_for(pln_blueprint, "PLN/TRY", "binance") is None


def test_market_fetch_result_ok_flag() -> None:
    assert MarketFetchResult(platform="binance", pair=UAH_USDT).ok is True
    assert MarketFetchResult(platform="binance", pair=UAH_USDT, error="boom").ok is False


def test_parser_stores_the_snapshot_the_adapter_returned(pln_blueprint) -> None:
    """The parser must persist exactly the adapter's snapshot, untouched."""
    adapter = _StubAdapter("binance", [make_ad(platform="binance", pair="PLN/USDT", price="4.33")])
    store = MarketStore()
    _parser({"binance": adapter}, store).run_once(pln_blueprint, pairs=["PLN/USDT"])
    stored = store.get("binance", "PLN/USDT")
    assert stored is not None
    assert stored.middle == Decimal("4.33")
    assert stored.filtered[0].price == Decimal("4.33")
    assert stored.fetched_at is not None


def test_engine_can_price_from_a_parser_produced_snapshot(pln_blueprint, uah_blueprint) -> None:
    """Cross-check: the parser's snapshot feeds ``RateEngine``'s market_middle source."""
    from p2pbot.engine import RateEngine
    from p2pbot.rates import RateStore

    store = MarketStore()
    adapter = _StubAdapter(
        "binance",
        [
            make_ad(platform="binance", pair="PLN/USDT", price="4.29"),
            make_ad(platform="binance", pair="PLN/USDT", price="4.33", adv_no="adv-2"),
        ],
    )
    okx = _StubAdapter("okx", [make_ad(platform="okx", pair="PLN/USDT", price="4.31")])
    parser = MarketParser({"binance": adapter, "okx": okx}, store)
    parser.run_once(pln_blueprint, pairs=["PLN/USDT"])

    rates = RateStore()
    rates.set_cap("PLN/USDT", "10.00")
    ads = RateEngine(pln_blueprint, rates, store).compute_pair(
        pln_blueprint.pair("PLN/USDT"), {}
    )
    by_platform = {ad.platform: ad.price for ad in ads}
    assert by_platform["binance"] == Decimal("4.31")
    assert by_platform["okx"] == Decimal("4.31")
    assert by_platform["bybit"] == Decimal("4.31")  # copy:Binance

    with pytest.raises(MissingMarketDataError):
        RateEngine(pln_blueprint, rates, None).compute_pair(pln_blueprint.pair("PLN/USDT"), {})
