"""Blueprint parsing/validation: sources, hardcoded-spread rejection, error paths (SPEC 4)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot.blueprint import (
    HARDCODED_SPREAD_MESSAGE,
    Blueprint,
    load_blueprint,
    load_blueprint_by_name,
    parse_blueprint,
)
from p2pbot.constants import BINANCE_FILTERS, OKX_FILTERS
from p2pbot.errors import BlueprintError
from p2pbot.models import Filters, Pair


def _uah_data() -> dict:
    """A minimal valid UAH fixed_spread blueprint (mirrors scenarios/uah.json)."""
    return {
        "version": 1,
        "name": "uah-mini",
        "fiat": "UAH",
        "strategy": "fixed_spread",
        "defaults": {
            "min_amount": "1000",
            "max_amount": "200000",
            "payment_methods": ["Monobank"],
            "price_offset": "0",
        },
        "pairs": [
            {"pair": "UAH/USDT", "anchor": True, "accounts": ["Binance#1", "Okx#1", "Bybit#1"]},
            {"pair": "UAH/USDC", "anchor": False, "accounts": ["Binance#1", "Okx#1", "Bybit#1"]},
        ],
    }


# -- shipped scenarios -----------------------------------------------------------------
def test_uah_scenario_resolves_fixed_spread_sources(uah_blueprint: Blueprint) -> None:
    assert (uah_blueprint.name, uah_blueprint.fiat, uah_blueprint.strategy) == ("uah", "UAH", "fixed_spread")
    assert uah_blueprint.version == 1
    assert uah_blueprint.parser.enabled is False
    assert uah_blueprint.parser.interval_minutes == 25
    assert uah_blueprint.parser.cron is None

    usdt, usdc = uah_blueprint.pairs
    assert usdt.pair.symbol == "UAH/USDT"
    assert usdt.anchor is True
    assert usdt.linked_to is None
    assert dict(usdt.sources) == {
        "binance": "base_rate",
        "okx": "base_rate",
        "bybit": "base_rate",
    }
    assert usdc.pair.symbol == "UAH/USDC"
    assert usdc.anchor is False
    assert usdc.linked_to == Pair.parse("UAH/USDT")
    assert dict(usdc.sources) == {
        "binance": "base_rate_minus_spread",
        "okx": "base_rate_minus_spread",
        "bybit": "base_rate_minus_spread",
    }
    assert usdt.accounts == ("Binance#1", "Binance#2", "Okx#1", "Bybit#1")
    assert usdt.accounts_for("binance") == ("Binance#1", "Binance#2")
    assert usdt.accounts_for("bybit") == ("Bybit#1",)
    assert uah_blueprint.defaults.min_amount == Decimal("1000")
    assert uah_blueprint.defaults.max_amount == Decimal("200000")
    assert uah_blueprint.defaults.payment_methods == ("Monobank", "PrivatBank")
    assert uah_blueprint.defaults.price_offset == Decimal("0")
    assert _market_sources(uah_blueprint) == ()
    assert uah_blueprint.enabled_pairs() == uah_blueprint.pairs
    assert tuple(plan for plan in uah_blueprint.pairs if plan.anchor) == (usdt,)


def _market_sources(blueprint) -> tuple[tuple[str, str], ...]:
    """``(platform, pair)`` of every ``market_middle`` source (the parser's targets)."""
    return tuple(
        (platform, plan.pair.symbol)
        for plan in blueprint.enabled_pairs()
        for platform, source in plan.sources.items()
        if source == "market_middle"
    )


def test_uah_scenario_carries_the_hardcoded_filters(uah_blueprint: Blueprint) -> None:
    usdt = uah_blueprint.pair("UAH/USDT")
    assert usdt.filters["binance"] == BINANCE_FILTERS
    assert usdt.filters["okx"] == OKX_FILTERS
    assert usdt.filters["bybit"] == Filters(user_type="merchant")


def test_pln_scenario_enables_the_parser_and_copies_bybit_from_binance(
    pln_blueprint: Blueprint,
) -> None:
    assert (pln_blueprint.name, pln_blueprint.fiat, pln_blueprint.strategy) == (
        "pln",
        "PLN",
        "market_middle",
    )
    assert pln_blueprint.parser.enabled is True
    assert pln_blueprint.parser.interval_minutes == 25
    assert pln_blueprint.parser.cron == "*/25 * * * *"

    usdt, usdc = pln_blueprint.pairs
    assert dict(usdt.sources) == {
        "binance": "market_middle",
        "okx": "market_middle",
        "bybit": "copy:Binance",
    }
    assert dict(usdc.sources) == {
        "binance": "market_middle",
        "okx": "market_middle",
        "bybit": "copy:Binance",
    }
    assert usdc.linked_to == Pair.parse("PLN/USDT")
    assert _market_sources(pln_blueprint) == (
        ("binance", "PLN/USDT"),
        ("okx", "PLN/USDT"),
        ("binance", "PLN/USDC"),
        ("okx", "PLN/USDC"),
    )


def test_blueprint_pair_lookup_and_error(uah_blueprint: Blueprint) -> None:
    assert uah_blueprint.pair("uah/usdt").pair == Pair.parse("UAH/USDT")
    with pytest.raises(BlueprintError, match="is not part of blueprint"):
        uah_blueprint.pair("UAH/TRY")
    with pytest.raises(BlueprintError, match="invalid pair"):
        uah_blueprint.pair("not-a-pair")


def test_pair_plan_exposes_resolved_sources_per_platform(uah_blueprint: Blueprint) -> None:
    plan = uah_blueprint.pair("UAH/USDT")
    assert plan.sources["binance"] == "base_rate"
    assert "kucoin" not in plan.sources
    assert set(plan.sources) == {"binance", "okx", "bybit"}


# -- loading ---------------------------------------------------------------------------
def test_load_blueprint_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BlueprintError, match="blueprint file not found"):
        load_blueprint(tmp_path / "absent.json")


def test_load_blueprint_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BlueprintError, match="is not valid JSON"):
        load_blueprint(path)


def test_load_blueprint_reads_a_written_copy(tmp_path: Path, scenarios_dir: Path) -> None:
    path = tmp_path / "copy.json"
    path.write_text((scenarios_dir / "uah.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert load_blueprint(path).name == "uah"


def test_load_blueprint_by_name_accepts_name_or_filename(scenarios_dir: Path) -> None:
    assert load_blueprint_by_name("uah", scenarios_dir).name == "uah"
    assert load_blueprint_by_name("pln.json", scenarios_dir).name == "pln"
    assert load_blueprint_by_name("scenarios/uah", scenarios_dir).name == "uah"


def test_load_blueprint_by_name_reports_available_scenarios(scenarios_dir: Path) -> None:
    with pytest.raises(BlueprintError) as excinfo:
        load_blueprint_by_name("nope", scenarios_dir)
    assert "unknown scenario 'nope'" in str(excinfo.value)
    assert "uah.json" in str(excinfo.value)


def test_load_blueprint_by_name_rejects_empty_name(scenarios_dir: Path) -> None:
    with pytest.raises(BlueprintError, match="scenario name must be a non-empty string"):
        load_blueprint_by_name("  ", scenarios_dir)


# -- top-level validation --------------------------------------------------------------
@pytest.mark.parametrize("payload", [[], "uah", 7])
def test_blueprint_must_be_a_mapping(payload: object) -> None:
    with pytest.raises(BlueprintError, match="blueprint must be a JSON object"):
        parse_blueprint(payload)  # type: ignore[arg-type]


def test_unknown_top_level_key_is_rejected() -> None:
    data = _uah_data()
    data["spread"] = "0.5"
    with pytest.raises(BlueprintError, match=r"unknown key\(s\) spread"):
        parse_blueprint(data)


@pytest.mark.parametrize(
    ("version", "accepted"),
    [(1, True), ("1", True), (2, False), ("2", False), ("x", False), (None, False), (True, False)],
)
def test_version_validation(version: object, accepted: bool) -> None:
    data = _uah_data()
    data["version"] = version
    if accepted:
        assert parse_blueprint(data).version == 1
    else:
        with pytest.raises(BlueprintError):
            parse_blueprint(data)


@pytest.mark.parametrize("name", ["", "   ", None, 5])
def test_name_validation(name: object) -> None:
    data = _uah_data()
    data["name"] = name
    with pytest.raises(BlueprintError, match="'name' must be a non-empty string"):
        parse_blueprint(data)


@pytest.mark.parametrize("fiat", ["USD", "", None, 980])
def test_fiat_validation(fiat: object) -> None:
    data = _uah_data()
    data["fiat"] = fiat
    with pytest.raises(BlueprintError, match="'fiat' must be one of"):
        parse_blueprint(data)


def test_strategy_is_normalised_and_validated() -> None:
    data = _uah_data()
    data["strategy"] = "FIXED_SPREAD"
    assert parse_blueprint(data).strategy == "fixed_spread"
    data["strategy"] = "scalping"
    with pytest.raises(BlueprintError, match="'strategy' must be one of"):
        parse_blueprint(data)


# -- pair validation -------------------------------------------------------------------
def test_pairs_block_must_be_a_non_empty_list() -> None:
    for bad in (None, [], {}, "UAH/USDT"):
        data = _uah_data()
        data["pairs"] = bad
        with pytest.raises(BlueprintError, match="'pairs' must be a non-empty list"):
            parse_blueprint(data)


def test_pair_entry_must_be_an_object() -> None:
    data = _uah_data()
    data["pairs"] = ["UAH/USDT"]
    with pytest.raises(BlueprintError, match=r"pairs\[0\] must be a JSON object"):
        parse_blueprint(data)


def test_pair_fiat_must_match_the_blueprint() -> None:
    data = _uah_data()
    data["pairs"][0]["pair"] = "PLN/USDT"
    with pytest.raises(BlueprintError, match="uses fiat PLN but the blueprint fiat is UAH"):
        parse_blueprint(data)


def test_invalid_pair_symbol_is_reported_with_position() -> None:
    data = _uah_data()
    data["pairs"][0]["pair"] = "UAHUSDT"
    with pytest.raises(BlueprintError, match=r"pairs\[0\].*invalid pair"):
        parse_blueprint(data)


def test_duplicate_pairs_are_rejected() -> None:
    data = _uah_data()
    data["pairs"][1]["pair"] = "UAH/USDT"
    with pytest.raises(BlueprintError, match=r"duplicate pair UAH/USDT \(pairs\[1\] and pairs\[0\]\)"):
        parse_blueprint(data)


def test_unknown_pair_key_is_rejected() -> None:
    data = _uah_data()
    data["pairs"][0]["spread"] = "0.5"
    with pytest.raises(BlueprintError, match=HARDCODED_SPREAD_MESSAGE):
        parse_blueprint(data)


@pytest.mark.parametrize("key", ["spread", "min_spread", "spread_uah", "diff"])
def test_every_hardcoded_spread_key_is_rejected_at_pair_level(key: str) -> None:
    data = _uah_data()
    data["pairs"][0][key] = "0.25"
    with pytest.raises(BlueprintError) as excinfo:
        parse_blueprint(data)
    assert HARDCODED_SPREAD_MESSAGE in str(excinfo.value)
    assert f"remove the {key!r} key" in str(excinfo.value)


@pytest.mark.parametrize("key", ["spread", "min_spread", "spread_uah", "diff"])
def test_every_hardcoded_spread_key_is_rejected_at_platform_level(key: str) -> None:
    data = _uah_data()
    data["pairs"][0]["platforms"] = {"binance": {key: "0.25"}}
    with pytest.raises(BlueprintError, match=HARDCODED_SPREAD_MESSAGE):
        parse_blueprint(data)


def test_spread_key_is_rejected_inside_filters() -> None:
    data = _uah_data()
    data["defaults"]["filters"] = {"binance": {"spread": "0.25"}}
    with pytest.raises(BlueprintError, match=HARDCODED_SPREAD_MESSAGE):
        parse_blueprint(data)


# -- accounts --------------------------------------------------------------------------
def test_accounts_must_be_a_list_of_known_platform_ids() -> None:
    data = _uah_data()
    data["pairs"][0]["accounts"] = "Binance#1"
    with pytest.raises(BlueprintError, match="must be a list of account ids"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["accounts"] = [7]
    with pytest.raises(BlueprintError, match="account id must be a string"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["accounts"] = ["Binance1"]
    with pytest.raises(BlueprintError, match="invalid account id"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["accounts"] = ["Kraken#1"]
    with pytest.raises(BlueprintError, match="uses unknown platform 'kraken'"):
        parse_blueprint(data)


def test_account_ids_are_canonicalised_and_deduplicated() -> None:
    data = _uah_data()
    data["pairs"][0]["accounts"] = ["binance#2", "Binance#1", "BINANCE#2"]
    plan = parse_blueprint(data).pair("UAH/USDT")
    assert plan.accounts == ("Binance#2", "Binance#1")
    assert plan.accounts_for("binance") == ("Binance#1", "Binance#2")


def test_pair_without_accounts_has_no_platforms_and_no_sources() -> None:
    data = _uah_data()
    data["pairs"] = [{"pair": "UAH/USDT", "anchor": True, "accounts": []}]
    plan = parse_blueprint(data).pairs[0]
    assert plan.sources == {}
    assert plan.accounts == ()


# -- platform overrides ----------------------------------------------------------------
def test_per_platform_account_override_wins_for_that_platform() -> None:
    data = _uah_data()
    data["pairs"][0]["platforms"] = {"binance": {"accounts": ["Binance#2"]}}
    plan = parse_blueprint(data).pair("UAH/USDT")
    assert plan.accounts_for("binance") == ("Binance#2",)
    assert plan.accounts_for("okx") == ("Okx#1",)
    assert dict(plan.sources) == {"binance": "base_rate", "okx": "base_rate", "bybit": "base_rate"}


def test_platform_override_can_introduce_a_platform_without_accounts() -> None:
    data = _uah_data()
    data["pairs"][0]["accounts"] = ["Binance#1"]
    data["pairs"][0]["platforms"] = {"bybit": {"source": "base_rate"}}
    plan = parse_blueprint(data).pair("UAH/USDT")
    assert set(plan.sources) == {"binance", "bybit"}
    assert plan.accounts_for("bybit") == ()


def test_unknown_platform_in_platforms_block() -> None:
    data = _uah_data()
    data["pairs"][0]["platforms"] = {"kucoin": {"source": "base_rate"}}
    with pytest.raises(BlueprintError, match="unknown platform 'kucoin'"):
        parse_blueprint(data)


def test_platform_block_must_be_an_object_with_known_keys() -> None:
    data = _uah_data()
    data["pairs"][0]["platforms"] = {"binance": "base_rate"}
    with pytest.raises(BlueprintError, match=r"platforms.binance must be a JSON object"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["platforms"] = {"binance": {"sauce": "base_rate"}}
    with pytest.raises(BlueprintError, match=r"unknown key\(s\) sauce"):
        parse_blueprint(data)


@pytest.mark.parametrize(
    "source",
    ["base_rate", "BASE_RATE", "base_rate_minus_spread", "market_middle", "copy:Binance", "copy:binance"],
)
def test_allowed_source_expressions(source: str) -> None:
    data = _uah_data()
    data["pairs"][1]["platforms"] = {"okx": {"source": source}}
    plan = parse_blueprint(data).pair("UAH/USDC")
    assert plan.sources["okx"] in {
        "base_rate",
        "base_rate_minus_spread",
        "market_middle",
        "copy:Binance",
    }


def test_unknown_source_expression_and_unknown_copy_platform() -> None:
    data = _uah_data()
    data["pairs"][0]["platforms"] = {"binance": {"source": "spread_it"}}
    with pytest.raises(BlueprintError, match="unknown source expression 'spread_it'"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["platforms"] = {"binance": {"source": "copy:Kraken"}}
    with pytest.raises(BlueprintError, match="unknown platform 'kraken' in 'copy:Kraken'"):
        parse_blueprint(data)


def test_copy_requires_the_copied_platform_plan_on_the_same_pair() -> None:
    data = _uah_data()
    data["pairs"][0]["accounts"] = ["Bybit#1"]
    data["pairs"][0]["platforms"] = {"bybit": {"source": "copy:Binance"}}
    with pytest.raises(BlueprintError, match="requires a binance plan for the same pair"):
        parse_blueprint(data)


def test_copy_from_itself_is_rejected() -> None:
    data = _uah_data()
    data["pairs"][0]["platforms"] = {"binance": {"source": "copy:Binance"}}
    with pytest.raises(BlueprintError, match="platform binance cannot copy from itself"):
        parse_blueprint(data)


def test_copy_chains_are_rejected() -> None:
    data = _uah_data()
    data["pairs"][0]["accounts"] = ["Binance#1", "Okx#1"]
    data["pairs"][0]["platforms"] = {
        "binance": {"source": "copy:Okx"},
        "okx": {"source": "copy:Binance"},
    }
    with pytest.raises(BlueprintError, match="copy chains are not allowed"):
        parse_blueprint(data)


# -- amounts / offsets -----------------------------------------------------------------
@pytest.mark.parametrize("key", ["min_amount", "max_amount"])
def test_amounts_must_be_positive(key: str) -> None:
    data = _uah_data()
    data["defaults"][key] = "0"
    with pytest.raises(BlueprintError, match="must be greater than 0"):
        parse_blueprint(data)


def test_max_amount_must_not_be_below_min_amount() -> None:
    data = _uah_data()
    data["defaults"]["min_amount"] = "200000"
    data["defaults"]["max_amount"] = "1000"
    with pytest.raises(BlueprintError, match="must be greater than or equal to"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["min_amount"] = "5000"
    data["pairs"][0]["max_amount"] = "1000"
    with pytest.raises(BlueprintError, match=r"pairs\[0\].max_amount"):
        parse_blueprint(data)


def test_float_amounts_are_rejected() -> None:
    data = _uah_data()
    data["defaults"]["min_amount"] = 1000.0
    with pytest.raises(BlueprintError, match="floats are rejected"):
        parse_blueprint(data)


def test_price_offset_must_parse_and_may_be_negative() -> None:
    blueprint = parse_blueprint(_uah_data())
    assert blueprint.defaults.price_offset == Decimal("0")

    data = _uah_data()
    data["pairs"][0]["price_offset"] = "-0.05"
    assert parse_blueprint(data).pair("UAH/USDT").price_offset == Decimal("-0.05")

    data = _uah_data()
    data["defaults"]["price_offset"] = "abc"
    with pytest.raises(BlueprintError, match="defaults.price_offset"):
        parse_blueprint(data)


def test_pair_amounts_inherit_defaults() -> None:
    blueprint = parse_blueprint(_uah_data())
    usdt = blueprint.pair("UAH/USDT")
    assert (usdt.min_amount, usdt.max_amount) == (Decimal("1000"), Decimal("200000"))
    assert usdt.payment_methods == ("Monobank",)
    data = _uah_data()
    data["pairs"][0]["min_amount"] = "2000"
    data["pairs"][0]["payment_methods"] = ["Monobank", "PrivatBank"]
    assert parse_blueprint(data).pair("UAH/USDT").min_amount == Decimal("2000")


def test_payment_methods_must_be_non_empty_unique_strings() -> None:
    data = _uah_data()
    data["defaults"]["payment_methods"] = "Monobank"
    with pytest.raises(BlueprintError, match="must be a list of payment-method names"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["payment_methods"] = ["Monobank", ""]
    with pytest.raises(BlueprintError, match="must be a non-empty string"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["payment_methods"] = ["Monobank", "Monobank", " PrivatBank "]
    assert parse_blueprint(data).pair("UAH/USDT").payment_methods == ("Monobank", "PrivatBank")


# -- linking ---------------------------------------------------------------------------
def test_auto_link_to_the_single_same_fiat_anchor() -> None:
    blueprint = parse_blueprint(_uah_data())
    assert blueprint.pair("UAH/USDC").linked_to == Pair.parse("UAH/USDT")


def test_non_anchor_pair_without_any_anchor_needs_linked_to() -> None:
    data = _uah_data()
    data["pairs"] = [{"pair": "UAH/USDT", "anchor": False, "accounts": ["Binance#1"]}]
    with pytest.raises(BlueprintError, match="is not an anchor and must declare 'linked_to'"):
        parse_blueprint(data)


def test_auto_linking_only_applies_to_uah() -> None:
    data = _uah_data()
    data["fiat"] = "PLN"
    data["pairs"] = [
        {"pair": "PLN/USDT", "anchor": True, "accounts": ["Binance#1"]},
        {"pair": "PLN/USDC", "anchor": False, "accounts": ["Binance#1"]},
    ]
    with pytest.raises(BlueprintError, match="must declare 'linked_to'"):
        parse_blueprint(data)


def test_several_candidate_anchors_require_explicit_linked_to() -> None:
    data = _uah_data()
    data["pairs"] = [
        {"pair": "UAH/USDT", "anchor": True, "accounts": ["Binance#1"]},
        {"pair": "UAH/TRY", "anchor": True, "accounts": ["Binance#1"]},
        {"pair": "UAH/USDC", "anchor": False, "accounts": ["Binance#1"]},
    ]
    with pytest.raises(BlueprintError, match="several anchor pairs trade UAH"):
        parse_blueprint(data)


def test_linked_to_validation_paths() -> None:
    data = _uah_data()
    data["pairs"][1]["linked_to"] = "UAH/USDC"
    with pytest.raises(BlueprintError, match="cannot link to itself"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][1]["linked_to"] = "PLN/USDT"
    with pytest.raises(BlueprintError, match="trades a different fiat"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][1]["linked_to"] = "UAH/TRY"
    with pytest.raises(BlueprintError, match="is not declared in this blueprint"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"] = [
        {"pair": "UAH/USDT", "anchor": True, "accounts": ["Binance#1"]},
        {"pair": "UAH/USDC", "anchor": False, "accounts": ["Binance#1"]},
        {"pair": "UAH/TRY", "anchor": False, "linked_to": "UAH/USDC", "accounts": ["Binance#1"]},
    ]
    with pytest.raises(BlueprintError, match="which is not marked as an anchor"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][1]["linked_to"] = ""
    with pytest.raises(BlueprintError, match="linked_to must be a pair string"):
        parse_blueprint(data)


def test_anchor_pair_must_not_declare_linked_to() -> None:
    data = _uah_data()
    data["pairs"][0]["linked_to"] = "UAH/USDC"
    with pytest.raises(BlueprintError, match="anchor pair UAH/USDT must not declare 'linked_to'"):
        parse_blueprint(data)


def test_non_anchor_without_linked_to_but_both_sources_resolved() -> None:
    """A fully linked PLN blueprint keeps market_middle sources for its non-anchor pair."""
    data = {
        "version": 1,
        "name": "pln-mini",
        "fiat": "PLN",
        "strategy": "market_middle",
        "pairs": [
            {"pair": "PLN/USDT", "anchor": True, "accounts": ["Binance#1", "Okx#1", "Bybit#1"]},
            {
                "pair": "PLN/USDC",
                "anchor": False,
                "linked_to": "PLN/USDT",
                "accounts": ["Binance#1", "Okx#1", "Bybit#1"],
            },
        ],
    }
    blueprint = parse_blueprint(data)
    assert [plan.pair.symbol for plan in blueprint.pairs] == ["PLN/USDT", "PLN/USDC"]
    assert dict(blueprint.pair("PLN/USDC").sources) == {
        "binance": "market_middle",
        "okx": "market_middle",
        "bybit": "copy:Binance",
    }


def test_anchor_first_ordering_is_enforced_regardless_of_file_order() -> None:
    data = _uah_data()
    data["pairs"].reverse()
    blueprint = parse_blueprint(data)
    assert [plan.pair.symbol for plan in blueprint.pairs] == ["UAH/USDT", "UAH/USDC"]
    assert [plan.anchor for plan in blueprint.pairs] == [True, False]


def test_disabled_pair_is_parsed_but_not_enabled() -> None:
    data = _uah_data()
    data["pairs"][1]["enabled"] = False
    blueprint = parse_blueprint(data)
    assert [plan.pair.symbol for plan in blueprint.enabled_pairs()] == ["UAH/USDT"]
    assert len(blueprint.pairs) == 2
    with pytest.raises(BlueprintError, match="must be true or false"):
        parse_blueprint({**_uah_data(), "pairs": [{**_uah_data()["pairs"][0], "enabled": "yes"}]})


# -- parser / defaults blocks ----------------------------------------------------------
def test_parser_block_validation() -> None:
    data = _uah_data()
    data["parser"] = {"enabled": "true", "interval_minutes": "25"}
    parser = parse_blueprint(data).parser
    assert parser.enabled is True
    assert parser.interval_minutes == 25

    for bad in ({"enabled": "yes"}, {"interval_minutes": 0}, {"interval_minutes": 2.5},
                {"interval_minutes": "x"}, {"cron": ""}, {"cron": 25}, {"extra": 1}, "parser"):
        data = _uah_data()
        data["parser"] = bad
        with pytest.raises(BlueprintError):
            parse_blueprint(data)


def test_defaults_block_validation() -> None:
    data = _uah_data()
    data["defaults"] = "defaults"
    with pytest.raises(BlueprintError, match="'defaults' must be a JSON object"):
        parse_blueprint(data)

    data = _uah_data()
    data["defaults"]["surprise"] = 1
    with pytest.raises(BlueprintError, match=r"unknown key\(s\) surprise"):
        parse_blueprint(data)


def test_filters_block_can_override_thresholds_per_platform() -> None:
    data = _uah_data()
    data["pairs"][0]["filters"] = {"binance": {"min_month_order_count": "900"}}
    plan = parse_blueprint(data).pair("UAH/USDT")
    assert plan.filters["binance"].min_month_order_count == Decimal("900")
    assert plan.filters["binance"].min_positive_rate == Decimal("0.97")
    assert plan.filters["okx"] == OKX_FILTERS


def test_filters_block_rejects_unknown_platform_and_bad_shape() -> None:
    data = _uah_data()
    data["pairs"][0]["filters"] = {"kucoin": {"user_type": "merchant"}}
    with pytest.raises(BlueprintError, match="unknown platform 'kucoin'"):
        parse_blueprint(data)

    data = _uah_data()
    data["pairs"][0]["filters"] = ["merchant"]
    with pytest.raises(BlueprintError, match="must be a JSON object mapping platform"):
        parse_blueprint(data)


def test_blueprint_is_json_round_trippable_through_disk(tmp_path: Path) -> None:
    data = _uah_data()
    path = tmp_path / "reexport.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_blueprint(path).pair("UAH/USDC").linked_to == Pair.parse("UAH/USDT")
