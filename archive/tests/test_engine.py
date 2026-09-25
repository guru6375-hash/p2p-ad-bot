"""Price engine: sources, cap clamping and ordering (SPEC section 7)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace
from typing import Mapping

import pytest

from p2pbot.blueprint import PairPlan
from p2pbot.engine import RateEngine, quantize_price
from p2pbot.errors import (
    MissingCapError,
    MissingMarketDataError,
    MissingRateError,
    PriceError,
)
from p2pbot.market import MarketStore
from p2pbot.models import ComputedAd, Pair
from p2pbot.rates import RateStore

from conftest import make_snapshot

USDT = Pair.parse("UAH/USDT")
USDC = Pair.parse("UAH/USDC")
PLN_USDT = Pair.parse("PLN/USDT")


def _uah_rates(
    *, base: str | None = "47.00", usdc_base: str | None = "46.90", cap: str | None = "60.00"
) -> RateStore:
    store = RateStore()
    if base is not None:
        store.set_base("UAH/USDT", base)
    if usdc_base is not None:
        store.set_base("UAH/USDC", usdc_base)
    if cap is not None:
        store.set_cap("UAH/USDT", cap)
        store.set_cap("UAH/USDC", cap)
    return store


def _prices(ads: tuple[ComputedAd, ...]) -> dict[tuple[str, str], Decimal]:
    return {(ad.pair.symbol, ad.platform): ad.price for ad in ads}


def _plan(**overrides) -> PairPlan:
    defaults = dict(
        pair=USDT,
        anchor=True,
        linked_to=None,
        accounts=("Binance#1", "Okx#1", "Bybit#1"),
        min_amount=Decimal("1000"),
        max_amount=Decimal("200000"),
        payment_methods=("Monobank",),
        price_offset=Decimal("0"),
        enabled=True,
        sources={"binance": "base_rate", "okx": "base_rate", "bybit": "base_rate"},
        filters={},
        platform_accounts={},
    )
    defaults.update(overrides)
    return PairPlan(**defaults)  # type: ignore[arg-type]


# -- cap clamping ----------------------------------------------------------------------
def test_cap_below_every_price_clamps_every_ad(base_rate_blueprint) -> None:
    """Cap 46.50 beats every base rate: every value equals the cap."""
    ads = RateEngine(base_rate_blueprint, _uah_rates(cap="46.50")).compute()
    assert {ad.price for ad in ads} == {Decimal("46.50")}
    assert all(ad.clamped is True for ad in ads)


def test_cap_above_every_price_changes_nothing(base_rate_blueprint) -> None:
    ads = RateEngine(base_rate_blueprint, _uah_rates(cap="47.20")).compute()
    assert all(ad.clamped is False for ad in ads)
    assert _prices(ads)[("UAH/USDT", "binance")] == Decimal("47.00")
    assert _prices(ads)[("UAH/USDC", "binance")] == Decimal("46.90")


def test_cap_equal_to_the_computed_price_is_not_clamped(base_rate_blueprint) -> None:
    ads = RateEngine(base_rate_blueprint, _uah_rates(cap="47.00")).compute()
    usdt = [ad for ad in ads if ad.pair == USDT]
    assert {ad.price for ad in usdt} == {Decimal("47.00")}
    assert all(ad.clamped is False for ad in usdt)


def test_quantize_price_rounds_half_up_and_falls_back_to_the_default_tick() -> None:
    assert quantize_price(Decimal("47.005"), "UAH") == Decimal("47.01")
    assert quantize_price(Decimal("47.004"), "UAH") == Decimal("47.00")
    assert quantize_price(Decimal("4.315"), "pln") == Decimal("4.32")
    assert quantize_price(Decimal("1.005"), "XYZ") == Decimal("1.01")
    assert isinstance(quantize_price(Decimal("1"), "UAH"), Decimal)


# -- missing inputs --------------------------------------------------------------------
def test_missing_base_rate_is_reported_per_pair(base_rate_blueprint) -> None:
    engine = RateEngine(base_rate_blueprint, _uah_rates(base=None))
    with pytest.raises(MissingRateError) as excinfo:
        engine.compute()
    assert "UAH/USDT" in str(excinfo.value)
    assert "binance" in str(excinfo.value)


def test_missing_cap_is_refused_even_when_the_price_is_known(base_rate_blueprint) -> None:
    engine = RateEngine(base_rate_blueprint, _uah_rates(cap=None))
    with pytest.raises(MissingCapError) as excinfo:
        engine.compute()
    assert "UAH/USDT" in str(excinfo.value)


def test_market_middle_without_a_store_or_a_snapshot(pln_blueprint) -> None:
    plan = pln_blueprint.pair("PLN/USDT")
    with pytest.raises(MissingMarketDataError, match="no market store configured"):
        RateEngine(pln_blueprint, _uah_rates(), None).compute_pair(plan, {})

    empty_store = MarketStore()
    with pytest.raises(MissingMarketDataError, match="no market middle available"):
        RateEngine(pln_blueprint, _uah_rates(), empty_store).compute_pair(plan, {})


def test_market_middle_with_an_empty_filtered_range(pln_blueprint) -> None:
    store = MarketStore()
    store.put(make_snapshot(platform="binance", pair="PLN/USDT", prices=()))
    store.put(make_snapshot(platform="okx", pair="PLN/USDT", prices=()))
    rates = RateStore()
    rates.set_cap("PLN/USDT", "10")
    with pytest.raises(MissingMarketDataError):
        RateEngine(pln_blueprint, rates, store).compute_pair(pln_blueprint.pair("PLN/USDT"), {})


def test_copy_source_without_a_computed_price_is_a_missing_market_data_error(base_rate_blueprint) -> None:
    plan = _plan(accounts=("Bybit#1",), sources={"bybit": "copy:Binance"})
    with pytest.raises(MissingMarketDataError, match="to copy into bybit"):
        RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(plan, {})


def test_malformed_copy_source_is_rejected(base_rate_blueprint) -> None:
    plan = _plan(accounts=(), sources={"bybit": "copy:"})
    with pytest.raises(MissingMarketDataError, match="malformed copy source"):
        RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(plan, {})


def test_unknown_source_expression_is_a_price_error(base_rate_blueprint) -> None:
    plan = _plan(accounts=(), sources={"binance": "black_magic"})
    with pytest.raises(PriceError, match="unknown price source 'black_magic'"):
        RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(plan, {})


def test_plan_without_sources_is_a_price_error(base_rate_blueprint) -> None:
    plan = _plan(accounts=(), sources={})
    with pytest.raises(PriceError, match="has no platform price sources resolved"):
        RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(plan, {})


def test_non_positive_price_is_never_published(base_rate_blueprint) -> None:
    rates = RateStore()
    rates.set_base("UAH/USDT", "0.01")
    rates.set_cap("UAH/USDT", "10")
    plan = _plan(price_offset=Decimal("-0.02"), accounts=("Binance#1",), sources={"binance": "base_rate"})
    with pytest.raises(PriceError, match="not publishable"):
        RateEngine(base_rate_blueprint, rates).compute_pair(plan, {})


# -- offsets ---------------------------------------------------------------------------
def test_pair_price_offset_is_added_before_clamping_and_quantizing() -> None:
    import json

    data = json.loads(
        """{
          "version": 1, "name": "offset", "fiat": "UAH", "strategy": "market_middle",
          "pairs": [
            {"pair": "UAH/USDT", "anchor": true, "price_offset": "-0.05",
             "accounts": ["Binance#1"], "platforms": {"binance": {"source": "base_rate"}}},
            {"pair": "UAH/USDC", "anchor": false, "linked_to": "UAH/USDT", "price_offset": "-0.05",
             "accounts": ["Binance#1"], "platforms": {"binance": {"source": "base_rate"}}}
          ]
        }"""
    )
    from p2pbot.blueprint import parse_blueprint

    blueprint = parse_blueprint(data)
    rates = RateStore()
    rates.set_base("UAH/USDT", "47.00")
    rates.set_base("UAH/USDC", "46.75")
    rates.set_cap("UAH/USDT", "60")
    rates.set_cap("UAH/USDC", "60")
    ads = RateEngine(blueprint, rates).compute()
    assert _prices(ads) == {
        ("UAH/USDT", "binance"): Decimal("46.95"),
        ("UAH/USDC", "binance"): Decimal("46.70"),
    }


def test_a_positive_offset_can_trigger_the_cap_clamp(base_rate_blueprint) -> None:
    plan = _plan(price_offset=Decimal("1.00"), accounts=("Binance#1",), sources={"binance": "base_rate"})
    ads = RateEngine(base_rate_blueprint, _uah_rates(cap="46.50")).compute_pair(plan, {})
    assert ads[0].price == Decimal("46.50")
    assert ads[0].clamped is True


# -- copy sources ----------------------------------------------------------------------
def _pln_engine(pln_blueprint, *, binance_middle: str = "4.31", okx_middle: str = "4.31", cap: str = "10.00"):
    store = MarketStore()
    store.put(make_snapshot(platform="binance", pair="PLN/USDT", prices=(binance_middle,)))
    store.put(make_snapshot(platform="okx", pair="PLN/USDT", prices=(okx_middle,)))
    store.put(make_snapshot(platform="binance", pair="PLN/USDC", prices=(binance_middle,)))
    store.put(make_snapshot(platform="okx", pair="PLN/USDC", prices=(okx_middle,)))
    rates = RateStore()
    rates.set_cap("PLN/USDT", cap)
    rates.set_cap("PLN/USDC", cap)
    return RateEngine(pln_blueprint, rates, store), store, rates


def test_bybit_copies_the_binance_price_of_the_same_cycle(pln_blueprint) -> None:
    engine, _store, _rates = _pln_engine(pln_blueprint)
    ads = engine.compute_pair(pln_blueprint.pair("PLN/USDT"), {})

    assert [ad.platform for ad in ads] == ["binance", "okx", "bybit"]
    by_platform = {ad.platform: ad for ad in ads}
    assert by_platform["bybit"].price == by_platform["binance"].price
    assert by_platform["bybit"].source == "copy:Binance"
    assert by_platform["bybit"].base == by_platform["binance"].price
    assert by_platform["bybit"].clamped is False
    assert by_platform["okx"].price == Decimal("4.31")


def test_copied_price_follows_the_binance_price_not_a_stale_value(pln_blueprint) -> None:
    engine, _store, _rates = _pln_engine(pln_blueprint, binance_middle="4.41")
    ads = engine.compute_pair(pln_blueprint.pair("PLN/USDT"), {("binance", "PLN/USDT"): Decimal("9.99")})
    by_platform = {ad.platform: ad for ad in ads}
    assert by_platform["binance"].price == Decimal("4.41")
    assert by_platform["bybit"].price == Decimal("4.41")


def test_copy_inherits_the_cap_clamp_of_the_source_platform(pln_blueprint) -> None:
    engine, _store, _rates = _pln_engine(pln_blueprint, binance_middle="4.41", cap="4.30")
    ads = engine.compute_pair(pln_blueprint.pair("PLN/USDT"), {})
    by_platform = {ad.platform: ad for ad in ads}
    assert by_platform["binance"].price == Decimal("4.30")
    assert by_platform["binance"].clamped is True
    assert by_platform["bybit"].price == Decimal("4.30")
    assert by_platform["bybit"].clamped is True


def test_prices_update_when_the_cap_changes(base_rate_blueprint) -> None:
    rates = _uah_rates(cap="60.00")
    first = _prices(RateEngine(base_rate_blueprint, rates).compute())
    rates.set_cap("UAH/USDT", "46.50")
    rates.set_cap("UAH/USDC", "46.50")
    second = _prices(RateEngine(base_rate_blueprint, rates).compute())
    assert first[("UAH/USDT", "binance")] == Decimal("47.00")
    assert second[("UAH/USDT", "binance")] == Decimal("46.50")


# -- plan/engine bookkeeping -----------------------------------------------------------
def test_compute_pair_does_not_mutate_the_previous_mapping(base_rate_blueprint) -> None:
    previous: dict[tuple[str, str], Decimal] = {}
    RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(base_rate_blueprint.pair("UAH/USDT"), previous)
    assert previous == {}


def test_disabled_plan_produces_nothing(base_rate_blueprint) -> None:
    engine = RateEngine(base_rate_blueprint, _uah_rates())
    assert engine.compute_pair(_plan(enabled=False), {}) == ()
    disabled = _plan(pair=USDC, anchor=False, linked_to=USDT, enabled=False)
    assert engine.compute_pair(disabled, {}) == ()


def test_unknown_platforms_are_ordered_after_the_canonical_ones(base_rate_blueprint) -> None:
    store = MarketStore()
    store.put(make_snapshot(platform="kucoin", pair="UAH/USDT", prices=("47.10",)))
    plan = _plan(accounts=(), sources={"kucoin": "market_middle", "binance": "base_rate"})
    ads = RateEngine(base_rate_blueprint, _uah_rates(), store).compute_pair(plan, {})
    assert [ad.platform for ad in ads] == ["binance", "kucoin"]
    assert ads[1].price == Decimal("47.10")


def test_accounts_for_prefers_the_plan_helper(base_rate_blueprint) -> None:
    plan = _plan(accounts=("Binance#1",), platform_accounts={"binance": ("Binance#2", "Binance#1")})
    ads = RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(plan, {})
    assert ads[0].accounts == ("Binance#1", "Binance#2")


def test_invalid_or_foreign_account_ids_are_dropped_from_the_ad(base_rate_blueprint) -> None:
    """A plan without ``accounts_for`` falls back to parsing its raw ids defensively."""
    plan = SimpleNamespace(
        pair=USDT,
        anchor=True,
        linked_to=None,
        accounts=("Binance#1", "not-an-account", "Okx#9"),
        min_amount=Decimal("1000"),
        max_amount=Decimal("200000"),
        payment_methods=("Monobank",),
        price_offset=Decimal("0"),
        enabled=True,
        sources={"binance": "base_rate"},
    )
    ads = RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(plan, {})
    assert ads[0].accounts == ("Binance#1",)


def test_cap_is_never_exceeded_even_when_it_is_not_on_the_tick() -> None:
    """Defence in depth: a non-tick cap must not round a price above the cap."""

    class _OffTickRates:
        """Rates store stub exposing a cap that is not representable on the 0.01 tick."""

        def __init__(self) -> None:
            self.cap_value = Decimal("46.505")

        def base(self, pair) -> Decimal:  # noqa: D102 - duck-typed store
            return Decimal("47.00")

        def cap(self, pair) -> Decimal:  # noqa: D102 - duck-typed store
            return self.cap_value

    plan = _plan(accounts=("Binance#1",), sources={"binance": "base_rate"})
    ads = RateEngine(_BlueprintStub(), _OffTickRates()).compute_pair(plan, {})
    assert ads[0].price <= Decimal("46.505")
    assert ads[0].price == Decimal("46.50")
    assert ads[0].clamped is True


class _BlueprintStub:
    """The engine only reads ``enabled_pairs``/``pairs`` off the blueprint."""


def test_engine_can_run_without_a_blueprint_attribute_access(base_rate_blueprint) -> None:
    """``compute()`` accepts any object exposing enabled, ordered pair plans."""
    plans = tuple(base_rate_blueprint.pairs)
    stub = type("Stub", (), {"pairs": plans, "enabled_pairs": lambda self: plans})()
    ads = RateEngine(stub, _uah_rates()).compute()
    assert len(ads) == 6


def test_previous_mapping_keys_are_lowercased_platform_and_symbol(base_rate_blueprint) -> None:
    engine = RateEngine(base_rate_blueprint, _uah_rates())
    plan = _plan(accounts=("Bybit#1",), sources={"bybit": "base_rate"})
    ads = engine.compute_pair(plan, {("binance", "UAH/USDT"): Decimal("1.00")})
    assert ads[0].price == Decimal("47.00")


def test_computed_ads_are_immutable(base_rate_blueprint) -> None:
    ad = RateEngine(base_rate_blueprint, _uah_rates()).compute()[0]
    with pytest.raises(FrozenInstanceError):
        ad.price = Decimal("1")  # type: ignore[misc]


def test_mapping_type_hint_is_respected(base_rate_blueprint) -> None:
    """``compute_pair`` accepts any Mapping for ``previous`` (e.g. a live dict view)."""
    previous: Mapping[tuple[str, str], Decimal] = {("binance", "PLN/USDT"): Decimal("4.31")}
    ads = RateEngine(base_rate_blueprint, _uah_rates()).compute_pair(base_rate_blueprint.pair("UAH/USDT"), previous)
    assert len(ads) == 3


# -- partial-failure isolation (SPEC 7.1) ----------------------------------------------
def test_compute_with_problems_isolates_a_missing_market(pln_blueprint) -> None:
    """An empty market store empties the pass but never raises; every entry is reported."""
    rates = RateStore()
    rates.set_cap("PLN/USDT", "10.00")
    rates.set_cap("PLN/USDC", "10.00")
    engine = RateEngine(pln_blueprint, rates, MarketStore())

    ads, problems = engine.compute_with_problems()

    assert ads == ()
    assert [(problem.split()[0], problem.split()[1].rstrip(":")) for problem in problems] == [
        ("PLN/USDT", "binance"),
        ("PLN/USDT", "okx"),
        ("PLN/USDT", "bybit"),
        ("PLN/USDC", "binance"),
        ("PLN/USDC", "okx"),
        ("PLN/USDC", "bybit"),
    ]
    assert problems[0] == (
        "PLN/USDT binance: MissingMarketDataError: no market middle available for PLN/USDT on binance"
    )
    assert problems[2] == (
        "PLN/USDT bybit: MissingMarketDataError: "
        "no computed price for PLN/USDT on binance to copy into bybit"
    )
    assert all("MissingMarketDataError" in problem for problem in problems)
    with pytest.raises(MissingMarketDataError):
        engine.compute()


def test_compute_with_problems_keeps_the_healthy_platforms_of_a_pair(pln_blueprint) -> None:
    rates = RateStore()
    rates.set_cap("PLN/USDT", "10.00")
    rates.set_cap("PLN/USDC", "10.00")
    store = MarketStore()
    store.put(make_snapshot(platform="binance", pair="PLN/USDT", prices=("4.31",)))
    store.put(make_snapshot(platform="binance", pair="PLN/USDC", prices=("4.29",)))
    store.put(make_snapshot(platform="okx", pair="PLN/USDC", prices=("4.29",)))
    engine = RateEngine(pln_blueprint, rates, store)

    ads, problems = engine.compute_with_problems()

    by_pair = {}
    for ad in ads:
        by_pair.setdefault(ad.pair.symbol, set()).add(ad.platform)
    assert by_pair == {"PLN/USDT": {"binance", "bybit"}, "PLN/USDC": {"binance", "okx", "bybit"}}
    assert problems == (
        "PLN/USDT okx: MissingMarketDataError: no market middle available for PLN/USDT on okx",
    )
    bybit = next(ad for ad in ads if ad.pair == PLN_USDT and ad.platform == "bybit")
    assert bybit.price == Decimal("4.31")  # copied from the healthy binance price
    with pytest.raises(MissingMarketDataError):
        engine.compute()


def test_compute_with_problems_skips_only_the_pair_without_a_cap(base_rate_blueprint) -> None:
    rates = RateStore()
    rates.set_base("UAH/USDT", "47.00")
    rates.set_base("UAH/USDC", "46.90")
    rates.set_cap("UAH/USDT", "47.20")  # UAH/USDC deliberately uncapped
    engine = RateEngine(base_rate_blueprint, rates)

    ads, problems = engine.compute_with_problems()

    assert {ad.pair.symbol for ad in ads} == {"UAH/USDT"}
    assert len(ads) == 3
    assert len(problems) == 3
    assert all(problem.startswith("UAH/USDC ") for problem in problems)
    assert all("MissingCapError" in problem for problem in problems)
    assert all(ad.price <= ad.cap for ad in ads)


def test_a_skipped_copy_source_is_reported_not_invented() -> None:
    """When the copied platform is skipped, the copy platform is skipped too."""
    from p2pbot.blueprint import parse_blueprint

    blueprint = parse_blueprint(
        {
            "version": 1,
            "name": "copy-only",
            "fiat": "PLN",
            "strategy": "market_middle",
            "pairs": [
                {"pair": "PLN/USDT", "anchor": True, "accounts": ["Binance#1", "Bybit#1"]}
            ],
        }
    )
    engine = RateEngine(blueprint, RateStore(), MarketStore())

    ads, problems = engine.compute_with_problems()

    assert ads == ()
    assert len(problems) == 2
    assert problems[0].startswith("PLN/USDT binance: MissingMarketDataError")
    assert problems[1].startswith("PLN/USDT bybit: MissingMarketDataError")
    assert "to copy into bybit" in problems[1]


def test_problems_and_ads_never_violate_the_cap(base_rate_blueprint) -> None:
    rates = RateStore()
    rates.set_base("UAH/USDT", "47.00")
    rates.set_base("UAH/USDC", "46.90")
    rates.set_cap("UAH/USDT", "46.50")
    rates.set_cap("UAH/USDC", "46.50")
    ads, problems = RateEngine(base_rate_blueprint, rates).compute_with_problems()

    assert problems == ()
    assert len(ads) == 6
    for ad in ads:
        assert ad.clamped is True
        assert ad.price == Decimal("46.50")
        assert ad.price <= ad.cap


def test_strict_compute_re_raises_the_first_captured_error(base_rate_blueprint) -> None:
    rates = RateStore()
    rates.set_base("UAH/USDT", "47.00")
    rates.set_base("UAH/USDC", "46.90")
    rates.set_cap("UAH/USDT", "47.20")
    engine = RateEngine(base_rate_blueprint, rates)

    with pytest.raises(MissingCapError) as excinfo:
        engine.compute()
    _ads, problems = engine.compute_with_problems()
    assert str(excinfo.value) in problems[0]
    assert problems[0].startswith("UAH/USDC binance:")


def test_compute_pair_stays_strict_while_the_pass_isolates(base_rate_blueprint) -> None:
    rates = RateStore()
    rates.set_base("UAH/USDT", "47.00")
    rates.set_base("UAH/USDC", "46.90")
    rates.set_cap("UAH/USDT", "47.20")
    plan = _plan(pair=USDC, anchor=False, linked_to=USDT)
    engine = RateEngine(base_rate_blueprint, rates)
    with pytest.raises(MissingCapError):
        engine.compute_pair(plan, {})
