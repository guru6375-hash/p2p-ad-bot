"""Base/cap rate store (SPEC section 6).

Rates are the only operator-entered numbers in the system: ``base_rate`` is the anchor
price of a pair and ``cap_rate`` is the hard ceiling an advertisement must never exceed.
Every stored value is a :class:`~decimal.Decimal` quantized to the pair fiat's tick with
``ROUND_HALF_UP``; JSON persistence keeps them as decimal *strings* so no float ever
touches the file. Writes go through :func:`os.replace`, which is atomic on Windows.
"""

from __future__ import annotations

import json
import os
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Mapping

from .constants import DEFAULT_PRICE_TICK, PRICE_TICK
from .errors import ConfigError, RateError
from .models import Pair, parse_decimal

__all__ = ["RateStore", "quantize_rate"]

#: The two rate sections kept per pair.
SECTIONS: tuple[str, ...] = ("base", "cap")


def quantize_rate(value: Decimal, fiat: str) -> Decimal:
    """Quantize ``value`` to the fiat price tick, rounding half up."""
    tick = PRICE_TICK.get(fiat.upper(), DEFAULT_PRICE_TICK)
    return value.quantize(tick, rounding=ROUND_HALF_UP)


class RateStore:
    """In-memory base/cap rates with optional JSON persistence.

    Args:
        data: initial mapping shaped ``{"base": {"UAH/USDT": "47.00"}, "cap": {...}}``.
        path: file used by :meth:`save`; ``None`` disables persistence.
    """

    def __init__(self, data: Mapping[str, dict[str, Any]] | None = None, path: Path | None = None) -> None:
        self.path: Path | None = Path(path) if path is not None else None
        self._rates: dict[str, dict[str, Decimal]] = {section: {} for section in SECTIONS}
        if data is not None:
            self._merge(data)

    # -- mutation ------------------------------------------------------------------
    def set_base(self, pair: Pair | str, rate: Decimal | str | int) -> None:
        """Store ``base_rate`` for ``pair``; the rate must be greater than zero."""
        self._set("base", pair, rate)

    def set_cap(self, pair: Pair | str, rate: Decimal | str | int) -> None:
        """Store the hard ceiling for ``pair``; the cap must be greater than zero."""
        self._set("cap", pair, rate)

    def clear(self, pair: Pair | str) -> None:
        """Forget both rates of ``pair`` (missing entries are not an error)."""
        symbol = self._pair(pair).symbol
        for section in SECTIONS:
            self._rates[section].pop(symbol, None)

    # -- lookup --------------------------------------------------------------------
    def base(self, pair: Pair | str) -> Decimal | None:
        """The stored ``base_rate`` for ``pair``, or ``None``."""
        return self._rates["base"].get(self._pair(pair).symbol)

    def cap(self, pair: Pair | str) -> Decimal | None:
        """The stored ``cap_rate`` for ``pair``, or ``None``."""
        return self._rates["cap"].get(self._pair(pair).symbol)

    def pairs(self) -> tuple[str, ...]:
        """Every pair symbol that has a base or a cap, sorted."""
        known = set(self._rates["base"]) | set(self._rates["cap"])
        return tuple(sorted(known))

    # -- serialization -------------------------------------------------------------
    def as_dict(self) -> dict[str, dict[str, str]]:
        """JSON-ready mapping with decimal strings, sections and pairs sorted."""
        return {
            section: {symbol: str(self._rates[section][symbol]) for symbol in sorted(self._rates[section])}
            for section in SECTIONS
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, dict[str, Any]]) -> "RateStore":
        """Build a store from an :meth:`as_dict` shaped mapping."""
        return cls(data=data)

    def save(self) -> None:
        """Write the store atomically; a store without a path is a no-op."""
        if self.path is None:
            return
        target = Path(self.path)
        payload = json.dumps(self.as_dict(), indent=2) + "\n"
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            raise RateError(f"cannot write rate file {target}: {exc}") from exc
        finally:
            if temporary.exists():  # pragma: no cover - only reached after a failed write
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @classmethod
    def load(cls, path: Path | None) -> "RateStore":
        """Read ``path``; a missing or empty file yields an empty store bound to it."""
        store = cls(path=path)
        if path is None:
            return store
        file_path = Path(path)
        try:
            text = file_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return store
        except OSError as exc:
            raise RateError(f"cannot read rate file {file_path}: {exc}") from exc
        if not text.strip():
            return store
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RateError(f"rate file {file_path} is not valid JSON: {exc}") from exc
        store._merge(data)
        return store

    # -- internals -----------------------------------------------------------------
    def _set(self, section: str, pair: Pair | str, rate: Decimal | str | int) -> None:
        target = self._pair(pair)
        self._rates[section][target.symbol] = self._rate(target, rate, section)

    def _merge(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise RateError(f"rate data must be a JSON object, got {type(data).__name__}")
        unknown = sorted(set(data) - set(SECTIONS))
        if unknown:
            raise RateError(f"unknown rate section(s): {', '.join(unknown)}")
        for section in SECTIONS:
            entries = data.get(section)
            if entries is None:
                continue
            if not isinstance(entries, Mapping):
                raise RateError(f"rate section {section!r} must be a JSON object")
            for symbol, value in entries.items():
                pair = self._pair(symbol)
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                self._rates[section][pair.symbol] = self._rate(pair, value, section)

    @staticmethod
    def _pair(pair: Pair | str) -> Pair:
        try:
            return Pair.parse(pair)
        except ConfigError as exc:
            raise RateError(f"invalid pair: {exc}") from exc

    @staticmethod
    def _rate(pair: Pair, rate: Decimal | str | int, section: str) -> Decimal:
        try:
            value = parse_decimal(rate, f"{section}_rate")
        except ConfigError as exc:
            raise RateError(f"{section} rate for {pair.symbol} is not a usable number: {exc}") from exc
        if value <= 0:
            raise RateError(f"{section} rate for {pair.symbol} must be > 0, got {value}")
        return quantize_rate(value, pair.fiat)
