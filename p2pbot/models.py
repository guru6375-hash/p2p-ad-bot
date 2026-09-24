"""Domain models shared by every layer of the bot.

Everything that represents money is :class:`decimal.Decimal`; floats are rejected at the
boundary by :func:`parse_decimal` so a binary float can never leak into a price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ConfigError

_PAIR_RE = re.compile(r"^([A-Za-z]{2,10})/([A-Za-z]{2,10})$")
_ACCOUNT_RE = re.compile(r"^([A-Za-z]+)#(\d+)$")


def utcnow() -> datetime:
    """Timezone-aware UTC now (single clock source for the whole package)."""
    return datetime.now(timezone.utc)


def parse_decimal(value: Any, field_name: str = "value") -> Decimal:
    """Convert ``value`` to :class:`Decimal`, rejecting binary floats.

    Accepts ``Decimal``, ``int``, ``str`` (including whitespace-padded and ``+``-prefixed)
    and refuses ``bool`` and ``float`` so no silent precision loss is possible.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a decimal value, got a boolean")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ConfigError(f"{field_name} must not be empty")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ConfigError(f"{field_name} is not a valid decimal number: {value!r}") from exc
    else:
        raise ConfigError(
            f"{field_name} must be a decimal string (got {type(value).__name__}); "
            "floats are rejected to protect price precision"
        )
    if not result.is_finite():
        raise ConfigError(f"{field_name} must be a finite number, got {value!r}")
    return result


@dataclass(frozen=True, order=True)
class Pair:
    """A fiat/crypto trading pair such as ``UAH/USDT`` (fiat first)."""

    fiat: str
    crypto: str

    def __post_init__(self) -> None:
        for part, label in ((self.fiat, "fiat"), (self.crypto, "crypto")):
            if not isinstance(part, str) or not re.fullmatch(r"[A-Za-z]{2,10}", part):
                raise ConfigError(f"invalid {label} code: {part!r}")
        object.__setattr__(self, "fiat", self.fiat.upper())
        object.__setattr__(self, "crypto", self.crypto.upper())

    @property
    def symbol(self) -> str:
        return f"{self.fiat}/{self.crypto}"

    @classmethod
    def parse(cls, value: "Pair | str") -> "Pair":
        if isinstance(value, Pair):
            return value
        if not isinstance(value, str):
            raise ConfigError(f"pair must be a string like UAH/USDT, got {type(value).__name__}")
        match = _PAIR_RE.match(value.strip())
        if not match:
            raise ConfigError(f"invalid pair {value!r}; expected FIAT/CRYPTO, e.g. UAH/USDT")
        return cls(fiat=match.group(1), crypto=match.group(2))

    def __str__(self) -> str:
        return self.symbol


@dataclass(frozen=True)
class AccountRef:
    """A reference to one account of one platform, e.g. ``Binance#2``."""

    platform: str
    index: int

    def __post_init__(self) -> None:
        if not isinstance(self.platform, str) or not self.platform:
            raise ConfigError("account platform must be a non-empty string")
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index < 1:
            raise ConfigError(f"account index must be a positive integer, got {self.index!r}")
        object.__setattr__(self, "platform", self.platform.lower())

    @property
    def id(self) -> str:
        return f"{self.platform.capitalize()}#{self.index}"

    @classmethod
    def parse(cls, value: "AccountRef | str") -> "AccountRef":
        if isinstance(value, AccountRef):
            return value
        if not isinstance(value, str):
            raise ConfigError(f"account id must be a string like Binance#1, got {type(value).__name__}")
        match = _ACCOUNT_RE.match(value.strip())
        if not match:
            raise ConfigError(f"invalid account id {value!r}; expected <Platform>#<n>, e.g. Binance#1")
        return cls(platform=match.group(1), index=int(match.group(2)))

    def __str__(self) -> str:
        return self.id


@dataclass(frozen=True)
class Account:
    """A platform account plus its credentials from ``.env``."""

    ref: AccountRef
    credentials: Mapping[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.ref.id

    @property
    def platform(self) -> str:
        return self.ref.platform

    def credential(self, name: str) -> str | None:
        """Case-insensitive credential lookup; ``None`` when unset or blank."""
        wanted = name.upper()
        value = self.credentials.get(wanted)
        if value is None:
            for key, candidate in self.credentials.items():
                if key.upper() == wanted:
                    value = candidate
                    break
        if value is None:
            return None
        text = value.strip()
        return text or None

    def require(self, name: str) -> str:
        """Like :meth:`credential` but raises :class:`ConfigError` when missing."""
        value = self.credential(name)
        if not value:
            raise ConfigError(f"account {self.id} is missing required credential {name}")
        return value

    def redacted(self) -> dict[str, str]:
        from .constants import REDACTED, SECRET_FIELDS

        return {
            key: (REDACTED if key.upper() in SECRET_FIELDS else value)
            for key, value in self.credentials.items()
        }


@dataclass(frozen=True)
class Filters:
    """Advertiser filter for competitor data.

    ``user_type`` ``None`` disables the merchant check; numeric thresholds are ``None``
    when the venue does not publish that metric. Comparisons are strict (``>``).
    """

    user_type: str | None = "merchant"
    min_month_order_count: Decimal | None = None
    min_positive_rate: Decimal | None = None
    min_month_finish_rate: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.user_type is not None:
            payload["user_type"] = self.user_type
        for key in ("min_month_order_count", "min_positive_rate", "min_month_finish_rate"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = str(value)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, *, base: "Filters | None" = None) -> "Filters":
        if data is None:
            return base if base is not None else cls()
        if not isinstance(data, Mapping):
            raise ConfigError("filters must be a JSON object")
        allowed = {
            "user_type",
            "min_month_order_count",
            "min_positive_rate",
            "min_month_finish_rate",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"unknown filter keys: {', '.join(sorted(unknown))}")
        origin = base if base is not None else cls()
        user_type = data.get("user_type", origin.user_type)
        if user_type is not None:
            user_type = str(user_type).lower()
        return cls(
            user_type=user_type,
            min_month_order_count=(
                parse_decimal(data["min_month_order_count"], "filters.min_month_order_count")
                if "min_month_order_count" in data
                else origin.min_month_order_count
            ),
            min_positive_rate=(
                parse_decimal(data["min_positive_rate"], "filters.min_positive_rate")
                if "min_positive_rate" in data
                else origin.min_positive_rate
            ),
            min_month_finish_rate=(
                parse_decimal(data["min_month_finish_rate"], "filters.min_month_finish_rate")
                if "min_month_finish_rate" in data
                else origin.min_month_finish_rate
            ),
        )


@dataclass(frozen=True)
class CompetitorAd:
    """A rival advertisement, normalized across venues.

    Rate metrics are normalized to the ``0..1`` scale (``97.5 %`` -> ``0.975``) by the
    adapters, so filters can be compared without venue-specific knowledge.
    """

    platform: str
    pair: Pair
    price: Decimal
    advertiser: str = ""
    user_type: str = ""
    month_order_count: Decimal | None = None
    positive_rate: Decimal | None = None
    month_finish_rate: Decimal | None = None
    adv_no: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "pair": self.pair.symbol,
            "price": str(self.price),
            "advertiser": self.advertiser,
            "user_type": self.user_type,
            "month_order_count": _decimal_to_str(self.month_order_count),
            "positive_rate": _decimal_to_str(self.positive_rate),
            "month_finish_rate": _decimal_to_str(self.month_finish_rate),
            "adv_no": self.adv_no,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompetitorAd":
        return cls(
            platform=str(data["platform"]),
            pair=Pair.parse(str(data["pair"])),
            price=parse_decimal(data["price"], "price"),
            advertiser=str(data.get("advertiser", "")),
            user_type=str(data.get("user_type", "")),
            month_order_count=_optional_decimal(data.get("month_order_count")),
            positive_rate=_optional_decimal(data.get("positive_rate")),
            month_finish_rate=_optional_decimal(data.get("month_finish_rate")),
            adv_no=data.get("adv_no"),
        )


@dataclass(frozen=True)
class MarketSnapshot:
    """Competitor advertisements for one ``(platform, pair)`` at one point in time."""

    platform: str
    pair: Pair
    ads: tuple[CompetitorAd, ...] = ()
    filtered: tuple[CompetitorAd, ...] = ()
    middle: Decimal | None = None
    fetched_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "pair": self.pair.symbol,
            "fetched_at": (self.fetched_at or utcnow()).isoformat(),
            "ads": [ad.to_dict() for ad in self.ads],
            "filtered": [ad.to_dict() for ad in self.filtered],
            "middle": _decimal_to_str(self.middle),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MarketSnapshot":
        fetched = data.get("fetched_at")
        return cls(
            platform=str(data["platform"]),
            pair=Pair.parse(str(data["pair"])),
            ads=tuple(CompetitorAd.from_dict(item) for item in data.get("ads", ())),
            filtered=tuple(CompetitorAd.from_dict(item) for item in data.get("filtered", ())),
            middle=_optional_decimal(data.get("middle")),
            fetched_at=datetime.fromisoformat(fetched) if fetched else None,
        )


@dataclass(frozen=True)
class AdSpec:
    """The advertisement we want a venue to publish.

    ``quantity`` is the token amount offered by the advertisement and ``payment_ids`` are
    venue-specific payment-method ids (Binance ``payId``, Bybit ``paymentIds``). Both are
    optional: an adapter derives a sensible default when they are absent (Binance resolves
    ``payId`` from the account's own payment methods by name, Bybit falls back to the
    documented sample value).
    """

    pair: Pair
    price: Decimal
    min_amount: Decimal
    max_amount: Decimal
    payment_methods: tuple[str, ...] = ()
    active: bool = True
    side: str = "sell"
    quantity: Decimal | None = None
    payment_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair.symbol,
            "price": str(self.price),
            "min_amount": str(self.min_amount),
            "max_amount": str(self.max_amount),
            "payment_methods": list(self.payment_methods),
            "active": self.active,
            "side": self.side,
            "quantity": _decimal_to_str(self.quantity),
            "payment_ids": list(self.payment_ids),
        }


@dataclass(frozen=True)
class AdActionResult:
    """Outcome of a venue call that created or updated one advertisement."""

    platform: str
    account_id: str
    pair: Pair
    adv_no: str | None
    price: Decimal
    created: bool
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdRecord:
    """Locally remembered advertisement of one account for one pair."""

    account_id: str
    pair: Pair
    adv_no: str | None
    price: Decimal
    active: bool = True
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "pair": self.pair.symbol,
            "adv_no": self.adv_no,
            "price": str(self.price),
            "active": self.active,
            "updated_at": (self.updated_at or utcnow()).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdRecord":
        updated = data.get("updated_at")
        return cls(
            account_id=str(data["account_id"]),
            pair=Pair.parse(str(data["pair"])),
            adv_no=data.get("adv_no"),
            price=parse_decimal(data["price"], "price"),
            active=bool(data.get("active", True)),
            updated_at=datetime.fromisoformat(updated) if updated else None,
        )


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one create/update attempt for one account."""

    account_id: str
    platform: str
    pair: Pair
    status: str
    price: Decimal | None = None
    adv_no: str | None = None
    error: str | None = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("created", "updated", "skipped", "dry_run")

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "pair": self.pair.symbol,
            "status": self.status,
            "price": _decimal_to_str(self.price),
            "adv_no": self.adv_no,
            "error": self.error,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ComputedAd:
    """A price the engine decided to advertise, for one pair on one platform."""

    pair: Pair
    platform: str
    price: Decimal
    source: str
    cap: Decimal
    accounts: tuple[str, ...] = ()
    base: Decimal | None = None
    clamped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair.symbol,
            "platform": self.platform,
            "price": str(self.price),
            "source": self.source,
            "cap": str(self.cap),
            "base": _decimal_to_str(self.base),
            "clamped": self.clamped,
            "accounts": list(self.accounts),
        }


def _decimal_to_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return parse_decimal(value)
