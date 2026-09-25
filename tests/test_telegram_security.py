"""Owner-only access policy: sender gate, attachments, chat type, rate limit (SPEC 11.1)."""

from __future__ import annotations

import logging

import pytest

from p2pbot import constants
from p2pbot.telegram import security
from p2pbot.telegram.api import CallbackQuery, Message, Update
from p2pbot.telegram.security import (
    RATE_LIMIT_REPLY,
    AccessController,
    Decision,
    RateLimiter,
    audit,
    has_attachments,
    is_owner,
)

from conftest import MonotonicFakeClock

OWNER = 4242


def _update(
    *,
    from_id: int | None = OWNER,
    chat_type: str = "private",
    text: str = "/status",
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
            chat_id=from_id,
            chat_type=chat_type,
            from_id=from_id,
            text=text,
            attachment_keys=attachments,
        ),
        raw={},
    )


def _press(*, from_id: int | None = OWNER, chat_type: str = "private") -> Update:
    return Update(
        update_id=2,
        callback=CallbackQuery(
            id="cb",
            from_id=from_id,
            data="setrate:uah",
            message=Message(message_id=5, chat_id=42, chat_type=chat_type, from_id=1),
        ),
    )


# -- inline-button presses -------------------------------------------------------------
def test_a_press_by_the_owner_in_a_private_chat_is_allowed() -> None:
    access = AccessController(OWNER)
    assert is_owner(_press(), OWNER) is True
    assert access.authorize(_press()) == Decision(allow=True, reason="allow", silent=False)


def test_a_press_is_judged_by_who_pressed_not_who_sent_the_message() -> None:
    access = AccessController(OWNER)
    assert access.authorize(_press(from_id=999)).reason == "not-owner"
    assert access.authorize(_press(from_id=None)).reason == "not-owner"
    assert access.authorize(_press(chat_type="group")).reason == "non-private-chat"


def test_presses_count_towards_the_rate_limit() -> None:
    access = AccessController(OWNER, limiter=RateLimiter(max_messages=1, window_seconds=60))
    assert access.authorize(_press()).allow is True
    assert access.authorize(_press()).reason == "rate-limit"


def test_audit_names_the_presser(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        audit(logging.getLogger("t"), _press(from_id=999), Decision(False, "not-owner", True))
    assert "from=999" in caplog.text


# -- owner gate ------------------------------------------------------------------------
def test_is_owner_only_for_the_configured_sender() -> None:
    assert is_owner(_update(), OWNER) is True
    assert is_owner(_update(from_id=999), OWNER) is False
    assert is_owner(_update(), None) is False
    assert is_owner(_update(message=False), OWNER) is False


def test_is_owner_rejects_updates_without_a_sender() -> None:
    update = Update(message=Message(message_id=1, chat_type="private", from_id=None))
    assert is_owner(update, OWNER) is False


def test_is_owner_compares_numerically() -> None:
    update = Update(message=Message(message_id=1, chat_type="private", from_id=OWNER))
    assert is_owner(update, OWNER) is True
    broken = Update(message=Message(message_id=1, chat_type="private", from_id=OWNER))
    object.__setattr__(broken.message, "from_id", "not-a-number")
    assert is_owner(broken, OWNER) is False


# -- attachments -----------------------------------------------------------------------
def test_has_attachments_reflects_the_detected_payload_keys() -> None:
    assert has_attachments(Message(attachment_keys=("photo",))) is True
    assert has_attachments(Message(attachment_keys=())) is False
    assert has_attachments(None) is False


# -- rate limiter ----------------------------------------------------------------------
def test_rate_limiter_allows_the_limit_then_blocks() -> None:
    clock = MonotonicFakeClock()
    limiter = RateLimiter(max_messages=3, window_seconds=60, clock=clock)
    assert [limiter.check(1) for _ in range(3)] == [True, True, True]
    assert limiter.check(1) is False
    assert limiter.allow_count(1) == 3


def test_rate_limiter_window_resets_after_the_window() -> None:
    clock = MonotonicFakeClock()
    limiter = RateLimiter(max_messages=2, window_seconds=60, clock=clock)
    limiter.check(1)
    clock.advance(30)
    limiter.check(1)
    assert limiter.check(1) is False

    clock.advance(30)  # first hit is 60s old now -> slides out
    assert limiter.check(1) is True
    clock.advance(31)
    assert limiter.check(1) is True


def test_rate_limiter_rejection_does_not_extend_the_block() -> None:
    clock = MonotonicFakeClock()
    limiter = RateLimiter(max_messages=1, window_seconds=10, clock=clock)
    assert limiter.check(1) is True
    for _ in range(5):
        clock.advance(1)
        assert limiter.check(1) is False
    clock.advance(5)  # t = 10 -> the single recorded hit expires
    assert limiter.check(1) is True


def test_rate_limiter_is_per_user() -> None:
    clock = MonotonicFakeClock()
    limiter = RateLimiter(max_messages=1, window_seconds=60, clock=clock)
    assert limiter.check(1) is True
    assert limiter.check(1) is False
    assert limiter.check(2) is True


def test_rate_limiter_defaults_match_the_policy_constants() -> None:
    limiter = RateLimiter()
    assert limiter.max_messages == constants.RATE_LIMIT_MAX_MESSAGES
    assert limiter.window_seconds == constants.RATE_LIMIT_WINDOW_SECONDS


# -- access controller -----------------------------------------------------------------
def test_missing_message_is_silently_refused() -> None:
    decision = AccessController(OWNER).authorize(_update(message=False))
    assert decision == Decision(allow=False, reason="no-message", silent=True)


def test_non_owner_is_silently_refused() -> None:
    decision = AccessController(OWNER).authorize(_update(from_id=999))
    assert decision.allow is False
    assert decision.reason == "not-owner"
    assert decision.silent is True


def test_unconfigured_owner_refuses_everyone() -> None:
    assert AccessController(None).authorize(_update()).allow is False


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", "", "GROUP"])
def test_non_private_chats_are_refused_even_for_the_owner(chat_type: str) -> None:
    decision = AccessController(OWNER).authorize(_update(chat_type=chat_type))
    assert decision.allow is False
    assert decision.reason == "non-private-chat"
    assert decision.silent is True


@pytest.mark.parametrize("key", constants.ATTACHMENT_KEYS)
def test_every_attachment_key_is_refused(key: str) -> None:
    decision = AccessController(OWNER).authorize(_update(attachments=(key,)))
    assert decision == Decision(allow=False, reason="attachment", silent=True)


def test_attachment_refusal_wins_over_the_rate_limit() -> None:
    clock = MonotonicFakeClock()
    limiter = RateLimiter(max_messages=1, window_seconds=60, clock=clock)
    controller = AccessController(OWNER, limiter=limiter)
    assert controller.authorize(_update()).allow is True
    decision = controller.authorize(_update(attachments=("document",)))
    assert decision.reason == "attachment"
    assert decision.silent is True


def test_rate_limit_refusal_is_the_only_answered_refusal() -> None:
    clock = MonotonicFakeClock()
    limiter = RateLimiter(max_messages=2, window_seconds=60, clock=clock)
    controller = AccessController(OWNER, limiter=limiter)
    assert controller.authorize(_update()).allow is True
    assert controller.authorize(_update()).allow is True
    decision = controller.authorize(_update())
    assert decision.allow is False
    assert decision.reason == "rate-limit"
    assert decision.silent is False
    assert RATE_LIMIT_REPLY == "Too many requests."


def test_allowed_decision_for_the_owner_in_a_private_chat() -> None:
    decision = AccessController(OWNER).authorize(_update())
    assert decision == Decision(allow=True, reason="allow", silent=False)


# -- audit -----------------------------------------------------------------------------
def test_audit_logs_refusals_with_reason_and_sender(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("p2pbot.audit-test")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        audit(logger, _update(from_id=999, update_id=7), Decision(False, "not-owner", True))
    assert "audit: rejected not-owner" in caplog.text
    assert "update=7" in caplog.text
    assert "from=999" in caplog.text


def test_audit_says_nothing_for_an_allowed_update(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("p2pbot.audit-test-allowed")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        audit(logger, _update(), Decision(True, "allow", False))
    assert caplog.text == ""


def test_audit_handles_an_update_without_a_message(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("p2pbot.audit-test-nomessage")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        audit(logger, _update(message=False, update_id=3), Decision(False, "no-message", True))
    assert "audit: rejected no-message" in caplog.text
    assert "from=?" in caplog.text


def test_security_module_exports_its_public_surface() -> None:
    assert security.__all__ == [
        "AccessController",
        "Decision",
        "RATE_LIMIT_REPLY",
        "RateLimiter",
        "audit",
        "has_attachments",
        "is_owner",
    ]
