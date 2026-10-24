"""Telegram command/message handlers — the persistent control-panel UI.

Handlers pull the PriceUpdateService, Settings, and PlatformJobManager out of
`context.application.bot_data` (set up in telegram_bot.bot.build_application)
rather than closing over globals, which keeps this module independently
testable and avoids any hidden state.

This module is UI plumbing only: it never computes a price, decides a retry,
or talks to DigiKala directly. All of that stays in update.service /
core.pricing / platforms.digikala — handlers only call PriceUpdateService.run()
(with observer hooks for live reporting) and format the results via
telegram_bot.reporting.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from faraz_honar.config import Settings
from faraz_honar.core.models import Product, RunSummary, UpdateResult
from faraz_honar.logger import clear_log_file
from faraz_honar.platform_names import PLATFORM_DISPLAY_NAMES
from faraz_honar.reporting.status_matrix import build_status_matrix
from faraz_honar.reporting.xlsx_export import export_status_matrix_xlsx
from faraz_honar.telegram_bot.auth import build_allowed_ids, is_allowed
from faraz_honar.telegram_bot.keyboards import (
    BTN_ALL_PLATFORMS,
    BTN_BACK,
    BTN_CHECK_PRICES,
    BTN_CLEAR_DATABASE,
    BTN_CONFIRM_NO,
    BTN_CONFIRM_YES,
    BTN_LAST_STATUS,
    BTN_SELECT_PLATFORM,
    BTN_STATUS,
    BTN_STOP,
    build_confirm_keyboard,
    build_main_menu_keyboard,
    build_platform_submenu_keyboard,
    platform_button_label,
    platform_display_name,
    platform_from_button_label,
)
from faraz_honar.telegram_bot.reporting import (
    format_all_platforms_already_running_message,
    format_apply_checked_busy_message,
    format_apply_checked_confirmation,
    format_apply_checked_declined_message,
    format_apply_checked_scope_note,
    format_all_platforms_busy_message,
    format_all_platforms_finished_message,
    format_all_platforms_started_message,
    format_all_platforms_stopped_message,
    format_change_notice,
    format_clear_database_blocked_message,
    format_clear_database_cancelled_message,
    format_clear_database_confirmation,
    format_clear_database_result_message,
    format_dry_run_banner,
    format_final_digikala_report,
    format_hourly_recheck_report,
    format_no_active_run_message,
    format_price_check_all_busy_message,
    format_price_check_already_running_message,
    format_price_check_nothing_to_check_message,
    format_price_check_report,
    format_price_check_started_message,
    format_parallel_run_confirmation,
    format_parallel_run_declined_message,
    format_retry_failed_confirmation,
    format_retry_failed_scope_note,
    format_retry_nothing_left_message,
    format_status_message,
    format_stop_cancelled_message,
    format_stop_confirmation,
    format_stop_requested_message,
    format_stopped_message,
    to_jalali_date,
)
from faraz_honar.telegram_bot.sending import (
    broadcast_long_message_with_retry,
    broadcast_message_with_retry,
)
from faraz_honar.telegram_bot.state import AllPlatformsBatch, JobProgress, PlatformJobManager
from faraz_honar.update.price_check import CheckSelection, CheckStatus, PlatformCheckResult, PriceCheckEntry
from faraz_honar.update.service import (
    PlatformBacklog,
    PriceUpdateService,
    compute_platform_backlog,
)

logger = logging.getLogger(__name__)

_UNAUTHORIZED_TEXT = "You are not authorized to use this bot."
_MAIN_MENU_TEXT = "🤖 Faraz Honar"

# "📦 انتخاب پلتفرم" (start a run) leads to a platform-name submenu, so we
# need to remember that a person is in the middle of picking a platform to
# run before they tap a platform name. Stored per-chat in context.user_data
# and popped as soon as the platform choice arrives. "📊 آخرین وضعیت" (show
# last status) no longer goes through this submenu at all -- the price-status
# Excel report always covers every platform at once, so there's nothing left
# to pick (see _handle_last_status).
_AWAITING_PLATFORM_FOR_KEY = "awaiting_platform_for"
_MODE_RUN = "run"

# A بله/خیر confirmation currently awaiting the person's reply, stored per-chat
# in context.user_data so the plain-text "✅ بله"/"❌ خیر" buttons (shared by
# both warnings below) know which question they're answering. Popped as soon
# as an answer (or anything else) arrives — see menu_message_handler.
_PENDING_CONFIRMATION_KEY = "pending_confirmation"
_CONFIRM_PARALLEL_RUN = "parallel_run"
_CONFIRM_STOP = "stop"
_CONFIRM_RETRY_FAILED = "retry_failed_only"
_CONFIRM_CLEAR_DATABASE = "clear_database"
# Asked right after a price-check report that lists unchanged products: "should
# I apply the price change on these?" The (platform -> product ids) the check
# found unchanged are kept in the pending dict itself -- unlike the retry
# question they cannot be recomputed later, they are exactly what THIS check saw.
_CONFIRM_APPLY_CHECKED = "apply_checked"

# The retry question covers two groups, computed from the CURRENT sheet plus
# the latest recorded state of every product (not from any single past run —
# a partial/retry run would otherwise hide everything it didn't touch):
#   - "ناموفق": latest outcome is any FAILED_* status or CAPPED_12PCT (a capped
#     product's applied price is still short of the target; the hourly recheck
#     also picks it up, but the operator can retry it immediately from here).
#   - "بررسی‌نشده": eligible on this platform but has no recorded outcome yet.
# See update.service.compute_platform_backlog.


def _load_platform_backlog(service: PriceUpdateService, platform: str) -> PlatformBacklog:
    """Blocking (may hit the Google Sheets API) — call via asyncio.to_thread."""
    products = service.product_source.fetch_products()
    states = service.state_repository.list_all_product_states()
    return compute_platform_backlog(products, states, platform)


def _service(context: ContextTypes.DEFAULT_TYPE) -> PriceUpdateService:
    return context.application.bot_data["service"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _job_manager(context: ContextTypes.DEFAULT_TYPE) -> PlatformJobManager:
    return context.application.bot_data["job_manager"]


def _report_recipients(settings: Settings, *, fallback_chat_id: int) -> list[int]:
    """Who a report-type message (start banner, DRY_RUN banner, final
    report, stopped-by-user message) goes to — regardless of which team
    member's chat triggered the run. Precedence: TELEGRAM_CHAT_IDS, else the
    numbered TELEGRAM_CHAT_ID_N slots, else `fallback_chat_id` (the
    triggering chat) — TELEGRAM_ALLOWED_USER_IDS is an authorization list,
    not a report-recipient list, so it plays no part here; with neither of
    the first two configured, a manual run reports back only to whoever
    triggered it, exactly as before either setting existed.

    The unexpected-error notice is the one exception: it goes to
    ADMIN_CHAT_ID alone when one is configured (see _execute_run), and only
    falls back to this list when ADMIN_CHAT_ID is blank.
    """
    configured = settings.telegram_chat_ids_set or settings.numbered_chat_ids_set
    return list(configured) if configured else [fallback_chat_id]


def _change_notice_recipients(settings: Settings, *, chat_id: int, user_id: int | None) -> list[int]:
    """Who gets the "who started what" heads-up: the usual report recipients
    plus every numbered chat (TELEGRAM_CHAT_ID_1..10), minus the requester
    themself. Empty when no numbered chats are configured, so with none set
    no extra message is ever sent (behavior identical to before).
    """
    numbered = settings.numbered_chat_ids_set
    if not numbered:
        return []
    recipients = set(_report_recipients(settings, fallback_chat_id=chat_id)) | numbered
    recipients.discard(chat_id)
    if user_id is not None:
        recipients.discard(user_id)
    return sorted(recipients)


def _is_authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    user_id = user.id if user else None
    allowed = build_allowed_ids(
        settings.telegram_allowed_user_ids_set, settings.numbered_chat_ids_set
    )
    return is_allowed(user_id, allowed)


# -- Entry points --------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    settings = _settings(context)
    if not _is_authorized(update, settings):
        await message.reply_text(_UNAUTHORIZED_TEXT)
        logger.warning("Unauthorized /start attempt from user_id=%s", update.effective_user)
        return

    await message.reply_text(_MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard())


async def menu_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Routes every plain-text message (the persistent reply-keyboard button
    presses) — this is what lets the bot work as a standing control panel
    instead of a one-shot /start command (spec section 1 and 11).
    """
    message = update.message
    if message is None or message.text is None:
        return

    settings = _settings(context)
    if not _is_authorized(update, settings):
        await message.reply_text(_UNAUTHORIZED_TEXT)
        logger.warning("Unauthorized message from user_id=%s", update.effective_user)
        return

    text = message.text.strip()
    service = _service(context)

    if _PENDING_CONFIRMATION_KEY in context.user_data:
        if text == BTN_CONFIRM_YES:
            await _resolve_pending_confirmation(update, context, confirmed=True)
            return
        if text == BTN_CONFIRM_NO:
            await _resolve_pending_confirmation(update, context, confirmed=False)
            return
        # Any other message abandons the pending confirmation instead of
        # leaving the person stuck unable to use the rest of the menu.
        context.user_data.pop(_PENDING_CONFIRMATION_KEY, None)

    if text == BTN_SELECT_PLATFORM:
        context.user_data[_AWAITING_PLATFORM_FOR_KEY] = _MODE_RUN
        await message.reply_text(
            "یک پلتفرم را انتخاب کنید:",
            reply_markup=build_platform_submenu_keyboard(service.available_platforms),
        )
        return

    if text == BTN_LAST_STATUS:
        await _handle_last_status(update, context)
        return

    if text == BTN_CHECK_PRICES:
        await _handle_check_prices(update, context)
        return

    if text == BTN_BACK:
        context.user_data.pop(_AWAITING_PLATFORM_FOR_KEY, None)
        await message.reply_text(_MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard())
        return

    if text == BTN_STATUS:
        await _handle_status(update, context)
        return

    if text == BTN_STOP:
        await _handle_stop(update, context)
        return

    if text == BTN_CLEAR_DATABASE:
        await _handle_clear_database(update, context)
        return

    if text == BTN_ALL_PLATFORMS:
        context.user_data.pop(_AWAITING_PLATFORM_FOR_KEY, None)
        user = update.effective_user
        await _start_all_platforms_run(update, context, user_id=user.id if user else None)
        return

    platform = platform_from_button_label(text)
    if platform is not None:
        context.user_data.pop(_AWAITING_PLATFORM_FOR_KEY, None)
        user = update.effective_user
        await _start_platform_run(update, context, platform, user_id=user.id if user else None)
        return

    # Unrecognized text: never dead-end the user — just show the persistent
    # menu again instead of requiring /start.
    await message.reply_text(_MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard())


async def hourly_recheck_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Spec section 8: re-attempt 12%-capped products every hour and report
    via Telegram again if anything changed. Unchanged report format/content
    from before this control-panel feature — only addition is per-platform
    locking (via PlatformJobManager) so this can never run at the same time
    as a manual Telegram-triggered run of the same platform.
    """
    service: PriceUpdateService = context.application.bot_data["service"]
    settings: Settings = context.application.bot_data["settings"]
    job_manager: PlatformJobManager = context.application.bot_data["job_manager"]

    summaries: dict[str, RunSummary] = {}
    for platform_name in service.available_platforms:
        progress = job_manager.try_start(platform_name, triggered_by="scheduler:hourly")
        if progress is None:
            logger.info(
                "Skipping hourly recheck for '%s': a run is already in progress.", platform_name
            )
            continue
        try:
            platform_summaries = await asyncio.to_thread(
                service.recheck_capped_products, [platform_name], triggered_by="scheduler:hourly"
            )
            summaries.update(platform_summaries)
        finally:
            job_manager.finish(platform_name, progress)

    report = format_hourly_recheck_report(summaries)
    if report is None:
        return  # nothing was capped; no need to notify anyone

    # TELEGRAM_CHAT_IDS, when configured, is the broadcast list — every
    # chat id listed there receives the report. Left unconfigured, behavior
    # is unchanged from before this setting existed: TELEGRAM_ALLOWED_
    # USER_IDS is used instead.
    recipients = settings.telegram_chat_ids_set or settings.telegram_allowed_user_ids_set
    await broadcast_long_message_with_retry(context.bot, chat_ids=recipients, text=report)


# -- Menu actions ----------------------------------------------------------------


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    service = _service(context)
    job_manager = _job_manager(context)

    # Only runs a person started are shown here. The scheduled hourly recheck
    # (triggered_by "scheduler:...") never feeds progress counters, so listing
    # it would show a phantom "running, 0 / 0" entry for that platform. When
    # a parallel run was confirmed, every concurrent job for a platform is
    # listed separately (current_all), not just one of them.
    active = [
        format_status_message(job, platform_label=platform_button_label(p))
        for p in service.available_platforms
        for job in job_manager.current_all(p)
        if not job.triggered_by.startswith("scheduler:")
    ]
    text = "\n\n".join(active) if active else format_no_active_run_message()
    await message.reply_text(text, reply_markup=build_main_menu_keyboard())


async def _handle_last_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """"📊 آخرین وضعیت": sends the product×platform price-status Excel report
    (reporting/status_matrix.py + reporting/xlsx_export.py) built from
    whatever is currently in `product_state` — not from a fresh run — so it
    always reflects the latest attempt on every platform for every product,
    in one file, with no platform to pick first.

    Sent only to the requester's own chat (not broadcast to every configured
    recipient like the end-of-run report) since this is a personal lookup,
    not a "here's what just happened" team notification. Same public/admin
    split as the end-of-run report: the requester gets the sheet without
    failure-reason comments/columns unless their own chat is ADMIN_CHAT_ID,
    in which case `include_reasons=True` adds them.
    """
    message = update.message
    if message is None:
        return
    service = _service(context)
    settings = _settings(context)

    products = service.product_source.fetch_products()
    states = service.state_repository.list_all_product_states()
    matrix = build_status_matrix(products, states, platforms=service.available_platforms)

    is_admin = settings.admin_chat_id is not None and message.chat.id == settings.admin_chat_id
    output_path = export_status_matrix_xlsx(
        matrix,
        _exports_dir(settings) / f"price_status_{_jalali_date_now()}_{_hhmm_now()}.xlsx",
        include_reasons=is_admin,
    )
    with output_path.open("rb") as report_file:
        await context.bot.send_document(
            chat_id=message.chat.id,
            document=report_file,
            caption="📊 آخرین وضعیت قیمت‌ها",
            reply_markup=build_main_menu_keyboard(),
        )


def _exports_dir(settings: Settings) -> Path:
    """Where price-status Excel reports (📊 آخرین وضعیت) are written, inside
    the same data directory everything else in the app already uses
    (Settings.data_dir, default ./data) rather than a hardcoded path.
    """
    path = settings.data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _jalali_date_now() -> str:
    """Today's date (Tehran-local, per to_jalali_date's own UTC->Tehran
    conversion) for the exported report's filename.

    to_jalali_date() returns a slash-separated string (e.g. "1405/07/05"),
    which is fine for display but not safe to embed in a filename: on
    every OS "/" is a path separator, so "price_status_1405/07/05_HHMM.xlsx"
    silently turns into nested directories exports/1405/07/05_HHMM.xlsx
    instead of a flat file. Replace the slashes with "-" here, at the
    filename call site, rather than in to_jalali_date() itself, since other
    callers (e.g. the retry-date message) rely on the slash format for
    display.
    """
    return to_jalali_date(datetime.now(UTC).isoformat()).replace("/", "-")


def _hhmm_now() -> str:
    """Current UTC time as HHMM for the exported report's filename — just a
    tie-breaker between reports generated on the same Jalali day, so exact
    timezone doesn't matter here the way it does for the date itself.
    """
    return datetime.now(UTC).strftime("%H%M")


# -- Price check ("🔍 چک و بررسی قیمت‌ها") --------------------------------------------
#
# Read-only: reads every platform's live price (all platforms at the same
# time, see PriceUpdateService.check_prices) and lists the products whose price
# is not the sheet's price. It registers a JobProgress(kind="check") per
# platform, so the per-platform lock (no overlap with an update run or the
# hourly recheck), the "📊 وضعیت اجرا" view and the Stop button all work for it
# without any extra wiring. Nothing is written to the platforms or the state DB.


def _active_price_check(job_manager: PlatformJobManager, platforms: list[str]) -> JobProgress | None:
    for p in platforms:
        for job in job_manager.current_all(p):
            if job.kind == "check":
                return job
    return None


async def _handle_check_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    service = _service(context)
    job_manager = _job_manager(context)
    settings = _settings(context)
    platforms = _ordered_platforms(service.available_platforms)

    if _active_price_check(job_manager, platforms) is not None:
        await message.reply_text(
            format_price_check_already_running_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    try:
        # Blocking (Google Sheets API) — keep it off the event loop.
        products = await asyncio.to_thread(service.product_source.fetch_products)
    except Exception:  # noqa: BLE001 - reported to the person, details in the log
        logger.exception("Price check: loading the product sheet failed")
        await message.reply_text(
            "❌ خواندن شیت با خطا مواجه شد؛ لطفاً کمی بعد دوباره تلاش کنید.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    selections = service.plan_price_check(platforms, products)
    if not selections:
        await message.reply_text(
            format_price_check_nothing_to_check_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    user = update.effective_user
    triggered_by = f"telegram:{user.id}" if user else "telegram:unknown"

    # Lock every platform that is free; a platform another run is working on is
    # skipped (and said so) rather than checked in parallel with that run. No
    # await between here and create_task, so nothing can slip in between.
    acquired: dict[str, JobProgress] = {}
    busy: list[str] = []
    for platform in selections:
        progress = job_manager.try_start(platform, triggered_by=triggered_by)
        if progress is None:
            busy.append(platform)
            continue
        progress.kind = "check"
        progress.total = len(selections[platform].products)
        acquired[platform] = progress
    if not acquired:
        await message.reply_text(
            format_price_check_all_busy_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    run_selections = {p: selections[p] for p in acquired}
    is_admin = settings.admin_chat_id is not None and message.chat.id == settings.admin_chat_id
    task = asyncio.create_task(
        _execute_price_check(
            context,
            message.chat.id,
            acquired,
            run_selections,
            busy,
            estimate_seconds=service.estimate_price_check_seconds(run_selections),
            is_admin=is_admin,
            user_id=user.id if user else None,
        )
    )
    background_tasks: set = context.application.bot_data.setdefault("_background_tasks", set())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _execute_price_check(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    acquired: dict[str, JobProgress],
    selections: dict[str, CheckSelection],
    busy: list[str],
    *,
    estimate_seconds: float,
    is_admin: bool,
    user_id: int | None = None,
) -> None:
    """Runs the check in a worker thread (so the bot stays responsive), then
    sends the report to the requester's own chat only — like "📊 گزارش", this
    is a personal lookup, not a team notification.

    When the check found unchanged products ("تغییر نکرده") and ran to the end,
    the requester is then asked whether to apply the price change on them
    (see _offer_apply_checked / _resolve_apply_checked_confirmation)."""
    job_manager = _job_manager(context)
    settings = _settings(context)
    service = _service(context)

    def on_product_start(platform: str, product: Product) -> None:
        acquired[platform].current_product_name = product.name

    def on_entry(entry: PriceCheckEntry) -> None:
        progress = acquired[entry.platform]
        progress.processed += 1
        if entry.status is CheckStatus.MATCH:
            progress.success += 1
        elif entry.status is CheckStatus.MISMATCH:
            progress.mismatched += 1
        else:
            progress.failed += 1

    def should_cancel(platform: str) -> bool:
        return acquired[platform].cancel_requested

    results: dict[str, PlatformCheckResult] = {}
    try:
        await broadcast_message_with_retry(
            context.bot,
            chat_ids=[chat_id],
            text=format_price_check_started_message(
                platform_counts=[(p, len(sel.products)) for p, sel in selections.items()],
                estimate_seconds=estimate_seconds,
                busy_platforms=busy,
            ),
            reply_markup=build_main_menu_keyboard(),
        )
        results = await asyncio.to_thread(
            service.check_prices,
            selections,
            on_product_start=on_product_start,
            on_entry=on_entry,
            should_cancel=should_cancel,
        )
    except Exception:  # noqa: BLE001 - one product's/platform's error boundary is inside check_platform
        logger.exception("Price check failed unexpectedly")
        error_recipients = [settings.admin_chat_id] if settings.admin_chat_id is not None else [chat_id]
        await broadcast_message_with_retry(
            context.bot,
            chat_ids=error_recipients,
            text="❌ بررسی قیمت‌ها با خطای غیرمنتظره متوقف شد.",
            reply_markup=build_main_menu_keyboard(),
        )
        return
    finally:
        # Always release every platform's lock, even on crash/cancel.
        for platform, progress in acquired.items():
            job_manager.finish(platform, progress)

    interrupted_note = (
        "⏹ بررسی توسط کاربر متوقف شد؛ این گزارش فقط شامل کالاهای بررسی‌شده تا آن لحظه است."
        if any(r.cancelled for r in results.values())
        else None
    )
    try:
        report_text = format_price_check_report(
            results,
            busy_platforms=busy,
            dry_run=settings.dry_run,
            include_reasons=is_admin,
            interrupted_note=interrupted_note,
        )
    except Exception:  # noqa: BLE001 - the report itself must still go out
        logger.exception("Building the price-check report failed")
        report_text = (
            "🔍 گزارش بررسی قیمت‌ها\n\n"
            "⚠️ ساخت جزئیات گزارش با خطا مواجه شد؛ لطفاً لاگ برنامه را بررسی کنید."
        )
    await broadcast_long_message_with_retry(
        context.bot,
        chat_ids=[chat_id],
        text=report_text,
        reply_markup=build_main_menu_keyboard(),
    )

    # A check the person stopped only covers part of the products, so no price
    # change is offered after it -- they can simply run the check again.
    if interrupted_note is None:
        await _offer_apply_checked(context, chat_id, results, user_id=user_id)


async def _offer_apply_checked(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    results: dict[str, PlatformCheckResult],
    *,
    user_id: int | None,
) -> None:
    """Asks the requester (بله/خیر) whether to apply the sheet price to the
    products the check found unchanged. Only MISMATCH entries count -- a product
    whose price could not be read (ERROR) is not known to be unchanged. Nothing
    is asked when every product matched."""
    product_ids: dict[str, list[str]] = {}
    for platform, result in results.items():
        ids = list(dict.fromkeys(entry.product.internal_id for entry in result.mismatched))
        if ids:
            product_ids[platform] = ids
    if not product_ids:
        return

    settings = _settings(context)
    # Per platform: everything the check reported as unsuccessful (unchanged +
    # unreadable), so the final report of the apply run can state the total.
    check_totals = {
        platform: {
            "failed": len(results[platform].mismatched) + len(results[platform].errors),
            "unread": len(results[platform].errors),
        }
        for platform in product_ids
    }
    context.user_data[_PENDING_CONFIRMATION_KEY] = {
        "type": _CONFIRM_APPLY_CHECKED,
        "user_id": user_id,
        "product_ids": product_ids,
        "check_totals": check_totals,
    }
    display_name = settings.display_name(user_id) if user_id is not None else "کاربر"
    await broadcast_message_with_retry(
        context.bot,
        chat_ids=[chat_id],
        text=format_apply_checked_confirmation(
            display_name=display_name,
            platform_counts=[(p, len(ids)) for p, ids in product_ids.items()],
        ),
        reply_markup=build_confirm_keyboard(),
    )


async def _handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """"⏹ توقف اجرا": asks for confirmation first, since stopping discards
    the in-progress product history collected so far for whatever run(s)
    are active (format_stop_confirmation). The actual cancellation only
    happens in _resolve_pending_confirmation once the person answers "بله".
    Skips the confirmation entirely when nothing is running, same as
    before this warning existed.
    """
    message = update.message
    if message is None:
        return
    service = _service(context)
    job_manager = _job_manager(context)

    if (
        not any(job_manager.is_active(p) for p in service.available_platforms)
        and _active_batch(context) is None
    ):
        await message.reply_text(format_no_active_run_message(), reply_markup=build_main_menu_keyboard())
        return

    context.user_data[_PENDING_CONFIRMATION_KEY] = {"type": _CONFIRM_STOP}
    await message.reply_text(format_stop_confirmation(), reply_markup=build_confirm_keyboard())


async def _resolve_pending_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, confirmed: bool
) -> None:
    """Dispatches a بله/خیر reply to whichever warning is currently pending
    (parallel-run start, or stop) and clears it either way."""
    message = update.message
    if message is None:
        return
    pending = context.user_data.pop(_PENDING_CONFIRMATION_KEY, None)
    if pending is None:
        await message.reply_text(_MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard())
        return

    kind = pending.get("type")
    if kind == _CONFIRM_PARALLEL_RUN:
        await _resolve_parallel_run_confirmation(update, context, pending, confirmed=confirmed)
    elif kind == _CONFIRM_STOP:
        await _resolve_stop_confirmation(update, context, confirmed=confirmed)
    elif kind == _CONFIRM_RETRY_FAILED:
        await _resolve_retry_failed_confirmation(update, context, pending, confirmed=confirmed)
    elif kind == _CONFIRM_CLEAR_DATABASE:
        await _resolve_clear_database_confirmation(update, context, confirmed=confirmed)
    elif kind == _CONFIRM_APPLY_CHECKED:
        await _resolve_apply_checked_confirmation(update, context, pending, confirmed=confirmed)
    else:
        await message.reply_text(_MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard())


async def _resolve_parallel_run_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, *, confirmed: bool
) -> None:
    """"بله" starts the requested platform's run anyway, alongside whatever
    else is currently running (job_manager.start_parallel() if it turns out
    to collide with an active job on this exact platform, otherwise a
    normal try_start() since a different platform being busy never blocks
    this one); "خیر" leaves every existing run untouched and starts
    nothing.
    """
    message = update.message
    if message is None:
        return
    platform = pending.get("platform")

    if not confirmed:
        await message.reply_text(
            format_parallel_run_declined_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    service = _service(context)
    if platform not in service.available_platforms:
        await message.reply_text(
            "این پلتفرم در دسترس نیست.", reply_markup=build_main_menu_keyboard()
        )
        return

    user_id = pending.get("user_id")
    job_manager = _job_manager(context)
    triggered_by = f"telegram:{user_id}"
    # Most of the time the OTHER running job is on a different, still-free
    # platform, so a plain try_start() for THIS platform succeeds normally.
    # Only fall back to start_parallel() when this exact platform is the one
    # that's actually busy (the classic same-platform case).
    progress = job_manager.try_start(platform, triggered_by=triggered_by)
    if progress is None:
        progress = job_manager.start_parallel(platform, triggered_by=triggered_by)
    await _launch_run(update, context, platform, progress, user_id=user_id)


async def _resolve_stop_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, confirmed: bool
) -> None:
    message = update.message
    if message is None:
        return
    if not confirmed:
        await message.reply_text(
            format_stop_cancelled_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    service = _service(context)
    job_manager = _job_manager(context)
    stopped_any = any(job_manager.request_cancel(p) for p in service.available_platforms)
    # Also stop an "all platforms" sequence, so it doesn't just move on to the
    # next platform once the current one is cancelled.
    batch = _active_batch(context)
    if batch is not None:
        batch.cancel_requested = True
        stopped_any = True
    text = format_stop_requested_message() if stopped_any else format_no_active_run_message()
    await message.reply_text(text, reply_markup=build_main_menu_keyboard())


async def _handle_clear_database(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """"🧹 پاکسازی دیتابیس": permanently wipes product_state, update_runs,
    run_product_results, and the app's rotating log file.

    Blocked outright (no confirmation offered) while ANY platform has an
    active run — the same run is writing to product_state and the log file
    at that exact moment, so clearing mid-run could produce a crash or
    half-written data. Otherwise asks for بله/خیر first, since this is
    irreversible (unlike Stop, which only discards in-progress history for
    the current run).
    """
    message = update.message
    if message is None:
        return
    service = _service(context)
    job_manager = _job_manager(context)

    if (
        any(job_manager.is_active(p) for p in service.available_platforms)
        or _active_batch(context) is not None
    ):
        await message.reply_text(
            format_clear_database_blocked_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    context.user_data[_PENDING_CONFIRMATION_KEY] = {"type": _CONFIRM_CLEAR_DATABASE}
    await message.reply_text(format_clear_database_confirmation(), reply_markup=build_confirm_keyboard())


async def _resolve_clear_database_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, confirmed: bool
) -> None:
    message = update.message
    if message is None:
        return
    if not confirmed:
        await message.reply_text(
            format_clear_database_cancelled_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    service = _service(context)
    job_manager = _job_manager(context)

    # Fail-safe re-check: a run may have started between the confirmation
    # prompt and this reply arriving. If so, abort exactly like the initial
    # guard in _handle_clear_database rather than clearing anything.
    if (
        any(job_manager.is_active(p) for p in service.available_platforms)
        or _active_batch(context) is not None
    ):
        await message.reply_text(
            format_clear_database_blocked_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    db_cleared = False
    try:
        service.state_repository.clear_all()
        db_cleared = True
    except Exception:  # noqa: BLE001 - a failed clear must still be reported, not swallowed
        logger.exception("Clearing the database failed")

    log_cleared = False
    try:
        log_cleared = clear_log_file()
    except Exception:  # noqa: BLE001 - same as above, for the log-clearing step
        logger.exception("Clearing the log file failed")

    await message.reply_text(
        format_clear_database_result_message(db_cleared=db_cleared, log_cleared=log_cleared),
        reply_markup=build_main_menu_keyboard(),
    )


async def _resolve_retry_failed_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, *, confirmed: bool
) -> None:
    """"بله" processes only the products that are still outstanding on this
    platform — failed (FAILED_* / CAPPED_12PCT) plus never-checked ones;
    "خیر" starts a new full run over every eligible product, exactly as if
    this question had never been asked.

    The product ids are deliberately never stored in `pending` — the backlog
    is recomputed here, once the person actually answers, so the retry always
    targets the freshest state rather than a snapshot taken when the question
    was first shown.
    """
    message = update.message
    if message is None:
        return
    platform = pending.get("platform")
    user_id = pending.get("user_id")
    service = _service(context)
    job_manager = _job_manager(context)

    if platform not in service.available_platforms:
        await message.reply_text(
            "این پلتفرم در دسترس نیست.", reply_markup=build_main_menu_keyboard()
        )
        return

    triggered_by = f"telegram:{user_id}"
    retry_product_ids: list[str] | None = None
    retry_failed_count: int | None = None
    retry_unchecked_count: int | None = None

    if confirmed:
        backlog = await asyncio.to_thread(_load_platform_backlog, service, platform)
        if backlog.is_empty:
            await message.reply_text(
                format_retry_nothing_left_message(platform_button_label(platform)),
                reply_markup=build_main_menu_keyboard(),
            )
            return
        retry_product_ids = list(backlog.retry_ids)
        retry_failed_count = len(backlog.failed_ids)
        retry_unchecked_count = len(backlog.unchecked_ids)

    progress = job_manager.try_start(platform, triggered_by=triggered_by)
    if progress is None:
        progress = job_manager.start_parallel(platform, triggered_by=triggered_by)
    await _launch_run(
        update,
        context,
        platform,
        progress,
        user_id=user_id,
        retry_product_ids=retry_product_ids,
        retry_failed_count=retry_failed_count,
        retry_unchecked_count=retry_unchecked_count,
    )


async def _resolve_apply_checked_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, *, confirmed: bool
) -> None:
    """"بله" applies the sheet price to the products the price check found
    unchanged; "خیر" changes nothing. The update itself is a normal price
    update restricted to exactly those products, platform after platform."""
    message = update.message
    if message is None:
        return
    if not confirmed:
        await message.reply_text(
            format_apply_checked_declined_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    service = _service(context)
    product_ids: dict[str, list[str]] = {
        platform: list(ids)
        for platform, ids in (pending.get("product_ids") or {}).items()
        if platform in service.available_platforms and ids
    }
    if not product_ids:
        await message.reply_text(_MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard())
        return

    user_id = pending.get("user_id")
    triggered_by = f"telegram:{user_id}"
    settings = _settings(context)
    chat_id = message.chat.id
    task = asyncio.create_task(_execute_apply_checked(
            context,
            chat_id,
            product_ids,
            triggered_by,
            check_totals=dict(pending.get("check_totals") or {}),
        )
    )
    background_tasks: set = context.application.bot_data.setdefault("_background_tasks", set())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

    notice_recipients = _change_notice_recipients(settings, chat_id=chat_id, user_id=user_id)
    if notice_recipients:
        display_name = settings.display_name(user_id) if user_id is not None else "یک کاربر"
        labels = "، ".join(platform_display_name(p) for p in product_ids)
        notice_task = asyncio.create_task(
            broadcast_message_with_retry(
                context.bot,
                chat_ids=notice_recipients,
                text=format_change_notice(display_name, labels),
            )
        )
        background_tasks.add(notice_task)
        notice_task.add_done_callback(background_tasks.discard)


async def _execute_apply_checked(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    product_ids: dict[str, list[str]],
    triggered_by: str,
    *,
    check_totals: dict[str, dict[str, int]] | None = None,
) -> None:
    """Applies the sheet price to the unchanged products, one platform after
    another (each with its own start banner and final report, exactly like a
    normal platform run). A platform that another run is working on right now
    is skipped and reported, never run in parallel."""
    job_manager = _job_manager(context)
    busy: list[str] = []
    for platform in _ordered_platforms(list(product_ids)):
        progress = job_manager.try_start(platform, triggered_by=triggered_by)
        if progress is None:
            busy.append(platform)
            continue
        context.application.bot_data["last_platform"] = platform
        await _execute_run(
            context,
            chat_id,
            platform,
            progress,
            triggered_by,
            retry_product_ids=product_ids[platform],
            apply_checked=True,
            check_totals=(check_totals or {}).get(platform),
        )
        if progress.cancel_requested:
            break
    if busy:
        await broadcast_message_with_retry(
            context.bot,
            chat_ids=[chat_id],
            text=format_apply_checked_busy_message([platform_button_label(p) for p in busy]),
            reply_markup=build_main_menu_keyboard(),
        )


# -- Starting and running a platform update --------------------------------------


async def _start_platform_run(
    update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str, *, user_id: int | None
) -> None:
    message = update.message
    if message is None:
        return
    service = _service(context)
    job_manager = _job_manager(context)

    if platform not in service.available_platforms:
        await message.reply_text(
            "این پلتفرم در دسترس نیست.", reply_markup=build_main_menu_keyboard()
        )
        return

    # The warning is about ANY other process currently running — not only a
    # conflict on this exact platform. Starting "digikala2" while "digikala"
    # is running must still warn, naming whichever platform that other job
    # belongs to (spec: "پردازش دیگری ... برای پلتفرم X در حال انجام است").
    other_job = _find_any_active_job(job_manager, service.available_platforms)
    if other_job is not None:
        # "بله" starts this run anyway — job_manager.start_parallel() if the
        # OTHER job happens to be on this same platform, or a plain
        # try_start() if it's a different (still-free) platform — see
        # _resolve_parallel_run_confirmation. "خیر" cancels this new
        # attempt and leaves every existing run untouched.
        settings = _settings(context)
        other_display_name = _display_name_for_triggered_by(settings, other_job.triggered_by)
        requester_display_name = (
            settings.display_name(user_id) if user_id is not None else "کاربر"
        )
        context.user_data[_PENDING_CONFIRMATION_KEY] = {
            "type": _CONFIRM_PARALLEL_RUN,
            "platform": platform,
            "user_id": user_id,
        }
        await message.reply_text(
            format_parallel_run_confirmation(
                requester_display_name=requester_display_name,
                other_display_name=other_display_name,
                platform_label=platform_button_label(other_job.platform),
            ),
            reply_markup=build_confirm_keyboard(),
        )
        return

    # Spec section 3, acceptance criteria 1-2: only reached when no parallel-
    # run warning fired above — the two confirmations are never chained in
    # the same tap (design assumption in the spec), so a person only ever
    # answers one بله/خیر question before a run actually starts. The product
    # ids themselves are NOT stored in user_data here; see
    # _resolve_retry_failed_confirmation, which recomputes the backlog fresh
    # once the person answers.
    #
    # The question is only asked when something is outstanding AND the
    # platform has been processed at least once: on a platform that has never
    # been run (or whose history was cleared), "everything is unchecked" is
    # just a normal full run, so there is nothing to choose between.
    backlog = await asyncio.to_thread(_load_platform_backlog, service, platform)
    if not backlog.is_empty and backlog.has_any_recorded_state:
        context.user_data[_PENDING_CONFIRMATION_KEY] = {
            "type": _CONFIRM_RETRY_FAILED,
            "platform": platform,
            "user_id": user_id,
        }
        settings = _settings(context)
        display_name = settings.display_name(user_id) if user_id is not None else "کاربر"
        await message.reply_text(
            format_retry_failed_confirmation(
                display_name=display_name,
                platform_label=platform_button_label(platform),
                failed_count=len(backlog.failed_ids),
                unchecked_count=len(backlog.unchecked_ids),
            ),
            reply_markup=build_confirm_keyboard(),
        )
        return

    triggered_by = f"telegram:{user_id}"
    progress = job_manager.try_start(platform, triggered_by=triggered_by)
    if progress is None:
        # Extremely rare race: another job started for this exact platform
        # between the check above and this line. Since nothing was shown to
        # the person to decline, proceed rather than silently drop the tap.
        progress = job_manager.start_parallel(platform, triggered_by=triggered_by)

    await _launch_run(update, context, platform, progress, user_id=user_id)


# -- "All platforms" run -----------------------------------------------------------

_ALL_PLATFORMS_BATCH_KEY = "all_platforms_batch"
# How often a queued platform re-checks whether it is free (only matters when
# the hourly recheck, or another person's run, is holding that platform).
_BATCH_LOCK_POLL_SECONDS = 5.0
_ALL_PLATFORMS_LABEL = "همه‌ی پلتفرم‌ها"


def _active_batch(context: ContextTypes.DEFAULT_TYPE) -> AllPlatformsBatch | None:
    return context.application.bot_data.get(_ALL_PLATFORMS_BATCH_KEY)


def _ordered_platforms(available: list[str]) -> list[str]:
    """Run order for "همه‌ی پلتفرم‌ها": the canonical order in
    platform_names.PLATFORM_DISPLAY_NAMES (Digikala first), with any platform
    not listed there appended in registry order."""
    canonical = [p for p in PLATFORM_DISPLAY_NAMES if p in available]
    return canonical + [p for p in available if p not in canonical]


async def _start_all_platforms_run(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, user_id: int | None
) -> None:
    """"🚀 همه‌ی پلتفرم‌ها": runs the normal full process for every platform,
    one after another — each platform starts only once the previous one has
    completely finished (report sent, job released). The handler returns
    immediately; the whole sequence lives in one background task."""
    message = update.message
    if message is None:
        return
    service = _service(context)
    job_manager = _job_manager(context)

    if _active_batch(context) is not None:
        await message.reply_text(
            format_all_platforms_already_running_message(), reply_markup=build_main_menu_keyboard()
        )
        return
    # A scheduled hourly recheck is not a conflict (the sequence simply waits
    # for that platform); a person's own run is, since chaining onto it would
    # double up work on the same platform.
    if any(
        not job.triggered_by.startswith("scheduler:")
        for p in service.available_platforms
        for job in job_manager.current_all(p)
    ):
        await message.reply_text(
            format_all_platforms_busy_message(), reply_markup=build_main_menu_keyboard()
        )
        return

    platforms = _ordered_platforms(service.available_platforms)
    if not platforms:
        await message.reply_text("هیچ پلتفرمی در دسترس نیست.", reply_markup=build_main_menu_keyboard())
        return

    triggered_by = f"telegram:{user_id}"
    batch = AllPlatformsBatch(triggered_by=triggered_by, remaining=list(platforms))
    context.application.bot_data[_ALL_PLATFORMS_BATCH_KEY] = batch

    chat_id = message.chat.id
    settings = _settings(context)
    task = asyncio.create_task(
        _execute_all_platforms_run(context, chat_id, batch, platforms, triggered_by)
    )
    background_tasks: set = context.application.bot_data.setdefault("_background_tasks", set())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

    notice_recipients = _change_notice_recipients(settings, chat_id=chat_id, user_id=user_id)
    if notice_recipients:
        display_name = settings.display_name(user_id) if user_id is not None else "یک کاربر"
        notice_task = asyncio.create_task(
            broadcast_message_with_retry(
                context.bot,
                chat_ids=notice_recipients,
                text=format_change_notice(display_name, _ALL_PLATFORMS_LABEL),
            )
        )
        background_tasks.add(notice_task)
        notice_task.add_done_callback(background_tasks.discard)


async def _execute_all_platforms_run(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    batch: AllPlatformsBatch,
    platforms: list[str],
    triggered_by: str,
) -> None:
    job_manager = _job_manager(context)
    settings = _settings(context)
    recipients = _report_recipients(settings, fallback_chat_id=chat_id)
    labels = [platform_button_label(p) for p in platforms]

    try:
        await broadcast_message_with_retry(
            context.bot,
            chat_ids=recipients,
            text=format_all_platforms_started_message(labels),
            reply_markup=build_main_menu_keyboard(),
        )

        completed = 0
        for platform in platforms:
            if batch.cancel_requested:
                break
            batch.current = platform

            # Wait for this platform to be free (e.g. the hourly recheck is
            # mid-run on it) instead of skipping it or running in parallel.
            progress = job_manager.try_start(platform, triggered_by=triggered_by)
            while progress is None and not batch.cancel_requested:
                await asyncio.sleep(_BATCH_LOCK_POLL_SECONDS)
                progress = job_manager.try_start(platform, triggered_by=triggered_by)
            if progress is None:  # cancelled while waiting
                break

            context.application.bot_data["last_platform"] = platform
            # _execute_run sends this platform's own start banner and final
            # report, and releases the job in its own `finally`.
            await _execute_run(context, chat_id, platform, progress, triggered_by)
            batch.remaining.remove(platform)
            if progress.cancel_requested:
                batch.cancel_requested = True
                break
            completed += 1

        if batch.cancel_requested:
            not_run = [platform_button_label(p) for p in batch.remaining]
            text = format_all_platforms_stopped_message(not_run)
        else:
            text = format_all_platforms_finished_message(completed, len(platforms))
        await broadcast_message_with_retry(
            context.bot, chat_ids=recipients, text=text, reply_markup=build_main_menu_keyboard()
        )
    finally:
        # Always release the batch, so a crash can never leave "already
        # running" / "clear database blocked" stuck until the next restart.
        context.application.bot_data.pop(_ALL_PLATFORMS_BATCH_KEY, None)


def _find_any_active_job(
    job_manager: PlatformJobManager, platforms: list[str]
) -> JobProgress | None:
    """First active job across ANY platform (manual or the hourly
    scheduler). Used for the parallel-run warning: the person asked that a
    new run — even one started on a different, currently-free platform —
    still be flagged whenever some other update is running anywhere, not
    only when it collides with the same platform.
    """
    for p in platforms:
        jobs = job_manager.current_all(p)
        if jobs:
            return jobs[0]
    return None


def _display_name_for_triggered_by(settings: Settings, triggered_by: str | None) -> str:
    """Best-effort human name for whoever (or whatever) owns an already-
    running job, for the parallel-run warning message. Falls back to a
    generic label when the run was started by the hourly scheduler or the
    triggering user can't be identified.
    """
    if triggered_by is None:
        return "کاربر دیگر"
    if triggered_by.startswith("scheduler:"):
        return "بروزرسانی خودکار زمان‌بندی‌شده"
    prefix = "telegram:"
    if not triggered_by.startswith(prefix):
        return "کاربر دیگر"
    try:
        other_user_id = int(triggered_by[len(prefix) :])
    except ValueError:
        return "کاربر دیگر"
    return settings.display_name(other_user_id)


async def _launch_run(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    platform: str,
    progress: JobProgress,
    *,
    user_id: int | None,
    retry_product_ids: list[str] | None = None,
    retry_failed_count: int | None = None,
    retry_unchecked_count: int | None = None,
) -> None:
    """Actually kicks off a run's background task, whether `progress` came
    from a plain try_start() or from start_parallel() after the person
    confirmed running alongside an existing job. Shared by both paths so
    the fire-and-forget task wiring and the "who did what" heads-up only
    ever live in one place.

    `retry_product_ids` (and the two accompanying counts, used only to build
    the start banner and the final report's scope note) come from
    _resolve_retry_failed_confirmation when the person answered "بله" to the
    "failed/unchecked only" question; every other caller leaves them at their default of
    None, which _execute_run treats as "run the normal full process".
    """
    message = update.message
    if message is None:
        return
    context.application.bot_data["last_platform"] = platform
    chat_id = message.chat.id
    settings = _settings(context)
    triggered_by = progress.triggered_by

    # Fire-and-forget: the handler returns immediately so the bot stays
    # responsive (e.g. to the Stop button) while the run proceeds in the
    # background (spec section 2).
    task = asyncio.create_task(
        _execute_run(
            context,
            chat_id,
            platform,
            progress,
            triggered_by,
            retry_product_ids=retry_product_ids,
            retry_failed_count=retry_failed_count,
            retry_unchecked_count=retry_unchecked_count,
        )
    )
    background_tasks: set = context.application.bot_data.setdefault("_background_tasks", set())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

    # Best-effort "who did what" heads-up to everyone else. Sent from its own
    # background task so it can never delay the handler or the run itself
    # (broadcast_message_with_retry retries indefinitely during an outage and
    # already swallows/logs per-recipient failures).
    notice_recipients = _change_notice_recipients(settings, chat_id=chat_id, user_id=user_id)
    if notice_recipients:
        display_name = settings.display_name(user_id) if user_id is not None else "یک کاربر"
        notice_text = format_change_notice(display_name, platform_display_name(platform))
        notice_task = asyncio.create_task(
            broadcast_message_with_retry(context.bot, chat_ids=notice_recipients, text=notice_text)
        )
        background_tasks.add(notice_task)
        notice_task.add_done_callback(background_tasks.discard)


def _summary_from_results(platform: str, triggered_by: str, results: list[UpdateResult]) -> RunSummary:
    """A RunSummary built from whatever results were collected so far — used
    to still produce a (partial) report when a run is stopped or crashes."""
    summary = RunSummary(platform=platform, triggered_by=triggered_by)
    for result in results:
        summary.add_result(result)
    return summary


async def _send_final_report(
    context: ContextTypes.DEFAULT_TYPE,
    recipients: list[int],
    *,
    summary: RunSummary,
    platform: str,
    dry_run: bool,
    interrupted_note: str | None = None,
) -> None:
    """Sends the end-of-run performance report for every manual platform
    run, regardless of who triggered it.

    There is only ever one report (format_final_digikala_report) and one
    audience: everyone in `recipients` plus ADMIN_CHAT_ID (added once, if
    configured and not already among `recipients`). Admin and everyone
    else see exactly the same text — no separate admin-only detail.

    Long reports are split across several messages (Telegram rejects >4096
    chars, which used to make a big report silently never arrive). A
    formatting bug must never swallow the report, so there's a minimal
    fallback message.
    """
    settings = _settings(context)
    admin_chat_id = settings.admin_chat_id
    platform_label = platform_display_name(platform)

    all_recipients = list(recipients)
    if admin_chat_id is not None and admin_chat_id not in all_recipients:
        all_recipients.append(admin_chat_id)

    try:
        report_text = format_final_digikala_report(
            summary, dry_run=dry_run, platform_label=platform_label, interrupted_note=interrupted_note
        )
    except Exception:  # noqa: BLE001 - the report itself must still go out
        logger.exception("Building the final report for platform '%s' failed", platform)
        report_text = (
            f"📊 گزارش نهایی بروزرسانی {platform_label}\n\n"
            f"تعداد بررسی‌شده: {len(summary.results)}\n\n"
            "⚠️ ساخت جزئیات گزارش با خطا مواجه شد؛ لطفاً لاگ برنامه را بررسی کنید."
        )
    await broadcast_long_message_with_retry(
        context.bot, chat_ids=all_recipients, text=report_text, reply_markup=build_main_menu_keyboard()
    )


async def _execute_run(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    platform: str,
    progress: JobProgress,
    triggered_by: str,
    *,
    retry_product_ids: list[str] | None = None,
    retry_failed_count: int | None = None,
    retry_unchecked_count: int | None = None,
    apply_checked: bool = False,
    check_totals: dict[str, int] | None = None,
) -> None:
    """Runs the platform update and ALWAYS finishes by sending the final
    performance report — no per-product messages during the run. That holds
    whichever way the run ends: normally, stopped by the user, or crashed
    part-way (then the report covers the products processed so far and says
    so). The hooks below only update `progress` (for the pull-based
    "📊 وضعیت اجرا" status menu) and collect results; they never touch
    Telegram themselves, so there's no cross-thread message delivery to
    manage here at all: service.run() does all its work in a worker thread
    via asyncio.to_thread, and the only Telegram calls in this function
    happen before/after that await, safely on the event loop.

    `retry_product_ids` not None means this is a "failed + unchecked only"
    run: service.rerun_specific_products() is called instead of
    service.run(), the start banner names the retry, and the final report
    (on the normal, non-interrupted path only, since a retry run being
    stopped or crashing already produces its own interrupted_note) gets an
    extra scope note built from `retry_failed_count`/`retry_unchecked_count`.
    """
    job_manager = _job_manager(context)
    settings = _settings(context)
    service = _service(context)
    platform_label = platform_button_label(platform)
    recipients = _report_recipients(settings, fallback_chat_id=chat_id)

    if apply_checked and retry_product_ids is not None:
        # `retry_product_ids` here are the products a price check found
        # unchanged (see _execute_apply_checked); the run is the normal update
        # restricted to them.
        start_text = f"▶️ شروع تغییر قیمت {len(retry_product_ids)} کالای تغییرنکرده {platform_label}…"
    elif retry_product_ids is not None:
        start_text = (
            f"▶️ شروع بررسی مجدد {len(retry_product_ids)} کالا "
            f"({retry_failed_count or 0} ناموفق، {retry_unchecked_count or 0} بررسی‌نشده) "
            f"{platform_label}…"
        )
    else:
        start_text = f"▶️ شروع پردازش {platform_label}…"
    # Carrying the main-menu keyboard on the start banner is what takes the
    # requester back to the main menu right after tapping "✅ بله"/"❌ خیر"
    # (or a platform button): without it, the بله/خیر keyboard would stay on
    # screen for the whole run, until the final report finally replaced it.
    await broadcast_message_with_retry(
        context.bot,
        chat_ids=recipients,
        text=start_text,
        reply_markup=build_main_menu_keyboard(),
    )
    if settings.dry_run:
        await broadcast_message_with_retry(
            context.bot,
            chat_ids=recipients,
            text=format_dry_run_banner(platform_display_name(platform)),
        )

    # Every result as it lands, so a partial report can be built if the run
    # is stopped or fails before service.run() returns its summaries.
    collected: list[UpdateResult] = []

    def on_products_loaded(total: int) -> None:
        progress.total = total

    def on_product_start(product: Product) -> None:
        progress.current_product_name = product.name

    def on_round_start(round_no: int, product_count: int) -> None:
        progress.start_round(round_no, product_count)

    def on_result(result: UpdateResult) -> None:
        progress.record(result)
        if progress.round_no > 1:
            # A retried product replaces its earlier entry, so a partial
            # report (stop/crash) lists each product once, with its newest
            # outcome. `is` (not ==) so same-named sheet rows stay separate.
            for i in range(len(collected) - 1, -1, -1):
                if collected[i].product is result.product:
                    collected[i] = result
                    return
        collected.append(result)

    def should_cancel() -> bool:
        return progress.cancel_requested

    try:
        try:
            if retry_product_ids is not None:
                retry_summary = await asyncio.to_thread(
                    service.rerun_specific_products,
                    platform,
                    retry_product_ids,
                    triggered_by=triggered_by,
                    on_products_loaded=on_products_loaded,
                    on_product_start=on_product_start,
                    on_result=on_result,
                    on_round_start=on_round_start,
                    should_cancel=should_cancel,
                    **({"require_check_eligible": True} if apply_checked else {}),
                )
                summaries = {platform: retry_summary}
            else:
                summaries = await asyncio.to_thread(
                    service.run,
                    [platform],
                    triggered_by=triggered_by,
                    on_products_loaded=on_products_loaded,
                    on_product_start=on_product_start,
                    on_result=on_result,
                    on_round_start=on_round_start,
                    should_cancel=should_cancel,
                )
        finally:
            # Always release this specific job, whether it finished, was
            # cancelled, or raised. Passing `progress` (rather than just
            # `platform`) matters when a parallel run was confirmed: it
            # ensures only this job is cleared, leaving any other job still
            # running for the same platform untouched.
            job_manager.finish(platform, progress)
    except Exception:  # noqa: BLE001 - one product's error boundary is inside
        # PriceUpdateService itself; this only catches something outside
        # that (e.g. the sheet source failing to load at all).
        logger.exception("Manual run for platform '%s' failed unexpectedly", platform)
        # The unexpected-error notice must only reach ADMIN_CHAT_ID — regular
        # team members must not see error text (the final report below,
        # sent to everyone including admin, never contains error text
        # either way — see format_final_digikala_report). Fall back to the
        # full recipient list only when no admin is configured, so someone
        # still finds out the run stopped (unchanged pre-existing behavior).
        error_recipients = (
            [settings.admin_chat_id] if settings.admin_chat_id is not None else recipients
        )
        await broadcast_message_with_retry(
            context.bot,
            chat_ids=error_recipients,
            text=(
                "❌ اجرا با خطای غیرمنتظره متوقف شد.\n\n"
                f"تعداد پردازش‌شده:\n{progress.processed}"
            ),
            reply_markup=build_main_menu_keyboard(),
        )
        if collected:
            await _send_final_report(
                context,
                recipients,
                summary=_summary_from_results(platform, triggered_by, collected),
                platform=platform,
                dry_run=settings.dry_run,
                interrupted_note=(
                    "❌ اجرا با خطا متوقف شد؛ این گزارش فقط شامل محصولات پردازش‌شده تا آن لحظه است."
                ),
            )
        return

    if progress.cancel_requested:
        await broadcast_message_with_retry(
            context.bot,
            chat_ids=recipients,
            text=format_stopped_message(progress.processed),
            reply_markup=build_main_menu_keyboard(),
        )
        if collected:
            await _send_final_report(
                context,
                recipients,
                summary=_summary_from_results(platform, triggered_by, collected),
                platform=platform,
                dry_run=settings.dry_run,
                interrupted_note=(
                    "⏹ اجرا توسط کاربر متوقف شد؛ این گزارش فقط شامل محصولات پردازش‌شده تا آن لحظه است."
                ),
            )
        return

    summary = summaries.get(platform) or _summary_from_results(platform, triggered_by, collected)
    if apply_checked:
        retry_scope_note = format_apply_checked_scope_note(
            len(summary.results),
            check_failed_total=(check_totals or {}).get("failed"),
            check_unread=(check_totals or {}).get("unread"),
        )
    else:
        retry_scope_note = (
            format_retry_failed_scope_note(retry_failed_count, retry_unchecked_count)
            if retry_product_ids is not None
            and retry_failed_count is not None
            and retry_unchecked_count is not None
            else None
        )
    await _send_final_report(
        context,
        recipients,
        summary=summary,
        platform=platform,
        dry_run=settings.dry_run,
        interrupted_note=retry_scope_note,
    )