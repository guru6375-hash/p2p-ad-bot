"""The advertisement publisher: the last gate before a venue sees a price (SPEC section 10).

This module owns two things:

* :class:`AdStore` — the local ledger of the advertisements we manage
  (``(account_id, pair) -> AdRecord``), persisted as JSON so that a pair's ad can be found
  again by :meth:`AdPublisher.edit_ad`.
* :class:`AdPublisher` — reads the live advertisements of every account
  (:meth:`~AdPublisher.fetch_own_ads`) and edits one existing **buy** ad at a time
  (:meth:`~AdPublisher.edit_ad`). It never creates an advertisement and never touches a
  sell ad. There is no cap: the price asked for is the price sent.

Per-account faults are never raised: they are captured into the returned
:class:`~p2pbot.models.PublishResult` / :class:`~p2pbot.models.OwnAdsResult`.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .config import Settings
from .constants import PLATFORMS, SIDE_BUY
from .errors import BotError, ConfigError, ExchangeError
from .logging_setup import get_logger
from .models import (
    AD_STATUS_CLOSED,
    AccountRef,
    AdRecord,
    AdSpec,
    OwnAd,
    OwnAdsResult,
    Pair,
    PublishResult,
    parse_decimal,
    utcnow,
)

if TYPE_CHECKING:  # pragma: no cover - typing only: the adapter package may not be importable
    from .exchanges.base import ExchangeAdapter

__all__ = ["AdStore", "AdPublisher"]

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

        The ledger is a cache of what we manage: losing it means the ads must be linked
        again (``edit_ad(adv_no=...)``), so a damaged file must never stop the bot. Every
        failure is logged and swallowed.
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


class AdPublisher:
    """Read the live advertisements and edit existing buy ads.

    Args:
        adapters: venue adapters keyed by platform (``binance``/``okx``/``bybit``).
        settings: configuration used to resolve an account id into its credentials.
        store: ledger of the advertisements we manage (``edit_ad`` finds a pair's ad there).
        dry_run: default for :meth:`edit_ad` when the call does not decide itself.
    """

    def __init__(
        self,
        adapters: Mapping[str, "ExchangeAdapter"],
        settings: Settings,
        store: AdStore,
        dry_run: bool = False,
    ) -> None:
        self.adapters: dict[str, "ExchangeAdapter"] = {
            str(name).strip().lower(): adapter for name, adapter in adapters.items()
        }
        self.settings = settings
        self.store = store
        self.dry_run = bool(dry_run)
        #: exchanges switched off with ``DISABLED_EXCHANGES``: never read, never edited
        self.disabled_platforms: frozenset[str] = frozenset(
            getattr(settings, "disabled_platforms", frozenset())
        )
        self._sessions: set[str] = set()
        self._session_failures: dict[str, str] = {}
        self._dirty = False

    # -- editing -------------------------------------------------------------------
    def edit_ad(
        self,
        account_id: str,
        pair: Pair | str,
        adv_no: str | None = None,
        *,
        price: Decimal | str | int | None = None,
        price_floating_ratio: Decimal | str | int | None = None,
        min_amount: Decimal | str | int | None = None,
        max_amount: Decimal | str | int | None = None,
        quantity: Decimal | str | int | None = None,
        payment_methods: Sequence[str] | None = None,
        active: bool | None = None,
        dry_run: bool | None = None,
    ) -> PublishResult:
        """Change only the given fields of one live advertisement; never creates one.

        The ad is read from the venue first (:meth:`ExchangeAdapter.fetch_own_ads`), and
        every field that is not passed keeps its live value: side, fixed price or floating
        ratio, amounts, total quantity, payment methods and on/off state.

        Args:
            account_id: the account owning the advertisement, e.g. ``"Binance#1"``.
            pair: the advertised pair; it must match the live ad (a safety check).
            adv_no: the venue advertisement id; ``None`` uses the ledger's ad for ``pair``.
            price: new fixed price (a floating ad becomes a fixed one).
            price_floating_ratio: new floating ratio in percent of the reference price, e.g.
                ``91`` (Binance only; a fixed ad becomes a floating one).
            min_amount: new minimum order amount (fiat).
            max_amount: new maximum order amount (fiat).
            quantity: new total crypto amount of the ad.
            payment_methods: new payment method names.
            active: ``True`` switches the ad on (with any other changes); ``False`` only
                switches it off and cannot be combined with other changes.
            dry_run: overrides the constructor default; the ad is still read, but nothing
                is sent or stored.

        Only **buy** ads are handled: a sell ad is refused untouched.

        Refused (as a ``status="error"`` result, never raised): a sell, missing, closed or
        mismatched ad; both ``price`` and ``price_floating_ratio``; changes to an offline
        ad without ``active=True``.
        """
        effective_dry_run = self.dry_run if dry_run is None else bool(dry_run)
        pair = Pair.parse(pair)
        self._sessions.clear()
        self._session_failures.clear()
        self._dirty = False

        def failed(
            message: str,
            *,
            account: str = str(account_id),
            platform: str = "",
            edit_price: Decimal | None = None,
        ) -> PublishResult:
            _log.warning("edit %s %s refused: %s", account, pair.symbol, message)
            return PublishResult(
                account_id=account,
                platform=platform,
                pair=pair,
                status="error",
                price=edit_price,
                adv_no=adv_no,
                error=message,
                dry_run=effective_dry_run,
            )

        try:
            account = self.settings.account(account_id)
        except ConfigError as exc:
            return failed(str(exc))
        platform = account.platform
        if platform in self.disabled_platforms:
            return failed(
                f"{platform} is disabled (DISABLED_EXCHANGES)", platform=platform, account=account.id
            )
        adapter = self.adapters.get(platform)
        if adapter is None:
            return failed(
                f"no adapter registered for platform {platform!r}",
                platform=platform,
                account=account.id,
            )
        record = self.store.get(account.id, pair)
        adv_no = adv_no or (record.adv_no if record is not None else None)
        if not adv_no:
            return failed(
                f"no advertisement on record for {account.id} {pair.symbol}; pass adv_no",
                platform=platform,
                account=account.id,
            )

        try:
            live_ads = adapter.fetch_own_ads(account)
        except ExchangeError as exc:
            return failed(
                f"cannot read the advertisement: {type(exc).__name__}: {exc}",
                platform=platform,
                account=account.id,
            )
        live = next((ad for ad in live_ads if ad.adv_no == adv_no), None)
        try:
            if live is None:
                raise ConfigError(f"{account.id} has no advertisement {adv_no}")
            if live.pair != pair:
                raise ConfigError(f"advertisement {adv_no} is {live.pair.symbol}, not {pair.symbol}")
            if live.status == AD_STATUS_CLOSED:
                raise ConfigError(f"advertisement {adv_no} is closed")
            if live.side != SIDE_BUY:
                raise ConfigError(
                    f"advertisement {adv_no} is a {live.side or 'side-less'} ad; only buy ads "
                    "are handled"
                )
            spec = self._edited_spec(
                live,
                price=price,
                price_floating_ratio=price_floating_ratio,
                min_amount=min_amount,
                max_amount=max_amount,
                quantity=quantity,
                payment_methods=payment_methods,
                active=active,
            )
        except ConfigError as exc:
            return failed(str(exc), platform=platform, account=account.id)

        if effective_dry_run:
            return PublishResult(
                account_id=account.id,
                platform=platform,
                pair=pair,
                status="dry_run",
                price=spec.price,
                adv_no=adv_no,
                dry_run=True,
            )
        # remember the ad only when the ledger has none for this pair (or has this one)
        remember = record is None or record.adv_no == adv_no
        result = self._push(adapter, account, platform, pair, spec, adv_no, remember=remember)
        if self._dirty:
            self.store.save()
        return result

    def _edited_spec(
        self,
        live: OwnAd,
        *,
        price: Decimal | str | int | None,
        price_floating_ratio: Decimal | str | int | None,
        min_amount: Decimal | str | int | None,
        max_amount: Decimal | str | int | None,
        quantity: Decimal | str | int | None,
        payment_methods: Sequence[str] | None,
        active: bool | None,
    ) -> AdSpec:
        """The live ad with the requested changes applied; raises ``ConfigError`` to refuse."""
        changes = [
            value
            for value in (price, price_floating_ratio, min_amount, max_amount, quantity, payment_methods)
            if value is not None
        ]
        if active is False and changes:
            raise ConfigError("active=False only switches the ad off; change other fields separately")
        if active is None and not changes:
            raise ConfigError("nothing to change")
        if active is None and not live.active:
            raise ConfigError(
                f"advertisement {live.adv_no} is {live.status}; pass active=True to update it and "
                "bring it online"
            )
        if price is not None and price_floating_ratio is not None:
            raise ConfigError("pass either price or price_floating_ratio, not both")

        ratio = live.price_floating_ratio
        if price is not None:
            new_price = parse_decimal(price, "price")
            ratio = None
        elif price_floating_ratio is not None:
            ratio = parse_decimal(price_floating_ratio, "price_floating_ratio")
            if ratio <= 0:
                raise ConfigError(f"price_floating_ratio must be positive, got {ratio}")
            if live.price is None or live.price_floating_ratio is None:
                raise ConfigError(
                    f"advertisement {live.adv_no} has no floating price to rescale; its new price "
                    "cannot be estimated"
                )
            # informational: the venue prices a floating ad from its reference price
            new_price = live.price * ratio / live.price_floating_ratio
        elif live.price is not None:
            new_price = live.price
        else:
            raise ConfigError(f"the venue reports no price for advertisement {live.adv_no}")
        if new_price <= 0:
            raise ConfigError(f"price must be positive, got {new_price}")

        low = parse_decimal(min_amount, "min_amount") if min_amount is not None else live.min_amount
        high = parse_decimal(max_amount, "max_amount") if max_amount is not None else live.max_amount
        total = parse_decimal(quantity, "quantity") if quantity is not None else live.total_quantity
        if low is None or high is None:
            raise ConfigError(f"the venue reports no order limits for {live.adv_no}; pass both")
        if total is None:
            raise ConfigError(f"the venue reports no quantity for {live.adv_no}; pass quantity")
        if low <= 0:
            raise ConfigError(f"min_amount must be positive, got {low}")
        if high < low:
            raise ConfigError(f"max_amount {high} is below min_amount {low}")
        if total <= 0:
            raise ConfigError(f"quantity must be positive, got {total}")

        if payment_methods is not None:
            methods, payment_ids = tuple(str(method) for method in payment_methods), ()
        else:
            methods, payment_ids = (), live.payment_ids
        return AdSpec(
            pair=live.pair,
            price=new_price,
            min_amount=low,
            max_amount=high,
            payment_methods=methods,
            active=live.active if active is None else bool(active),
            side=live.side,
            quantity=total,
            payment_ids=payment_ids,
            price_floating_ratio=ratio,
        )

    # -- reading ---------------------------------------------------------------------
    def fetch_own_ads(
        self,
        account_ids: Sequence[str] | None = None,
        *,
        include_closed: bool = False,
    ) -> tuple[OwnAdsResult, ...]:
        """Every advertisement currently in the venue profiles, online and offline alike.

        Args:
            account_ids: accounts to read, e.g. ``["Binance#1"]``; ``None`` reads every
                configured account, ordered by platform then index.
            include_closed: also list ads the venue reports as closed/completed.

        This reads the venues, not the local ledger, so it also shows ads created by hand.
        Nothing is changed or stored. One result per account: a failing account (unknown,
        no adapter, a venue error) carries ``error`` and the others are still read. Accounts
        of an exchange in ``DISABLED_EXCHANGES`` are skipped (no result at all).
        """
        if account_ids is None:
            account_ids = [
                account.id
                for platform in PLATFORMS
                if platform not in self.disabled_platforms
                for account in self.settings.accounts_for(platform)
            ]
        results: list[OwnAdsResult] = []
        for account_id in account_ids:
            try:
                account = self.settings.account(account_id)
            except ConfigError as exc:
                results.append(OwnAdsResult(account_id=str(account_id), platform="", error=str(exc)))
                continue
            if account.platform in self.disabled_platforms:
                _log.debug("skipped %s: %s is disabled", account.id, account.platform)
                continue
            adapter = self.adapters.get(account.platform)
            if adapter is None:
                results.append(
                    OwnAdsResult(
                        account_id=account.id,
                        platform=account.platform,
                        error=f"no adapter registered for platform {account.platform!r}",
                    )
                )
                continue
            try:
                ads = adapter.fetch_own_ads(account)
            except ExchangeError as exc:
                _log.warning("listing the ads of %s failed: %s", account.id, exc)
                results.append(
                    OwnAdsResult(
                        account_id=account.id,
                        platform=account.platform,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if not include_closed:
                ads = tuple(ad for ad in ads if ad.status != AD_STATUS_CLOSED)
            results.append(OwnAdsResult(account_id=account.id, platform=account.platform, ads=ads))
        return tuple(results)

    # -- internals -----------------------------------------------------------------
    def _push(
        self,
        adapter: "ExchangeAdapter",
        account: Any,
        platform: str,
        pair: Pair,
        spec: AdSpec,
        adv_no: str,
        *,
        remember: bool = True,
    ) -> PublishResult:
        """Send one update request and (unless ``remember`` is false) record the outcome."""
        failure = self._session_failures.get(account.id)
        if failure is not None:
            return PublishResult(
                account_id=account.id,
                platform=platform,
                pair=pair,
                status="error",
                price=spec.price,
                adv_no=adv_no,
                error=failure,
            )

        try:
            self._ensure_session(adapter, account)
            request = adapter.build_update_ad_request(account, spec, adv_no)
            action = adapter.parse_ad_result(
                adapter.send_private(account, request),
                account=account,
                pair=pair,
                spec=spec,
            )
        except ExchangeError as exc:
            _log.warning("%s %s failed: %s", account.id, pair.symbol, exc)
            return PublishResult(
                account_id=account.id,
                platform=platform,
                pair=pair,
                status="error",
                price=spec.price,
                adv_no=adv_no,
                error=f"{type(exc).__name__}: {exc}",
            )

        stored_adv_no = action.adv_no or adv_no
        if remember:
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
        _log.info("updated %s %s at %s (adv %s)", account.id, pair.symbol, spec.price, stored_adv_no)
        return PublishResult(
            account_id=account.id,
            platform=platform,
            pair=pair,
            status="updated",
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
