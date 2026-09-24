"""Publisher: cap re-assertion, create/update, dry-run, ledger persistence (SPEC 10)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot.blueprint import parse_blueprint
from p2pbot.constants import SIDE_SELL
from p2pbot.errors import BotError, ConfigError, TransportError
from p2pbot.models import AdActionResult, AdRecord, AdSpec, ComputedAd, Pair, PublishResult
from p2pbot.publisher import AdPublisher, AdStore, build_ad_spec
from p2pbot.rates import RateStore

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
    ) -> None:
        self.platform = platform
        self.adv_no = adv_no
        self.error = error
        self.login_request = login_request
        self.login_error = login_error
        self.built: list[tuple[str, str, Decimal, bool]] = []
        self.sent: list[tuple[str, object]] = []
        self.logins: list[str] = []

    # -- builder hooks --------------------------------------------------------------
    def build_login_request(self, account):
        self.logins.append(account.id)
        return self.login_request

    def build_create_ad_request(self, account, spec, adv_no=None):
        self.built.append(("create", account.id, spec.price, spec.active))
        return f"create:{account.id}"

    def build_update_ad_request(self, account, spec, adv_no):
        self.built.append(("update", account.id, spec.price, spec.active))
        return f"update:{account.id}:{adv_no}"

    # -- execution hooks -------------------------------------------------------------
    def send_private(self, account, request):
        self.sent.append((account.id, request))
        if request == self.login_request and self.login_error is not None:
            raise self.login_error
        if self.error is not None:
            raise self.error
        return {"raw": True}

    def parse_ad_result(self, payload, *, account, pair, spec, created):
        return AdActionResult(
            platform=self.platform,
            account_id=account.id,
            pair=pair,
            adv_no=self.adv_no,
            price=spec.price,
            created=created,
            raw=payload,
        )


def _computed(
    *,
    pair: str = "UAH/USDT",
    platform: str = "binance",
    price: str = "47.00",
    cap: str = "47.20",
    accounts: tuple[str, ...] = ("Binance#1",),
) -> ComputedAd:
    return ComputedAd(
        pair=Pair.parse(pair),
        platform=platform,
        price=Decimal(price),
        source="base_rate",
        cap=Decimal(cap),
        accounts=accounts,
    )


def _publisher(
    settings,
    *,
    adapters=None,
    store=None,
    rates=None,
    dry_run=False,
    plans=None,
) -> tuple[AdPublisher, dict[str, _FakeAdapter], AdStore, RateStore]:
    resolved = adapters if adapters is not None else {"binance": _FakeAdapter("binance")}
    ad_store = store if store is not None else AdStore()
    rate_store = rates if rates is not None else RateStore()
    publisher = AdPublisher(resolved, settings, ad_store, rate_store, dry_run, plans=plans)
    return publisher, resolved, ad_store, rate_store


# -- cap re-assertion ------------------------------------------------------------------
def test_cap_is_re_asserted_before_the_request_is_built(settings) -> None:
    rates = RateStore()
    rates.set_cap("UAH/USDT", "46.50")
    publisher, adapters, store, _rates = _publisher(settings, rates=rates)

    results = publisher.publish([_computed(price="47.00", cap="47.20")])

    assert adapters["binance"].built == [("create", "Binance#1", Decimal("46.50"), True)]
    assert results[0].price == Decimal("46.50")
    assert store.get("Binance#1", UAH_USDT).price == Decimal("46.50")


@pytest.mark.parametrize("price", ["47.00", "46.51", "60.00"])
def test_no_published_price_ever_exceeds_the_stored_cap(settings, price: str) -> None:
    rates = RateStore()
    rates.set_cap("UAH/USDT", "46.50")
    publisher, adapters, _store, _rates = _publisher(settings, rates=rates)

    results = publisher.publish([_computed(price=price)])

    for result in results:
        assert result.price is not None
        assert result.price <= Decimal("46.50")
    for _kind, _account, sent_price, _active in adapters["binance"].built:
        assert sent_price <= Decimal("46.50")


def test_computed_cap_is_the_fallback_when_the_store_has_none(settings) -> None:
    publisher, adapters, _store, _rates = _publisher(settings, rates=RateStore())
    publisher.publish([_computed(price="47.00", cap="46.40")])
    assert adapters["binance"].built[0][2] == Decimal("46.40")


def test_a_price_below_the_cap_is_untouched(settings) -> None:
    rates = RateStore()
    rates.set_cap("UAH/USDT", "47.20")
    publisher, adapters, _store, _rates = _publisher(settings, rates=rates)
    publisher.publish([_computed(price="47.00")])
    assert adapters["binance"].built[0][2] == Decimal("47.00")


# -- dry run ---------------------------------------------------------------------------
def test_dry_run_builds_no_request_and_writes_nothing(settings, tmp_path: Path) -> None:
    ledger = tmp_path / "ads.json"
    publisher, adapters, store, _rates = _publisher(settings, store=AdStore(path=ledger))

    results = publisher.publish([_computed()], dry_run=True)

    assert adapters["binance"].built == []
    assert adapters["binance"].sent == []
    assert [result.status for result in results] == ["dry_run"]
    assert results[0].dry_run is True
    assert results[0].price == Decimal("47.00")
    assert store.items() == ()
    assert not ledger.exists()


def test_constructor_dry_run_default_is_used_when_the_call_does_not_decide(settings) -> None:
    publisher, adapters, _store, _rates = _publisher(settings, dry_run=True)
    results = publisher.publish([_computed()])
    assert results[0].dry_run is True
    assert adapters["binance"].built == []


def test_call_level_dry_run_false_overrides_the_constructor(settings) -> None:
    publisher, adapters, _store, _rates = _publisher(settings, dry_run=True)
    results = publisher.publish([_computed()], dry_run=False)
    assert results[0].status == "created"
    assert len(adapters["binance"].built) == 1


def test_dry_run_reports_an_adv_no_only_when_one_is_remembered(settings) -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="77", price=Decimal("47.00")))
    publisher, _adapters, _store, _rates = _publisher(settings, store=store)
    results = publisher.publish([_computed()], dry_run=True)
    assert results[0].adv_no == "77"


# -- create vs update ------------------------------------------------------------------
def test_first_publish_creates_and_remembers_the_advertisement(settings, tmp_path: Path) -> None:
    ledger = tmp_path / "ads.json"
    store = AdStore(path=ledger)
    publisher, adapters, _store, _rates = _publisher(settings, store=store)

    results = publisher.publish([_computed()])

    assert results[0].status == "created"
    assert results[0].adv_no == "2048"
    assert [entry[0] for entry in adapters["binance"].built] == ["create"]
    record = store.get("Binance#1", UAH_USDT)
    assert record.adv_no == "2048"
    assert record.price == Decimal("47.00")
    assert record.active is True
    assert record.updated_at is not None
    assert ledger.is_file()

    reloaded = AdStore.load(ledger)
    assert reloaded.get("Binance#1", "uah/usdt") == record
    assert reloaded.as_dict() == store.as_dict()


def test_second_publish_updates_the_remembered_advertisement(settings) -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="2048", price=Decimal("47.00")))
    publisher, adapters, _store, _rates = _publisher(settings, store=store)

    results = publisher.publish([_computed(price="47.10")])

    assert results[0].status == "updated"
    assert results[0].adv_no == "2048"
    assert [entry[0] for entry in adapters["binance"].built] == ["update"]
    assert adapters["binance"].sent[-1][1] == "update:Binance#1:2048"
    assert store.get("Binance#1", UAH_USDT).price == Decimal("47.10")


def test_update_keeps_the_previous_adv_no_when_the_venue_omits_it(settings) -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="2048", price=Decimal("47.00")))
    adapters = {"binance": _FakeAdapter("binance", adv_no=None)}
    publisher, _adapters, _store, _rates = _publisher(settings, adapters=adapters, store=store)

    results = publisher.publish([_computed()])
    assert results[0].adv_no == "2048"
    assert store.get("Binance#1", UAH_USDT).adv_no == "2048"


def test_create_missing_false_skips_an_unknown_account(settings) -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDC, adv_no="555", price=Decimal("46.75")))
    publisher, adapters, _store, _rates = _publisher(settings, store=store)

    results = publisher.publish([_computed()], create_missing=False)

    assert results[0].status == "skipped"
    assert results[0].price == Decimal("47.00")
    assert adapters["binance"].built == []


def test_create_missing_false_still_updates_a_known_advertisement(settings) -> None:
    store = AdStore()
    store.put(AdRecord(account_id="Binance#1", pair=UAH_USDT, adv_no="2048", price=Decimal("47.00")))
    publisher, adapters, _store, _rates = _publisher(settings, store=store)
    results = publisher.publish([_computed()], create_missing=False)
    assert results[0].status == "updated"
    assert [entry[0] for entry in adapters["binance"].built] == ["update"]


def test_active_flag_is_forwarded_to_the_venue(settings) -> None:
    publisher, adapters, _store, _rates = _publisher(settings)
    results = publisher.publish([_computed()], active=False)
    assert adapters["binance"].built[0][3] is False
    assert results[0].status == "created"


# -- per-account isolation -------------------------------------------------------------
def test_one_failing_account_does_not_stop_the_others(settings) -> None:
    class _FailingOneAccount(_FakeAdapter):
        def send_private(self, account, request):
            if account.id == "Binance#1":
                raise TransportError("connection reset by peer")
            return super().send_private(account, request)

    adapters = {"binance": _FailingOneAccount("binance")}
    publisher, _adapters, store, _rates = _publisher(settings, adapters=adapters)

    results = publisher.publish([_computed(accounts=("Binance#1", "Binance#2"))])

    assert [result.account_id for result in results] == ["Binance#1", "Binance#2"]
    assert results[0].status == "error"
    assert results[0].error == "TransportError: connection reset by peer"
    assert results[1].status == "created"
    assert store.get("Binance#2", UAH_USDT) is not None
    assert store.get("Binance#1", UAH_USDT) is None


def test_unknown_account_is_reported_without_a_request(settings) -> None:
    publisher, adapters, _store, _rates = _publisher(settings)
    results = publisher.publish([_computed(accounts=("Kraken#1",))])
    assert results[0].status == "error"
    assert "Kraken#1" in results[0].error
    assert adapters["binance"].built == []


def test_missing_adapter_is_reported(settings) -> None:
    publisher, _adapters, _store, _rates = _publisher(settings, adapters={})
    results = publisher.publish([_computed()])
    assert results[0].status == "error"
    assert results[0].error == "no adapter registered for platform 'binance'"


def test_duplicate_accounts_are_published_once(settings) -> None:
    publisher, adapters, _store, _rates = _publisher(settings)
    results = publisher.publish([_computed(accounts=("Binance#1", "Binance#1"))])
    assert len(results) == 1
    assert len(adapters["binance"].built) == 1


def test_an_error_result_never_carries_a_price_above_the_cap(settings) -> None:
    rates = RateStore()
    rates.set_cap("UAH/USDT", "46.50")
    adapters = {"binance": _FakeAdapter("binance", error=TransportError("boom"))}
    publisher, _adapters, _store, _rates = _publisher(settings, adapters=adapters, rates=rates)
    results = publisher.publish([_computed(price="47.00")])
    assert results[0].status == "error"
    assert results[0].price == Decimal("46.50")


# -- sessions --------------------------------------------------------------------------
def test_venues_without_a_session_need_no_extra_call(settings) -> None:
    """All three shipped venues return ``None`` from build_login_request."""
    adapters = {
        name: _FakeAdapter(name) for name in ("binance", "okx", "bybit")
    }
    publisher, _adapters, _store, _rates = _publisher(settings, adapters=adapters)
    publisher.publish(
        [
            _computed(platform="binance", accounts=("Binance#1",)),
            _computed(platform="okx", accounts=("Okx#1",)),
            _computed(platform="bybit", accounts=("Bybit#1",)),
        ]
    )
    for name in ("binance", "okx", "bybit"):
        assert adapters[name].logins != []
        assert len(adapters[name].sent) == 1  # only the create request


def test_a_required_session_is_established_once_per_account(settings) -> None:
    adapter = _FakeAdapter("binance", login_request="login-request")
    publisher, _adapters, _store, _rates = _publisher(settings, adapters={"binance": adapter})

    publisher.publish(
        [
            _computed(platform="binance", accounts=("Binance#1",)),
            _computed(pair="UAH/USDC", platform="binance", accounts=("Binance#1",)),
        ]
    )
    assert adapter.sent[0] == ("Binance#1", "login-request")
    assert sum(1 for _account, request in adapter.sent if request == "login-request") == 1
    assert len(adapter.built) == 2


def test_a_failing_session_reports_the_account_and_stops_retrying(settings) -> None:
    adapter = _FakeAdapter(
        "binance", login_request="login-request", login_error=TransportError("no session")
    )
    publisher, _adapters, store, _rates = _publisher(settings, adapters={"binance": adapter})

    results = publisher.publish(
        [
            _computed(pair="UAH/USDT", accounts=("Binance#1",)),
            _computed(pair="UAH/USDC", accounts=("Binance#1",)),
        ]
    )
    assert [result.status for result in results] == ["error", "error"]
    assert results[0].error == "TransportError: no session"
    assert results[1].error == "TransportError: no session"
    assert adapter.built == []
    assert store.items() == ()


# -- plan wiring -----------------------------------------------------------------------
def test_publish_without_a_plan_uses_default_amounts(settings, caplog) -> None:
    publisher, _adapters, _store, _rates = _publisher(settings)
    assert publisher.plan_for("UAH/USDT") is None
    with caplog.at_level("WARNING"):
        publisher.publish([_computed()])
    assert "no pair plan for UAH/USDT" in caplog.text


def test_set_blueprint_supplies_the_publish_plan(uah_blueprint, settings) -> None:
    publisher, adapters, _store, _rates = _publisher(settings)
    publisher.set_blueprint(uah_blueprint)
    plan = publisher.plan_for("uah/usdt")
    assert plan is not None
    assert plan.pair == UAH_USDT
    publisher.publish([_computed()])
    assert adapters["binance"].built != []


def test_plan_for_reports_an_unknown_pair(uah_blueprint, settings) -> None:
    publisher, _adapters, _store, _rates = _publisher(settings, plans=uah_blueprint)
    with pytest.raises(ConfigError, match="has no pair UAH/TRY"):
        publisher.plan_for("UAH/TRY")


# -- build_ad_spec ---------------------------------------------------------------------
def test_build_ad_spec_uses_the_blueprint_plan(uah_blueprint) -> None:
    spec = build_ad_spec(_computed(), uah_blueprint)
    assert spec.pair == UAH_USDT
    assert spec.price == Decimal("47.00")
    assert spec.min_amount == Decimal("1000")
    assert spec.max_amount == Decimal("200000")
    assert spec.payment_methods == ("Monobank", "PrivatBank")
    assert spec.active is True
    assert spec.side == SIDE_SELL


def test_build_ad_spec_accepts_a_single_plan_and_overrides(uah_blueprint) -> None:
    plan = uah_blueprint.pair("UAH/USDT")
    spec = build_ad_spec(_computed(), plan, price=Decimal("46.50"), active=False)
    assert spec.price == Decimal("46.50")
    assert spec.active is False


def test_build_ad_spec_without_a_plan_uses_blueprint_defaults() -> None:
    spec = build_ad_spec(_computed())
    assert spec.min_amount == Decimal("1")
    assert spec.max_amount == Decimal("1000000")
    assert spec.payment_methods == ()
    assert spec.active is True


def test_build_ad_spec_rejects_a_mismatched_plan(uah_blueprint) -> None:
    plan = uah_blueprint.pair("UAH/USDC")
    with pytest.raises(ConfigError, match="pair plan is for UAH/USDC, not UAH/USDT"):
        build_ad_spec(_computed(), plan)


def test_build_ad_spec_rejects_a_blueprint_without_the_pair() -> None:
    other = parse_blueprint(
        {
            "version": 1,
            "name": "mini",
            "fiat": "UAH",
            "strategy": "fixed_spread",
            "pairs": [{"pair": "UAH/USDT", "anchor": True, "accounts": ["Binance#1"]}],
        }
    )
    with pytest.raises(ConfigError, match="has no pair UAH/USDC"):
        build_ad_spec(_computed(pair="UAH/USDC"), other)


def test_build_ad_spec_keeps_a_disabled_plan_inactive() -> None:
    blueprint = parse_blueprint(
        {
            "version": 1,
            "name": "off",
            "fiat": "UAH",
            "strategy": "fixed_spread",
            "pairs": [
                {"pair": "UAH/USDT", "anchor": True, "enabled": False, "accounts": ["Binance#1"]}
            ],
        }
    )
    assert build_ad_spec(_computed(), blueprint).active is False
    assert build_ad_spec(_computed(), blueprint, active=True).active is True


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


def test_publish_result_is_the_documented_shape(settings) -> None:
    publisher, _adapters, _store, _rates = _publisher(settings)
    result = publisher.publish([_computed()])[0]
    assert isinstance(result, PublishResult)
    assert result.to_dict()["status"] == "created"
    assert result.platform == "binance"
    assert result.ok is True


def test_spec_is_built_from_the_plan_for_the_venue(settings, uah_blueprint) -> None:
    """The venue receives the plan's amounts/payment methods, not defaults."""
    captured: list[AdSpec] = []

    class _CapturingAdapter(_FakeAdapter):
        def build_create_ad_request(self, account, spec, adv_no=None):
            captured.append(spec)
            return super().build_create_ad_request(account, spec, adv_no)

    publisher, _adapters, _store, _rates = _publisher(
        settings, adapters={"binance": _CapturingAdapter("binance")}, plans=uah_blueprint
    )
    publisher.publish([_computed()])
    assert captured[0].min_amount == Decimal("1000")
    assert captured[0].payment_methods == ("Monobank", "PrivatBank")


def test_ledger_key_of_a_non_canonical_account_id_is_kept_verbatim() -> None:
    """A hand-edited ledger may hold an id AccountRef cannot parse; it must still resolve."""
    store = AdStore()
    store.put(AdRecord(account_id="legacy-account", pair=UAH_USDT, adv_no="1", price=Decimal("47.00")))
    assert store.get("legacy-account", UAH_USDT).adv_no == "1"
    assert [record.account_id for record in store.items()] == ["legacy-account"]
