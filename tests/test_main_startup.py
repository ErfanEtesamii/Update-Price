"""Verifies the app starts (or fails clearly) with placeholder/missing
credentials, per the "verify project can start correctly with
placeholder/missing credentials" requirement. No real network call is made:
Application.builder().build() does not connect anywhere, and we never call
run_polling()/initialize() in these tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from faraz_honar.config import Settings
from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.main import build_product_source, build_service
from faraz_honar.telegram_bot.bot import build_application

FIXTURES = Path(__file__).parent / "fixtures"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        dry_run=True,
        google_sheet_id="1A5hVB4PN4TDurhwb4cPjtcuDgvK7XalwmekDfSne3Ss",
        google_service_account_file=None,
        product_source_csv_path=None,
        telegram_bot_token=None,
        telegram_allowed_user_ids="",
    )
    base.update(overrides)
    return Settings(**base)


def test_service_build_fails_clearly_without_google_credentials():
    settings = _settings()
    with pytest.raises(ConfigurationError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        build_service(settings)


def test_service_builds_successfully_with_csv_fallback():
    settings = _settings(product_source_csv_path=FIXTURES / "sample_product_sheet.csv")
    service = build_service(settings)
    # Both DigiKala accounts (digikala and digikala2), both SnappShop accounts
    # (Tehran and Isfahan), TapsiShop, Basalam, and WooCommerce are
    # registered independently.
    assert service.available_platforms == [
        "digikala",
        "digikala2",
        "snappshop_tehran",
        "snappshop_isfahan",
        "tapsi_shop",
        "basalam",
        "woocommerce",
    ]
    products = service._product_source.fetch_products()  # noqa: SLF001 - white-box sanity check
    assert len(products) > 100


def test_bot_build_fails_clearly_without_token():
    settings = _settings(product_source_csv_path=FIXTURES / "sample_product_sheet.csv")
    service = build_service(settings)
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
        build_application(settings, service)


def test_bot_builds_with_placeholder_token_no_network_call():
    settings = _settings(
        product_source_csv_path=FIXTURES / "sample_product_sheet.csv",
        telegram_bot_token="123456:placeholder-token-not-real",
        telegram_allowed_user_ids="1,2,3",
    )
    service = build_service(settings)
    application = build_application(settings, service)
    assert application.bot_data["service"] is service
    assert application.bot_data["settings"] is settings


def test_product_source_selection_prefers_csv_when_configured():
    settings = _settings(product_source_csv_path=FIXTURES / "sample_product_sheet.csv")
    source = build_product_source(settings)
    from faraz_honar.sheets.csv_source import CSVProductSource

    assert isinstance(source, CSVProductSource)


def test_service_wires_configured_digikala_delays_into_client():
    # Proves the configured DIGIKALA_READ/WRITE_DELAY_SECONDS values flow
    # through Settings -> build_platform_registry -> the real DigikalaClient,
    # not just that Settings itself parses them (see test_config.py).
    settings = _settings(
        product_source_csv_path=FIXTURES / "sample_product_sheet.csv",
        digikala_read_delay_seconds=3.0,
        digikala_write_delay_seconds=6.0,
    )
    service = build_service(settings)
    digikala = service._platform_registry["digikala"]  # noqa: SLF001 - white-box sanity check
    assert digikala._read_delay_seconds == 3.0  # noqa: SLF001
    assert digikala._write_delay_seconds == 6.0  # noqa: SLF001


def test_bot_applies_raised_timeouts_error_handler_and_proxy():
    settings = _settings(
        product_source_csv_path=FIXTURES / "sample_product_sheet.csv",
        telegram_bot_token="123456:placeholder-token-not-real",
        telegram_allowed_user_ids="1",
        telegram_request_timeout_seconds=30,
        telegram_proxy_url="http://127.0.0.1:10809",
    )
    application = build_application(settings, build_service(settings))
    assert application.error_handlers, "global error handler must be registered"
    request = application.bot._request[1]  # noqa: SLF001 - white-box: the normal-API request
    assert request._client.timeout.connect == 30  # noqa: SLF001
    assert application.bot._request[0]._client.timeout.read == 40  # noqa: SLF001 - getUpdates: 30+10
