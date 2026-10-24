"""Orchestrates one price-update run: sheet -> pricing -> platform -> state.

This is the one place that wires everything together (spec section 3's
numbered flow). It never talks to a platform's HTTP API directly — only
through the PlatformClient interface — and never decides the 12% logic
itself — only through core.pricing. That separation is what lets a second
platform, or a different sheet backend, be added without touching this file.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from faraz_honar.core.exceptions import (
    ConfigurationError,
    DigikalaReferencePriceRejected,
    PlatformAPIError,
    PlatformAuthError,
    ProductDataError,
    VariantResolutionError,
)
from faraz_honar.core.models import (
    FAILURE_STATUSES,
    OutcomeStatus,
    Product,
    RunSummary,
    UpdateResult,
)
from faraz_honar.core.pricing import CAPPED_PLATFORMS, compute_price_to_apply
from faraz_honar.core.retry import is_retryable, is_transient_error
from faraz_honar.platforms.base import PlatformClient
from faraz_honar.sheets.base import ProductSource
from faraz_honar.update.price_check import (
    CheckSelection,
    PlatformCheckResult,
    CheckStatus,
    PriceCheckEntry,
    check_platform,
    select_products_for_check,
)
from faraz_honar.state.repository import StateRepository

logger = logging.getLogger(__name__)


def _is_eligible(product: Product, platform: str) -> bool:
    """A product must have both a platform reference (e.g. the sheet's
    "دیجی کالا اصلی" column for the "digikala" platform) and a valid target price before
    any lookup is attempted. Filtering with this at the product-list level —
    before the per-product loop even starts — means an ineligible row never
    reaches the platform client at all, rather than merely avoiding the API
    call once it's already inside _update_one. Used by both run() and
    recheck_capped_products() so a product that lost its reference/target
    price between runs is skipped the same way in both places.
    """
    return product.is_eligible(platform)


def _log_products_skipped_for_missing_target_price(products: list[Product], platform: str) -> None:
    """Rows that have a reference for `platform` but no usable target price
    ("قیمت اصلی" empty or unparseable) are filtered out by _is_eligible BEFORE
    any lookup, so they never produce a result, an error or a report line —
    their price just silently never changes. Make that visible in the log."""
    skipped = [p for p in products if p.platform_ref(platform) and p.target_price is None]
    if not skipped:
        return
    sample = ", ".join(p.internal_id for p in skipped[:50])
    more = f" (+{len(skipped) - 50} more)" if len(skipped) > 50 else ""
    logger.warning(
        "%s: %d product(s) have a %s reference but NO usable target price (target-price "
        "column empty or unparseable) and were SKIPPED — their price will not change: %s%s",
        platform,
        len(skipped),
        platform,
        sample,
        more,
    )


# "Not corrected" for the manual retry question means anything that did NOT
# land on the sheet's target price on its latest attempt: every FAILED_*
# status, plus CAPPED_12PCT (its applied price is still short of the target).
RETRY_ELIGIBLE_STATUSES = frozenset(
    {status.value for status in FAILURE_STATUSES} | {OutcomeStatus.CAPPED_12PCT.value}
)


@dataclass(frozen=True)
class PlatformBacklog:
    """What is still outstanding on one platform, judged from the CURRENT
    sheet plus the latest persisted state of every product — not from any
    single past run.

    - failed_ids: eligible products whose latest recorded outcome is FAILED_*
      or CAPPED_12PCT.
    - unchecked_ids: eligible products with no recorded outcome at all on
      this platform (never processed, e.g. they were skipped by an earlier
      run that only covered a subset, or were added to the sheet later).
    - eligible_total: every eligible product on this platform.
    """

    failed_ids: tuple[str, ...]
    unchecked_ids: tuple[str, ...]
    retry_ids: tuple[str, ...]  # failed + unchecked, in sheet order
    eligible_total: int

    @property
    def is_empty(self) -> bool:
        return not self.retry_ids

    @property
    def has_any_recorded_state(self) -> bool:
        """True when at least one eligible product already has an outcome —
        i.e. this is not a platform that has simply never been run."""
        return self.eligible_total - len(self.unchecked_ids) > 0


def compute_platform_backlog(
    products: list[Product], states: list, platform: str
) -> PlatformBacklog:
    """Classifies every eligible product for `platform` as failed, unchecked
    or done. `states` is StateRepository.list_all_product_states() (any
    platform; rows for other platforms and for products that are no longer
    eligible are ignored)."""
    status_by_product = {s.product_id: s.status for s in states if s.platform == platform}
    failed: list[str] = []
    unchecked: list[str] = []
    retry: list[str] = []
    eligible_total = 0
    for product in products:
        if not _is_eligible(product, platform):
            continue
        eligible_total += 1
        status = status_by_product.get(product.internal_id)
        if status is None:
            unchecked.append(product.internal_id)
            retry.append(product.internal_id)
        elif status in RETRY_ELIGIBLE_STATUSES:
            failed.append(product.internal_id)
            retry.append(product.internal_id)
    return PlatformBacklog(
        failed_ids=tuple(failed),
        unchecked_ids=tuple(unchecked),
        retry_ids=tuple(retry),
        eligible_total=eligible_total,
    )


class PriceUpdateService:
    def __init__(
        self,
        *,
        product_source: ProductSource,
        platform_registry: dict[str, PlatformClient],
        state_repository: StateRepository,
        max_increase_pct: float = 0.12,
        max_retry_attempts: int = 2,
        error_rounds_enabled: bool = True,
        error_rounds_cooldown_seconds: float = 60.0,
    ):
        self._product_source = product_source
        self._platform_registry = platform_registry
        self._state_repository = state_repository
        self._max_increase_pct = max_increase_pct
        self._max_retry_attempts = max_retry_attempts
        self._error_rounds_enabled = error_rounds_enabled
        self._error_rounds_cooldown_seconds = max(0.0, error_rounds_cooldown_seconds)

    @property
    def available_platforms(self) -> list[str]:
        return list(self._platform_registry.keys())

    @property
    def state_repository(self) -> StateRepository:
        """Exposes the repository read-only so Telegram handlers (e.g. the
        "📊 آخرین وضعیت" button) can read persisted run history directly,
        without a separate bot_data key or touching main.py/bot.py wiring.
        """
        return self._state_repository

    @property
    def product_source(self) -> ProductSource:
        """Exposes the product source read-only, same rationale and same
        style as state_repository above -- the price-status Excel report
        (reporting/status_matrix.py) needs the current product list
        alongside persisted state, without touching main.py/bot.py wiring.
        """
        return self._product_source

    # -- Entry points ----------------------------------------------------------
    def run(
        self,
        platform_names: list[str],
        *,
        triggered_by: str,
        on_products_loaded: Callable[[int], None] | None = None,
        on_product_start: Callable[[Product], None] | None = None,
        on_price_resolved: Callable[[Product, str, int], None] | None = None,
        on_result: Callable[[UpdateResult], None] | None = None,
        on_round_start: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, RunSummary]:
        """Full run: every ELIGIBLE product in the sheet (has both a
        platform reference and a target price — see _is_eligible), for each
        requested platform. This is what "start the update process" from
        Telegram triggers (spec sections 3 and 11).

        Products without a platform reference or without a target price are
        filtered out here, before any platform lookup — they never reach
        _update_one, never make a DigiKala request, and never appear in the
        returned RunSummary. This is the one place that filtering happens;
        _update_one's own target_price/platform_ref checks stay in place as
        a defensive fallback for any other caller, but in normal operation
        they'll never trigger since ineligible products never reach them.

        The optional `on_*`/`should_cancel` hooks exist solely so a caller
        (the Telegram control panel) can observe progress and request
        graceful cancellation between products, without this service knowing
        anything about Telegram. They default to None (no-op) for every
        other caller — recheck_capped_products() never passes them, so the
        hourly recheck's behavior is completely unaffected.
        """
        products = self._product_source.fetch_products()
        summaries: dict[str, RunSummary] = {}
        for platform_name in platform_names:
            if should_cancel is not None and should_cancel():
                break
            client = self._require_client(platform_name)
            eligible_products = [p for p in products if _is_eligible(p, platform_name)]
            _log_products_skipped_for_missing_target_price(products, platform_name)
            if on_products_loaded is not None:
                on_products_loaded(len(eligible_products))
            summary = self._run_products(
                client,
                eligible_products,
                triggered_by=triggered_by,
                on_product_start=on_product_start,
                on_price_resolved=on_price_resolved,
                on_result=on_result,
                on_round_start=on_round_start,
                should_cancel=should_cancel,
            )
            summaries[platform_name] = summary
        return summaries

    def recheck_capped_products(
        self, platform_names: list[str], *, triggered_by: str = "scheduler:hourly"
    ) -> dict[str, RunSummary]:
        """Re-attempt only products still capped by the 12% rule (spec
        section 8). Called by the hourly scheduler job.
        """
        summaries: dict[str, RunSummary] = {}
        for platform_name in platform_names:
            client = self._require_client(platform_name)
            capped_rows = self._state_repository.list_capped_products(platform_name)
            if not capped_rows:
                continue

            products_by_id = {p.internal_id: p for p in self._product_source.fetch_products()}
            capped_products = []
            for row in capped_rows:
                product = products_by_id.get(row.product_id)
                if product is None:
                    logger.warning(
                        "Capped product '%s' (%s) no longer found in the sheet; skipping recheck",
                        row.product_id,
                        platform_name,
                    )
                    continue
                if not _is_eligible(product, platform_name):
                    logger.info(
                        "Capped product '%s' (%s) no longer has a platform reference/target "
                        "price in the sheet; skipping recheck",
                        row.product_id,
                        platform_name,
                    )
                    continue
                capped_products.append(product)

            summary = self._run_products(client, capped_products, triggered_by=triggered_by)
            summaries[platform_name] = summary
        return summaries

    def rerun_specific_products(
        self,
        platform: str,
        product_ids: list[str],
        *,
        triggered_by: str,
        on_products_loaded: Callable[[int], None] | None = None,
        on_product_start: Callable[[Product], None] | None = None,
        on_price_resolved: Callable[[Product, str, int], None] | None = None,
        on_result: Callable[[UpdateResult], None] | None = None,
        on_round_start: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        require_check_eligible: bool = False,
    ) -> RunSummary:
        """Re-attempt only the given `product_ids` for one platform — used by
        the Telegram "retry failed only" confirmation (spec: the products
        that ended a previous run as FAILED_* or CAPPED_12PCT, never
        SUCCESS/SKIPPED_NO_CHANGE, since those already reached the target or
        needed no change). Which statuses count is decided by the caller
        (telegram_bot/handlers.py::_RETRY_ELIGIBLE_STATUSES) — this method
        itself doesn't care why an id was picked, it just re-attempts it.

        Mirrors recheck_capped_products' shape (resolve ids against the
        current sheet, drop anything gone or no longer eligible) but takes an
        explicit id list instead of reading state_repository.list_capped_products,
        and — like run() — exposes the on_*/should_cancel hooks so a Telegram-
        triggered retry behaves the same as a normal manual run.

        `require_check_eligible=True` (used when applying the prices of a
        price check) additionally drops any product that no longer passes the
        price-check rules in the CURRENT sheet (Product.is_check_eligible:
        "اسم جدید" + "قیمت اصلی" filled, platform cell green), so a row edited
        between the check and the confirmation is never changed by mistake.

        Reuses _run_products, so this always creates one new, independent run
        in update_runs/run_product_results; nothing about the run(s) being
        retried is read, modified, or referenced here.
        """
        client = self._require_client(platform)
        products_by_id = {p.internal_id: p for p in self._product_source.fetch_products()}
        selected: list[Product] = []
        for product_id in product_ids:
            product = products_by_id.get(product_id)
            if product is None:
                logger.warning(
                    "Product '%s' (%s) no longer found in the sheet; skipping retry",
                    product_id,
                    platform,
                )
                continue
            if not _is_eligible(product, platform):
                logger.info(
                    "Product '%s' (%s) no longer eligible; skipping retry",
                    product_id,
                    platform,
                )
                continue
            if require_check_eligible and not product.is_check_eligible(platform):
                logger.info(
                    "Product '%s' (%s) no longer passes the price-check rules "
                    "(اسم جدید / قیمت اصلی / green cell); skipping",
                    product_id,
                    platform,
                )
                continue
            selected.append(product)

        if on_products_loaded is not None:
            on_products_loaded(len(selected))

        return self._run_products(
            client,
            selected,
            triggered_by=triggered_by,
            on_product_start=on_product_start,
            on_price_resolved=on_price_resolved,
            on_result=on_result,
            on_round_start=on_round_start,
            should_cancel=should_cancel,
        )

    # -- Read-only price check -------------------------------------------------
    def plan_price_check(
        self, platform_names: list[str], products: list[Product]
    ) -> dict[str, CheckSelection]:
        """Which products each platform's price check would read. Platforms
        with nothing to check are left out. Pure bookkeeping, no requests."""
        selections: dict[str, CheckSelection] = {}
        for platform_name in platform_names:
            self._require_client(platform_name)
            selection = select_products_for_check(products, platform_name)
            if selection.products:
                selections[platform_name] = selection
        return selections

    def estimate_price_check_seconds(self, selections: dict[str, CheckSelection]) -> float:
        """Rough upper bound for a whole check. The platforms run at the same
        time, so the slowest one decides."""
        if not selections:
            return 0.0
        return max(
            self._require_client(name).estimate_check_seconds(len(selection.products))
            for name, selection in selections.items()
        )

    def check_prices(
        self,
        selections: dict[str, CheckSelection],
        *,
        on_product_start: Callable[[str, Product], None] | None = None,
        on_entry: Callable[[PriceCheckEntry], None] | None = None,
        should_cancel: Callable[[str], bool] | None = None,
    ) -> dict[str, PlatformCheckResult]:
        """READ-ONLY comparison of every platform's live price with the sheet
        price (see update/price_check.py). All platforms are checked at the same
        time, one thread each; a failure on one platform never affects another.
        Never writes a price to a platform. It DOES record every checked
        product's outcome in product_state (see _record_check_results), so the
        "📊 آخرین وضعیت" Excel always agrees with the check report.

        `on_product_start(platform, product)`, `on_entry(entry)` and
        `should_cancel(platform)` exist so the Telegram control panel can show
        live progress and stop a check; each platform only ever calls them from
        its own thread, for its own products.
        """
        if not selections:
            return {}

        def _run(platform_name: str) -> PlatformCheckResult:
            client = self._require_client(platform_name)
            return check_platform(
                client,
                selections[platform_name],
                on_product_start=(
                    (lambda product, p=platform_name: on_product_start(p, product))
                    if on_product_start is not None
                    else None
                ),
                on_entry=on_entry,
                should_cancel=(
                    (lambda p=platform_name: should_cancel(p)) if should_cancel is not None else None
                ),
            )

        names = list(selections)
        with ThreadPoolExecutor(max_workers=len(names), thread_name_prefix="price-check") as pool:
            futures = {name: pool.submit(_run, name) for name in names}
            results = {name: future.result() for name, future in futures.items()}
        self._record_check_results(results)
        return results

    def _record_check_results(self, results: dict[str, PlatformCheckResult]) -> None:
        """Makes product_state (and so the Excel "آخرین وضعیت") follow the check.

        The check reads the real live price, so it is the accurate picture; a
        previous update run only knows the platform ACCEPTED the request.
        Mapping, per checked product:
          - price equals the sheet's  -> SKIPPED_NO_CHANGE (counts as success)
          - price differs              -> FAILED_PRICE_MISMATCH, except a product
                                          already CAPPED_12PCT stays capped (it
                                          still needs the platform ticket)
          - price could not be read    -> the matching FAILED_* status
        When several sheet rows share one name (product_state is keyed by
        name + platform) the worst outcome wins, so a failure is never hidden.
        A platform that could not be checked at all has no entries and is left
        untouched. Update-run history (update_runs/run_product_results) is never
        touched. A storage problem is logged and never breaks the check report.
        """
        for platform_name, result in results.items():
            chosen: dict[str, PriceCheckEntry] = {}
            for entry in result.entries:
                key = entry.product.internal_id
                current = chosen.get(key)
                if current is None or (
                    current.status is CheckStatus.MATCH and entry.status is not CheckStatus.MATCH
                ):
                    chosen[key] = entry
            for entry in chosen.values():
                try:
                    self._state_repository.record_result(self._result_from_check(entry))
                except Exception:  # noqa: BLE001 - the check report must still go out
                    logger.exception(
                        "Could not record the price-check result of %s/%s in the state DB",
                        platform_name,
                        entry.product.internal_id,
                    )

    def _result_from_check(self, entry: PriceCheckEntry) -> UpdateResult:
        common = dict(
            product=entry.product,
            platform=entry.platform,
            previous_price=entry.current_price,
            applied_price=entry.current_price,
            target_price=entry.target_price,
            variant_id=entry.variant_id,
        )
        if entry.status is CheckStatus.MATCH:
            return UpdateResult(status=OutcomeStatus.SKIPPED_NO_CHANGE, **common)
        if entry.status is CheckStatus.MISMATCH:
            state = self._state_repository.get_product_state(
                entry.product.internal_id, entry.platform
            )
            if state is not None and state.status == OutcomeStatus.CAPPED_12PCT.value:
                return UpdateResult(status=OutcomeStatus.CAPPED_12PCT, **common)
            current = f"{entry.current_price // 10:,}" if entry.current_price is not None else "?"
            target = f"{entry.target_price // 10:,}" if entry.target_price is not None else "?"
            return UpdateResult(
                status=OutcomeStatus.FAILED_PRICE_MISMATCH,
                error_message=(
                    f"Price check: the platform sells at {current} Toman but the sheet "
                    f"says {target} Toman."
                ),
                **common,
            )
        return UpdateResult(
            status=entry.error_status or OutcomeStatus.FAILED_API,
            error_message=entry.error_message or "Price could not be read.",
            **{**common, "previous_price": None, "applied_price": None},
        )

    def _require_client(self, platform_name: str) -> PlatformClient:
        client = self._platform_registry.get(platform_name)
        if client is None:
            available = ", ".join(self.available_platforms) or "(none registered)"
            raise ConfigurationError(f"Unknown platform '{platform_name}'. Available: {available}")
        return client

    # -- Internals -------------------------------------------------------------
    def _run_products(
        self,
        client: PlatformClient,
        products: list[Product],
        *,
        triggered_by: str,
        on_product_start: Callable[[Product], None] | None = None,
        on_price_resolved: Callable[[Product, str, int], None] | None = None,
        on_result: Callable[[UpdateResult], None] | None = None,
        on_round_start: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> RunSummary:
        """Processes `products` for one platform, then re-runs the ones that
        failed only because of a transient error ("error rounds").

        Round 1 is exactly the old single pass over every product. After it,
        the products whose latest result is a transient failure
        (UpdateResult.transient, see core.retry.is_transient_error) are
        collected (E0). If E0 == 0 we are done; otherwise, after a cooldown,
        ONLY those products are processed again and the still-transient count
        (E1) is taken. If it went down, the next round runs on just those
        E1 products, and so on. As soon as the count does not go down (or
        reaches 0, or the user cancels) we stop; whatever is still failing
        stays in the report with its failure status. There is deliberately no
        cap on the number of rounds: each round only re-processes the previous
        round's failures, so the error count can never grow, and a round that
        does not shrink it ends the loop - the algorithm always terminates.

        Everything runs under ONE update_runs row. product_state is updated
        after every attempt (so it always holds the latest outcome), while
        run_product_results and the RunSummary get exactly one entry per
        product - its FINAL outcome - written in the `finally` below, so a
        cancel or crash still records what was reached.

        With error_rounds_enabled=False this is a single pass, as before.
        """
        summary = RunSummary(platform=client.name, triggered_by=triggered_by)
        started_at = datetime.now(UTC)
        # Created up front, before the loop, so a run that gets stopped
        # (should_cancel) or crashes partway through is still attached to a
        # real run_id — the "last status" report can then show it honestly as
        # incomplete instead of it having no record at all.
        run_id = self._state_repository.start_run(
            platform=client.name, triggered_by=triggered_by, started_at=started_at
        )

        # Final outcome per product. Keyed by the product's position in
        # `products` rather than by internal_id: internal_id is the sheet's
        # name cell and nothing guarantees it is unique, so two rows with the
        # same name must keep their own result (as they did before rounds
        # existed). Insertion order == sheet order, and a retry replaces the
        # value in place, so the order never changes.
        final_results: dict[int, UpdateResult] = {}
        rounds_done = 0  # extra rounds after the first
        try:
            pending: list[int] = list(range(len(products)))
            round_no = 1
            previous_errors: int | None = None
            stop_reason = "no_errors"
            while True:
                if on_round_start is not None:
                    on_round_start(round_no, len(pending))
                cancelled = self._process_round(
                    client,
                    products,
                    pending,
                    final_results,
                    on_product_start=on_product_start,
                    on_price_resolved=on_price_resolved,
                    on_result=on_result,
                    should_cancel=should_cancel,
                )
                if round_no > 1:
                    rounds_done += 1
                if cancelled:
                    stop_reason = "cancelled"
                    break
                if not self._error_rounds_enabled:
                    stop_reason = "disabled"
                    break

                errors = [i for i in pending if _is_transient_failure(final_results.get(i))]
                if round_no == 1:
                    logger.info(
                        "platform=%s round=1 processed=%d → errors=%d",
                        client.name,
                        len(pending),
                        len(errors),
                    )
                else:
                    logger.info(
                        "platform=%s round=%d retrying=%d → errors=%d",
                        client.name,
                        round_no,
                        len(pending),
                        len(errors),
                    )
                if not errors:
                    stop_reason = "no_errors"
                    break
                if previous_errors is not None and len(errors) >= previous_errors:
                    # Count did not go down: more rounds would just repeat
                    # the same failures. (> can't happen - a round only
                    # re-runs the previous round's failures - but >= keeps
                    # the loop terminating even if that ever changed.)
                    stop_reason = "no_improvement"
                    break

                previous_errors = len(errors)
                if not self._interruptible_cooldown(should_cancel):
                    stop_reason = "cancelled"
                    break
                pending = errors
                round_no += 1

            if self._error_rounds_enabled:
                logger.info(
                    "platform=%s error rounds finished: reason=%s extra_rounds=%d "
                    "remaining_errors=%d",
                    client.name,
                    stop_reason,
                    rounds_done,
                    sum(1 for r in final_results.values() if _is_transient_failure(r)),
                )
        finally:
            # Runs even on should_cancel's break and on any exception, so this
            # run_id is never left "open"/orphaned in update_runs — finished_at
            # always ends up set, and summary reflects whatever was actually
            # processed before the stop/crash. Exactly one run_product_results
            # row (and one RunSummary entry) per product: its final outcome.
            try:
                for result in final_results.values():
                    summary.add_result(result)
                    self._state_repository.record_run_product_result(run_id, result)
            finally:
                summary.error_rounds = rounds_done
                self._state_repository.finish_run(
                    run_id, summary=summary, finished_at=datetime.now(UTC)
                )
        return summary

    def _process_round(
        self,
        client: PlatformClient,
        products: list[Product],
        indices: list[int],
        final_results: dict[int, UpdateResult],
        *,
        on_product_start: Callable[[Product], None] | None,
        on_price_resolved: Callable[[Product, str, int], None] | None,
        on_result: Callable[[UpdateResult], None] | None,
        should_cancel: Callable[[], bool] | None,
    ) -> bool:
        """One pass over `products[i] for i in indices`. Stores each outcome in
        final_results (replacing any earlier one) and in product_state right
        away. Returns True if it stopped early because of should_cancel.
        """
        for index in indices:
            # Graceful cancellation: only checked BETWEEN products, never
            # inside _update_one's retry loop — a product already mid-API-
            # operation always finishes safely first.
            if should_cancel is not None and should_cancel():
                return True
            result = self._update_one(
                client,
                products[index],
                on_product_start=on_product_start,
                on_price_resolved=on_price_resolved,
            )
            final_results[index] = result
            self._state_repository.record_result(result)
            if on_result is not None:
                on_result(result)
        return False

    def _interruptible_cooldown(self, should_cancel: Callable[[], bool] | None) -> bool:
        """Pause between two rounds so rate limits can clear. Sleeps in
        1-second slices and checks should_cancel before each one, so a Stop
        takes effect within about a second. Returns False if cancelled.
        """
        remaining = self._error_rounds_cooldown_seconds
        while True:
            if should_cancel is not None and should_cancel():
                return False
            if remaining <= 0:
                return True
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    def _update_one(
        self,
        client: PlatformClient,
        product: Product,
        *,
        on_product_start: Callable[[Product], None] | None = None,
        on_price_resolved: Callable[[Product, str, int], None] | None = None,
    ) -> UpdateResult:
        if on_product_start is not None:
            on_product_start(product)

        platform = client.name

        if product.target_price is None:
            return UpdateResult(
                product=product,
                platform=platform,
                status=OutcomeStatus.FAILED_INVALID_DATA,
                error_message=(
                    "No target price found for this product. Check "
                    "config/sheet_mapping.yaml: target_price_column."
                ),
            )
        # Note: reference price is intentionally NOT validated here against
        # product.reference_price (the sheet column). Where the price comes
        # from is a per-platform decision (client.get_reference_price) — for
        # DigiKala it's fetched live from the API, not the sheet, per the
        # documented 'price_list' ("reference price") field.
        if not product.platform_ref(platform):
            return UpdateResult(
                product=product,
                platform=platform,
                status=OutcomeStatus.FAILED_MISSING_IDENTIFIER,
                target_price=product.target_price,
                error_message=f"No '{platform}' reference in the sheet for this product.",
            )

        # Retry policy depends on error type, decided by the exception's own
        # `retryable` flag (set where it's raised, e.g. platforms/digikala/
        # client.py): non-retryable errors (429, auth, config, invalid data,
        # variant-not-found) stop after exactly 1 attempt; retryable errors
        # (5xx, network) get up to `max_retry_attempts` total attempts
        # (default 2 = 1 retry). This loop never overrides that per-type
        # decision — it only enforces the cap once an error IS retryable.
        last_error: Exception | None = None
        attempt = 0
        for attempt in range(1, self._max_retry_attempts + 1):
            try:
                variant_id = client.resolve_variant_id(product)
                reference_price = client.get_reference_price(product, variant_id)
                if on_price_resolved is not None:
                    on_price_resolved(product, variant_id, reference_price)
                decision = compute_price_to_apply(
                    reference_price,
                    product.target_price,
                    max_increase_pct=self._max_increase_pct,
                    apply_cap=platform in CAPPED_PLATFORMS,
                )
                client.update_price(variant_id, decision.price_to_apply)
            except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
                last_error = exc
                retryable = is_retryable(exc)
                logger.warning(
                    "Update attempt %d/%d failed for %s/%s: %s (retryable=%s)",
                    attempt,
                    self._max_retry_attempts,
                    platform,
                    product.internal_id,
                    exc,
                    retryable,
                )
                if not retryable or attempt == self._max_retry_attempts:
                    break
                continue
            else:
                status = OutcomeStatus.CAPPED_12PCT if decision.is_capped else OutcomeStatus.SUCCESS
                return UpdateResult(
                    product=product,
                    platform=platform,
                    status=status,
                    previous_price=decision.reference_price,
                    applied_price=decision.price_to_apply,
                    target_price=decision.target_price,
                    variant_id=variant_id,
                    attempts=attempt,
                )

        return UpdateResult(
            product=product,
            platform=platform,
            status=_status_for_error(last_error),
            previous_price=product.reference_price,
            target_price=product.target_price,
            attempts=attempt,
            error_message=str(last_error) if last_error else "Unknown error",
            transient=is_transient_error(last_error),
        )


def _is_transient_failure(result: UpdateResult | None) -> bool:
    """True only for a real failure that was caused by an accidental error.
    Decided on `transient` + `is_failure`, never on the status alone (FAILED_API
    also covers permanent 400/404), and CAPPED_12PCT / SUCCESS are never
    included - the 12% cap is handled by the hourly recheck."""
    return result is not None and result.is_failure and result.transient


def _status_for_error(error: Exception | None) -> OutcomeStatus:
    if isinstance(error, DigikalaReferencePriceRejected):
        return OutcomeStatus.CAPPED_12PCT
    if isinstance(error, PlatformAuthError):
        return OutcomeStatus.FAILED_AUTH
    if isinstance(error, VariantResolutionError):
        return OutcomeStatus.FAILED_MISSING_IDENTIFIER
    if isinstance(error, PlatformAPIError):
        return OutcomeStatus.FAILED_API
    if isinstance(error, ProductDataError):
        return OutcomeStatus.FAILED_INVALID_DATA
    return OutcomeStatus.FAILED_UNEXPECTED