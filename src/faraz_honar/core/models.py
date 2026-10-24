"""Core domain models, independent of any platform or data-source detail."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from faraz_honar.core.exceptions import CrossPlatformStateError


@dataclass(frozen=True)
class Product:
    """One row from the product source (Google Sheet or CSV), normalized.

    `platform_refs` holds whatever raw identifier each platform's column
    contained (a link, a numeric code, etc.) — interpreting it is the
    platform client's job, not the sheet layer's.
    """

    internal_id: str
    name: str
    reference_price: int | None  # Rial, from the sheet's price column; None if missing/unparseable
    target_price: int | None  # Rial, same target across platforms for now (spec section 2)
    platform_refs: dict[str, str] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)  # full source row, for debugging/logging
    # Platforms whose product-code cell in the source sheet is shaded purple
    # or red -- the operators' marker for "do not expect this product on this
    # platform right now". Only filled by GoogleSheetsProductSource (a CSV
    # export carries no cell colors). Used by the status report so such cells
    # are not counted as "هنوز اجرا نشده"; the update run itself ignores it.
    color_excluded_platforms: frozenset[str] = frozenset()
    # The raw "اسم جدید" cell (None when empty). `name` falls back to the
    # original "اسم" when this is empty, so it cannot tell the two apart.
    display_name: str | None = None
    # Platforms whose product-code cell in the source sheet is shaded GREEN --
    # the operators' marker for "this product is live on this platform and
    # should be price-checked". Like color_excluded_platforms it is only filled
    # by GoogleSheetsProductSource (a CSV export carries no cell colors).
    green_platforms: frozenset[str] = frozenset()

    def platform_ref(self, platform: str) -> str | None:
        value = self.platform_refs.get(platform)
        return value if value else None

    def is_color_excluded(self, platform: str) -> bool:
        return platform in self.color_excluded_platforms

    def is_green(self, platform: str) -> bool:
        return platform in self.green_platforms

    def is_check_eligible(self, platform: str) -> bool:
        """The ONE definition of "this product is part of `platform`'s price
        check": "اسم جدید" is filled, "قیمت اصلی" is filled, the platform has a
        code for it, and that code's cell is green. Anything else is not
        checked at all (and so is never offered for a price change either).
        """
        return (
            bool(self.display_name)
            and self.target_price is not None
            and self.platform_ref(platform) is not None
            and self.is_green(platform)
        )

    def is_eligible(self, platform: str) -> bool:
        """The ONE definition of "this product is part of `platform`'s run":
        "اسم جدید" is filled, "قیمت اصلی" is filled, it has a code on that
        platform AND that code's cell is shaded green.

        Shared by the price check, the update run and its live "x / total"
        count (update/service.py::_is_eligible), the retry/backlog logic and
        the status matrix / Excel report (reporting/status_matrix.py), so
        they can never disagree about how many products there are.
        """
        return self.is_check_eligible(platform)


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    CAPPED_12PCT = "capped_12pct"
    FAILED_API = "failed_api"
    FAILED_AUTH = "failed_auth"
    FAILED_INVALID_DATA = "failed_invalid_data"
    FAILED_MISSING_IDENTIFIER = "failed_missing_identifier"
    FAILED_UNEXPECTED = "failed_unexpected"
    # Recorded by the read-only price check: the platform's live price differs
    # from the sheet price (see PriceUpdateService.check_prices).
    FAILED_PRICE_MISMATCH = "failed_price_mismatch"
    SKIPPED_NO_CHANGE = "skipped_no_change"


FAILURE_STATUSES = frozenset(
    {
        OutcomeStatus.FAILED_API,
        OutcomeStatus.FAILED_AUTH,
        OutcomeStatus.FAILED_INVALID_DATA,
        OutcomeStatus.FAILED_MISSING_IDENTIFIER,
        OutcomeStatus.FAILED_UNEXPECTED,
        OutcomeStatus.FAILED_PRICE_MISMATCH,
    }
)


@dataclass
class UpdateResult:
    product: Product
    platform: str
    status: OutcomeStatus
    previous_price: int | None = None
    applied_price: int | None = None
    target_price: int | None = None
    attempts: int = 0
    error_message: str | None = None
    variant_id: str | None = None
    # True when the failure was an accidental one (429/5xx/timeout/network —
    # see core.retry.is_transient_error). In-memory only, used within a single
    # run to decide which products go into the next error round; never
    # persisted. Default keeps every existing UpdateResult(...) call valid.
    transient: bool = False

    @property
    def is_capped(self) -> bool:
        return self.status is OutcomeStatus.CAPPED_12PCT

    @property
    def is_success(self) -> bool:
        return self.status is OutcomeStatus.SUCCESS

    @property
    def is_failure(self) -> bool:
        return self.status in FAILURE_STATUSES


@dataclass
class RunSummary:
    platform: str
    triggered_by: str
    results: list[UpdateResult] = field(default_factory=list)
    # Number of EXTRA error rounds (after the first full pass) that were run
    # for this platform - 0 when none were needed or the feature is off.
    # In-memory only; used for the final Telegram report line.
    error_rounds: int = 0

    def add_result(self, result: UpdateResult) -> None:
        """The only sanctioned way to add a result to this summary.

        Every product-level outcome must be labeled with the platform it
        was actually processed for (see update/service.py::_update_one),
        and this is the one place that label is checked against the
        summary it's about to join — so a RunSummary can never end up
        holding a result that belongs to a different platform, and by
        extension a final report (built only from summary.successful/
        summary.capped/summary.failed) can never show another platform's
        products. Never caught/silenced: a mismatch here is always a bug,
        never an expected condition.
        """
        if result.platform != self.platform:
            raise CrossPlatformStateError(
                f"Refusing to add a '{result.platform}' result to the "
                f"'{self.platform}' RunSummary — results must never cross "
                "platform boundaries."
            )
        self.results.append(result)

    @property
    def successful(self) -> list[UpdateResult]:
        return [r for r in self.results if r.is_success]

    @property
    def capped(self) -> list[UpdateResult]:
        return [r for r in self.results if r.is_capped]

    @property
    def failed(self) -> list[UpdateResult]:
        return [r for r in self.results if r.is_failure]