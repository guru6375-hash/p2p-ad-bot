"""``p2pbot/exchanges/binance.py``: public competitor search + C2C Agent SAPI.

Fixtures are the real captured payloads from ``docs/research/binance.md`` (Wayback capture of
the exact search URL) and ``docs/research/ground-truth-probes.md`` (the architect's live POST
ladder). Every signature is recomputed here from the documented pre-hash, independently of
the adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from decimal import Decimal
from typing import Any

import pytest

from p2pbot.errors import ApiError, ConfigError, TransportError
from p2pbot.exchanges.base import HttpRequest
from p2pbot.exchanges.binance import (  # noqa: F401
    ADS_CONDITION_DEFAULTS,
    ADS_DETAIL_PATH,
    ADS_LIST_PATH,
    ADS_POST_PATH,
    ADS_UPDATE_PATH,
    ADS_UPDATE_STATUS_PATH,
    PAY_METHODS_PATH,
    RECV_WINDOW_MS,
    SEARCH_URL,
    BinanceAdapter,
)
from p2pbot.models import Pair

from _fake_transport import (
    BINANCE_CREDENTIALS,
    FIXED_NOW_MS,
    FakeTransport,
    FixedClock,
    json_response,
    make_account,
    make_spec,
    text_response,
)

BINANCE = "https://api.binance.com"
UAH_USDT = Pair.parse("UAH/USDT")
ACCOUNT = make_account("binance", 1, BINANCE_CREDENTIALS)
OTHER_ACCOUNT = make_account("binance", 2, BINANCE_CREDENTIALS)

#: Real trimmed body of ``POST .../friendly/c2c/adv/search`` (Wayback 2026-09-04T14:49:48Z).
ARCHIVED_SEARCH_PAYLOAD: dict[str, Any] = {
    "code": "000000",
    "message": None,
    "messageDetail": None,
    "data": [
        {
            "adv": {
                "advNo": "13928301035093368832",
                "classify": "profession",
                "tradeType": "SELL",
                "asset": "USDT",
                "fiatUnit": "AED",
                "price": "3.681",
                "initAmount": None,
                "surplusAmount": "41253.59",
                "tradableQuantity": "41232.97",
                "maxSingleTransAmount": "65000",
                "minSingleTransAmount": "10000",
                "payTimeLimit": 15,
                "tradeMethods": [
                    {
                        "payId": None,
                        "payMethodId": "",
                        "payType": "BANK",
                        "identifier": "BANK",
                        "tradeMethodName": "Bank Transfer",
                        "tradeMethodBgColor": "#F0B90B",
                    },
                    {
                        "payId": None,
                        "payMethodId": "",
                        "payType": "BankTransferMena",
                        "identifier": "BankTransferMena",
                        "tradeMethodName": "Bank Transfer (Middle East)",
                        "tradeMethodBgColor": "#F0B90B",
                    },
                ],
                "takerAdditionalKycRequired": 1,
                "assetScale": 2,
                "fiatScale": 3,
                "priceScale": 3,
                "isTradable": True,
            },
            "advertiser": {
                "userNo": "s2c07b60623f23eb891d12508a47cffd5",
                "nickName": "BLOCKSY",
                "orderCount": None,
                "monthOrderCount": 2166,
                "monthFinishRate": 1,
                "positiveRate": 1,
                "userType": "merchant",
                "userGrade": 3,
                "userIdentity": "BLOCK_MERCHANT",
                "badges": ["Block", "Pro"],
                "isBlocked": False,
            },
            "privilegeDesc": "Featured Ad",
            "privilegeType": 2,
        },
        {
            "adv": {
                "advNo": "12929187549246205952",
                "classify": "mass",
                "tradeType": "BUY",  # mirrored: it reports the taker side, never ours
                "asset": "USDT",
                "fiatUnit": "AED",
                "price": "3.676",
                "surplusAmount": "2375.51",
                "tradableQuantity": "2373.13",
                "maxSingleTransAmount": "10000",
                "minSingleTransAmount": "7000",
                "payTimeLimit": 15,
                "tradeMethods": [
                    {"payType": "BANK", "identifier": "BANK", "tradeMethodName": "Bank Transfer"}
                ],
            },
            "advertiser": {
                "userNo": "se4e6c3e334f237c6b7cdd124b49b377c",
                "nickName": "maher show",
                "orderCount": None,
                "monthOrderCount": 5,
                "monthFinishRate": 0.834,
                "positiveRate": 1,
                "userType": "user",
                "userGrade": 2,
                "userIdentity": "",
            },
        },
    ],
}

#: ``getPayMethodByUserId`` shape as the adapter documents/normalizes it (the venue's own
#: payload is UNCONFIRMED — see the adapter's ``UNCONFIRMED`` index).
PAY_METHODS_PAYLOAD: dict[str, Any] = {
    "code": "000000",
    "message": None,
    "data": [
        {
            "payId": 1,
            "payType": "BANK",
            "payMethodId": "BANK",
            "identifier": "BANK",
            "tradeMethodName": "Bank Transfer",
            "tradeMethodBgColor": "#F0B90B",
        },
        {
            "payId": 2,
            "payType": "BankTransferMena",
            "identifier": "BankTransferMena",
            "tradeMethodName": "Bank Transfer (Middle East)",
        },
        {"payId": "3", "payType": "Monobank", "tradeMethodName": "Monobank"},
        {
            "payId": 4,
            "payType": "PrivatBank",
            "identifier": "PrivatBank",
            "tradeMethodName": "PrivatBank (CARD)",
        },
        {"payId": "acct-9", "payType": "Cash", "tradeMethodName": "Cash Deposit"},
    ],
}

#: A getDetailByNo ``data`` object: the full ad object ``ads/update`` demands back.
AD_DETAIL_PAYLOAD: dict[str, Any] = {
    "code": "000000",
    "message": None,
    "data": {
        "advNo": "13928301035093368832",
        "tradeType": "SELL",
        "asset": "USDT",
        "fiatUnit": "UAH",
        "priceType": 1,
        "price": "47.10",
        "initAmount": "11215.53",
        "minSingleTransAmount": "950.00",
        "maxSingleTransAmount": "44000.00",
        "buyerKycLimit": 0,
        "payTimeLimit": 15,
        "advStatus": 1,
        "remarks": None,
        "autoReplyMsg": None,
        # the *read* shape: Binance omits payId here, which is why an update must
        # re-resolve the methods against the account before posting them back
        "tradeMethods": [
            {
                "identifier": "BANK",
                "tradeMethodName": "Bank Transfer",
                "iconUrlColor": "/image/admin_mgs_image_upload/bank.png",
            }
        ],
    },
}

LIST_ADS_PAYLOAD: dict[str, Any] = {
    "code": "000000",
    "message": None,
    "data": {
        "total": 2,
        "items": [
            {"advNo": "13928301035093368832", "price": "47.10", "advStatus": 1},
            {"advNo": "12929187549246205952", "price": "47.20", "advStatus": 3},
        ],
    },
}


def assert_trade_methods(request, expected, *, sell=True):
    """Pin the venue contract of ``tradeMethods``.

    Sell entries must carry Binance's full method object (``identifier``/``payId``/
    ``payType`` plus the display fields); an abbreviated body is rejected live with
    ``-1000 System abnormality``. Buy entries address methods by ``identifier`` only.
    """
    entries = request.json_body["tradeMethods"]
    assert len(entries) == len(expected)
    for entry, core in zip(entries, expected):
        assert {key: entry[key] for key in core} == core
        if sell:
            assert set(entry) == {
                "identifier", "payId", "payType", "payAccount", "payBank", "paySubBank",
                "tradeMethodName",
            }
            assert isinstance(entry["identifier"], str) and entry["identifier"]
            assert isinstance(entry["tradeMethodName"], str) and entry["tradeMethodName"]
        else:
            assert set(entry) == {"identifier"}


def _item(
    adv_no: str,
    price: str,
    user_type: str,
    month_order_count: int,
    positive_rate: float,
    month_finish_rate: float,
) -> dict[str, Any]:
    return {
        "adv": {"advNo": adv_no, "tradeType": "SELL", "asset": "USDT", "fiatUnit": "UAH", "price": price},
        "advertiser": {
            "nickName": f"advertiser-{adv_no}",
            "userNo": f"user-{adv_no}",
            "userType": user_type,
            "monthOrderCount": month_order_count,
            "positiveRate": positive_rate,
            "monthFinishRate": month_finish_rate,
        },
    }


#: The live UAH/USDT ladder from ``ground-truth-probes.md`` (price, userType,
#: monthOrderCount are the captures; the two rate fractions are typical of the same probe,
#: whose captures confirm they are fractions on the 0..1 scale).
LADDER_PAYLOAD: dict[str, Any] = {
    "code": "000000",
    "message": None,
    "messageDetail": None,
    "data": [
        _item("1", "53.93", "user", 2, 0.99, 0.95),
        _item("2", "48.20", "merchant", 1257, 0.99, 0.95),
        _item("3", "48.00", "merchant", 1143, 0.99, 0.93),
        _item("4", "47.95", "merchant", 1071, 0.99, 0.95),
    ],
}

def make_binance(*scripted: Any) -> BinanceAdapter:
    return BinanceAdapter(FakeTransport(*scripted), now=FixedClock())


def signed_params(request: HttpRequest, secret: str = "binance-secret") -> dict[str, str]:
    """Independent check of the SAPI signature; returns the non-signature query params."""
    params = dict(request.params or {})
    signature = params.pop("signature")
    assert list(request.params or {})[-1] == "signature"
    query = "&".join(
        f"{urllib.parse.quote_plus(name)}={urllib.parse.quote_plus(value)}"
        for name, value in params.items()
    )
    assert signature == hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return params


# --------------------------------------------------------------------------------------
# public search: request shape
# --------------------------------------------------------------------------------------
def test_search_request_is_an_unauthenticated_post_with_the_documented_body() -> None:
    request = make_binance().build_search_request(UAH_USDT)

    assert request.method == "POST"
    assert request.url == SEARCH_URL == "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    assert request.json_body == {
        "page": 1,
        "rows": 20,
        "payTypes": [],
        "publisherType": None,
        "asset": "USDT",
        "fiat": "UAH",
        "tradeType": "SELL",
    }
    assert request.headers == {"Content-Type": "application/json"}
    assert request.params is None


@pytest.mark.parametrize(("side", "trade_type"), [("sell", "SELL"), ("SELL", "SELL"), ("buy", "BUY")])
def test_search_request_maps_the_advertisers_side_onto_trade_type(side: str, trade_type: str) -> None:
    request = make_binance().build_search_request(UAH_USDT, side=side, page=3, rows=8)

    assert request.json_body["tradeType"] == trade_type
    assert request.json_body["page"] == 3
    assert request.json_body["rows"] == 8


def test_search_request_clamps_page_and_rows_and_rejects_an_unknown_side() -> None:
    adapter = make_binance()

    clamped = adapter.build_search_request(UAH_USDT, page=0, rows=-5)
    assert (clamped.json_body["page"], clamped.json_body["rows"]) == (1, 1)

    with pytest.raises(ConfigError) as excinfo:
        adapter.build_search_request(UAH_USDT, side="both")
    assert str(excinfo.value) == "binance: unsupported side 'both'"


# --------------------------------------------------------------------------------------
# public search: normalization
# --------------------------------------------------------------------------------------
def test_parse_search_response_normalizes_the_archived_capture() -> None:
    ads = make_binance().parse_search_response(ARCHIVED_SEARCH_PAYLOAD, UAH_USDT)

    assert len(ads) == 2
    merchant, ordinary = ads
    assert merchant.platform == "binance"
    assert merchant.pair is UAH_USDT
    assert merchant.price == Decimal("3.681")
    assert merchant.advertiser == "BLOCKSY"
    assert merchant.user_type == "merchant"
    assert merchant.month_order_count == Decimal("2166")
    assert merchant.positive_rate == Decimal("1")
    assert merchant.month_finish_rate == Decimal("1")
    assert merchant.adv_no == "13928301035093368832"
    # The item is preserved verbatim, so the fields CompetitorAd has no room for stay reachable.
    assert merchant.raw is ARCHIVED_SEARCH_PAYLOAD["data"][0]
    assert merchant.raw["adv"]["payTimeLimit"] == 15
    assert merchant.raw["adv"]["tradeMethods"][1]["tradeMethodName"] == "Bank Transfer (Middle East)"

    assert ordinary.advertiser == "maher show"
    assert ordinary.user_type == "user"
    assert ordinary.month_order_count == Decimal("5")
    assert ordinary.month_finish_rate == Decimal("0.834")
    # The mirrored adv.tradeType ("BUY" on a SELL request) never changes the parsed ad.
    assert ordinary.raw["adv"]["tradeType"] == "BUY"
    assert ordinary.price == Decimal("3.676")


@pytest.mark.parametrize(
    ("positive_rate", "month_finish_rate", "expected_positive", "expected_finish"),
    [
        (1, 1, "1", "1"),
        (0.98979591, 0.9401, "0.98979591", "0.9401"),
        (97.5, 94.5, "0.975", "0.945"),
        (99, 100, "0.99", "1"),
    ],
)
def test_rates_are_normalized_to_the_fraction_scale(
    positive_rate: Any, month_finish_rate: Any, expected_positive: str, expected_finish: str
) -> None:
    payload = {
        "code": "000000",
        "data": [
            {
                "adv": {"advNo": "1", "price": "47.10"},
                "advertiser": {
                    "nickName": "M",
                    "userType": "merchant",
                    "positiveRate": positive_rate,
                    "monthFinishRate": month_finish_rate,
                },
            }
        ],
    }

    ad = make_binance().parse_search_response(payload, UAH_USDT)[0]

    assert ad.positive_rate == Decimal(expected_positive)
    assert ad.month_finish_rate == Decimal(expected_finish)


def test_metrics_the_venue_omits_stay_none() -> None:
    payload = {
        "code": "000000",
        "data": [
            {"adv": {"advNo": "1", "price": "47.10"}, "advertiser": {"nickName": "M", "userType": "merchant"}},
            {
                "adv": {"advNo": "2", "price": "47.10"},
                "advertiser": {
                    "nickName": "N",
                    "userType": "Merchant",
                    "monthOrderCount": None,
                    "positiveRate": "",
                    "monthFinishRate": "   ",
                },
            },
        ],
    }

    first, second = make_binance().parse_search_response(payload, UAH_USDT)

    assert (first.month_order_count, first.positive_rate, first.month_finish_rate) == (None, None, None)
    assert (second.month_order_count, second.positive_rate, second.month_finish_rate) == (None, None, None)
    assert second.user_type == "merchant"  # the venue capitalizes it; normalized lowercase


def test_a_malformed_row_is_skipped_without_dropping_the_valid_ones(caplog: pytest.LogCaptureFixture) -> None:
    """A junk row (bad price OR bad metric) is dropped; the valid rows still come back."""
    payload = {
        "code": "000000",
        "data": [
            {
                "adv": {"advNo": "1", "price": "47,10"},
                "advertiser": {"nickName": "bad-price", "userType": "merchant"},
            },
            {
                "adv": {"advNo": "2", "price": "47.00"},
                "advertiser": {"nickName": "bad-metric", "userType": "merchant", "monthOrderCount": True},
            },
            {
                "adv": {"advNo": "3", "price": "47.10"},
                "advertiser": {
                    "nickName": "good",
                    "userType": "merchant",
                    "monthOrderCount": 700,
                    "positiveRate": 0.99,
                    "monthFinishRate": 0.95,
                },
            },
        ],
    }

    with caplog.at_level("WARNING"):
        ads = make_binance().parse_search_response(payload, UAH_USDT)

    assert [ad.adv_no for ad in ads] == ["3"]
    assert ads[0].month_order_count == Decimal("700")
    # Nothing is coerced or invented: the surviving ad keeps the venue's own values.
    assert ads[0].raw["advertiser"]["monthOrderCount"] == 700
    assert ads[0].raw["advertiser"]["positiveRate"] == 0.99
    assert "skipping malformed advertisement 1" in caplog.text
    assert "skipping malformed advertisement 2" in caplog.text


def test_advertiser_name_and_type_fall_back_to_the_available_fields() -> None:
    payload = {
        "code": "000000",
        "data": [
            # no nickName -> userNo; no userType -> ""
            {"adv": {"advNo": "1", "price": "47.10"}, "advertiser": {"userNo": "u-1"}},
            # no advertiser object at all
            {"adv": {"advNo": "2", "price": "47.20"}},
            # advNo missing -> None
            {"adv": {"price": "47.30"}, "advertiser": {"nickName": "X", "userType": "user"}},
        ],
    }

    ads = make_binance().parse_search_response(payload, UAH_USDT)

    assert (ads[0].advertiser, ads[0].user_type, ads[0].adv_no) == ("u-1", "", "1")
    assert (ads[1].advertiser, ads[1].user_type, ads[1].adv_no) == ("", "", "2")
    assert ads[2].adv_no is None


@pytest.mark.parametrize(
    "item",
    [
        "not-an-object",
        {"adv": {"price": "0"}, "advertiser": {"nickName": "Z"}},
        {"adv": {"price": None}, "advertiser": {"nickName": "Z"}},
        {"adv": {"surplusAmount": "1"}, "advertiser": {"nickName": "Z"}},
    ],
)
def test_rows_without_a_usable_price_are_skipped(item: Any) -> None:
    payload = {"code": "000000", "data": [item]}

    assert make_binance().parse_search_response(payload, UAH_USDT) == ()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (["not", "an", "object"], "binance search payload was not an object (list)"),
        ({"code": "000000", "data": "nope"}, "binance search payload carried a non-list data field"),
    ],
)
def test_search_items_rejects_a_malformed_envelope(payload: Any, message: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        make_binance().parse_search_response(payload, UAH_USDT)

    assert str(excinfo.value) == message
    assert excinfo.value.payload == payload


def test_search_items_returns_empty_when_the_venue_omits_data() -> None:
    assert make_binance().parse_search_response({"code": "000000"}, UAH_USDT) == ()
    assert make_binance().parse_search_response({"code": "000000", "data": None}, UAH_USDT) == ()


# --------------------------------------------------------------------------------------
# public search: envelope failures
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"code": "000000"},
        {"code": "0"},
        {"code": 0},
        {"code": " 000000 "},
        {"code": "000000", "success": True},
        {"code": 0, "message": None},
    ],
)
def test_ensure_success_accepts_both_documented_success_codes(payload: Any) -> None:
    assert make_binance().ensure_success(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "999999"},
        {"code": "000001"},
        {"code": 1},
        {"code": -1000},
        {"code": None},
        {"code": True},
        {},
        {"code": "000000", "success": False},
    ],
)
def test_ensure_success_raises_for_every_other_code(payload: Any) -> None:
    with pytest.raises(ApiError) as excinfo:
        make_binance().ensure_success(payload)

    assert "binance reported failure (code=" in str(excinfo.value)
    assert excinfo.value.payload == payload


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"code": -9000, "message": "Internal error"}, "message='Internal error'"),
        ({"code": -2015, "messageDetail": "Invalid API-key"}, "message='Invalid API-key'"),
        ({"code": 1, "msg": "boom"}, "message='boom'"),
        ({"code": "000001"}, ""),
    ],
)
def test_ensure_success_reports_the_venue_message(payload: Any, detail: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        make_binance().ensure_success(payload)

    assert detail in str(excinfo.value)


def test_ensure_success_rejects_a_non_object_payload() -> None:
    with pytest.raises(ApiError) as excinfo:
        make_binance().ensure_success("<html>Access Denied</html>")

    assert "returned a non-object payload (str)" in str(excinfo.value)
    assert excinfo.value.payload == "<html>Access Denied</html>"


def test_search_ads_end_to_end_applies_the_binance_thresholds() -> None:
    adapter = make_binance(json_response(LADDER_PAYLOAD))

    snapshot = adapter.search_ads(UAH_USDT)

    assert adapter.transport.last.json_body["tradeType"] == "SELL"
    assert [ad.price for ad in snapshot.ads] == [
        Decimal("53.93"),
        Decimal("48.20"),
        Decimal("48.00"),
        Decimal("47.95"),
    ]
    # > 500 orders, positiveRate > 0.97 and monthFinishRate > 0.94 (strict) for merchants only.
    assert [ad.price for ad in snapshot.filtered] == [Decimal("48.20"), Decimal("47.95")]
    assert snapshot.middle == Decimal("48.08")


def test_search_ads_raises_with_the_status_and_payload_on_an_http_failure() -> None:
    adapter = make_binance(json_response({"code": -1003, "msg": "too many requests"}, status=429))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert excinfo.value.status == 429
    assert excinfo.value.payload == {"code": -1003, "msg": "too many requests"}


def test_search_ads_raises_on_a_waf_html_body() -> None:
    adapter = make_binance(text_response("<html>Access Denied</html>", status=403))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert excinfo.value.status == 403
    assert excinfo.value.payload is None


def test_search_ads_propagates_a_connection_failure_as_transport_error() -> None:
    adapter = make_binance(TransportError(f"POST {SEARCH_URL} failed: timed out"))

    with pytest.raises(TransportError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert SEARCH_URL in str(excinfo.value)


# --------------------------------------------------------------------------------------
# private requests: signing
# --------------------------------------------------------------------------------------
def test_build_login_request_is_none_because_the_agent_api_is_api_key_based() -> None:
    assert make_binance().build_login_request(ACCOUNT) is None


def test_list_ads_request_is_signed_over_the_query_string_in_insertion_order() -> None:
    request = make_binance().build_list_ads_request(ACCOUNT, UAH_USDT)

    assert request.method == "POST"
    assert request.url == f"{BINANCE}{ADS_LIST_PATH}"
    assert request.json_body == {"page": 1, "rows": 100}
    assert request.headers["X-MBX-APIKEY"] == "binance-key"
    assert request.headers["Content-Type"] == "application/json"

    params = signed_params(request)
    assert list(params) == ["timestamp", "recvWindow"]  # never sorted
    assert params == {"timestamp": str(FIXED_NOW_MS), "recvWindow": str(RECV_WINDOW_MS)}


def test_signature_uses_a_naive_clock_as_utc() -> None:
    from datetime import datetime

    adapter = BinanceAdapter(FakeTransport(), now=lambda: datetime(2026, 9, 24, 12, 0, 0, 123_000))

    params = signed_params(adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert params["timestamp"] == str(FIXED_NOW_MS)


def test_a_different_secret_produces_a_different_signature() -> None:
    adapter = make_binance()
    request = adapter.build_list_ads_request(ACCOUNT, UAH_USDT)
    other = make_account("binance", 1, {"API_KEY": "binance-key", "SECRET_KEY": "other-secret"})

    assert adapter.build_list_ads_request(other, UAH_USDT).params["signature"] != request.params["signature"]


def test_detail_request_carries_advno_in_the_signed_query_string() -> None:
    transport = FakeTransport(json_response(AD_DETAIL_PAYLOAD), json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())

    adapter.build_update_ad_request(ACCOUNT, make_spec(quantity="10"), "13928301035093368832")

    detail = transport.requests[0]
    assert detail.method == "POST"
    assert detail.url == f"{BINANCE}{ADS_DETAIL_PATH}"
    assert detail.json_body is None
    params = signed_params(detail)
    assert list(params) == ["advNo", "timestamp", "recvWindow"]
    assert params["advNo"] == "13928301035093368832"


@pytest.mark.parametrize(("active", "adv_status"), [(True, 1), (False, 3)])
def test_status_request_maps_active_onto_the_adv_status_enum(active: bool, adv_status: int) -> None:
    request = make_binance().build_status_request(ACCOUNT, "13928301035093368832", active=active)

    assert request.url == f"{BINANCE}{ADS_UPDATE_STATUS_PATH}"
    assert request.json_body == {"advNos": ["13928301035093368832"], "advStatus": adv_status}
    signed_params(request)


def test_status_request_stringifies_a_numeric_adv_no() -> None:
    request = make_binance().build_status_request(ACCOUNT, 12345, active=False)  # type: ignore[arg-type]

    assert request.json_body == {"advNos": ["12345"], "advStatus": 3}


# --------------------------------------------------------------------------------------
# private requests: create
# --------------------------------------------------------------------------------------
def test_create_request_resolves_the_pay_id_from_the_accounts_own_methods() -> None:
    transport = FakeTransport(json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())
    spec = make_spec(
        price="47.00", min_amount="1000.00", max_amount="47000.00", payment_methods=("Bank Transfer",)
    )

    request = adapter.build_create_ad_request(ACCOUNT, spec)

    # The pay-method lookup is the only request the *builder* sends.
    assert len(transport) == 1
    pay_request = transport.last
    assert pay_request.method == "GET"
    assert pay_request.url == f"{BINANCE}{PAY_METHODS_PATH}"
    assert pay_request.json_body is None
    assert pay_request.headers == {"X-MBX-APIKEY": "binance-key"}
    signed_params(pay_request)

    assert request.method == "POST"
    assert request.url == f"{BINANCE}{ADS_POST_PATH}"
    assert request.headers["Content-Type"] == "application/json"
    body = request.json_body
    # Binance rejects an abbreviated create body live (-1000 System abnormality), so the
    # full field set of its own web client is required; the adapter must send all of it.
    assert set(body) == set(ADS_CONDITION_DEFAULTS) | {
        "classify", "tradeType", "asset", "fiatUnit", "priceType", "price", "initAmount",
        "maxSingleTransAmount", "minSingleTransAmount", "buyerKycLimit", "tradeMethods",
        "onlineNow", "payTimeLimit",
    }
    assert body["classify"] == "profession"  # merchant accounts: "mass" is refused
    assert body["tradeType"] == "SELL"           # the ad endpoints take words, not 1/0
    assert body["asset"] == "USDT" and body["fiatUnit"] == "UAH"
    assert body["priceType"] == 1
    assert body["price"] == "47.00"
    assert body["initAmount"] == "1000.00"
    assert body["maxSingleTransAmount"] == "47000.00"
    assert body["minSingleTransAmount"] == "1000.00"
    assert body["buyerKycLimit"] == 1
    assert body["onlineNow"] is True
    assert body["payTimeLimit"] == 15
    # condition fields travel as the venue's own "no restriction" values
    assert body["visible"] == 1 and body["allowTradeMerchant"] == 1
    assert body["nonTradableRegions"] == [] and body["remarks"] == ""
    assert body["buyerRegDaysLimit"] == -1 and body["userTradeVolumeMax"] == 1000000
    assert_trade_methods(request, [{"payId": 1, "payType": "BANK"}])
    signed_params(request)


def test_create_request_reuses_the_cached_payment_methods_and_dedupes_entries() -> None:
    transport = FakeTransport(json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())
    # Both spellings resolve to the very same account payment method.
    spec = make_spec(payment_methods=("Bank Transfer", "bank transfer"), payment_ids=("1",))

    first = adapter.build_create_ad_request(ACCOUNT, spec)
    second = adapter.build_create_ad_request(ACCOUNT, spec)

    assert len(transport) == 1  # one lookup for (account, fiat), reused afterwards
    assert_trade_methods(first, [{"payId": 1, "payType": "BANK"}])
    assert_trade_methods(second, [{"payId": 1, "payType": "BANK"}])


def test_create_request_matches_methods_by_substring_and_by_pay_id() -> None:
    transport = FakeTransport(json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())

    by_name = adapter.build_create_ad_request(ACCOUNT, make_spec(payment_methods=("PrivatBank",)))
    assert_trade_methods(by_name, [{"payId": 4, "payType": "PrivatBank"}])

    # No display name equals "Transfer", so this is the documented loose (substring) fallback.
    loose = adapter.build_create_ad_request(ACCOUNT, make_spec(payment_methods=("Transfer",)))
    assert_trade_methods(loose, [{"payId": 1, "payType": "BANK"}])

    by_id = adapter.build_create_ad_request(ACCOUNT, make_spec(payment_ids=("2", "3", "acct-9")))
    assert_trade_methods(
        by_id,
        [
            {"payId": 2, "payType": "BankTransferMena"},
            {"payId": 3, "payType": "Monobank"},
            {"payId": "acct-9", "payType": "Cash"},  # a non-numeric payId stays a string
        ],
    )


@pytest.mark.parametrize(
    ("spec_kwargs", "message"),
    [
        (
            {"payment_methods": ("Wise",)},
            "binance: account Binance#1 has no payment method matching 'Wise' for UAH/USDT",
        ),
        (
            {"payment_ids": ("99",)},
            "binance: account Binance#1 has no payment method with payId '99' for UAH/USDT",
        ),
    ],
)
def test_create_request_rejects_an_unresolvable_payment_method(
    spec_kwargs: dict[str, Any], message: str
) -> None:
    transport = FakeTransport(json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())

    with pytest.raises(ApiError) as excinfo:
        adapter.build_create_ad_request(ACCOUNT, make_spec(**spec_kwargs))

    assert str(excinfo.value) == message


def test_create_request_refuses_an_ad_without_any_payment_method() -> None:
    transport = FakeTransport()
    adapter = BinanceAdapter(transport, now=FixedClock())

    with pytest.raises(ApiError) as excinfo:
        adapter.build_create_ad_request(ACCOUNT, make_spec())

    assert str(excinfo.value) == (
        "binance: account Binance#1 has no payment method configured for UAH/USDT; "
        "a sell advertisement cannot be published without one"
    )
    assert len(transport) == 0  # nothing is sent when the ad cannot be published


def test_a_sell_method_without_a_pay_id_is_refused() -> None:
    payload = {"code": "000000", "data": [{"payType": "BANK", "tradeMethodName": "Bank Transfer"}]}
    adapter = BinanceAdapter(FakeTransport(json_response(payload)), now=FixedClock())

    with pytest.raises(ApiError) as excinfo:
        adapter.build_create_ad_request(ACCOUNT, make_spec(payment_methods=("Bank Transfer",)))

    assert "carries no payId; it cannot be used on a sell advertisement" in str(excinfo.value)


def test_buy_ads_address_payment_methods_by_identifier_without_a_lookup() -> None:
    transport = FakeTransport()
    adapter = BinanceAdapter(transport, now=FixedClock())

    request = adapter.build_create_ad_request(
        ACCOUNT, make_spec(side="buy", payment_ids=("BANK", "BankTransferMena"))
    )

    assert len(transport) == 0  # supplied identifiers are used verbatim
    assert request.json_body["tradeType"] == "BUY"
    assert_trade_methods(
        request, [{"identifier": "BANK"}, {"identifier": "BankTransferMena"}], sell=False
    )


def test_buy_ads_resolve_display_names_against_the_own_methods() -> None:
    transport = FakeTransport(json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())

    request = adapter.build_create_ad_request(
        ACCOUNT, make_spec(side="buy", payment_methods=("Bank Transfer", "Monobank"))
    )

    assert len(transport) == 1
    assert_trade_methods(
        request, [{"identifier": "BANK"}, {"identifier": "Monobank"}], sell=False
    )


def test_a_buy_method_without_identifier_pay_type_or_pay_id_is_refused() -> None:
    payload = {"code": "000000", "data": [{"tradeMethodName": "Cash only"}]}
    adapter = BinanceAdapter(FakeTransport(json_response(payload)), now=FixedClock())

    with pytest.raises(ApiError) as excinfo:
        adapter.build_create_ad_request(
            ACCOUNT, make_spec(side="buy", payment_methods=("Cash only",))
        )

    assert "carries no identifier; it cannot be used on a buy advertisement" in str(excinfo.value)


def test_create_request_derives_the_quantity_from_max_amount_over_price() -> None:
    transport = FakeTransport(json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())

    derived = adapter.build_create_ad_request(
        ACCOUNT,
        make_spec(price="47.00", max_amount="1000.00", payment_methods=("Bank Transfer",)),
    )
    explicit = adapter.build_create_ad_request(
        ACCOUNT,
        make_spec(quantity="555.5", payment_methods=("Bank Transfer",)),
    )

    assert derived.json_body["initAmount"] == "21.27"  # floor(1000.00 / 47.00) to 0.01
    assert explicit.json_body["initAmount"] == "555.5"


@pytest.mark.parametrize(
    ("spec_kwargs", "message_part"),
    [
        ({"quantity": "0"}, "must be positive, got 0"),
        ({"price": "0", "quantity": None}, "cannot derive an advertisement quantity"),
        (
            {"price": "1000.00", "max_amount": "1.00", "quantity": None},
            "is 0.00; raise max_amount or lower the price",
        ),
    ],
)
def test_create_request_refuses_an_unusable_quantity(spec_kwargs: dict[str, Any], message_part: str) -> None:
    transport = FakeTransport()
    adapter = BinanceAdapter(transport, now=FixedClock())
    spec = make_spec(payment_methods=("Bank Transfer",), **spec_kwargs)

    with pytest.raises(ConfigError) as excinfo:
        adapter.build_create_ad_request(ACCOUNT, spec)

    assert message_part in str(excinfo.value)
    assert len(transport) == 0


def test_create_request_rejects_an_unknown_side() -> None:
    adapter = BinanceAdapter(FakeTransport(), now=FixedClock())

    with pytest.raises(ConfigError) as excinfo:
        adapter.build_create_ad_request(ACCOUNT, make_spec(side="both"))

    assert str(excinfo.value) == "binance: unsupported side 'both'"


# --------------------------------------------------------------------------------------
# private requests: update (full-object workflow)
# --------------------------------------------------------------------------------------
def test_update_of_an_inactive_spec_only_posts_update_status() -> None:
    transport = FakeTransport()
    adapter = BinanceAdapter(transport, now=FixedClock())

    request = adapter.build_update_ad_request(
        ACCOUNT, make_spec(active=False), "13928301035093368832"
    )

    assert len(transport) == 0  # no getDetailByNo for a take-down
    assert request.url == f"{BINANCE}{ADS_UPDATE_STATUS_PATH}"
    assert request.json_body == {"advNos": ["13928301035093368832"], "advStatus": 3}


def test_update_of_an_active_spec_fetches_the_detail_and_posts_the_full_object() -> None:
    transport = FakeTransport(json_response(AD_DETAIL_PAYLOAD), json_response(PAY_METHODS_PAYLOAD))
    adapter = BinanceAdapter(transport, now=FixedClock())
    spec = make_spec(
        price="47.00",
        min_amount="1000.00",
        max_amount="47000.00",
        payment_methods=("Bank Transfer",),
        quantity="1234.5",
    )

    request = adapter.build_update_ad_request(ACCOUNT, spec, "13928301035093368832")

    assert len(transport) == 2  # detail + the payment-method lookup the write shape needs
    assert request.method == "POST"
    assert request.url == f"{BINANCE}{ADS_UPDATE_PATH}"

    body = request.json_body
    detail = AD_DETAIL_PAYLOAD["data"]
    # Every field of the detail object is echoed, so Binance sees a complete ad object.
    assert set(body) == set(detail)
    assert body["remarks"] is None
    assert body["payTimeLimit"] == 15
    # the read shape cannot be echoed back (live: 83664 "no payment method yet"),
    # so the blueprint's methods are resolved into the write shape
    assert_trade_methods(request, [{"payId": 1, "payType": "BANK"}])
    # The fields this adapter changes.
    assert body["advNo"] == "13928301035093368832"
    assert body["tradeType"] == "SELL"  # the venue's own spelling, not 1/0
    assert body["priceType"] == 1
    assert body["price"] == "47.00"
    assert body["initAmount"] == "1234.5"
    assert body["minSingleTransAmount"] == "1000.00"
    assert body["maxSingleTransAmount"] == "47000.00"
    assert body["advStatus"] == 1
    signed_params(request)


def test_update_normalizes_the_buy_read_shape_to_the_write_enum() -> None:
    detail = dict(AD_DETAIL_PAYLOAD["data"], tradeType="BUY")
    transport = FakeTransport(
        json_response({"code": "000000", "data": detail}), json_response(PAY_METHODS_PAYLOAD)
    )
    adapter = BinanceAdapter(transport, now=FixedClock())

    request = adapter.build_update_ad_request(
        ACCOUNT, make_spec(side="buy", quantity="10"), "13928301035093368832"
    )

    assert request.json_body["tradeType"] == "BUY"


def test_update_pins_the_addressed_adv_no_even_when_the_detail_disagrees() -> None:
    detail = dict(AD_DETAIL_PAYLOAD["data"], advNo="999")
    transport = FakeTransport(
        json_response({"code": "000000", "data": detail}), json_response(PAY_METHODS_PAYLOAD)
    )
    adapter = BinanceAdapter(transport, now=FixedClock())

    request = adapter.build_update_ad_request(
        ACCOUNT, make_spec(quantity="10"), "13928301035093368832"
    )

    assert request.json_body["advNo"] == "13928301035093368832"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"code": "000000", "data": True},
            "binance advertisement 13928301035093368832 of account Binance#1 has no detail object",
        ),
        (
            {"code": "000000"},
            "binance advertisement 13928301035093368832 of account Binance#1 has no detail object",
        ),
        ("nope", "binance returned a non-object payload (str)"),
    ],
)
def test_update_raises_when_the_detail_payload_has_no_object(payload: Any, message: str) -> None:
    transport = FakeTransport(json_response(payload))
    adapter = BinanceAdapter(transport, now=FixedClock())

    with pytest.raises(ApiError) as excinfo:
        adapter.build_update_ad_request(ACCOUNT, make_spec(quantity="10"), "13928301035093368832")

    assert str(excinfo.value) == message
    assert excinfo.value.payload == payload


def test_update_propagates_a_venue_failure_of_the_detail_call() -> None:
    transport = FakeTransport(json_response({"code": -2015, "msg": "Invalid API-key"}), json_response({"code": "000000"}))
    adapter = BinanceAdapter(transport, now=FixedClock())

    with pytest.raises(ApiError) as excinfo:
        adapter.build_update_ad_request(ACCOUNT, make_spec(quantity="10"), "13928301035093368832")

    assert "binance reported failure (code=-2015, message='Invalid API-key')" in str(excinfo.value)
    assert len(transport) == 1  # the update itself is never sent


# --------------------------------------------------------------------------------------
# payment-method payload normalization
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "expected_entry"),
    [
        ([{"payId": 1, "tradeMethodName": "Bank Transfer"}], {"payId": 1}),
        ({"items": [{"payId": 1, "tradeMethodName": "Bank Transfer"}]}, {"payId": 1}),
        ({"list": [{"payId": 1, "tradeMethodName": "Bank Transfer"}]}, {"payId": 1}),
        ({"rows": [{"payId": 1, "tradeMethodName": "Bank Transfer"}]}, {"payId": 1}),
        ({"payMethods": [{"payId": 1, "tradeMethodName": "Bank Transfer"}]}, {"payId": 1}),
        # A single record (not wrapped in a list) is accepted too.
        ({"payId": 1, "tradeMethodName": "Bank Transfer"}, {"payId": 1}),
        ({"payId": 1, "payMethodName": "Bank Transfer"}, {"payId": 1}),
        ({"payId": 1, "name": "Bank Transfer"}, {"payId": 1}),
        # No display name anywhere: the payType (then the payId) becomes the resolvable name.
        ({"payId": 1, "payType": "BANK"}, {"payId": 1, "payType": "BANK"}),
    ],
)
def test_payment_method_payload_shapes_all_resolve_to_one_entry(
    data: Any, expected_entry: dict[str, Any]
) -> None:
    transport = FakeTransport(json_response({"code": "000000", "data": data}))
    adapter = BinanceAdapter(transport, now=FixedClock())

    request = adapter.build_create_ad_request(ACCOUNT, make_spec(payment_methods=("Bank Transfer", "BANK")))

    assert_trade_methods(request, [expected_entry])


@pytest.mark.parametrize(
    ("payload", "message_part"),
    [
        ({"code": "000000", "data": "nope"}, "binance payment-method payload was not a list of records"),
        ({"code": "000000"}, "binance payment-method payload was not a list of records"),
        ({"code": "000000", "data": True}, "binance payment-method payload was not a list of records"),
        ({"code": "000000", "data": []}, "binance reported no payment methods for the account"),
        ({"code": "000000", "data": [1, "junk"]}, "binance reported no payment methods for the account"),
    ],
)
def test_payment_method_payload_without_usable_records_is_an_error(
    payload: Any, message_part: str
) -> None:
    transport = FakeTransport(json_response(payload))
    adapter = BinanceAdapter(transport, now=FixedClock())

    with pytest.raises(ApiError) as excinfo:
        adapter.build_create_ad_request(ACCOUNT, make_spec(payment_methods=("Bank Transfer",)))

    assert str(excinfo.value) == message_part
    assert excinfo.value.payload == payload


def test_payment_method_lookup_is_cached_per_account_and_fiat_only() -> None:
    transport = FakeTransport(*[json_response(PAY_METHODS_PAYLOAD)] * 3)
    adapter = BinanceAdapter(transport, now=FixedClock())
    spec = make_spec(payment_methods=("Bank Transfer",))

    adapter.build_create_ad_request(ACCOUNT, spec)
    adapter.build_create_ad_request(ACCOUNT, spec)
    adapter.build_create_ad_request(OTHER_ACCOUNT, spec)
    adapter.build_create_ad_request(ACCOUNT, make_spec(pair="PLN/USDT", payment_methods=("Bank Transfer",)))

    assert len(transport) == 3
    assert transport.urls() == [f"{BINANCE}{PAY_METHODS_PATH}"] * 3


def test_payment_method_cache_lives_on_the_adapter_instance() -> None:
    transport = FakeTransport(json_response(PAY_METHODS_PAYLOAD), json_response(PAY_METHODS_PAYLOAD))
    spec = make_spec(payment_methods=("Bank Transfer",))

    BinanceAdapter(transport, now=FixedClock()).build_create_ad_request(ACCOUNT, spec)
    BinanceAdapter(transport, now=FixedClock()).build_create_ad_request(ACCOUNT, spec)

    assert len(transport) == 2  # no shared/global state and no AdStore involved


# --------------------------------------------------------------------------------------
# private responses
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("13928301035093368832", "13928301035093368832"),
        ("  42 ", "42"),
        ("", None),
        ({"advNo": "77"}, "77"),
        ({"adNo": "88"}, "88"),
        ({"advNo": "", "adNo": ""}, None),
        (True, None),
        (None, None),
        ([], None),
    ],
)
def test_parse_ad_response_reads_the_shapes_the_venue_documents(data: Any, expected: str | None) -> None:
    result = make_binance().parse_ad_response({"code": "000000", "data": data})

    assert result.adv_no == expected
    assert result.platform == "binance"
    assert result.price is None
    assert result.created is False
    assert result.pair == Pair.parse("XXX/XXX")
    assert result.raw == {"code": "000000", "data": data}


def test_parse_ad_response_keeps_a_non_object_payload_as_raw() -> None:
    result = make_binance().parse_ad_response("boom")

    assert result.adv_no is None
    assert result.raw == {"payload": "boom"}


def test_parse_ad_result_bridges_the_create_response_into_the_identity_fields() -> None:
    adapter = make_binance()
    spec = make_spec(price="47.00")

    result = adapter.parse_ad_result(
        {"code": "000000", "data": "13928301035093368832"},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=spec,
        created=True,
    )

    assert result.adv_no == "13928301035093368832"
    assert result.platform == "binance"
    assert result.account_id == "Binance#1"
    assert result.pair == UAH_USDT
    assert result.price == Decimal("47.00")  # the create response echoes no price
    assert result.created is True


def test_parse_ad_result_uses_the_addressed_adv_no_for_an_update_response() -> None:
    adapter = make_binance()

    result = adapter.parse_ad_result(
        {"code": "000000", "data": True},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=make_spec(price="47.10"),
        created=False,
        adv_no="13928301035093368832",
    )

    assert result.adv_no == "13928301035093368832"
    assert result.price == Decimal("47.10")


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "000000", "data": {"items": [{"advNo": "1"}, {"advNo": "2"}]}},
        {"code": "000000", "data": {"list": [{"advNo": "1"}, {"advNo": "2"}]}},
        {"code": "000000", "data": {"rows": [{"advNo": "1"}, {"advNo": "2"}]}},
        {"code": "000000", "data": [{"advNo": "1"}, {"advNo": "2"}]},
        [{"advNo": "1"}, {"advNo": "2"}],
    ],
)
def test_parse_ad_list_reads_the_documented_envelopes(payload: Any) -> None:
    records = make_binance().parse_ad_list(payload)

    assert records == ({"advNo": "1"}, {"advNo": "2"})


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": "000000"}, ()),
        ({"code": "000000", "data": {"items": "nope"}}, ()),
        ({"code": "000000", "data": {"items": [{"advNo": "1"}, "junk", 7]}}, ({"advNo": "1"},)),
        (None, ()),
        ([], ()),
    ],
)
def test_parse_ad_list_returns_only_object_records(payload: Any, expected: tuple[Any, ...]) -> None:
    assert make_binance().parse_ad_list(payload) == expected


def test_list_my_ads_returns_the_venue_records() -> None:
    adapter = make_binance(json_response(LIST_ADS_PAYLOAD))

    records = adapter.list_my_ads(ACCOUNT, UAH_USDT)

    assert records == (
        {"advNo": "13928301035093368832", "price": "47.10", "advStatus": 1},
        {"advNo": "12929187549246205952", "price": "47.20", "advStatus": 3},
    )
    assert adapter.transport.last.url == f"{BINANCE}{ADS_LIST_PATH}"
    assert adapter.transport.last.json_body == {"page": 1, "rows": 100}


def test_send_private_surfaces_a_venue_error_payload() -> None:
    adapter = make_binance(json_response({"code": -9000, "msg": "Full ad object required"}))

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert str(excinfo.value) == "binance reported failure (code=-9000, message='Full ad object required')"
    assert excinfo.value.payload == {"code": -9000, "msg": "Full ad object required"}
