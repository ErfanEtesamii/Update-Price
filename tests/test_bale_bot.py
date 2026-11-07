"""Regression tests for Bale transport configuration."""
from unittest.mock import MagicMock

from faraz_honar.bale_bot.bot import build_bale_application
from faraz_honar.config import Settings


def test_bale_application_uses_bale_api_and_file_endpoints():
    settings = Settings(
        telegram_bot_token="123456:placeholder-token-not-real",
        bale_api_base_url="https://tapi.bale.ai/bot",
        bale_file_base_url="https://tapi.bale.ai/file/bot",
    )

    application = build_bale_application(settings, MagicMock())

    assert application.bot.base_url == "https://tapi.bale.ai/bot123456:placeholder-token-not-real"
    assert application.bot.base_file_url == "https://tapi.bale.ai/file/bot123456:placeholder-token-not-real"
