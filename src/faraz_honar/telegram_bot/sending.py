"""Persistent-retry wrapper for outbound Telegram sends.

Telegram reporting is mandatory: a temporary network/connectivity outage
must never cause a report to be silently dropped. This module is the ONE
place that policy is implemented, used only for `bot.send_message` calls in
telegram_bot/handlers.py — it does not touch, share code with, or in any
way change core/retry.py (the generic platform-API retry helper) or any
platform client's own throttle/retry logic, which all keep their existing
finite `max_attempts` exactly as before. Telegram's retry policy is
fundamentally different (unlimited, delay-based, keyed off python-telegram-
bot's own exception hierarchy) and is kept fully independent, the same way
every platform client in this project already keeps its own retry/throttle
state independent of the others.

Classification (see _is_retryable): a temporary connectivity failure (a
python-telegram-bot NetworkError/TimedOut, or a raw unwrapped transport
exception, or a 429 RetryAfter) is retried forever. A permanent/request-
level error (BadRequest — invalid parameters/malformed payload; Forbidden/
InvalidToken — auth failure; anything else python-telegram-bot raises) is
raised immediately, exactly as it always was with no wrapper at all —
existing error handling for those cases is fully preserved.

Security: never log `str(exc)` for a retried error. python-telegram-bot's
own NetworkError wrapping can embed the underlying HTTP client exception's
text (see telegram.request._httpxrequest.HTTPXRequest.do_request), which
can include the request URL — and the bot token is part of that URL
(`https://api.telegram.org/bot<TOKEN>/...`). Only the exception's class
name is ever logged.

Multiple recipients (see broadcast_message_with_retry): every configured
chat ID gets its own independent send_message_with_retry call, run
concurrently, so a prolonged outage/failure delivering to one recipient
never delays or blocks delivery to the others, and a permanent/request-
level error for one recipient (e.g. the bot was blocked in that chat) is
logged and skipped without affecting any other recipient. The chat ID list
itself is caller-owned configuration (Settings.telegram_chat_ids_set) and
is never mutated here — a temporary failure never removes a recipient from
it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from telegram import Bot, Message
from telegram.error import BadRequest, NetworkError, RetryAfter

logger = logging.getLogger(__name__)

# A fixed, reasonable spacing between retries — long enough that a
# prolonged outage doesn't produce excessive request spam (or log spam),
# short enough that a brief blip recovers quickly. Not configurable via
# Settings on purpose: this is Telegram-transport plumbing, not a business
# rule like the platform read/write delays in config.py.
DEFAULT_RETRY_DELAY_SECONDS = 5.0

# Telegram rejects a single message longer than 4096 characters with a
# permanent BadRequest ("message is too long") — which the sender below logs
# and drops. A final report listing hundreds of products easily exceeds that,
# so long reports are split into several messages first (see
# split_message_text / broadcast_long_message_with_retry). Kept a bit under
# the hard limit as a safety margin (Telegram counts UTF-16 units, and emoji
# count as two).
TELEGRAM_SAFE_MESSAGE_CHARS = 3500


def _is_retryable(exc: Exception) -> bool:
    """True for a temporary connectivity failure (retry forever). False for
    a permanent/request-level error (preserve existing behavior: raise
    immediately, never retried).
    """
    if isinstance(exc, RetryAfter):
        return True
    if isinstance(exc, BadRequest):
        # BadRequest is structurally a NetworkError subclass in
        # python-telegram-bot, but semantically a permanent, request-level
        # error (invalid parameters/malformed payload) — must be checked,
        # and excluded, before the general NetworkError check below.
        return False
    if isinstance(exc, NetworkError):
        return True  # includes TimedOut and any other transport failure
    # Defensive fallback for a raw transport-level exception that somehow
    # escaped python-telegram-bot's own request-layer wrapping.
    return isinstance(exc, (TimeoutError, ConnectionError))


def _retry_after_seconds(exc: RetryAfter, default: float) -> float:
    retry_after = exc.retry_after
    if hasattr(retry_after, "total_seconds"):
        return retry_after.total_seconds()
    return float(retry_after) if retry_after else default


async def send_message_with_retry(
    bot: Bot, *, retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS, **kwargs: Any
) -> Message:
    """`bot.send_message(**kwargs)`, retried indefinitely — no maximum
    attempt count — on a temporary connectivity failure (see
    module docstring). Returns as soon as Telegram responds successfully;
    a permanent/request-level error is raised immediately.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await bot.send_message(**kwargs)
        except Exception as exc:  # noqa: BLE001 - classified by _is_retryable, re-raised if not
            if not _is_retryable(exc):
                raise
            delay = (
                _retry_after_seconds(exc, retry_delay_seconds)
                if isinstance(exc, RetryAfter)
                else retry_delay_seconds
            )
            logger.warning(
                "Telegram send failed (%s, attempt %d); retrying in %s seconds...",
                type(exc).__name__,
                attempt,
                delay,
            )
            await asyncio.sleep(delay)


async def broadcast_message_with_retry(
    bot: Bot,
    *,
    chat_ids: Iterable[int],
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    **kwargs: Any,
) -> None:
    """Sends the same message to every chat id in `chat_ids`, each via its
    own send_message_with_retry call (so each recipient gets the full
    unlimited-retry-on-network-failure guarantee), run concurrently so one
    recipient's outage never delays or blocks the others. A permanent/
    request-level error for one recipient is logged and does not affect
    delivery to any other recipient, and never removes that recipient from
    the caller's configured list — see module docstring.
    """

    async def _send_one(chat_id: int) -> None:
        try:
            await send_message_with_retry(
                bot, retry_delay_seconds=retry_delay_seconds, chat_id=chat_id, **kwargs
            )
        except Exception:  # noqa: BLE001 - one recipient's failure must not break the others
            logger.exception("Failed to deliver a Telegram message to chat_id=%s", chat_id)

    await asyncio.gather(*(_send_one(chat_id) for chat_id in chat_ids))


def split_message_text(text: str, limit: int = TELEGRAM_SAFE_MESSAGE_CHARS) -> list[str]:
    """Split `text` into chunks of at most `limit` characters, breaking only
    between lines (so a numbered product entry is never cut in half). A single
    line longer than `limit` is hard-split as a last resort. Always returns at
    least one chunk; short text comes back unchanged as one chunk.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current))
            current, current_len = [], 0

    for line in text.split("\n"):
        # A line that can't fit in any chunk on its own: hard-split it.
        while len(line) > limit:
            flush()
            chunks.append(line[:limit])
            line = line[limit:]
        added = len(line) + (1 if current else 0)  # +1 for the joining newline
        if current and current_len + added > limit:
            flush()
            added = len(line)
        current.append(line)
        current_len += added
    flush()
    return chunks


async def broadcast_long_message_with_retry(
    bot: Bot,
    *,
    chat_ids: Iterable[int],
    text: str,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    **kwargs: Any,
) -> None:
    """broadcast_message_with_retry for text that may exceed Telegram's
    per-message length limit: the text is split with split_message_text and
    the chunks are sent IN ORDER (each chunk goes to all recipients
    concurrently, and the next chunk only starts once that one has been
    delivered or has permanently failed, so every chat sees the parts in the
    right order). Extra kwargs such as `reply_markup` are attached to the LAST
    chunk only, so a keyboard isn't repeated on every part.
    """
    recipients = list(chat_ids)
    chunks = split_message_text(text)
    for index, chunk in enumerate(chunks):
        chunk_kwargs = dict(kwargs)
        if index < len(chunks) - 1:
            chunk_kwargs.pop("reply_markup", None)
        await broadcast_message_with_retry(
            bot,
            chat_ids=recipients,
            retry_delay_seconds=retry_delay_seconds,
            text=chunk,
            **chunk_kwargs,
        )
