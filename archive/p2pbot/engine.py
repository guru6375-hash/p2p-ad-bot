"""Price engine: turns a blueprint plus stored rates and market data into ad prices.

Every enabled pair plan of the blueprint is priced per platform following SPEC 7:

1. the resolved source (``base_rate``, ``market_middle`` or ``copy:<Platform>``) produces a
   reference price;
2. ``plan.price_offset`` is added and the price is quantized to the fiat tick with
   ``ROUND_HALF_UP`` (:func:`quantize_price`);
3. the stored **cap wins over everything**: a price above it is clamped, flagged
   (``clamped=True``) and logged as a warning, because an advertisement must never be
   published above the owner's ceiling; a ``copy:`` price that mirrors an already clamped
   price lands exactly on the cap and inherits the flag;
4. a non-positive price or a missing rate/cap/market datum raises the matching
   :mod:`p2pbot.errors` error naming the offending pair and platform.

Ordering guarantee: anchors are computed first, then linked pairs, and within a pair
non-copy platforms come before ``copy:`` platforms, so the copied price is always already
known from this very cycle.

Sparse venues (SPEC 7.1): :meth:`RateEngine.compute` is strict — the first failing entry
raises — while :meth:`RateEngine.compute_with_problems` attempts every entry
independently, skips the ones that cannot be priced and reports one problem line each, so
a venue with no usable competitor ads for one pair never blocks the prices that *are*
computable.  Both run the identical pass in the identical order; the cap rule is never
relaxed by the tolerant variant (isolation only ever removes an ad).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from .constants import DEFAULT_PRICE_TICK, PLATFORMS, PRICE_TICK
from .errors import (
    ConfigError,
    EngineError,
    MissingCapError,
    MissingMarketDataError,
    MissingRateError,
    PriceError,
)
from .models import AccountRef, ComputedAd, Pair

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .blueprint import Blueprint, PairPlan
    from .market import MarketStore
    from .rates import RateStore

__all__ = ["ComputedAd", "RateEngine", "quantize_price"]

_log = logging.getLogger(__name__)

#: Price from the owner's stored ``base_rate`` for the pair itself.
SOURCE_BASE_RATE = "base_rate"
#: Price from the filtered middle of the venue's competitor prices.
SOURCE_MARKET_MIDDLE = "market_middle"
#: Price copied from another platform's price computed in the same cycle.
COPY_SOURCE_PREFIX = "copy:"


def quantize_price(value: Decimal, fiat: str) -> Decimal:
    """Quantize ``value`` to the fiat's price tick, rounding half up.

    Unknown fiats fall back to :data:`constants.DEFAULT_PRICE_TICK`.
    """
    return value.quantize(_tick(fiat), rounding=ROUND_HALF_UP)


def _tick(fiat: str) -> Decimal:
    return PRICE_TICK.get(str(fiat).strip().upper(), DEFAULT_PRICE_TICK)


class RateEngine:
    """Computes the advertisement price of every pair/platform of a blueprint."""

    def __init__(
        self,
        blueprint: Blueprint,
        rates: RateStore,
        market: MarketStore | None = None,
    ) -> None:
        self.blueprint = blueprint
        self.rates = rates
        self.market = market

    # -- public API ----------------------------------------------------------------
    def compute(self) -> tuple[ComputedAd, ...]:
        """Prices for every enabled pair plan, in dependency (anchor-first) order.

        Strict: the first skipped entry's exception is re-raised (same type and message as
        :meth:`compute_with_problems` recorded for that entry), so a missing rate/cap or a
        data-less venue keeps failing loudly for callers that cannot tolerate gaps.
        """
        ads, _, errors = self._run_pass()
        if errors:
            raise errors[0]
        return ads

    def compute_with_problems(self) -> tuple[tuple[ComputedAd, ...], tuple[str, ...]]:
        """Every price that *could* be computed, plus one line per skipped entry.

        A sparse venue must not stop the rest of the cycle: each enabled
        ``(pair, platform, source)`` entry is attempted independently and an
        :class:`~p2pbot.errors.EngineError` skips only that entry, recording
        ``"<PAIR> <platform>: <ExceptionType>: <message>"``.  A ``copy:<Platform>`` source
        whose source platform was skipped is skipped too (its own problem line names the
        missing source platform) — a stale or invented price is never published.  Caps are
        untouched: isolation only ever removes an ad, never relaxes the ceiling.
        """
        ads, problems, _ = self._run_pass()
        return ads, problems

    def compute_pair(
        self,
        plan: PairPlan,
        previous: Mapping[tuple[str, str], Decimal] | None = None,
    ) -> tuple[ComputedAd, ...]:
        """Prices of one pair plan, resolving ``copy:`` sources from ``previous``.

        ``previous`` maps ``(platform_lower, pair_symbol)`` to a price computed earlier in
        this cycle; results of this call are added to it as they are produced, so a copy
        platform can mirror a sibling platform of the same pair.  Strict like
        :meth:`compute`: the first skipped entry of the plan is re-raised.
        """
        resolved: dict[tuple[str, str], Decimal] = dict(previous or {})
        ads: list[ComputedAd] = []
        problems: list[str] = []
        errors: list[EngineError] = []
        self._collect_pair(plan, resolved, {}, ads, problems, errors)
        if errors:
            raise errors[0]
        return tuple(ads)

    # -- internals -----------------------------------------------------------------
    def _run_pass(
        self,
    ) -> tuple[tuple[ComputedAd, ...], tuple[str, ...], tuple[EngineError, ...]]:
        """One full pass: ads in deterministic order, problem lines, captured errors."""
        previous: dict[tuple[str, str], Decimal] = {}
        capped: dict[tuple[str, str], bool] = {}
        ads: list[ComputedAd] = []
        problems: list[str] = []
        errors: list[EngineError] = []
        for plan in self._ordered_plans():
            self._collect_pair(plan, previous, capped, ads, problems, errors)
        return tuple(ads), tuple(problems), tuple(errors)

    def _collect_pair(
        self,
        plan: PairPlan,
        previous: dict[tuple[str, str], Decimal],
        capped: dict[tuple[str, str], bool],
        ads: list[ComputedAd],
        problems: list[str],
        errors: list[EngineError],
    ) -> None:
        """Attempt every ``(platform, source)`` entry of one plan, isolating failures."""
        if not getattr(plan, "enabled", True):
            _log.debug("skipping disabled pair plan %s", getattr(plan, "pair", "?"))
            return
        pair = Pair.parse(plan.pair)
        try:
            entries = _ordered_sources(plan, pair)
        except EngineError as exc:
            # a plan without any resolved source has no platform to name
            problems.append(f"{pair.symbol}: {type(exc).__name__}: {exc}")
            errors.append(exc)
            return
        for platform, source in entries:
            try:
                ad = self._compute_one(plan, pair, platform, source, previous, capped)
            except EngineError as exc:
                problems.append(f"{pair.symbol} {platform}: {type(exc).__name__}: {exc}")
                errors.append(exc)
                _log.warning(
                    "price computation skipped for %s on %s: %s", pair.symbol, platform, exc
                )
                continue
            key = (ad.platform, ad.pair.symbol)
            previous[key] = ad.price
            capped[key] = ad.clamped
            ads.append(ad)

    def _ordered_plans(self) -> tuple[PairPlan, ...]:
        getter = getattr(self.blueprint, "enabled_pairs", None)
        plans = tuple(getter()) if callable(getter) else tuple(getattr(self.blueprint, "pairs", ()))
        enabled = [plan for plan in plans if getattr(plan, "enabled", True)]
        enabled.sort(key=lambda plan: 0 if getattr(plan, "anchor", False) else 1)
        return tuple(enabled)

    def _compute_one(
        self,
        plan: PairPlan,
        pair: Pair,
        platform: str,
        source: str,
        previous: Mapping[tuple[str, str], Decimal],
        capped: Mapping[tuple[str, str], bool],
    ) -> ComputedAd:
        reference, base, inherited_clamp = self._reference_price(
            plan, pair, platform, source, previous, capped
        )
        computed = quantize_price(reference + _offset_of(plan), pair.fiat)
        cap = self.rates.cap(pair)
        if cap is None:
            raise MissingCapError(
                f"no cap_rate stored for {pair.symbol}; refusing to price it on {platform}"
            )
        price = computed
        clamped = False
        if price > cap:
            price = _clamp_to_cap(cap, pair.fiat)
            clamped = True
            _log.warning(
                "price for %s on %s clamped to cap %s (computed %s)",
                pair.symbol,
                platform,
                cap,
                computed,
            )
        elif inherited_clamp and price == cap:
            # copied from a platform whose price the cap already limited, so this price is
            # cap-derived too even though it did not need clamping itself
            clamped = True
        if price <= 0:
            raise PriceError(
                f"computed price for {pair.symbol} on {platform} is not publishable: {price}"
            )
        return ComputedAd(
            pair=pair,
            platform=platform,
            price=price,
            source=source,
            cap=cap,
            accounts=_accounts_for(plan, platform),
            base=base,
            clamped=clamped,
        )

    def _reference_price(
        self,
        plan: PairPlan,
        pair: Pair,
        platform: str,
        source: str,
        previous: Mapping[tuple[str, str], Decimal],
        capped: Mapping[tuple[str, str], bool],
    ) -> tuple[Decimal, Decimal, bool]:
        """Resolve ``source`` into ``(reference price, base, inherited cap flag)``.

        ``base`` is the anchoring value the source derived the price from: the pair's
        stored ``base_rate``, the venue's middle price, or the already-computed price a
        ``copy:`` mirrors.
        """
        lowered = source.strip().lower()
        if lowered == SOURCE_BASE_RATE:
            base = self._require_base(pair, f"needed by platform {platform}")
            return base, base, False
        if lowered == SOURCE_MARKET_MIDDLE:
            if self.market is None:
                raise MissingMarketDataError(
                    f"no market store configured; cannot price {pair.symbol} on {platform} "
                    "from market_middle"
                )
            middle = self.market.middle(platform, pair)
            if middle is None:
                raise MissingMarketDataError(
                    f"no market middle available for {pair.symbol} on {platform}"
                )
            return middle, middle, False
        if lowered.startswith(COPY_SOURCE_PREFIX):
            reference = source.split(":", 1)[1].strip().lower()
            if not reference:
                raise MissingMarketDataError(
                    f"malformed copy source {source!r} for {pair.symbol} on {platform}"
                )
            copied = previous.get((reference, pair.symbol))
            if copied is None:
                raise MissingMarketDataError(
                    f"no computed price for {pair.symbol} on {reference} to copy into {platform}"
                )
            return copied, copied, bool(capped.get((reference, pair.symbol), False))
        raise PriceError(f"unknown price source {source!r} for {pair.symbol} on {platform}")

    def _require_base(self, pair: Pair, context: str) -> Decimal:
        base = self.rates.base(pair)
        if base is None:
            raise MissingRateError(f"no base_rate stored for {pair.symbol} ({context})")
        return base


def _ordered_sources(plan: PairPlan, pair: Pair) -> tuple[tuple[str, str], ...]:
    """``(platform, source)`` of ``plan`` in platform order, with ``copy:`` platforms last."""
    sources = getattr(plan, "sources", None) or {}
    if not sources:
        raise PriceError(f"pair {pair.symbol} has no platform price sources resolved")
    entries = [
        (str(platform).strip().lower(), str(source).strip())
        for platform, source in sources.items()
    ]
    entries.sort(
        key=lambda item: (
            1 if item[1].lower().startswith(COPY_SOURCE_PREFIX) else 0,
            _platform_rank(item[0]),
        )
    )
    return tuple(entries)


def _platform_rank(platform: str) -> tuple[int, str]:
    """Sort key placing the canonical platforms (``constants.PLATFORMS``) first."""
    try:
        return (PLATFORMS.index(platform), platform)
    except ValueError:
        return (len(PLATFORMS), platform)


def _offset_of(plan: PairPlan) -> Decimal:
    offset = getattr(plan, "price_offset", None)
    if offset is None:
        return Decimal("0")
    return offset if isinstance(offset, Decimal) else Decimal(str(offset))


def _accounts_for(plan: PairPlan, platform: str) -> tuple[str, ...]:
    """Account ids of ``platform`` listed for this pair (canonical ``Platform#n`` form).

    ``PairPlan.accounts_for(platform)`` (which honours per-platform account overrides) is
    preferred when the blueprint provides it; otherwise the plan's account list is filtered
    by platform prefix.  Both paths go through :class:`AccountRef`, so ids are canonical.
    """
    resolved: tuple[Any, ...] | None = None
    helper = getattr(plan, "accounts_for", None)
    if callable(helper):
        try:
            resolved = tuple(helper(platform))
        except (ConfigError, KeyError, TypeError):
            _log.debug("accounts_for(%r) unavailable on plan %s", platform, platform)
            resolved = None
    if resolved is None:
        resolved = tuple(getattr(plan, "accounts", ()) or ())
    accounts: list[str] = []
    for raw in resolved:
        try:
            ref = raw if isinstance(raw, AccountRef) else AccountRef.parse(str(raw))
        except ConfigError:
            _log.warning("ignoring invalid account id %r on platform %s", raw, platform)
            continue
        if ref.platform == platform:
            accounts.append(ref.id)
    return tuple(accounts)


def _clamp_to_cap(cap: Decimal, fiat: str) -> Decimal:
    """The cap as a publishable price: never above the cap itself."""
    tick = _tick(fiat)
    quantized = cap.quantize(tick, rounding=ROUND_HALF_UP)
    if quantized > cap:
        return cap.quantize(tick, rounding=ROUND_DOWN)
    return quantized
