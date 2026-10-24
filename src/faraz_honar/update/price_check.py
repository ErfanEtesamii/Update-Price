"""Read-only price check: compares what every platform is selling at right now
with the sheet's price ("قیمت اصلی"), and reports which products differ.

Strictly READ-ONLY towards the platforms: it only calls
PlatformClient.resolve_variant_id / get_current_price (plus the optional
prepare/finish hooks) — never update_price — so it can never change a price.
DRY_RUN therefore has no effect on it. This module itself writes nothing; the
caller (PriceUpdateService.check_prices) records each entry's outcome in the
state DB afterwards, because the check is the most accurate picture of what
the platforms really sell at and the "📊 آخرین وضعیت" Excel must follow it.

Speed: every platform is checked in its OWN thread at the same time (the
platforms are independent APIs with independent throttles). Within one
platform the products are read one after another, because each client already
throttles its own read requests to avoid HTTP 429. Platforms that list their
whole catalog page by page (TapsiShop, WooCommerce) read it once up front via
prepare_price_check instead of searching it once per product.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from faraz_honar.core.exceptions import PlatformAuthError, VariantResolutionError
from faraz_honar.core.models import OutcomeStatus, Product
from faraz_honar.core.retry import is_retryable
from faraz_honar.platforms.base import PlatformClient

logger = logging.getLogger(__name__)

# Same attempt budget as a normal update run: only retryable errors (5xx,
# network) get a second attempt, everything else stops after one.
DEFAULT_MAX_ATTEMPTS = 2


class CheckStatus(StrEnum):
    MATCH = "match"  # platform price == sheet price (after the platform's own rounding)
    MISMATCH = "mismatch"  # platform price != sheet price -> not changed / changed by someone else
    ERROR = "error"  # the platform price could not be read


@dataclass
class PriceCheckEntry:
    product: Product
    platform: str
    status: CheckStatus
    current_price: int | None = None  # Rial, as read from the platform
    expected_price: int | None = None  # Rial, the sheet price after the platform's rounding
    target_price: int | None = None  # Rial, the raw sheet price
    variant_id: str | None = None
    error_message: str | None = None
    # Only set on ERROR entries: which FAILED_* status the failure maps to, so
    # the state DB / Excel can label it the way an update run would.
    error_status: OutcomeStatus | None = None

    @property
    def difference(self) -> int | None:
        """platform - sheet, in Rial (positive = platform is higher)."""
        if self.current_price is None or self.expected_price is None:
            return None
        return self.current_price - self.expected_price


@dataclass
class PlatformCheckResult:
    platform: str
    entries: list[PriceCheckEntry] = field(default_factory=list)
    planned: int = 0  # products this check set out to read
    skipped_no_display_name: int = 0  # has a platform reference but "اسم جدید" is empty
    skipped_no_target: int = 0  # has a platform reference but no usable sheet price
    skipped_not_green: int = 0  # platform cell is not shaded green = "not to be checked"
    cancelled: bool = False
    platform_error: str | None = None  # whole platform could not be checked
    elapsed_seconds: float = 0.0

    @property
    def matched(self) -> list[PriceCheckEntry]:
        return [e for e in self.entries if e.status is CheckStatus.MATCH]

    @property
    def mismatched(self) -> list[PriceCheckEntry]:
        return [e for e in self.entries if e.status is CheckStatus.MISMATCH]

    @property
    def errors(self) -> list[PriceCheckEntry]:
        return [e for e in self.entries if e.status is CheckStatus.ERROR]


@dataclass(frozen=True)
class CheckSelection:
    """Which products a platform's check covers, and what was left out."""

    products: tuple[Product, ...]
    skipped_no_target: int
    skipped_not_green: int
    skipped_no_display_name: int = 0


def select_products_for_check(products: list[Product], platform: str) -> CheckSelection:
    """Which products a price check reads on `platform`: ONLY those with
    "اسم جدید" filled, "قیمت اصلی" filled, a code on the platform, and that
    code's cell shaded green (Product.is_check_eligible). Everything else needs
    no check -- reading it would only cost throttled requests (and, for a
    non-green cell, produce "not found" noise). Products without a platform
    code are not applicable and are not counted at all; the rest are counted
    under the FIRST rule they fail, so each product is counted once."""
    selected: list[Product] = []
    skipped_no_display_name = 0
    skipped_no_target = 0
    skipped_not_green = 0
    for product in products:
        if product.platform_ref(platform) is None:
            continue
        if not product.display_name:
            skipped_no_display_name += 1
            continue
        if product.target_price is None:
            skipped_no_target += 1
            continue
        if not product.is_green(platform):
            skipped_not_green += 1
            continue
        selected.append(product)
    return CheckSelection(
        products=tuple(selected),
        skipped_no_target=skipped_no_target,
        skipped_not_green=skipped_not_green,
        skipped_no_display_name=skipped_no_display_name,
    )


_SECRET_RE = re.compile(r"(consumer_(?:key|secret)|access_token|token)=[^&\s'\"]+", re.IGNORECASE)


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)


def _status_for_check_error(error: Exception | None) -> OutcomeStatus:
    """Which FAILED_* status a price-read failure is recorded as."""
    if isinstance(error, PlatformAuthError):
        return OutcomeStatus.FAILED_AUTH
    if isinstance(error, VariantResolutionError):
        return OutcomeStatus.FAILED_MISSING_IDENTIFIER
    return OutcomeStatus.FAILED_API


def _toman(rial: int | None) -> str:
    return "-" if rial is None else f"{rial // 10:,}"


def _log_entry(entry: PriceCheckEntry) -> None:
    """One detailed, greppable line per product (prices in Toman, like the sheet):
    `PRICE_CHECK platform=... status=... sheet=... expected=... platform_price=... diff=...`"""
    diff = entry.difference
    logger.info(
        "PRICE_CHECK platform=%s product=%r variant=%s status=%s "
        "sheet_toman=%s expected_toman=%s platform_toman=%s diff_toman=%s%s",
        entry.platform,
        entry.product.internal_id,
        entry.variant_id or "-",
        entry.status.value,
        _toman(entry.target_price),
        _toman(entry.expected_price),
        _toman(entry.current_price),
        "-" if diff is None else f"{diff / 10:+,.0f}",
        f" error={_redact(entry.error_message)!r}"[:300]
        if entry.error_message
        else "",
    )


def check_one(
    client: PlatformClient, product: Product, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> PriceCheckEntry:
    """Reads one product's live price and compares it with the sheet's."""
    target = product.target_price
    if target is None:  # select_products_for_check never lets this through
        return PriceCheckEntry(
            product=product,
            platform=client.name,
            status=CheckStatus.ERROR,
            error_message="No target price found for this product.",
            error_status=OutcomeStatus.FAILED_INVALID_DATA,
        )
    expected = client.expected_price_for_target(target)

    variant_id: str | None = None
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            variant_id = client.resolve_variant_id(product)
            current = client.get_current_price(product, variant_id)
        except Exception as exc:  # noqa: BLE001 - recorded on the entry, never swallowed
            last_error = exc
            retryable = is_retryable(exc)
            logger.warning(
                "Price check attempt %d/%d failed for %s/%s: %s (retryable=%s)",
                attempt,
                max_attempts,
                client.name,
                product.internal_id,
                _redact(str(exc)),
                retryable,
            )
            if not retryable or attempt == max_attempts:
                break
            continue
        entry = PriceCheckEntry(
            product=product,
            platform=client.name,
            status=CheckStatus.MATCH if current == expected else CheckStatus.MISMATCH,
            current_price=current,
            expected_price=expected,
            target_price=target,
            variant_id=variant_id,
        )
        _log_entry(entry)
        return entry

    entry = PriceCheckEntry(
        product=product,
        platform=client.name,
        status=CheckStatus.ERROR,
        expected_price=expected,
        target_price=target,
        variant_id=variant_id,
        error_message=str(last_error) if last_error else "Unknown error",
        error_status=_status_for_check_error(last_error),
    )
    _log_entry(entry)
    return entry


def check_platform(
    client: PlatformClient,
    selection: CheckSelection,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_product_start: Callable[[Product], None] | None = None,
    on_entry: Callable[[PriceCheckEntry], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PlatformCheckResult:
    """Checks every product of one platform, one after another. Never raises:
    a platform that cannot be read at all comes back with `platform_error` set
    (and the other platforms' checks are unaffected)."""
    result = PlatformCheckResult(
        platform=client.name,
        planned=len(selection.products),
        skipped_no_target=selection.skipped_no_target,
        skipped_not_green=selection.skipped_not_green,
        skipped_no_display_name=selection.skipped_no_display_name,
    )
    started = time.monotonic()
    logger.info(
        "PRICE_CHECK_START platform=%s planned=%d skipped_no_display_name=%d "
        "skipped_no_target=%d skipped_not_green=%d",
        client.name,
        result.planned,
        result.skipped_no_display_name,
        result.skipped_no_target,
        result.skipped_not_green,
    )
    try:
        try:
            client.prepare_price_check()
        except Exception as exc:  # noqa: BLE001 - reported on the result
            logger.exception("Price check: preparing platform '%s' failed", client.name)
            result.platform_error = str(exc) or type(exc).__name__
            return result

        for product in selection.products:
            if should_cancel is not None and should_cancel():
                result.cancelled = True
                break
            if on_product_start is not None:
                on_product_start(product)
            entry = check_one(client, product, max_attempts=max_attempts)
            result.entries.append(entry)
            if on_entry is not None:
                on_entry(entry)
    except Exception as exc:  # noqa: BLE001 - e.g. a callback blew up; keep what was read
        logger.exception("Price check for platform '%s' failed unexpectedly", client.name)
        result.platform_error = str(exc) or type(exc).__name__
    finally:
        try:
            client.finish_price_check()
        except Exception:  # noqa: BLE001 - cleanup must never hide the result
            logger.exception("Price check: finishing platform '%s' failed", client.name)
        result.elapsed_seconds = time.monotonic() - started
        logger.info(
            "PRICE_CHECK_SUMMARY platform=%s planned=%d checked=%d match=%d mismatch=%d "
            "error=%d cancelled=%s platform_error=%s elapsed=%.1fs",
            client.name,
            result.planned,
            len(result.entries),
            len(result.matched),
            len(result.mismatched),
            len(result.errors),
            result.cancelled,
            bool(result.platform_error),
            result.elapsed_seconds,
        )
    return result
