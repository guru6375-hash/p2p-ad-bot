"""``p2pbot/exchanges/bybit.py``: public competitor search + the signed V5 P2P API.

The public fixture is the real sample from Bybit's own documentation
(``docs/p2p/ad/online-ad-list``, quoted in ``docs/research/bybit.md``); the private request
bodies follow the documented ``/v5/p2p/item/*`` pages. Every signature is recomputed here
from the documented pre-hash (``timestamp + api_key + recv_window + literal body``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from p2pbot.constants import SIDE_BUY, SIDE_SELL
from p2pbot.errors import ApiError, ConfigError, TransportError
from p2pbot.exchanges.base import HttpRequest
from p2pbot.exchanges.bybit import (
    ACTION_ACTIVE,
    ACTION_MODIFY,
    CANCEL_PATH,
    DOCUMENTED_SEARCH_URL,
    ITEM_INFO_PATH,
    LIST_PATH,
    RECV_WINDOW,
    UPDATE_PATH,
    WEB_SEARCH_URL,
    BybitAdapter,
)
from p2pbot.models import Filters, Pair

from _fake_transport import (
    FIXED_NOW_MS,
    FakeTransport,
    FixedClock,
    json_response,
    make_account,
    make_spec,
    text_response,
)

UAH_USDT = Pair.parse("UAH/USDT")
ACCOUNT = make_account("bybit", 1, {"API_KEY": "bybit-key", "SECRET_KEY": "bybit-secret"})

#: Real trimmed sample of ``POST /v5/p2p/item/online`` from Bybit's documentation.
ONLINE_AD_LIST_PAYLOAD: dict[str, Any] = {
    "ret_code": 0,
    "ret_msg": "SUCCESS",
    "result": {
        "count": 3,
        "items": [
            {
                "id": "1899658238346616832",
                "accountId": "290120",
                "userId": "290118",
                "nickName": "cjmtest",
                "tokenId": "USDT",
                "currencyId": "EUR",
                "side": 0,
                "priceType": 0,
                "price": "0.93",
                "premium": "0",
                "lastQuantity": "10000",
                "quantity": "10000",
                "frozenQuantity": "0",
                "executedQuantity": "0",
                "minAmount": "200",
                "maxAmount": "9300",
                "remark": "1111121212",
                "status": 10,
                "createDate": "1741748793000",
                "payments": ["14"],
                "recentOrderNum": 0,
                "recentExecuteRate": 0,
                "isOnline": True,
                "authTag": ["BA"],
                "userType": "ORG",
                "itemType": "ORIGIN",
                "paymentPeriod": 15,
                "tradingPreferenceSet": {
                    "hasUnPostAd": 0,
                    "isKyc": 1,
                    "orderFinishNumberDay30": 0,
                    "completeRateDay30": "0",
                    "hasOrderFinishNumberDay30": 0,
                    "hasCompleteRateDay30": 0,
                    "hasNationalLimit": 0,
                },
                "symbolInfo": {
                    "token": {"tokenId": "USDT", "scale": 4},
                    "currency": {"currencyId": "EUR", "scale": 3},
                },
            },
            {
                "id": "1899659847717838848",
                "userId": "290118",
                "nickName": "cjmtest",
                "tokenId": "USDT",
                "currencyId": "EUR",
                "side": 0,
                "price": "0.92",
                "lastQuantity": "20000",
                "minAmount": "20",
                "maxAmount": "18400",
                "payments": ["377"],
                "recentOrderNum": 0,
                "recentExecuteRate": 0,
                "authTag": ["BA"],
                "tradingPreferenceSet": {
                    "orderFinishNumberDay30": 60,
                    "completeRateDay30": "95",
                    "hasOrderFinishNumberDay30": 1,
                    "hasCompleteRateDay30": 1,
                },
            },
        ],
    },
}


def make_bybit(*scripted: Any, **attributes: Any) -> BybitAdapter:
    adapter = BybitAdapter(FakeTransport(*scripted), now=FixedClock())
    for name, value in attributes.items():
        setattr(adapter, name, value)
    return adapter


def assert_bybit_signature(request: HttpRequest, secret: str = "bybit-secret") -> None:
    """Independent recomputation of the lowercase-hex V5 signature over the literal body."""
    body_string = json.dumps(request.json_body)
    plain = (
        f"{request.headers['X-BAPI-TIMESTAMP']}"
        f"{request.headers['X-BAPI-API-KEY']}"
        f"{request.headers['X-BAPI-RECV-WINDOW']}"
        f"{body_string}"
    )
    expected = hmac.new(secret.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected
    assert request.headers["X-BAPI-API-KEY"] == "bybit-key"
    assert request.headers["X-BAPI-RECV-WINDOW"] == RECV_WINDOW == "5000"
    assert request.headers["X-BAPI-TIMESTAMP"] == str(FIXED_NOW_MS)
    assert request.headers["Content-Type"] == "application/json"


# --------------------------------------------------------------------------------------
# public search
# --------------------------------------------------------------------------------------
def test_search_request_posts_the_documented_website_body() -> None:
    request = make_bybit().build_search_request(UAH_USDT)

    assert request.method == "POST"
    assert request.url == WEB_SEARCH_URL == "https://www.bybit.com/x-api/fiat/otc/item/recommend/online"
    assert request.headers == {"Content-Type": "application/json", "Accept": "application/json"}
    assert request.json_body == {
        "userId": "",
        "tokenId": "USDT",
        "currencyId": "UAH",
        "payment": [],
        "side": "1",
        "size": 20,
        "page": 1,
        "action": "recommend",
    }


@pytest.mark.parametrize(("side", "code"), [(SIDE_SELL, "1"), ("SELL", "1"), (SIDE_BUY, "0")])
def test_search_request_maps_the_advertisers_side_onto_the_side_code(side: str, code: str) -> None:
    request = make_bybit().build_search_request(UAH_USDT, side=side, page=3, rows=8)

    assert request.json_body["side"] == code
    assert request.json_body["page"] == 3
    assert request.json_body["size"] == 8


def test_search_request_rejects_an_unknown_side() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_bybit().build_search_request(UAH_USDT, side="both")

    assert str(excinfo.value) == "unknown side 'both'; expected 'sell' or 'buy'"


def test_the_search_route_is_a_single_class_attribute_switch() -> None:
    """The operator repoints the bot-protected website route without touching the builder."""
    default = make_bybit().build_search_request(UAH_USDT)
    repointed = make_bybit(search_url=DOCUMENTED_SEARCH_URL).build_search_request(UAH_USDT)

    assert default.url == WEB_SEARCH_URL
    assert repointed.url == DOCUMENTED_SEARCH_URL == "https://api.bybit.com/v5/p2p/item/online"
    assert repointed.json_body == default.json_body


def test_parse_search_response_normalizes_the_documented_sample() -> None:
    ads = make_bybit().parse_search_response(ONLINE_AD_LIST_PAYLOAD, UAH_USDT)

    assert len(ads) == 2
    first, second = ads
    assert first.platform == "bybit"
    assert first.pair is UAH_USDT
    assert first.price == Decimal("0.93")
    assert first.advertiser == "cjmtest"
    assert first.user_type == "merchant"  # authTag ["BA"] = Block Advertiser
    assert first.month_order_count == Decimal("0")
    # Bybit publishes no "positive rate": the metric stays None.
    assert first.positive_rate is None
    assert first.month_finish_rate == Decimal("0")
    assert first.adv_no == "1899658238346616832"
    assert first.raw["minAmount"] == "200"
    assert first.raw["maxAmount"] == "9300"
    assert first.raw["payments"] == ["14"]

    # The counterparty requirements are NOT the advertiser's own metrics.
    assert second.raw["tradingPreferenceSet"]["orderFinishNumberDay30"] == 60
    assert second.raw["tradingPreferenceSet"]["completeRateDay30"] == "95"
    assert second.month_order_count == Decimal("0")
    assert second.month_finish_rate == Decimal("0")


@pytest.mark.parametrize(
    ("auth_tag", "user_type", "expected"),
    [
        (["VA"], "", "merchant"),
        (["GA"], "", "merchant"),
        (["ba"], "PERSONAL", "merchant"),  # tags are upper-cased before matching
        ([], "ORG", "merchant"),
        ([], "PERSONAL", "user"),
        (["XX"], "PERSONAL", "user"),
        (None, None, "user"),
        (5, "PERSONAL", "user"),  # a scalar where a list is documented must not raise
    ],
)
def test_advertiser_class_comes_from_the_auth_tag_or_the_org_user_type(
    auth_tag: Any, user_type: Any, expected: str
) -> None:
    payload = {
        "ret_code": 0,
        "ret_msg": "SUCCESS",
        "result": {"items": [{"price": "0.93", "authTag": auth_tag, "userType": user_type}]},
    }

    ad = make_bybit().parse_search_response(payload, UAH_USDT)[0]

    assert ad.user_type == expected


@pytest.mark.parametrize(
    ("recent_execute_rate", "expected"),
    [
        (0, "0"),
        (0.95, "0.95"),
        (95, "0.95"),
        ("97.5", "0.975"),
        (None, None),
        ("n/a", None),
    ],
)
def test_recent_execute_rate_is_normalized_to_the_fraction_scale(
    recent_execute_rate: Any, expected: str | None
) -> None:
    payload = {
        "ret_code": 0,
        "ret_msg": "SUCCESS",
        "result": {"items": [{"price": "0.93", "recentExecuteRate": recent_execute_rate}]},
    }

    ad = make_bybit().parse_search_response(payload, UAH_USDT)[0]

    assert ad.month_finish_rate == (None if expected is None else Decimal(expected))


def test_recent_order_count_and_advertiser_name_fall_back_to_available_fields() -> None:
    payload = {
        "ret_code": 0,
        "ret_msg": "SUCCESS",
        "result": {
            "items": [
                {"price": "0.93", "recentOrderNum": 37, "userId": "290118"},
                {"price": "0.94"},
            ]
        },
    }

    with_user_id, without_anything = make_bybit().parse_search_response(payload, UAH_USDT)

    assert (with_user_id.month_order_count, with_user_id.advertiser) == (Decimal("37"), "290118")
    assert (without_anything.month_order_count, without_anything.advertiser) == (None, "")


@pytest.mark.parametrize(
    "item",
    [
        "not-an-object",
        {"minAmount": "20"},
        {"price": ""},
        {"price": None},
        {"price": "not-a-number"},  # garbage is skipped, never rounded
    ],
)
def test_rows_without_a_usable_price_are_skipped(item: Any) -> None:
    payload = {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"items": [item]}}

    assert make_bybit().parse_search_response(payload, UAH_USDT) == ()


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"ret_code": 0, "ret_msg": "SUCCESS"},
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": []},
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"items": "nope"}},
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"items": []}},
    ],
)
def test_parse_search_response_returns_nothing_for_an_unusable_envelope(payload: Any) -> None:
    assert make_bybit().parse_search_response(payload, UAH_USDT) == ()


def test_search_ads_end_to_end_skips_rows_without_a_price() -> None:
    payload = {
        "ret_code": 0,
        "ret_msg": "SUCCESS",
        "result": {
            "items": [
                {"price": "0.93", "authTag": ["BA"], "nickName": "merchant"},
                {"price": "", "authTag": ["BA"], "nickName": "broken"},
            ]
        },
    }
    adapter = make_bybit(json_response(payload))

    snapshot = adapter.search_ads(UAH_USDT)

    assert adapter.transport.last.json_body["side"] == "1"
    assert snapshot.platform == "bybit"
    assert [ad.advertiser for ad in snapshot.ads] == ["merchant"]
    # No hardcoded Bybit filter exists, so the default Filters() keeps merchants only.
    assert [ad.advertiser for ad in snapshot.filtered] == ["merchant"]
    assert snapshot.middle == Decimal("0.93")


def test_search_ads_applies_the_merchant_filter_to_the_documented_sample() -> None:
    payload = {
        "ret_code": 0,
        "ret_msg": "SUCCESS",
        "result": {
            "items": [
                {"price": "0.93", "authTag": ["VA"], "nickName": "verified"},
                {"price": "0.94", "authTag": [], "userType": "PERSONAL", "nickName": "ordinary"},
            ]
        },
    }
    adapter = make_bybit(json_response(payload))

    filtered = adapter.search_ads(UAH_USDT)
    unfiltered = make_bybit(json_response(payload)).search_ads(UAH_USDT, filters=Filters(user_type=None))

    assert [ad.advertiser for ad in filtered.filtered] == ["verified"]
    assert [ad.advertiser for ad in unfiltered.filtered] == ["verified", "ordinary"]


def test_search_ads_raises_on_the_bot_protected_403_page() -> None:
    adapter = make_bybit(text_response("<html><title>Access Denied</title></html>", status=403))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert excinfo.value.status == 403
    assert "bybit search for UAH/USDT failed with HTTP 403" in str(excinfo.value)
    assert excinfo.value.payload is None


def test_search_ads_never_reports_zero_ads_for_a_non_json_body() -> None:
    adapter = make_bybit(text_response("<html>Access Denied</html>", status=200))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert "not valid JSON" in str(excinfo.value)


def test_search_ads_propagates_a_connection_failure_as_transport_error() -> None:
    adapter = make_bybit(TransportError(f"POST {WEB_SEARCH_URL} failed: connection reset"))

    with pytest.raises(TransportError):
        adapter.search_ads(UAH_USDT)


def test_search_ads_raises_on_a_venue_error_envelope() -> None:
    adapter = make_bybit(json_response({"ret_code": 10001, "ret_msg": "Invalid api key"}))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert str(excinfo.value) == "bybit error 10001: Invalid api key"


# --------------------------------------------------------------------------------------
# envelope success/failure
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"ret_code": 0, "ret_msg": "SUCCESS"},
        {"ret_code": 0, "ret_msg": "", "result": None},
        {"ret_code": 0},
        {"ret_code": 0, "ret_msg": "OK"},
        {"retCode": 0, "retMsg": "OK"},
        {"ret_code": "0", "ret_msg": "success"},
    ],
)
def test_ensure_success_accepts_documented_success_envelopes(payload: Any) -> None:
    assert make_bybit().ensure_success(payload) is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"ret_code": 10001, "ret_msg": "Invalid api key"}, "bybit error 10001: Invalid api key"),
        ({"ret_code": 10001}, "bybit error 10001: no ret_msg"),
        (
            {"retCode": 10006, "retMsg": "Too many visits. Please try again later."},
            "bybit error 10006: Too many visits. Please try again later.",
        ),
        ({"ret_code": 0, "ret_msg": "FAIL"}, "bybit returned ret_code 0 with unexpected ret_msg 'FAIL'"),
        ({"ret_code": "abc", "ret_msg": ""}, "bybit ret_code is not an integer: 'abc'"),
        ({}, "bybit response carries no ret_code/retCode; treating it as a failure"),
        ({"ret_msg": "SUCCESS"}, "bybit response carries no ret_code/retCode; treating it as a failure"),
        (
            {"ret_code": None, "retCode": None},
            "bybit response carries no ret_code/retCode; treating it as a failure",
        ),
    ],
)
def test_ensure_success_reports_every_failure_shape(payload: Any, message: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        make_bybit().ensure_success(payload)

    assert str(excinfo.value) == message
    assert excinfo.value.payload == payload


def test_ensure_success_rejects_a_non_json_envelope() -> None:
    with pytest.raises(ApiError) as excinfo:
        make_bybit().ensure_success("<html><title>Access Denied</title></html>")

    assert "bybit answered without a JSON envelope" in str(excinfo.value)
    assert excinfo.value.payload == "<html><title>Access Denied</title></html>"


# --------------------------------------------------------------------------------------
# private requests
# --------------------------------------------------------------------------------------
def test_build_login_request_is_none_because_v5_authenticates_every_call() -> None:
    assert make_bybit().build_login_request(ACCOUNT) is None


def test_list_ads_request_uses_string_pagination_fields_and_signs_them() -> None:
    request = make_bybit().build_list_ads_request(ACCOUNT, UAH_USDT)

    assert request.method == "POST"
    assert request.url == f"https://api.bybit.com{LIST_PATH}"
    assert request.json_body == {"tokenId": "USDT", "currencyId": "UAH", "page": "1", "size": "30"}
    assert_bybit_signature(request)


@pytest.mark.parametrize(("page_size", "expected"), [(100, "30"), (5, "5")])
def test_list_ads_page_size_is_capped_at_the_venue_maximum(page_size: int, expected: str) -> None:
    request = make_bybit(list_page_size=page_size).build_list_ads_request(ACCOUNT, UAH_USDT)

    assert request.json_body["size"] == expected


#: The live preferences as item/info answers them: numbers plus undocumented keys.
LIVE_PREFERENCES = {
    "completeRateDay30": "",
    "hasCompleteRateDay30": 0,
    "isKyc": 1,
    "registerTimeThreshold": 0,
    "nationalLimit": "",
    "hasSingleUserOrderLimit": 0,
    "singleUserOrderLimit": 0,
}
#: ...and as item/update accepts them: documented keys only, every value a string.
SENT_PREFERENCES = {
    "completeRateDay30": "",
    "hasCompleteRateDay30": "0",
    "isKyc": "1",
    "registerTimeThreshold": "0",
    "nationalLimit": "",
}


def item_info(**fields: Any) -> Any:
    """An ``item/info`` answer carrying the fields an update echoes."""
    item = {
        "id": "1899658238346616832",
        "remark": "1 платіж, без комісії.",
        "tradingPreferenceSet": dict(LIVE_PREFERENCES),
        "paymentPeriod": 30,
        "quantity": "112085.6784",
        "payments": ["43", "22"],
        "paymentTerms": [{"id": "2366358", "paymentType": 43}, {"id": "2782816", "paymentType": 22}],
    }
    item.update(fields)
    return json_response({"ret_code": 0, "ret_msg": "SUCCESS", "result": item})


def test_update_ad_request_uses_explicit_quantity_and_payment_ids() -> None:
    spec = make_spec(quantity="10000", payment_ids=("14", "377"), payment_methods=("BLIK",))

    body = make_bybit(item_info()).build_update_ad_request(ACCOUNT, spec, "1").json_body

    assert body["quantity"] == "10000"
    assert body["paymentIds"] == ["14", "377"]


def test_update_ad_request_derives_the_quantity_from_max_amount_over_price() -> None:
    spec = make_spec(price="47.00", max_amount="9300.00", quantity=None)

    body = make_bybit(item_info()).build_update_ad_request(ACCOUNT, spec, "1").json_body

    assert body["quantity"] == "197"  # floor(9300.00 / 47.00)


def test_update_ad_request_refuses_more_payment_ids_than_the_venue_accepts() -> None:
    spec = make_spec(payment_ids=tuple(str(index) for index in range(6)))

    with pytest.raises(ConfigError) as excinfo:
        make_bybit(item_info()).build_update_ad_request(ACCOUNT, spec, "1")

    assert str(excinfo.value) == "bybit accepts at most 5 payment ids, got 6"


@pytest.mark.parametrize(
    ("spec_kwargs", "message"),
    [
        (
            {"price": "0", "quantity": None},
            "cannot derive a quantity for UAH/USDT: price must be positive, got 0",
        ),
        (
            {"price": "47.00", "max_amount": "10.00", "quantity": None},
            "cannot derive a quantity for UAH/USDT: max_amount 10.00 is below the price 47.00",
        ),
    ],
)
def test_update_ad_request_refuses_an_unusable_quantity(spec_kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_bybit(item_info()).build_update_ad_request(ACCOUNT, make_spec(**spec_kwargs), "1")

    assert str(excinfo.value) == message


def test_update_ad_request_echoes_the_live_remark_preferences_and_payment_period() -> None:
    spec = make_spec(price="47.25", min_amount="900", max_amount="44000", quantity="2")
    adapter = make_bybit(item_info(), item_info())

    modify = adapter.build_update_ad_request(ACCOUNT, spec, "1899658238346616832")
    relist = adapter.build_update_ad_request(
        ACCOUNT, spec, "1899658238346616832", action_type="active"
    )

    info_request = adapter.transport.requests[0]
    assert info_request.url == f"https://api.bybit.com{ITEM_INFO_PATH}"
    assert info_request.json_body == {"itemId": "1899658238346616832"}
    assert_bybit_signature(info_request)
    assert modify.url == f"https://api.bybit.com{UPDATE_PATH}"
    assert modify.json_body == {
        "id": "1899658238346616832",
        "actionType": ACTION_MODIFY,
        "priceType": "0",
        "premium": "0",
        "price": "47.25",
        "minAmount": "900",
        "maxAmount": "44000",
        "remark": "1 платіж, без комісії.",  # the ad's own text, never replaced
        "tradingPreferenceSet": SENT_PREFERENCES,  # same requirements, update-schema shape
        "paymentIds": ["2366358", "2782816"],  # the ad's own payment methods, not type ids
        "quantity": "2",
        "paymentPeriod": "30",  # the ad's own period, not a default
    }
    assert relist.json_body["actionType"] == ACTION_ACTIVE
    assert_bybit_signature(modify)
    assert_bybit_signature(relist)


def test_update_refuses_to_drop_an_undocumented_preference_that_is_set() -> None:
    prefs = dict(LIVE_PREFERENCES, singleUserOrderLimit=3)
    adapter = make_bybit(item_info(tradingPreferenceSet=prefs))

    with pytest.raises(ConfigError, match="singleUserOrderLimit=3, which item/update cannot carry"):
        adapter.build_update_ad_request(ACCOUNT, make_spec(quantity="1"), "1")


@pytest.mark.parametrize("terms", [None, [], [{"id": ""}]])
def test_update_refuses_an_ad_that_reports_no_payment_methods(terms: Any) -> None:
    adapter = make_bybit(item_info(paymentTerms=terms))

    with pytest.raises(ApiError, match="reports no payment methods .paymentTerms.; refusing to replace them"):
        adapter.build_update_ad_request(ACCOUNT, make_spec(quantity="1"), "1")


def test_update_refuses_more_live_payment_methods_than_the_venue_accepts() -> None:
    terms = [{"id": str(index)} for index in range(6)]

    with pytest.raises(ConfigError, match="at most 5 payment ids, got 6"):
        make_bybit(item_info(paymentTerms=terms)).build_update_ad_request(ACCOUNT, make_spec(quantity="1"), "1")


def test_update_keeps_an_empty_remark_empty() -> None:
    body = make_bybit(item_info(remark="")).build_update_ad_request(ACCOUNT, make_spec(quantity="1"), "1").json_body

    assert body["remark"] == ""


@pytest.mark.parametrize(
    ("fields", "missing"),
    [
        ({"remark": None}, "remark"),
        ({"tradingPreferenceSet": None}, "tradingPreferenceSet"),
        ({"tradingPreferenceSet": "none"}, "tradingPreferenceSet"),
        ({"paymentPeriod": None}, "paymentPeriod"),
    ],
)
def test_update_refuses_to_overwrite_a_field_the_live_ad_does_not_report(
    fields: dict[str, Any], missing: str
) -> None:
    adapter = make_bybit(item_info(**fields))

    with pytest.raises(ApiError) as excinfo:
        adapter.build_update_ad_request(ACCOUNT, make_spec(quantity="1"), "1")

    assert f"lacks {missing}; refusing to overwrite them with defaults" in str(excinfo.value)
    assert len(adapter.transport) == 1  # only item/info: no update is built


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"ret_code": 0, "ret_msg": "SUCCESS", "result": None}, "has no detail object"),
        ({"ret_code": 10001, "ret_msg": "item not found"}, "item not found"),
    ],
)
def test_update_reports_an_unreadable_live_ad(payload: Any, error: str) -> None:
    with pytest.raises(ApiError, match=error):
        make_bybit(json_response(payload)).build_update_ad_request(ACCOUNT, make_spec(quantity="1"), "1")


def test_update_ad_request_rejects_an_unknown_action() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_bybit().build_update_ad_request(ACCOUNT, make_spec(), "1", action_type="FOO")

    assert str(excinfo.value) == "unknown bybit update action 'FOO'; expected one of MODIFY, ACTIVE"


def test_an_inactive_spec_is_taken_down_with_the_cancel_endpoint() -> None:
    adapter = make_bybit()

    request = adapter.build_update_ad_request(ACCOUNT, make_spec(active=False), "1899658238346616832")

    assert request.url == f"https://api.bybit.com{CANCEL_PATH}"
    assert request.json_body == {"itemId": "1899658238346616832"}
    assert_bybit_signature(request)
    assert adapter.build_cancel_ad_request(ACCOUNT, "42").json_body == {"itemId": "42"}


@pytest.mark.parametrize("adv_no", ["", "   ", None])
def test_cancel_and_update_need_an_advertisement_id(adv_no: Any) -> None:
    adapter = make_bybit()

    with pytest.raises(ConfigError) as excinfo:
        adapter.build_cancel_ad_request(ACCOUNT, adv_no)

    assert str(excinfo.value) == "bybit needs the advertisement id (itemId) to update or cancel an ad"

    with pytest.raises(ConfigError):
        adapter.build_update_ad_request(ACCOUNT, make_spec(), adv_no)


def test_signature_is_reproducible_and_depends_on_the_secret() -> None:
    adapter = make_bybit()
    request = adapter.build_list_ads_request(ACCOUNT, UAH_USDT)
    other = make_account("bybit", 1, {"API_KEY": "bybit-key", "SECRET_KEY": "another-secret"})

    assert request == make_bybit().build_list_ads_request(ACCOUNT, UAH_USDT)
    assert adapter.build_list_ads_request(other, UAH_USDT).headers["X-BAPI-SIGN"] != request.headers["X-BAPI-SIGN"]


def test_timestamp_is_milliseconds_from_the_injected_clock_in_utc() -> None:
    naive = BybitAdapter(FakeTransport(), now=FixedClock(datetime(2026, 9, 24, 12, 0, 0, 123_000)))
    elsewhere = BybitAdapter(
        FakeTransport(),
        now=FixedClock(datetime(2026, 9, 24, 14, 30, 0, 123_000, tzinfo=timezone(timedelta(hours=2, minutes=30)))),
    )

    assert naive.build_list_ads_request(ACCOUNT, UAH_USDT).headers["X-BAPI-TIMESTAMP"] == str(FIXED_NOW_MS)
    assert elsewhere.build_list_ads_request(ACCOUNT, UAH_USDT).headers["X-BAPI-TIMESTAMP"] == str(FIXED_NOW_MS)
    assert naive.timestamp_ms() == str(FIXED_NOW_MS)


# --------------------------------------------------------------------------------------
# private responses
# --------------------------------------------------------------------------------------
def test_parse_ad_response_reads_result_item_id_only() -> None:
    result = make_bybit().parse_ad_response({"ret_code": 0, "ret_msg": "SUCCESS", "result": {"itemId": "1899658238346616832"}})

    assert result.adv_no == "1899658238346616832"
    assert result.platform == "bybit"
    assert result.price is None  # update/cancel echo no price
    assert result.account_id == ""
    assert result.pair == Pair.parse("XXX/XXX")
    assert result.raw == {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"itemId": "1899658238346616832"}}


@pytest.mark.parametrize(
    "payload",
    [
        {"ret_code": 0, "ret_msg": "SUCCESS"},
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": []},
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"itemId": ""}},
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"itemId": None}},
    ],
)
def test_parse_ad_response_without_an_item_id(payload: Any) -> None:
    result = make_bybit().parse_ad_response(payload)

    assert result.adv_no is None
    assert result.raw == payload


def test_parse_ad_response_keeps_a_non_object_payload_as_an_empty_raw() -> None:
    result = make_bybit().parse_ad_response("nope")

    assert result.adv_no is None
    assert result.raw == {}


def test_parse_ad_result_bridges_the_response_into_the_identity_fields() -> None:
    adapter = make_bybit()

    echoed = adapter.parse_ad_result(
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"itemId": "1899658238346616832"}},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=make_spec(price="47.00"),
    )
    addressed = adapter.parse_ad_result(
        {"ret_code": 0, "ret_msg": "SUCCESS"},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=make_spec(price="47.25"),
        adv_no="1899658238346616832",
    )

    assert (echoed.adv_no, echoed.price) == ("1899658238346616832", Decimal("47.00"))
    assert echoed.account_id == "Bybit#1"
    assert echoed.pair is UAH_USDT
    assert (addressed.adv_no, addressed.price) == ("1899658238346616832", Decimal("47.25"))


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ret_code": 0, "result": {"items": [{"id": "1"}]}}, ({"id": "1"},)),
        ({"ret_code": 0, "result": {"items": [{"id": "1"}, "junk", 5]}}, ({"id": "1"},)),
        ({"ret_code": 0, "result": {}}, ()),
        ({"ret_code": 0, "result": {"items": "nope"}}, ()),
        ({"ret_code": 0}, ()),
        (None, ()),
    ],
)
def test_parse_ad_list_reads_result_items(payload: Any, expected: tuple[Any, ...]) -> None:
    assert make_bybit().parse_ad_list(payload) == expected


def test_list_my_ads_returns_the_venue_records() -> None:
    adapter = make_bybit(json_response({"ret_code": 0, "ret_msg": "SUCCESS", "result": {"items": [{"id": "1", "price": "47.00"}]}}))

    records = adapter.list_my_ads(ACCOUNT, UAH_USDT)

    assert records == ({"id": "1", "price": "47.00"},)
    assert adapter.transport.last.url == f"https://api.bybit.com{LIST_PATH}"


def test_send_private_surfaces_a_venue_error_payload() -> None:
    adapter = make_bybit(json_response({"ret_code": 10001, "ret_msg": "Invalid api key"}))

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert str(excinfo.value) == "bybit error 10001: Invalid api key"
    assert excinfo.value.payload == {"ret_code": 10001, "ret_msg": "Invalid api key"}


def test_send_private_surfaces_an_http_failure() -> None:
    adapter = make_bybit(text_response("Service Unavailable", status=503))

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert excinfo.value.status == 503
    assert "bybit request for account Bybit#1 failed with HTTP 503" in str(excinfo.value)


# -- every own advertisement -----------------------------------------------------------
def _own_item(item_id: str, **fields: Any) -> dict[str, Any]:
    item = {
        "id": item_id,
        "tokenId": "USDT",
        "currencyId": "UAH",
        "side": 1,
        "price": "47.00",
        "lastQuantity": "250.5",
        "minAmount": "1000",
        "maxAmount": "200000",
        "status": 10,
        "payments": ["7110", "", None],
        "paymentTerms": [{"id": "2366358", "paymentType": 7110}, {"id": ""}, "junk"],
    }
    item.update(fields)
    return item


def _own_page(*items: dict[str, Any]) -> Any:
    return json_response(
        {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"count": len(items), "items": list(items)}}
    )


def test_own_ads_request_lists_every_pair_and_status() -> None:
    request = make_bybit().build_own_ads_request(ACCOUNT, page=2)

    assert request.url == f"https://api.bybit.com{LIST_PATH}"
    assert request.json_body == {"page": "2", "size": "30"}
    assert_bybit_signature(request)


def test_own_ads_page_size_is_capped_at_the_venue_maximum() -> None:
    request = make_bybit(own_ads_page_size=100).build_own_ads_request(ACCOUNT, page=1)

    assert request.json_body["size"] == "30"


def test_fetch_own_ads_normalizes_online_offline_and_completed_ads() -> None:
    adapter = make_bybit(
        _own_page(
            _own_item("1"),
            _own_item("2", status=20, side=0, tokenId="USDC", currencyId="PLN", price="bad"),
            _own_item("3", status=30),
            _own_item("4", status=99),
            _own_item("", status=10),
            _own_item("5", tokenId=None),
        ),
        _own_page(),
    )

    ads = adapter.fetch_own_ads(ACCOUNT)

    assert [(ad.adv_no, ad.pair.symbol, ad.side, ad.status) for ad in ads] == [
        ("1", "UAH/USDT", SIDE_SELL, "online"),
        ("2", "PLN/USDC", SIDE_BUY, "offline"),
        ("3", "UAH/USDT", SIDE_SELL, "closed"),
        ("4", "UAH/USDT", SIDE_SELL, "unknown"),
    ]
    first = ads[0]
    assert first.account_id == "Bybit#1"
    assert first.price == Decimal("47.00")
    assert first.quantity == Decimal("250.5")
    assert (first.min_amount, first.max_amount) == (Decimal("1000"), Decimal("200000"))
    assert first.payment_methods == ("7110",)
    assert first.venue_status == "10"
    assert ads[1].price is None


def test_fetch_own_ads_follows_full_pages() -> None:
    full = [_own_item(str(index)) for index in range(30)]
    adapter = make_bybit(_own_page(*full), _own_page(_own_item("30")))

    ads = adapter.fetch_own_ads(ACCOUNT)

    assert len(ads) == 31
    assert [request.json_body["page"] for request in adapter.transport.requests] == ["1", "2"]


def test_parse_own_ad_reports_the_total_quantity_payment_ids_and_a_floating_premium() -> None:
    adapter = make_bybit()

    fixed = adapter.parse_own_ad(
        _own_item("1", quantity="300", lastQuantity="120", priceType=0, premium=""), ACCOUNT
    )
    floating = adapter.parse_own_ad(_own_item("2", priceType=1, premium="101.5"), ACCOUNT)

    # the update sends the amount left on the ad, never the listing's total
    assert fixed.total_quantity == Decimal("120")
    assert fixed.payment_methods == ("7110",)  # payment *type* ids, for display
    assert fixed.payment_ids == ("2366358",)  # the account's own ids, what an update needs
    assert fixed.price_floating_ratio is None
    assert floating.price_floating_ratio == Decimal("101.5")


def test_update_ad_request_refuses_a_floating_ratio() -> None:
    spec = make_spec(quantity="1", price_floating_ratio="91")

    with pytest.raises(ConfigError, match="bybit: floating-price updates are not supported"):
        make_bybit().build_update_ad_request(ACCOUNT, spec, "1")
