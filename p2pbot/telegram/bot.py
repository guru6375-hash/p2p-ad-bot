"""Poll loop wiring ``TelegramAPI`` + :mod:`p2pbot.telegram.security` + ``Dispatcher``.

Responsibilities, in order: authorize the update, audit refusals (silently - a refused
sender never receives an answer), dispatch the command and send the reply text (with its
inline buttons). A button press is acknowledged (``answerCallbackQuery``) and the message
that carried the button is rewritten, which removes the buttons so they cannot be pressed
twice.

The loop never dies on a network fault: transport/API failures are logged and retried
with exponential backoff (1s -> 30s cap), and the well-known ``401``/``409`` failures get
explicit operator guidance.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .. import constants
from ..errors import TelegramError, TransportError
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
    if message is None and update.callback is not None:
        message = update.callback.message  # the chat the button was pressed in
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
        self._dispatcher = Dispatcher(services, logger=self._logger)
        self._offset: int | None = None

    # ----- wiring -------------------------------------------------------------------

    @property
    def offset(self) -> int | None:
        """Next ``update_id`` requested from the Bot API (``None`` before the first poll)."""
        return self._offset

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
        callback = update.callback if update.message is None else None
        if callback is not None:
            self._logger.info("update %s: button %s", update.update_id, callback.data or "?")
            self._answer(callback.id)
        elif update.message is not None:
            head = (update.message.text or "").strip().split(" ", 1)[0]
            self._logger.info(
                "update %s: %s", update.update_id, head if head.startswith("/") else "text"
            )
        result = self._dispatcher.dispatch(update)
        if result is None or result.silent:
            return result
        chat_id = _chat_id(update)
        if chat_id is None:
            self._logger.warning("update %s has no chat id; reply dropped", update.update_id)
            return result
        pressed = None if callback is None else callback.message
        if result.edit and pressed is not None and pressed.message_id is not None:
            self._edit(chat_id, pressed.message_id, result.edit)
        self._send(chat_id, result.text, result.buttons)
        return result

    def _reply(self, update: Update, text: str) -> HandlerResult | None:
        chat_id = _chat_id(update)
        if chat_id is None:
            return None
        self._send(chat_id, text)
        return HandlerResult(text)

    def _send(self, chat_id: int, text: str, buttons: Any = None) -> None:
        try:
            if buttons:
                self._api.send_message(chat_id, text, buttons)
            else:
                self._api.send_message(chat_id, text)
        except (TelegramError, TransportError) as exc:
            self._logger.error("sendMessage to chat %s failed: %s", chat_id, exc)

    def _answer(self, callback_id: str) -> None:
        try:
            self._api.answer_callback_query(callback_id)
        except (TelegramError, TransportError) as exc:
            self._logger.warning("answerCallbackQuery failed: %s", exc)

    def _edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._api.edit_message_text(chat_id, message_id, text)
        except (TelegramError, TransportError) as exc:
            self._logger.warning("editMessageText in chat %s failed: %s", chat_id, exc)

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

        Every iteration is one ``getUpdates`` call. On a
        transport/API failure the loop sleeps with exponential backoff (1s -> 30s) and
        retries; it never spins without sleeping and never propagates the failure to the
        caller. ``sleep`` is injectable so tests and the smoke harness run instantly.
        """
        delay = _BACKOFF_START_SECONDS
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
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
