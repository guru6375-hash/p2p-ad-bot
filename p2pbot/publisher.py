"""The advertisement publisher: the last gate before a venue sees a price (SPEC section 10).

This module owns two things:

* :class:`AdStore` — the local ledger of the advertisements we created
  (``(account_id, pair) -> AdRecord``), persisted as JSON so that a restart *updates* the
  existing advertisements instead of creating duplicates.
* :class:`AdPublisher` — pushes a computed price to every account of every pair. The
  stored ``cap_rate`` is re-asserted immediately before a request is built (defence in
  depth: the engine clamps as well, but the publisher is the last line of defence).

:func:`build_ad_spec` turns one :class:`~p2pbot.models.ComputedAd` plus the active
blueprint (or a single :class:`~p2pbot.blueprint.PairPlan`) into the venue-facing
:class:`~p2pbot.models.AdSpec`: the price comes from the computed ad, the amount bounds and
payment methods come from the pair plan, and the side defaults to ``sell``.

The publisher never raises for a per-account fault: every failure is captured into a
:class:`~p2pbot.models.PublishResult` with ``status="error"`` so the remaining accounts of
the same pair are still published.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .blueprint import DEFAULT_MAX_AMOUNT, DEFAULT_MIN_AMOUNT, Blueprint, PairPlan
from .config import Settings
from .constants import SIDE_SELL
from .errors import BotError, ConfigError, ExchangeError
from .logging_setup import get_logger
from .models import (
    AccountRef,
    AdRecord,
    AdSpec,
    ComputedAd,
    Pair,
    PublishResult,
    utcnow,
)
from .rates import RateStore

if TYPE_CHECKING:  # pragma: no cover - typing only: the adapter package may not be importable
    from .exchanges.base import ExchangeAdapter

__all__ = ["AdStore", "AdPublisher", "build_ad_spec"]

_log = get_logger(__name__)


def _record_key(account_id: str, pair: Pair | str) -> tuple[str, str]:
    """Canonical ledger key: ``(account display id, PAIR symbol)``."""
    try:
        account = AccountRef.parse(str(account_id)).id
    except ConfigError:  # a hand-edited ledger may hold a non-canonical id
        account = str(account_id).strip()
    return (account, Pair.parse(pair).symbol)


class AdStore:
    """``(account_id, pair)`` -> :class:`~p2pbot.models.AdRecord`, optionally persisted.

    Args:
        data: payload shaped like :meth:`as_dict` output (``{"records": [...]}``); a bare
            list of records is accepted too.
        path: file used by :meth:`save`; ``None`` (the default) keeps the store in memory.
    """

    def __init__(self, data: Any = None, path: str | Path | None = None) -> None:
        self.path: Path | None = Path(path) if path is not None else None
        self._records: dict[tuple[str, str], AdRecord] = {}
        if data is not None:
            self._merge(data)

    # -- lookup --------------------------------------------------------------------
    def get(self, account_id: str, pair: Pair | str) -> AdRecord | None:
        """The remembered advertisement of ``account_id`` for ``pair``, or ``None``."""
        return self._records.get(_record_key(account_id, pair))

    def items(self) -> tuple[AdRecord, ...]:
        """Every remembered advertisement, ordered by ``(account_id, pair)``."""
        return tuple(self._records[key] for key in sorted(self._records))

    # -- mutation ------------------------------------------------------------------
    def put(self, record: AdRecord) -> None:
        """Remember ``record``, replacing any previous record for the same account/pair."""
        self._records[_record_key(record.account_id, record.pair)] = record

    # -- serialization -------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """JSON-ready payload accepted back by :meth:`from_dict`."""
        return {"records": [record.to_dict() for record in self.items()]}

    @classmethod
    def from_dict(cls, data: Any) -> "AdStore":
        """Rebuild a store from :meth:`as_dict` output."""
        return cls(data=data)

    def save(self) -> None:
        """Write the ledger atomically; a store without a path is a no-op."""
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
            raise BotError(f"cannot write ad ledger {target}: {exc}") from exc
        finally:
            if temporary.exists():  # pragma: no cover - only reached after a failed write
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @classmethod
    def load(cls, path: str | Path | None) -> "AdStore":
        """Read ``path``; a missing or corrupt file degrades to an empty store.

        The ledger is a cache of what we published: losing it costs a duplicate-create, so
        a damaged file must never stop the bot. Every failure is logged and swallowed.
        """
        store = cls(path=path)
        if path is None:
            return store
        file_path = Path(path)
        try:
            text = file_path.read_text(encoding="utf-8")
            data: Any = json.loads(text) if text.strip() else {}
        except FileNotFoundError:
            return store
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            _log.warning("ad ledger %s is unreadable (%s); starting empty", file_path, exc)
            return store
        try:
            store._merge(data if data is not None else {})
        except (ConfigError, TypeError, ValueError, KeyError, AttributeError) as exc:
            _log.warning("ad ledger %s is malformed (%s); starting empty", file_path, exc)
            return cls(path=file_path)
        return store

    # -- internals -----------------------------------------------------------------
    def _merge(self, data: Any) -> None:
        records = data.get("records") if isinstance(data, Mapping) else data
        if records is None:
            return
        if not isinstance(records, (list, tuple)):
            raise ConfigError("ad ledger must hold a list of records under 'records'")
        for entry in records:
            if not isinstance(entry, Mapping):
                _log.warning("ad ledger entry is not an object (%s); skipped", type(entry).__name__)
                continue
            try:
                record = AdRecord.from_dict(entry)
            except (ConfigError, KeyError, TypeError, ValueError) as exc:
                _log.warning("skipped unreadable ad ledger entry: %s", exc)
                continue
            self.put(record)


def _resolve_plan(source: Blueprint | PairPlan | None, pair: Pair) -> PairPlan | None:
    """The pair plan inside ``source`` for ``pair``, or ``None`` when there is none."""
    if source is None:
        return None
    if isinstance(source, Blueprint):
        for plan in source.pairs:
            if plan.pair == pair:
                return plan
        raise ConfigError(
            f"blueprint {source.name!r} has no pair {pair.symbol}; "
            f"known pairs: {', '.join(plan.pair.symbol for plan in source.pairs) or 'none'}"
        )
    if getattr(source, "pair", None) == pair:
        return source
    raise ConfigError(f"pair plan is for {source.pair.symbol}, not {pair.symbol}")


def build_ad_spec(
    computed: ComputedAd,
    blueprint_or_plan: Blueprint | PairPlan | None = None,
    *,
    price: Decimal | None = None,
    active: bool | None = None,
) -> AdSpec:
    """Build the venue-facing :class:`~p2pbot.models.AdSpec` for ``computed``.

    Args:
        computed: the price the engine decided to advertise.
        blueprint_or_plan: the active blueprint (the plan of ``computed.pair`` is looked
            up) or that plan itself; ``None`` falls back to the blueprint-level amount
            defaults, which is only sensible when no scenario is known.
        price: price to publish instead of ``computed.price`` (the publisher passes the
            cap-re-asserted price).
        active: ``True``/``False`` to force the ad on/off; ``None`` keeps the plan's
            ``enabled`` flag.

    Payment methods and amount bounds come from the plan; the side is always ``sell``.
    """
    pair = Pair.parse(computed.pair)
    plan = _resolve_plan(blueprint_or_plan, pair)
    if plan is None:
        min_amount = DEFAULT_MIN_AMOUNT
        max_amount = DEFAULT_MAX_AMOUNT
        payment_methods: tuple[str, ...] = ()
        default_active = True
    else:
        min_amount = plan.min_amount
        max_amount = plan.max_amount
        payment_methods = tuple(plan.payment_methods)
        default_active = bool(getattr(plan, "enabled", True))
    return AdSpec(
        pair=pair,
        price=computed.price if price is None else price,
        min_amount=min_amount,
        max_amount=max_amount,
        payment_methods=payment_methods,
        active=default_active if active is None else bool(active),
        side=SIDE_SELL,
    )


class AdPublisher:
    """Create or update the advertisements of every account of every computed price.

    Args:
        adapters: venue adapters keyed by platform (``binance``/``okx``/``bybit``).
        settings: configuration used to resolve an account id into its credentials.
        store: ledger of the advertisements we own, so a rerun updates instead of creates.
        rates: rate store; its ``cap_rate`` is re-asserted for every advertisement.
        dry_run: default for :meth:`publish` when the call does not decide itself.
        plans: the active blueprint (or a single pair plan) supplying the amount bounds and
            payment methods of the ad specs; :meth:`set_blueprint` replaces it later, e.g.
            when the operator switches scenario.
    """

    def __init__(
        self,
        adapters: Mapping[str, "ExchangeAdapter"],
        settings: Settings,
        store: AdStore,
        rates: RateStore,
        dry_run: bool = False,
        *,
        plans: Blueprint | PairPlan | None = None,
    ) -> None:
        self.adapters: dict[str, "ExchangeAdapter"] = {
            str(name).strip().lower(): adapter for name, adapter in adapters.items()
        }
        self.settings = settings
        self.store = store
        self.rates = rates
        self.dry_run = bool(dry_run)
        self.plans: Blueprint | PairPlan | None = plans
        self._sessions: set[str] = set()
        self._session_failures: dict[str, str] = {}
        self._dirty = False

    # -- configuration -------------------------------------------------------------
    def set_blueprint(self, blueprint: Blueprint | PairPlan | None) -> None:
        """Attach the scenario whose pair plans describe the advertisements to publish."""
        self.plans = blueprint

    def plan_for(self, pair: Pair | str) -> PairPlan | None:
        """The pair plan describing ``pair``, or ``None`` when no scenario is attached."""
        return _resolve_plan(self.plans, Pair.parse(pair))

    # -- publishing ----------------------------------------------------------------
    def publish(
        self,
        ads: Sequence[ComputedAd],
        *,
        dry_run: bool | None = None,
        active: bool | None = None,
        create_missing: bool = True,
    ) -> tuple[PublishResult, ...]:
        """Push ``ads`` to every account listed on them, one result per account.

        Args:
            ads: computed prices, in the order the engine produced them.
            dry_run: overrides the constructor default for this call; a dry run builds no
                request at all and persists nothing.
            active: force the ads on/off; ``None`` keeps the blueprints' active flag.
            create_missing: when false, an account without a remembered ``adv_no`` is
                reported as ``skipped`` instead of being published.

        Per-account faults (``ExchangeError``, an unknown account, a missing adapter) are
        captured into that account's result; other accounts continue. The ledger is written
        once at the end, and only when an advertisement was actually created or updated - a
        dry run therefore leaves the state on disk untouched.
        """
        effective_dry_run = self.dry_run if dry_run is None else bool(dry_run)
        self._sessions.clear()
        self._session_failures.clear()
        self._dirty = False
        results: list[PublishResult] = []
        published: set[tuple[str, str]] = set()
        for computed in ads:
            pair = Pair.parse(computed.pair)
            plan = self.plan_for(pair)
            if plan is None:
                _log.warning(
                    "no pair plan for %s: publishing with the default amount bounds", pair.symbol
                )
            price = self._capped_price(computed, pair)
            spec = build_ad_spec(computed, plan, price=price, active=active)
            for account_id in computed.accounts:
                # same canonicalisation as the ledger key, so "Binance#1" and "binance#1"
                # are recognised as one account and published exactly once
                key = _record_key(account_id, pair)
                if key in published:
                    _log.warning("duplicate account %s for %s skipped", key[0], pair.symbol)
                    continue
                published.add(key)
                results.append(
                    self._publish_account(
                        computed, pair, account_id, spec, effective_dry_run, create_missing
                    )
                )
        if self._dirty:
            self.store.save()
        return tuple(results)

    # -- internals -----------------------------------------------------------------
    def _capped_price(self, computed: ComputedAd, pair: Pair) -> Decimal:
        """The price actually pushed: never above the stored (or computed) ceiling."""
        cap = self.rates.cap(pair)
        if cap is None:
            cap = computed.cap
        price: Decimal = computed.price
        if cap is not None and price > cap:
            _log.warning(
                "cap re-asserted for %s on %s: %s clamped to %s",
                pair.symbol,
                computed.platform,
                price,
                cap,
            )
            price = cap
        return price

    def _publish_account(
        self,
        computed: ComputedAd,
        pair: Pair,
        account_id: str,
        spec: AdSpec,
        dry_run: bool,
        create_missing: bool,
    ) -> PublishResult:
        """One create/update attempt for one account; never raises for a venue fault."""
        try:
            account = self.settings.account(account_id)
        except ConfigError as exc:
            _log.warning("account %s is not configured: %s", account_id, exc)
            return PublishResult(
                account_id=str(account_id),
                platform=computed.platform,
                pair=pair,
                status="error",
                price=spec.price,
                error=str(exc),
                dry_run=dry_run,
            )

        existing = self.store.get(account.id, pair)
        adv_no = existing.adv_no if existing is not None else None
        if dry_run:
            return PublishResult(
                account_id=account.id,
                platform=computed.platform,
                pair=pair,
                status="dry_run",
                price=spec.price,
                adv_no=adv_no,
                dry_run=True,
            )
        if adv_no is None and not create_missing:
            _log.info("skipped %s %s: no advertisement on record", account.id, pair.symbol)
            return PublishResult(
                account_id=account.id,
                platform=computed.platform,
                pair=pair,
                status="skipped",
                price=spec.price,
                dry_run=False,
            )

        adapter = self.adapters.get(str(computed.platform).strip().lower())
        if adapter is None:
            message = f"no adapter registered for platform {computed.platform!r}"
            _log.warning("%s: %s", account.id, message)
            return PublishResult(
                account_id=account.id,
                platform=computed.platform,
                pair=pair,
                status="error",
                price=spec.price,
                adv_no=adv_no,
                error=message,
            )

        failure = self._session_failures.get(account.id)
        if failure is not None:
            return PublishResult(
                account_id=account.id,
                platform=computed.platform,
                pair=pair,
                status="error",
                price=spec.price,
                adv_no=adv_no,
                error=failure,
            )

        try:
            self._ensure_session(adapter, account)
            created = not adv_no
            status = "created" if created else "updated"
            if created:
                request = adapter.build_create_ad_request(account, spec)
            else:
                request = adapter.build_update_ad_request(account, spec, adv_no)
            action = adapter.parse_ad_result(
                adapter.send_private(account, request),
                account=account,
                pair=pair,
                spec=spec,
                created=created,
            )
        except ExchangeError as exc:
            _log.warning("%s %s failed: %s", account.id, pair.symbol, exc)
            return PublishResult(
                account_id=account.id,
                platform=computed.platform,
                pair=pair,
                status="error",
                price=spec.price,
                adv_no=adv_no,
                error=f"{type(exc).__name__}: {exc}",
            )

        stored_adv_no = action.adv_no or adv_no
        self.store.put(
            AdRecord(
                account_id=account.id,
                pair=pair,
                adv_no=stored_adv_no,
                price=spec.price,
                active=spec.active,
                updated_at=utcnow(),
            )
        )
        self._dirty = True
        _log.info(
            "%s %s %s at %s%s", status, account.id, pair.symbol, spec.price,
            f" (adv {stored_adv_no})" if stored_adv_no else "",
        )
        return PublishResult(
            account_id=account.id,
            platform=computed.platform,
            pair=pair,
            status=status,
            price=spec.price,
            adv_no=stored_adv_no,
        )

    def _ensure_session(self, adapter: "ExchangeAdapter", account: Any) -> None:
        """Bootstrap the venue session once per account per run, when the venue needs one."""
        if account.id in self._sessions:
            return
        request = adapter.build_login_request(account)
        if request is None:
            self._sessions.add(account.id)
            return
        try:
            adapter.send_private(account, request)
        except ExchangeError as exc:
            self._session_failures[account.id] = f"{type(exc).__name__}: {exc}"
            raise
        self._sessions.add(account.id)
        _log.debug("session established for %s", account.id)
