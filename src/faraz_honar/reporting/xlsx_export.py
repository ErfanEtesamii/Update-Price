"""Turns a StatusMatrix (reporting/status_matrix.py) into the three-sheet
Excel workbook sent by the "📊 آخرین وضعیت" button.

All Persian labels, reason text and redaction reuse the exact helpers
telegram_bot/reporting.py already uses for the live-run reports
(_NOT_CORRECTED_REASON_LABELS, _not_corrected_reason, _redact, _truncate) so
a product's failure reason reads identically whether it comes from a fresh
run's Telegram message or from this Excel export -- and so credential-like
error text is redacted here exactly as it already is there. One known gap
inherited from reusing that dict as-is (per spec): it has no entry for
CAPPED_12PCT (that status gets its own "⚠️ محدودشده..." section in the
Telegram reports instead of a "not corrected" reason), so a CAPPED_12PCT
failure cell here falls back to the dict's default "دلیل نامشخص" label.
Extending the shared dict was out of scope for this change since it would
also affect those existing Telegram reports.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from faraz_honar.platform_names import platform_display_name
from faraz_honar.reporting.status_matrix import CellStatus, StatusMatrix
from faraz_honar.telegram_bot.reporting import (
    _NOT_CORRECTED_REASON_LABELS,
    _not_corrected_reason,
    _redact,
    _truncate,
)

_SHEET_MATRIX_TITLE = "وضعیت قیمت‌ها"
_SHEET_SUMMARY_TITLE = "خلاصه"
_SHEET_FAILURES_TITLE = "جزئیات کالاهای ناموفق"

# ARGB hex per StatusCell.color ("green"/"red"/"yellow"/"gray") -- the same
# soft palette Excel's own conditional-formatting presets use, so the sheet
# reads the same way a manually-formatted Google Sheet would.
_FILL_COLORS: dict[str, PatternFill] = {
    "green": PatternFill(start_color="FFC6EFCE", end_color="FFC6EFCE", fill_type="solid"),
    "red": PatternFill(start_color="FFFFC7CE", end_color="FFFFC7CE", fill_type="solid"),
    "yellow": PatternFill(start_color="FFFFEB9C", end_color="FFFFEB9C", fill_type="solid"),
    "gray": PatternFill(start_color="FFD9D9D9", end_color="FFD9D9D9", fill_type="solid"),
}

_HEADER_FONT = Font(bold=True)
_CENTERED = Alignment(horizontal="center", vertical="center")
_COMMENT_AUTHOR = "Faraz Honar Bot"

_MIN_COLUMN_WIDTH = 8
_MAX_COLUMN_WIDTH = 40


def export_status_matrix_xlsx(
    matrix: StatusMatrix, output_path: Path, *, include_reasons: bool
) -> Path:
    """Writes the three-sheet workbook described in the module docstring to
    `output_path` (creating parent directories as needed) and returns it
    unchanged, so callers can chain it straight into send_document.

    `include_reasons` is the one thing that differs between the public and
    admin versions: it adds a per-cell comment on sheet 1 and four extra
    columns on sheet 3. Sheet 2 (خلاصه) is always identical -- it never
    contains per-product failure reasons to begin with.
    """
    workbook = Workbook()

    matrix_sheet = workbook.active
    matrix_sheet.title = _SHEET_MATRIX_TITLE
    _write_matrix_sheet(matrix_sheet, matrix, include_reasons=include_reasons)

    summary_sheet = workbook.create_sheet(_SHEET_SUMMARY_TITLE)
    _write_summary_sheet(summary_sheet, matrix)

    failures_sheet = workbook.create_sheet(_SHEET_FAILURES_TITLE)
    _write_failures_sheet(failures_sheet, matrix, include_reasons=include_reasons)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return output_path


# -- Sheet 1: وضعیت قیمت‌ها --------------------------------------------------


def _write_matrix_sheet(ws: Worksheet, matrix: StatusMatrix, *, include_reasons: bool) -> None:
    ws.sheet_view.rightToLeft = True

    headers = ["ردیف", "نام کالا", *(platform_display_name(p) for p in matrix.platforms)]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HEADER_FONT

    for row_index, row in enumerate(matrix.rows, start=1):
        values = [row_index, row.product.name]
        for platform in matrix.platforms:
            status_cell = row.cell(platform)
            text = f"{status_cell.symbol} {status_cell.jalali_date}" if status_cell.jalali_date else status_cell.symbol
            values.append(text)
        ws.append(values)

    first_platform_col = 3  # A=ردیف, B=نام کالا, C.. = platforms
    for sheet_row, row in enumerate(matrix.rows, start=2):
        for col_offset, platform in enumerate(matrix.platforms):
            status_cell = row.cell(platform)
            cell = ws.cell(row=sheet_row, column=first_platform_col + col_offset)
            cell.fill = _FILL_COLORS[status_cell.color]
            cell.alignment = _CENTERED
            if include_reasons and status_cell.is_failure and status_cell.state is not None:
                comment_text = _not_corrected_reason(
                    status=status_cell.state.status, error_message=status_cell.state.last_error
                )
                cell.comment = Comment(comment_text, _COMMENT_AUTHOR)

    ws.freeze_panes = "C2"
    _autofit_columns(ws)


# -- Sheet 2: خلاصه -----------------------------------------------------------


def _write_summary_sheet(ws: Worksheet, matrix: StatusMatrix) -> None:
    ws.sheet_view.rightToLeft = True

    headers = ["پلتفرم", "تعداد کل واجد شرایط", "موفق ✅", "ناموفق ❌", "هنوز اجرا نشده ⏳", "درصد موفقیت"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HEADER_FONT

    total_eligible = total_success = total_failure = total_not_run = 0
    for platform in matrix.platforms:
        success, failure, not_run = _counts_for_platform(matrix, platform)
        eligible = success + failure + not_run  # excludes ➖ (not sold there at all)
        ws.append(
            [
                platform_display_name(platform),
                eligible,
                success,
                failure,
                not_run,
                _success_rate(success, eligible),
            ]
        )
        total_eligible += eligible
        total_success += success
        total_failure += failure
        total_not_run += not_run

    ws.append(
        [
            "جمع کل",
            total_eligible,
            total_success,
            total_failure,
            total_not_run,
            _success_rate(total_success, total_eligible),
        ]
    )
    for cell in ws[ws.max_row]:
        cell.font = _HEADER_FONT

    _autofit_columns(ws)


def _counts_for_platform(matrix: StatusMatrix, platform: str) -> tuple[int, int, int]:
    success = failure = not_run = 0
    for row in matrix.rows:
        status = row.cell(platform).status
        if status is CellStatus.SUCCESS:
            success += 1
        elif status is CellStatus.FAILURE:
            failure += 1
        elif status is CellStatus.NOT_RUN_YET:
            not_run += 1
        # CellStatus.NOT_APPLICABLE is intentionally not counted anywhere --
        # a product not sold on this platform was never "واجد شرایط" for it.
    return success, failure, not_run


def _success_rate(success: int, eligible: int) -> str:
    return f"{(success / eligible * 100):.1f}%" if eligible else "—"


# -- Sheet 3: جزئیات کالاهای ناموفق ------------------------------------------


def _write_failures_sheet(ws: Worksheet, matrix: StatusMatrix, *, include_reasons: bool) -> None:
    ws.sheet_view.rightToLeft = True

    headers = ["ردیف", "نام کالا", "پلتفرم", "تاریخ آخرین تلاش"]
    if include_reasons:
        headers += ["وضعیت دقیق", "قیمت مرجع", "قیمت هدف", "پیام خطا"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HEADER_FONT

    row_number = 0
    for row in matrix.rows:
        for platform in matrix.platforms:
            status_cell = row.cell(platform)
            if not status_cell.is_failure:
                continue
            row_number += 1
            state = status_cell.state
            values = [
                row_number,
                row.product.name,
                platform_display_name(platform),
                status_cell.jalali_date or "-",
            ]
            if include_reasons:
                label = _NOT_CORRECTED_REASON_LABELS.get(state.status, "دلیل نامشخص") if state else "دلیل نامشخص"
                error_text = _truncate(_redact(state.last_error)) if state and state.last_error else ""
                values += [
                    label,
                    _fmt_price(state.reference_price if state else None),
                    _fmt_price(state.target_price if state else None),
                    error_text,
                ]
            ws.append(values)

    ws.freeze_panes = "A2"
    _autofit_columns(ws)


def _fmt_price(value: int | None) -> str:
    return f"{value:,}" if value is not None else "?"


# -- Shared helpers ------------------------------------------------------------


def _autofit_columns(ws: Worksheet) -> None:
    """openpyxl has no built-in auto-fit -- widths are computed here from the
    longest rendered value per column (headers included) and clamped to a
    sane range so one long comment/name can't blow out the whole sheet.
    """
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            widths[cell.column] = max(widths.get(cell.column, 0), len(str(cell.value)))
    for col_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = max(
            _MIN_COLUMN_WIDTH, min(width + 2, _MAX_COLUMN_WIDTH)
        )


__all__ = ["export_status_matrix_xlsx"]
