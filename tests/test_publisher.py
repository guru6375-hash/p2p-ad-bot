"""Publisher: the ad ledger, buy-ad edits (``edit_ad``) and the live ad listing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot.constants import SIDE_BUY
from p2pbot.errors import BotError, ConfigError, TransportError
from p2pbot.models import AdActionResult, AdRecord, AdSpec, OwnAd, Pair
from p2pbot.publisher import AdPublisher, AdStore

UAH_USDT = Pair.parse("UAH/USDT")
UAH_USDC = Pair.parse("UAH/USDC")


class _FakeAdapter:
    """Adapter double recording the requests the publisher builds."""

    def __init__(
        self,
        platform: str = "binance",
        *,
        adv_no: str | None = "2048",
        error: BaseException | None = None,
        login_request: object | None = None,
        login_error: BaseException | None = None,
        own_ads: tuple[OwnAd, ...] = (),
        list_error: BaseException | None = None,
    ) -> None:
        self.platform = platform
        self.own_ads = own_ads
        self.list_error = list_error
        self.adv_no = adv_no
        self.error = error
        self.login_request = login_request
        self.login_error = login_error
        self.built: list[tuple[str, str, Decimal, bool]] = []
        self.specs: list[AdSpec] = []
        self.sent: list[tuple[str, object]] = []
        self.logins: list[str] = []

    # -- builder hooks --------------------------------------------------------------
    def build_login_request(self, account):
        self.logins.append(account.id)
        return self.login_request

    def build_update_ad_request(self, account, spec, adv_no):
        self.built.append(("update", account.id, spec.price, spec.active))
        self.specs.append(spec)
        return f"update:{account.id}:{adv_no}"

    def fetch_own_ads(self, account):
        if self.list_error is not None:
            raise self.list_error
        return tuple(ad for ad in self.own_ads if ad.account_id == account.id)

    # -- execution hooks -------------------------------------------------------------
    def send_private(self, account, request):
        self.sent.append((account.id, request))
        if request == self.login_request and self.login_error is not None:
            raise self.login_error
        if self.error is not None:
            raise self.error
        return {"raw": True}

    def parse_ad_result(self, payload, *, account, pair, spec):
        return AdActionResult(
            platform=self.platform,
            account_id=account.id,
            pair=pair,
            adv_no=self.adv_no,
            price=spec.price,
            raw=payload,
        )


def _publisher(
    settings,
    *,
    adapters=None,
    store=None,
    dry_run=False,
) -> tuple[AdPublisher, dict[str, _FakeAdapter], AdStore]:
    resolved = adapters if adapters is not None else {"binance": _FakeAdapter("binance")}
    ad_store = store if store is not None else AdStore()
    publisher = AdPublisher(resolved, settings, ad_store, dry_run)
    return publisher, resolved, ad_store


# -- AdStore ---------------------------------------------------------------------------
def test_store_key_is_canonicalised_for_lookup() -> None:
    store = AdStore()
    store.put(AdRecord(account_id="binance#2", pair=UAH_USDC, adv_no="2", price=Decimal("46.75")))
    assert store.get("BINANCE#2", "uah/usdc").adv_no == "2"
    assert store.get("binance#2", UAH_USDC).adv_no == "2"


def test_store_items_are_ordered_by_account_and_pair() -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#2", pair=UAH_USDT, adv_no="2", price=Decimal("47.00")))
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="1", price=Decimal("47.00")))
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDC, adv_no="3", price=Decimal("46.75")))
    assert [(record.account_id, record.pair.symbol) for record in store.items()] == [
        ("Binance#1", "UAH/USDC"),
        ("Binance#1", "UAH/USDT"),
        ("Binance#2", "UAH/USDT"),
    ]


def test_store_put_replaces_the_previous_record() -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="1", price=Decimal("47.00")))
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="1", price=Decimal("47.10")))
    assert len(store.items()) == 1
    assert store.get("Binance#1", UAH_USDT).price == Decimal("47.10")


def test_store_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ads.json"
    moment = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    store = AdStore(path=path)
    store.put(
        AdRecord(
            account_id="Binance#1",
            pair=UAH_USDT,
            adv_no="2048",
            price=Decimal("47.00"),
            active=False,
            updated_at=moment,
        )
    )
    store.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["records"][0]["adv_no"] == "2048"
    assert payload["records"][0]["price"] == "47.00"
    assert payload["records"][0]["active"] is False
    assert list(tmp_path.glob("**/*.tmp")) == []

    reloaded = AdStore.load(path)
    assert reloaded.as_dict() == store.as_dict()
    assert reloaded.get("Binance#1", UAH_USDT).updated_at == moment
    assert reloaded.path == path


def test_store_save_without_a_path_is_a_no_op(tmp_path: Path) -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="1", price=Decimal("47.00")))
    store.save()
    assert list(tmp_path.iterdir()) == []


def test_store_save_failure_is_reported(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    store = AdStore(path=blocked / "ads.json")
    with pytest.raises(BotError, match="cannot write ad ledger"):
        store.save()


def test_store_load_missing_file_is_empty(tmp_path: Path) -> None:
    store = AdStore.load(tmp_path / "absent.json")
    assert store.items() == ()
    assert store.path == tmp_path / "absent.json"
    assert AdStore.load(None).items() == ()


def test_store_load_of_a_corrupt_file_degrades_to_empty(tmp_path: Path, caplog) -> None:
    path = tmp_path / "ads.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = AdStore.load(path)
    assert store.items() == ()
    assert "is unreadable" in caplog.text


def test_store_load_of_a_malformed_payload_degrades_to_empty(tmp_path: Path, caplog) -> None:
    path = tmp_path / "ads.json"
    path.write_text(json.dumps({"records": "not-a-list"}), encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = AdStore.load(path)
    assert store.items() == ()
    assert "is malformed" in caplog.text


def test_store_load_skips_unreadable_entries(tmp_path: Path, caplog) -> None:
    path = tmp_path / "ads.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {"account_id": "Binance#1", "pair": "UAH/USDT", "adv_no": "1", "price": "47.00"},
                    "junk",
                    {"account_id": "Binance#2", "pair": "UAH/USDC", "price": "nope"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        store = AdStore.load(path)
    assert [record.account_id for record in store.items()] == ["Binance#1"]
    assert "not an object" in caplog.text


def test_store_accepts_a_bare_list_and_an_empty_payload(tmp_path: Path) -> None:
    records = AdStore().as_dict()["records"]
    records.append(
        AdRecord(account_id="Okx#1", pair=UAH_USDT, adv_no="9", price=Decimal("47.00")).to_dict()
    )
    assert AdStore.from_dict(records).get("Okx#1", UAH_USDT).adv_no == "9"

    empty = tmp_path / "empty.json"
    empty.write_text("   ", encoding="utf-8")
    assert AdStore.load(empty).items() == ()


def test_store_rejects_a_non_list_records_block() -> None:
    with pytest.raises(ConfigError, match="must hold a list of records"):
        AdStore(data={"records": 5})


def test_ledger_key_of_a_non_canonical_account_id_is_kept_verbatim() -> None:
    """A hand-edited ledger may hold an id AccountRef cannot parse; it must still resolve."""
    store = AdStore()
    store.put(AdRecord(account_id="legacy-account", pair=UAH_USDT, adv_no="1", price=Decimal("47.00")))
    assert store.get("legacy-account", UAH_USDT).adv_no == "1"
    assert [record.account_id for record in store.items()] == ["legacy-account"]


# -- edit_ad ---------------------------------------------------------------------------
USD_USDT = Pair.parse("USD/USDT")


def _live(**fields) -> OwnAd:
    """A live Binance#1 USD/USDT buy ad at a floating 90% (like the one on the venue)."""
    values = dict(
        platform="binance",
        account_id="Binance#1",
        adv_no="777",
        pair=USD_USDT,
        side="buy",
        status="online",
        price=Decimal("0.9"),
        min_amount=Decimal("50"),
        max_amount=Decimal("15000"),
        quantity=Decimal("49000"),
        total_quantity=Decimal("50000"),
        price_floating_ratio=Decimal("90"),
    )
    values.update(fields)
    return OwnAd(**values)


def _editor(settings, *ads: OwnAd, store=None, **adapter_kwargs):
    adapter = _FakeAdapter("binance", adv_no=None, own_ads=ads or (_live(),), **adapter_kwargs)
    return _publisher(settings, adapters={"binance": adapter}, store=store)


def test_edit_ad_changes_only_the_given_field_of_the_live_ad(settings, tmp_path: Path) -> None:
    store = AdStore(path=tmp_path / "ads.json")
    publisher, adapters, _store = _editor(settings, store=store)

    result = publisher.edit_ad("Binance#1", "USD/USDT", "777", price_floating_ratio="91")

    assert (result.status, result.adv_no) == ("updated", "777")
    spec = adapters["binance"].specs[-1]
    assert spec.side == "buy"  # never forced to sell
    assert spec.price_floating_ratio == Decimal("91")  # stays floating
    assert spec.price == Decimal("0.91")  # informational estimate: 0.9 * 91 / 90
    assert (spec.min_amount, spec.max_amount) == (Decimal("50"), Decimal("15000"))
    assert spec.quantity == Decimal("50000")  # the total amount, not the remaining one
    assert spec.active is True
    assert (spec.payment_methods, spec.payment_ids) == ((), ())  # the venue keeps its methods
    assert adapters["binance"].sent[-1][1] == "update:Binance#1:777"
    # an ad the ledger did not know for this pair is adopted
    assert AdStore.load(tmp_path / "ads.json").get("Binance#1", USD_USDT).adv_no == "777"


def test_edit_ad_overrides_limits_quantity_and_payment_methods(settings) -> None:
    ad = _live(payment_ids=("7110",))
    publisher, adapters, _store = _editor(settings, ad)

    publisher.edit_ad(
        "Binance#1",
        USD_USDT,
        "777",
        min_amount="100",
        max_amount="5000",
        quantity="1000",
        payment_methods=["Wise"],
    )

    spec = adapters["binance"].specs[-1]
    assert (spec.min_amount, spec.max_amount, spec.quantity) == (
        Decimal("100"), Decimal("5000"), Decimal("1000"),
    )
    assert (spec.payment_methods, spec.payment_ids) == (("Wise",), ())
    assert spec.price_floating_ratio == Decimal("90")  # untouched


def test_edit_ad_keeps_the_venue_payment_ids_when_none_are_given(settings) -> None:
    publisher, adapters, _store = _editor(settings, _live(payment_ids=("7110", "12")))

    publisher.edit_ad("Binance#1", USD_USDT, "777", max_amount="14000")

    assert adapters["binance"].specs[-1].payment_ids == ("7110", "12")


def test_edit_ad_with_a_price_makes_a_floating_ad_fixed(settings) -> None:
    publisher, adapters, _store = _editor(settings)

    result = publisher.edit_ad("Binance#1", USD_USDT, "777", price="0.89")

    spec = adapters["binance"].specs[-1]
    assert (spec.price, spec.price_floating_ratio) == (Decimal("0.89"), None)
    assert result.price == Decimal("0.89")


def test_edit_ad_uses_the_ledger_adv_no_and_leaves_a_different_ledger_ad_alone(settings) -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=USD_USDT, adv_no="777", price=Decimal("0.9")))
    other = _live(adv_no="888")
    publisher, adapters, _store = _editor(settings, _live(), other, store=store)

    by_ledger = publisher.edit_ad("Binance#1", USD_USDT, max_amount="14000")
    explicit = publisher.edit_ad("Binance#1", USD_USDT, "888", max_amount="13000")

    assert (by_ledger.adv_no, explicit.adv_no) == ("777", "888")
    assert [request for _account, request in adapters["binance"].sent] == [
        "update:Binance#1:777",
        "update:Binance#1:888",
    ]
    assert store.get("Binance#1", USD_USDT).adv_no == "777"  # 888 is not the ledger's ad


def test_edit_ad_switches_an_ad_off_alone(settings) -> None:
    publisher, adapters, store = _editor(settings)

    result = publisher.edit_ad("Binance#1", USD_USDT, "777", active=False)

    assert result.status == "updated"
    assert adapters["binance"].specs[-1].active is False
    assert store.get("Binance#1", USD_USDT).active is False


def test_edit_ad_brings_an_offline_ad_online_with_its_changes(settings) -> None:
    publisher, adapters, _store = _editor(settings, _live(status="offline"))

    result = publisher.edit_ad("Binance#1", USD_USDT, "777", price_floating_ratio="91", active=True)

    assert result.status == "updated"
    assert adapters["binance"].specs[-1].active is True


def test_edit_ad_sends_the_price_asked_for_there_is_no_cap(settings) -> None:
    publisher, adapters, _store = _editor(settings)

    result = publisher.edit_ad("Binance#1", USD_USDT, "777", price="99.99")

    assert result.price == Decimal("99.99")
    assert adapters["binance"].specs[-1].price == Decimal("99.99")


@pytest.mark.parametrize(
    ("ads", "kwargs", "message"),
    [
        ((), {"price": "0.9"}, "Binance#1 has no advertisement 777"),
        ((_live(pair=Pair.parse("EUR/USDT")),), {"price": "0.9"}, "is EUR/USDT, not USD/USDT"),
        ((_live(status="closed"),), {"price": "0.9"}, "advertisement 777 is closed"),
        ((_live(side=""),), {"price": "0.9"}, "is a side-less ad; only buy ads are handled"),
        ((_live(side="sell"),), {"price": "0.9"}, "is a sell ad; only buy ads are handled"),
        ((_live(side="sell"),), {"active": False}, "is a sell ad; only buy ads are handled"),
        ((_live(),), {}, "nothing to change"),
        ((_live(),), {"active": False, "price": "0.9"}, "active=False only switches the ad off"),
        ((_live(status="offline"),), {"price": "0.9"}, "pass active=True"),
        ((_live(),), {"price": "0.9", "price_floating_ratio": "91"}, "not both"),
        ((_live(),), {"price": "0"}, "price must be positive"),
        ((_live(),), {"price": 0.9}, "floats are rejected"),
        ((_live(),), {"price_floating_ratio": "-1"}, "price_floating_ratio must be positive"),
        ((_live(price_floating_ratio=None),), {"price_floating_ratio": "91"}, "no floating price"),
        ((_live(price=None),), {"max_amount": "100"}, "reports no price"),
        ((_live(min_amount=None),), {"price": "0.9"}, "no order limits"),
        ((_live(total_quantity=None),), {"price": "0.9"}, "pass quantity"),
        ((_live(),), {"min_amount": "0"}, "min_amount must be positive"),
        ((_live(),), {"max_amount": "10"}, "below min_amount"),
        ((_live(),), {"quantity": "0"}, "quantity must be positive"),
    ],
)
def test_edit_ad_refuses_what_it_cannot_do_safely(settings, ads, kwargs, message) -> None:
    adapter = _FakeAdapter("binance", own_ads=ads)
    publisher, _adapters, _store = _publisher(settings, adapters={"binance": adapter})

    result = publisher.edit_ad("Binance#1", USD_USDT, "777", **kwargs)

    assert result.status == "error"
    assert message in result.error
    assert adapter.sent == []


def test_edit_ad_establishes_a_required_session_once_before_the_update(settings) -> None:
    publisher, adapters, _store = _editor(settings, login_request="login-request")

    publisher.edit_ad("Binance#1", USD_USDT, "777", max_amount="14000")

    assert [request for _account, request in adapters["binance"].sent] == [
        "login-request",
        "update:Binance#1:777",
    ]


def test_edit_ad_reports_a_failing_session_without_updating(settings) -> None:
    publisher, adapters, store = _editor(
        settings, login_request="login-request", login_error=TransportError("no session")
    )

    result = publisher.edit_ad("Binance#1", USD_USDT, "777", max_amount="14000")

    assert (result.status, result.error) == ("error", "TransportError: no session")
    assert adapters["binance"].built == []
    assert store.items() == ()


def test_edit_ad_dry_run_reads_but_sends_and_stores_nothing(settings, tmp_path: Path) -> None:
    publisher, adapters, store = _editor(settings, store=AdStore(path=tmp_path / "ads.json"))

    result = publisher.edit_ad("Binance#1", USD_USDT, "777", price_floating_ratio="91", dry_run=True)

    assert (result.status, result.price) == ("dry_run", Decimal("0.91"))
    assert adapters["binance"].sent == []
    assert store.items() == ()
    assert not (tmp_path / "ads.json").exists()


def test_edit_ad_reports_accounts_adapters_ledger_gaps_and_venue_errors(settings) -> None:
    publisher, _adapters, _store = _editor(settings)
    no_adapter, _a, _s = _publisher(settings, adapters={})
    unreadable, _a2, _s2 = _editor(settings, list_error=TransportError("down"))
    failing, _a3, _s3 = _editor(settings, error=TransportError("boom"))

    assert "unknown account" in publisher.edit_ad("Binance#9", USD_USDT, "777", price="0.9").error
    assert "pass adv_no" in publisher.edit_ad("Binance#1", USD_USDT, price="0.9").error
    assert "no adapter registered" in no_adapter.edit_ad("Binance#1", USD_USDT, "777", price="0.9").error
    assert unreadable.edit_ad("Binance#1", USD_USDT, "777", price="0.9").error == (
        "cannot read the advertisement: TransportError: down"
    )
    assert failing.edit_ad("Binance#1", USD_USDT, "777", price="0.9").error == "TransportError: boom"


# -- fetch_own_ads ---------------------------------------------------------------------
class _ListingAdapter:
    """Adapter double answering ``fetch_own_ads`` with fixed ads (or an error)."""

    def __init__(self, platform: str, statuses=("online",), error: BaseException | None = None):
        self.platform = platform
        self.statuses = statuses
        self.error = error
        self.asked: list[str] = []

    def fetch_own_ads(self, account):
        self.asked.append(account.id)
        if self.error is not None:
            raise self.error
        return tuple(
            OwnAd(
                platform=self.platform,
                account_id=account.id,
                adv_no=f"{account.id}-{index}",
                pair=UAH_USDT,
                side=SIDE_BUY,
                status=status,
            )
            for index, status in enumerate(self.statuses)
        )


def test_fetch_own_ads_reads_every_configured_account_in_platform_order(settings) -> None:
    adapters = {
        "binance": _ListingAdapter("binance", statuses=("online", "offline", "closed")),
        "okx": _ListingAdapter("okx", statuses=("offline",)),
        "bybit": _ListingAdapter("bybit", statuses=()),
    }
    publisher, _adapters, store = _publisher(settings, adapters=adapters)

    results = publisher.fetch_own_ads()

    assert [result.account_id for result in results] == ["Binance#1", "Binance#2", "Okx#1", "Bybit#1"]
    assert all(result.ok for result in results)
    binance = results[0]
    assert [(ad.adv_no, ad.status, ad.active) for ad in binance.ads] == [
        ("Binance#1-0", "online", True),
        ("Binance#1-1", "offline", False),
    ]
    assert [ad.status for ad in results[2].ads] == ["offline"]
    assert results[3].ads == ()
    assert store.items() == ()  # reading never touches the ledger
    assert binance.to_dict()["ads"][1] == {
        "platform": "binance",
        "account_id": "Binance#1",
        "adv_no": "Binance#1-1",
        "pair": "UAH/USDT",
        "side": "buy",
        "status": "offline",
        "active": False,
        "price": None,
        "min_amount": None,
        "max_amount": None,
        "quantity": None,
        "payment_methods": [],
        "venue_status": "",
        "total_quantity": None,
        "price_floating_ratio": None,
        "payment_ids": [],
    }


def test_fetch_own_ads_can_include_closed_ads_and_select_accounts(settings) -> None:
    adapters = {"binance": _ListingAdapter("binance", statuses=("online", "closed"))}
    publisher, _adapters, _store = _publisher(settings, adapters=adapters)

    results = publisher.fetch_own_ads(["binance#2"], include_closed=True)

    assert [result.account_id for result in results] == ["Binance#2"]
    assert [ad.status for ad in results[0].ads] == ["online", "closed"]
    assert adapters["binance"].asked == ["Binance#2"]


def test_fetch_own_ads_isolates_a_failing_account(settings) -> None:
    adapters = {
        "binance": _ListingAdapter("binance"),
        "okx": _ListingAdapter("okx", error=TransportError("timed out")),
    }
    publisher, _adapters, _store = _publisher(settings, adapters=adapters)

    results = publisher.fetch_own_ads(["Okx#1", "Binance#1", "Bybit#1", "Binance#9"])

    okx, binance, bybit, unknown = results
    assert not okx.ok and okx.error == "TransportError: timed out" and okx.ads == ()
    assert binance.ok and len(binance.ads) == 1
    assert bybit.error == "no adapter registered for platform 'bybit'"
    assert unknown.platform == "" and "unknown account" in unknown.error
    assert okx.to_dict() == {"account_id": "Okx#1", "platform": "okx", "ads": [], "error": "TransportError: timed out"}


# -- DISABLED_EXCHANGES -------------------------------------------------------------------
@pytest.fixture
def okx_off(env_factory):
    from p2pbot.config import load_settings

    return load_settings(env_path=None, env=env_factory(DISABLED_EXCHANGES=" OKX , "), dotenv=False)


def test_disabled_exchanges_are_parsed_and_validated(okx_off, env_factory) -> None:
    from p2pbot.config import load_settings

    assert okx_off.disabled_platforms == frozenset({"okx"})
    bad = load_settings(env_path=None, env=env_factory(DISABLED_EXCHANGES="okx,kraken"), dotenv=False)
    with pytest.raises(ConfigError, match="DISABLED_EXCHANGES has unknown exchange.s. kraken"):
        bad.disabled_platforms


def test_a_disabled_exchange_is_never_listed(okx_off) -> None:
    adapters = {name: _ListingAdapter(name) for name in ("binance", "okx", "bybit")}
    publisher, _adapters, _store = _publisher(okx_off, adapters=adapters)

    every = publisher.fetch_own_ads()
    named = publisher.fetch_own_ads(["Okx#1", "Bybit#1"])

    assert [result.account_id for result in every] == ["Binance#1", "Binance#2", "Bybit#1"]
    assert [result.account_id for result in named] == ["Bybit#1"]
    assert adapters["okx"].asked == []


def test_a_disabled_exchange_is_never_edited(okx_off) -> None:
    okx = _FakeAdapter("okx", own_ads=(_live(platform="okx", account_id="Okx#1"),))
    publisher, _adapters, _store = _publisher(okx_off, adapters={"okx": okx})

    result = publisher.edit_ad("Okx#1", USD_USDT, "777", price="0.9")

    assert (result.status, result.error) == ("error", "okx is disabled (DISABLED_EXCHANGES)")
    assert okx.sent == []
