"""Application entrypoint.

Run with: python -m faraz_honar.main

Wires: product source (Google Sheets, or a local CSV fallback for
dev/testing) -> platform registry -> state repository -> update service ->
Telegram bot (+ hourly recheck job) -> starts polling.

Nothing here makes a real price-changing API call on its own — that only
happens when a Telegram command explicitly triggers PriceUpdateService.run(),
and even then only if DRY_RUN=false.
"""
from __future__ import annotations

import logging
import sys

from faraz_honar.config import Settings, get_settings
from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.logger import setup_logging
from faraz_honar.platforms.registry import build_platform_registry
from faraz_honar.scheduler.jobs import schedule_hourly_recheck
from faraz_honar.sheets.base import ProductSource
from faraz_honar.sheets.csv_source import CSVProductSource
from faraz_honar.sheets.google_sheets import GoogleSheetsProductSource
from faraz_honar.sheets.mapping import SheetMapping
from faraz_honar.state.repository import StateRepository
from faraz_honar.telegram_bot.bot import build_application
from faraz_honar.update.service import PriceUpdateService

logger = logging.getLogger(__name__)

DEFAULT_SHEET_MAPPING_PATH = "config/sheet_mapping.yaml"


def configure_logging() -> None:
    # stderr + rotating logs/faraz-honar.log (see faraz_honar/logger.py).
    setup_logging()


def build_product_source(settings: Settings) -> ProductSource:
    from pathlib import Path

    mapping = SheetMapping.load(Path(DEFAULT_SHEET_MAPPING_PATH))

    if settings.product_source_csv_path:
        logger.warning(
            "PRODUCT_SOURCE_CSV_PATH is set — using local CSV product source (%s) instead of "
            "live Google Sheets. This is a development/testing fallback only; leave this "
            "variable unset in production so the app reads the live Google Sheet.",
            settings.product_source_csv_path,
        )
        return CSVProductSource(settings.product_source_csv_path, mapping)

    logger.info("Using live Google Sheets product source (sheet_id=%s)", settings.google_sheet_id)
    return GoogleSheetsProductSource(
        sheet_id=settings.google_sheet_id or "",
        mapping=mapping,
        service_account_file=settings.google_service_account_file,
        worksheet_gid=settings.google_sheet_worksheet_gid,
        worksheet_name=settings.google_sheet_worksheet_name,
    )


def build_service(settings: Settings) -> PriceUpdateService:
    product_source = build_product_source(settings)
    platform_registry = build_platform_registry(settings)
    state_repository = StateRepository(settings.state_db_path)
    return PriceUpdateService(
        product_source=product_source,
        platform_registry=platform_registry,
        state_repository=state_repository,
        max_increase_pct=settings.max_price_increase_pct,
        max_retry_attempts=settings.max_retry_attempts,
        error_rounds_enabled=settings.error_rounds_enabled,
        error_rounds_cooldown_seconds=settings.error_rounds_cooldown_seconds,
    )


def main() -> int:
    configure_logging()
    settings = get_settings()
    settings.ensure_data_dir()

    logger.info("Starting Faraz Honar price updater (dry_run=%s)", settings.dry_run)

    try:
        service = build_service(settings)
    except ConfigurationError as exc:
        logger.error("Cannot build the update service: %s", exc)
        return 1

    try:
        application = build_application(settings, service)
    except ConfigurationError as exc:
        logger.error("Cannot start the Telegram bot: %s", exc)
        logger.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS in .env and try again.")
        return 1

    schedule_hourly_recheck(application, settings)

    logger.info("Bot ready. Available platforms: %s", ", ".join(service.available_platforms) or "(none)")
    application.run_polling()
    return 0


if __name__ == "__main__":
    sys.exit(main())
