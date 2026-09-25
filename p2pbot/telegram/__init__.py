"""Telegram layer: owner-only long-polling bot (stdlib only).

Public surface::

    from p2pbot.telegram import BotRunner, Dispatcher, TelegramAPI

The layer depends on exactly ONE duck-typed façade object (SPEC 11.5, built by
``p2pbot.services.build_services``) and imports it only under ``if TYPE_CHECKING:``:
``p2pbot.services`` / ``p2pbot.publisher`` are never imported here, so
this package keeps working while those modules evolve.
"""

from __future__ import annotations

from .api import CallbackQuery, Message, TelegramAPI, TelegramApiError, Update, split_message
from .bot import BotRunner
from .handlers import HELP_TEXT, Dispatcher, HandlerResult
from .security import AccessController, Decision, RateLimiter, audit, has_attachments, is_owner

__all__ = [
    "AccessController",
    "BotRunner",
    "CallbackQuery",
    "Decision",
    "Dispatcher",
    "HELP_TEXT",
    "HandlerResult",
    "Message",
    "RateLimiter",
    "TelegramAPI",
    "TelegramApiError",
    "Update",
    "audit",
    "has_attachments",
    "is_owner",
    "split_message",
]
