"""Application entrypoint.

Run with: python -m faraz_honar.main

Wires: product source (the Excel file last sent to the bot, or a local CSV
fallback for dev/testing) -> platform registry -> state repository -> update
service -> the Bale bot (+ hourly recheck job) -> starts polling.

Nothing here makes a real price-changing API call on its own — that only
happens when a Bale command explicitly triggers PriceUpdateService.run(),
and even then only if DRY_RUN=false.
"""
from __future__ import annotations

import logging
import sys

from faraz_honar.bale_bot.bot import build_bale_application
from faraz_honar.config import Settings, get_settings
from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.logger import setup_logging
from faraz_honar.platforms.registry import build_platform_registry
from faraz_honar.scheduler.jobs import schedule_hourly_recheck
from faraz_honar.sheets.base import ProductSource
from faraz_honar.sheets.csv_source import CSVProductSource
from faraz_honar.sheets.excel_source import ExcelProductSource
from faraz_honar.sheets.mapping import SheetMapping
from faraz_honar.state.repository import StateRepository
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
            "the uploaded Excel file. This is a development/testing fallback only; leave this "
            "variable unset in production.",
            settings.product_source_csv_path,
        )
        return CSVProductSource(settings.product_source_csv_path, mapping)

    # This bot no longer reads Google Sheets: product data comes from
    # whatever .xlsx file was last sent to it through "📤 ارسال اکسل محصولات"
    # (telegram_bot/handlers.py::document_message_handler), saved at
    # excel_product_source_path and re-read fresh on every call below.
    logger.info(
        "Using Excel product source (%s, worksheet=%s)",
        settings.excel_product_source_path,
        settings.excel_worksheet_name,
    )
    return ExcelProductSource(
        path=settings.excel_product_source_path,
        mapping=mapping,
        worksheet_name=settings.excel_worksheet_name,
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
        application = build_bale_application(settings, service)
    except ConfigurationError as exc:
        logger.error("Cannot start the Bale bot: %s", exc)
        logger.error("Set BALE_BOT_TOKEN and BALE_ALLOWED_USER_IDS in .env and try again.")
        return 1

    schedule_hourly_recheck(application, settings)

    logger.info("Bot ready. Available platforms: %s", ", ".join(service.available_platforms) or "(none)")
    # Keep retrying when the service starts while DNS/the network is not ready.
    # Runtime polling errors are handled by the global alert handler.
    logging.getLogger("telegram.ext._utils.networkloop").setLevel(logging.DEBUG)
    application.run_polling(bootstrap_retries=-1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
