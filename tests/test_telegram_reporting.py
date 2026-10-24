from faraz_honar.core.models import OutcomeStatus, Product, RunSummary, UpdateResult
from faraz_honar.core.pricing import compute_price_to_apply
from faraz_honar.telegram_bot import reporting
from faraz_honar.telegram_bot.reporting import (
    format_change_notice,
    format_dry_run_banner,
    format_duplicate_run_message,
    format_final_digikala_report,
    format_hourly_recheck_report,
    format_no_active_run_message,
    format_price_check_report,
    format_price_check_started_message,
    format_retry_failed_confirmation,
    format_retry_failed_scope_note,
    format_retry_nothing_left_message,
    format_run_report,
    format_status_message,
    format_stop_requested_message,
    format_stopped_message,
)
from faraz_honar.telegram_bot.state import JobProgress
from faraz_honar.update.price_check import CheckStatus, PlatformCheckResult, PriceCheckEntry


def _product(name="Widget") -> Product:
    return Product(internal_id=name, name=name, reference_price=1_000_000, target_price=1_500_000)


def test_report_counts_success_capped_failed():
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=1000, applied_price=1100, target_price=1100, variant_id="111"),
        UpdateResult(product=_product("B"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1000, applied_price=1120, target_price=2000, variant_id="222"),
        UpdateResult(product=_product("C"), platform="digikala", status=OutcomeStatus.FAILED_API,
                     error_message="HTTP 500: internal error"),
    ]
    text = format_run_report({"digikala": summary})

    assert "Products checked: 3" in text
    assert "Updated successfully: 1" in text
    assert "Limited by 🔴 Digikala's reference price: 1" in text
    assert "Failed: 1" in text
    assert "B (Digikala #222)" in text
    assert "1,120" in text and "2,000" in text
    assert "HTTP 500" in text
    assert "completed with some failures" in text


def test_report_all_success_has_clean_completion_message():
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=1000, applied_price=1100, target_price=1100),
    ]
    text = format_run_report({"digikala": summary})
    assert "completed successfully" in text
    assert "Failed" in text  # count line still present, just zero


def test_hourly_report_is_none_when_nothing_checked():
    empty_summary = RunSummary(platform="digikala", triggered_by="scheduler:hourly")
    assert format_hourly_recheck_report({"digikala": empty_summary}) is None
    assert format_hourly_recheck_report({}) is None


def test_hourly_report_present_when_something_checked():
    summary = RunSummary(platform="digikala", triggered_by="scheduler:hourly")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1000, applied_price=1120, target_price=2000),
    ]
    text = format_hourly_recheck_report({"digikala": summary})
    assert text is not None
    assert "Hourly recheck" in text


def test_long_error_message_is_truncated():
    long_error = "x" * 500
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.FAILED_API,
                     error_message=long_error),
    ]
    text = format_run_report({"digikala": summary})
    assert "x" * 500 not in text
    assert "…" in text


# -- Per-product detail: name, variant id, Toman/Rial prices, reference/applied ------


def test_success_line_includes_full_product_detail():
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(
            product=_product("Nut Bowl"),
            platform="digikala",
            status=OutcomeStatus.SUCCESS,
            previous_price=1_000_000,  # DigiKala's live reference price
            applied_price=1_100_000,  # final selling price actually set
            target_price=1_100_000,  # already-converted Rial target
            variant_id="83841925",
        ),
    ]
    text = format_run_report({"digikala": summary})

    assert "Nut Bowl" in text
    assert "Digikala #83841925" in text
    assert "110,000 Toman" in text  # 1,100,000 Rial / 10 -> the sheet's Toman figure
    assert "1,100,000 Rial" in text
    assert "1,000,000" in text  # reference price
    assert "✅ Updated successfully" in text


def test_capped_line_shows_pending_status():
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(
            product=_product("B"),
            platform="digikala",
            status=OutcomeStatus.CAPPED_12PCT,
            previous_price=1_000_000,
            applied_price=1_120_000,
            target_price=2_000_000,
            variant_id="222",
        ),
    ]
    text = format_run_report({"digikala": summary})
    assert "⚠️ Rejected by 🔴 Digikala's reference-price range (needs a ticket)" in text
    assert "200,000 Toman" in text  # 2,000,000 Rial / 10


def test_failed_line_shows_failed_status_with_question_marks_for_missing_fields():
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(
            product=_product("C"),
            platform="digikala",
            status=OutcomeStatus.FAILED_INVALID_DATA,
            error_message="No target price found for this product.",
        ),
    ]
    text = format_run_report({"digikala": summary})
    assert "❌ Failed — invalid product data" in text
    assert "Digikala #?" in text  # never resolved a variant for this product


def test_skipped_status_is_reported():
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(
            product=_product("D"),
            platform="digikala",
            status=OutcomeStatus.SKIPPED_NO_CHANGE,
            previous_price=1_000_000,
            target_price=1_000_000,
        ),
    ]
    text = format_run_report({"digikala": summary})
    assert "⏭️ Skipped: 1" in text
    assert "⏭️ Skipped" in text  # per-product status line


def test_detail_list_is_truncated_and_grouped_not_one_message_per_product():
    # 20 successes should not produce 20 unbounded lines — proves the report
    # stays readable/grouped rather than confusing for a large run.
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(
            product=_product(f"P{i}"),
            platform="digikala",
            status=OutcomeStatus.SUCCESS,
            previous_price=1000,
            applied_price=1000,
            target_price=1000,
        )
        for i in range(20)
    ]
    text = format_run_report({"digikala": summary})
    assert "…and 5 more" in text


# -- Never expose secrets ------------------------------------------------------------


def test_report_never_contains_tokens_or_auth_headers():
    # Reporting only ever reads RunSummary/UpdateResult/Product fields —
    # never Settings or TokenManager — so no bot token, access token,
    # refresh token, or Authorization header can appear here even for a
    # realistic auth-failure message (the exact shape DigikalaClient raises,
    # which itself never includes the token value — see
    # platforms/digikala/client.py's PlatformAuthError messages).
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(
            product=_product("A"),
            platform="digikala",
            status=OutcomeStatus.FAILED_AUTH,
            error_message=(
                "DigiKala access token expired/invalid and refresh could not be "
                "completed (PUT /open-api/v1/variants/1 (401 response)): 401 Unauthorized"
            ),
        ),
    ]
    text = format_run_report({"digikala": summary})

    for secret_pattern in (
        "Bearer ",
        "Authorization",
        "access_token",
        "refresh_token",
        "eyJ",  # JWT header prefix
    ):
        assert secret_pattern not in text


# ===========================================================================
# Persian control-panel messages (manual Telegram run, live reporting)
# ===========================================================================


def test_dry_run_banner_clearly_states_simulation_mode():
    text = format_dry_run_banner()
    assert "DRY RUN" in text
    assert "شبیه‌سازی" in text


def test_final_report_lists_capped_product_names_but_never_successful_names():
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.results = [
        UpdateResult(product=_product("Nut Bowl"), platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=1_000_000, applied_price=1_100_000, target_price=1_100_000),
        UpdateResult(product=_product("Gift Pack"), platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=500_000, applied_price=550_000, target_price=550_000),
        UpdateResult(product=_product("Traditional Cloth"), platform="digikala",
                     status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1_000_000, applied_price=1_120_000, target_price=2_000_000),
    ]
    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")

    assert "📊 گزارش نهایی بروزرسانی Digikala" in text
    assert "⚠️ محدودشده در پلتفرم Digikala — برای تغییر نهایی تیکت ثبت کنید:" in text
    assert "1. Traditional Cloth" in text
    # Successful products are counted but their names are never listed.
    assert "✅ موفق: 2" in text
    assert "Nut Bowl" not in text
    assert "Gift Pack" not in text


def test_final_report_shows_clear_empty_state_for_each_section():
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")

    # Only two sections list products now (capped, not-corrected) — each
    # shows an explicit empty state.
    assert text.count("هیچ موردی وجود ندارد.") == 2


def test_final_report_shows_todays_date():
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    assert "📅 تاریخ:" in text


def test_final_report_title_distinguishes_digikala_from_digikala_saree():
    # Same summary, same formatter, only platform_label differs — proves
    # the title (and nothing about which products are listed) is what
    # tells the two accounts' reports apart.
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1000, applied_price=1000, target_price=2000),
    ]
    main_text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    saree_text = format_final_digikala_report(
        summary, dry_run=False, platform_label="Digikala Saree"
    )

    assert "📊 گزارش نهایی بروزرسانی Digikala\n" in main_text
    assert "Digikala Saree" not in main_text
    assert "📊 گزارش نهایی بروزرسانی Digikala Saree" in saree_text
    assert "1. A" in main_text and "1. A" in saree_text  # same product list either way


def test_final_report_lists_every_not_corrected_product_by_name_without_a_reason():
    # Products that could not be corrected — for ANY reason — are listed by
    # name only, in their own section; the reason/error text is never shown
    # to any report recipient any more.
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.results = [
        UpdateResult(product=_product("Not Found"), platform="digikala",
                     status=OutcomeStatus.FAILED_MISSING_IDENTIFIER,
                     error_message="No DigiKala variant found matching DKP code '999'."),
        UpdateResult(product=_product("Real Error"), platform="digikala",
                     status=OutcomeStatus.FAILED_API,
                     error_message="DigiKala price update failed: HTTP 500: internal error"),
        UpdateResult(product=_product("Auth Error"), platform="digikala",
                     status=OutcomeStatus.FAILED_AUTH),
        UpdateResult(product=_product("Bad Data"), platform="digikala",
                     status=OutcomeStatus.FAILED_INVALID_DATA,
                     error_message="No target price found for this product."),
        UpdateResult(product=_product("Odd One"), platform="digikala",
                     status=OutcomeStatus.FAILED_UNEXPECTED, error_message="KeyError: 'x'"),
        UpdateResult(product=_product("Skipped One"), platform="digikala",
                     status=OutcomeStatus.SKIPPED_NO_CHANGE),
    ]
    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")

    section = text.split("❌ کالاهای ناموفق:")[1]
    for name in ("Not Found", "Real Error", "Auth Error", "Bad Data", "Odd One"):
        assert name in section
    # A skipped (already-at-target) product is NOT a failure: same as the
    # live status view, it is counted under "ردشده" and not listed here.
    assert "Skipped One" not in section
    # Numbered, one entry per product.
    assert "1. Not Found" in section and "5. Odd One" in section
    # No reason labels and no raw error text anywhere in the report.
    for leaked in (
        "شناسه محصول در پلتفرم پیدا نشد",
        "خطای API پلتفرم",
        "خطای احراز هویت",
        "اطلاعات محصول نامعتبر",
        "خطای غیرمنتظره",
        "ردشد (نیازی به تغییر نبود)",
        "No DigiKala variant found",
        "internal error",
        "KeyError",
    ):
        assert leaked not in text
    # Same totals line as the price-check report: the skipped (already equal)
    # product counts as successful, the five real failures as failed.
    assert "📦 کل کالا: 6   ✅ موفق: 1   ❌ ناموفق: 5" in text


def test_final_report_has_a_counts_line_and_clean_empty_not_corrected_section():
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=1000, applied_price=1100, target_price=1100),
        UpdateResult(product=_product("B"), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                     previous_price=1000, applied_price=1120, target_price=2000),
    ]
    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")

    # A capped product did not reach the sheet price, so it counts as failed in
    # the shared totals line; the breakdown line says it is a capped one.
    assert "📦 کل کالا: 2   ✅ موفق: 1   ❌ ناموفق: 1" in text
    assert "1 کالا از ناموفق‌ها محدودشده در پلتفرم است" in text
    section = text.split("❌ کالاهای ناموفق:")[1]
    assert section.strip().splitlines()[0] == "هیچ موردی وجود ندارد."
    # A capped product is reported in its own list, not as "not corrected".
    assert "B" not in section.split("\n\n")[0]


def test_not_corrected_reason_still_redacts_credential_like_text():
    # format_final_digikala_report no longer shows any reason/error text, but
    # the shared helper it used to call is still used by the price-status
    # Excel export's admin comments (xlsx_export.py), so its redaction must
    # keep working even though it's no longer exercised via the Telegram report.
    text = reporting._not_corrected_reason(
        status=OutcomeStatus.FAILED_API,
        error_message=(
            "request failed with header Authorization: Bearer abc.def.ghi and "
            "token eyJhbGciOiJIUzI1NiJ9.payload.sig plus refresh_token=zzz999"
        ),
    )
    for leaked in ("abc.def.ghi", "eyJ", "zzz999", "Bearer ", "Authorization", "refresh_token"):
        assert leaked not in text
    assert "[redacted]" in text
    assert "request failed with header" in text  # the useful part of the message survives


def test_final_report_interrupted_note_marks_a_partial_report():
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    note = "⏹ اجرا توسط کاربر متوقف شد؛ این گزارش فقط شامل محصولات پردازش‌شده تا آن لحظه است."
    text = format_final_digikala_report(
        summary, dry_run=False, platform_label="Digikala", interrupted_note=note
    )
    assert text.split("\n")[0] == "📊 گزارش نهایی بروزرسانی Digikala"
    assert note in text
    plain = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    assert "⏹" not in plain


def test_final_report_dry_run_line_names_the_platform():
    summary = RunSummary(platform="basalam", triggered_by="telegram:1")
    text = format_final_digikala_report(summary, dry_run=True, platform_label="Basalam")
    assert "در Basalam واقعاً اعمال نشده‌اند" in text
    assert "DigiKala" not in text


def test_dry_run_banner_names_the_platform_when_given():
    banner = format_dry_run_banner("SnappShop Tehran")
    assert "هیچ قیمت واقعی در SnappShop Tehran تغییر نمی‌کند" in banner
    assert "DigiKala" not in format_dry_run_banner()


def test_result_and_summary_lines_use_the_platforms_own_display_name():
    # Used to be a hardcoded "DigiKala #…" for every platform.
    summary = RunSummary(platform="snappshop_tehran", triggered_by="test")
    summary.results = [
        UpdateResult(product=_product("A"), platform="snappshop_tehran",
                     status=OutcomeStatus.SUCCESS, previous_price=1000, applied_price=1100,
                     target_price=1100, variant_id="555"),
    ]
    text = format_run_report({"snappshop_tehran": summary})
    assert "A (SnappShop Tehran #555)" in text
    assert "SnappShop Tehran price update" in text
    assert "DigiKala" not in text


def test_final_report_reuses_business_logic_output_not_a_second_calculation():
    # The capped entry comes purely from RunSummary.capped (an UpdateResult
    # whose status core.pricing.compute_price_to_apply's caller set) — no
    # price value is recomputed or even displayed here, only the name.
    decision = compute_price_to_apply(reference_price=1_000_000, target_price=2_000_000)
    assert decision.is_capped is True

    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.results = [
        UpdateResult(
            product=_product("Capped Product"),
            platform="digikala",
            status=OutcomeStatus.CAPPED_12PCT,
            previous_price=decision.reference_price,
            applied_price=decision.price_to_apply,
            target_price=decision.target_price,
        ),
    ]
    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    assert "1. Capped Product" in text


def test_final_report_notes_dry_run_simulation():
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=1000, applied_price=1000, target_price=1000),
    ]
    dry_text = format_final_digikala_report(summary, dry_run=True, platform_label="Digikala")
    real_text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")

    assert "DRY RUN" in dry_text
    assert "شبیه‌سازی" in dry_text
    assert "✅ موفق: 1" in dry_text  # the simulated success is still counted
    assert "DRY RUN" not in real_text


def test_final_report_never_contains_a_secret_or_any_error_text():
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.results = [
        UpdateResult(
            product=_product("A"),
            platform="digikala",
            status=OutcomeStatus.FAILED_AUTH,
            error_message=(
                "DigiKala access token expired/invalid and refresh could not be "
                "completed (PUT /open-api/v1/variants/1 (401 response)): 401 Unauthorized"
            ),
        ),
    ]
    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    for secret_pattern in ("Bearer ", "Authorization", "access_token", "refresh_token", "eyJ"):
        assert secret_pattern not in text
    # No error/reason text at all any more — just the product's name.
    assert "access token expired" not in text
    assert "401" not in text
    assert "1. A" in text


def test_status_message_shows_progress_when_running():
    progress = JobProgress(platform="digikala", triggered_by="telegram:1")
    progress.total = 737
    progress.processed = 37
    progress.success = 25
    progress.capped = 8
    progress.failed = 3
    progress.skipped = 1
    progress.current_product_name = "پک هدیه چکامه 10"

    text = format_status_message(progress, platform_label="Digikala")
    assert "🟢 در حال اجرا" in text
    assert "Digikala" in text
    assert "37 / 737" in text
    assert "موفق: 25" in text
    assert "پک هدیه چکامه 10" in text


def test_status_message_when_nothing_running():
    text = format_status_message(None, platform_label="Digikala")
    assert text == "⚪ در حال حاضر اجرایی فعال نیست."
    assert text == format_no_active_run_message()


def test_duplicate_run_message():
    text = format_duplicate_run_message("DigiKala")
    assert "⚠️" in text
    assert "DigiKala" in text
    assert "متوقف" in text


def test_retry_failed_confirmation_exact_output():
    text = format_retry_failed_confirmation(
        display_name="آقای اعتصامی",
        platform_label="DigiKala",
        failed_count=3,
        unchecked_count=5,
    )
    assert text == (
        "آقای اعتصامی عزیز، در وضعیت فعلی DigiKala:\n"
        "• 3 کالا ناموفق بوده‌اند\n"
        "• 5 کالا هنوز بررسی نشده‌اند\n"
        "\n"
        "آیا فقط همین کالاها بررسی شوند؟\n"
        "«بله» = فقط همین کالاها  |  «خیر» = شروع پردازش جدید برای همه کالاها"
    )


def test_retry_failed_confirmation_omits_a_zero_group():
    only_failed = format_retry_failed_confirmation(
        display_name="خانم رضایی", platform_label="Basalam", failed_count=1, unchecked_count=0
    )
    assert "1 کالا ناموفق" in only_failed
    assert "بررسی نشده" not in only_failed

    only_unchecked = format_retry_failed_confirmation(
        display_name="خانم رضایی", platform_label="Basalam", failed_count=0, unchecked_count=4
    )
    assert "4 کالا هنوز بررسی نشده" in only_unchecked
    assert "ناموفق" not in only_unchecked


def test_retry_failed_confirmation_names_who_what_and_how_many():
    text = format_retry_failed_confirmation(
        display_name="خانم رضایی", platform_label="Basalam", failed_count=2, unchecked_count=7
    )
    for piece in ("خانم رضایی", "Basalam", "2", "7"):
        assert piece in text


def test_retry_failed_scope_note_exact_output():
    text = format_retry_failed_scope_note(3, 2)
    assert text == (
        "ℹ️ این اجرا فقط شامل 5 کالا بود (3 ناموفق و 2 بررسی‌نشده)؛ "
        "سایر کالاها دوباره بررسی نشدند."
    )


def test_retry_nothing_left_message_names_the_platform():
    text = format_retry_nothing_left_message("DigiKala")
    assert "DigiKala" in text
    assert "باقی نمانده" in text


def test_stop_requested_and_stopped_messages():
    requested = format_stop_requested_message()
    assert "⏹" in requested
    assert "درخواست توقف ثبت شد" in requested

    stopped = format_stopped_message(42)
    assert "⏹ اجرا متوقف شد" in stopped
    assert "42" in stopped


def test_change_notice_exact_output():
    assert (
        format_change_notice("آقای اعتصامی", "DigiKala")
        == "🔔 آقای اعتصامی به‌روزرسانی قیمت‌های DigiKala را شروع کرد."
    )


def test_change_notice_with_persian_platform_label():
    assert (
        format_change_notice("خانم رضایی", "دیجی‌کالا")
        == "🔔 خانم رضایی به‌روزرسانی قیمت‌های دیجی‌کالا را شروع کرد."
    )


def test_change_notice_with_numeric_display_name_fallback():
    # display_name() falls back to str(chat_id) when no name is configured.
    assert (
        format_change_notice("123456789", "Basalam")
        == "🔔 123456789 به‌روزرسانی قیمت‌های Basalam را شروع کرد."
    )


def test_change_notice_names_both_who_and_what():
    text = format_change_notice("Ali", "SnappShop")
    assert "Ali" in text and "SnappShop" in text


# -- to_jalali_date --
#
# format_last_status_report_public/_admin and their tests were removed along
# with the "📊 آخرین وضعیت" text report they powered (see
# telegram_bot/handlers.py::_handle_last_status, now an Excel export via
# reporting/status_matrix.py + reporting/xlsx_export.py). to_jalali_date
# itself stays -- it's still used by status_matrix.py and by
# format_retry_failed_confirmation above.


def test_to_jalali_date_converts_utc_to_tehran_local_jalali_date():
    # 2026-09-20T10:00:00Z is 2026-09-20T13:30 in Tehran (UTC+3:30), well
    # clear of the midnight boundary, so the Jalali date is unambiguous.
    assert reporting.to_jalali_date("2026-09-20T10:00:00+00:00") == "1405/06/29"


def test_to_jalali_date_rolls_to_the_next_tehran_day_near_utc_midnight():
    # 2026-09-20T21:00:00Z is 2026-09-21T00:30 in Tehran (UTC+3:30) -> the
    # Jalali date must reflect the Tehran day, not the UTC day.
    tehran_after_midnight = reporting.to_jalali_date("2026-09-20T21:00:00+00:00")
    tehran_before_midnight = reporting.to_jalali_date("2026-09-20T20:00:00+00:00")
    assert tehran_after_midnight != tehran_before_midnight


def test_final_report_mentions_error_rounds_and_remaining_failures():
    still_failing = UpdateResult(
        product=_product("Stuck"), platform="digikala", status=OutcomeStatus.FAILED_API, transient=True
    )
    summary = RunSummary(platform="digikala", triggered_by="test", error_rounds=3)
    summary.add_result(still_failing)

    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    assert "3 دور مجدد انجام شد" in text
    assert "1 کالا همچنان خطا دارد" in text


def test_final_report_says_all_recovered_when_error_rounds_fixed_everything():
    ok = UpdateResult(product=_product("Fine"), platform="digikala", status=OutcomeStatus.SUCCESS)
    summary = RunSummary(platform="digikala", triggered_by="test", error_rounds=1)
    summary.add_result(ok)

    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    assert "1 دور مجدد انجام شد" in text
    assert "همه‌ی کالاهای خطادار موفق شدند" in text


def test_final_report_has_no_error_round_line_without_error_rounds():
    ok = UpdateResult(product=_product("Fine"), platform="digikala", status=OutcomeStatus.SUCCESS)
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.add_result(ok)

    text = format_final_digikala_report(summary, dry_run=False, platform_label="Digikala")
    assert "دور مجدد" not in text


# -- Price check ("🔍 چک و بررسی قیمت‌ها") ------------------------------------------------


def _check_entry(
    name: str,
    status: CheckStatus,
    *,
    platform: str = "digikala",
    current: int | None = None,
    target: int | None = None,
    error: str | None = None,
) -> PriceCheckEntry:
    return PriceCheckEntry(
        product=_product(name),
        platform=platform,
        status=status,
        current_price=current,
        expected_price=target,
        target_price=target,
        error_message=error,
    )


def _check_result(platform: str = "digikala", *entries: PriceCheckEntry, **kwargs) -> PlatformCheckResult:
    return PlatformCheckResult(
        platform=platform,
        entries=list(entries),
        planned=kwargs.pop("planned", len(entries)),
        **kwargs,
    )


def test_price_check_report_lists_mismatches_in_toman():
    # Prices are Rial internally; the report speaks Toman like the sheet does.
    result = _check_result(
        "digikala",
        _check_entry("Widget A", CheckStatus.MISMATCH, current=10_000_000, target=11_000_000),
    )

    text = format_price_check_report({"digikala": result})

    assert "Widget A" in text
    assert "1,000,000 تومان" in text  # what the platform sells at
    assert "شیت 1,100,000 تومان" in text  # what the sheet says
    assert "10,000,000" not in text  # never the raw Rial value
    assert "❌ کالاهای ناموفق:" in text


def test_price_check_report_only_counts_matching_products_without_listing_them():
    result = _check_result(
        "digikala",
        _check_entry("Same One", CheckStatus.MATCH, current=5_000_000, target=5_000_000),
        _check_entry("Same Two", CheckStatus.MATCH, current=6_000_000, target=6_000_000),
        _check_entry("Different", CheckStatus.MISMATCH, current=7_000_000, target=8_000_000),
    )

    text = format_price_check_report({"digikala": result})

    assert "📦 کل کالا: 3   ✅ موفق: 2   ❌ ناموفق: 1" in text
    assert "Same One" not in text
    assert "Same Two" not in text
    assert "Different" in text


def test_price_check_report_all_matching_says_everything_is_identical():
    result = _check_result(
        "digikala", _check_entry("Same", CheckStatus.MATCH, current=5_000_000, target=5_000_000)
    )

    text = format_price_check_report({"digikala": result})

    assert "✅ همه‌ی کالاها با شیت یکسان‌اند." in text
    assert "✅ قیمت همه‌ی کالاها در همه‌ی پلتفرم‌ها با شیت یکسان است." in text
    assert "بررسی با مغایرت یا خطا به پایان رسید" not in text


def test_price_check_report_hides_error_reasons_unless_include_reasons():
    result = _check_result(
        "digikala",
        _check_entry("Broken", CheckStatus.ERROR, error="HTTP 503: upstream exploded"),
    )

    without = format_price_check_report({"digikala": result})
    with_reasons = format_price_check_report({"digikala": result}, include_reasons=True)

    assert "Broken" in without  # still listed as "could not be read"
    assert "upstream exploded" not in without
    assert "upstream exploded" in with_reasons
    assert "⚠️ بررسی با مغایرت یا خطا به پایان رسید." in without


def test_price_check_report_redacts_secrets_even_for_the_admin():
    result = _check_result(
        "digikala",
        _check_entry("Broken", CheckStatus.ERROR, error="401 Bearer abc.def.ghi rejected"),
    )

    text = format_price_check_report({"digikala": result}, include_reasons=True)

    assert "abc.def.ghi" not in text
    assert "[redacted]" in text


def test_price_check_report_shows_the_dry_run_notice_only_when_dry_run():
    result = _check_result(
        "digikala", _check_entry("A", CheckStatus.MISMATCH, current=1_000_000, target=2_000_000)
    )

    dry = format_price_check_report({"digikala": result}, dry_run=True)
    live = format_price_check_report({"digikala": result}, dry_run=False)

    assert "DRY RUN" in dry
    assert dry.index("DRY RUN") < dry.index("گزارش بررسی قیمت‌ها")  # warning comes first
    assert "DRY RUN" not in live


def test_price_check_report_lists_busy_platforms_and_never_claims_all_identical():
    result = _check_result(
        "digikala", _check_entry("Same", CheckStatus.MATCH, current=5_000_000, target=5_000_000)
    )

    text = format_price_check_report({"digikala": result}, busy_platforms=["basalam"])

    assert "بررسی نشدند" in text
    assert "🔵 Basalam" in text
    # One platform was not looked at, so "everything matches" would be a lie.
    assert "قیمت همه‌ی کالاها در همه‌ی پلتفرم‌ها با شیت یکسان است" not in text
    assert "⚠️ بررسی با مغایرت یا خطا به پایان رسید." in text


def test_price_check_report_for_a_stopped_check_says_how_far_it_got():
    result = _check_result(
        "digikala",
        _check_entry("Done 1", CheckStatus.MATCH, current=5_000_000, target=5_000_000),
        _check_entry("Done 2", CheckStatus.MATCH, current=5_000_000, target=5_000_000),
        planned=5,
        cancelled=True,
    )

    text = format_price_check_report(
        {"digikala": result}, interrupted_note="⏹ بررسی توسط کاربر متوقف شد"
    )

    assert "فقط 2 از 5 کالا بررسی شد" in text
    assert "بررسی توسط کاربر متوقف شد" in text


def test_price_check_report_for_a_platform_that_could_not_be_checked():
    broken = PlatformCheckResult(platform="basalam", planned=3, platform_error="token expired")
    fine = _check_result(
        "digikala", _check_entry("Same", CheckStatus.MATCH, current=5_000_000, target=5_000_000)
    )

    public = format_price_check_report({"digikala": fine, "basalam": broken})
    admin = format_price_check_report({"digikala": fine, "basalam": broken}, include_reasons=True)

    assert "بررسی این پلتفرم انجام نشد" in public
    assert "token expired" not in public
    assert "دلیل: token expired" in admin
    assert "⚠️ بررسی با مغایرت یا خطا به پایان رسید." in public


def test_price_check_report_mentions_skipped_products():
    result = _check_result(
        "digikala",
        _check_entry("Same", CheckStatus.MATCH, current=5_000_000, target=5_000_000),
        skipped_no_target=2,
        skipped_not_green=4,
        skipped_no_display_name=3,
    )

    text = format_price_check_report({"digikala": result})

    assert "2 کالا بدون قیمت اصلی" in text
    assert "4 کالا با سلول غیرسبز" in text
    assert "3 کالا بدون «اسم جدید»" in text


def test_apply_checked_confirmation_lists_counts_per_platform_and_asks_yes_no():
    from faraz_honar.telegram_bot.reporting import format_apply_checked_confirmation

    text = format_apply_checked_confirmation(
        display_name="علی", platform_counts=[("digikala", 3), ("basalam", 2)]
    )

    assert "علی عزیز" in text
    assert "5 کالا" in text
    assert "Digikala: 3 کالا" in text and "Basalam: 2 کالا" in text
    assert "آیا می‌خواهید تغییر قیمت را روی این کالاها اعمال کنم؟" in text


def test_apply_checked_scope_note_states_the_reduced_scope():
    from faraz_honar.telegram_bot.reporting import format_apply_checked_scope_note

    assert "فقط شامل 4 کالای تغییرنکرده" in format_apply_checked_scope_note(4)


def test_price_check_report_orders_platforms_canonically():
    results = {
        "basalam": _check_result("basalam", _check_entry("B", CheckStatus.MATCH, platform="basalam")),
        "digikala": _check_result("digikala", _check_entry("D", CheckStatus.MATCH)),
    }

    text = format_price_check_report(results)

    assert text.index("🔴 Digikala —") < text.index("🔵 Basalam —")


def test_price_check_started_message_shows_estimate_and_counts():
    short = format_price_check_started_message(
        platform_counts=[("digikala", 3), ("basalam", 2)], estimate_seconds=30, busy_platforms=[]
    )
    long = format_price_check_started_message(
        platform_counts=[("digikala", 3)], estimate_seconds=300, busy_platforms=[]
    )

    assert "🔴 Digikala: 3 کالا" in short
    assert "🔵 Basalam: 2 کالا" in short
    assert "ممکن است طول بکشد" in short
    assert "کمتر از 1 دقیقه" in short
    assert "حدود 5 دقیقه" in long
    assert "هیچ قیمتی روی پلتفرم‌ها تغییر نمی‌کند" in short  # says it is read-only
    assert "⏭" not in short  # nothing was busy


def test_price_check_started_message_lists_busy_platforms():
    text = format_price_check_started_message(
        platform_counts=[("digikala", 3)], estimate_seconds=30, busy_platforms=["basalam", "tapsi_shop"]
    )

    assert "بررسی نشدند" in text
    assert "• 🔵 Basalam" in text
    assert "• 🟠 TapsiShop" in text


def test_status_message_for_a_price_check_shows_the_check_counters():
    progress = JobProgress(
        platform="digikala",
        triggered_by="test",
        kind="check",
        total=10,
        processed=6,
        success=3,
        mismatched=2,
        failed=1,
        current_product_name="Widget",
    )

    text = format_status_message(progress, platform_label="🔴 Digikala")

    assert "🔍 در حال بررسی قیمت‌ها" in text
    assert "6 / 10" in text
    assert "یکسان با شیت: 3" in text
    assert "تغییر نکرده: 2" in text
    assert "خطا در خواندن: 1" in text
    assert "Widget" in text
    # None of the update-run counters leak into a check's status view.
    assert "موفق:" not in text
    assert "محدودشده" not in text


def test_status_message_for_an_update_run_is_unchanged_by_the_check_kind():
    progress = JobProgress(platform="digikala", triggered_by="test", total=4, processed=1, success=1)

    text = format_status_message(progress, platform_label="🔴 Digikala")

    assert "🟢 در حال اجرا" in text
    assert "موفق: 1" in text
    assert "یکسان با شیت" not in text


def test_update_report_and_price_check_report_share_the_same_totals_line():
    summary = RunSummary(platform="digikala", triggered_by="test")
    summary.results = [
        UpdateResult(product=_product("A"), platform="digikala", status=OutcomeStatus.SUCCESS),
        UpdateResult(product=_product("B"), platform="digikala", status=OutcomeStatus.FAILED_API),
    ]
    update_text = format_final_digikala_report(summary, platform_label="Digikala")

    result = _check_result(
        "digikala",
        _check_entry("A", CheckStatus.MATCH, current=5_000_000, target=5_000_000),
        _check_entry("B", CheckStatus.ERROR, error="HTTP 503"),
    )
    check_text = format_price_check_report({"digikala": result})

    totals = "📦 کل کالا: 2   ✅ موفق: 1   ❌ ناموفق: 1"
    assert totals in update_text
    assert totals in check_text
    assert "❌ کالاهای ناموفق:" in update_text
    assert "❌ کالاهای ناموفق:" in check_text


def test_price_check_report_lists_mismatched_and_unreadable_products_in_one_failed_list():
    result = _check_result(
        "digikala",
        _check_entry("Different", CheckStatus.MISMATCH, current=7_000_000, target=8_000_000),
        _check_entry("Unreadable", CheckStatus.ERROR, error="HTTP 503"),
    )

    text = format_price_check_report({"digikala": result})
    section = text.split("❌ کالاهای ناموفق:")[1]

    assert "1. Different: 700,000 تومان ← شیت 800,000 تومان" in section
    assert "2. Unreadable — قیمت خوانده نشد" in section
    assert "HTTP 503" not in text  # reasons stay admin-only
