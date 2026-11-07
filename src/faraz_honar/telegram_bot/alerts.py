"""Admin alerts + global error handler for the Bale bot.

Why this exists: python-telegram-bot logs "No error handlers are
registered" and does nothing else when an update handler or the polling loop
raises. A flaky link (DNS/connect timeouts) then looks like a dead bot with
no explanation. This module:

* classifies errors (network blip vs. real bug),
* sends ONE alert per cooldown window to the admin (not one per error),
* sends it through send_message_with_retry, which retries forever - so the
  alert arrives as soon as Bale is reachable again, even if it was the
  outage itself that triggered it.

Security: only exception CLASS NAMES are ever logged or sent. str(exc) of a
telegram NetworkError can embed the request URL, which contains the bot token.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

from faraz_honar.config import Settings
from faraz_honar.telegram_bot.sending import send_message_with_retry

logger = logging.getLogger(__name__)

_MONITOR_KEY = "connection_monitor"


def alert_recipient(settings: Settings) -> int | None:
    """BALE_ADMIN_CHAT_ID, else the lowest allowed user id, else None."""
    if settings.admin_chat_id:
        return settings.admin_chat_id
    allowed = sorted(settings.telegram_allowed_user_ids_set)
    return allowed[0] if allowed else None


def is_network_error(exc: BaseException | None) -> bool:
    return isinstance(exc, (NetworkError, TimedOut, TimeoutError, ConnectionError, OSError))


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ConnectionMonitor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cooldown = max(1, settings.telegram_alert_cooldown_minutes) * 60
        self._last_alert_at: float | None = None
        self._first_error_at: float | None = None
        self._errors_since_alert = 0

    def should_alert(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if not self._settings.telegram_alerts_enabled:
            return False
        if self._last_alert_at is None or now - self._last_alert_at >= self._cooldown:
            return True
        return False

    def record_network_error(self, now: float | None = None) -> tuple[bool, int, float]:
        """Returns (send_alert_now, errors_in_window, window_seconds)."""
        now = time.monotonic() if now is None else now
        if self._first_error_at is None:
            self._first_error_at = now
        self._errors_since_alert += 1
        window = now - self._first_error_at
        if self.should_alert(now):
            count = self._errors_since_alert
            self._last_alert_at = now
            self._first_error_at = None
            self._errors_since_alert = 0
            return True, count, window
        return False, self._errors_since_alert, window

    def record_other_error(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if not self.should_alert(now):
            return False
        self._last_alert_at = now
        return True


async def _send_admin(application: Application, text: str) -> None:
    settings: Settings = application.bot_data["settings"]
    chat_id = alert_recipient(settings)
    if chat_id is None or not settings.telegram_alerts_enabled:
        return
    try:
        await send_message_with_retry(application.bot, chat_id=chat_id, text=text)
    except Exception:  # noqa: BLE001 - an alert must never raise into the bot
        logger.exception("Could not deliver admin alert")


def install(application: Application, settings: Settings) -> ConnectionMonitor:
    monitor = ConnectionMonitor(settings)
    application.bot_data[_MONITOR_KEY] = monitor
    application.add_error_handler(on_error)
    return monitor


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    application = context.application
    monitor: ConnectionMonitor | None = application.bot_data.get(_MONITOR_KEY)
    name = type(exc).__name__

    if is_network_error(exc):
        logger.warning("Bale network error (%s)", name)
        if monitor is None:
            return
        send_now, count, window = monitor.record_network_error()
        if send_now:
            span = f" (طی حدود {max(1, int(window // 60))} دقیقه)" if count > 1 else ""
            text = (
                "⚠️ هشدار: اختلال در اتصال بات به بله\n"
                f"نوع خطا: {name}\n"
                f"تعداد خطا: {count}{span}\n"
                f"زمان: {_now_text()}\n"
                "اکنون اتصال برقرار است (این پیام فقط بعد از وصل شدن می‌رسد). "
                "اگر بات جواب نداد، اینترنت/DNS سرور را بررسی کنید."
            )
            application.create_task(_send_admin(application, text))
        return

    # A real bug (handler crashed, bad data, ...): log the full traceback
    # locally; tell the admin only the class name + where it happened.
    logger.error("Unhandled bot error (%s)", name, exc_info=exc)
    if monitor is not None and monitor.record_other_error():
        text = (
            "🚨 خطای غیرمنتظره در بات\n"
            f"نوع خطا: {name}\n"
            f"زمان: {_now_text()}\n"
            "جزئیات کامل در logs\\faraz-honar.log ثبت شده است."
        )
        application.create_task(_send_admin(application, text))


async def send_startup_notice(application: Application) -> None:
    """post_init hook: tells the admin the bot (re)started - so a crash loop or
    an unexpected service restart never goes unnoticed."""
    text = (
        "✅ بات فراز هنر روی بله راه‌اندازی شد\n"
        f"زمان: {_now_text()}"
    )
    application.create_task(_send_admin(application, text))
