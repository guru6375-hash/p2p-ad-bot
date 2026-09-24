"""``p2pbot/exchanges/base.py``: HTTP plumbing, transport and the shared adapter contract.

Everything here is hermetic: the adapters run on :class:`FakeTransport` and
:class:`UrllibTransport` is driven through an injected fake ``urllib`` opener.
"""

from __future__ import annotations

import io
import urllib.error
from decimal import Decimal
from typing import Any, Mapping

import pytest

from p2pbot.constants import DEFAULT_USER_AGENT, SIDE_SELL
from p2pbot.errors import ApiError, TransportError
from p2pbot.exchanges.base import (
    ExchangeAdapter,
    HttpRequest,
    HttpResponse,
    Transport,
    UrllibTransport,
    safe_json,
)
from p2pbot.models import Account, AdActionResult, AdSpec, CompetitorAd, Filters, Pair

from _fake_transport import (
    FIXED_NOW,
    FakeOpener,
    FakeOpenerResponse,
    FakeTransport,
    FixedClock,
    json_response,
    make_account,
    make_spec,
    text_response,
)

UAH_USDT = Pair.parse("UAH/USDT")
ACCOUNT = make_account("stub", 1, {"API_KEY": "k", "SECRET_KEY": "s"})


class StubAdapter(ExchangeAdapter):
    """Minimal concrete adapter exercising the shared orchestration of ``base.py``.

    ``parse_search_response`` reads a simplified ``{"rows": [{price, advertiser,
    user_type, month_order_count, positive_rate, month_finish_rate}]}`` payload;
    ``parse_ad_response`` reads ``{"data": <adv_no>}`` plus an optional ``"price"`` echo.
    """

    platform = "stub"

    def __init__(
        self,
        transport: Transport | None = None,
        now: Any = None,
        *,
        platform: str | None = None,
        failure: str | None = None,
    ) -> None:
        super().__init__(transport, now=now)
        if platform is not None:
            self.platform = platform
        self._failure = failure
        self.search_calls: list[tuple[Any, ...]] = []

    # -- venue hooks ---------------------------------------------------------------
    def build_search_request(
        self, pair: Pair, *, side: str = SIDE_SELL, page: int = 1, rows: int = 20
    ) -> HttpRequest:
        self.search_calls.append((pair, side, page, rows))
        return HttpRequest(
            method="POST",
            url="https://stub.test/search",
            json_body={"page": page, "rows": rows, "side": side},
        )

    def parse_search_response(self, payload: Any, pair: Pair) -> tuple[CompetitorAd, ...]:
        ads = []
        for row in payload.get("rows", ()):
            ads.append(
                CompetitorAd(
                    platform=self.platform,
                    pair=pair,
                    price=Decimal(str(row["price"])),
                    advertiser=row.get("advertiser", ""),
                    user_type=row.get("user_type", ""),
                    month_order_count=_optional(row.get("month_order_count")),
                    positive_rate=_optional(row.get("positive_rate")),
                    month_finish_rate=_optional(row.get("month_finish_rate")),
                    adv_no=row.get("adv_no"),
                )
            )
        return tuple(ads)

    def ensure_success(self, payload: Any) -> None:
        if self._failure is not None:
            raise ApiError(self._failure, payload=payload)
        if not isinstance(payload, (Mapping, list)):
            raise ApiError(f"stub: non-object payload ({type(payload).__name__})", payload=payload)

    def build_login_request(self, account: Account) -> HttpRequest | None:
        return None

    def build_list_ads_request(self, account: Account, pair: Pair) -> HttpRequest:
        return HttpRequest(method="POST", url="https://stub.test/list", json_body={"fiat": pair.fiat})

    def build_create_ad_request(
        self, account: Account, spec: AdSpec, adv_no: str | None = None
    ) -> HttpRequest:
        return HttpRequest(method="POST", url="https://stub.test/create")

    def build_update_ad_request(self, account: Account, spec: AdSpec, adv_no: str) -> HttpRequest:
        return HttpRequest(method="POST", url="https://stub.test/update", json_body={"id": adv_no})

    def parse_ad_response(self, payload: Any) -> AdActionResult:
        data = payload.get("data") if isinstance(payload, Mapping) else None
        price = payload.get("price") if isinstance(payload, Mapping) else None
        return AdActionResult(
            platform=self.platform,
            account_id="",
            pair=Pair.parse("XXX/XXX"),
            adv_no=str(data) if isinstance(data, str) else None,
            price=Decimal(str(price)) if price is not None else None,  # type: ignore[arg-type]
            created=False,
            raw={"payload": payload},
        )


def _optional(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def make_adapter(*scripted: Any, platform: str | None = None, failure: str | None = None) -> Any:
    return StubAdapter(FakeTransport(*scripted), now=FixedClock(), platform=platform, failure=failure)


# --------------------------------------------------------------------------------------
# HttpRequest / HttpResponse / safe_json
# --------------------------------------------------------------------------------------
def test_with_headers_merges_over_a_copy_and_leaves_the_original_untouched() -> None:
    original = HttpRequest(
        method="post",
        url="https://x.test/a",
        params={"p": "1"},
        form_body={"f": "2"},
        headers={"A": "1"},
    )
    merged = original.with_headers({"B": "2", "A": "over"})

    assert merged.headers == {"A": "over", "B": "2"}
    assert (merged.method, merged.url, merged.params, merged.form_body) == (
        "post",
        "https://x.test/a",
        {"p": "1"},
        {"f": "2"},
    )
    assert merged.json_body is None
    assert original.headers == {"A": "1"}


def test_response_text_and_json_decode_utf8() -> None:
    response = HttpResponse(status=200, body='{"nickName": "żółć"}'.encode("utf-8"))

    assert response.text == '{"nickName": "żółć"}'
    assert response.json == {"nickName": "żółć"}


def test_response_text_replaces_undecodable_bytes_and_json_then_raises() -> None:
    response = HttpResponse(status=200, body=b"\xff\xfe<html>")

    assert response.text == "\ufffd\ufffd<html>"
    with pytest.raises(ApiError) as excinfo:
        response.json
    assert excinfo.value.status == 200
    assert "not valid JSON" in str(excinfo.value)


def test_response_json_raises_api_error_carrying_the_status_and_body_excerpt() -> None:
    response = HttpResponse(status=451, body=b"<html>Access Denied</html>")

    with pytest.raises(ApiError) as excinfo:
        response.json

    message = str(excinfo.value)
    assert "<html>Access Denied</html>" in message
    assert "HTTP 451" in message
    assert excinfo.value.status == 451


def test_response_header_lookup_is_case_insensitive_and_defaultable() -> None:
    response = HttpResponse(status=200, headers={"Content-Type": "application/json", "X-Bapi-Limit": "10"})

    assert response.header("content-type") == "application/json"
    assert response.header("X-BAPI-LIMIT") == "10"
    assert response.header("missing") is None
    assert response.header("missing", "fallback") == "fallback"


def test_safe_json_returns_none_instead_of_raising() -> None:
    assert safe_json(json_response({"data": [], "code": "000000"})) == {"data": [], "code": "000000"}
    assert safe_json(text_response("<html>403</html>", status=403)) is None


def test_fake_transport_satisfies_the_transport_protocol() -> None:
    assert isinstance(FakeTransport(), Transport)
    assert not isinstance(object(), Transport)


# --------------------------------------------------------------------------------------
# UrllibTransport (driven through an injected fake opener - never a socket)
# --------------------------------------------------------------------------------------
def test_urllib_transport_builds_a_signed_json_post_with_the_default_headers() -> None:
    opener = FakeOpener(FakeOpenerResponse(body=b'{"ok": 1}', headers={"Content-Type": "application/json"}))
    transport = UrllibTransport(opener=opener)

    response = transport.send(
        HttpRequest(
            method="post",
            url="https://api.test/a",
            params={"advNo": "7", "signature": "abc"},
            json_body={"page": 1},
            headers={"X-MBX-APIKEY": "k"},
        ),
        timeout=7.5,
    )

    prepared = opener.requests[0]
    assert prepared.full_url == "https://api.test/a?advNo=7&signature=abc"
    assert prepared.get_method() == "POST"
    assert prepared.data == b'{"page": 1}'
    assert prepared.get_header("User-agent") == DEFAULT_USER_AGENT
    assert prepared.get_header("Accept") == "application/json, text/plain, */*"
    assert prepared.get_header("X-mbx-apikey") == "k"
    assert prepared.get_header("Content-type") == "application/json"
    assert opener.timeouts == [7.5]
    assert response.status == 200
    assert response.json == {"ok": 1}
    # Response header keys are lower-cased for predictable lookup.
    assert response.headers == {"content-type": "application/json"}
    assert response.header("CONTENT-TYPE") == "application/json"


def test_urllib_transport_appends_params_to_a_url_with_an_existing_query_and_drops_nones() -> None:
    opener = FakeOpener(FakeOpenerResponse(body=b""))
    UrllibTransport(opener=opener).send(
        HttpRequest(method="GET", url="https://x.test/a?fixed=1", params={"a": "1", "b": None})
    )

    assert opener.requests[0].full_url == "https://x.test/a?fixed=1&a=1"


def test_urllib_transport_omits_the_query_when_every_param_is_empty() -> None:
    opener = FakeOpener(FakeOpenerResponse(body=b""))
    UrllibTransport(opener=opener).send(
        HttpRequest(method="GET", url="https://x.test/a", params={"a": None, "b": None})
    )

    assert opener.requests[0].full_url == "https://x.test/a"


def test_urllib_transport_urlencodes_a_form_body_and_defaults_the_method() -> None:
    opener = FakeOpener(FakeOpenerResponse(body=b""))
    UrllibTransport(opener=opener).send(
        HttpRequest(method="", url="https://x.test/form", form_body={"a": "1 2", "b": "ć"})
    )

    prepared = opener.requests[0]
    assert prepared.get_method() == "GET"
    assert prepared.data == "a=1+2&b=%C4%87".encode("ascii")
    assert prepared.get_header("Content-type") == "application/x-www-form-urlencoded"


def test_urllib_transport_keeps_an_explicit_content_type_and_custom_user_agent() -> None:
    opener = FakeOpener(FakeOpenerResponse(body=b""))
    transport = UrllibTransport(opener=opener, user_agent="custom-agent/1.0")
    transport.send(
        HttpRequest(
            method="POST",
            url="https://x.test/a",
            json_body={"a": 1},
            headers={"Content-Type": "application/vnd.test+json"},
        )
    )

    prepared = opener.requests[0]
    assert prepared.get_header("Content-type") == "application/vnd.test+json"
    assert prepared.get_header("User-agent") == "custom-agent/1.0"


def test_urllib_transport_returns_the_venue_error_body_as_a_response() -> None:
    error = urllib.error.HTTPError(
        "https://x.test/a",
        451,
        "Unavailable For Legal Reasons",
        {"Retry-After": "60", "x-request-id": "abc"},
        io.BytesIO(b'{"code": "-1003", "msg": "rate limited"}'),
    )
    transport = UrllibTransport(opener=FakeOpener(error))

    response = transport.send(HttpRequest(method="GET", url="https://x.test/a"))

    assert response.status == 451
    assert response.json == {"code": "-1003", "msg": "rate limited"}
    assert response.header("retry-after") == "60"


def test_urllib_transport_survives_an_unreadable_error_body() -> None:
    class Unreadable(io.BytesIO):
        def read(self, amt: Any = None) -> bytes:  # type: ignore[override]
            raise OSError("connection dropped while reading the error body")

    error = urllib.error.HTTPError("https://x.test/a", 502, "Bad Gateway", {}, Unreadable(b"x"))
    transport = UrllibTransport(opener=FakeOpener(error))

    response = transport.send(HttpRequest(method="GET", url="https://x.test/a"))

    assert response.status == 502
    assert response.body == b""
    assert response.headers == {}


def test_urllib_transport_defaults_a_statusless_headerless_response() -> None:
    transport = UrllibTransport(opener=FakeOpener(FakeOpenerResponse(body=b"x", status=None)))

    response = transport.send(HttpRequest(method="GET", url="https://x.test/a"))

    assert response.status == 200
    assert response.body == b"x"
    assert response.headers == {}


@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError("dns"), OSError("connection reset"), TimeoutError("timed out")],
)
def test_urllib_transport_translates_connection_failures_into_transport_error(
    failure: BaseException,
) -> None:
    transport = UrllibTransport(opener=FakeOpener(failure))

    with pytest.raises(TransportError) as excinfo:
        transport.send(HttpRequest(method="POST", url="https://x.test/a"))

    assert "POST https://x.test/a failed" in str(excinfo.value)
    assert excinfo.value.__cause__ is failure


# --------------------------------------------------------------------------------------
# ExchangeAdapter: construction, clock, search_ads orchestration
# --------------------------------------------------------------------------------------
def test_default_transport_is_urllib_and_the_clock_is_injectable() -> None:
    assert isinstance(StubAdapter().transport, UrllibTransport)

    explicit = FakeTransport()
    assert StubAdapter(explicit).transport is explicit
    assert StubAdapter(now=FixedClock()).now() == FIXED_NOW


def test_base_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        ExchangeAdapter()  # type: ignore[abstract]


def test_search_ads_builds_sends_parses_and_snapshots() -> None:
    adapter = make_adapter(
        json_response(
            {
                "rows": [
                    {"price": "47.10", "advertiser": "merchant-a", "user_type": "merchant"},
                    {"price": "47.05", "advertiser": "user-b", "user_type": "user"},
                    {"price": "47.20", "advertiser": "merchant-c", "user_type": "Merchant"},
                ]
            }
        )
    )
    transport = adapter.transport

    snapshot = adapter.search_ads(UAH_USDT, side="sell", page=2, rows=5)

    assert adapter.search_calls == [(UAH_USDT, "sell", 2, 5)]
    assert transport.last.json_body == {"page": 2, "rows": 5, "side": "sell"}
    assert transport.last.url == "https://stub.test/search"
    assert snapshot.platform == "stub"
    assert snapshot.pair == UAH_USDT
    assert [ad.price for ad in snapshot.ads] == [
        Decimal("47.10"),
        Decimal("47.05"),
        Decimal("47.20"),
    ]
    # No hardcoded filter for "stub": the default Filters() keeps merchants only
    # (case-insensitively) and the middle price comes from the kept ads.
    assert [ad.advertiser for ad in snapshot.filtered] == ["merchant-a", "merchant-c"]
    assert snapshot.middle == Decimal("47.15")
    assert snapshot.fetched_at == FIXED_NOW


def test_search_ads_uses_the_platforms_hardcoded_filter_by_default() -> None:
    adapter = make_adapter(
        json_response(
            {
                "rows": [
                    {
                        "price": "47.10",
                        "advertiser": "passes",
                        "user_type": "merchant",
                        "month_order_count": 501,
                        "positive_rate": "0.98",
                        "month_finish_rate": "0.95",
                    },
                    {
                        "price": "47.00",
                        "advertiser": "too-few-orders",
                        "user_type": "merchant",
                        "month_order_count": 500,
                        "positive_rate": "0.98",
                        "month_finish_rate": "0.95",
                    },
                    {
                        "price": "46.90",
                        "advertiser": "no-metrics",
                        "user_type": "merchant",
                    },
                ]
            }
        ),
        platform="binance",
    )

    snapshot = adapter.search_ads(UAH_USDT)

    assert snapshot.platform == "binance"
    assert len(snapshot.ads) == 3
    assert [ad.advertiser for ad in snapshot.filtered] == ["passes"]
    assert snapshot.middle == Decimal("47.10")


def test_search_ads_explicit_filters_override_the_platform_default() -> None:
    adapter = make_adapter(
        json_response(
            {
                "rows": [
                    {"price": "47.10", "advertiser": "merchant-a", "user_type": "merchant"},
                    {"price": "47.05", "advertiser": "user-b", "user_type": "user"},
                ]
            }
        )
    )

    snapshot = adapter.search_ads(UAH_USDT, filters=Filters(user_type=None))

    assert [ad.advertiser for ad in snapshot.filtered] == ["merchant-a", "user-b"]
    assert snapshot.middle == Decimal("47.08")


def test_search_ads_raises_api_error_with_the_payload_on_an_http_failure() -> None:
    adapter = make_adapter(json_response({"code": -1003, "msg": "too many requests"}, status=429))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert excinfo.value.status == 429
    assert excinfo.value.payload == {"code": -1003, "msg": "too many requests"}
    assert "stub search for UAH/USDT failed with HTTP 429" in str(excinfo.value)


def test_search_ads_propagates_a_non_json_body_as_api_error() -> None:
    adapter = make_adapter(text_response("<html>Access Denied</html>", status=200))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert "not valid JSON" in str(excinfo.value)


def test_search_ads_raises_when_the_venue_payload_reports_failure() -> None:
    adapter = make_adapter(json_response({"code": "999999"}), failure="stub reported failure")

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert str(excinfo.value) == "stub reported failure"
    assert excinfo.value.payload == {"code": "999999"}


def test_search_ads_lets_transport_failures_escape_unchanged() -> None:
    adapter = make_adapter(TransportError("GET https://stub.test/search failed: dns"))

    with pytest.raises(TransportError):
        adapter.search_ads(UAH_USDT)


# --------------------------------------------------------------------------------------
# ExchangeAdapter: private execution helpers
# --------------------------------------------------------------------------------------
def test_send_private_returns_the_parsed_payload() -> None:
    adapter = make_adapter(json_response({"code": "000000", "data": {"advNo": "1"}}))

    payload = adapter.send_private(
        ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT)
    )

    assert payload == {"code": "000000", "data": {"advNo": "1"}}
    assert adapter.transport.last.url == "https://stub.test/list"


def test_send_private_raises_api_error_on_an_http_failure() -> None:
    adapter = make_adapter(json_response({"code": -2015, "msg": "Invalid API-key"}, status=401))

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert excinfo.value.status == 401
    assert excinfo.value.payload == {"code": -2015, "msg": "Invalid API-key"}
    assert "stub request for account Stub#1 failed with HTTP 401" in str(excinfo.value)


def test_send_private_raises_when_the_venue_payload_reports_failure() -> None:
    adapter = make_adapter(json_response({"code": -1022, "msg": "Signature for this request is not valid."}), failure="stub rejected the signature")

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert str(excinfo.value) == "stub rejected the signature"


def test_send_private_raises_when_a_200_body_is_not_json() -> None:
    adapter = make_adapter(text_response("<html>maintenance</html>"))

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert "stub: non-object payload (NoneType)" in str(excinfo.value)


def test_list_my_ads_uses_the_listing_request_and_the_default_parser() -> None:
    adapter = make_adapter(json_response([{"advNo": "1"}, {"advNo": "2"}]))

    records = adapter.list_my_ads(ACCOUNT, UAH_USDT)

    assert records == ({"advNo": "1"}, {"advNo": "2"})
    assert adapter.transport.last.json_body == {"fiat": "UAH"}


def test_parse_ad_list_default_returns_only_mapping_items_of_a_list_payload() -> None:
    adapter = StubAdapter(FakeTransport())

    assert adapter.parse_ad_list([{"advNo": "1"}]) == ({"advNo": "1"},)
    assert adapter.parse_ad_list([{"advNo": "1"}, "junk", 5, {"advNo": "2"}]) == (
        {"advNo": "1"},
        {"advNo": "2"},
    )
    assert adapter.parse_ad_list(()) == ()
    assert adapter.parse_ad_list({"data": [{"advNo": "1"}]}) == ()
    assert adapter.parse_ad_list(None) == ()


# --------------------------------------------------------------------------------------
# ExchangeAdapter: parse_ad_result identity bridging
# --------------------------------------------------------------------------------------
def test_parse_ad_result_fills_identity_fields_from_the_request_context() -> None:
    adapter = StubAdapter(FakeTransport())
    spec = make_spec(price="47.25")

    result = adapter.parse_ad_result(
        {"data": "13928301035093368832"},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=spec,
        created=True,
    )

    assert result == AdActionResult(
        platform="stub",
        account_id="Stub#1",
        pair=UAH_USDT,
        adv_no="13928301035093368832",
        price=Decimal("47.25"),
        created=True,
        raw={"payload": {"data": "13928301035093368832"}},
    )


def test_parse_ad_result_prefers_the_venue_echoed_price() -> None:
    adapter = StubAdapter(FakeTransport())
    spec = make_spec(price="47.25")

    result = adapter.parse_ad_result(
        {"data": "9", "price": "47.99"},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=spec,
        created=False,
    )

    assert result.price == Decimal("47.99")
    assert result.created is False
    assert result.adv_no == "9"


def test_parse_ad_result_falls_back_to_the_addressed_adv_no() -> None:
    adapter = StubAdapter(FakeTransport())
    spec = make_spec(price="47.25")

    result = adapter.parse_ad_result(
        {"data": True},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=spec,
        created=False,
        adv_no="777",
    )

    assert result.adv_no == "777"
    assert result.price == Decimal("47.25")


def test_parse_ad_result_keeps_the_venue_payload_as_raw() -> None:
    adapter = StubAdapter(FakeTransport())
    payload = {"code": "000000", "data": {"advNo": "5"}}

    result = adapter.parse_ad_result(
        payload, account=ACCOUNT, pair=UAH_USDT, spec=make_spec(), created=True
    )

    assert result.raw == {"payload": payload}
    assert result.account_id == ACCOUNT.id
    assert result.pair is UAH_USDT


# --------------------------------------------------------------------------------------
# the registry (``p2pbot/exchanges/__init__.py``)
# --------------------------------------------------------------------------------------
def test_registry_maps_every_platform_name_to_its_adapter_class() -> None:
    from p2pbot.constants import PLATFORMS
    from p2pbot.exchanges import ADAPTERS
    from p2pbot.exchanges.binance import BinanceAdapter
    from p2pbot.exchanges.bybit import BybitAdapter
    from p2pbot.exchanges.okx import OkxAdapter

    assert list(ADAPTERS) == list(PLATFORMS) == ["binance", "okx", "bybit"]
    assert ADAPTERS == {"binance": BinanceAdapter, "okx": OkxAdapter, "bybit": BybitAdapter}


def test_build_adapters_shares_one_transport_and_the_injected_clock() -> None:
    from p2pbot.exchanges import ADAPTERS, build_adapters

    shared = FakeTransport()
    adapters = build_adapters(shared, now=FixedClock())

    assert list(adapters) == ["binance", "okx", "bybit"]
    assert {name: type(adapter) for name, adapter in adapters.items()} == dict(ADAPTERS)
    assert all(adapter.transport is shared for adapter in adapters.values())
    assert [adapter.now() for adapter in adapters.values()] == [FIXED_NOW] * 3


def test_build_adapters_defaults_to_the_urllib_transport() -> None:
    from p2pbot.exchanges import build_adapters

    adapters = build_adapters()

    assert all(isinstance(adapter.transport, UrllibTransport) for adapter in adapters.values())


def test_registry_reexports_the_shared_plumbing() -> None:
    from p2pbot import exchanges

    assert exchanges.HttpRequest is HttpRequest
    assert exchanges.HttpResponse is HttpResponse
    assert exchanges.Transport is Transport
    assert exchanges.UrllibTransport is UrllibTransport
    assert exchanges.ExchangeAdapter is ExchangeAdapter
