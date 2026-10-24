"""Turns a RunSummary into the Telegram-facing report text (spec section 9).

Pure string formatting — no Telegram/network dependency — so it's fully
unit-testable. Detailed technical error text is included (spec section 13
says don't replace real API errors with a generic message) but truncated to
keep messages readable; full detail always goes through the app's logger
separately (see update.service logging calls).

Only ever reads RunSummary/UpdateResult/Product fields — never Settings or
TokenManager — so no access token, refresh token, bot token, or
Authorization header can flow into a report; those objects don't carry any
of those fields in the first place.
"""
from __future__ import annotations

import re

from faraz_honar.core.models import OutcomeStatus, RunSummary, UpdateResult
from faraz_honar.platform_names import (
    PLATFORM_DISPLAY_NAMES,
    platform_display_name,
    platform_plain_name,
)
from faraz_honar.telegram_bot.state import JobProgress
from faraz_honar.update.price_check import CheckStatus, PlatformCheckResult

_MAX_DETAIL_LINES = 15
_MAX_ERROR_CHARS = 200

# Google Sheet prices are Toman; Product.target_price (and so
# UpdateResult.target_price) is already converted to Rial at the sheet-read
# boundary (sheets/mapping.py::toman_to_rial). This is that conversion's
# exact inverse, used only to also show the operator the original Toman
# figure they'd recognize from the sheet.
_RIAL_PER_TOMAN = 10

_STATUS_LINE_LABELS = {
    OutcomeStatus.SUCCESS: "✅ Updated successfully",
    # CAPPED_12PCT has no fixed label here — the platform's own name is
    # spliced in per-result (see _result_line), since this dict has no
    # access to which platform a given result belongs to.
    OutcomeStatus.SKIPPED_NO_CHANGE: "⏭️ Skipped",
    OutcomeStatus.FAILED_API: "❌ Failed — platform API error",
    OutcomeStatus.FAILED_AUTH: "❌ Failed — authentication/token error",
    OutcomeStatus.FAILED_INVALID_DATA: "❌ Failed — invalid product data",
    OutcomeStatus.FAILED_MISSING_IDENTIFIER: "❌ Failed — missing product identifier",
    OutcomeStatus.FAILED_UNEXPECTED: "❌ Failed — unexpected error",
    OutcomeStatus.FAILED_PRICE_MISMATCH: "❌ Failed — platform price differs from the sheet",
}

_FAILURE_STATUSES = {
    OutcomeStatus.FAILED_API,
    OutcomeStatus.FAILED_AUTH,
    OutcomeStatus.FAILED_INVALID_DATA,
    OutcomeStatus.FAILED_MISSING_IDENTIFIER,
    OutcomeStatus.FAILED_UNEXPECTED,
    OutcomeStatus.FAILED_PRICE_MISMATCH,
}


def format_run_report(summaries: dict[str, RunSummary]) -> str:
    if not summaries:
        return "No platforms were updated (nothing selected or nothing to do)."
    return "\n\n".join(format_platform_summary(s) for s in summaries.values())


def format_platform_summary(summary: RunSummary) -> str:
    total = len(summary.results)
    skipped = [r for r in summary.results if r.status == OutcomeStatus.SKIPPED_NO_CHANGE]

    lines = [f"\U0001F4E6 {platform_display_name(summary.platform)} price update"]
    lines.append(f"Products checked: {total}")
    lines.append(f"✅ Updated successfully: {len(summary.successful)}")
    lines.append(f"⚠️ Limited by {platform_display_name(summary.platform)}'s reference price: {len(summary.capped)}")
    lines.append(f"❌ Failed: {len(summary.failed)}")
    if skipped:
        lines.append(f"⏭️ Skipped: {len(skipped)}")

    if summary.successful:
        lines.append("")
        lines.append("Updated successfully:")
        lines.extend(_grouped_lines(summary.successful))

    if summary.capped:
        lines.append("")
        lines.append(
            f"Rejected by {platform_display_name(summary.platform)}'s reference-price range "
            "this run (submit a ticket to raise it):"
        )
        lines.extend(_grouped_lines(summary.capped))

    if skipped:
        lines.append("")
        lines.append("Skipped:")
        lines.extend(_grouped_lines(skipped))

    if summary.failed:
        lines.append("")
        lines.append("Failed — will not retry further this run:")
        lines.extend(_grouped_lines(summary.failed))

    lines.append("")
    lines.append(
        "✅ Update process completed successfully."
        if not summary.failed
        else "⚠️ Update process completed with some failures — see above."
    )
    return "\n".join(lines)


def format_hourly_recheck_report(summaries: dict[str, RunSummary]) -> str | None:
    """None means nothing was capped, so nothing needs reporting this hour."""
    total_checked = sum(len(s.results) for s in summaries.values())
    if total_checked == 0:
        return None
    header = "\U0001F551 Hourly recheck of price-capped products"
    return header + "\n\n" + format_run_report(summaries)


def _grouped_lines(results: list[UpdateResult]) -> list[str]:
    lines = [_result_line(r) for r in results[:_MAX_DETAIL_LINES]]
    if len(results) > _MAX_DETAIL_LINES:
        lines.append(f"  …and {len(results) - _MAX_DETAIL_LINES} more")
    return lines


def _result_line(r: UpdateResult) -> str:
    variant = r.variant_id or "?"
    toman = _fmt_price(_to_toman(r.target_price))
    rial = _fmt_price(r.target_price)
    reference = _fmt_price(r.previous_price)
    applied = _fmt_price(r.applied_price)
    if r.status == OutcomeStatus.CAPPED_12PCT:
        status_text = f"⚠️ Rejected by {platform_display_name(r.platform)}'s reference-price range (needs a ticket)"
    else:
        status_text = _STATUS_LINE_LABELS.get(r.status, "unknown status")

    detail = ""
    if r.status in _FAILURE_STATUSES and r.error_message:
        detail = f" — {_truncate(r.error_message)}"

    return (
        f"  • {r.product.name} ({platform_plain_name(r.platform)} #{variant}): "
        f"sheet {toman} Toman → target {rial} Rial | "
        f"reference {reference} → applied {applied} | {status_text}{detail}"
    )


def _to_toman(rial_target_price: int | None) -> int | None:
    if rial_target_price is None:
        return None
    return rial_target_price // _RIAL_PER_TOMAN


def _fmt_price(value: int | None) -> str:
    return f"{value:,}" if value is not None else "?"


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_ERROR_CHARS:
        return text
    return text[: _MAX_ERROR_CHARS - 1] + "…"


# ===========================================================================
# Persian control-panel messages (manual Telegram run: menu, status, stop,
# and the ONE final report — no live per-product messages; see
# format_final_digikala_report). These are separate from format_run_report/
# format_platform_summary/format_hourly_recheck_report above, which remain
# exactly as they were and are still what the hourly recheck scheduler
# sends.
#
# Every function below only ever reads UpdateResult/RunSummary/JobProgress
# fields, same as the English formatters above, so nothing here can leak a
# token/Authorization header/credential either.
# ===========================================================================

_NO_ITEMS_TEXT = "هیچ موردی وجود ندارد."


def format_dry_run_banner(platform_label: str | None = None) -> str:
    target = platform_label or "پلتفرم"
    return f"🧪 DRY RUN\n\nاین اجرا شبیه‌سازی است و هیچ قیمت واقعی در {target} تغییر نمی‌کند."


# Why a product was NOT corrected, in plain Persian, per failure kind. The raw
# platform error text (if any) is appended after it — see _not_corrected_lines.
_NOT_CORRECTED_REASON_LABELS = {
    OutcomeStatus.FAILED_API: "خطای API پلتفرم",
    OutcomeStatus.FAILED_AUTH: "خطای احراز هویت / توکن",
    OutcomeStatus.FAILED_INVALID_DATA: "اطلاعات محصول نامعتبر",
    OutcomeStatus.FAILED_MISSING_IDENTIFIER: "شناسه محصول در پلتفرم پیدا نشد",
    OutcomeStatus.FAILED_UNEXPECTED: "خطای غیرمنتظره",
    OutcomeStatus.FAILED_PRICE_MISMATCH: "قیمت پلتفرم با شیت یکسان نیست",
    OutcomeStatus.SKIPPED_NO_CHANGE: "ردشد (نیازی به تغییر نبود)",
}

# Defence in depth: error text from a platform is shown to people in the
# report, so anything that even looks like a credential is masked first.
_SECRET_LIKE_PATTERNS = (
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"eyJ[\w\-.]+"),
    re.compile(r"(?:access_token|refresh_token|authorization)\W{0,3}\S*", re.IGNORECASE),
)


def _redact(text: str) -> str:
    for pattern in _SECRET_LIKE_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _not_corrected_reason(*, status: OutcomeStatus | str, error_message: str | None) -> str:
    """Shared by the UpdateResult-based end-of-run report (_not_corrected_lines
    below) and the price-status Excel export's admin comments
    (reporting/xlsx_export.py imports this directly), so the reason label and
    redaction logic are never duplicated. `status` may be an OutcomeStatus
    (live run) or the equivalent plain string stored in the DB
    (ProductStateRow.status) — OutcomeStatus is a StrEnum, so the dict lookup
    below matches either.
    """
    label = _NOT_CORRECTED_REASON_LABELS.get(status, "دلیل نامشخص")
    detail = _truncate(_redact(error_message)) if error_message else ""
    return f"دلیل: {label}" + (f" — {detail}" if detail else "")


def _totals_line(total: int, successful: int, failed: int) -> str:
    """The ONE counts line shared by the end-of-run report and the price-check
    report (overall and per platform), so both always read the same way:
    کل کالا / موفق / ناموفق. total == successful + failed, always."""
    return f"📦 کل کالا: {total}   ✅ موفق: {successful}   ❌ ناموفق: {failed}"


def format_final_digikala_report(
    summary: RunSummary,
    *,
    dry_run: bool = False,
    platform_label: str,
    interrupted_note: str | None = None,
) -> str:
    """The ONE end-of-run report, sent to every audience — everyone in
    `recipients` and ADMIN_CHAT_ID alike (see handlers._send_final_report).
    There is no separate "public" vs "admin" variant any more: every
    recipient sees exactly the same text.

    Deliberately excludes, for every audience:
      - the list of successful product names (only their count is shown)
      - any per-product failure reason or technical error text

    Built only from UpdateResult.status groupings set entirely by
    update.service/core.pricing — nothing here recomputes the +12% rule or
    any other business decision:
      1. counts — checked / successful / capped / failed, plus the date
      2. products limited by the platform's reference-price range, by name
         only (still actionable — needs a ticket)
      3. products that were NOT corrected, by name only (real failures
         only, exactly what the live status view calls "خطا") — no reason,
         no error text. Products skipped because the price already matched
         the target are NOT failures: they get their own "ردشده" count,
         same as the live status view, and are not listed as failed.

    Products filtered out before any lookup (no sheet reference for this
    platform / no target price) were never processed, so they are absent
    from summary.results and are not listed anywhere.

    `platform_label` (e.g. "Digikala" or "Digikala Saree") only affects the
    title/wording — it plays no part in which products end up in which list.
    `interrupted_note`, when given, is printed under the title (used when the
    run was stopped or crashed part-way, so the report is clearly partial).
    """
    capped = [r.product.name for r in summary.capped]
    not_corrected = [r.product.name for r in summary.results if r.is_failure]
    skipped = [r for r in summary.results if r.status is OutcomeStatus.SKIPPED_NO_CHANGE]

    lines: list[str] = []
    if dry_run:
        lines.append(
            f"🧪 DRY RUN — نتایج زیر شبیه‌سازی هستند و در {platform_label} واقعاً اعمال نشده‌اند."
        )
        lines.append("")
    lines.append(f"📊 گزارش نهایی بروزرسانی {platform_label}")
    lines.append(f"📅 تاریخ: {_today_jalali_date()}")
    if interrupted_note:
        lines.append("")
        lines.append(interrupted_note)
    lines.append("")
    # Same three numbers as the price-check report. "موفق" = the platform now
    # has the sheet price (updated, or already equal so nothing needed
    # changing); "ناموفق" = it does not (a real failure, or limited by the
    # platform's reference-price range). The breakdown line below keeps the
    # old detail so nothing is hidden.
    successful_total = len(summary.successful) + len(skipped)
    failed_total = len(not_corrected) + len(capped)
    lines.append(_totals_line(len(summary.results), successful_total, failed_total))
    breakdown: list[str] = []
    if skipped:
        breakdown.append(f"{len(skipped)} کالا از موفق‌ها از قبل با شیت یکسان بود")
    if capped:
        breakdown.append(f"{len(capped)} کالا از ناموفق‌ها محدودشده در پلتفرم است")
    if breakdown:
        lines.append("ℹ️ " + "؛ ".join(breakdown))
    if summary.error_rounds > 0:
        still_failing = sum(1 for r in summary.results if r.is_failure and r.transient)
        lines.append(
            f"🔁 {summary.error_rounds} دور مجدد انجام شد؛ "
            + (f"{still_failing} کالا همچنان خطا دارد" if still_failing else "همه‌ی کالاهای خطادار موفق شدند")
        )
    lines.append("")
    lines.append(
        f"⚠️ محدودشده در پلتفرم {platform_label} — برای تغییر نهایی تیکت ثبت کنید:"
    )
    lines.extend(_numbered_or_empty(capped))
    lines.append("")
    lines.append("❌ کالاهای ناموفق:")
    lines.extend(_numbered_or_empty(not_corrected))
    lines.append("")
    lines.append(
        "✅ بروزرسانی با موفقیت کامل انجام شد."
        if not summary.failed
        else "⚠️ بروزرسانی با چند خطا به پایان رسید."
    )
    return "\n".join(lines)


def _today_jalali_date() -> str:
    """Today's date (Tehran-local), for the final report's "📅 تاریخ:" line.
    Reuses to_jalali_date's own UTC->Tehran conversion (see its docstring)
    rather than duplicating the timezone logic here.
    """
    from datetime import UTC, datetime

    return to_jalali_date(datetime.now(UTC).isoformat())


def _numbered_or_empty(names: list[str]) -> list[str]:
    if not names:
        return [_NO_ITEMS_TEXT]
    return [f"{i}. {name}" for i, name in enumerate(names, start=1)]


def format_status_message(progress: JobProgress | None, *, platform_label: str) -> str:
    if progress is None:
        return "⚪ در حال حاضر اجرایی فعال نیست."
    if progress.kind == "check":
        check_lines = [
            "🔍 در حال بررسی قیمت‌ها",
            "",
            f"پلتفرم: {platform_label}",
            "",
            f"پیشرفت:\n{progress.processed} / {progress.total}",
            "",
            f"یکسان با شیت: {progress.success}",
            f"تغییر نکرده: {progress.mismatched}",
            f"خطا در خواندن: {progress.failed}",
        ]
        if progress.current_product_name:
            check_lines.append("")
            check_lines.append(f"محصول فعلی:\n{progress.current_product_name}")
        return "\n".join(check_lines)
    lines = [
        "🟢 در حال اجرا",
        "",
        f"پلتفرم: {platform_label}",
        "",
        f"پیشرفت:\n{progress.processed} / {progress.total}",
        "",
        f"موفق: {progress.success}",
        f"محدودشده: {progress.capped}",
        f"خطا: {progress.failed}",
        f"ردشده: {progress.skipped}",
    ]
    if progress.round_no > 1:
        lines.append("")
        lines.append(f"🔁 دور مجدد {progress.round_no - 1}: {progress.round_total} کالا")
    if progress.current_product_name:
        lines.append("")
        lines.append(f"محصول فعلی:\n{progress.current_product_name}")
    return "\n".join(lines)


# -- Price check ("🔍 چک و بررسی قیمت‌ها") --------------------------------------------


def format_price_check_duration(seconds: float) -> str:
    if seconds < 60:
        return "کمتر از 1 دقیقه"
    return f"حدود {round(seconds / 60)} دقیقه"


def format_price_check_started_message(
    *,
    platform_counts: list[tuple[str, int]],
    estimate_seconds: float,
    busy_platforms: list[str],
) -> str:
    """Sent when a price check starts. Always says up front that it can take a
    while (and roughly how long), like the update run's start banner does."""
    lines = [
        "🔍 شروع بررسی قیمت‌ها",
        "فقط خواندن قیمت‌ها؛ هیچ قیمتی روی پلتفرم‌ها تغییر نمی‌کند.",
        "",
        "پلتفرم‌ها (همه هم‌زمان):",
    ]
    lines.extend(
        f"• {platform_display_name(platform)}: {count} کالا" for platform, count in platform_counts
    )
    lines.append("")
    lines.append(
        f"⏱ این بررسی ممکن است طول بکشد: {format_price_check_duration(estimate_seconds)} "
        "(پلتفرم کندتر زمان کل را تعیین می‌کند)."
    )
    lines.append("پیشرفت را با «📊 وضعیت اجرا» ببینید؛ گزارش نهایی پس از پایان ارسال می‌شود.")
    if busy_platforms:
        lines.append("")
        lines.append("⏭ این پلتفرم‌ها اجرای دیگری در حال انجام دارند و بررسی نشدند:")
        lines.extend(f"• {platform_display_name(p)}" for p in busy_platforms)
    return "\n".join(lines)


def format_price_check_all_busy_message() -> str:
    return (
        "همه‌ی پلتفرم‌ها اجرای دیگری در حال انجام دارند؛ ابتدا آن را متوقف کنید "
        "یا منتظر پایانش بمانید."
    )


def format_price_check_already_running_message() -> str:
    return "یک بررسی قیمت از قبل در حال اجراست؛ منتظر گزارش آن بمانید."


def format_price_check_nothing_to_check_message() -> str:
    return (
        "هیچ کالایی برای بررسی پیدا نشد. فقط کالاهایی بررسی می‌شوند که «اسم جدید» و "
        "«قیمت اصلی» آن‌ها پر و سلول پلتفرمشان سبز باشد."
    )


def _ordered_check_platforms(results: dict[str, PlatformCheckResult]) -> list[str]:
    canonical = [p for p in PLATFORM_DISPLAY_NAMES if p in results]
    return canonical + [p for p in results if p not in canonical]


def format_price_check_report(
    results: dict[str, PlatformCheckResult],
    *,
    busy_platforms: list[str] | None = None,
    dry_run: bool = False,
    include_reasons: bool = False,
    interrupted_note: str | None = None,
) -> str:
    """The price-check report: per platform, the products whose price is NOT
    the sheet's price ("تغییر نکرده"), then the ones whose price could not be
    read. Prices are shown in Toman, like the sheet. Failure reasons are only
    included for the admin (`include_reasons`), same rule as the Excel report.
    """
    ordered = _ordered_check_platforms(results)
    total = sum(len(r.entries) for r in results.values())
    matched = sum(len(r.matched) for r in results.values())
    mismatched = sum(len(r.mismatched) for r in results.values())
    errors = sum(len(r.errors) for r in results.values())
    platforms_failed = sum(1 for r in results.values() if r.platform_error)

    lines: list[str] = []
    if dry_run:
        lines.append(
            "🧪 DRY RUN فعال است: قیمت‌ها هیچ‌وقت روی پلتفرم‌ها اعمال نشده‌اند، "
            "پس مغایرت‌های زیر طبیعی‌اند."
        )
        lines.append("")
    lines.append("🔍 گزارش بررسی قیمت‌ها")
    lines.append(f"📅 تاریخ: {_today_jalali_date()}")
    if interrupted_note:
        lines.append("")
        lines.append(interrupted_note)
    lines.append("")
    lines.append(_totals_line(total, matched, mismatched + errors))
    if mismatched or errors:
        lines.append(f"ℹ️ ناموفق شامل {mismatched} کالای تغییرنکرده و {errors} کالای خطادار است")
    if platforms_failed:
        lines.append(f"❌ پلتفرم بررسی‌نشده به‌علت خطا: {platforms_failed}")

    for platform in ordered:
        result = results[platform]
        label = platform_display_name(platform)
        lines.append("")
        lines.append("━━━━━━━━━━━━")
        if result.platform_error:
            lines.append(f"{label} — ❌ بررسی این پلتفرم انجام نشد")
            if include_reasons:
                lines.append(f"دلیل: {_truncate(_redact(result.platform_error))}")
            continue
        lines.append(
            f"{label} — 📦 {len(result.entries)}   ✅ {len(result.matched)}   "
            f"❌ {len(result.mismatched) + len(result.errors)}"
        )
        if result.cancelled:
            lines.append(
                f"⏹ متوقف شد؛ فقط {len(result.entries)} از {result.planned} کالا بررسی شد."
            )
        if result.skipped_no_display_name:
            lines.append(f"ℹ️ {result.skipped_no_display_name} کالا بدون «اسم جدید» بررسی نشد.")
        if result.skipped_no_target:
            lines.append(f"ℹ️ {result.skipped_no_target} کالا بدون قیمت اصلی در شیت بررسی نشد.")
        if result.skipped_not_green:
            lines.append(f"ℹ️ {result.skipped_not_green} کالا با سلول غیرسبز بررسی نشد.")
        failed_entries = result.mismatched + result.errors
        if failed_entries:
            lines.append("")
            lines.append("❌ کالاهای ناموفق:")
            for i, entry in enumerate(failed_entries, start=1):
                if entry.status is CheckStatus.MISMATCH:
                    line = (
                        f"{i}. {entry.product.name}: "
                        f"{_fmt_price(_to_toman(entry.current_price))} تومان ← "
                        f"شیت {_fmt_price(_to_toman(entry.target_price))} تومان"
                    )
                else:
                    line = f"{i}. {entry.product.name} — قیمت خوانده نشد"
                    if include_reasons and entry.error_message:
                        line += f": {_truncate(_redact(entry.error_message))}"
                lines.append(line)
        elif result.entries:
            lines.append("✅ همه‌ی کالاها با شیت یکسان‌اند.")

    if busy_platforms:
        lines.append("")
        lines.append("⏭ بررسی نشدند (اجرای دیگری در حال انجام بود):")
        lines.extend(f"• {platform_display_name(p)}" for p in busy_platforms)

    lines.append("")
    if mismatched == 0 and errors == 0 and platforms_failed == 0 and not busy_platforms:
        lines.append("✅ قیمت همه‌ی کالاها در همه‌ی پلتفرم‌ها با شیت یکسان است.")
    else:
        lines.append("⚠️ بررسی با مغایرت یا خطا به پایان رسید.")
    return "\n".join(lines)


def format_duplicate_run_message(platform_label: str) -> str:
    return (
        f"⚠️ یک اجرای {platform_label} در حال انجام است.\n\n"
        "ابتدا اجرای فعلی را متوقف کنید یا منتظر پایان آن باشید."
    )


def _backlog_breakdown(failed_count: int, unchecked_count: int) -> list[str]:
    parts: list[str] = []
    if failed_count:
        parts.append(f"{failed_count} کالا ناموفق بوده‌اند")
    if unchecked_count:
        parts.append(f"{unchecked_count} کالا هنوز بررسی نشده‌اند")
    return parts


def format_retry_failed_confirmation(
    *, display_name: str, platform_label: str, failed_count: int, unchecked_count: int
) -> str:
    """Shown when the user starts a platform run and, judging from the current
    sheet plus the latest recorded state of every product (not from any one
    past run), some eligible products either FAILED (FAILED_* / CAPPED_12PCT)
    or were never checked at all on that platform. Answering "بله" processes
    only those products (see handlers._resolve_retry_failed_confirmation);
    "خیر" starts a new full process over every eligible product.
    """
    lines = [f"{display_name} عزیز، در وضعیت فعلی {platform_label}:"]
    lines.extend(f"• {part}" for part in _backlog_breakdown(failed_count, unchecked_count))
    lines.append("")
    lines.append("آیا فقط همین کالاها بررسی شوند؟")
    lines.append("«بله» = فقط همین کالاها  |  «خیر» = شروع پردازش جدید برای همه کالاها")
    return "\n".join(lines)


def format_retry_failed_scope_note(failed_count: int, unchecked_count: int) -> str:
    """Passed as `interrupted_note` to format_final_digikala_report when a
    run only covers the failed/unchecked products, so the final report makes
    the reduced scope explicit rather than reading like a normal full run.
    """
    total = failed_count + unchecked_count
    return (
        f"ℹ️ این اجرا فقط شامل {total} کالا بود ({failed_count} ناموفق و "
        f"{unchecked_count} بررسی‌نشده)؛ سایر کالاها دوباره بررسی نشدند."
    )


def format_all_platforms_started_message(platform_labels: list[str]) -> str:
    lines = ["🚀 پردازش همه‌ی پلتفرم‌ها شروع شد.", "", "ترتیب اجرا:"]
    lines.extend(f"{i}. {label}" for i, label in enumerate(platform_labels, start=1))
    lines.append("")
    lines.append("هر پلتفرم پس از پایان کامل پلتفرم قبلی شروع می‌شود.")
    return "\n".join(lines)


def format_all_platforms_finished_message(completed: int, total: int) -> str:
    return f"✅ پردازش همه‌ی پلتفرم‌ها به پایان رسید ({completed} از {total})."


def format_all_platforms_stopped_message(not_run_labels: list[str]) -> str:
    text = "⏹ پردازش همه‌ی پلتفرم‌ها متوقف شد."
    if not_run_labels:
        text += "\n\nاجرا نشدند:\n" + "\n".join(f"• {label}" for label in not_run_labels)
    return text


def format_all_platforms_busy_message() -> str:
    return (
        "پردازش دیگری در حال انجام است؛ برای اجرای همه‌ی پلتفرم‌ها ابتدا آن را "
        "متوقف کنید یا منتظر پایانش بمانید."
    )


def format_all_platforms_already_running_message() -> str:
    return "پردازش همه‌ی پلتفرم‌ها از قبل در حال اجراست."


def format_apply_checked_confirmation(
    *, display_name: str, platform_counts: list[tuple[str, int]]
) -> str:
    """Asked right after a price-check report that lists unchanged products
    ("تغییر نکرده"). "بله" applies the sheet price to exactly those products
    (see handlers._resolve_apply_checked_confirmation); "خیر" changes nothing.
    """
    total = sum(count for _, count in platform_counts)
    lines = [
        f"{display_name} عزیز، {total} کالا قیمتشان با شیت یکسان نیست (تغییر نکرده):",
    ]
    lines.extend(
        f"• {platform_display_name(platform)}: {count} کالا" for platform, count in platform_counts
    )
    lines.append("")
    lines.append("آیا می‌خواهید تغییر قیمت را روی این کالاها اعمال کنم؟")
    lines.append("«بله» = شروع تغییر قیمت فقط همین کالاها  |  «خیر» = هیچ قیمتی تغییر نکند")
    return "\n".join(lines)


def format_apply_checked_declined_message() -> str:
    return "تغییر قیمتی انجام نشد؛ هیچ قیمتی روی پلتفرم‌ها عوض نشد."


def format_apply_checked_scope_note(
    count: int, *, check_failed_total: int | None = None, check_unread: int | None = None
) -> str:
    """Passed as `interrupted_note` to the final report of an apply-after-check
    run, so the report says its scope is only the products the check found
    unchanged. When the check's own totals are known, it also says how many
    products were unsuccessful in the check overall (unchanged + unreadable),
    so the number matches the check report instead of only this run's scope."""
    lines = [
        f"ℹ️ این اجرا فقط شامل {count} کالای تغییرنکرده‌ی گزارش بررسی قیمت بود؛ "
        "سایر کالاها دوباره بررسی نشدند."
    ]
    if check_failed_total is not None:
        unread = check_unread or 0
        line = f"📋 در بررسی قیمت در کل {check_failed_total} کالا ناموفق بود"
        if unread:
            line += (
                f" ({check_failed_total - unread} کالای تغییرنکرده + {unread} کالا که قیمتشان "
                "خوانده نشد)"
            )
        lines.append(line + ".")
        if unread:
            lines.append(
                f"⚠️ {unread} کالای خوانده‌نشده در این اجرا بررسی یا بروزرسانی نشدند و همچنان "
                "در اکسل ❌ می‌مانند."
            )
    return "\n".join(lines)


def format_apply_checked_busy_message(platform_labels: list[str]) -> str:
    lines = ["⏭ این پلتفرم‌ها اجرای دیگری در حال انجام دارند و تغییر قیمت روی آن‌ها شروع نشد:"]
    lines.extend(f"• {label}" for label in platform_labels)
    lines.append("پس از پایان اجرای فعلی، دوباره «🔍 چک و بررسی قیمت‌ها» را بزنید.")
    return "\n".join(lines)


def format_retry_nothing_left_message(platform_label: str) -> str:
    """Shown if, by the time the person answers "بله", the failed/unchecked
    products were already dealt with (e.g. by a parallel run or the hourly
    recheck), so there is nothing left to retry."""
    return (
        f"در {platform_label} دیگر کالای ناموفق یا بررسی‌نشده‌ای باقی نمانده است؛ "
        "پردازشی شروع نشد."
    )


def format_parallel_run_confirmation(
    *, requester_display_name: str, other_display_name: str, platform_label: str
) -> str:
    """Shown instead of an immediate block when the requester tries to
    start a platform run while another one is already in progress (started
    by themself or by someone else). Answering "بله" runs a second, parallel
    process for the same platform; "خیر" cancels this new attempt and
    leaves the existing run untouched.
    """
    return (
        f"{requester_display_name} عزیز، پردازش دیگری توسط {other_display_name} "
        f"برای پلتفرم {platform_label} در حال انجام است.\n\n"
        "آیا می‌خواهید پردازشی موازی این پردازش اجرا کنید؟"
    )


def format_parallel_run_declined_message() -> str:
    return "پردازش جدید شروع نشد؛ پردازش در حال اجرا بدون تغییر ادامه می‌یابد."


def format_stop_confirmation() -> str:
    """Shown before actually cancelling an active run: stopping discards
    the in-progress product history for that run (nothing already recorded
    from a completed earlier run is affected). Answering "بله" proceeds
    with the stop; "خیر" leaves the run untouched.
    """
    return (
        "پس از توقف پردازش، تاریخچه پردازش کالا تا اینجای کار از بین می‌رود.\n\n"
        "آیا مایل به توقف هستید؟"
    )


def format_stop_cancelled_message() -> str:
    return "توقف لغو شد؛ پردازش همچنان در حال اجراست."


def format_stop_requested_message() -> str:
    return "⏹ درخواست توقف ثبت شد.\n\nپردازش فعلی تکمیل می‌شود و اجرای بعدی متوقف خواهد شد."


def format_stopped_message(processed_count: int) -> str:
    return f"⏹ اجرا متوقف شد.\n\nتعداد پردازش‌شده:\n{processed_count}"


def format_no_active_run_message() -> str:
    return "⚪ در حال حاضر اجرایی فعال نیست."


def format_clear_database_confirmation() -> str:
    """Shown before "🧹 پاکسازی دیتابیس" actually runs — this operation is
    irreversible, so it always asks first (see
    handlers._handle_clear_database). Answering "بله" wipes the full
    history; "خیر" leaves everything untouched.
    """
    return (
        "با این کار تاریخچه‌ی کامل دیتابیس (وضعیت موفق/ناموفق/محدودشده‌ی همه "
        "کالاها و آخرین اجراها) و فایل لاگ برنامه برای همیشه پاک می‌شود.\n\n"
        "آیا مطمئن هستید؟"
    )


def format_clear_database_cancelled_message() -> str:
    return "پاکسازی لغو شد."


def format_clear_database_blocked_message() -> str:
    """Shown instead of the بله/خیر confirmation (or, as a fail-safe, instead
    of actually clearing) when a run is active — clearing while product_state
    or the log file is being written mid-run could leave either in an
    inconsistent state.
    """
    return (
        "⚠️ یک اجرا در حال انجام است؛ ابتدا آن را متوقف یا تمام کنید، سپس دوباره تلاش کنید."
    )


def format_clear_database_result_message(*, db_cleared: bool, log_cleared: bool) -> str:
    """The one result message for "🧹 پاکسازی دیتابیس", covering both the
    full-success and the partial-failure case — never silent about which
    part did or didn't happen (db and log are cleared independently; see
    handlers._handle_clear_database_confirmed).
    """
    if db_cleared and log_cleared:
        return "✅ دیتابیس و لاگ برنامه پاکسازی شد."
    if db_cleared and not log_cleared:
        return "⚠️ دیتابیس پاک شد ولی پاکسازی لاگ با خطا مواجه شد."
    if not db_cleared and log_cleared:
        return "⚠️ لاگ برنامه پاک شد ولی پاکسازی دیتابیس با خطا مواجه شد."
    return "❌ پاکسازی دیتابیس و لاگ هر دو با خطا مواجه شدند."


def format_change_notice(display_name: str, platform_label: str) -> str:
    """Heads-up sent to the *other* configured people when someone starts a
    price update ("who did what"). Pure formatting — no I/O.
    """
    return f"🔔 {display_name} به‌روزرسانی قیمت‌های {platform_label} را شروع کرد."


def to_jalali_date(iso_utc: str) -> str:
    """started_at is stored as a UTC ISO string (datetime.now(UTC).isoformat()
    in state/repository.py). Converted to Tehran local time before taking the
    date, so a run just before/after midnight UTC doesn't show the wrong
    Jalali day. jdatetime/zoneinfo are only needed here, so imported locally
    rather than adding a module-wide dependency for the rest of this file.

    Public (not `_to_jalali_date`) so telegram_bot/handlers.py can reuse it
    for the "retry failed only" confirmation message (spec section 3).
    """
    import jdatetime
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    dt_utc = datetime.fromisoformat(iso_utc).replace(tzinfo=UTC)
    dt_tehran = dt_utc.astimezone(ZoneInfo("Asia/Tehran"))
    j = jdatetime.date.fromgregorian(date=dt_tehran.date())
    return f"{j.year:04d}/{j.month:02d}/{j.day:02d}"