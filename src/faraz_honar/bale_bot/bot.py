"""Build the price-updater control bot on Bale's Bot API.

Bale documents a Telegram-Bot-API-compatible schema. The existing mature
client library is used only as transport, with both endpoints pinned to Bale.
"""
from __future__ import annotations

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from faraz_honar.config import Settings
from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.telegram_bot import alerts
from faraz_honar.telegram_bot.handlers import (
    document_message_handler,
    menu_message_handler,
    start_command,
)
from faraz_honar.telegram_bot.state import PlatformJobManager
from faraz_honar.update.service import PriceUpdateService


def build_bale_application(settings: Settings, service: PriceUpdateService) -> Application:
    if not settings.telegram_bot_token:
        raise ConfigurationError("BALE_BOT_TOKEN is not configured (formerly TELEGRAM_BOT_TOKEN).")
    timeout = max(5.0, float(settings.telegram_request_timeout_seconds))
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .base_url(settings.bale_api_base_url)
        .base_file_url(settings.bale_file_base_url)
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
    application.bot_data["job_manager"] = PlatformJobManager()
    alerts.install(application, settings)
    application.add_handler(CommandHandler("start", start_command))
    # The uploaded product Excel file ("📤 ارسال اکسل محصولات") — must be
    # registered before the catch-all text handler below since a document
    # update has no .text (it's never matched by filters.TEXT anyway, but
    # keeping it first mirrors the intended precedence if that ever changes).
    application.add_handler(MessageHandler(filters.Document.ALL, document_message_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_message_handler))
    return application


build_application = build_bale_application
