"""Telegram Bot API client: payload shapes, splitting, token safety (SPEC section 11.3).

Every test injects :class:`~tests.conftest.FakeTransport`, so no socket is ever opened.
The ``http_request_bridge`` fixture keeps the client's lazy ``HttpRequest`` import working
while the exchange adapter package is still being completed.
"""

from __future__ import annotations

import json

import pytest

from p2pbot import constants
from p2pbot.errors import TelegramError, TransportError
from p2pbot.telegram.api import (
    CallbackQuery,
    Message,
    TelegramAPI,
    TelegramApiError,
    Update,
    attachment_keys,
    split_message,
)

from conftest import FakeTransport, json_response, text_response

TOKEN = "123456:SECRET-TOKEN"
BASE = "https://api.telegram.org"


def _api(transport: FakeTransport, **kwargs) -> TelegramAPI:
    return TelegramAPI(BASE, TOKEN, transport=transport, **kwargs)


# -- split_message ---------------------------------------------------------------------
def test_split_message_leaves_short_text_alone() -> None:
    assert split_message("hello") == ["hello"]


def test_split_message_of_empty_text_is_empty() -> None:
    assert split_message("") == []


def test_split_message_prefers_line_boundaries_and_keeps_every_character() -> None:
    text = "\n".join(["a" * 30, "b" * 30, "c" * 30])
    chunks = split_message(text, limit=70)
    assert all(len(chunk) <= 70 for chunk in chunks)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 30 + "\n" + "b" * 30
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_split_message_hard_splits_a_single_long_line() -> None:
    chunks = split_message("x" * 250, limit=100)
    assert [len(chunk) for chunk in chunks] == [100, 100, 50]
    assert "".join(chunks) == "x" * 250


def test_split_message_uses_the_telegram_limit_by_default() -> None:
    text = "y" * (constants.TELEGRAM_MAX_MESSAGE_LENGTH + 10)
    chunks = split_message(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == constants.TELEGRAM_MAX_MESSAGE_LENGTH


# -- attachment detection --------------------------------------------------------------
@pytest.mark.parametrize("key", constants.ATTACHMENT_KEYS)
def test_every_policy_attachment_key_is_detected(key: str) -> None:
    assert attachment_keys({key: {"file_id": "x"}}) == (key,)


def test_falsy_attachment_values_are_not_reported() -> None:
    assert attachment_keys({"document": None, "photo": [], "text": "hi"}) == ()


def test_unknown_file_id_keys_fail_closed() -> None:
    assert attachment_keys({"video_thumbnail_file_id": "abc"}) == ("video_thumbnail_file_id",)


def test_plain_text_payload_has_no_attachments() -> None:
    assert attachment_keys({"text": "hello", "message_id": 5}) == ()


# -- payload parsing -------------------------------------------------------------------
def test_message_from_payload_reads_only_the_allowed_fields() -> None:
    message = Message.from_payload(
        {
            "message_id": "7",
            "chat": {"id": -100, "type": "private"},
            "from": {"id": 42, "username": "owner"},
            "text": "/status",
            "photo": [{"file_id": "p1"}],
        }
    )
    assert message.message_id == 7
    assert message.chat_id == -100
    assert message.chat_type == "private"
    assert message.from_id == 42
    assert message.text == "/status"
    assert message.attachment_keys == ("photo",)


def test_message_from_payload_tolerates_odd_shapes() -> None:
    message = Message.from_payload({"chat": "nope", "from": None, "text": 5, "message_id": "x"})
    assert (message.chat_id, message.from_id, message.text, message.message_id) == (None, None, "", None)
    assert Message.from_payload("not-a-mapping") == Message()  # type: ignore[arg-type]


def test_update_from_payload() -> None:
    update = Update.from_payload(
        {"update_id": 99, "message": {"message_id": 1, "chat": {"id": 5, "type": "private"}, "text": "/x"}}
    )
    assert update.update_id == 99
    assert update.message is not None
    assert update.message.text == "/x"
    assert update.raw["update_id"] == 99


def test_update_from_payload_without_a_message_or_id() -> None:
    update = Update.from_payload({"channel_post": {"text": "hi"}})
    assert update.update_id == 0
    assert update.message is None
    assert update.callback is None


def test_a_button_press_is_parsed_as_a_callback_query() -> None:
    update = Update.from_payload(
        {
            "update_id": 5,
            "callback_query": {
                "id": "4382",
                "from": {"id": 4242},
                "data": "setrate:uah",
                "message": {"message_id": 9, "chat": {"id": 42, "type": "private"}, "text": "pick"},
            },
        }
    )
    assert update.message is None
    assert update.callback == CallbackQuery(
        id="4382",
        from_id=4242,
        data="setrate:uah",
        message=Message(message_id=9, chat_id=42, chat_type="private", text="pick"),
    )


def test_an_odd_callback_query_is_tolerated() -> None:
    callback = Update.from_payload({"callback_query": {"data": 5, "message": "x"}}).callback
    assert callback == CallbackQuery()


# -- constructor -----------------------------------------------------------------------
def test_api_requires_base_url_and_token() -> None:
    with pytest.raises(TelegramError, match="base url must not be empty"):
        TelegramAPI("", TOKEN)
    with pytest.raises(TelegramError, match="token must not be empty"):
        TelegramAPI(BASE, "")


def test_api_repr_never_leaks_the_token() -> None:
    api = _api(FakeTransport())
    assert TOKEN not in repr(api)
    assert "***" in repr(api)
    assert api.base_url == BASE  # trailing slashes are normalised


# -- calls -----------------------------------------------------------------------------
def test_get_me_posts_the_documented_request(transport: FakeTransport, http_request_bridge) -> None:
    transport.push_json({"ok": True, "result": {"id": 1, "username": "bot"}})
    api = _api(transport)
    assert api.get_me() == {"id": 1, "username": "bot"}

    request = transport.last_request
    assert request.method == "POST"
    assert request.url == f"{BASE}/bot{TOKEN}/getMe"
    assert request.json_body == {}


def test_get_me_of_a_non_dict_result_is_empty(transport: FakeTransport, http_request_bridge) -> None:
    transport.push_json({"ok": True, "result": [1, 2]})
    assert _api(transport).get_me() == {}


def test_get_updates_sends_offset_and_timeout(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json(
        {
            "ok": True,
            "result": [
                {"update_id": 10, "message": {"message_id": 1, "chat": {"id": 5, "type": "private"}, "text": "/a"}},
                "junk",
            ],
        }
    )
    api = _api(transport)
    updates = api.get_updates(offset=7, timeout=1)
    assert [update.update_id for update in updates] == [10]
    payload = transport.last_request.json_body
    assert payload == {"timeout": 1, "allowed_updates": ["message", "callback_query"], "offset": 7}
    assert transport.last_request.url.endswith("/getUpdates")


def test_get_updates_without_offset_omits_it(transport: FakeTransport, http_request_bridge) -> None:
    transport.push_json({"ok": True, "result": []})
    assert _api(transport).get_updates() == ()
    payload = transport.last_request.json_body
    assert "offset" not in payload
    assert payload["timeout"] == constants.TELEGRAM_POLL_TIMEOUT_SECONDS


def test_get_updates_of_a_non_list_result_is_empty(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json({"ok": True, "result": {"unexpected": True}})
    assert _api(transport).get_updates(timeout=1) == ()


def test_send_message_sends_one_request_for_a_short_text(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json({"ok": True, "result": {"message_id": 3}})
    result = _api(transport).send_message(42, "hello")
    assert result == {"message_id": 3}
    assert transport.last_request.json_body == {"chat_id": 42, "text": "hello"}


def test_send_message_puts_the_buttons_under_the_last_chunk(
    transport: FakeTransport, http_request_bridge
) -> None:
    for _ in range(2):
        transport.push_json({"ok": True, "result": {"message_id": 3}})
    text = "\n".join(["x" * 100] * 60)  # two chunks

    _api(transport).send_message(42, text, [[("A", "a"), ("B", "b")]])

    first, last = (request.json_body for request in transport.requests)
    assert "reply_markup" not in first
    assert last["reply_markup"] == {
        "inline_keyboard": [[{"text": "A", "callback_data": "a"}, {"text": "B", "callback_data": "b"}]]
    }


def test_answer_callback_query_and_edit_message_text(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json({"ok": True, "result": True})
    transport.push_json({"ok": True, "result": True})
    transport.push_json({"ok": True, "result": True})
    api = _api(transport)

    assert api.answer_callback_query("4382") is True
    assert api.answer_callback_query("4383", "done") is True
    assert api.edit_message_text(42, 9, "picked") is True

    calls = [(str(r.url).rsplit("/", 1)[-1], r.json_body) for r in transport.requests]
    assert calls == [
        ("answerCallbackQuery", {"callback_query_id": "4382"}),
        ("answerCallbackQuery", {"callback_query_id": "4383", "text": "done"}),
        ("editMessageText", {"chat_id": 42, "message_id": 9, "text": "picked"}),
    ]


def test_send_message_splits_a_long_text_into_several_calls(
    transport: FakeTransport, http_request_bridge
) -> None:
    text = "z" * (constants.TELEGRAM_MAX_MESSAGE_LENGTH + 5)
    transport.push_json({"ok": True, "result": {"message_id": 1}})
    transport.push_json({"ok": True, "result": {"message_id": 2}})
    result = _api(transport).send_message(42, text)
    assert result == {"message_id": 2}
    bodies = [request.json_body["text"] for request in transport.requests]
    assert len(bodies) == 2
    assert "".join(bodies) == text


def test_set_my_commands_normalises_names(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json({"ok": True, "result": True})
    assert _api(transport).set_my_commands([("/Start", "usage"), ("rates", "table")]) is True
    assert transport.last_request.json_body == {
        "commands": [
            {"command": "start", "description": "usage"},
            {"command": "rates", "description": "table"},
        ]
    }


# -- failures --------------------------------------------------------------------------
def test_http_error_becomes_a_telegram_api_error_without_the_token(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json(
        {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, status=400
    )
    api = _api(transport)
    with pytest.raises(TelegramApiError) as excinfo:
        api.send_message(42, "hi")
    error = excinfo.value
    assert error.status == 400
    assert error.error_code == 400
    assert error.method == "sendMessage"
    assert "chat not found" in str(error)
    assert TOKEN not in str(error)


def test_ok_false_with_http_200_is_still_an_error(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json({"ok": False, "error_code": 401, "description": "Unauthorized"})
    with pytest.raises(TelegramApiError) as excinfo:
        _api(transport).get_me()
    assert excinfo.value.error_code == 401
    assert excinfo.value.status == 200


def test_conflict_is_reported_with_its_error_code(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json(
        {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates"},
        status=409,
    )
    with pytest.raises(TelegramApiError) as excinfo:
        _api(transport).get_updates(timeout=1)
    assert excinfo.value.error_code == 409
    assert excinfo.value.status == 409


def test_non_json_body_is_reported(transport: FakeTransport, http_request_bridge) -> None:
    transport.push_text("<html>gateway error</html>", status=502)
    with pytest.raises(TelegramApiError, match="non-JSON response") as excinfo:
        _api(transport).get_me()
    assert "<html>" in str(excinfo.value)


def test_empty_body_is_reported(transport: FakeTransport, http_request_bridge) -> None:
    transport.push(text_response("", status=500))
    with pytest.raises(TelegramApiError):
        _api(transport).get_me()


def test_transport_failure_is_reraised_with_the_token_redacted(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_error(TransportError(f"POST {BASE}/bot{TOKEN}/getMe failed: timed out"))
    with pytest.raises(TransportError) as excinfo:
        _api(transport).get_me()
    assert TOKEN not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_json_body_with_a_bad_success_shape_is_reported(
    transport: FakeTransport, http_request_bridge
) -> None:
    transport.push_json([1, 2, 3])
    with pytest.raises(TelegramApiError, match="non-JSON response"):
        _api(transport).get_me()


def test_poll_timeout_is_used_for_get_updates(transport: FakeTransport, http_request_bridge) -> None:
    transport.push_json({"ok": True, "result": []})
    _api(transport, poll_timeout=3).get_updates()
    assert transport.last_request.json_body["timeout"] == 3


# -- default (no injected transport) sender --------------------------------------------
class _FakeHTTPResponse:
    """Minimal stand-in for the object ``urllib.request.urlopen`` yields."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_api_without_a_transport_posts_json_over_urllib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stdlib sender is the default when no transport is injected (offline fake)."""
    import urllib.request

    from p2pbot.telegram import api as api_module

    recorded: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        recorded["url"] = request.full_url
        recorded["data"] = request.data
        recorded["method"] = request.get_method()
        recorded["timeout"] = timeout
        return _FakeHTTPResponse(200, json.dumps({"ok": True, "result": {"id": 7}}).encode())

    monkeypatch.setattr(api_module.urllib.request, "urlopen", fake_urlopen)
    assert urllib.request.urlopen is fake_urlopen

    api = TelegramAPI(BASE, TOKEN)
    assert api.get_me() == {"id": 7}
    assert recorded["url"] == f"{BASE}/bot{TOKEN}/getMe"
    assert recorded["data"] == b"{}"
    assert recorded["method"] == "POST"
    assert recorded["timeout"] == 40.0


def test_urllib_sender_honours_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from p2pbot.telegram import api as api_module

    recorded: list[float | None] = []

    def fake_urlopen(request, timeout=None):
        recorded.append(timeout)
        return _FakeHTTPResponse(200, json.dumps({"ok": True, "result": {}}).encode())

    monkeypatch.setattr(api_module.urllib.request, "urlopen", fake_urlopen)
    TelegramAPI(BASE, TOKEN, timeout=7).get_me()
    TelegramAPI(BASE, TOKEN, timeout=0.0).get_me()
    assert recorded == [7.0, 0.0]


def test_http_error_from_urllib_becomes_a_telegram_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error
    from io import BytesIO

    from p2pbot.telegram import api as api_module

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            409,
            "Conflict",
            {},
            BytesIO(b'{"ok": false, "error_code": 409, "description": "Conflict"}'),
        )

    monkeypatch.setattr(api_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TelegramApiError) as excinfo:
        TelegramAPI(BASE, TOKEN).get_updates(timeout=1)
    assert excinfo.value.status == 409
    assert excinfo.value.error_code == 409
    assert TOKEN not in str(excinfo.value)


def test_url_error_from_urllib_becomes_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    from p2pbot.telegram import api as api_module

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr(api_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TransportError) as excinfo:
        TelegramAPI(BASE, TOKEN).get_me()
    assert "getaddrinfo failed" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_socket_level_oserror_becomes_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from p2pbot.telegram import api as api_module

    def fake_urlopen(request, timeout=None):
        raise TimeoutError("the read operation timed out")

    monkeypatch.setattr(api_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TransportError, match="the read operation timed out"):
        TelegramAPI(BASE, TOKEN).get_me()
