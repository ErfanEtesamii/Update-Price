"""Assembles the Telegram Application: handlers + bot_data wiring.

Kept separate from main.py so tests can build an Application (or just the
handler functions) without needing a live bot token.
"""
from __future__ import annotations

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from faraz_honar.config import Settings
from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.telegram_bot.handlers import menu_message_handler, start_command
from faraz_honar.telegram_bot.state import PlatformJobManager
from faraz_honar.update.service import PriceUpdateService


def build_application(settings: Settings, service: PriceUpdateService) -> Application:
    if not settings.telegram_bot_token:
        raise ConfigurationError("TELEGRAM_BOT_TOKEN is not configured.")

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["service"] = service
    # One in-process lock + live-progress tracker per platform, shared by
    # the persistent Telegram control panel (handlers.py) and the hourly
    # recheck job (scheduler/jobs.py -> handlers.hourly_recheck_job), so the
    # two can never process the same platform at the same time.
    application.bot_data["job_manager"] = PlatformJobManager()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_message_handler))
    return application
