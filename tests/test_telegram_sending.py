"""Tests for the Telegram send-with-unlimited-retry wrapper (spec: Telegram
reporting is mandatory and must survive a temporary network outage).

Uses asyncio.run() directly (no pytest-asyncio dependency), matching this
project's existing convention in test_telegram_handlers.py.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
)

from faraz_honar.telegram_bot.sending import (
    DEFAULT_RETRY_DELAY_SECONDS,
    TELEGRAM_SAFE_MESSAGE_CHARS,
    _is_retryable,
    broadcast_long_message_with_retry,
    broadcast_message_with_retry,
    send_message_with_retry,
    split_message_text,
)


def _run(coro):
    return asyncio.run(coro)


def _bot(side_effect) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=side_effect)
    return bot


def _multi_chat_bot(behaviors: dict[int, list]) -> MagicMock:
    """A bot whose send_message behavior depends on the chat_id kwarg —
    each chat_id has its own queue of behaviors (an Exception instance to
    raise, or any other value to return), consumed one per call for that
    chat_id. Needed because broadcast_message_with_retry sends to several
    chat_ids concurrently through the same bot object.
    """
    remaining = {chat_id: list(queue) for chat_id, queue in behaviors.items()}

    async def _send_message(*, chat_id: int, **_kwargs):
        queue = remaining[chat_id]
        behavior = queue.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=_send_message)
    return bot


# -- Classification: temporary connectivity failure vs. permanent error ---------------


def test_network_error_is_retryable():
    assert _is_retryable(NetworkError("connection reset")) is True


def test_timed_out_is_retryable():
    assert _is_retryable(TimedOut()) is True


def test_retry_after_is_retryable():
    assert _is_retryable(RetryAfter(3)) is True


def test_raw_connection_error_is_retryable():
    assert _is_retryable(ConnectionError("boom")) is True


def test_raw_timeout_error_is_retryable():
    assert _is_retryable(TimeoutError("boom")) is True


def test_bad_request_is_not_retryable_even_though_it_is_a_network_error_subclass():
    # BadRequest is structurally a NetworkError subclass in
    # python-telegram-bot, but semantically a permanent, request-level
    # error (invalid parameters/malformed payload) that must never be
    # retried forever — preserve the existing (no-retry) behavior for it.
    assert issubclass(BadRequest, NetworkError)
    assert _is_retryable(BadRequest("invalid parameter")) is False


def test_forbidden_is_not_retryable():
    assert _is_retryable(Forbidden("bot was blocked")) is False


def test_invalid_token_is_not_retryable():
    assert _is_retryable(InvalidToken("bad token")) is False


def test_conflict_is_not_retryable():
    assert _is_retryable(Conflict("terminated by other getUpdates")) is False


def test_chat_migrated_is_not_retryable():
    assert _is_retryable(ChatMigrated(12345)) is False


def test_unrelated_exception_is_not_retryable():
    assert _is_retryable(ValueError("not a telegram error at all")) is False


# -- 1/2/3: retries continue through repeated network failures, no final failure,
#    no fixed maximum retry count -----------------------------------------------------


def test_retries_through_several_network_errors_then_succeeds():
    sent = MagicMock()
    bot = _bot([NetworkError("a"), NetworkError("b"), NetworkError("c"), sent])

    result = _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert result is sent
    assert bot.send_message.await_count == 4  # 3 failures + the succeeding call


def test_network_errors_never_produce_a_final_failure():
    # Even many consecutive failures must not raise -- the call only
    # returns once Telegram actually responds successfully.
    sent = MagicMock()
    bot = _bot([NetworkError("x")] * 10 + [sent])

    result = _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert result is sent


def test_no_fixed_maximum_retry_count():
    # A number of failures well beyond any platform client's max_retry_
    # attempts (2, per core/pricing.py's neighbors in this project) must
    # still eventually succeed -- proving there is no hardcoded attempt cap
    # for Telegram sends, unlike the generic platform-API retry system.
    many_failures = 47
    sent = MagicMock()
    bot = _bot([NetworkError("x")] * many_failures + [sent])

    result = _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert result is sent
    assert bot.send_message.await_count == many_failures + 1


# -- 4: a delay is applied between retries ---------------------------------------------


@patch("faraz_honar.telegram_bot.sending.asyncio.sleep", new_callable=AsyncMock)
def test_delay_is_applied_between_retries(mock_sleep: AsyncMock):
    sent = MagicMock()
    bot = _bot([NetworkError("a"), NetworkError("b"), sent])

    _run(send_message_with_retry(bot, retry_delay_seconds=5.0, chat_id=1, text="hi"))

    assert mock_sleep.await_count == 2  # once per failure, never after the final success
    for call in mock_sleep.await_args_list:
        assert call.args[0] == 5.0


@patch("faraz_honar.telegram_bot.sending.asyncio.sleep", new_callable=AsyncMock)
def test_default_retry_delay_is_used_when_not_overridden(mock_sleep: AsyncMock):
    sent = MagicMock()
    bot = _bot([NetworkError("a"), sent])

    _run(send_message_with_retry(bot, chat_id=1, text="hi"))

    mock_sleep.assert_awaited_once_with(DEFAULT_RETRY_DELAY_SECONDS)


@patch("faraz_honar.telegram_bot.sending.asyncio.sleep", new_callable=AsyncMock)
def test_retry_after_seconds_are_honored_over_the_default_delay(mock_sleep: AsyncMock):
    sent = MagicMock()
    bot = _bot([RetryAfter(30), sent])

    _run(send_message_with_retry(bot, retry_delay_seconds=5.0, chat_id=1, text="hi"))

    mock_sleep.assert_awaited_once_with(30.0)


# -- 5: a successful response immediately exits the retry loop ------------------------


def test_success_on_first_attempt_sends_exactly_once():
    sent = MagicMock()
    bot = _bot([sent])

    result = _run(send_message_with_retry(bot, chat_id=1, text="hi"))

    assert result is sent
    assert bot.send_message.await_count == 1


@patch("faraz_honar.telegram_bot.sending.asyncio.sleep", new_callable=AsyncMock)
def test_success_stops_immediately_no_extra_calls_or_sleeps_after(mock_sleep: AsyncMock):
    sent = MagicMock()
    bot = _bot([NetworkError("a"), sent])

    _run(send_message_with_retry(bot, chat_id=1, text="hi"))

    assert bot.send_message.await_count == 2  # never a third call once it succeeded
    assert mock_sleep.await_count == 1  # never a sleep after the successful attempt


# -- 6: sensitive credentials never appear in logs -------------------------------------


def test_bot_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture):
    fake_token = "123456:AAFakeSecretBotTokenAbCdEfGhIjKlMnOpQrStUv"
    # Simulates python-telegram-bot's own NetworkError wrapping, which can
    # embed the underlying HTTP client exception's text -- and the bot
    # token is part of the request URL
    # (https://api.telegram.org/bot<TOKEN>/sendMessage). The retry loop
    # must never log str(exc).
    leaky_error = NetworkError(
        f"httpx.ConnectError: Connection failed for https://api.telegram.org/bot{fake_token}/sendMessage"
    )
    sent = MagicMock()
    bot = _bot([leaky_error, sent])

    with caplog.at_level("WARNING"):
        _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert fake_token not in caplog.text
    assert "api.telegram.org" not in caplog.text
    # The log line should still exist (so the outage is visible in logs),
    # just without the sensitive/URL-bearing exception text.
    assert "Telegram send failed" in caplog.text
    assert "NetworkError" in caplog.text


def test_retry_after_value_never_leaks_a_token_either(caplog: pytest.LogCaptureFixture):
    fake_token = "987654:AnotherFakeTokenValueForTesting"
    leaky_error = RetryAfter(2)
    leaky_error.message = f"Too Many Requests: retry after 2 (bot{fake_token})"  # defensive
    sent = MagicMock()
    bot = _bot([leaky_error, sent])

    with caplog.at_level("WARNING"):
        _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert fake_token not in caplog.text


# -- 7: existing permanent Telegram errors retain their current behavior --------------


def test_bad_request_is_raised_immediately_not_retried():
    bot = _bot([BadRequest("chat not found")])

    with pytest.raises(BadRequest):
        _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert bot.send_message.await_count == 1  # never retried


def test_forbidden_is_raised_immediately_not_retried():
    bot = _bot([Forbidden("bot was blocked by the user")])

    with pytest.raises(Forbidden):
        _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert bot.send_message.await_count == 1


def test_invalid_token_is_raised_immediately_not_retried():
    bot = _bot([InvalidToken("bad token")])

    with pytest.raises(InvalidToken):
        _run(send_message_with_retry(bot, retry_delay_seconds=0, chat_id=1, text="hi"))

    assert bot.send_message.await_count == 1


# -- Arguments are passed through to bot.send_message unchanged -----------------------


def test_kwargs_are_forwarded_unchanged():
    sent = MagicMock()
    bot = _bot([sent])
    markup = MagicMock()

    _run(send_message_with_retry(bot, chat_id=42, text="hello", reply_markup=markup))

    bot.send_message.assert_awaited_once_with(chat_id=42, text="hello", reply_markup=markup)


# -- broadcast_message_with_retry: same message to multiple chat IDs ------------------


def test_broadcast_sends_to_every_configured_chat_id():
    sent = MagicMock()
    bot = _multi_chat_bot({1: [sent], 2: [sent], 3: [sent]})

    _run(
        broadcast_message_with_retry(
            bot, chat_ids=[1, 2, 3], retry_delay_seconds=0, text="report"
        )
    )

    sent_chat_ids = {c.kwargs["chat_id"] for c in bot.send_message.await_args_list}
    assert sent_chat_ids == {1, 2, 3}
    assert bot.send_message.await_count == 3


def test_broadcast_one_recipients_network_failures_do_not_affect_others():
    sent = MagicMock()
    bot = _multi_chat_bot(
        {
            1: [NetworkError("a"), NetworkError("b"), sent],  # recovers after 2 failures
            2: [sent],
            3: [sent],
        }
    )

    _run(
        broadcast_message_with_retry(
            bot, chat_ids=[1, 2, 3], retry_delay_seconds=0, text="report"
        )
    )

    # Every recipient still receives the message, regardless of another
    # recipient's temporary failures.
    calls_per_chat: dict[int, int] = {}
    for call in bot.send_message.await_args_list:
        chat_id = call.kwargs["chat_id"]
        calls_per_chat[chat_id] = calls_per_chat.get(chat_id, 0) + 1
    assert calls_per_chat == {1: 3, 2: 1, 3: 1}


def test_broadcast_permanent_error_for_one_recipient_does_not_affect_others():
    sent = MagicMock()
    bot = _multi_chat_bot(
        {
            1: [BadRequest("chat not found")],  # permanent -- never retried
            2: [sent],
            3: [sent],
        }
    )

    # Must not raise: one recipient's permanent failure is isolated.
    _run(
        broadcast_message_with_retry(
            bot, chat_ids=[1, 2, 3], retry_delay_seconds=0, text="report"
        )
    )

    calls_per_chat: dict[int, int] = {}
    for call in bot.send_message.await_args_list:
        chat_id = call.kwargs["chat_id"]
        calls_per_chat[chat_id] = calls_per_chat.get(chat_id, 0) + 1
    assert calls_per_chat == {1: 1, 2: 1, 3: 1}


def test_broadcast_permanent_error_is_logged(caplog: pytest.LogCaptureFixture):
    sent = MagicMock()
    bot = _multi_chat_bot({1: [BadRequest("chat not found")], 2: [sent]})

    with caplog.at_level("ERROR"):
        _run(broadcast_message_with_retry(bot, chat_ids=[1, 2], retry_delay_seconds=0, text="x"))

    assert "chat_id=1" in caplog.text


def test_broadcast_does_not_remove_a_failing_recipient_from_the_caller_owned_list():
    # A recipient that fails permanently on one broadcast must still be
    # attempted on the NEXT broadcast -- chat_ids is caller-owned
    # configuration and is never mutated by this function.
    sent = MagicMock()
    bot = _multi_chat_bot(
        {
            1: [BadRequest("chat not found"), sent],  # fails once, then would succeed
            2: [sent],
        }
    )
    chat_ids = [1, 2]

    _run(broadcast_message_with_retry(bot, chat_ids=chat_ids, retry_delay_seconds=0, text="one"))
    assert chat_ids == [1, 2]  # the caller's list itself is untouched

    _run(broadcast_message_with_retry(bot, chat_ids=chat_ids, retry_delay_seconds=0, text="two"))

    calls_to_chat_1 = [c for c in bot.send_message.await_args_list if c.kwargs["chat_id"] == 1]
    assert len(calls_to_chat_1) == 2  # attempted again on the second broadcast


def test_broadcast_runs_recipients_concurrently_not_sequentially():
    # chat 1's send blocks until chat 2's send has started -- this can only
    # succeed if the two sends run concurrently, not one-after-another.
    chat_2_started = asyncio.Event()

    async def _send_message(*, chat_id: int, **_kwargs):
        if chat_id == 1:
            await asyncio.wait_for(chat_2_started.wait(), timeout=1.0)
            return MagicMock()
        chat_2_started.set()
        return MagicMock()

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=_send_message)

    # Must not time out / hang -- proves chat 2 was not queued behind chat 1.
    _run(broadcast_message_with_retry(bot, chat_ids=[1, 2], retry_delay_seconds=0, text="x"))

    assert bot.send_message.await_count == 2


# -- Long-message splitting (Telegram rejects >4096 chars with a permanent BadRequest) ---


def _long_report(lines: int = 400) -> str:
    return "\n".join(f"{i}. product number {i} " + "x" * 40 for i in range(1, lines + 1))


def test_short_text_is_not_split():
    assert split_message_text("hello") == ["hello"]


def test_long_text_is_split_into_safe_chunks_without_losing_anything():
    text = _long_report()
    assert len(text) > 4096  # would have been rejected by Telegram as a single message

    chunks = split_message_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_SAFE_MESSAGE_CHARS for chunk in chunks)
    assert "\n".join(chunks) == text  # same content, same order, breaks only at line ends


def test_a_single_overlong_line_is_hard_split():
    text = "a\n" + "y" * 9000 + "\nb"
    chunks = split_message_text(text)
    assert all(len(chunk) <= TELEGRAM_SAFE_MESSAGE_CHARS for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == "a" + "y" * 9000 + "b"


def test_broadcast_long_message_sends_parts_in_order_to_every_chat():
    text = _long_report()
    sent: list[dict] = []

    async def _send_message(**kwargs):
        sent.append(kwargs)

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=_send_message)

    _run(broadcast_long_message_with_retry(bot, chat_ids={1, 2}, text=text, reply_markup="KEYBOARD"))

    for chat_id in (1, 2):
        parts = [kw for kw in sent if kw["chat_id"] == chat_id]
        assert len(parts) > 1
        assert "\n".join(kw["text"] for kw in parts) == text
        # The keyboard is attached to the LAST part only.
        assert ["reply_markup" in kw for kw in parts] == [False] * (len(parts) - 1) + [True]


def test_broadcast_long_message_with_short_text_is_one_plain_message():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    _run(broadcast_long_message_with_retry(bot, chat_ids=[7], text="short", reply_markup="KB"))

    bot.send_message.assert_awaited_once_with(chat_id=7, text="short", reply_markup="KB")
