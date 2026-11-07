"""Tests for telegram_bot/alerts.py (error handler + admin alerts).

Uses asyncio.run() directly (no pytest-asyncio dependency), matching
test_telegram_sending.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import NetworkError, TimedOut

from faraz_honar.config import Settings
from faraz_honar.telegram_bot import alerts


def _settings(**kw) -> Settings:
    base = dict(
        telegram_bot_token="123456:placeholder-token-not-real",
        telegram_allowed_user_ids="42,7",
        admin_chat_id=775,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def _app(settings):
    created = []
    app = SimpleNamespace(
        bot_data={"settings": settings, "connection_monitor": alerts.ConnectionMonitor(settings)},
        bot=object(),
        create_task=lambda coro: created.append(coro) or coro,
    )
    return app, created


def test_recipient_prefers_admin_then_lowest_allowed_then_none():
    assert alerts.alert_recipient(_settings()) == 775
    assert alerts.alert_recipient(_settings(admin_chat_id=None)) == 7
    assert alerts.alert_recipient(_settings(admin_chat_id=None, telegram_allowed_user_ids="")) is None


def test_network_error_classification():
    assert alerts.is_network_error(TimedOut())
    assert alerts.is_network_error(NetworkError("x"))
    assert alerts.is_network_error(ConnectionError())
    assert not alerts.is_network_error(ValueError("bug"))
    assert not alerts.is_network_error(None)


def test_monitor_one_alert_per_cooldown_window():
    m = alerts.ConnectionMonitor(_settings(telegram_alert_cooldown_minutes=30))
    send, count, _ = m.record_network_error(now=1000.0)
    assert send and count == 1
    for t in (1100.0, 1200.0, 2000.0):
        send, _, _ = m.record_network_error(now=t)
        assert not send  # storm inside the cooldown -> silent
    send, count, _ = m.record_network_error(now=1000.0 + 1800.0)
    assert send and count == 4  # 3 suppressed + this one


def test_monitor_disabled_never_alerts():
    m = alerts.ConnectionMonitor(_settings(telegram_alerts_enabled=False))
    assert m.record_network_error(now=1.0)[0] is False
    assert m.record_other_error(now=1.0) is False


def test_on_error_network_sends_one_admin_alert_without_secrets(monkeypatch):
    sent = []

    async def fake_send(bot, **kw):
        sent.append(kw)

    async def scenario():
        app, created = _app(_settings())
        secret = "https://api.telegram.org/bot123456:SECRET/getMe"
        ctx = SimpleNamespace(error=NetworkError(secret), application=app)
        await alerts.on_error(None, ctx)
        await alerts.on_error(None, ctx)  # second one is inside the cooldown
        assert len(created) == 1
        await asyncio.gather(*created)

    monkeypatch.setattr(alerts, "send_message_with_retry", fake_send)
    asyncio.run(scenario())

    assert len(sent) == 1
    assert sent[0]["chat_id"] == 775
    assert "NetworkError" in sent[0]["text"]
    assert "SECRET" not in sent[0]["text"] and "bot123456" not in sent[0]["text"]


def test_on_error_bug_alerts_class_name_only(monkeypatch):
    sent = []

    async def fake_send(bot, **kw):
        sent.append(kw)

    async def scenario():
        app, created = _app(_settings())
        ctx = SimpleNamespace(error=ValueError("boom 123456:SECRET"), application=app)
        await alerts.on_error(None, ctx)
        await asyncio.gather(*created)

    monkeypatch.setattr(alerts, "send_message_with_retry", fake_send)
    asyncio.run(scenario())

    assert len(sent) == 1
    assert "ValueError" in sent[0]["text"]
    assert "SECRET" not in sent[0]["text"]


def test_alert_delivery_failure_never_raises(monkeypatch):
    monkeypatch.setattr(alerts, "send_message_with_retry", AsyncMock(side_effect=RuntimeError("x")))
    app, _ = _app(_settings())
    asyncio.run(alerts._send_admin(app, "hi"))  # must not raise


def test_no_recipient_means_no_send(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(alerts, "send_message_with_retry", send)
    app, _ = _app(_settings(admin_chat_id=None, telegram_allowed_user_ids=""))
    asyncio.run(alerts._send_admin(app, "hi"))
    send.assert_not_called()


def test_startup_notice_is_sent_to_admin(monkeypatch):
    sent = []

    async def fake_send(bot, **kw):
        sent.append(kw)

    async def scenario():
        app, created = _app(_settings())
        await alerts.send_startup_notice(app)
        await asyncio.gather(*created)

    monkeypatch.setattr(alerts, "send_message_with_retry", fake_send)
    asyncio.run(scenario())
    assert len(sent) == 1 and sent[0]["chat_id"] == 775
