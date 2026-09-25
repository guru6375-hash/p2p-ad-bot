"""Poll loop: authorization, replies, button presses, offset bookkeeping, backoff.

Every Telegram call is an injected :class:`~tests.conftest.FakeTransport` request, so the
suite never opens a socket and the assertions read the real request objects.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from p2pbot.errors import TelegramError, TransportError
from p2pbot.telegram.api import CallbackQuery, Message, TelegramAPI, Update
from p2pbot.telegram.bot import BotRunner
from p2pbot.telegram.handlers import HELP_TEXT, HandlerResult
from p2pbot.telegram.security import AccessController, RateLimiter

from conftest import (
    FakeClock,
    FakeTransport,
    MonotonicFakeClock,
    StubServices,
    json_response,
)

TOKEN = "123456:SECRET-TOKEN"
BASE = "https://api.telegram.org"
OWNER = 4242
UTC = timezone.utc


def _update(
    text: str = "/help",
    *,
    from_id: int = OWNER,
    chat_id: int | None = 42,
    chat_type: str = "private",
    attachments: tuple[str, ...] = (),
    message: bool = True,
    update_id: int = 1,
) -> Update:
    if not message:
        return Update(update_id=update_id, raw={})
    return Update(
        update_id=update_id,
        message=Message(
            message_id=1,
            chat_id=chat_id,
            chat_type=chat_type,
            from_id=from_id,
            text=text,
            attachment_keys=attachments,
        ),
        raw={},
    )


@pytest.fixture
def api(transport: FakeTransport, http_request_bridge) -> TelegramAPI:
    return TelegramAPI(BASE, TOKEN, transport=transport)


@pytest.fixture
def runner(stub_services: StubServices, api: TelegramAPI) -> BotRunner:
    return BotRunner(stub_services, api)


def _sent_texts(transport: FakeTransport) -> list[str]:
    return [
        request.json_body["text"]
        for request in transport.requests
        if str(request.url).endswith("/sendMessage")
    ]


# -- authorization ---------------------------------------------------------------------
def test_non_owner_update_produces_no_reply_at_all(
    runner: BotRunner, transport: FakeTransport
) -> None:
    assert runner.handle_update(_update("/status", from_id=999)) is None
    assert transport.requests == []


def test_non_owner_attachment_produces_no_reply(runner: BotRunner, transport: FakeTransport) -> None:
    assert runner.handle_update(_update("/status", from_id=999, attachments=("document",))) is None
    assert transport.requests == []


def test_owner_attachment_is_refused_silently(runner: BotRunner, transport: FakeTransport) -> None:
    assert runner.handle_update(_update("/status", attachments=("photo",))) is None
    assert transport.requests == []


def test_group_chat_is_refused_even_for_the_owner(runner: BotRunner, transport: FakeTransport) -> None:
    assert runner.handle_update(_update("/status", chat_type="group")) is None
    assert transport.requests == []


def test_update_without_a_message_is_ignored(runner: BotRunner, transport: FakeTransport) -> None:
    assert runner.handle_update(_update(message=False)) is None
    assert transport.requests == []


def test_rate_limited_owner_gets_the_fixed_reply(
    stub_services: StubServices, api: TelegramAPI, transport: FakeTransport
) -> None:
    limiter = RateLimiter(max_messages=1, window_seconds=60, clock=MonotonicFakeClock())
    runner = BotRunner(stub_services, api, access=AccessController(OWNER, limiter=limiter))

    transport.push_json({"ok": True, "result": {"message_id": 1}})
    transport.push_json({"ok": True, "result": {"message_id": 2}})
    assert runner.handle_update(_update("/help")) is not None
    result = runner.handle_update(_update("/help"))
    assert result is not None
    assert result.text == "Too many requests."
    assert _sent_texts(transport)[-1] == "Too many requests."


# -- dispatch and reply ----------------------------------------------------------------
def test_owner_command_is_dispatched_and_replied(
    runner: BotRunner, transport: FakeTransport
) -> None:
    transport.push_json({"ok": True, "result": {"message_id": 7}})
    result = runner.handle_update(_update("/help"))
    assert result is not None
    assert result.text == HELP_TEXT
    request = transport.last_request
    assert request.method == "POST"
    assert request.url == f"{BASE}/bot{TOKEN}/sendMessage"
    assert request.json_body == {"chat_id": 42, "text": HELP_TEXT}


def test_update_without_a_chat_id_is_not_sent(
    runner: BotRunner, transport: FakeTransport
) -> None:
    result = runner.handle_update(_update("/help", chat_id=None))
    assert result is not None
    assert transport.requests == []


def test_a_failing_send_does_not_break_the_loop(
    runner: BotRunner, transport: FakeTransport
) -> None:
    transport.push_error(TransportError("connection reset"))
    result = runner.handle_update(_update("/help"))
    assert result is not None
    assert result.text == HELP_TEXT


def test_silent_handler_result_is_not_sent(
    stub_services: StubServices, api: TelegramAPI, transport: FakeTransport
) -> None:
    """A silent handler result must produce no ``sendMessage`` for that update."""
    transport.push_json({"ok": True, "result": {"message_id": 1}})
    runner = BotRunner(stub_services, api)
    monkeypatched = HandlerResult("handled silently", silent=True)
    assert runner.handle_update(_update("/help")) is not None
    assert len(_sent_texts(transport)) == 1

    class _SilentDispatcher:
        def dispatch(self, update: Update) -> HandlerResult:
            return monkeypatched

    runner._dispatcher = _SilentDispatcher()  # noqa: SLF001 - forced silent result
    transport.reset()
    assert runner.handle_update(_update("/help")) == monkeypatched
    assert transport.requests == []


def test_owner_id_is_read_from_the_settings_façade(api: TelegramAPI, transport: FakeTransport) -> None:
    services = SimpleNamespace(
        version="9.9",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        settings=SimpleNamespace(owner_id=77),
        scheduler=None,
    )
    runner = BotRunner(services, api)
    transport.push_json({"ok": True, "result": {}})
    assert runner.handle_update(_update("/help", from_id=77)) is not None
    transport.reset()
    assert runner.handle_update(_update("/help", from_id=OWNER)) is None
    assert transport.requests == []


def test_missing_owner_configuration_denies_everyone(api: TelegramAPI, transport: FakeTransport) -> None:
    services = SimpleNamespace(
        version="9.9",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        settings=SimpleNamespace(telegram_owner_id=None),
        scheduler=None,
    )
    runner = BotRunner(services, api)
    assert runner.handle_update(_update("/help")) is None
    assert transport.requests == []


# -- registration ----------------------------------------------------------------------
def test_register_commands_publishes_the_command_table(
    runner: BotRunner, transport: FakeTransport
) -> None:
    from p2pbot import constants

    transport.push_json({"ok": True, "result": True})
    assert runner.register_commands() is True
    commands = transport.last_request.json_body["commands"]
    assert [item["command"] for item in commands] == [name for name, _ in constants.TELEGRAM_COMMANDS]


def test_register_commands_failure_is_not_fatal(
    runner: BotRunner, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    transport.push_error(TransportError("offline"))
    with caplog.at_level(logging.WARNING):
        assert runner.register_commands() is False
    assert "setMyCommands failed" in caplog.text


# -- inline buttons --------------------------------------------------------------------
def _press(data: str, *, from_id: int = OWNER, chat_type: str = "private") -> Update:
    return Update(
        update_id=3,
        callback=CallbackQuery(
            id="cb-9",
            from_id=from_id,
            data=data,
            message=Message(message_id=55, chat_id=42, chat_type=chat_type, from_id=1),
        ),
    )


def _calls(transport: FakeTransport) -> list[tuple[str, dict]]:
    return [(str(request.url).rsplit("/", 1)[-1], request.json_body) for request in transport.requests]


def test_setrate_is_sent_with_the_market_buttons(runner: BotRunner, transport: FakeTransport) -> None:
    transport.push_json({"ok": True, "result": {"message_id": 55}})

    runner.handle_update(_update("/setrate"))

    assert _calls(transport) == [
        (
            "sendMessage",
            {
                "chat_id": 42,
                "text": "💱 Set rate · pick a market",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "🇺🇦 UAH", "callback_data": "setrate:uah"},
                            {"text": "🇵🇱 PLN", "callback_data": "setrate:pln"},
                        ]
                    ]
                },
            },
        )
    ]


def test_a_press_is_answered_the_buttons_removed_and_the_rate_asked(
    stub_services: StubServices, runner: BotRunner, transport: FakeTransport
) -> None:
    for _ in range(3):
        transport.push_json({"ok": True, "result": True})

    runner.handle_update(_press("setrate:pln"))

    names = [name for name, _ in _calls(transport)]
    assert names == ["answerCallbackQuery", "editMessageText", "sendMessage"]
    calls = dict(_calls(transport))
    assert calls["answerCallbackQuery"] == {"callback_query_id": "cb-9"}
    assert calls["editMessageText"] == {"chat_id": 42, "message_id": 55, "text": "💱 Set rate · 🇵🇱 PLN"}
    assert calls["sendMessage"]["text"].startswith("🇵🇱 Send the PLN rate")

    transport.push_json({"ok": True, "result": {"message_id": 56}})
    runner.handle_update(_update("3.85"))
    assert stub_services.setrate_calls[-1]["market"] == "pln"


def test_a_stranger_pressing_a_button_gets_nothing(
    stub_services: StubServices, runner: BotRunner, transport: FakeTransport
) -> None:
    assert runner.handle_update(_press("setrate:uah", from_id=999)) is None
    assert runner.handle_update(_press("setrate:uah", chat_type="group")) is None
    assert transport.requests == []


def test_a_failing_callback_answer_does_not_stop_the_reply(
    runner: BotRunner, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    transport.push_error(TransportError("offline"))
    transport.push_json({"ok": True, "result": True})
    transport.push_json({"ok": True, "result": True})
    with caplog.at_level(logging.WARNING):
        result = runner.handle_update(_press("setrate:uah"))
    assert result is not None
    assert "answerCallbackQuery failed" in caplog.text
    assert _sent_texts(transport)[-1].startswith("🇺🇦 Send the UAH rate")


# -- loop ------------------------------------------------------------------------------
def test_run_forever_tracks_the_offset_and_dispatches(
    runner: BotRunner, transport: FakeTransport
) -> None:
    transport.push_json(
        {"ok": True, "result": [{"update_id": 5}, {"update_id": 7}]}
    )
    transport.push_json({"ok": True, "result": []})

    iterations = runner.run_forever(max_iterations=2, sleep=lambda seconds: None)

    assert iterations == 2
    polls = [request for request in transport.requests if str(request.url).endswith("/getUpdates")]
    assert len(polls) == 2
    assert "offset" not in polls[0].json_body
    assert polls[1].json_body["offset"] == 8
    assert runner.offset == 8


def test_run_forever_handles_updates_inside_the_loop(
    runner: BotRunner, transport: FakeTransport
) -> None:
    transport.push_json(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": OWNER},
                        "text": "/help",
                    },
                }
            ],
        }
    )
    transport.push_json({"ok": True, "result": {"message_id": 9}})
    runner.run_forever(max_iterations=1, sleep=lambda seconds: None)
    assert _sent_texts(transport) == [HELP_TEXT]


def test_run_forever_retries_with_exponential_backoff(
    runner: BotRunner, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    sleeps: list[float] = []
    transport.push_error(TelegramError("boom-1"))
    transport.push_error(TelegramError("boom-2"))
    transport.push_error(TelegramError("boom-3"))
    transport.push_json({"ok": True, "result": []})

    with caplog.at_level(logging.WARNING):
        runner.run_forever(max_iterations=4, sleep=sleeps.append)

    # three failures back off 1s -> 2s -> 4s, then the successful (idle) poll naps
    assert [value for value in sleeps if value >= 1.0] == [1.0, 2.0, 4.0]
    assert len(sleeps) == 4
    assert "retrying in 1s" in caplog.text
    assert "retrying in 4s" in caplog.text


def test_an_empty_poll_sleeps_briefly_instead_of_spinning(
    runner: BotRunner, transport: FakeTransport
) -> None:
    sleeps: list[float] = []
    transport.push_json({"ok": True, "result": []})
    runner.run_forever(max_iterations=1, sleep=sleeps.append)
    assert len(sleeps) == 1
    assert 0 < sleeps[0] < 1.0


def test_a_poll_with_updates_does_not_idle_sleep(
    runner: BotRunner, transport: FakeTransport
) -> None:
    sleeps: list[float] = []
    transport.push_json({"ok": True, "result": [{"update_id": 3}]})
    runner.run_forever(max_iterations=1, sleep=sleeps.append)
    assert sleeps == []


def test_backoff_resets_after_a_successful_poll(runner: BotRunner, transport: FakeTransport) -> None:
    sleeps: list[float] = []
    transport.push_error(TelegramError("boom"))
    transport.push_json({"ok": True, "result": []})
    transport.push_error(TelegramError("boom-again"))
    transport.push_json({"ok": True, "result": []})

    runner.run_forever(max_iterations=4, sleep=sleeps.append)
    assert [value for value in sleeps if value >= 1.0] == [1.0, 1.0]


def test_backoff_is_capped_at_thirty_seconds(runner: BotRunner, transport: FakeTransport) -> None:
    sleeps: list[float] = []
    for _ in range(7):
        transport.push_error(TelegramError("down"))
    runner.run_forever(max_iterations=7, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_unauthorized_poll_failure_gets_operator_guidance(
    runner: BotRunner, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    api_error = TelegramError("unauthorized")
    api_error.status = 401
    transport.push_error(api_error)
    transport.push_json({"ok": True, "result": []})
    with caplog.at_level(logging.ERROR):
        runner.run_forever(max_iterations=2, sleep=lambda seconds: None)
    assert "TELEGRAM_BOT_TOKEN is missing or was revoked" in caplog.text


def test_conflict_poll_failure_gets_operator_guidance(
    runner: BotRunner, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    api_error = TelegramError("conflict")
    api_error.status = 409
    transport.push_error(api_error)
    transport.push_json({"ok": True, "result": []})
    with caplog.at_level(logging.ERROR):
        runner.run_forever(max_iterations=2, sleep=lambda seconds: None)
    assert "another process" in caplog.text


def test_other_poll_failures_are_logged_as_warnings(
    runner: BotRunner, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    transport.push_error(TelegramError("glitch"))
    transport.push_json({"ok": True, "result": []})
    with caplog.at_level(logging.WARNING):
        runner.run_forever(max_iterations=2, sleep=lambda seconds: None)
    assert "poll failed: glitch" in caplog.text


def test_run_forever_returns_zero_iterations(runner: BotRunner, transport: FakeTransport) -> None:
    assert runner.run_forever(max_iterations=0, sleep=lambda seconds: None) == 0
    assert transport.requests == []


def test_offset_starts_unset_and_survives_empty_polls(
    runner: BotRunner, transport: FakeTransport
) -> None:
    assert runner.offset is None
    transport.push_json({"ok": True, "result": []})
    runner.run_forever(max_iterations=1, sleep=lambda seconds: None)
    assert runner.offset is None


def test_owner_id_that_is_not_a_number_denies_everyone(api: TelegramAPI, transport: FakeTransport) -> None:
    services = SimpleNamespace(
        version="9.9",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        settings=SimpleNamespace(owner_id="not-a-number"),
        scheduler=None,
    )
    runner = BotRunner(services, api)
    assert runner.handle_update(_update("/help")) is None
    assert transport.requests == []


def test_rate_limit_refusal_without_a_chat_id_is_not_sent(
    stub_services: StubServices, api: TelegramAPI, transport: FakeTransport
) -> None:
    limiter = RateLimiter(max_messages=0, window_seconds=60, clock=MonotonicFakeClock())
    runner = BotRunner(stub_services, api, access=AccessController(OWNER, limiter=limiter))
    assert runner.handle_update(_update("/help", chat_id=None)) is None
    assert transport.requests == []
