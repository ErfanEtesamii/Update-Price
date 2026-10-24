"""Telegram control-interface access control (spec section 11).

The bot is the operational control surface for triggering real price
updates, so only explicitly allow-listed Telegram user IDs may use it.
"""
from __future__ import annotations


def is_allowed(user_id: int | None, allowed_user_ids: set[int]) -> bool:
    if user_id is None:
        return False
    if not allowed_user_ids:
        # Fail closed: an empty allowlist means nobody is authorized yet,
        # not "everybody is". TELEGRAM_ALLOWED_USER_IDS must be configured.
        return False
    return user_id in allowed_user_ids


def build_allowed_ids(allowed_user_ids: set[int], numbered_chat_ids: set[int]) -> set[int]:
    """The full allowlist: TELEGRAM_ALLOWED_USER_IDS ∪ TELEGRAM_CHAT_ID_1..10.

    An empty result still means nobody is authorized (is_allowed fails
    closed on an empty set), so leaving both settings blank is unchanged.
    """
    return set(allowed_user_ids) | set(numbered_chat_ids)
