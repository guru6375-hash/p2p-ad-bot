"""Domain-model behaviour: parsing, redaction and JSON round-trips (SPEC sections 2-3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from p2pbot.errors import ConfigError
from p2pbot.models import (
    Account,
    AccountRef,
    AdRecord,
    AdSpec,
    AdActionResult,
    CompetitorAd,
    ComputedAd,
    Filters,
    MarketSnapshot,
    Pair,
    PublishResult,
    parse_decimal,
    utcnow,
)


# -- parse_decimal ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("47.005"), Decimal("47.005")),
        (47, Decimal("47")),
        ("47.00", Decimal("47.00")),
        ("  47.00  ", Decimal("47.00")),
        ("+0.97", Decimal("0.97")),
        ("-0.01", Decimal("-0.01")),
    ],
)
def test_parse_decimal_accepts_exact_inputs(raw: object, expected: Decimal) -> None:
    assert parse_decimal(raw, "rate") == expected


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (47.0, "floats are rejected"),
        (True, "boolean"),
        (None, "decimal string"),
        ([], "decimal string"),
        ("", "must not be empty"),
        ("   ", "must not be empty"),
        ("not-a-number", "not a valid decimal number"),
        ("NaN", "finite"),
        ("Infinity", "finite"),
    ],
)
def test_parse_decimal_rejects_unsafe_inputs(raw: object, fragment: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_decimal(raw, "rate")
    assert fragment in str(excinfo.value)
    assert "rate" in str(excinfo.value)


def test_parse_decimal_rejects_float_even_when_integral() -> None:
    """Binary floats must never reach a price, even when they look harmless."""
    with pytest.raises(ConfigError):
        parse_decimal(47.0)


# -- Pair ------------------------------------------------------------------------------
def test_pair_parse_normalises_case_and_symbol() -> None:
    pair = Pair.parse(" uah/usdt ")
    assert (pair.fiat, pair.crypto, pair.symbol, str(pair)) == ("UAH", "USDT", "UAH/USDT", "UAH/USDT")


def test_pair_parse_returns_same_object_for_pair_input() -> None:
    pair = Pair.parse("UAH/USDC")
    assert Pair.parse(pair) is pair


@pytest.mark.parametrize("raw", ["UAHUSDT", "UAH/", "/USDT", "UAH/USDT/EXTRA", "U/USDT", 47, None])
def test_pair_parse_rejects_malformed_symbols(raw: object) -> None:
    with pytest.raises(ConfigError):
        Pair.parse(raw)


def test_pair_constructor_validates_codes() -> None:
    with pytest.raises(ConfigError, match="invalid fiat code"):
        Pair(fiat="U", crypto="USDT")
    with pytest.raises(ConfigError, match="invalid crypto code"):
        Pair(fiat="UAH", crypto="USDT/USDC")


# -- AccountRef / Account ---------------------------------------------------------------
def test_account_ref_parse_canonicalises_id() -> None:
    ref = AccountRef.parse("binance#2")
    assert (ref.platform, ref.index, ref.id, str(ref)) == ("binance", 2, "Binance#2", "Binance#2")


@pytest.mark.parametrize("raw", ["Binance", "Binance#0", "Binance#x", "#1", 3])
def test_account_ref_parse_rejects_malformed_ids(raw: object) -> None:
    with pytest.raises(ConfigError):
        AccountRef.parse(raw)


def test_account_ref_rejects_non_positive_index() -> None:
    with pytest.raises(ConfigError, match="positive integer"):
        AccountRef(platform="binance", index=0)
    with pytest.raises(ConfigError, match="positive integer"):
        AccountRef(platform="binance", index=True)


def test_account_credential_lookup_is_case_insensitive_both_ways() -> None:
    account = Account(ref=AccountRef.parse("Binance#1"), credentials={"api_key": "k", "Secret_Key": "s"})
    assert account.credential("API_KEY") == "k"
    assert account.credential("api_key") == "k"
    assert account.credential("SECRET_KEY") == "s"
    assert account.credential("PASSPHRASE") is None
    assert account.id == "Binance#1"
    assert account.platform == "binance"


def test_account_credential_treats_blank_as_missing() -> None:
    account = Account(ref=AccountRef.parse("Okx#1"), credentials={"API_KEY": "   "})
    assert account.credential("API_KEY") is None
    assert "API_KEY" in account.credentials  # still declared, just unusable


def test_account_require_raises_for_missing_credential() -> None:
    account = Account(ref=AccountRef.parse("Bybit#1"), credentials={"API_KEY": "k"})
    assert account.require("API_KEY") == "k"
    with pytest.raises(ConfigError, match="Bybit#1 is missing required credential SECRET_KEY"):
        account.require("SECRET_KEY")


def test_account_redacted_masks_every_secret_field() -> None:
    account = Account(
        ref=AccountRef.parse("Binance#1"),
        credentials={"API_KEY": "key", "SESSION_COOKIE": "cookie", "NOTE": "plain"},
    )
    redacted = account.redacted()
    assert redacted["API_KEY"] == "***"
    assert redacted["SESSION_COOKIE"] == "***"
    assert redacted["NOTE"] == "plain"
    assert "key" not in json.dumps(redacted)
    assert "cookie" not in json.dumps(redacted)


# -- Filters ---------------------------------------------------------------------------
def test_filters_default_is_merchant_only() -> None:
    filters = Filters()
    assert filters.user_type == "merchant"
    assert filters.min_month_order_count is None
    assert filters.min_positive_rate is None
    assert filters.min_month_finish_rate is None


def test_filters_round_trip_keeps_decimal_strings() -> None:
    filters = Filters(
        user_type="merchant",
        min_month_order_count=Decimal("500"),
        min_positive_rate=Decimal("0.97"),
        min_month_finish_rate=Decimal("0.94"),
    )
    payload = filters.to_dict()
    assert payload == {
        "user_type": "merchant",
        "min_month_order_count": "500",
        "min_positive_rate": "0.97",
        "min_month_finish_rate": "0.94",
    }
    assert Filters.from_dict(payload) == filters
    assert json.loads(json.dumps(payload)) == payload


def test_filters_from_dict_merges_over_a_base() -> None:
    base = Filters(user_type="merchant", min_month_order_count=Decimal("500"))
    merged = Filters.from_dict({"min_positive_rate": "0.97"}, base=base)
    assert merged.min_month_order_count == Decimal("500")
    assert merged.min_positive_rate == Decimal("0.97")
    assert merged.user_type == "merchant"


def test_filters_from_dict_rejects_unknown_keys_and_bad_shapes() -> None:
    with pytest.raises(ConfigError, match="unknown filter keys: min_orders"):
        Filters.from_dict({"min_orders": "1"})
    with pytest.raises(ConfigError, match="filters must be a JSON object"):
        Filters.from_dict("merchant")  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        Filters.from_dict({"min_positive_rate": 0.97})


def test_filters_from_dict_lowercases_user_type_and_allows_disabling() -> None:
    assert Filters.from_dict({"user_type": "MERCHANT"}).user_type == "merchant"
    assert Filters.from_dict({"user_type": None}).user_type is None


# -- CompetitorAd ----------------------------------------------------------------------
def test_competitor_ad_round_trip_through_json() -> None:
    ad = CompetitorAd(
        platform="binance",
        pair=Pair.parse("UAH/USDT"),
        price=Decimal("47.01"),
        advertiser="merchant-a",
        user_type="merchant",
        month_order_count=Decimal("501"),
        positive_rate=Decimal("0.975"),
        month_finish_rate=Decimal("0.95"),
        adv_no="204812345",
    )
    payload = json.loads(json.dumps(ad.to_dict()))
    assert CompetitorAd.from_dict(payload) == ad


def test_competitor_ad_from_dict_tolerates_missing_metrics() -> None:
    ad = CompetitorAd.from_dict({"platform": "okx", "pair": "UAH/USDT", "price": "46.99"})
    assert ad.month_order_count is None
    assert ad.positive_rate is None
    assert ad.month_finish_rate is None
    assert ad.adv_no is None
    assert ad.user_type == ""


def test_competitor_ad_rejects_float_price() -> None:
    with pytest.raises(ConfigError):
        CompetitorAd.from_dict({"platform": "okx", "pair": "UAH/USDT", "price": 46.99})


# -- AdSpec / AdActionResult -----------------------------------------------------------
def test_ad_spec_defaults_and_serialisation() -> None:
    spec = AdSpec(
        pair=Pair.parse("PLN/USDT"),
        price=Decimal("4.31"),
        min_amount=Decimal("500"),
        max_amount=Decimal("100000"),
        payment_methods=("BLIK",),
    )
    assert spec.active is True
    assert spec.side == "sell"
    assert spec.quantity is None
    assert spec.payment_ids == ()
    assert spec.to_dict() == {
        "pair": "PLN/USDT",
        "price": "4.31",
        "min_amount": "500",
        "max_amount": "100000",
        "payment_methods": ["BLIK"],
        "active": True,
        "side": "sell",
        "quantity": None,
        "payment_ids": [],
    }


def test_ad_spec_carries_quantity_and_venue_payment_ids() -> None:
    spec = AdSpec(
        pair=Pair.parse("PLN/USDT"),
        price=Decimal("4.31"),
        min_amount=Decimal("500"),
        max_amount=Decimal("100000"),
        payment_methods=("BLIK",),
        quantity=Decimal("1000"),
        payment_ids=("BLIK", "Przelewy24"),
    )
    payload = spec.to_dict()
    assert payload["quantity"] == "1000"
    assert payload["payment_ids"] == ["BLIK", "Przelewy24"]


def test_ad_action_result_keeps_venue_payload() -> None:
    result = AdActionResult(
        platform="binance",
        account_id="Binance#1",
        pair=Pair.parse("UAH/USDT"),
        adv_no="2048",
        price=Decimal("47.00"),
        created=True,
        raw={"code": "000000"},
    )
    assert result.created is True
    assert result.raw["code"] == "000000"


# -- AdRecord / PublishResult / ComputedAd ---------------------------------------------
def test_ad_record_round_trip() -> None:
    record = AdRecord(
        account_id="Binance#1",
        pair=Pair.parse("UAH/USDT"),
        adv_no="2048",
        price=Decimal("46.75"),
        active=False,
        updated_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    restored = AdRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert restored == record


def test_publish_result_ok_matrix() -> None:
    base = {"account_id": "Binance#1", "platform": "binance", "pair": Pair.parse("UAH/USDT")}
    assert PublishResult(status="created", **base).ok
    assert PublishResult(status="updated", **base).ok
    assert PublishResult(status="skipped", **base).ok
    assert PublishResult(status="dry_run", price=Decimal("47.00"), dry_run=True, **base).ok
    assert not PublishResult(status="error", error="boom", **base).ok


def test_publish_result_to_dict_uses_decimal_strings() -> None:
    result = PublishResult(
        account_id="Okx#1",
        platform="okx",
        pair=Pair.parse("UAH/USDC"),
        status="updated",
        price=Decimal("46.99"),
        adv_no="77",
    )
    payload = result.to_dict()
    assert payload["price"] == "46.99"
    assert payload["pair"] == "UAH/USDC"
    assert payload["error"] is None
    assert payload["dry_run"] is False


def test_computed_ad_to_dict_exposes_source_and_clamp_flag() -> None:
    ad = ComputedAd(
        pair=Pair.parse("UAH/USDC"),
        platform="binance",
        price=Decimal("46.50"),
        source="base_rate_minus_spread",
        cap=Decimal("46.50"),
        accounts=("Binance#1", "Binance#2"),
        base=Decimal("47.00"),
        clamped=True,
    )
    assert ad.to_dict() == {
        "pair": "UAH/USDC",
        "platform": "binance",
        "price": "46.50",
        "source": "base_rate_minus_spread",
        "cap": "46.50",
        "base": "47.00",
        "clamped": True,
        "accounts": ["Binance#1", "Binance#2"],
    }
    assert isinstance(ad.price, Decimal)


# -- MarketSnapshot --------------------------------------------------------------------
def test_market_snapshot_round_trip_preserves_filtered_and_middle() -> None:
    ad = CompetitorAd(platform="binance", pair=Pair.parse("UAH/USDT"), price=Decimal("47.10"))
    snapshot = MarketSnapshot(
        platform="binance",
        pair=Pair.parse("UAH/USDT"),
        ads=(ad,),
        filtered=(ad,),
        middle=Decimal("47.10"),
        fetched_at=datetime(2026, 2, 2, 8, 30, tzinfo=timezone.utc),
    )
    restored = MarketSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
    assert restored == snapshot


def test_market_snapshot_accepts_empty_store_payload() -> None:
    restored = MarketSnapshot.from_dict({"platform": "okx", "pair": "UAH/USDT"})
    assert restored.ads == ()
    assert restored.filtered == ()
    assert restored.middle is None
    assert restored.fetched_at is None


# -- clock -----------------------------------------------------------------------------
def test_utcnow_is_timezone_aware_utc() -> None:
    moment = utcnow()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == datetime.now(timezone.utc).utcoffset()
