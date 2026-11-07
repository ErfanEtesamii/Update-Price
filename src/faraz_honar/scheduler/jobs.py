"""Wires the hourly 12%-recheck job (spec section 8) onto the bot's
application scheduler. Uses python-telegram-bot's built-in JobQueue
(APScheduler-backed) as a Bale API transport dependency,
so there's a single event loop instead of a second scheduler thread.
"""
from __future__ import annotations

import logging

from telegram.ext import Application

from faraz_honar.config import Settings
from faraz_honar.telegram_bot.handlers import hourly_recheck_job

logger = logging.getLogger(__name__)

JOB_NAME = "hourly_capped_price_recheck"


def schedule_hourly_recheck(application: Application, settings: Settings) -> None:
    if not settings.hourly_recheck_enabled:
        logger.info(
            "Hourly recheck of 12%%-capped products is disabled (HOURLY_RECHECK_ENABLED=false); "
            "nothing is scheduled."
        )
        return

    if application.job_queue is None:
        logger.warning(
            "No JobQueue available (python-telegram-bot's APScheduler extra may be "
            "missing) — the hourly 12%% recheck will NOT run automatically. Install "
            "the 'job-queue' extra or run the recheck via an external cron instead."
        )
        return

    interval_seconds = settings.hourly_recheck_minutes * 60
    # Offset only the FIRST run's delay (see Settings.schedule_offset_seconds'
    # docstring) - `interval` itself stays the plain hourly period. Since
    # interval_seconds is always a whole multiple of order-sync-platform's
    # poll cadence, adding a fixed, non-multiple offset once here keeps
    # every subsequent repeat out of phase with it too, not just the first.
    first_run_seconds = interval_seconds + settings.schedule_offset_seconds
    application.job_queue.run_repeating(
        hourly_recheck_job,
        interval=interval_seconds,
        first=first_run_seconds,
        name=JOB_NAME,
    )
    logger.info(
        "Scheduled hourly 12%% recheck every %d minute(s) (first run in %d seconds, "
        "including a %ds offset to avoid colliding with order-sync-platform's poll cycle)",
        settings.hourly_recheck_minutes, first_run_seconds, settings.schedule_offset_seconds,
    )
