"""Competitor-market parsing, snapshot storage and the periodic fetch pass.

Three responsibilities live here:

* :func:`filter_ads` — the advertiser filter (merchants only; Binance additionally needs
  *more than* 500 orders in the last 30 days, a positive rate *above* 0.97 and a monthly
  finish rate *above* 0.94).  Comparisons are strict (``>``) and a missing metric fails
  closed (the ad is dropped and the reason logged at ``DEBUG``).
* :func:`middle_price` / :func:`build_snapshot` — the middle of the filtered price range,
  quantized to the pair fiat's tick with ``ROUND_HALF_UP``.
* :class:`MarketStore` / :class:`MarketParser` — the ``(platform, pair)`` snapshot store
  (in memory plus an optional JSON file, written atomically) and the fetch pass that fills
  it for every ``market_middle`` pair/platform of a blueprint.

Import note: ``p2pbot.exchanges`` is imported for typing only.  ``exchanges/base.py``
imports :func:`build_snapshot` from this module, so a runtime import would be circular;
:class:`MarketParser` therefore receives its adapters as an injected mapping.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import DEFAULT_PRICE_TICK, FILTERS_BY_PLATFORM, PLATFORMS, PRICE_TICK
from .errors import ConfigError, ExchangeError
from .models import CompetitorAd, Filters, MarketSnapshot, Pair, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .blueprint import Blueprint, PairPlan
    from .exchanges.base import ExchangeAdapter

__all__ = [
    "MarketFetchResult",
    "MarketParser",
    "MarketStore",
    "build_snapshot",
    "default_filters_for",
    "enabled_plans",
    "filter_ads",
    "middle_price",
    "snapshot_key",
]

_log = logging.getLogger(__name__)

#: Source expression that asks for the filtered middle price of the venue's order book.
MARKET_MIDDLE_SOURCE = "market_middle"
#: Prefix of a source expression that mirrors another platform's computed price.
COPY_SOURCE_PREFIX = "copy:"

#: Metric thresholds only exist for Binance (see ``constants.BINANCE_FILTERS``).
_THRESHOLD_PLATFORM = "binance"


def filter_ads(
    ads: Sequence[CompetitorAd],
    platform: str,
    filters: Filters | None,
) -> tuple[CompetitorAd, ...]:
    """Return the ads that pass ``filters`` for ``platform``.

    * the merchant check is case-insensitive on :attr:`CompetitorAd.user_type`;
    * the numeric thresholds are applied for Binance only (SPEC 8 — other venues publish
      no such metrics, and the blueprint's per-platform overrides carry them explicitly);
    * every comparison is strict (``>``) and a metric the filter asks for but the ad does
      not publish (``None``) excludes the ad;
    * ``filters`` ``None`` means "no filter configured for this platform", in which case
      every ad is kept.
    """
    if filters is None:
        return tuple(ads)
    venue = str(platform).strip().lower()
    wanted_user_type = (filters.user_type or "").strip().lower() if filters.user_type else ""
    check_thresholds = venue == _THRESHOLD_PLATFORM
    kept: list[CompetitorAd] = []
    for ad in ads:
        if wanted_user_type:
            actual = (ad.user_type or "").strip().lower()
            if actual != wanted_user_type:
                _log.debug(
                    "market filter dropped %s ad %s: user_type %r is not %r",
                    venue,
                    ad.adv_no or "-",
                    ad.user_type,
                    filters.user_type,
                )
                continue
        if check_thresholds and not _passes_thresholds(ad, filters, venue):
            continue
        kept.append(ad)
    return tuple(kept)


def _passes_thresholds(ad: CompetitorAd, filters: Filters, venue: str) -> bool:
    """Strict-``>`` metric checks; a missing metric excludes the ad (fail-closed)."""
    for metric, minimum in (
        ("month_order_count", filters.min_month_order_count),
        ("positive_rate", filters.min_positive_rate),
        ("month_finish_rate", filters.min_month_finish_rate),
    ):
        if minimum is None:
            continue
        value = getattr(ad, metric, None)
        if value is None:
            _log.debug(
                "market filter dropped %s ad %s: metric %s is missing",
                venue,
                ad.adv_no or "-",
                metric,
            )
            return False
        if not value > minimum:
            _log.debug(
                "market filter dropped %s ad %s: %s=%s is not > %s",
                venue,
                ad.adv_no or "-",
                metric,
                value,
                minimum,
            )
            return False
    return True


def middle_price(prices: Sequence[Decimal], fiat: str = "UAH") -> Decimal | None:
    """``(min + max) / 2`` of ``prices``, quantized to the fiat tick.

    Returns ``None`` for an empty sequence (there is no middle to compute).
    """
    if not prices:
        return None
    return ((min(prices) + max(prices)) / 2).quantize(_fiat_tick(fiat), rounding=ROUND_HALF_UP)


def build_snapshot(
    platform: str,
    pair: Pair,
    ads: Sequence[CompetitorAd],
    filters: Filters | None = None,
    fetched_at: datetime | None = None,
) -> MarketSnapshot:
    """Build a :class:`MarketSnapshot` with the filtered ads and their middle price."""
    venue = str(platform).strip().lower()
    resolved_pair = Pair.parse(pair)
    kept = filter_ads(ads, venue, filters)
    return MarketSnapshot(
        platform=venue,
        pair=resolved_pair,
        ads=tuple(ads),
        filtered=kept,
        middle=middle_price([ad.price for ad in kept], resolved_pair.fiat),
        fetched_at=fetched_at if fetched_at is not None else utcnow(),
    )


def _fiat_tick(fiat: str) -> Decimal:
    return PRICE_TICK.get(str(fiat).strip().upper(), DEFAULT_PRICE_TICK)


def default_filters_for(platform: str, plan: PairPlan) -> Filters:
    """Resolve the advertiser filter for ``platform`` on ``plan``.

    ``PairPlan.filters`` carries the blueprint's effective per-platform values (base policy
    merged with the scenario override); when the plan has none for this platform the
    hardcoded :data:`constants.FILTERS_BY_PLATFORM` default is used, falling back to
    :class:`Filters` (merchant-only) for venues without a hardcoded policy.
    """
    venue = str(platform).strip().lower()
    override = _plan_filters(plan, venue)
    if override is not None:
        return override
    return FILTERS_BY_PLATFORM.get(venue, Filters())


def _plan_filters(plan: PairPlan | None, platform: str) -> Filters | None:
    """Per-platform override carried by the blueprint, if any."""
    if plan is None:
        return None
    for attribute in ("filters", "filters_by_platform"):
        value = getattr(plan, attribute, None)
        if value is None:
            continue
        if isinstance(value, Filters):
            return value
        if isinstance(value, Mapping):
            candidate = _mapping_lookup(value, platform)
            if isinstance(candidate, Filters):
                return candidate
            if isinstance(candidate, Mapping):
                return Filters.from_dict(candidate)
    return None


def _mapping_lookup(mapping: Mapping[Any, Any], key: str) -> Any:
    """Case-insensitive mapping lookup (platform keys are lowercase by convention)."""
    if key in mapping:
        return mapping[key]
    for candidate_key, value in mapping.items():
        if str(candidate_key).strip().lower() == key:
            return value
    return None


@dataclass
class MarketStore:
    """``(platform, pair)`` -> :class:`MarketSnapshot`, optionally persisted to JSON.

    ``data`` accepts the payload produced by :meth:`as_dict` (a ``snapshots`` list) as well
    as a bare mapping of snapshot payloads (keyed by ``"platform:PAIR"`` or
    ``("platform", "PAIR")``).  ``save()`` writes atomically and is a no-op without a path.
    """

    data: Mapping[Any, Any] | None = None
    path: Path | None = None

    def __post_init__(self) -> None:
        self._snapshots: dict[tuple[str, str], MarketSnapshot] = _coerce_snapshots(self.data)
        self._path: Path | None = Path(self.path) if self.path is not None else None

    def put(self, snapshot: MarketSnapshot) -> None:
        """Store ``snapshot`` under its ``(platform, pair)`` key, replacing any previous one."""
        self._snapshots[snapshot_key(snapshot.platform, snapshot.pair)] = snapshot

    def get(self, platform: str, pair: Pair | str) -> MarketSnapshot | None:
        """Return the stored snapshot for ``(platform, pair)``, or ``None``."""
        return self._snapshots.get(snapshot_key(platform, pair))

    def middle(self, platform: str, pair: Pair | str) -> Decimal | None:
        """Filtered middle price for ``(platform, pair)``; ``None`` when unavailable.

        A stored snapshot that was built without a middle (``None``) is re-derived from its
        filtered ads, so hand-populated stores behave like parsed ones.
        """
        snapshot = self.get(platform, pair)
        if snapshot is None:
            return None
        if snapshot.middle is not None:
            return snapshot.middle
        if not snapshot.filtered:
            return None
        return middle_price([ad.price for ad in snapshot.filtered], snapshot.pair.fiat)

    def items(self) -> tuple[MarketSnapshot, ...]:
        """Every stored snapshot, ordered by ``(platform, pair)``."""
        return tuple(self._snapshots[key] for key in sorted(self._snapshots))

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable payload accepted back by :meth:`from_dict`."""
        return {"snapshots": [snapshot.to_dict() for snapshot in self.items()]}

    @classmethod
    def from_dict(cls, data: Mapping[Any, Any] | None) -> MarketStore:
        """Rebuild a store from :meth:`as_dict` output."""
        return cls(data=data)

    def save(self) -> None:
        """Atomically persist the store; no-op when no path was configured."""
        if self._path is None:
            return
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    @classmethod
    def load(cls, path: Path | str | None) -> MarketStore:
        """Load a store from ``path``; a missing, unreadable or corrupt file yields empty.

        The file is only a cache of competitor snapshots, so a damaged one must never stop the
        bot from starting: it is logged and replaced by the next parser pass (the same policy
        ``AdStore`` applies to its ledger).
        """
        if path is None:
            return cls()
        resolved = Path(path)
        if not resolved.exists():
            return cls(path=resolved)
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log.warning("market store %s is unreadable (%s); starting empty", resolved, exc)
            return cls(path=resolved)
        if not isinstance(payload, (Mapping, list)):
            _log.warning(
                "market store %s holds %s instead of an object/list; starting empty",
                resolved,
                type(payload).__name__,
            )
            return cls(path=resolved)
        try:
            return cls(data=payload, path=resolved)
        except ConfigError as exc:
            # Valid JSON whose entries are not snapshots (hand-edited or truncated) is still
            # damage: the cache must never be able to stop the bot from starting.
            _log.warning("market store %s holds invalid snapshot entries (%s); starting empty", resolved, exc)
            return cls(path=resolved)


def snapshot_key(platform: str, pair: Pair | str) -> tuple[str, str]:
    """Canonical store key: ``(lowercased platform, PAIR symbol)``."""
    return (str(platform).strip().lower(), Pair.parse(pair).symbol)


def _to_snapshot(value: Any) -> MarketSnapshot | None:
    if isinstance(value, MarketSnapshot):
        return value
    if isinstance(value, Mapping):
        try:
            return MarketSnapshot.from_dict(value)
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _coerce_snapshots(data: Any) -> dict[tuple[str, str], MarketSnapshot]:
    if data is None:
        return {}
    payload: Any = data
    if isinstance(data, Mapping) and "snapshots" in data:
        payload = data["snapshots"]
    entries: Iterable[Any]
    if isinstance(payload, Mapping):
        entries = payload.values()
    elif isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        entries = payload
    else:
        raise ConfigError("market store data must be a mapping or a sequence of snapshots")
    stored: dict[tuple[str, str], MarketSnapshot] = {}
    for entry in entries:
        snapshot = _to_snapshot(entry)
        if snapshot is None:
            raise ConfigError(f"invalid market snapshot payload: {entry!r}")
        stored[snapshot_key(snapshot.platform, snapshot.pair)] = snapshot
    return stored


@dataclass(frozen=True)
class MarketFetchResult:
    """Outcome of one ``(platform, pair)`` competitor fetch."""

    platform: str
    pair: Pair
    fetched: int = 0
    kept: int = 0
    middle: Decimal | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """``True`` when the fetch completed and the snapshot was stored."""
        return self.error is None


class MarketParser:
    """Fetch competitor ads for every ``market_middle`` pair/platform and store snapshots.

    Adapters are injected (``p2pbot.exchanges`` must not be imported at runtime here), and
    the clock is injectable so the parser is deterministic under test.  A failing venue is
    captured per fetch: the pass never aborts because one exchange misbehaved.
    """

    def __init__(
        self,
        adapters: Mapping[str, ExchangeAdapter],
        market: MarketStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapters: dict[str, ExchangeAdapter] = {
            str(name).strip().lower(): adapter for name, adapter in adapters.items()
        }
        self._market = market
        self._now = clock if clock is not None else utcnow

    @property
    def market(self) -> MarketStore:
        """The store this parser writes snapshots into."""
        return self._market

    @property
    def adapters(self) -> Mapping[str, ExchangeAdapter]:
        """Registered adapters keyed by lowercased platform name."""
        return dict(self._adapters)

    def run_once(
        self, blueprint: Blueprint, pairs: Iterable[str] | None = None
    ) -> tuple[MarketFetchResult, ...]:
        """Fetch every enabled ``market_middle`` pair/platform of ``blueprint``.

        ``pairs`` narrows the pass to the given pair symbols; ``copy:`` platforms are never
        fetched (they mirror a price computed by the engine).
        """
        wanted = _symbol_filter(pairs)
        results: list[MarketFetchResult] = []
        for plan in enabled_plans(blueprint):
            pair = Pair.parse(plan.pair)
            if wanted is not None and pair.symbol not in wanted:
                continue
            for platform, source in _ordered_sources(plan):
                if source.strip().lower() != MARKET_MIDDLE_SOURCE:
                    continue
                results.append(self._fetch(plan, pair, platform, source))
        return tuple(results)

    def middle_for(self, blueprint: Blueprint, pair_symbol: str, platform: str) -> Decimal | None:
        """Stored middle price for ``pair_symbol`` on ``platform``, or ``None``."""
        pair = _resolve_pair(blueprint, pair_symbol)
        return self._market.middle(platform, pair)

    def _fetch(
        self, plan: PairPlan, pair: Pair, platform: str, source: str
    ) -> MarketFetchResult:
        adapter = self._adapters.get(platform)
        if adapter is None:
            message = f"no adapter registered for platform {platform!r}"
            _log.warning("market fetch skipped for %s on %s: %s", pair.symbol, platform, message)
            return MarketFetchResult(platform=platform, pair=pair, error=message)
        filters = default_filters_for(platform, plan)
        try:
            snapshot = adapter.search_ads(pair, filters=filters)
        except ExchangeError as exc:
            message = f"{type(exc).__name__}: {exc}"
            _log.warning("market fetch failed for %s on %s: %s", pair.symbol, platform, message)
            return MarketFetchResult(platform=platform, pair=pair, error=message)
        self._market.put(snapshot)
        _log.debug(
            "market fetch for %s on %s kept %d/%d ads (source %s)",
            pair.symbol,
            platform,
            len(snapshot.filtered),
            len(snapshot.ads),
            source,
        )
        return MarketFetchResult(
            platform=platform,
            pair=pair,
            fetched=len(snapshot.ads),
            kept=len(snapshot.filtered),
            middle=snapshot.middle,
        )


def enabled_plans(blueprint: Blueprint) -> tuple[PairPlan, ...]:
    """Enabled pair plans of ``blueprint`` (``Blueprint.enabled_pairs`` when available)."""
    getter = getattr(blueprint, "enabled_pairs", None)
    plans = tuple(getter()) if callable(getter) else tuple(getattr(blueprint, "pairs", ()))
    return tuple(plan for plan in plans if getattr(plan, "enabled", True))


def _ordered_sources(plan: PairPlan) -> tuple[tuple[str, str], ...]:
    """``(platform, source expression)`` pairs in canonical platform order."""
    sources = getattr(plan, "sources", None) or {}
    return tuple(
        sorted(
            (
                (str(platform).strip().lower(), str(source).strip())
                for platform, source in sources.items()
            ),
            key=lambda item: _platform_rank(item[0]),
        )
    )


def _platform_rank(platform: str) -> tuple[int, str]:
    """Sort key placing the canonical platforms (``constants.PLATFORMS``) first."""
    try:
        return (PLATFORMS.index(platform), platform)
    except ValueError:
        return (len(PLATFORMS), platform)


def _symbol_filter(pairs: Iterable[str] | None) -> frozenset[str] | None:
    if pairs is None:
        return None
    symbols = {str(item).strip().upper() for item in pairs if str(item).strip()}
    return frozenset(symbols) if symbols else None


def _resolve_pair(blueprint: Blueprint, pair_symbol: str) -> Pair:
    pair = Pair.parse(pair_symbol)
    resolver = getattr(blueprint, "pair", None)
    if callable(resolver):
        try:
            plan = resolver(pair.symbol)
        except (KeyError, ConfigError):
            plan = None
        if plan is not None:
            return Pair.parse(plan.pair)
    return pair
