"""Stdlib Telegram Bot API client for the owner-only bot.

Every call is a JSON ``POST {base_url}/bot{token}/{method}``, the shape the Bot API
accepts for ``getMe``, ``getUpdates``, ``sendMessage`` and ``setMyCommands``.

Design notes
------------
* ``base_url`` is the configurable ``TELEGRAM_API_BASE`` (``https://api.telegram.org`` by
  default) so tests and the smoke harness can point the client at a local stub server.
* A ``Transport`` (``p2pbot.exchanges.base``) may be injected; it is imported lazily so
  this package never depends on the exchange/market layer in order to import. When no
  transport is given, a ~40-line urllib sender is used, because the Telegram client needs
  nothing but JSON POSTs.
* The bot token is redacted (``constants.REDACTED``) from every error and log message:
  the ``/bot<token>/`` path segment never leaves this module.
* Text longer than ``constants.TELEGRAM_MAX_MESSAGE_LENGTH`` is split on line boundaries
  and sent as several ``sendMessage`` calls.
* Uploads are only ever *detected* (payload key names). There is deliberately no
  ``getFile``/download counterpart in this client.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .. import constants
from ..errors import TelegramError, TransportError

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the runtime import graph small
    from ..exchanges.base import Transport

__all__ = [
    "Message",
    "TelegramAPI",
    "TelegramApiError",
    "Update",
    "attachment_keys",
    "split_message",
]

_LOGGER = logging.getLogger(__name__)


class TelegramApiError(TelegramError):
    """The Bot API answered with a non-2xx status or ``{"ok": false}``.

    ``status``/``error_code`` are kept as attributes so the poll loop can add guidance
    for the well-known failures (401 credentials, 409 concurrent ``getUpdates``).
    """

    def __init__(
        self,
        message: str,
        *,
        method: str = "",
        status: int | None = None,
        error_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.status = status
        self.error_code = error_code


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def attachment_keys(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Names of the payload keys that mark an upload. File contents are never read.

    Detection uses :data:`p2pbot.constants.ATTACHMENT_KEYS` plus any present key ending
    in ``_file_id`` so a venue/key we did not enumerate still fails closed.
    """
    found: list[str] = []
    for key in constants.ATTACHMENT_KEYS:
        if payload.get(key):
            found.append(key)
    for key, value in payload.items():
        if isinstance(key, str) and key.endswith("_file_id") and value and key not in found:
            found.append(key)
    return tuple(found)


@dataclass(frozen=True)
class Message:
    """The subset of a Telegram message the bot is allowed to look at."""

    message_id: int | None = None
    chat_id: int | None = None
    chat_type: str = ""
    from_id: int | None = None
    text: str = ""
    attachment_keys: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Message":
        """Build a message from a raw payload, tolerating missing/odd fields."""
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        chat = data.get("chat")
        sender = data.get("from")
        chat_data: Mapping[str, Any] = chat if isinstance(chat, Mapping) else {}
        sender_data: Mapping[str, Any] = sender if isinstance(sender, Mapping) else {}
        text = data.get("text")
        return cls(
            message_id=_int_or_none(data.get("message_id")),
            chat_id=_int_or_none(chat_data.get("id")),
            chat_type=str(chat_data.get("type") or ""),
            from_id=_int_or_none(sender_data.get("id")),
            text=text if isinstance(text, str) else "",
            attachment_keys=attachment_keys(data),
        )


@dataclass(frozen=True)
class Update:
    """One long-polling update; ``raw`` keeps the untouched payload for diagnostics."""

    update_id: int = 0
    message: Message | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Update":
        """Build an update from a raw payload, tolerating missing/odd fields."""
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        raw_message = data.get("message")
        message = Message.from_payload(raw_message) if isinstance(raw_message, Mapping) else None
        update_id = _int_or_none(data.get("update_id"))
        return cls(update_id=0 if update_id is None else update_id, message=message, raw=data)


def split_message(text: str, limit: int = constants.TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters, preferring line breaks.

    A single line longer than ``limit`` is hard-split, so the result never exceeds the
    Bot API size limit and no characters are dropped.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        for piece in [line[index : index + limit] for index in range(0, len(line), limit)] or [""]:
            candidate = piece if not current else f"{current}\n{piece}"
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


class _UrllibSender:
    """Minimal JSON-POST sender used when no ``Transport`` is injected."""

    def __init__(self, default_timeout: float) -> None:
        self._default_timeout = float(default_timeout)

    def post(self, url: str, payload: Mapping[str, Any], timeout: float) -> tuple[int, bytes]:
        """POST a JSON body; HTTP error bodies are returned, socket faults raise."""
        body = json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": constants.DEFAULT_USER_AGENT,
            },
        )
        effective = float(timeout) if timeout and timeout > 0 else self._default_timeout
        try:
            with urllib.request.urlopen(request, timeout=effective) as response:
                return int(getattr(response, "status", None) or response.getcode()), response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except OSError:  # pragma: no cover - a broken error body is not actionable
                raw = b""
            return int(exc.code), raw
        except urllib.error.URLError as exc:
            raise TransportError(f"POST {url} failed: {exc.reason}") from exc
        except OSError as exc:  # socket timeout, connection reset
            raise TransportError(f"POST {url} failed: {exc}") from exc


class TelegramAPI:
    """Long-polling client for the subset of the Bot API this project uses."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: "Transport | None" = None,
        *,
        poll_timeout: int = constants.TELEGRAM_POLL_TIMEOUT_SECONDS,
        timeout: float = 40.0,
    ) -> None:
        if not base_url:
            raise TelegramError("Telegram API base url must not be empty")
        if not token:
            raise TelegramError("Telegram bot token must not be empty")
        self.base_url = base_url.rstrip("/")
        self.poll_timeout = int(poll_timeout)
        self.timeout = float(timeout)
        self._token = token
        self._transport = transport
        self._sender = _UrllibSender(self.timeout)

    def __repr__(self) -> str:
        return f"TelegramAPI(base_url={self.base_url!r}, token={constants.REDACTED})"

    # ----- transport ----------------------------------------------------------------

    def _method_url(self, method: str) -> str:
        return f"{self.base_url}/bot{self._token}/{method}"

    def _redact(self, text: str) -> str:
        return text.replace(self._token, constants.REDACTED) if text else text

    def _send(self, url: str, payload: Mapping[str, Any], timeout: float) -> tuple[int, bytes]:
        """Deliver one request, re-raising transport faults with the token redacted."""
        try:
            if self._transport is not None:
                # Local import: the exchange layer drags in the market layer, which the
                # Telegram layer must not need in order to import or run.
                from ..exchanges.base import HttpRequest

                request = HttpRequest(method="POST", url=url, json_body=dict(payload))
                response = self._transport.send(request, timeout=timeout)
                return int(response.status), bytes(response.body)
            return self._sender.post(url, payload, timeout)
        except TransportError as exc:
            raise TransportError(self._redact(str(exc))) from exc

    def _error(
        self,
        method: str,
        status: int,
        data: Mapping[str, Any] | None,
        detail: str = "",
    ) -> TelegramApiError:
        error_code = data.get("error_code") if isinstance(data, Mapping) else None
        raw_description = data.get("description") if isinstance(data, Mapping) else None
        description = raw_description if isinstance(raw_description, str) else ""
        message = f"Telegram {method} failed: HTTP {status}"
        if error_code is not None:
            message += f" (error_code={error_code})"
        if description:
            message += f": {description}"
        if detail:
            message += f": {detail}"
        return TelegramApiError(
            self._redact(message),
            method=method,
            status=status,
            error_code=_int_or_none(error_code),
        )

    def _call(self, method: str, payload: Mapping[str, Any], timeout: float) -> Any:
        url = self._method_url(method)
        _LOGGER.debug("telegram %s -> %s", method, self._redact(url))
        status, body = self._send(url, payload, timeout)
        text = body.decode("utf-8", errors="replace")
        try:
            data: Any = json.loads(text) if text.strip() else {}
        except ValueError:
            data = None
        if not isinstance(data, Mapping):
            raise self._error(method, status, None, f"non-JSON response: {text[:200]!r}")
        if not 200 <= status < 300 or data.get("ok") is not True:
            raise self._error(method, status, data)
        return data.get("result")

    # ----- Bot API methods ----------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        """``getMe``: identity of the bot account (used as a token/connectivity check)."""
        result = self._call("getMe", {}, self.timeout)
        return dict(result) if isinstance(result, Mapping) else {}

    def get_updates(self, offset: int | None = None, timeout: int | None = None) -> tuple[Update, ...]:
        """``getUpdates``: long poll for new updates, skipping everything below ``offset``."""
        wait = self.poll_timeout if timeout is None else int(timeout)
        payload: dict[str, Any] = {"timeout": wait, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = int(offset)
        result = self._call("getUpdates", payload, max(self.timeout, wait + 10.0))
        if not isinstance(result, (list, tuple)):
            return ()
        return tuple(Update.from_payload(item) for item in result if isinstance(item, Mapping))

    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """``sendMessage``: send ``text``, split on line boundaries when it is too long."""
        result: dict[str, Any] = {}
        for chunk in split_message(text, constants.TELEGRAM_MAX_MESSAGE_LENGTH):
            raw = self._call("sendMessage", {"chat_id": int(chat_id), "text": chunk}, self.timeout)
            result = dict(raw) if isinstance(raw, Mapping) else {}
        return result

    def set_my_commands(self, commands: Sequence[tuple[str, str]]) -> bool:
        """``setMyCommands``: publish the command list Telegram shows in the client."""
        payload = {
            "commands": [
                {"command": str(name).strip().lstrip("/").lower(), "description": str(description)}
                for name, description in commands
            ]
        }
        self._call("setMyCommands", payload, self.timeout)
        return True
