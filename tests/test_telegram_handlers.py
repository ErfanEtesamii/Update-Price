"""Tests for the persistent Telegram control panel: menu routing, starting a
run, the single final report (no live per-product messages), graceful
cancellation, duplicate-run prevention, and the hourly recheck's locking
against a manual run.

The fake PriceUpdateService below only ever calls the hooks the real
PriceUpdateService.run()/recheck_capped_products() are proven (in
test_update_service.py) to call, with the same signatures — so these tests
exercise handlers.py's wiring/formatting/locking, not a re-implementation of
the business logic itself. Product-list filtering (missing "دیجی کالا
سریع" / missing target price) is likewise entirely a service-layer concern,
exhaustively tested in test_update_service.py — these tests only prove that
whatever RunSummary the service hands back is reported correctly (one final
message, right lists, no per-product spam).
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from openpyxl import load_workbook

from faraz_honar.config import Settings
from faraz_honar.core.models import OutcomeStatus, Product, RunSummary, UpdateResult
from faraz_honar.telegram_bot import handlers
from faraz_honar.telegram_bot.keyboards import (
    BTN_ALL_PLATFORMS,
    BTN_CHECK_PRICES,
    BTN_CONFIRM_NO,
    BTN_CONFIRM_YES,
    BTN_LAST_STATUS,
    BTN_SELECT_PLATFORM,
    BTN_STATUS,
    BTN_STOP,
)
from faraz_honar.telegram_bot import sending
from faraz_honar.telegram_bot.reporting import (
    format_change_notice,
    format_price_check_all_busy_message,
    format_price_check_nothing_to_check_message,
)
from faraz_honar.telegram_bot.state import PlatformJobManager
from faraz_honar.state.repository import ProductStateRow, RunReportRow
from faraz_honar.update.price_check import (
    CheckSelection,
    CheckStatus,
    PlatformCheckResult,
    PriceCheckEntry,
    select_products_for_check,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        dry_run=True,
        telegram_bot_token="123456:placeholder-token-not-real",
        telegram_allowed_user_ids="1",
    )
    base.update(overrides)
    return Settings(**base)


def _product(name: str = "Widget") -> Product:
    return Product(internal_id=name, name=name, reference_price=1_000_000, target_price=1_100_000)


class FakeProductSource:
    """Stands in for the ProductSource behind PriceUpdateService.product_source
    — the "📊 آخرین وضعیت" handler's only use of it is fetch_products() to
    feed build_status_matrix.
    """

    def __init__(self, products: list[Product] | None = None):
        self._products = products if products is not None else []
        self.fetch_products_calls = 0

    def fetch_products(self) -> list[Product]:
        self.fetch_products_calls += 1
        return self._products


class FakeService:
    """Stands in for PriceUpdateService, honoring the exact same hook
    contract the real one is unit-tested against in test_update_service.py.
    """

    def __init__(
        self,
        available_platforms: tuple[str, ...] = ("digikala",),
        run_impl=None,
        recheck_impl=None,
        rerun_specific_impl=None,
        state_repository=None,
        product_source=None,
    ):
        self.available_platforms = list(available_platforms)
        self.run_calls: list[list[str]] = []
        self.recheck_calls: list[list[str]] = []
        self.rerun_specific_calls: list[tuple[str, list[str]]] = []
        self._run_impl = run_impl
        self._recheck_impl = recheck_impl
        self._rerun_specific_impl = rerun_specific_impl
        # Real PriceUpdateService.state_repository exposes the StateRepository
        # used by the "📊 آخرین وضعیت" handler (list_all_product_states) and
        # the "retry failed only" confirmation (get_last_run). Defaults to an
        # empty FakeStateRepository (no recorded runs -> get_last_run always
        # returns None) rather than None, since the real service always has a
        # real StateRepository and _start_platform_run now unconditionally
        # calls service.state_repository.get_last_run(...).
        self.state_repository = (
            state_repository if state_repository is not None else FakeStateRepository()
        )
        # Real PriceUpdateService.product_source is the other half of the
        # "📊 آخرین وضعیت" handler's input (build_status_matrix needs both the
        # product list and the persisted states).
        self.product_source = (
            product_source if product_source is not None else FakeProductSource()
        )

    def run(self, platform_names, *, triggered_by, **hooks):
        self.run_calls.append(list(platform_names))
        if self._run_impl is not None:
            return self._run_impl(platform_names, triggered_by=triggered_by, **hooks)
        return {p: RunSummary(platform=p, triggered_by=triggered_by) for p in platform_names}

    def recheck_capped_products(self, platform_names, *, triggered_by="scheduler:hourly"):
        self.recheck_calls.append(list(platform_names))
        if self._recheck_impl is not None:
            return self._recheck_impl(platform_names, triggered_by=triggered_by)
        return {p: RunSummary(platform=p, triggered_by=triggered_by) for p in platform_names}

    def rerun_specific_products(self, platform, product_ids, *, triggered_by, **hooks):
        self.rerun_specific_calls.append((platform, list(product_ids)))
        if self._rerun_specific_impl is not None:
            return self._rerun_specific_impl(
                platform, product_ids, triggered_by=triggered_by, **hooks
            )
        return RunSummary(platform=platform, triggered_by=triggered_by)


class FakeStateRepository:
    """Stands in for StateRepository.list_recent_runs / get_last_run /
    list_all_product_states. Real handlers.py only ever calls these three
    methods on it: the first two for the "retry failed only" confirmation,
    the third for the "📊 آخرین وضعیت" Excel export.
    """

    def __init__(
        self,
        runs_by_platform: dict[str, list[RunReportRow]] | None = None,
        product_states: list[ProductStateRow] | None = None,
    ):
        self._runs_by_platform = runs_by_platform or {}
        self._product_states = product_states if product_states is not None else []
        self.list_recent_runs_calls: list[tuple[str, int]] = []
        self.get_last_run_calls: list[str] = []
        self.list_all_product_states_calls = 0

    def list_recent_runs(self, platform: str, *, limit: int = 2) -> list[RunReportRow]:
        self.list_recent_runs_calls.append((platform, limit))
        return self._runs_by_platform.get(platform, [])

    def get_last_run(self, platform: str) -> RunReportRow | None:
        # Tracked separately from list_recent_runs_calls (real
        # StateRepository.get_last_run is a thin wrapper over
        # list_recent_runs, but this fake keeps the two call logs distinct
        # so tests asserting one aren't affected by the unrelated other
        # check that runs on every _start_platform_run call).
        self.get_last_run_calls.append(platform)
        runs = self._runs_by_platform.get(platform, [])
        return runs[0] if runs else None

    def list_all_product_states(self) -> list[ProductStateRow]:
        self.list_all_product_states_calls += 1
        return self._product_states


def _context(settings: Settings, service: FakeService, job_manager: PlatformJobManager | None = None):
    bot = AsyncMock()
    sent_message = MagicMock()
    sent_message.message_id = 999
    bot.send_message.return_value = sent_message

    application = SimpleNamespace(
        bot_data={
            "settings": settings,
            "service": service,
            "job_manager": job_manager if job_manager is not None else PlatformJobManager(),
        }
    )
    # Real python-telegram-bot contexts carry a per-chat/user dict at
    # context.user_data; handlers.py uses it to remember that a platform-name
    # tap belongs to the "📦 انتخاب پلتفرم" (start a run) submenu.
    return SimpleNamespace(application=application, bot=bot, user_data={})


def _update(text: str | None = None, *, user_id: int | None = 1, chat_id: int = 100):
    message = AsyncMock()
    message.text = text
    message.chat = SimpleNamespace(id=chat_id)
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(message=message, effective_user=user)


def _run(coro):
    return asyncio.run(coro)


async def _drive(handler_coro, context) -> None:
    """Runs a handler call AND waits for any background task it created
    (e.g. _start_platform_run's fire-and-forget _execute_run) to finish —
    all on the same event loop, since asyncio.create_task()'d tasks cannot
    outlive/cross the loop they were scheduled on.
    """
    await handler_coro
    tasks = context.application.bot_data.get("_background_tasks")
    if tasks:
        await asyncio.gather(*list(tasks))


def _run_menu(update, context) -> None:
    _run(_drive(handlers.menu_message_handler(update, context), context))


# -- Persistent menu / no repeated /start ----------------------------------------


def test_start_command_shows_persistent_main_menu():
    settings = _settings()
    context = _context(settings, FakeService())
    update = _update("/start")

    _run(handlers.start_command(update, context))

    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs
    assert kwargs["reply_markup"].is_persistent is True


def test_unrecognized_text_shows_main_menu_without_requiring_start():
    settings = _settings()
    context = _context(settings, FakeService())
    update = _update("something random")

    _run(handlers.menu_message_handler(update, context))

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert kwargs["reply_markup"].is_persistent is True


def test_unauthorized_user_is_rejected():
    settings = _settings(telegram_allowed_user_ids="999")
    context = _context(settings, FakeService())
    update = _update(BTN_STATUS, user_id=1)

    _run(handlers.menu_message_handler(update, context))

    update.message.reply_text.assert_awaited_once_with("You are not authorized to use this bot.")


def test_select_platform_shows_platform_submenu():
    settings = _settings()
    context = _context(settings, FakeService())
    update = _update(BTN_SELECT_PLATFORM)

    _run(handlers.menu_message_handler(update, context))

    _, kwargs = update.message.reply_text.call_args
    labels = {b.text for row in kwargs["reply_markup"].keyboard for b in row}
    assert "🛒 🔴 Digikala" in labels


# -- Selecting DigiKala starts processing -----------------------------------------


def test_digikala_button_starts_a_run():
    settings = _settings()
    service = FakeService()
    job_manager = PlatformJobManager()
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    # The handler only launches a background task and returns immediately
    # (spec: bot stays responsive) — _run_menu also awaits that background
    # task (on the same loop) so we can assert on its effects afterward.
    _run_menu(update, context)

    assert service.run_calls == [["digikala"]]
    assert context.application.bot_data["last_platform"] == "digikala"


def test_snappshop_tehran_button_starts_a_run():
    # Same wiring as DigiKala's button — proves platform selection, job
    # locking, and reporting are fully generic (no SnappShop-specific
    # handler code was needed).
    settings = _settings()
    service = FakeService(available_platforms=("digikala", "digikala2", "snappshop_tehran"))
    job_manager = PlatformJobManager()
    context = _context(settings, service, job_manager)
    update = _update("🛒 🟣 SnappShop Tehran")

    _run_menu(update, context)

    assert service.run_calls == [["snappshop_tehran"]]
    assert context.application.bot_data["last_platform"] == "snappshop_tehran"
    assert job_manager.current("snappshop_tehran") is None
    assert job_manager.try_start("snappshop_tehran", triggered_by="test") is not None


def test_snappshop_tehran_run_produces_a_final_report_like_other_platforms():
    settings = _settings(dry_run=False)
    result = UpdateResult(
        product=_product("SnappShop Widget"), platform="snappshop_tehran",
        status=OutcomeStatus.SUCCESS, previous_price=1_000_000, applied_price=1_100_000,
        target_price=1_100_000,
    )

    def run_impl(platform_names, *, triggered_by, on_products_loaded, on_product_start, on_result,
                 should_cancel, on_round_start=None):
        on_products_loaded(1)
        on_product_start(result.product)
        on_result(result)
        return {
            "snappshop_tehran": RunSummary(
                platform="snappshop_tehran", triggered_by=triggered_by, results=[result]
            )
        }

    service = FakeService(
        available_platforms=("digikala", "digikala2", "snappshop_tehran"), run_impl=run_impl
    )
    context = _context(settings, service, PlatformJobManager())
    update = _update("🛒 🟣 SnappShop Tehran")

    _run_menu(update, context)

    # The unified final report shows a successful product's name only as a
    # count, not by name (see reporting.format_final_digikala_report), so
    # the wiring is checked through the report header/counts line instead.
    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    report_sends = [
        t for t in send_texts if "گزارش نهایی بروزرسانی" in t and "SnappShop Tehran" in t
    ]
    assert len(report_sends) == 1
    assert "✅ موفق: 1" in report_sends[0]


def test_snappshop_isfahan_button_starts_a_run():
    # Same wiring proof as Tehran's test above, for the second SnappShop
    # account — confirms the platform-selection/locking/reporting layer
    # needed zero Isfahan-specific handler code.
    settings = _settings()
    service = FakeService(
        available_platforms=("digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan")
    )
    job_manager = PlatformJobManager()
    context = _context(settings, service, job_manager)
    update = _update("🛒 🟣 SnappShop Isfahan")

    _run_menu(update, context)

    assert service.run_calls == [["snappshop_isfahan"]]
    assert context.application.bot_data["last_platform"] == "snappshop_isfahan"
    assert job_manager.current("snappshop_isfahan") is None
    assert job_manager.try_start("snappshop_isfahan", triggered_by="test") is not None


def test_tapsi_shop_button_starts_a_run():
    # Same wiring proof as the other platform buttons — confirms the
    # platform-selection/locking/reporting layer needed zero
    # TapsiShop-specific handler code.
    settings = _settings()
    service = FakeService(
        available_platforms=(
            "digikala",
            "digikala2",
            "snappshop_tehran",
            "snappshop_isfahan",
            "tapsi_shop",
        )
    )
    job_manager = PlatformJobManager()
    context = _context(settings, service, job_manager)
    update = _update("🛒 🟠 TapsiShop")

    _run_menu(update, context)

    assert service.run_calls == [["tapsi_shop"]]
    assert context.application.bot_data["last_platform"] == "tapsi_shop"
    assert job_manager.current("tapsi_shop") is None
    assert job_manager.try_start("tapsi_shop", triggered_by="test") is not None


def test_run_completes_and_releases_the_lock():
    settings = _settings()
    service = FakeService()
    job_manager = PlatformJobManager()
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)

    assert job_manager.current("digikala") is None
    # Lock is free again -> a second run can start.
    assert job_manager.try_start("digikala", triggered_by="test") is not None


# -- Duplicate-run prevention ------------------------------------------------------


def test_duplicate_digikala_run_asks_for_parallel_confirmation_instead_of_blocking():
    settings = _settings()
    service = FakeService()
    job_manager = PlatformJobManager()
    job_manager.try_start("digikala", triggered_by="telegram:2")  # simulate an already-active run
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala", user_id=1)

    _run(handlers.menu_message_handler(update, context))

    assert service.run_calls == []  # not started until the person confirms
    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert "پردازش دیگری" in args[0]
    assert "Digikala" in args[0]
    assert "موازی" in args[0]
    labels = {b.text for row in kwargs["reply_markup"].keyboard for b in row}
    assert "✅ بله" in labels
    assert "❌ خیر" in labels
    assert context.user_data["pending_confirmation"] == {
        "type": "parallel_run",
        "platform": "digikala",
        "user_id": 1,
    }


def test_confirming_parallel_run_starts_a_second_job_alongside_the_first():
    settings = _settings()
    service = FakeService()
    job_manager = PlatformJobManager()
    # Simulates an already-active run directly (never released via finish()
    # in this test, same as the other tests in this file that do this).
    existing = job_manager.try_start("digikala", triggered_by="telegram:2")
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala", user_id=1)
    _run(handlers.menu_message_handler(update, context))  # triggers the confirmation prompt

    update = _update("✅ بله", user_id=1)
    _run_menu(update, context)

    assert service.run_calls == [["digikala"]]  # the second, parallel run did start
    # The confirmed parallel run completes and releases itself; the
    # pre-existing (simulated) run is untouched and still registered.
    assert job_manager.current_all("digikala") == [existing]


def test_declining_parallel_run_leaves_the_existing_job_untouched():
    settings = _settings()
    service = FakeService()
    job_manager = PlatformJobManager()
    existing = job_manager.try_start("digikala", triggered_by="telegram:2")
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala", user_id=1)
    _run(handlers.menu_message_handler(update, context))  # triggers the confirmation prompt

    update = _update("❌ خیر", user_id=1)
    _run(handlers.menu_message_handler(update, context))

    assert service.run_calls == []  # nothing new was started
    assert job_manager.current("digikala") is existing  # original run still registered
    args, _ = update.message.reply_text.call_args
    assert "شروع نشد" in args[0]


def test_different_platform_start_also_warns_about_the_other_active_run():
    # job_manager itself still isolates platforms independently (see
    # test_platform_isolation.py) — but the Telegram-facing warning is
    # about ANY other run in progress, not only a same-platform conflict,
    # so selecting digikala2 while digikala is running must still ask for
    # confirmation, naming digikala as the one already running.
    settings = _settings()
    service = FakeService(available_platforms=("digikala", "digikala2"))
    job_manager = PlatformJobManager()
    job_manager.try_start("digikala", triggered_by="telegram:2")  # digikala already running
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala Saree", user_id=1)

    _run(handlers.menu_message_handler(update, context))

    assert service.run_calls == []  # not started until confirmed
    args, kwargs = update.message.reply_text.call_args
    assert "پردازش دیگری" in args[0]
    assert "Digikala" in args[0] and "Saree" not in args[0]  # names the OTHER (digikala) run
    assert context.user_data["pending_confirmation"] == {
        "type": "parallel_run",
        "platform": "digikala2",
        "user_id": 1,
    }


def test_confirming_a_different_platform_start_runs_it_without_touching_the_other():
    settings = _settings()
    service = FakeService(available_platforms=("digikala", "digikala2"))
    job_manager = PlatformJobManager()
    existing = job_manager.try_start("digikala", triggered_by="telegram:2")  # digikala already running
    context = _context(settings, service, job_manager)
    _run(handlers.menu_message_handler(_update("🛒 🔴 Digikala Saree", user_id=1), context))

    update = _update("✅ بله", user_id=1)
    _run_menu(update, context)

    assert service.run_calls == [["digikala2"]]  # digikala2's job did start
    assert job_manager.current("digikala") is existing  # digikala's own run untouched
    assert job_manager.current("digikala2") is None  # digikala2's job already finished/released


# -- "Failed + unchecked only" confirmation ---------------------------------------
#
# The question is computed from the CURRENT sheet (FakeProductSource) plus the
# latest persisted state of every product (FakeStateRepository.
# list_all_product_states) — never from a single past run.


def _sheet_product(pid: str, platform: str = "digikala") -> Product:
    return Product(
        internal_id=pid,
        name=f"Widget {pid}",
        reference_price=1_000_000,
        target_price=1_100_000,
        platform_refs={platform: f"ref-{pid}"}, display_name=f"Widget {pid}", green_platforms=frozenset({platform}),
    )


def _state(
    pid: str,
    status: OutcomeStatus,
    platform: str = "digikala",
    *,
    is_capped: bool = False,
) -> ProductStateRow:
    return ProductStateRow(
        product_id=pid,
        platform=platform,
        product_name=f"Widget {pid}",
        reference_price=1_000_000,
        target_price=1_100_000,
        applied_price=None,
        is_capped=is_capped,
        variant_id="V1",
        status=status.value,
        retry_count=0,
        last_error=None,
        updated_at="",
    )


def _backlog_service(
    products: list[Product], states: list[ProductStateRow], **kwargs: Any
) -> tuple[FakeService, FakeStateRepository]:
    repo = FakeStateRepository(product_states=states)
    service = FakeService(
        state_repository=repo, product_source=FakeProductSource(products), **kwargs
    )
    return service, repo


def _ask(service: FakeService, *, dry_run: bool = True):
    settings = _settings(dry_run=dry_run)
    context = _context(settings, service)
    update = _update("🛒 🔴 Digikala", user_id=1)
    _run(handlers.menu_message_handler(update, context))
    return update, context


def _answer(context, text: str) -> None:
    _run_menu(_update(text, user_id=1), context)


def test_no_question_when_platform_has_never_been_processed():
    # Nothing recorded at all -> "everything is unchecked" is just a normal full run.
    service, _ = _backlog_service([_sheet_product("p1"), _sheet_product("p2")], [])
    _run_menu(_update("🛒 🔴 Digikala"), _context(_settings(), service))

    assert service.run_calls == [["digikala"]]
    assert service.rerun_specific_calls == []


def test_no_question_when_every_product_already_succeeded():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    states = [_state("p1", OutcomeStatus.SUCCESS), _state("p2", OutcomeStatus.SKIPPED_NO_CHANGE)]
    service, _ = _backlog_service(products, states)
    _run_menu(_update("🛒 🔴 Digikala"), _context(_settings(), service))

    assert service.run_calls == [["digikala"]]
    assert service.rerun_specific_calls == []


def test_question_lists_failed_and_unchecked_counts():
    products = [_sheet_product(p) for p in ("p1", "p2", "p3", "p4", "p5")]
    states = [
        _state("p1", OutcomeStatus.SUCCESS),
        _state("p2", OutcomeStatus.FAILED_API),
        _state("p3", OutcomeStatus.FAILED_INVALID_DATA),
        # p4, p5 never checked
    ]
    service, _ = _backlog_service(products, states)
    update, context = _ask(service)

    assert service.run_calls == []  # not started until the person answers
    args, kwargs = update.message.reply_text.call_args
    assert "2 کالا ناموفق" in args[0]
    assert "2 کالا هنوز بررسی نشده" in args[0]
    assert "Digikala" in args[0]
    labels = {b.text for row in kwargs["reply_markup"].keyboard for b in row}
    assert "✅ بله" in labels
    assert "❌ خیر" in labels
    assert context.user_data["pending_confirmation"] == {
        "type": "retry_failed_only",
        "platform": "digikala",
        "user_id": 1,
    }


def test_question_shown_when_only_unchecked_products_exist():
    # e.g. products that were previously skipped by a sheet-reading bug and
    # only became eligible later: nothing failed, but they were never checked.
    products = [_sheet_product("p1"), _sheet_product("p2")]
    service, _ = _backlog_service(products, [_state("p1", OutcomeStatus.SUCCESS)])
    update, context = _ask(service)

    args, _ = update.message.reply_text.call_args
    assert "1 کالا هنوز بررسی نشده" in args[0]
    assert "ناموفق" not in args[0].split("\n", 1)[1].split("آیا")[0]
    assert context.user_data["pending_confirmation"]["type"] == "retry_failed_only"


def test_question_shown_when_only_failed_products_exist():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    states = [_state("p1", OutcomeStatus.SUCCESS), _state("p2", OutcomeStatus.FAILED_API)]
    service, _ = _backlog_service(products, states)
    update, _ = _ask(service)

    args, _ = update.message.reply_text.call_args
    assert "1 کالا ناموفق" in args[0]
    assert "بررسی نشده" not in args[0]


def test_capped_products_count_as_failed():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    states = [
        _state("p1", OutcomeStatus.SUCCESS),
        _state("p2", OutcomeStatus.CAPPED_12PCT, is_capped=True),
    ]
    service, _ = _backlog_service(products, states)
    update, context = _ask(service)

    args, _ = update.message.reply_text.call_args
    assert "1 کالا ناموفق" in args[0]
    _answer(context, "✅ بله")
    assert service.rerun_specific_calls == [("digikala", ["p2"])]


def test_confirming_processes_failed_and_unchecked_in_sheet_order():
    products = [_sheet_product(p) for p in ("p1", "p2", "p3", "p4")]
    states = [
        _state("p1", OutcomeStatus.SUCCESS),
        _state("p3", OutcomeStatus.FAILED_API),
    ]
    service, _ = _backlog_service(products, states)
    _, context = _ask(service)

    _answer(context, "✅ بله")

    assert service.run_calls == []  # the normal full process never ran
    # p1 (SUCCESS) excluded; p2 (unchecked), p3 (failed), p4 (unchecked) included.
    assert service.rerun_specific_calls == [("digikala", ["p2", "p3", "p4"])]


def test_declining_starts_a_new_full_process():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    states = [_state("p1", OutcomeStatus.FAILED_API)]
    service, _ = _backlog_service(products, states)
    _, context = _ask(service)

    _answer(context, "❌ خیر")

    assert service.run_calls == [["digikala"]]
    assert service.rerun_specific_calls == []


def test_backlog_ignores_other_platforms_and_ineligible_products():
    ineligible = Product(
        internal_id="no-target",
        name="no target",
        reference_price=1,
        target_price=None,
        platform_refs={"digikala": "x"}, display_name="no target", green_platforms=frozenset({"digikala"}),
    )
    other_platform_only = _sheet_product("p9", platform="basalam")
    products = [_sheet_product("p1"), ineligible, other_platform_only]
    states = [
        _state("p1", OutcomeStatus.SUCCESS),
        # failure on ANOTHER platform must not leak into digikala's question
        _state("p1", OutcomeStatus.FAILED_API, platform="basalam"),
        # state row for a product that is no longer eligible is ignored
        _state("no-target", OutcomeStatus.FAILED_API),
    ]
    service, _ = _backlog_service(products, states)
    _run_menu(_update("🛒 🔴 Digikala"), _context(_settings(), service))

    assert service.run_calls == [["digikala"]]  # nothing outstanding -> no question
    assert service.rerun_specific_calls == []


def test_backlog_is_recomputed_when_the_person_answers():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    repo_states = [_state("p1", OutcomeStatus.SUCCESS)]
    service, repo = _backlog_service(products, repo_states)
    _, context = _ask(service)

    # While the question was showing, p2 got processed (e.g. by another run).
    repo_states.append(_state("p2", OutcomeStatus.SUCCESS))
    calls_before = service.product_source.fetch_products_calls
    update = _update("✅ بله", user_id=1)
    _run_menu(update, context)

    assert service.product_source.fetch_products_calls == calls_before + 1
    assert service.rerun_specific_calls == []  # nothing left -> nothing started
    assert service.run_calls == []
    args, _ = update.message.reply_text.call_args
    assert "باقی نمانده" in args[0]


def test_retry_does_not_store_product_ids_in_user_data():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    service, _ = _backlog_service(products, [_state("p1", OutcomeStatus.FAILED_API)])
    _, context = _ask(service)

    assert context.user_data["pending_confirmation"] == {
        "type": "retry_failed_only",
        "platform": "digikala",
        "user_id": 1,
    }


def test_retry_start_banner_names_both_counts():
    products = [_sheet_product(p) for p in ("p1", "p2", "p3")]
    states = [_state("p1", OutcomeStatus.FAILED_API)]
    service, _ = _backlog_service(products, states)
    _, context = _ask(service)

    _answer(context, "✅ بله")

    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    banner = [t for t in send_texts if "بررسی مجدد" in t]
    assert len(banner) == 1
    assert "3 کالا" in banner[0]
    assert "1 ناموفق" in banner[0]
    assert "2 بررسی‌نشده" in banner[0]
    assert "Digikala" in banner[0]


def test_retry_final_report_includes_the_scope_note():
    products = [_sheet_product(p) for p in ("p1", "p2", "p3")]
    states = [_state("p1", OutcomeStatus.SUCCESS), _state("p2", OutcomeStatus.FAILED_API)]
    service, _ = _backlog_service(products, states)
    _, context = _ask(service, dry_run=False)

    _answer(context, "✅ بله")

    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    report_sends = [t for t in send_texts if "این اجرا فقط شامل" in t]
    assert len(report_sends) == 1
    assert "2 کالا" in report_sends[0]
    assert "1 ناموفق" in report_sends[0]
    assert "1 بررسی‌نشده" in report_sends[0]


def test_declining_final_report_has_no_scope_note():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    service, _ = _backlog_service(products, [_state("p1", OutcomeStatus.FAILED_API)])
    _, context = _ask(service, dry_run=False)

    _answer(context, "❌ خیر")

    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    assert not any("این اجرا فقط شامل" in t for t in send_texts)


def test_parallel_run_warning_takes_priority_over_the_retry_question():
    # Design assumption: when both would apply, only the parallel-run warning
    # fires — never a second, chained question.
    products = [_sheet_product("p1"), _sheet_product("p2")]
    service, _ = _backlog_service(products, [_state("p1", OutcomeStatus.FAILED_API)])
    job_manager = PlatformJobManager()
    job_manager.try_start("digikala", triggered_by="telegram:2")  # already running
    context = _context(_settings(), service, job_manager)
    update = _update("🛒 🔴 Digikala", user_id=1)

    _run(handlers.menu_message_handler(update, context))

    assert context.user_data["pending_confirmation"]["type"] == "parallel_run"
    # The backlog check never even ran -> the sheet was never fetched.
    assert service.product_source.fetch_products_calls == 0

    _answer(context, "✅ بله")

    # Confirming the parallel-run warning starts the normal full process.
    assert service.run_calls == [["digikala"]]
    assert service.rerun_specific_calls == []


def _start_banner_call(context):
    calls = [
        c for c in context.bot.send_message.await_args_list if "شروع" in c.kwargs.get("text", "")
    ]
    assert len(calls) == 1
    return calls[0]


def _is_main_menu(markup) -> bool:
    labels = {b.text for row in markup.keyboard for b in row}
    return BTN_SELECT_PLATFORM in labels and "✅ بله" not in labels


def test_answering_yes_returns_to_the_main_menu():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    service, _ = _backlog_service(products, [_state("p1", OutcomeStatus.FAILED_API)])
    _, context = _ask(service)

    _answer(context, "✅ بله")

    assert _is_main_menu(_start_banner_call(context).kwargs["reply_markup"])


def test_answering_no_returns_to_the_main_menu():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    service, _ = _backlog_service(products, [_state("p1", OutcomeStatus.FAILED_API)])
    _, context = _ask(service)

    _answer(context, "❌ خیر")

    assert service.run_calls == [["digikala"]]
    assert _is_main_menu(_start_banner_call(context).kwargs["reply_markup"])


def test_answering_yes_with_nothing_left_returns_to_the_main_menu():
    products = [_sheet_product("p1"), _sheet_product("p2")]
    states = [_state("p1", OutcomeStatus.SUCCESS)]
    service, _ = _backlog_service(products, states)
    _, context = _ask(service)
    states.append(_state("p2", OutcomeStatus.SUCCESS))

    update = _update("✅ بله", user_id=1)
    _run_menu(update, context)

    _, kwargs = update.message.reply_text.call_args
    assert _is_main_menu(kwargs["reply_markup"])



# -- "All platforms" run ----------------------------------------------------------

_THREE = ("digikala", "digikala2", "snappshop_tehran")


def _all_platforms_context(available=_THREE, *, run_impl=None, job_manager=None, dry_run=True):
    service = FakeService(available_platforms=available, run_impl=run_impl)
    context = _context(_settings(dry_run=dry_run), service, job_manager)
    return service, context


def _sent_texts(context) -> list[str]:
    return [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]


def test_all_platforms_runs_every_platform_in_order_one_after_another():
    events: list[str] = []

    def run_impl(platform_names, *, triggered_by, **hooks):
        (platform,) = platform_names
        events.append(f"start:{platform}")
        events.append(f"end:{platform}")
        return {platform: RunSummary(platform=platform, triggered_by=triggered_by)}

    service, context = _all_platforms_context(run_impl=run_impl)

    _run_menu(_update(BTN_ALL_PLATFORMS), context)

    assert service.run_calls == [["digikala"], ["digikala2"], ["snappshop_tehran"]]
    # Strictly sequential: each platform ends before the next one starts.
    assert events == [
        "start:digikala", "end:digikala",
        "start:digikala2", "end:digikala2",
        "start:snappshop_tehran", "end:snappshop_tehran",
    ]  # fmt: skip


def test_all_platforms_starts_with_digikala_even_if_registry_order_differs():
    service, context = _all_platforms_context(
        available=("basalam", "snappshop_tehran", "digikala", "digikala2")
    )

    _run_menu(_update(BTN_ALL_PLATFORMS), context)

    assert [c[0] for c in service.run_calls] == [
        "digikala",
        "digikala2",
        "snappshop_tehran",
        "basalam",
    ]


def test_all_platforms_next_platform_waits_for_previous_job_to_be_released():
    job_manager = PlatformJobManager()
    seen_active: list[tuple[str, bool]] = []

    def run_impl(platform_names, *, triggered_by, **hooks):
        (platform,) = platform_names
        # While this platform runs, no OTHER platform's job may be active.
        others = [p for p in _THREE if p != platform and job_manager.is_active(p)]
        seen_active.append((platform, bool(others)))
        return {platform: RunSummary(platform=platform, triggered_by=triggered_by)}

    service, context = _all_platforms_context(run_impl=run_impl, job_manager=job_manager)
    _run_menu(_update(BTN_ALL_PLATFORMS), context)

    assert seen_active == [(p, False) for p in _THREE]
    assert not any(job_manager.is_active(p) for p in _THREE)  # everything released


def test_all_platforms_sends_start_and_finish_messages_and_returns_to_main_menu():
    service, context = _all_platforms_context()

    _run_menu(_update(BTN_ALL_PLATFORMS), context)

    texts = _sent_texts(context)
    started = [t for t in texts if "پردازش همه‌ی پلتفرم‌ها شروع شد" in t]
    assert len(started) == 1
    # order listed in the start message
    assert started[0].index("Digikala\n") < started[0].index("Digikala Saree")
    assert started[0].index("Digikala Saree") < started[0].index("SnappShop Tehran")
    finished = [t for t in texts if "پایان رسید" in t]
    assert finished == ["✅ پردازش همه‌ی پلتفرم‌ها به پایان رسید (3 از 3)."]
    last = context.bot.send_message.await_args_list[-1]
    assert _is_main_menu(last.kwargs["reply_markup"])


def test_all_platforms_stop_halts_the_sequence_and_lists_what_did_not_run():
    job_manager = PlatformJobManager()

    def run_impl(platform_names, *, triggered_by, **hooks):
        (platform,) = platform_names
        if platform == "digikala2":
            job_manager.request_cancel("digikala2")  # user pressed Stop mid-run
        return {platform: RunSummary(platform=platform, triggered_by=triggered_by)}

    service, context = _all_platforms_context(run_impl=run_impl, job_manager=job_manager)
    _run_menu(_update(BTN_ALL_PLATFORMS), context)

    assert service.run_calls == [["digikala"], ["digikala2"]]  # snappshop never started
    stopped = [t for t in _sent_texts(context) if "پردازش همه‌ی پلتفرم‌ها متوقف شد" in t]
    assert len(stopped) == 1
    assert "SnappShop Tehran" in stopped[0]
    assert "Digikala Saree" not in stopped[0]
    assert not any("پایان رسید" in t for t in _sent_texts(context))


def test_all_platforms_refuses_when_someone_else_is_running_a_platform():
    job_manager = PlatformJobManager()
    job_manager.try_start("digikala2", triggered_by="telegram:2")
    service, context = _all_platforms_context(job_manager=job_manager)
    update = _update(BTN_ALL_PLATFORMS)

    _run_menu(update, context)

    assert service.run_calls == []
    args, kwargs = update.message.reply_text.call_args
    assert "پردازش دیگری در حال انجام است" in args[0]
    assert _is_main_menu(kwargs["reply_markup"])


def test_all_platforms_refuses_a_second_start_while_the_first_is_running():
    service, context = _all_platforms_context()
    context.application.bot_data["all_platforms_batch"] = object()  # a batch is in flight
    update = _update(BTN_ALL_PLATFORMS)

    _run_menu(update, context)

    assert service.run_calls == []
    args, _ = update.message.reply_text.call_args
    assert "از قبل در حال اجراست" in args[0]


def test_all_platforms_waits_for_a_platform_the_hourly_recheck_is_holding(monkeypatch):
    monkeypatch.setattr(handlers, "_BATCH_LOCK_POLL_SECONDS", 0.01)
    job_manager = PlatformJobManager()
    scheduler_job = job_manager.try_start("digikala2", triggered_by="scheduler:hourly")
    polls = {"n": 0}

    real_try_start = job_manager.try_start

    def try_start(platform, *, triggered_by):
        if platform == "digikala2" and scheduler_job is not None:
            polls["n"] += 1
            if polls["n"] == 3:  # the recheck finishes after two blocked attempts
                job_manager.finish("digikala2", scheduler_job)
        return real_try_start(platform, triggered_by=triggered_by)

    monkeypatch.setattr(job_manager, "try_start", try_start)
    service, context = _all_platforms_context(job_manager=job_manager)

    _run_menu(_update(BTN_ALL_PLATFORMS), context)

    assert polls["n"] >= 3
    assert service.run_calls == [["digikala"], ["digikala2"], ["snappshop_tehran"]]


def test_all_platforms_batch_is_released_afterwards():
    service, context = _all_platforms_context()

    _run_menu(_update(BTN_ALL_PLATFORMS), context)

    assert "all_platforms_batch" not in context.application.bot_data
    # ...so it can be started again
    _run_menu(_update(BTN_ALL_PLATFORMS), context)
    assert len(service.run_calls) == 6


def test_stop_button_is_offered_while_only_a_batch_is_active():
    service, context = _all_platforms_context()
    batch = handlers.AllPlatformsBatch(triggered_by="telegram:1", remaining=["digikala"])
    context.application.bot_data["all_platforms_batch"] = batch

    _run(handlers.menu_message_handler(_update(BTN_STOP), context))
    assert context.user_data["pending_confirmation"]["type"] == "stop"
    _run(handlers.menu_message_handler(_update("✅ بله"), context))

    assert batch.cancel_requested is True


def test_clear_database_is_blocked_while_a_batch_is_active():
    service, context = _all_platforms_context()
    context.application.bot_data["all_platforms_batch"] = handlers.AllPlatformsBatch(
        triggered_by="telegram:1"
    )
    update = _update("🧹 پاکسازی دیتابیس")

    _run(handlers.menu_message_handler(update, context))

    assert "pending_confirmation" not in context.user_data


def test_all_platforms_button_appears_in_the_platform_submenu():
    service, context = _all_platforms_context()
    update = _update(BTN_SELECT_PLATFORM)

    _run(handlers.menu_message_handler(update, context))

    _, kwargs = update.message.reply_text.call_args
    rows = [[b.text for b in row] for row in kwargs["reply_markup"].keyboard]
    assert rows[-2] == [BTN_ALL_PLATFORMS]


# -- Status menu --------------------------------------------------------------------


def test_status_shows_nothing_running():
    settings = _settings()
    context = _context(settings, FakeService())
    update = _update(BTN_STATUS)

    _run(handlers.menu_message_handler(update, context))

    args, _ = update.message.reply_text.call_args
    assert args[0] == "⚪ در حال حاضر اجرایی فعال نیست."


def test_status_shows_live_progress_while_running():
    settings = _settings()
    job_manager = PlatformJobManager()
    progress = job_manager.try_start("digikala", triggered_by="telegram:1")
    progress.total = 10
    progress.processed = 3
    progress.current_product_name = "Widget 3"
    context = _context(settings, FakeService(), job_manager)
    update = _update(BTN_STATUS)

    _run(handlers.menu_message_handler(update, context))

    args, _ = update.message.reply_text.call_args
    assert "🟢 در حال اجرا" in args[0]
    assert "3 / 10" in args[0]
    assert "Widget 3" in args[0]


def test_status_hides_a_platform_whose_active_job_is_the_scheduler():
    # The hourly recheck job never feeds progress counters (it isn't
    # something a person watches live), so it must never show up as a
    # phantom "🟢 در حال اجرا ... 0 / 0" entry — this was mistakenly
    # happening before triggered_by was checked in handlers._handle_status.
    settings = _settings()
    job_manager = PlatformJobManager()
    progress = job_manager.try_start("digikala", triggered_by="scheduler:hourly")
    progress.total = 10
    progress.processed = 3
    context = _context(settings, FakeService(), job_manager)
    update = _update(BTN_STATUS)

    _run(handlers.menu_message_handler(update, context))

    args, _ = update.message.reply_text.call_args
    assert args[0] == "⚪ در حال حاضر اجرایی فعال نیست."


# -- Graceful cancellation ----------------------------------------------------------


def test_stop_with_nothing_running():
    settings = _settings()
    context = _context(settings, FakeService())
    update = _update(BTN_STOP)

    _run(handlers.menu_message_handler(update, context))

    args, _ = update.message.reply_text.call_args
    assert args[0] == "⚪ در حال حاضر اجرایی فعال نیست."


def test_stop_asks_for_confirmation_before_cancelling():
    settings = _settings()
    job_manager = PlatformJobManager()
    progress = job_manager.try_start("digikala", triggered_by="telegram:1")
    context = _context(settings, FakeService(), job_manager)
    update = _update(BTN_STOP)

    _run(handlers.menu_message_handler(update, context))

    # Not cancelled yet — only the warning was shown.
    assert progress.cancel_requested is False
    args, kwargs = update.message.reply_text.call_args
    assert "تاریخچه پردازش کالا" in args[0]
    assert "از بین می‌رود" in args[0]
    labels = {b.text for row in kwargs["reply_markup"].keyboard for b in row}
    assert "✅ بله" in labels
    assert "❌ خیر" in labels
    assert context.user_data["pending_confirmation"] == {"type": "stop"}


def test_confirming_stop_cancels_the_running_job():
    settings = _settings()
    job_manager = PlatformJobManager()
    progress = job_manager.try_start("digikala", triggered_by="telegram:1")
    context = _context(settings, FakeService(), job_manager)
    _run(handlers.menu_message_handler(_update(BTN_STOP), context))  # confirmation prompt

    update = _update("✅ بله")
    _run(handlers.menu_message_handler(update, context))

    assert progress.cancel_requested is True
    args, _ = update.message.reply_text.call_args
    assert "درخواست توقف ثبت شد" in args[0]


def test_declining_stop_leaves_the_running_job_untouched():
    settings = _settings()
    job_manager = PlatformJobManager()
    progress = job_manager.try_start("digikala", triggered_by="telegram:1")
    context = _context(settings, FakeService(), job_manager)
    _run(handlers.menu_message_handler(_update(BTN_STOP), context))  # confirmation prompt

    update = _update("❌ خیر")
    _run(handlers.menu_message_handler(update, context))

    assert progress.cancel_requested is False
    args, _ = update.message.reply_text.call_args
    assert "لغو شد" in args[0]


def test_cancellation_finishes_current_product_then_stops_before_the_next(monkeypatch):
    settings = _settings(dry_run=False)
    job_manager = PlatformJobManager()

    def run_impl(platform_names, *, triggered_by, on_products_loaded, on_product_start,
                 on_result, should_cancel, on_round_start=None):
        products = [_product("a"), _product("b"), _product("c")]
        on_products_loaded(len(products))
        results = []
        for p in products:
            if should_cancel():
                break
            on_product_start(p)
            result = UpdateResult(
                product=p, platform="digikala", status=OutcomeStatus.SUCCESS,
                previous_price=1_000_000, applied_price=1_100_000, target_price=1_100_000,
                variant_id="V1",
            )
            results.append(result)
            on_result(result)
            # Simulate the Stop button being pressed while "a" was in
            # flight: it must still finish (it already has, right above),
            # and "b"/"c" must never start.
            if p.internal_id == "a":
                job_manager.request_cancel("digikala")
        return {"digikala": RunSummary(platform="digikala", triggered_by=triggered_by, results=results)}

    service = FakeService(run_impl=run_impl)
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)

    # Exactly one product processed, and the "stopped" message (not the
    # normal final summary) was sent, with the correct processed count.
    stop_texts = [
        call.kwargs.get("text", "") for call in context.bot.send_message.await_args_list
    ]
    assert any("⏹ اجرا متوقف شد" in t and "1" in t for t in stop_texts)
    assert job_manager.current("digikala") is None  # lock released


def test_stopped_run_also_sends_a_partial_final_report_with_an_interrupted_note():
    # The "⏹ اجرا متوقف شد" message is not the only thing sent after a stop —
    # the same performance report a completed run gets is still sent,
    # covering whatever was processed before the stop, with a note making
    # clear it's partial.
    settings = _settings(dry_run=False)
    job_manager = PlatformJobManager()

    def run_impl(platform_names, *, triggered_by, on_products_loaded, on_product_start,
                 on_result, should_cancel, on_round_start=None):
        products = [_product("Alpha"), _product("Beta")]
        on_products_loaded(len(products))
        results = []
        for p in products:
            if should_cancel():
                break
            on_product_start(p)
            result = UpdateResult(
                product=p, platform="digikala", status=OutcomeStatus.SUCCESS,
                previous_price=1_000_000, applied_price=1_100_000, target_price=1_100_000,
                variant_id="V1",
            )
            results.append(result)
            on_result(result)
            if p.internal_id == "Alpha":
                job_manager.request_cancel("digikala")
        return {"digikala": RunSummary(platform="digikala", triggered_by=triggered_by, results=results)}

    service = FakeService(run_impl=run_impl)
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)

    texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    assert any("⏹ اجرا متوقف شد" in t for t in texts)
    report = next(t for t in texts if "گزارش نهایی" in t)
    assert "اجرا توسط کاربر متوقف شد؛ این گزارش فقط شامل محصولات پردازش‌شده تا آن لحظه است" in report
    assert "✅ موفق: 1" in report  # Alpha was collected before the stop…
    assert "Alpha" not in report  # …but never listed by name
    assert "Beta" not in report  # never started, so absent from every list


# -- Unexpected crash mid-run: the lock is released and a partial report still goes out --


def test_unexpected_crash_mid_run_releases_the_lock_and_sends_a_partial_report():
    settings = _settings(dry_run=False)
    job_manager = PlatformJobManager()

    def run_impl(platform_names, *, triggered_by, on_products_loaded, on_product_start,
                 on_result, should_cancel, on_round_start=None):
        on_products_loaded(2)
        product = _product("Survivor")
        on_product_start(product)
        result = UpdateResult(
            product=product, platform="digikala", status=OutcomeStatus.SUCCESS,
            previous_price=1_000_000, applied_price=1_100_000, target_price=1_100_000,
            variant_id="V1",
        )
        on_result(result)
        raise RuntimeError("sheet source exploded")  # e.g. the sheet failed to load

    service = FakeService(run_impl=run_impl)
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)

    texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    assert any("❌ اجرا با خطای غیرمنتظره متوقف شد" in t and "1" in t for t in texts)
    report = next(t for t in texts if "گزارش نهایی" in t)
    assert "اجرا با خطا متوقف شد؛ این گزارش فقط شامل محصولات پردازش‌شده تا آن لحظه است" in report
    assert "✅ موفق: 1" in report  # the one collected result is counted…
    assert "Survivor" not in report  # …but never listed by name
    assert job_manager.current("digikala") is None  # never left "stuck busy"


def test_unexpected_crash_notice_goes_only_to_admin_when_admin_chat_id_is_set():
    # Regular team members (TELEGRAM_ALLOWED_USER_IDS) must not see the raw
    # unexpected-error notice once ADMIN_CHAT_ID is configured — only the
    # admin chat gets it. The partial final report itself still goes to
    # everyone (admin included, with the exact same text — see
    # _send_final_report).
    settings = _settings(
        dry_run=False, telegram_allowed_user_ids="1,2", admin_chat_id=999
    )
    job_manager = PlatformJobManager()

    def run_impl(platform_names, *, triggered_by, on_products_loaded, on_product_start,
                 on_result, should_cancel, on_round_start=None):
        on_products_loaded(2)
        product = _product("Survivor")
        on_product_start(product)
        result = UpdateResult(
            product=product, platform="digikala", status=OutcomeStatus.SUCCESS,
            previous_price=1_000_000, applied_price=1_100_000, target_price=1_100_000,
            variant_id="V1",
        )
        on_result(result)
        raise RuntimeError("sheet source exploded")

    service = FakeService(run_impl=run_impl)
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)

    calls = context.bot.send_message.await_args_list
    error_calls = [
        c for c in calls if "❌ اجرا با خطای غیرمنتظره متوقف شد" in c.kwargs.get("text", "")
    ]
    assert error_calls  # the notice was sent at all
    assert all(c.kwargs.get("chat_id") == 999 for c in error_calls)


def test_crash_before_any_product_is_processed_sends_only_the_error_message():
    # No collected results at all -> no partial report is sent (there is
    # nothing to report), just the unexpected-error notice.
    settings = _settings(dry_run=False)
    job_manager = PlatformJobManager()

    def run_impl(*args, **kwargs):
        raise RuntimeError("sheet source exploded")

    service = FakeService(run_impl=run_impl)
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)

    texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    assert any("❌ اجرا با خطای غیرمنتظره متوقف شد" in t and "0" in t for t in texts)
    assert not any("گزارش نهایی" in t for t in texts)
    assert job_manager.current("digikala") is None


# -- No live per-product messages; exactly one final report -----------------------


def _run_with_results(results: list[UpdateResult], *, dry_run: bool = False):
    settings = _settings(dry_run=dry_run)

    def run_impl(platform_names, *, triggered_by, on_products_loaded, on_product_start, on_result,
                 should_cancel, on_round_start=None):
        on_products_loaded(len(results))
        for result in results:
            on_product_start(result.product)
            on_result(result)
        return {"digikala": RunSummary(platform="digikala", triggered_by=triggered_by, results=results)}

    service = FakeService(run_impl=run_impl)
    context = _context(settings, service)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)
    return context


def test_no_message_is_ever_edited_during_a_run():
    # Removing live per-product reporting means there is no "current
    # product" status message to edit at all anymore.
    result = UpdateResult(
        product=_product("Nut Bowl"), platform="digikala", status=OutcomeStatus.SUCCESS,
        previous_price=1_000_000, applied_price=1_100_000, target_price=1_100_000,
        variant_id="83841925",
    )
    context = _run_with_results([result])
    context.bot.edit_message_text.assert_not_awaited()


def test_only_one_final_report_is_sent_for_a_completed_run():
    result = UpdateResult(
        product=_product("Nut Bowl"), platform="digikala", status=OutcomeStatus.SUCCESS,
        previous_price=1_000_000, applied_price=1_100_000, target_price=1_100_000,
        variant_id="83841925",
    )
    context = _run_with_results([result], dry_run=False)

    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    # "▶️ شروع پردازش…" is the one allowed non-per-product, run-level
    # notice; the report itself must appear exactly once.
    report_sends = [t for t in send_texts if "📊 گزارش نهایی بروزرسانی 🔴 Digikala" in t]
    assert len(report_sends) == 1
    # Successful products are counted, but never listed by name.
    assert "✅ موفق: 1" in report_sends[0]
    assert "Nut Bowl" not in report_sends[0]


def test_no_per_product_messages_are_sent_for_capped_not_found_or_failed():
    # No message is sent per product WHILE THE RUN IS IN PROGRESS — only the
    # run-start notice arrives before the run finishes. (The final report,
    # sent once the run is done, DOES list these products with their
    # reasons — see the next test — so this is about live spam during the
    # run, not about the report's content.)
    capped = UpdateResult(
        product=_product("Capped Item"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
        previous_price=1_000_000, applied_price=1_120_000, target_price=2_000_000,
    )
    not_found = UpdateResult(
        product=Product(
            internal_id="X", name="Missing Item", reference_price=None, target_price=1_000_000,
            platform_refs={"digikala": "999999"}, display_name="Missing Item", green_platforms=frozenset({"digikala"}),
        ),
        platform="digikala",
        status=OutcomeStatus.FAILED_MISSING_IDENTIFIER,
        error_message="No DigiKala variant found matching DKP code '999999'.",
    )
    failed = UpdateResult(
        product=_product("Broken Item"), platform="digikala", status=OutcomeStatus.FAILED_API,
        error_message="DigiKala price update failed: HTTP 500: internal error",
    )
    context = _run_with_results([capped, not_found, failed])

    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    # Exactly the run-start notice and the one final report — nothing sent
    # per product in between while the run was in progress.
    assert len(send_texts) == 2
    assert send_texts[0] == "▶️ شروع پردازش 🛒 🔴 Digikala…"
    assert "گزارش نهایی" in send_texts[1]


def test_failed_and_not_found_products_appear_in_final_report_by_name_only():
    # Once the run is done, the final report's "ناموفق" section lists EVERY
    # product that wasn't corrected — including a missing platform
    # identifier and a real API error — by name only; no reason or error
    # text is included for any recipient.
    not_found = UpdateResult(
        product=Product(
            internal_id="X", name="Missing Item", reference_price=None, target_price=1_000_000,
            platform_refs={"digikala": "999999"}, display_name="Missing Item", green_platforms=frozenset({"digikala"}),
        ),
        platform="digikala",
        status=OutcomeStatus.FAILED_MISSING_IDENTIFIER,
        error_message="No DigiKala variant found matching DKP code '999999'.",
    )
    failed = UpdateResult(
        product=_product("Broken Item"), platform="digikala", status=OutcomeStatus.FAILED_API,
        error_message="DigiKala price update failed: HTTP 500: internal error",
    )
    context = _run_with_results([not_found, failed])

    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    report = next(t for t in send_texts if "گزارش نهایی" in t)
    section = report.split("❌ کالاهای ناموفق:")[1]
    assert "Missing Item" in section
    assert "Broken Item" in section
    assert "شناسه محصول در پلتفرم پیدا نشد" not in report
    assert "HTTP 500" not in report  # no error text shown to anyone
    assert report.count("هیچ موردی وجود ندارد.") == 1  # capped is empty


def test_capped_product_appears_in_manual_attention_list():
    capped = UpdateResult(
        product=_product("Capped Item"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
        previous_price=1_000_000, applied_price=1_120_000, target_price=2_000_000,
    )
    context = _run_with_results([capped])
    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    report = next(t for t in send_texts if "گزارش نهایی" in t)
    assert "تیکت ثبت کنید" in report
    assert "1. Capped Item" in report


def test_not_found_product_appears_alone_in_the_not_corrected_section():
    not_found = UpdateResult(
        product=Product(
            internal_id="X", name="Missing Item", reference_price=None, target_price=1_000_000,
            platform_refs={"digikala": "999999"}, display_name="Missing Item", green_platforms=frozenset({"digikala"}),
        ),
        platform="digikala",
        status=OutcomeStatus.FAILED_MISSING_IDENTIFIER,
        error_message="No DigiKala variant found matching DKP code '999999'.",
    )
    context = _run_with_results([not_found])
    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    report = next(t for t in send_texts if "گزارش نهایی" in t)
    section = report.split("❌ کالاهای ناموفق:")[1]
    assert "1. Missing Item" in section
    assert "شناسه محصول در پلتفرم پیدا نشد" not in report
    assert "No DigiKala variant found matching DKP code '999999'." not in report
    assert report.count("هیچ موردی وجود ندارد.") == 1  # capped is empty


def test_dry_run_banner_is_sent_when_dry_run_true():
    result = UpdateResult(
        product=_product("A"), platform="digikala", status=OutcomeStatus.SUCCESS,
        previous_price=1000, applied_price=1000, target_price=1000,
    )
    context = _run_with_results([result], dry_run=True)
    texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    assert any("DRY RUN" in t for t in texts)
    # The final report itself also distinguishes simulated results.
    report = next(t for t in texts if "گزارش نهایی" in t)
    assert "DRY RUN" in report
    # The simulated success is still reflected in the report — as a count,
    # since the unified report never lists successful product names.
    assert "✅ موفق: 1" in report


def test_dry_run_banner_is_not_sent_when_dry_run_false():
    result = UpdateResult(
        product=_product("A"), platform="digikala", status=OutcomeStatus.SUCCESS,
        previous_price=1000, applied_price=1000, target_price=1000,
    )
    context = _run_with_results([result], dry_run=False)
    texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    assert not any("DRY RUN" in t for t in texts)


def test_no_message_ever_contains_a_secret():
    result = UpdateResult(
        product=_product("A"), platform="digikala", status=OutcomeStatus.FAILED_AUTH,
        error_message=(
            "DigiKala access token expired/invalid and refresh could not be "
            "completed (PUT /open-api/v1/variants/1 (401 response)): 401 Unauthorized"
        ),
    )
    context = _run_with_results([result])
    texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    all_text = "\n".join(texts)
    for secret_pattern in ("Bearer ", "Authorization", "access_token", "refresh_token", "eyJ"):
        assert secret_pattern not in all_text


def test_a_very_long_final_report_is_split_across_several_messages():
    # This is what mordad #1 fixes: Telegram rejects a message over 4096
    # characters, so a report for a large platform (hundreds of products)
    # used to be silently dropped and only reach the error log. Now it must
    # arrive, split into several ordered messages.
    #
    # The unified report no longer includes per-product error text (only
    # the failed product's name — see reporting.format_final_digikala_report),
    # so a large *count* of failed products is what's needed to push the
    # report past TELEGRAM_SAFE_MESSAGE_CHARS; 300 comfortably does it while
    # 40 no longer would.
    results = [
        UpdateResult(
            product=_product(f"Item {i}"), platform="digikala", status=OutcomeStatus.FAILED_API,
            error_message="DigiKala price update failed: " + ("x" * 150),
        )
        for i in range(300)
    ]
    context = _run_with_results(results)

    send_texts = [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]
    # More than the usual 2 messages (start banner + one report message) —
    # the report itself had to be split into several chunks.
    assert len(send_texts) > 2
    report_chunks = send_texts[1:]  # everything after the start banner
    for chunk in report_chunks:
        assert len(chunk) <= sending.TELEGRAM_SAFE_MESSAGE_CHARS
    joined = "".join(report_chunks)
    assert "Item 0" in joined and "Item 299" in joined

    # The main-menu keyboard must not repeat on every part of a split
    # report — only the LAST chunk sent carries it.
    # (The start banner, sent before the report, carries it too — that is what
    # returns the requester to the main menu the moment a run starts.)
    all_calls = context.bot.send_message.await_args_list
    keyboard_calls = [
        c for c in all_calls[1:] if c.kwargs.get("reply_markup") is not None
    ]
    assert len(keyboard_calls) == 1
    assert keyboard_calls[0] is all_calls[-1]


# -- Hourly recheck keeps working, and cannot overlap a manual run -----------------


def test_hourly_recheck_still_sends_its_existing_report_format():
    settings = _settings()
    summary = RunSummary(platform="digikala", triggered_by="scheduler:hourly")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1000, applied_price=1120, target_price=2000),
    ]
    service = FakeService(recheck_impl=lambda platforms, **kw: {"digikala": summary})
    context = _context(settings, service)

    _run(handlers.hourly_recheck_job(context))

    assert service.recheck_calls == [["digikala"]]
    context.bot.send_message.assert_awaited()
    args, kwargs = context.bot.send_message.await_args
    assert kwargs["chat_id"] == 1  # the one allowed user id
    assert "Hourly recheck" in kwargs["text"]


def test_hourly_recheck_skips_a_platform_whose_manual_run_is_active():
    settings = _settings()
    job_manager = PlatformJobManager()
    job_manager.try_start("digikala", triggered_by="telegram:1")  # manual run in progress
    service = FakeService()
    context = _context(settings, service, job_manager)

    _run(handlers.hourly_recheck_job(context))

    assert service.recheck_calls == []  # never touched the locked platform
    context.bot.send_message.assert_not_awaited()


def test_manual_run_asks_for_parallel_confirmation_while_hourly_recheck_holds_the_lock():
    settings = _settings()
    job_manager = PlatformJobManager()
    job_manager.try_start("digikala", triggered_by="scheduler:hourly")  # scheduler in progress
    service = FakeService()
    context = _context(settings, service, job_manager)
    update = _update("🛒 🔴 Digikala")

    _run(handlers.menu_message_handler(update, context))

    assert service.run_calls == []  # not started until confirmed
    update.message.reply_text.assert_awaited_once()
    args, _ = update.message.reply_text.call_args
    assert "پردازش دیگری" in args[0]
    assert "بروزرسانی خودکار زمان‌بندی‌شده" in args[0]


# -- Multiple Telegram chat IDs (TELEGRAM_CHAT_IDS) ---------------------------------


def test_manual_run_reports_broadcast_to_all_configured_chat_ids():
    settings = _settings(telegram_chat_ids="111,222,333")
    service = FakeService()
    context = _context(settings, service, PlatformJobManager())
    update = _update("🛒 🔴 Digikala", chat_id=100)  # the triggering chat is NOT in the list

    _run_menu(update, context)

    sent_chat_ids = {c.kwargs["chat_id"] for c in context.bot.send_message.await_args_list}
    assert sent_chat_ids == {111, 222, 333}
    assert 100 not in sent_chat_ids  # fully replaced, not appended to, when configured


def test_manual_run_reports_fall_back_to_triggering_chat_when_chat_ids_unset():
    settings = _settings()  # TELEGRAM_CHAT_IDS left unset
    service = FakeService()
    context = _context(settings, service, PlatformJobManager())
    update = _update("🛒 🔴 Digikala", chat_id=100)

    _run_menu(update, context)

    sent_chat_ids = {c.kwargs["chat_id"] for c in context.bot.send_message.await_args_list}
    assert sent_chat_ids == {100}  # unchanged from before TELEGRAM_CHAT_IDS existed


def test_every_manual_run_message_reaches_every_configured_chat_id():
    # Not just the final report -- the start banner, DRY_RUN banner, and
    # final report are each sent to every configured recipient.
    settings = _settings(telegram_chat_ids="111,222", dry_run=True)
    service = FakeService()
    context = _context(settings, service, PlatformJobManager())
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)

    texts_by_chat: dict[int, list[str]] = {111: [], 222: []}
    for call in context.bot.send_message.await_args_list:
        texts_by_chat[call.kwargs["chat_id"]].append(call.kwargs.get("text", ""))
    # Both recipients got the same set of messages (start banner, dry-run
    # banner, and the final report).
    assert len(texts_by_chat[111]) == len(texts_by_chat[222]) == 3
    assert texts_by_chat[111] == texts_by_chat[222]


def test_hourly_recheck_broadcasts_to_configured_chat_ids_when_set():
    settings = _settings(telegram_chat_ids="444,555")
    summary = RunSummary(platform="digikala", triggered_by="scheduler:hourly")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1000, applied_price=1120, target_price=2000),
    ]
    service = FakeService(recheck_impl=lambda platforms, **kw: {"digikala": summary})
    context = _context(settings, service)

    _run(handlers.hourly_recheck_job(context))

    sent_chat_ids = {c.kwargs["chat_id"] for c in context.bot.send_message.await_args_list}
    assert sent_chat_ids == {444, 555}
    assert 1 not in sent_chat_ids  # replaced TELEGRAM_ALLOWED_USER_IDS, not appended to


def test_hourly_recheck_falls_back_to_allowed_user_ids_when_chat_ids_unset():
    settings = _settings()  # TELEGRAM_CHAT_IDS left unset
    summary = RunSummary(platform="digikala", triggered_by="scheduler:hourly")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1000, applied_price=1120, target_price=2000),
    ]
    service = FakeService(recheck_impl=lambda platforms, **kw: {"digikala": summary})
    context = _context(settings, service)

    _run(handlers.hourly_recheck_job(context))

    sent_chat_ids = {c.kwargs["chat_id"] for c in context.bot.send_message.await_args_list}
    assert sent_chat_ids == {1}  # unchanged from before TELEGRAM_CHAT_IDS existed


def test_a_failing_recipient_does_not_prevent_other_recipients_from_receiving_reports():
    settings = _settings(telegram_chat_ids="111,222")
    service = FakeService()
    context = _context(settings, service, PlatformJobManager())

    from telegram.error import Forbidden

    real_send = context.bot.send_message

    async def _send_message(*, chat_id, **kwargs):
        if chat_id == 111:
            raise Forbidden("bot was blocked by this chat")
        return await real_send(chat_id=chat_id, **kwargs)

    context.bot.send_message = AsyncMock(side_effect=_send_message)
    update = _update("🛒 🔴 Digikala")

    _run_menu(update, context)  # must not raise despite chat 111 always failing

    sent_chat_ids = {
        c.kwargs["chat_id"] for c in context.bot.send_message.await_args_list if c.kwargs["chat_id"] == 222
    }
    assert 222 in sent_chat_ids  # chat 222 still received its reports


# -- "Who did what" change notice (TELEGRAM_CHAT_ID_1..10) ---------------------------


def _notice_settings(monkeypatch, **overrides: Any) -> Settings:
    # Isolated from any real TELEGRAM_* env vars / .env on the test machine.
    for n in range(1, 11):
        monkeypatch.delenv(f"TELEGRAM_CHAT_ID_{n}", raising=False)
        monkeypatch.delenv(f"TELEGRAM_CHAT_NAME_{n}", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)
    return _settings(_env_file=None, dry_run=False, **overrides)


def _notice_calls(context) -> list[Any]:
    return [
        c
        for c in context.bot.send_message.await_args_list
        if c.kwargs.get("text", "").startswith("🔔")
    ]


def test_change_notice_goes_to_other_numbered_chats_but_not_the_requester(monkeypatch):
    settings = _notice_settings(
        monkeypatch,
        telegram_allowed_user_ids="1",
        telegram_chat_id_1="1",
        telegram_chat_name_1="آقای اعتصامی",
        telegram_chat_id_2="222",
        telegram_chat_id_3="333",
    )
    context = _context(settings, FakeService(), PlatformJobManager())
    update = _update("🛒 🔴 Digikala", user_id=1, chat_id=1)

    _run_menu(update, context)

    notices = _notice_calls(context)
    assert {c.kwargs["chat_id"] for c in notices} == {222, 333}
    for call in notices:
        assert call.kwargs["text"] == format_change_notice("آقای اعتصامی", "🔴 Digikala")


def test_change_notice_also_reaches_telegram_chat_ids_recipients(monkeypatch):
    settings = _notice_settings(
        monkeypatch,
        telegram_chat_ids="111",
        telegram_chat_id_1="1",
        telegram_chat_id_2="222",
    )
    context = _context(settings, FakeService(), PlatformJobManager())
    update = _update("🛒 🔴 Digikala", user_id=1, chat_id=1)

    _run_menu(update, context)

    assert {c.kwargs["chat_id"] for c in _notice_calls(context)} == {111, 222}


def test_change_notice_uses_chat_id_when_requester_has_no_name(monkeypatch):
    settings = _notice_settings(monkeypatch, telegram_chat_id_1="1", telegram_chat_id_2="222")
    context = _context(settings, FakeService(), PlatformJobManager())
    update = _update("🛒 🔴 Digikala", user_id=1, chat_id=1)

    _run_menu(update, context)

    (call,) = _notice_calls(context)
    assert call.kwargs["text"] == format_change_notice("1", "🔴 Digikala")


def test_no_change_notice_when_numbered_chat_ids_are_empty(monkeypatch):
    settings = _notice_settings(monkeypatch, telegram_chat_ids="111,222")
    service = FakeService()
    context = _context(settings, service, PlatformJobManager())
    update = _update("🛒 🔴 Digikala", user_id=1, chat_id=100)

    _run_menu(update, context)

    assert _notice_calls(context) == []
    # Unchanged behavior: start banner + final report to each configured chat.
    assert len(context.bot.send_message.await_args_list) == 4
    assert {c.kwargs["chat_id"] for c in context.bot.send_message.await_args_list} == {111, 222}
    assert service.run_calls == [["digikala"]]


def test_no_change_notice_when_only_the_requester_is_numbered(monkeypatch):
    settings = _notice_settings(monkeypatch, telegram_chat_id_1="1")
    context = _context(settings, FakeService(), PlatformJobManager())
    update = _update("🛒 🔴 Digikala", user_id=1, chat_id=1)

    _run_menu(update, context)

    assert _notice_calls(context) == []


def test_change_notice_failure_does_not_block_the_run(monkeypatch):
    settings = _notice_settings(monkeypatch, telegram_chat_id_1="1", telegram_chat_id_2="222")
    service = FakeService()
    context = _context(settings, service, PlatformJobManager())

    async def _send(**kwargs):
        if kwargs.get("text", "").startswith("🔔"):
            raise RuntimeError("boom")  # permanent error -> logged & swallowed per recipient
        return MagicMock()

    context.bot.send_message.side_effect = _send
    update = _update("🛒 🔴 Digikala", user_id=1, chat_id=1)

    _run_menu(update, context)

    assert service.run_calls == [["digikala"]]


# -- "📊 آخرین وضعیت" (last status) -----------------------------------------
#
# Sends the product×platform price-status Excel report (reporting/
# status_matrix.py + reporting/xlsx_export.py) built from whatever is
# currently in product_state -- see handlers.py::_handle_last_status. No
# platform submenu anymore: the report always covers every platform at once.


def _sent_document_path(context) -> Path:
    """The real filesystem path _handle_last_status wrote to and passed as
    `document=` to context.bot.send_document -- recovered from the open file
    object AsyncMock captured, since the file is closed (but not deleted) by
    the time the test inspects it.
    """
    _, kwargs = context.bot.send_document.await_args
    return Path(kwargs["document"].name)


def test_last_status_button_sends_the_report_directly_no_submenu(tmp_path):
    service = FakeService(product_source=FakeProductSource([_product("Widget")]))
    settings = _settings(data_dir=tmp_path)
    context = _context(settings, service)
    update = _update(BTN_LAST_STATUS)

    _run(handlers.menu_message_handler(update, context))

    # No "which platform?" prompt anymore -- straight to the file.
    update.message.reply_text.assert_not_called()
    context.bot.send_document.assert_awaited_once()
    _, kwargs = context.bot.send_document.await_args
    assert kwargs["caption"] == "📊 آخرین وضعیت قیمت‌ها"
    assert kwargs["reply_markup"] == handlers.build_main_menu_keyboard()
    assert service.product_source.fetch_products_calls == 1
    assert service.state_repository.list_all_product_states_calls == 1


def test_last_status_report_is_sent_only_to_the_requesting_chat(tmp_path):
    service = FakeService(product_source=FakeProductSource([_product("Widget")]))
    settings = _settings(data_dir=tmp_path)
    context = _context(settings, service)

    _run(handlers.menu_message_handler(_update(BTN_LAST_STATUS, chat_id=777), context))

    context.bot.send_document.assert_awaited_once()
    _, kwargs = context.bot.send_document.await_args
    assert kwargs["chat_id"] == 777


def test_last_status_writes_an_xlsx_file_under_the_data_dir_exports_folder(tmp_path):
    service = FakeService(product_source=FakeProductSource([_product("Widget")]))
    settings = _settings(data_dir=tmp_path)
    context = _context(settings, service)

    _run(handlers.menu_message_handler(_update(BTN_LAST_STATUS), context))

    output_path = _sent_document_path(context)
    assert output_path.parent == tmp_path / "exports"
    assert output_path.suffix == ".xlsx"
    assert output_path.name.startswith("price_status_")


def test_last_status_matrix_sheet_reflects_every_product_platform_status(tmp_path):
    # One product with all four cell states across two platforms:
    #   digikala -> success (SKIPPED_NO_CHANGE counts as success)
    #   digikala2 -> failure (FAILED_API), plus no ref on the platforms below
    products = [
        Product(
            internal_id="p1",
            name="Widget",
            reference_price=1_000_000,
            target_price=1_100_000,
            platform_refs={"digikala": "123", "digikala2": "456"}, display_name="Widget", green_platforms=frozenset({"digikala", "digikala2"}),
        )
    ]
    states = [
        ProductStateRow(
            product_id="p1",
            platform="digikala",
            product_name="Widget",
            reference_price=1_000_000,
            target_price=1_100_000,
            applied_price=1_100_000,
            is_capped=False,
            variant_id=None,
            status=OutcomeStatus.SKIPPED_NO_CHANGE.value,
            retry_count=0,
            last_error=None,
            updated_at="2026-09-20T10:00:00+00:00",
        ),
        ProductStateRow(
            product_id="p1",
            platform="digikala2",
            product_name="Widget",
            reference_price=1_000_000,
            target_price=1_100_000,
            applied_price=None,
            is_capped=False,
            variant_id=None,
            status=OutcomeStatus.FAILED_API.value,
            retry_count=1,
            last_error="server exploded",
            updated_at="2026-09-21T10:00:00+00:00",
        ),
    ]
    service = FakeService(
        available_platforms=["digikala", "digikala2", "basalam"],
        product_source=FakeProductSource(products),
        state_repository=FakeStateRepository(product_states=states),
    )
    settings = _settings(data_dir=tmp_path)
    context = _context(settings, service)

    _run(handlers.menu_message_handler(_update(BTN_LAST_STATUS), context))

    workbook = load_workbook(_sent_document_path(context))
    matrix_sheet = workbook["وضعیت قیمت‌ها"]
    header = [c.value for c in matrix_sheet[1]]
    row = [c.value for c in matrix_sheet[2]]
    values_by_header = dict(zip(header, row))

    assert values_by_header["نام کالا"] == "Widget"
    assert "✅" in values_by_header["🔴 Digikala"]
    assert "❌" in values_by_header["🔴 Digikala Saree"]
    assert values_by_header["🔵 Basalam"] == "➖"  # no platform_ref at all -> not applicable


def test_last_status_public_report_has_no_failure_reasons_for_non_admin_chat(tmp_path):
    products = [
        Product(
            internal_id="p1",
            name="Failed Widget",
            reference_price=1_000_000,
            target_price=1_100_000,
            platform_refs={"digikala": "123"}, display_name="Failed Widget", green_platforms=frozenset({"digikala"}),
        )
    ]
    states = [
        ProductStateRow(
            product_id="p1",
            platform="digikala",
            product_name="Failed Widget",
            reference_price=1_000_000,
            target_price=1_100_000,
            applied_price=None,
            is_capped=False,
            variant_id=None,
            status=OutcomeStatus.FAILED_API.value,
            retry_count=1,
            last_error="server exploded",
            updated_at="2026-09-20T10:00:00+00:00",
        ),
    ]
    service = FakeService(
        product_source=FakeProductSource(products),
        state_repository=FakeStateRepository(product_states=states),
    )
    # No ADMIN_CHAT_ID configured, or the requester isn't it -> public version.
    settings = _settings(admin_chat_id=999, data_dir=tmp_path)
    context = _context(settings, service)

    _run(handlers.menu_message_handler(_update(BTN_LAST_STATUS, chat_id=100), context))

    workbook = load_workbook(_sent_document_path(context))
    matrix_sheet = workbook["وضعیت قیمت‌ها"]
    failure_cell = matrix_sheet.cell(row=2, column=3)  # first platform column
    assert failure_cell.comment is None

    failures_sheet = workbook["جزئیات کالاهای ناموفق"]
    headers = [c.value for c in failures_sheet[1]]
    assert headers == ["ردیف", "نام کالا", "پلتفرم", "تاریخ آخرین تلاش"]
    assert "server exploded" not in [c.value for row in failures_sheet.iter_rows() for c in row]


def test_last_status_admin_report_includes_failure_reasons_for_admin_chat(tmp_path):
    products = [
        Product(
            internal_id="p1",
            name="Failed Widget",
            reference_price=1_000_000,
            target_price=1_100_000,
            platform_refs={"digikala": "123"}, display_name="Failed Widget", green_platforms=frozenset({"digikala"}),
        )
    ]
    states = [
        ProductStateRow(
            product_id="p1",
            platform="digikala",
            product_name="Failed Widget",
            reference_price=1_000_000,
            target_price=1_100_000,
            applied_price=None,
            is_capped=False,
            variant_id=None,
            status=OutcomeStatus.FAILED_API.value,
            retry_count=1,
            last_error="server exploded",
            updated_at="2026-09-20T10:00:00+00:00",
        ),
    ]
    service = FakeService(
        product_source=FakeProductSource(products),
        state_repository=FakeStateRepository(product_states=states),
    )
    settings = _settings(admin_chat_id=999, data_dir=tmp_path)
    context = _context(settings, service)

    _run(handlers.menu_message_handler(_update(BTN_LAST_STATUS, chat_id=999), context))

    workbook = load_workbook(_sent_document_path(context))
    matrix_sheet = workbook["وضعیت قیمت‌ها"]
    failure_cell = matrix_sheet.cell(row=2, column=3)
    assert failure_cell.comment is not None
    assert "خطای API پلتفرم" in failure_cell.comment.text

    failures_sheet = workbook["جزئیات کالاهای ناموفق"]
    headers = [c.value for c in failures_sheet[1]]
    assert headers == [
        "ردیف",
        "نام کالا",
        "پلتفرم",
        "تاریخ آخرین تلاش",
        "وضعیت دقیق",
        "قیمت مرجع",
        "قیمت هدف",
        "پیام خطا",
    ]
    all_values = [c.value for row in failures_sheet.iter_rows() for c in row]
    assert "server exploded" in all_values


def test_last_status_summary_sheet_counts_products_per_platform(tmp_path):
    products = [
        Product(
            internal_id="p1",
            name="Widget A",
            reference_price=1_000_000,
            target_price=1_100_000,
            platform_refs={"digikala": "1"}, display_name="Widget A", green_platforms=frozenset({"digikala"}),
        ),
        Product(
            internal_id="p2",
            name="Widget B",
            reference_price=1_000_000,
            target_price=1_100_000,
            platform_refs={"digikala": "2"}, display_name="Widget B", green_platforms=frozenset({"digikala"}),
        ),
    ]
    states = [
        ProductStateRow(
            product_id="p1",
            platform="digikala",
            product_name="Widget A",
            reference_price=1_000_000,
            target_price=1_100_000,
            applied_price=1_100_000,
            is_capped=False,
            variant_id=None,
            status=OutcomeStatus.SUCCESS.value,
            retry_count=0,
            last_error=None,
            updated_at="2026-09-20T10:00:00+00:00",
        ),
        # p2/digikala has no product_state row at all -> "not run yet".
    ]
    service = FakeService(
        available_platforms=["digikala"],
        product_source=FakeProductSource(products),
        state_repository=FakeStateRepository(product_states=states),
    )
    settings = _settings(data_dir=tmp_path)
    context = _context(settings, service)

    _run(handlers.menu_message_handler(_update(BTN_LAST_STATUS), context))

    workbook = load_workbook(_sent_document_path(context))
    summary_sheet = workbook["خلاصه"]
    header = [c.value for c in summary_sheet[1]]
    digikala_row = dict(zip(header, [c.value for c in summary_sheet[2]]))
    total_row = dict(zip(header, [c.value for c in summary_sheet[3]]))

    assert digikala_row["پلتفرم"] == "🔴 Digikala"
    assert digikala_row["تعداد کل واجد شرایط"] == 2
    assert digikala_row["موفق ✅"] == 1
    assert digikala_row["هنوز اجرا نشده ⏳"] == 1
    assert total_row["پلتفرم"] == "جمع کل"
    assert total_row["تعداد کل واجد شرایط"] == 2


# -- Price check ("🔍 چک و بررسی قیمت‌ها") -------------------------------------------------


class FakeCheckService(FakeService):
    """FakeService plus the three methods the price-check handler calls on the
    real PriceUpdateService. ``plan_price_check`` reuses the real (pure)
    ``select_products_for_check``, so the handler sees the same selections it
    would in production; ``check_prices`` honours the same hook contract
    (``on_product_start(platform, product)``, ``on_entry(entry)``,
    ``should_cancel(platform)``) that test_update_service.py proves for the
    real one. ``check_impl`` replaces the default behaviour (every product
    matches) when a test needs a blocking, failing or stoppable check.
    """

    def __init__(self, *args: Any, check_impl=None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.check_calls: list[dict[str, CheckSelection]] = []
        self._check_impl = check_impl

    def plan_price_check(self, platform_names, products):
        selections: dict[str, CheckSelection] = {}
        for platform in platform_names:
            selection = select_products_for_check(products, platform)
            if selection.products:
                selections[platform] = selection
        return selections

    def estimate_price_check_seconds(self, selections) -> float:
        return 30.0

    def check_prices(self, selections, *, on_product_start=None, on_entry=None, should_cancel=None):
        self.check_calls.append(dict(selections))
        if self._check_impl is not None:
            return self._check_impl(
                selections,
                on_product_start=on_product_start,
                on_entry=on_entry,
                should_cancel=should_cancel,
            )
        results: dict[str, PlatformCheckResult] = {}
        for platform, selection in selections.items():
            result = PlatformCheckResult(platform=platform, planned=len(selection.products))
            for product in selection.products:
                if should_cancel is not None and should_cancel(platform):
                    result.cancelled = True
                    break
                if on_product_start is not None:
                    on_product_start(platform, product)
                entry = PriceCheckEntry(
                    product=product,
                    platform=platform,
                    status=CheckStatus.MATCH,
                    current_price=product.target_price,
                    expected_price=product.target_price,
                    target_price=product.target_price,
                )
                result.entries.append(entry)
                if on_entry is not None:
                    on_entry(entry)
            results[platform] = result
        return results


def _check_product(name: str, *platforms: str) -> Product:
    return Product(
        internal_id=name,
        name=name,
        reference_price=1_000_000,
        target_price=1_100_000,
        platform_refs={p: f"{p}-{name}" for p in platforms},
        # Only products with "اسم جدید" filled and a GREEN platform cell are
        # price-checked (Product.is_check_eligible).
        display_name=name,
        green_platforms=frozenset(platforms),
    )


def _check_context(
    *,
    platforms: tuple[str, ...] = ("digikala",),
    products: list[Product] | None = None,
    check_impl=None,
    rerun_specific_impl=None,
    job_manager: PlatformJobManager | None = None,
    **settings_overrides: Any,
):
    if products is None:
        products = [_check_product("Widget", *platforms)]
    service = FakeCheckService(
        available_platforms=platforms,
        product_source=FakeProductSource(products),
        check_impl=check_impl,
        rerun_specific_impl=rerun_specific_impl,
    )
    context = _context(_settings(**settings_overrides), service, job_manager)
    return context, service


def _sent_texts(context) -> list[str]:
    return [c.kwargs.get("text", "") for c in context.bot.send_message.await_args_list]


def _empty_results(selections) -> dict[str, PlatformCheckResult]:
    return {p: PlatformCheckResult(platform=p, planned=len(s.products)) for p, s in selections.items()}


# 17 -- the report goes to the requesting chat only


def test_check_button_sends_everything_only_to_the_requesting_chat():
    # Unlike an update run (broadcast to TELEGRAM_CHAT_IDS), a check is a
    # personal lookup: neither the start message nor the report is broadcast.
    context, service = _check_context(telegram_chat_ids="111,222")
    update = _update(BTN_CHECK_PRICES, chat_id=100)

    _run_menu(update, context)

    assert len(service.check_calls) == 1
    sent_chat_ids = {c.kwargs["chat_id"] for c in context.bot.send_message.await_args_list}
    assert sent_chat_ids == {100}


def test_check_report_uses_broadcast_long_message_with_only_the_requesting_chat(monkeypatch):
    long_mock = AsyncMock()
    monkeypatch.setattr(handlers, "broadcast_long_message_with_retry", long_mock)
    context, _ = _check_context(telegram_chat_ids="111,222")

    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    long_mock.assert_awaited_once()
    assert long_mock.await_args.kwargs["chat_ids"] == [100]
    assert "گزارش بررسی قیمت‌ها" in long_mock.await_args.kwargs["text"]


# 18 -- pressing the button again while a check runs


def test_pressing_check_while_a_check_is_registered_says_it_is_already_running():
    job_manager = PlatformJobManager()
    running = job_manager.try_start("digikala", triggered_by="telegram:1")
    running.kind = "check"
    context, service = _check_context(job_manager=job_manager)
    update = _update(BTN_CHECK_PRICES)

    _run_menu(update, context)

    args, _ = update.message.reply_text.call_args
    assert "از قبل در حال اجراست" in args[0]
    assert service.check_calls == []
    assert service.product_source.fetch_products_calls == 0  # rejected before touching the sheet
    assert job_manager.current("digikala") is running  # the running check's lock is untouched


def test_second_press_during_a_really_running_check_does_not_start_another():
    entered = threading.Event()
    release = threading.Event()

    def blocking_check(selections, **hooks):
        entered.set()
        release.wait(timeout=5)
        return _empty_results(selections)

    context, service = _check_context(check_impl=blocking_check)
    first = _update(BTN_CHECK_PRICES)
    second = _update(BTN_CHECK_PRICES)

    async def scenario() -> None:
        try:
            await handlers.menu_message_handler(first, context)
            for _ in range(500):  # wait (<= 5 s) until the worker thread is inside the check
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            await handlers.menu_message_handler(second, context)
        finally:
            release.set()
        await asyncio.gather(*list(context.application.bot_data["_background_tasks"]))

    _run(scenario())

    args, _ = second.message.reply_text.call_args
    assert "از قبل در حال اجراست" in args[0]
    assert len(service.check_calls) == 1  # only the first press started a check


# 19 -- platforms that are busy with an update run


def test_platform_with_a_running_update_is_skipped_and_named_in_the_start_message():
    job_manager = PlatformJobManager()
    update_job = job_manager.try_start("basalam", triggered_by="telegram:1")  # kind "update"
    context, service = _check_context(platforms=("digikala", "basalam"), job_manager=job_manager)

    _run_menu(_update(BTN_CHECK_PRICES), context)

    assert list(service.check_calls[0]) == ["digikala"]  # basalam was NOT checked
    started = _sent_texts(context)[0]
    assert "شروع بررسی قیمت‌ها" in started
    assert "اجرای دیگری در حال انجام دارند" in started
    assert "🔵 Basalam" in started
    # The update run on basalam is left exactly as it was.
    assert job_manager.current("basalam") is update_job
    assert update_job.kind == "update"


def test_when_every_platform_is_busy_the_all_busy_message_is_sent_and_nothing_runs():
    job_manager = PlatformJobManager()
    for platform in ("digikala", "basalam"):
        job_manager.try_start(platform, triggered_by="telegram:1")
    context, service = _check_context(platforms=("digikala", "basalam"), job_manager=job_manager)
    update = _update(BTN_CHECK_PRICES)

    _run_menu(update, context)

    args, _ = update.message.reply_text.call_args
    assert args[0] == format_price_check_all_busy_message()
    assert service.check_calls == []
    context.bot.send_message.assert_not_awaited()
    assert job_manager.is_active("digikala") and job_manager.is_active("basalam")


# 20 -- every platform's lock is released in `finally`


def test_check_releases_every_platform_lock_after_a_normal_finish():
    job_manager = PlatformJobManager()
    context, service = _check_context(platforms=("digikala", "basalam"), job_manager=job_manager)

    _run_menu(_update(BTN_CHECK_PRICES), context)

    assert sorted(service.check_calls[0]) == ["basalam", "digikala"]
    assert not job_manager.is_active("digikala")
    assert not job_manager.is_active("basalam")


def test_check_releases_every_platform_lock_after_an_unexpected_error():
    def exploding_check(selections, **hooks):
        raise RuntimeError("kaboom")

    job_manager = PlatformJobManager()
    context, _ = _check_context(
        platforms=("digikala", "basalam"), check_impl=exploding_check, job_manager=job_manager
    )

    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    assert not job_manager.is_active("digikala")
    assert not job_manager.is_active("basalam")
    assert any("خطای غیرمنتظره" in text for text in _sent_texts(context))


def test_check_releases_every_platform_lock_after_a_manual_stop():
    started = threading.Event()

    def stoppable_check(selections, *, on_product_start, on_entry, should_cancel):
        started.set()
        deadline = time.monotonic() + 5
        while not should_cancel("digikala") and time.monotonic() < deadline:
            time.sleep(0.01)
        results = _empty_results(selections)
        results["digikala"].cancelled = should_cancel("digikala")
        return results

    job_manager = PlatformJobManager()
    context, service = _check_context(
        platforms=("digikala", "basalam"), check_impl=stoppable_check, job_manager=job_manager
    )

    async def scenario() -> None:
        await handlers.menu_message_handler(_update(BTN_CHECK_PRICES), context)
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        assert job_manager.is_active("digikala") and job_manager.is_active("basalam")
        # The real Stop button: ask, then confirm.
        await handlers.menu_message_handler(_update(BTN_STOP), context)
        await handlers.menu_message_handler(_update(BTN_CONFIRM_YES), context)
        await asyncio.gather(*list(context.application.bot_data["_background_tasks"]))

    _run(scenario())

    assert not job_manager.is_active("digikala")
    assert not job_manager.is_active("basalam")
    assert any("بررسی توسط کاربر متوقف شد" in text for text in _sent_texts(context))


# 21 -- routing, and "nothing to check"


def test_check_button_is_routed_to_the_check_handler(monkeypatch):
    check_handler = AsyncMock()
    monkeypatch.setattr(handlers, "_handle_check_prices", check_handler)
    context, _ = _check_context()
    update = _update(BTN_CHECK_PRICES)

    _run(handlers.menu_message_handler(update, context))

    check_handler.assert_awaited_once_with(update, context)


def test_check_with_nothing_checkable_replies_that_there_is_nothing_to_check():
    products = [
        # has a code on the platform but no usable sheet price
        Product(
            internal_id="a",
            name="A",
            reference_price=1_000_000,
            target_price=None,
            platform_refs={"digikala": "digikala-a"}, display_name="A", green_platforms=frozenset({"digikala"}),
        ),
        # has a price but no code on the platform
        Product(
            internal_id="b", name="B", reference_price=1_000_000, target_price=1_100_000, platform_refs={}, display_name="B", green_platforms=frozenset({})
        ),
    ]
    job_manager = PlatformJobManager()
    context, service = _check_context(products=products, job_manager=job_manager)
    update = _update(BTN_CHECK_PRICES)

    _run_menu(update, context)

    args, _ = update.message.reply_text.call_args
    assert args[0] == format_price_check_nothing_to_check_message()
    assert "هیچ کالایی برای بررسی" in args[0]
    assert service.check_calls == []
    context.bot.send_message.assert_not_awaited()
    assert not job_manager.is_active("digikala")  # no lock was ever taken

# -- Applying the price change after a check ("تغییر نکرده" -> بله/خیر) ---------------


def _mismatch_check_impl(unchanged: dict[str, list[str]], *, errors: dict[str, list[str]] | None = None):
    """A check_impl where, per platform, the listed products came back
    MISMATCH, the ``errors`` ones could not be read, and the rest matched."""

    def impl(selections, *, on_product_start=None, on_entry=None, should_cancel=None):
        results: dict[str, PlatformCheckResult] = {}
        for platform, selection in selections.items():
            result = PlatformCheckResult(platform=platform, planned=len(selection.products))
            for product in selection.products:
                if product.internal_id in unchanged.get(platform, []):
                    status, current = CheckStatus.MISMATCH, 9_000_000
                elif product.internal_id in (errors or {}).get(platform, []):
                    status, current = CheckStatus.ERROR, None
                else:
                    status, current = CheckStatus.MATCH, product.target_price
                entry = PriceCheckEntry(
                    product=product,
                    platform=platform,
                    status=status,
                    current_price=current,
                    expected_price=product.target_price,
                    target_price=product.target_price,
                )
                result.entries.append(entry)
                if on_entry is not None:
                    on_entry(entry)
            results[platform] = result
        return results

    return impl


def _two_widgets(*platforms: str) -> list[Product]:
    return [_check_product("Widget A", *platforms), _check_product("Widget B", *platforms)]


def test_check_with_unchanged_products_asks_whether_to_apply_the_change_and_changes_nothing_yet():
    context, service = _check_context(
        products=_two_widgets("digikala"),
        check_impl=_mismatch_check_impl({"digikala": ["Widget A"]}),
    )

    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    question = _sent_texts(context)[-1]
    assert "آیا می‌خواهید تغییر قیمت را روی این کالاها اعمال کنم؟" in question
    assert "Digikala: 1 کالا" in question
    markup = context.bot.send_message.await_args_list[-1].kwargs["reply_markup"]
    assert [b.text for row in markup.keyboard for b in row] == [BTN_CONFIRM_YES, BTN_CONFIRM_NO]
    # Only the question is pending -- no price was touched.
    assert service.rerun_specific_calls == []
    assert context.user_data["pending_confirmation"]["product_ids"] == {"digikala": ["Widget A"]}


def test_answering_yes_applies_the_change_only_to_the_unchanged_products():
    hooks_seen: dict[str, Any] = {}

    def rerun(platform, product_ids, *, triggered_by, **hooks):
        hooks_seen.update(hooks)
        return RunSummary(platform=platform, triggered_by=triggered_by)

    context, service = _check_context(
        products=_two_widgets("digikala"),
        check_impl=_mismatch_check_impl({"digikala": ["Widget A"]}),
        rerun_specific_impl=rerun,
    )
    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    _run_menu(_update(BTN_CONFIRM_YES, chat_id=100), context)

    assert service.rerun_specific_calls == [("digikala", ["Widget A"])]
    # The service is told to re-validate each product against the CURRENT sheet.
    assert hooks_seen["require_check_eligible"] is True
    assert service.run_calls == []  # never a full run
    texts = _sent_texts(context)
    assert any("شروع تغییر قیمت 1 کالای تغییرنکرده" in t for t in texts)
    assert "pending_confirmation" not in context.user_data


def test_answering_no_changes_no_price():
    context, service = _check_context(
        products=_two_widgets("digikala"),
        check_impl=_mismatch_check_impl({"digikala": ["Widget A"]}),
    )
    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)
    no_update = _update(BTN_CONFIRM_NO, chat_id=100)

    _run_menu(no_update, context)

    assert service.rerun_specific_calls == []
    assert service.run_calls == []
    args, _ = no_update.message.reply_text.call_args
    assert "تغییر قیمتی انجام نشد" in args[0]
    assert "pending_confirmation" not in context.user_data


def test_no_question_when_every_checked_product_matches():
    context, service = _check_context(products=_two_widgets("digikala"))  # default: all MATCH

    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    assert not any("آیا می‌خواهید تغییر قیمت" in t for t in _sent_texts(context))
    assert "pending_confirmation" not in context.user_data


def test_products_whose_price_could_not_be_read_are_not_offered_for_a_change():
    context, _ = _check_context(
        products=_two_widgets("digikala"),
        check_impl=_mismatch_check_impl({}, errors={"digikala": ["Widget A", "Widget B"]}),
    )

    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    assert not any("آیا می‌خواهید تغییر قیمت" in t for t in _sent_texts(context))
    assert "pending_confirmation" not in context.user_data


def test_a_stopped_check_does_not_offer_a_price_change():
    base = _mismatch_check_impl({"digikala": ["Widget A"]})

    def cancelled_check(selections, **hooks):
        results = base(selections, **hooks)
        results["digikala"].cancelled = True
        return results

    context, _ = _check_context(products=_two_widgets("digikala"), check_impl=cancelled_check)

    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    assert not any("آیا می‌خواهید تغییر قیمت" in t for t in _sent_texts(context))
    assert "pending_confirmation" not in context.user_data


def test_apply_runs_each_platform_with_its_own_unchanged_products():
    context, service = _check_context(
        platforms=("digikala", "basalam"),
        products=_two_widgets("digikala", "basalam"),
        check_impl=_mismatch_check_impl({"digikala": ["Widget A"], "basalam": ["Widget B"]}),
    )
    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)

    _run_menu(_update(BTN_CONFIRM_YES, chat_id=100), context)

    assert sorted(service.rerun_specific_calls) == [
        ("basalam", ["Widget B"]),
        ("digikala", ["Widget A"]),
    ]


def test_apply_skips_a_platform_that_became_busy_and_says_so():
    job_manager = PlatformJobManager()
    context, service = _check_context(
        platforms=("digikala", "basalam"),
        products=_two_widgets("digikala", "basalam"),
        check_impl=_mismatch_check_impl({"digikala": ["Widget A"], "basalam": ["Widget B"]}),
        job_manager=job_manager,
    )
    _run_menu(_update(BTN_CHECK_PRICES, chat_id=100), context)
    busy_job = job_manager.try_start("basalam", triggered_by="telegram:2")  # another run starts meanwhile

    _run_menu(_update(BTN_CONFIRM_YES, chat_id=100), context)

    assert service.rerun_specific_calls == [("digikala", ["Widget A"])]
    assert any("تغییر قیمت روی آن‌ها شروع نشد" in t for t in _sent_texts(context))
    assert job_manager.current("basalam") is busy_job  # the other run is untouched
