"""Assembles the Telegram Application: handlers + bot_data wiring.

Kept separate from main.py so tests can build an Application (or just the
handler functions) without needing a live bot token.
"""
from __future__ import annotations

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from faraz_honar.config import Settings
from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.telegram_bot import alerts
from faraz_honar.telegram_bot.handlers import menu_message_handler, start_command
from faraz_honar.telegram_bot.state import PlatformJobManager
from faraz_honar.update.service import PriceUpdateService


def build_application(settings: Settings, service: PriceUpdateService) -> Application:
    if not settings.telegram_bot_token:
        raise ConfigurationError("TELEGRAM_BOT_TOKEN is not configured.")

    # python-telegram-bot's default HTTP timeouts are ~5 s. On a slow or flaky
    # link (DNS/connect delays of 10-30 s) that makes the bot look dead, so
    # every timeout is raised (TELEGRAM_REQUEST_TIMEOUT_SECONDS, default 30 s).
    # Long-polling (getUpdates) additionally needs read_timeout > the 10 s
    # server-side poll window.
    timeout = max(5.0, float(settings.telegram_request_timeout_seconds))
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .write_timeout(timeout)
        .pool_timeout(timeout)
        .get_updates_connect_timeout(timeout)
        .get_updates_read_timeout(timeout + 10.0)
        .get_updates_write_timeout(timeout)
        .get_updates_pool_timeout(timeout)
        .post_init(alerts.send_startup_notice)
    )
    proxy = (settings.telegram_proxy_url or "").strip()
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["service"] = service
    # One in-process lock + live-progress tracker per platform, shared by
    # the persistent Telegram control panel (handlers.py) and the hourly
    # recheck job (scheduler/jobs.py -> handlers.hourly_recheck_job), so the
    # two can never process the same platform at the same time.
    application.bot_data["job_manager"] = PlatformJobManager()

    # Global error handler + admin alerts (network outage / unexpected bug).
    alerts.install(application, settings)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_message_handler))
    return application
