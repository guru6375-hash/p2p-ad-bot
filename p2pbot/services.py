"""Composition root and the façade the Telegram layer talks to.

This is the only module that instantiates concrete components; everything else receives
its collaborators. ``BotServices`` is intentionally duck-typed so the Telegram layer can be
developed and tested against a stub.

The bot does two things:

* ``/getads`` - :meth:`BotServices.get_own_ads` lists every enabled account's live ads.
* ``/setrate`` - :meth:`BotServices.set_uah_rate` reprices the UAH buy ads as a ladder
  (``uah_config.STEP``) and :meth:`BotServices.set_pln_rate` sets one flat price on the
  PLN buy ads.

The scheduler, the cron job, the price engine and the stored base/cap rates were moved to
``archive/`` (see ``archive/README.md``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from .config import Settings
from .constants import VERSION
from .edit_queue import EditQueueReport, run_pln_rate_queue, run_uah_rate_queue
from .exchanges import build_adapters
from .exchanges.base import Transport
from .models import OwnAdsResult, utcnow
from .publisher import AdPublisher, AdStore
from .uah_config import load_uah_steps

__all__ = ["BotServices", "build_services"]


class BotServices:
    """The single façade handed to the Telegram layer."""

    def __init__(
        self,
        settings: Settings,
        adapters: Mapping[str, Any],
        publisher: AdPublisher,
        *,
        clock: Callable[[], datetime] = utcnow,
        version: str = VERSION,
        uah_steps: Mapping[str, Decimal] | None = None,
    ) -> None:
        self.settings = settings
        self.adapters = adapters
        self.publisher = publisher
        self.clock = clock
        self.version = version
        #: validated ``uah_config.STEP``: the UAH ladder goes one STEP lower per ad
        self.uah_steps = dict(uah_steps) if uah_steps is not None else load_uah_steps()

    def get_own_ads(self) -> tuple[OwnAdsResult, ...]:
        """``/getads``: every enabled account's live ads (online and offline, not closed)."""
        return self.publisher.fetch_own_ads()

    def set_uah_rate(self, rate: Decimal | str, *, dry_run: bool = False) -> EditQueueReport:
        """``/setrate`` UAH: per account, the online buy UAH/USDT ads become ``rate``,
        ``rate - STEP``, ``rate - 2*STEP``, ... and the UAH/USDC ads the same ladder one STEP
        lower."""
        return run_uah_rate_queue(rate, self.publisher, steps=self.uah_steps, dry_run=dry_run)

    def set_pln_rate(self, rate: Decimal | str, *, dry_run: bool = False) -> EditQueueReport:
        """``/setrate`` PLN: every online buy PLN/USDT and PLN/USDC ad is set to ``rate``."""
        return run_pln_rate_queue(rate, self.publisher, dry_run=dry_run)


def build_services(
    settings: Settings,
    transport: Transport | None = None,
    *,
    clock: Callable[[], datetime] = utcnow,
    adapters: Mapping[str, Any] | None = None,
    uah_steps: Mapping[str, Decimal] | None = None,
) -> BotServices:
    """Wire every component from ``settings``.

    ``uah_steps`` defaults to the validated ``uah_config.py`` setting; a bad setting raises
    :class:`ConfigError` here, before anything runs.
    """
    resolved_adapters = dict(adapters) if adapters is not None else build_adapters(transport)
    publisher = AdPublisher(resolved_adapters, settings, AdStore.load(settings.ads_path))
    return BotServices(
        settings,
        resolved_adapters,
        publisher,
        clock=clock,
        uah_steps=uah_steps if uah_steps is not None else load_uah_steps(),
    )
