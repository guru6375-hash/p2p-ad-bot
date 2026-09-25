"""Owner-only access policy: sender gate, attachment rejection and rate limiting.

Policy (SPEC 11.1), enforced before any command is dispatched and before any Telegram
call is made:

* only ``TELEGRAM_OWNER_ID`` may interact (messages and inline-button presses alike);
  anyone else is dropped **without a reply**
  and written to the audit log;
* non-private chats (group/supergroup/channel) are refused even for the owner;
* any message carrying an upload is refused - the bot never accepts files, so rejection
  happens before dispatch and before any file could be fetched;
* more than ``RATE_LIMIT_MAX_MESSAGES`` messages per ``RATE_LIMIT_WINDOW_SECONDS``
  (sliding window) are refused for the remainder of the window.

Refusals are silent for anything that is not a reply-worthy rate-limit violation, so a
stranger (or a group) can never make the bot emit a message; the reason is recorded in
the audit log instead.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from .. import constants

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .api import Message, Update

__all__ = [
    "AccessController",
    "Decision",
    "RATE_LIMIT_REPLY",
    "RateLimiter",
    "audit",
    "has_attachments",
    "is_owner",
]

#: Reply sent when the owner exceeds the message rate (SPEC 11.1 wording).
RATE_LIMIT_REPLY = "Too many requests."


@dataclass(frozen=True)
class Decision:
    """Result of :meth:`AccessController.authorize`."""

    allow: bool
    reason: str
    silent: bool


def is_owner(update: "Update", owner_id: int | None) -> bool:
    """True only when the update was sent by ``owner_id``.

    A missing configuration (``owner_id is None``) denies everyone, and an update with no
    sender (channel posts, service updates) is never the owner. A button press counts as
    sent by whoever pressed it.
    """
    if owner_id is None:
        return False
    sender = _sender(update)
    if sender is None:
        return False
    try:
        return int(sender) == int(owner_id)
    except (TypeError, ValueError):
        return False


def _sender(update: "Update") -> Any:
    """Who sent the message, or pressed the button, of ``update`` (``None`` when unknown)."""
    message = getattr(update, "message", None)
    if message is not None:
        return getattr(message, "from_id", None)
    callback = getattr(update, "callback", None)
    if callback is not None:
        return getattr(callback, "from_id", None)
    return None


def has_attachments(message: "Message | None") -> bool:
    """True when the message carries an upload (detected by payload key, never fetched)."""
    return bool(getattr(message, "attachment_keys", ()))


class RateLimiter:
    """Sliding-window message limiter, one window per user id.

    A rejected call does **not** consume the window, so a blocked sender cannot extend
    their own block by retrying.
    """

    def __init__(
        self,
        max_messages: int = constants.RATE_LIMIT_MAX_MESSAGES,
        window_seconds: float = constants.RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_messages = int(max_messages)
        self.window_seconds = float(window_seconds)
        self._clock = clock if clock is not None else time.monotonic
        self._hits: dict[int, deque[float]] = {}

    def _recent(self, user_id: int, now: float) -> deque[float]:
        hits = self._hits.setdefault(user_id, deque())
        while hits and now - hits[0] >= self.window_seconds:
            hits.popleft()
        return hits

    def check(self, user_id: int) -> bool:
        """Record a message for ``user_id``; False when the window is full."""
        key = int(user_id)
        now = self._clock()
        hits = self._recent(key, now)
        if len(hits) >= self.max_messages:
            return False
        hits.append(now)
        return True

    def allow_count(self, user_id: int) -> int:
        """Messages currently inside the window (diagnostics/tests)."""
        return len(self._recent(int(user_id), self._clock()))


class AccessController:
    """Combines the owner gate, chat-type gate, attachment gate and rate limiter."""

    def __init__(self, owner_id: int | None, limiter: RateLimiter | None = None) -> None:
        self.owner_id = owner_id
        self.limiter = limiter if limiter is not None else RateLimiter()

    def authorize(self, update: "Update") -> Decision:
        """Return the decision for ``update``; ``reason`` is a stable short string."""
        message = getattr(update, "message", None)
        callback = getattr(update, "callback", None)
        if message is None and callback is None:
            return Decision(allow=False, reason="no-message", silent=True)
        if not is_owner(update, self.owner_id):
            return Decision(allow=False, reason="not-owner", silent=True)
        # a button press is judged by the chat of the message that carries the button
        chat = message if message is not None else getattr(callback, "message", None)
        chat_type = str(getattr(chat, "chat_type", "") or "").lower()
        if chat_type != "private":
            return Decision(allow=False, reason="non-private-chat", silent=True)
        if message is not None and has_attachments(message):
            return Decision(allow=False, reason="attachment", silent=True)
        sender = int(_sender(update))  # is_owner() above proved it is an integer
        if not self.limiter.check(sender):
            return Decision(allow=False, reason="rate-limit", silent=False)
        return Decision(allow=True, reason="allow", silent=False)


def audit(logger: logging.Logger, update: "Update", decision: Decision) -> None:
    """Write one audit line for a refused update; never logs contents or secrets."""
    if decision.allow:
        return
    sender: Any = _sender(update)
    logger.warning(
        "audit: rejected %s update=%s from=%s",
        decision.reason,
        getattr(update, "update_id", "?"),
        "?" if sender is None else sender,
    )
