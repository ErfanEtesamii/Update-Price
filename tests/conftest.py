"""Shared pytest fixtures.

Settings() reads the real ``.env`` in the working directory and the real
process environment. Without isolation, a developer's own TELEGRAM_CHAT_IDS /
TELEGRAM_ALLOWED_USER_IDS leak into tests that build ``Settings(...)`` and
expect the field defaults (e.g. "chat IDs unset -> fall back to the triggering
chat"), so those tests pass on a clean checkout and fail on a configured
machine. This makes every test hermetic by default; a test that wants a value
passes it explicitly (``Settings(telegram_chat_ids="111")``) or uses
``monkeypatch.setenv``.
"""
from __future__ import annotations

import pytest

from faraz_honar.config import Settings
from faraz_honar.platforms.digikala.client import reset_shared_rate_limits


@pytest.fixture(autouse=True)
def _isolate_settings_from_local_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never read the developer's real .env file...
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # ...nor any of the real environment variables Settings knows about.
    for field_name, field in Settings.model_fields.items():
        monkeypatch.delenv(field.alias or field_name.upper(), raising=False)


@pytest.fixture(autouse=True)
def _reset_digikala_shared_rate_limit() -> None:
    # A 429 pauses every DigiKala client in the process (shared cooldown);
    # never let one test's pause leak into another test.
    reset_shared_rate_limits()
    yield
    reset_shared_rate_limits()
