"""Poll loop wiring ``TelegramAPI`` + :mod:`p2pbot.telegram.security` + ``Dispatcher``.

Responsibilities, in order: authorize the update, audit refusals (silently - a refused
sender never receives an answer), dispatch the command and send the reply text. Scheduler
jobs run once per poll iteration so the market parser keeps firing while the bot polls.

The loop never dies on a network fault: transport/API failures are logged and retried
with exponential backoff (1s -> 30s cap), and the well-known ``401``/``409`` failures get
explicit operator guidance.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable

from .. import constants
from ..errors import TelegramError, TransportError
from ..models import utcnow
from . import security
from .api import TelegramAPI, Update
from .handlers import Dispatcher, HandlerResult

__all__ = ["BotRunner"]

_LOGGER = logging.getLogger(__name__)

_BACKOFF_START_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0

#: Pause after an *empty* successful poll. ``getUpdates`` normally long-polls for the full
#: timeout, so an instant empty answer means the server (or a stub) declined to block:
#: sleeping briefly keeps the loop from spinning without sleeping.
_IDLE_SLEEP_SECONDS = 0.25

#: Operator guidance for the two failures that are configuration problems, not glitches.
_STATUS_GUIDANCE: dict[int, str] = {
    401: (
        "401 Unauthorized - TELEGRAM_BOT_TOKEN is missing or was revoked. "
        "Check .env (or ask BotFather for a new token) and restart the bot."
    ),
    409: (
        "409 Conflict - another process (or a getUpdates webhook) already consumes this "
        "bot token. Stop the other instance, or call deleteWebhook, then restart."
    ),
}


def _owner_id(services: Any) -> int | None:
    """Read the configured owner id from the façade settings, tolerating field naming."""
    settings = getattr(services, "settings", None)
    for attribute in ("owner_id", "telegram_owner_id", "owner"):
        value = getattr(settings, attribute, None)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _chat_id(update: Update) -> int | None:
    message = update.message
    return None if message is None else message.chat_id


class BotRunner:
    """Owner-only long-polling bot loop."""

    def __init__(
        self,
        services: Any,
        api: TelegramAPI,
        access: security.AccessController | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._services = services
        self._api = api
        self._logger = logger if logger is not None else _LOGGER
        self._access = (
            access
            if access is not None
            else security.AccessController(_owner_id(services), limiter=security.RateLimiter())
        )
        clock = getattr(services, "clock", None)
        self._dispatcher = Dispatcher(
            services,
            logger=self._logger,
            clock=clock if callable(clock) else None,
        )
        self._scheduler = getattr(services, "scheduler", None)
        self._offset: int | None = None

    # ----- wiring -------------------------------------------------------------------

    @property
    def offset(self) -> int | None:
        """Next ``update_id`` requested from the Bot API (``None`` before the first poll)."""
        return self._offset

    def attach_scheduler(self, scheduler: Any) -> None:
        """Route ``scheduler`` into the loop: its due jobs run once per poll iteration."""
        self._scheduler = scheduler

    def register_commands(self) -> bool:
        """Publish the command list to Telegram; failures are logged, never fatal."""
        try:
            return bool(self._api.set_my_commands(constants.TELEGRAM_COMMANDS))
        except (TelegramError, TransportError) as exc:
            self._logger.warning("setMyCommands failed: %s", exc)
            return False

    # ----- one update ---------------------------------------------------------------

    def handle_update(self, update: Update) -> HandlerResult | None:
        """Authorize, dispatch and reply for one update (``None`` when there is no reply)."""
        decision = self._access.authorize(update)
        if not decision.allow:
            security.audit(self._logger, update, decision)
            if decision.silent:
                return None
            # Only the rate-limit refusal is answered; every other refusal is silent.
            return self._reply(update, security.RATE_LIMIT_REPLY)
        result = self._dispatcher.dispatch(update)
        if result is None or result.silent:
            return result
        chat_id = _chat_id(update)
        if chat_id is None:
            self._logger.warning("update %s has no chat id; reply dropped", update.update_id)
            return result
        self._send(chat_id, result.text)
        return result

    def _reply(self, update: Update, text: str) -> HandlerResult | None:
        chat_id = _chat_id(update)
        if chat_id is None:
            return None
        self._send(chat_id, text)
        return HandlerResult(text)

    def _send(self, chat_id: int, text: str) -> None:
        try:
            self._api.send_message(chat_id, text)
        except (TelegramError, TransportError) as exc:
            self._logger.error("sendMessage to chat %s failed: %s", chat_id, exc)

    # ----- scheduler ----------------------------------------------------------------

    def _now(self) -> datetime:
        clock = getattr(self._services, "clock", None)
        return clock() if callable(clock) else utcnow()

    def run_due_jobs(self) -> int:
        """Run the scheduler's due jobs once; returns how many jobs ran."""
        scheduler = self._scheduler
        if scheduler is None or not hasattr(scheduler, "run_due"):
            return 0
        try:
            executed = scheduler.run_due(self._now())
        except Exception as exc:  # a failing job must not kill the poll loop
            self._logger.error("scheduler run_due failed: %s: %s", type(exc).__name__, exc)
            return 0
        return len(executed) if isinstance(executed, (list, tuple)) else 0

    # ----- loop ---------------------------------------------------------------------

    def _log_poll_failure(self, exc: BaseException, delay: float) -> None:
        status = getattr(exc, "status", None)
        guidance = _STATUS_GUIDANCE.get(status) if isinstance(status, int) else None
        if guidance:
            self._logger.error("poll failed: %s :: %s retrying in %.0fs", exc, guidance, delay)
        else:
            self._logger.warning("poll failed: %s; retrying in %.0fs", exc, delay)

    def run_forever(
        self,
        *,
        max_iterations: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        """Long-poll until interrupted (or ``max_iterations`` polls); returns the count.

        Every iteration runs the due scheduler jobs, then one ``getUpdates`` call. On a
        transport/API failure the loop sleeps with exponential backoff (1s -> 30s) and
        retries; it never spins without sleeping and never propagates the failure to the
        caller. ``sleep`` is injectable so tests and the smoke harness run instantly.
        """
        delay = _BACKOFF_START_SECONDS
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            self.run_due_jobs()
            try:
                updates = self._api.get_updates(self._offset)
            except (TelegramError, TransportError) as exc:
                self._log_poll_failure(exc, delay)
                sleep(delay)
                delay = min(delay * 2.0, _BACKOFF_MAX_SECONDS)
            else:
                delay = _BACKOFF_START_SECONDS
                if updates:
                    self._offset = max(update.update_id for update in updates) + 1
                    for update in updates:
                        self.handle_update(update)
                else:
                    sleep(_IDLE_SLEEP_SECONDS)
            iterations += 1
        return iterations
