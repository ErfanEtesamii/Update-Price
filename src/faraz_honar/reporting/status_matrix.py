"""Builds the "product x platform" status matrix for the price-status Excel
report (📊 آخرین وضعیت).

This module only computes a plain, testable data structure -- a list of rows,
each with one cell per platform -- from the current product list and the
persisted `product_state` rows. It has no dependency on openpyxl or the
Telegram bot: xlsx_export.py (phase 3) is the only consumer that turns this
into a spreadsheet, and telegram_bot/handlers.py (phase 4) is the only
consumer that wires it into the "📊 آخرین وضعیت" button.

Cell classification follows the spec's four-state table exactly:

    موفق (success)        -- a product_state row exists with status in
                              (SUCCESS, SKIPPED_NO_CHANGE). SKIPPED_NO_CHANGE
                              counts as success because it means the price
                              already matched the target -- nothing needed
                              changing.
    ناموفق (failure)       -- a product_state row exists with any other
                              status (CAPPED_12PCT or one of the FAILED_*
                              values).
    هنوز اجرا نشده        -- the product is eligible for this platform
    (not run yet)            (Product.is_eligible(platform): "اسم جدید" and
                              "قیمت اصلی" filled, a code there AND a green cell --
                              the exact rule the update run uses) but no product_state
                              row exists yet for this pair.
    غیرقابل‌اجرا           -- the product is not eligible: no reference on
    (not applicable)          this platform (it isn't sold there) or no target
                              price (the run skips it, so it will never
                              have a product_state row). Also used when the
                              reference exists but its cell in the source
                              sheet is shaded purple or red
                              (Product.color_excluded_platforms) and no
                              product_state row exists yet -- the operators'
                              marker for "not expected there right now", so
                              it must not inflate the "هنوز اجرا نشده" count.
                              A product_state row of an ELIGIBLE product always
                              wins; a leftover row of a no-longer-eligible
                              product is ignored (see _build_cell).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from faraz_honar.core.models import OutcomeStatus, Product
from faraz_honar.platform_names import PLATFORM_DISPLAY_NAMES
from faraz_honar.state.repository import ProductStateRow
from faraz_honar.telegram_bot.reporting import to_jalali_date

# Statuses that count as "the product is correctly priced on this platform
# right now" -- SKIPPED_NO_CHANGE included, per the spec note that it means
# the price was already at target and needed no update.
_SUCCESS_STATUSES = frozenset(
    {OutcomeStatus.SUCCESS.value, OutcomeStatus.SKIPPED_NO_CHANGE.value}
)


class CellStatus(StrEnum):
    """The four possible states of one (product, platform) cell -- matches
    the spec's table exactly, in the same order.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    NOT_RUN_YET = "not_run_yet"
    NOT_APPLICABLE = "not_applicable"


# Symbol/color per CellStatus, kept together so xlsx_export.py never has to
# re-derive them -- it only maps `color` onto an actual openpyxl PatternFill.
_SYMBOLS: dict[CellStatus, str] = {
    CellStatus.SUCCESS: "✅",
    CellStatus.FAILURE: "❌",
    CellStatus.NOT_RUN_YET: "⏳",
    CellStatus.NOT_APPLICABLE: "➖",
}

_COLORS: dict[CellStatus, str] = {
    CellStatus.SUCCESS: "green",
    CellStatus.FAILURE: "red",
    CellStatus.NOT_RUN_YET: "yellow",
    CellStatus.NOT_APPLICABLE: "gray",
}


@dataclass(frozen=True)
class StatusCell:
    """One cell of the matrix: one product on one platform.

    `state` carries the underlying ProductStateRow when one exists (success
    or failure), so xlsx_export.py can pull `last_error`/`status` for the
    admin comment and the "جزئیات کالاهای ناموفق" sheet without status_matrix
    needing to know anything about _NOT_CORRECTED_REASON_LABELS or comments
    itself. It's None for the two states that have no product_state row
    (not-run-yet, not-applicable).
    """

    platform: str
    status: CellStatus
    symbol: str
    color: str
    jalali_date: str | None
    state: ProductStateRow | None

    @property
    def is_failure(self) -> bool:
        return self.status is CellStatus.FAILURE


@dataclass(frozen=True)
class StatusMatrixRow:
    """One product's row across every platform in the matrix."""

    product: Product
    cells: dict[str, StatusCell] = field(default_factory=dict)

    def cell(self, platform: str) -> StatusCell:
        return self.cells[platform]


@dataclass(frozen=True)
class StatusMatrix:
    """The full report: column order plus one row per product."""

    platforms: list[str]
    rows: list[StatusMatrixRow]


def build_status_matrix(
    products: list[Product],
    states: list[ProductStateRow],
    platforms: list[str] | None = None,
) -> StatusMatrix:
    """Builds the matrix described above.

    `products` should come from `product_source.fetch_products()` and
    `states` from `state_repository.list_all_product_states()` -- both
    unfiltered, since filtering is exactly what this function does per cell.
    `platforms` sets the column order; defaults to
    PLATFORM_DISPLAY_NAMES's key order, which is also the order every other
    platform-listing surface in the app (keyboards, reports) already uses.
    """
    resolved_platforms = list(platforms) if platforms is not None else list(PLATFORM_DISPLAY_NAMES)

    states_by_key: dict[tuple[str, str], ProductStateRow] = {
        (row.product_id, row.platform): row for row in states
    }

    rows: list[StatusMatrixRow] = []
    for product in products:
        cells: dict[str, StatusCell] = {}
        for platform in resolved_platforms:
            cells[platform] = _build_cell(product, platform, states_by_key)
        rows.append(StatusMatrixRow(product=product, cells=cells))

    return StatusMatrix(platforms=resolved_platforms, rows=rows)


def _build_cell(
    product: Product,
    platform: str,
    states_by_key: dict[tuple[str, str], ProductStateRow],
) -> StatusCell:
    state = states_by_key.get((product.internal_id, platform))

    # A leftover product_state row from an earlier run must not count when the
    # product is no longer part of this platform's run (reference or target
    # price removed from the sheet since). The update run never touches such
    # a product again, so its stale failure would otherwise stay in the
    # report forever and make the failed count disagree with the run's own
    # report/status. (Color-excluded products are still run, so they are not
    # affected by this rule.)
    if state is not None and not product.is_eligible(platform):
        state = None

    if state is not None:
        cell_status = CellStatus.SUCCESS if state.status in _SUCCESS_STATUSES else CellStatus.FAILURE
        return StatusCell(
            platform=platform,
            status=cell_status,
            symbol=_SYMBOLS[cell_status],
            color=_COLORS[cell_status],
            jalali_date=to_jalali_date(state.updated_at),
            state=state,
        )

    if product.is_eligible(platform) and not product.is_color_excluded(platform):
        cell_status = CellStatus.NOT_RUN_YET
    else:
        cell_status = CellStatus.NOT_APPLICABLE
    return StatusCell(
        platform=platform,
        status=cell_status,
        symbol=_SYMBOLS[cell_status],
        color=_COLORS[cell_status],
        jalali_date=None,
        state=None,
    )


__all__ = ["CellStatus", "StatusCell", "StatusMatrixRow", "StatusMatrix", "build_status_matrix"]