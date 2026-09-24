"""RateStore: Decimal-only storage, quantization and atomic persistence (SPEC section 6)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot.errors import RateError
from p2pbot.models import Pair
from p2pbot.rates import RateStore, quantize_rate


def test_set_and_get_base_and_cap() -> None:
    store = RateStore()
    store.set_base("UAH/USDT", "47.00")
    store.set_cap(Pair.parse("UAH/USDT"), Decimal("47.20"))
    assert store.base("UAH/USDT") == Decimal("47.00")
    assert store.cap("uah/usdt") == Decimal("47.20")
    assert isinstance(store.base("UAH/USDT"), Decimal)
    assert store.pairs() == ("UAH/USDT",)
    assert store.base("UAH/USDC") is None
    assert store.cap("UAH/USDC") is None


def test_values_are_quantized_to_the_fiat_tick_half_up() -> None:
    store = RateStore()
    store.set_base("UAH/USDT", "47.005")
    store.set_cap("PLN/USDT", "4.314")
    assert store.base("UAH/USDT") == Decimal("47.01")
    assert store.cap("PLN/USDT") == Decimal("4.31")


def test_quantize_rate_falls_back_for_unknown_or_lowercase_fiats() -> None:
    assert quantize_rate(Decimal("1.005"), "uah") == Decimal("1.01")
    assert quantize_rate(Decimal("1.005"), "XYZ") == Decimal("1.01")


@pytest.mark.parametrize("bad", ["0", "0.00", "-1", Decimal("-0.01"), 0])
def test_non_positive_rates_are_rejected(bad: object) -> None:
    store = RateStore()
    with pytest.raises(RateError, match=r"must be > 0"):
        store.set_base("UAH/USDT", bad)  # type: ignore[arg-type]
    with pytest.raises(RateError, match=r"must be > 0"):
        store.set_cap("UAH/USDT", bad)  # type: ignore[arg-type]
    assert store.pairs() == ()


@pytest.mark.parametrize("bad", ["abc", "", "  ", 47.0, True, None])
def test_unusable_rate_values_are_rejected(bad: object) -> None:
    store = RateStore()
    with pytest.raises(RateError) as excinfo:
        store.set_base("UAH/USDT", bad)  # type: ignore[arg-type]
    assert "UAH/USDT" in str(excinfo.value)


def test_invalid_pair_is_reported_as_a_rate_error() -> None:
    store = RateStore()
    with pytest.raises(RateError, match="invalid pair"):
        store.set_base("UAHUSDT", "47")


def test_clear_removes_both_rates_and_is_idempotent() -> None:
    store = RateStore()
    store.set_base("UAH/USDT", "47")
    store.set_cap("UAH/USDT", "48")
    store.clear("UAH/USDT")
    assert (store.base("UAH/USDT"), store.cap("UAH/USDT")) == (None, None)
    store.clear("UAH/USDT")  # no error


def test_as_dict_uses_decimal_strings_and_sorts() -> None:
    store = RateStore()
    store.set_cap("UAH/USDC", "46.90")
    store.set_base("UAH/USDT", "47.00")
    store.set_base("UAH/USDC", "46.80")
    assert store.as_dict() == {
        "base": {"UAH/USDC": "46.80", "UAH/USDT": "47.00"},
        "cap": {"UAH/USDC": "46.90"},
    }


def test_from_dict_round_trip() -> None:
    original = RateStore()
    original.set_base("UAH/USDT", "47.00")
    original.set_cap("UAH/USDT", "47.20")
    restored = RateStore.from_dict(original.as_dict())
    assert restored.as_dict() == original.as_dict()
    assert restored.path is None


def test_constructor_accepts_the_persisted_shape_and_skips_empty_values() -> None:
    store = RateStore({"base": {"UAH/USDT": "47.00", "UAH/USDC": None, "UAH/TRY": ""}, "cap": {}})
    assert store.base("UAH/USDT") == Decimal("47.00")
    assert store.pairs() == ("UAH/USDT",)


def test_constructor_rejects_malformed_payloads() -> None:
    with pytest.raises(RateError, match="must be a JSON object"):
        RateStore(["base"])  # type: ignore[arg-type]
    with pytest.raises(RateError, match="unknown rate section"):
        RateStore({"bases": {}})
    with pytest.raises(RateError, match="rate section 'base' must be a JSON object"):
        RateStore({"base": "47"})
    with pytest.raises(RateError, match="invalid pair"):
        RateStore({"base": {"NOPE": "47"}})
    with pytest.raises(RateError, match="not a usable number"):
        RateStore({"base": {"UAH/USDT": 47.0}})


# -- persistence -----------------------------------------------------------------------
def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = RateStore(path=path)
    store.set_base("UAH/USDT", "47.00")
    store.set_cap("UAH/USDT", "47.20")
    store.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"base": {"UAH/USDT": "47.00"}, "cap": {"UAH/USDT": "47.20"}}
    assert '"47.00"' in path.read_text(encoding="utf-8")  # decimal string, never a float

    reloaded = RateStore.load(path)
    assert reloaded.as_dict() == store.as_dict()
    assert reloaded.path == path
    assert list(tmp_path.glob("**/*.tmp")) == []


def test_loaded_store_can_be_rewritten_in_place(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    RateStore(path=path).save()
    reloaded = RateStore.load(path)
    reloaded.set_base("PLN/USDT", "4.31")
    reloaded.save()
    assert RateStore.load(path).base("PLN/USDT") == Decimal("4.31")


def test_save_without_a_path_is_a_no_op(tmp_path: Path) -> None:
    store = RateStore()
    store.set_base("UAH/USDT", "47")
    store.save()
    assert list(tmp_path.iterdir()) == []


def test_save_reports_a_write_failure(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    store = RateStore(path=blocked / "state.json")
    store.set_base("UAH/USDT", "47")
    with pytest.raises(RateError, match="cannot write rate file"):
        store.save()


def test_load_missing_and_empty_files_yield_empty_stores(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    store = RateStore.load(missing)
    assert store.pairs() == ()
    assert store.path == missing

    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert RateStore.load(empty).pairs() == ()

    assert RateStore.load(None).pairs() == ()
    assert RateStore.load(None).path is None


def test_load_rejects_corrupt_and_malformed_files(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    with pytest.raises(RateError, match="is not valid JSON"):
        RateStore.load(broken)

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(RateError, match="rate data must be a JSON object"):
        RateStore.load(wrong_shape)

    bad_rate = tmp_path / "bad.json"
    bad_rate.write_text('{"base": {"UAH/USDT": "abc"}}', encoding="utf-8")
    with pytest.raises(RateError, match="UAH/USDT"):
        RateStore.load(bad_rate)


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = RateStore(path=path)
    store.set_base("UAH/USDT", "47.00")
    store.save()
    before = path.read_text(encoding="utf-8")

    store.set_base("UAH/USDT", "48.00")
    store.save()
    after = RateStore.load(path).base("UAH/USDT")
    assert after == Decimal("48.00")
    assert before != path.read_text(encoding="utf-8")
