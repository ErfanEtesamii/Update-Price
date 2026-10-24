import dataclasses
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from faraz_honar.core.exceptions import (
    ConfigurationError,
    DigikalaReferencePriceRejected,
    MissingProductIdentifierError,
    PlatformAPIError,
    PlatformAuthError,
    ProductDataError,
    VariantResolutionError,
)
from faraz_honar.core.models import OutcomeStatus, Product
from faraz_honar.platforms.base import PlatformClient
from faraz_honar.sheets.base import ProductSource
from faraz_honar.sheets.csv_source import CSVProductSource
from faraz_honar.sheets.mapping import SheetMapping
from faraz_honar.state.db import session
from faraz_honar.state.repository import StateRepository
from faraz_honar.update.price_check import CheckSelection, CheckStatus
from faraz_honar.update.service import PriceUpdateService


class FakeProductSource(ProductSource):
    def __init__(self, products: list[Product]):
        self._products = products

    def fetch_products(self) -> list[Product]:
        return list(self._products)


class FakePlatformClient(PlatformClient):
    """Scriptable fake: queue up behaviors per call to drive every branch
    of the orchestration service without any real network access."""

    name = "digikala"

    def __init__(self):
        self.update_calls: list[tuple[str, int]] = []
        self.variant_id_by_product: dict[str, str] = {}
        self.reference_price_override: dict[str, int] = {}
        self.update_price_errors: dict[str, list[Exception | None]] = {}
        self.resolve_errors: dict[str, Exception] = {}

    def resolve_variant_id(self, product: Product) -> str:
        if product.internal_id in self.resolve_errors:
            raise self.resolve_errors[product.internal_id]
        return self.variant_id_by_product.get(product.internal_id, "V-" + product.internal_id)

    def get_reference_price(self, product: Product, variant_id: str) -> int:
        override = self.reference_price_override.get(product.internal_id)
        if override is not None:
            return override
        assert product.reference_price is not None
        return product.reference_price

    def update_price(self, variant_id: str, new_price: int) -> None:
        self.update_calls.append((variant_id, new_price))
        queue = self.update_price_errors.get(variant_id)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error


class _AllGreenCSVSource(CSVProductSource):
    """A CSV export carries no cell colors, so no product would be green and
    (eligibility is green-only) nothing would run. Treat every code in the CSV
    as green, with the original name as the display name when "اسم جدید" is empty."""

    def fetch_products(self) -> list[Product]:
        return [
            dataclasses.replace(
                p,
                display_name=p.display_name or p.name,
                green_platforms=frozenset(p.platform_refs),
            )
            for p in super().fetch_products()
        ]


def _product(**overrides: Any) -> Product:
    defaults: dict[str, Any] = dict(
        internal_id="p1",
        name="Product 1",
        reference_price=1_000_000,
        target_price=1_100_000,
        platform_refs={"digikala": "ref-1"},
    )
    defaults.update(overrides)
    # Eligibility now also needs "اسم جدید" and a GREEN code cell per platform.
    defaults.setdefault("display_name", defaults["name"])
    defaults.setdefault("green_platforms", frozenset(defaults["platform_refs"]))
    return Product(**defaults)


def _service(
    tmp_path: Path, products: list[Product], client: FakePlatformClient
) -> PriceUpdateService:
    return PriceUpdateService(
        product_source=FakeProductSource(products),
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        # Matches PriceUpdateService's own production default: 2 = 1 retry,
        # for retryable errors only (see test_retryable_api_error_retries_
        # up_to_max_then_fails and test_retryable_error_succeeds_on_
        # second_attempt below).
        max_retry_attempts=2,
        # These legacy tests exercise a single pass; error rounds (default ON in
        # production) are covered by their own tests, which build the service
        # with error_rounds_enabled=True and cooldown 0.
        error_rounds_enabled=False,
    )


def test_successful_update_within_12pct(tmp_path: Path):
    client = FakePlatformClient()
    product = _product(reference_price=1_000_000, target_price=1_100_000)
    service = _service(tmp_path, [product], client)

    summaries = service.run(["digikala"], triggered_by="test")
    result = summaries["digikala"].results[0]

    assert result.status == OutcomeStatus.SUCCESS
    assert result.applied_price == 1_100_000
    assert result.attempts == 1
    assert client.update_calls == [("V-p1", 1_100_000)]


def test_reference_price_rejection_reports_capped_status(tmp_path: Path):
    # The 12% cap is no longer computed on our side (CAPPED_PLATFORMS is
    # empty) — the full target_price is always sent, and CAPPED_12PCT is now
    # produced only when DigiKala itself rejects it via its own
    # reference-price-range validation (DigikalaReferencePriceRejected).
    client = FakePlatformClient()
    product = _product(reference_price=1_000_000, target_price=2_000_000)
    client.update_price_errors["V-p1"] = [
        DigikalaReferencePriceRejected("rejected", retryable=False)
    ]
    service = _service(tmp_path, [product], client)

    summaries = service.run(["digikala"], triggered_by="test")
    result = summaries["digikala"].results[0]

    assert result.status == OutcomeStatus.CAPPED_12PCT
    assert result.applied_price is None
    assert result.target_price == 2_000_000
    # The full (uncapped) target price is what gets sent before rejection.
    assert client.update_calls == [("V-p1", 2_000_000)]


def test_missing_target_price_is_filtered_out_before_any_lookup(tmp_path: Path):
    # A product with no target price is excluded from the run entirely —
    # never reaches _update_one, never appears in the results, never makes
    # a platform request. It is not reported as a failure of any kind.
    client = FakePlatformClient()
    product = _product(target_price=None)
    service = _service(tmp_path, [product], client)

    started: list[str] = []
    summary = service.run(
        ["digikala"], triggered_by="test", on_product_start=lambda p: started.append(p.internal_id)
    )["digikala"]

    assert summary.results == []
    assert started == []
    assert client.update_calls == []


def test_products_skipped_for_missing_target_price_are_reported_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    # Still filtered out (no lookup, no result), but no longer SILENT: a
    # warning names them so "price never changes" has a visible cause.
    client = FakePlatformClient()
    skipped = _product(internal_id="no-target", target_price=None)
    ok = _product(internal_id="has-target")
    service = _service(tmp_path, [skipped, ok], client)

    with caplog.at_level("WARNING", logger="faraz_honar.update.service"):
        service.run(["digikala"], triggered_by="test")

    messages = [r.getMessage() for r in caplog.records]
    assert any("no-target" in m and "NO usable target price" in m for m in messages)
    assert not any("has-target" in m and "NO usable target price" in m for m in messages)


def test_no_skipped_warning_when_every_product_has_a_target_price(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    client = FakePlatformClient()
    service = _service(tmp_path, [_product()], client)

    with caplog.at_level("WARNING", logger="faraz_honar.update.service"):
        service.run(["digikala"], triggered_by="test")

    assert not any("NO usable target price" in r.getMessage() for r in caplog.records)


def test_missing_platform_ref_is_filtered_out_before_any_lookup(tmp_path: Path):
    # Same as above for a product with no DigiKala reference ("دیجی کالا
    # سریع") in the sheet at all.
    client = FakePlatformClient()
    product = _product(platform_refs={})
    service = _service(tmp_path, [product], client)

    started: list[str] = []
    summary = service.run(
        ["digikala"], triggered_by="test", on_product_start=lambda p: started.append(p.internal_id)
    )["digikala"]

    assert summary.results == []
    assert started == []
    assert client.update_calls == []


def test_empty_or_whitespace_qeymat_asli_never_reaches_digikala_lookup(tmp_path: Path):
    # "قیمت اصلی" (Original Price) IS the sheet's target_price_column (see
    # config/sheet_mapping.yaml) — this proves end-to-end, through the real
    # CSV parser (sheets/mapping.py), that an empty or whitespace-only cell
    # there excludes the product before any DigiKala lookup, same as a
    # missing platform reference does. Not a new mechanism: the same
    # target_price eligibility check already used above, exercised here via
    # the real column name instead of a directly-constructed Product.
    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "اسم,اسم جدید,قیمت,قیمت اصلی,دیجی کالا سریع\n"
        "P1,P1,500000,100000,V-P1\n"  # valid قیمت اصلی -> eligible
        "P2,P2,500000,,V-P2\n"  # empty قیمت اصلی -> excluded
        "P3,P3,500000,   ,V-P3\n",  # whitespace-only قیمت اصلی -> excluded
        encoding="utf-8",
    )
    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column="قیمت اصلی",
        platform_ref_columns={"digikala": "دیجی کالا سریع"},
    )
    source = _AllGreenCSVSource(csv_path, mapping)

    client = FakePlatformClient()
    client.reference_price_override["P1"] = 1_000_000
    service = PriceUpdateService(
        product_source=source,
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    started: list[str] = []
    summary = service.run(
        ["digikala"], triggered_by="test", on_product_start=lambda p: started.append(p.internal_id)
    )["digikala"]

    # Only P1 (valid قیمت اصلی = "100000" Toman -> 1,000,000 Rial) is ever
    # started or makes any DigiKala request; P2/P3 are silently absent.
    assert started == ["P1"]
    assert [r.product.internal_id for r in summary.results] == ["P1"]
    assert client.update_calls == [("V-P1", 1_000_000)]


def test_update_one_still_defensively_rejects_missing_target_price(tmp_path: Path):
    # _update_one's own guard is kept as a defensive fallback for any
    # caller that doesn't pre-filter (run() and recheck_capped_products()
    # both do) — proves that fallback still works, unchanged, even though
    # normal operation never reaches it.
    client = FakePlatformClient()
    service = _service(tmp_path, [], client)
    product = _product(target_price=None)

    result = service._update_one(client, product)  # noqa: SLF001 - direct fallback check
    assert result.status == OutcomeStatus.FAILED_INVALID_DATA
    assert result.attempts == 0
    assert client.update_calls == []


def test_update_one_still_defensively_rejects_missing_platform_ref(tmp_path: Path):
    client = FakePlatformClient()
    service = _service(tmp_path, [], client)
    product = _product(platform_refs={})

    result = service._update_one(client, product)  # noqa: SLF001 - direct fallback check
    assert result.status == OutcomeStatus.FAILED_MISSING_IDENTIFIER
    assert client.update_calls == []


def test_variant_resolution_error_maps_to_missing_identifier_and_does_not_retry(tmp_path: Path):
    client = FakePlatformClient()
    product = _product()
    client.resolve_errors["p1"] = VariantResolutionError("not implemented", retryable=False)
    service = _service(tmp_path, [product], client)

    result = service.run(["digikala"], triggered_by="test")["digikala"].results[0]
    assert result.status == OutcomeStatus.FAILED_MISSING_IDENTIFIER
    assert result.attempts == 1


def test_retryable_api_error_retries_up_to_max_then_fails(tmp_path: Path):
    # Retryable errors (5xx / network) get up to max_retry_attempts total
    # attempts (2 = 1 retry), never more.
    client = FakePlatformClient()
    product = _product()
    client.update_price_errors["V-p1"] = [
        PlatformAPIError("temp", retryable=True),
        PlatformAPIError("temp", retryable=True),
    ]
    service = _service(tmp_path, [product], client)

    result = service.run(["digikala"], triggered_by="test")["digikala"].results[0]
    assert result.status == OutcomeStatus.FAILED_API
    assert result.attempts == 2
    assert len(client.update_calls) == 2


def test_rate_limited_error_is_not_retried(tmp_path: Path):
    # HTTP 429 is classified as non-retryable at the point it's raised
    # (platforms/digikala/client.py). Proves the service-level effect of
    # that classification: exactly one attempt, no retry, regardless of
    # max_retry_attempts.
    client = FakePlatformClient()
    product = _product()
    client.update_price_errors["V-p1"] = [
        PlatformAPIError(
            "DigiKala price update failed: HTTP 429: rate limited",
            status_code=429,
            retryable=False,
        ),
        # A second queued error would be consumed if the service incorrectly
        # retried; its presence would turn a wrongly-passing test into a
        # loud failure (wrong status/attempts) rather than silently passing.
        PlatformAPIError("should never be reached", retryable=False),
    ]
    service = _service(tmp_path, [product], client)

    result = service.run(["digikala"], triggered_by="test")["digikala"].results[0]
    assert result.status == OutcomeStatus.FAILED_API
    assert "HTTP 429" in (result.error_message or "")
    assert result.attempts == 1
    assert len(client.update_calls) == 1


def test_retryable_error_succeeds_on_second_attempt(tmp_path: Path):
    client = FakePlatformClient()
    product = _product()
    client.update_price_errors["V-p1"] = [PlatformAPIError("temp", retryable=True), None]
    service = _service(tmp_path, [product], client)

    result = service.run(["digikala"], triggered_by="test")["digikala"].results[0]
    assert result.status == OutcomeStatus.SUCCESS
    assert result.attempts == 2


def test_auth_failure_maps_to_failed_auth(tmp_path: Path):
    client = FakePlatformClient()
    product = _product()
    client.update_price_errors["V-p1"] = [PlatformAuthError("expired", retryable=False)]
    service = _service(tmp_path, [product], client)

    result = service.run(["digikala"], triggered_by="test")["digikala"].results[0]
    assert result.status == OutcomeStatus.FAILED_AUTH
    assert result.attempts == 1
    assert len(client.update_calls) == 1


def test_unknown_platform_raises(tmp_path: Path):
    from faraz_honar.core.exceptions import ConfigurationError

    client = FakePlatformClient()
    service = _service(tmp_path, [_product()], client)
    with pytest.raises(ConfigurationError):
        service.run(["not-a-real-platform"], triggered_by="test")


def test_capped_product_is_rechecked_and_eventually_succeeds(tmp_path: Path):
    client = FakePlatformClient()
    product = _product(reference_price=1_000_000, target_price=2_000_000)
    client.update_price_errors["V-p1"] = [
        DigikalaReferencePriceRejected("rejected", retryable=False)
    ]
    service = _service(tmp_path, [product], client)

    first = service.run(["digikala"], triggered_by="test")["digikala"].results[0]
    assert first.status == OutcomeStatus.CAPPED_12PCT
    assert first.applied_price is None

    # DigiKala's own reference-price bracket has since been raised (e.g. via
    # an approved ticket), so the recheck now goes through with no error.
    recheck_summaries = service.recheck_capped_products(["digikala"])
    second = recheck_summaries["digikala"].results[0]
    assert second.status == OutcomeStatus.SUCCESS
    assert second.applied_price == 2_000_000


def test_hourly_recheck_uses_the_toman_to_rial_converted_target_price(tmp_path: Path):
    # Uses a real CSVProductSource (not FakeProductSource) so both the
    # initial run and the recheck go through the actual sheet-parsing
    # boundary (sheets/mapping.py::rows_to_products) that applies the
    # Toman -> Rial conversion — proving recheck isn't a separate code path
    # that could see stale/unconverted units.
    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "اسم,اسم جدید,قیمت,قیمت اصلی,دیجی کالا سریع\nP1,P1,500000,200000,V-P1\n",
        encoding="utf-8",
    )
    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column="قیمت اصلی",
        platform_ref_columns={"digikala": "دیجی کالا سریع"},
    )
    source = _AllGreenCSVSource(csv_path, mapping)

    client = FakePlatformClient()
    client.reference_price_override["P1"] = 1_000_000  # DigiKala's live reference price
    # DigiKala rejects the first attempt via its own reference-price check.
    client.update_price_errors["V-P1"] = [
        DigikalaReferencePriceRejected("rejected", retryable=False)
    ]
    service = PriceUpdateService(
        product_source=source,
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    # Sheet cell is "200000" Toman -> target_price must be 2,000,000 Rial,
    # rejected by DigiKala this run.
    first = service.run(["digikala"], triggered_by="test")["digikala"].results[0]
    assert first.target_price == 2_000_000
    assert first.status == OutcomeStatus.CAPPED_12PCT
    assert first.applied_price is None

    # DigiKala's own reference-price bracket has since been raised. The
    # recheck re-reads the same CSV (same Toman cell) — it must again
    # convert to 2,000,000 Rial, not re-use a stale value.
    second = service.recheck_capped_products(["digikala"])["digikala"].results[0]
    assert second.target_price == 2_000_000
    assert second.status == OutcomeStatus.SUCCESS
    assert second.applied_price == 2_000_000


# -- Observer hooks (spec: connect the Telegram control panel without a --------
# -- second business-logic engine) -----------------------------------------------


def test_on_products_loaded_receives_total_count(tmp_path: Path):
    client = FakePlatformClient()
    products = [_product(internal_id=f"p{i}", target_price=1_000_000) for i in range(3)]
    service = _service(tmp_path, products, client)

    totals: list[int] = []
    service.run(["digikala"], triggered_by="test", on_products_loaded=totals.append)
    assert totals == [3]


def test_on_products_loaded_reports_the_filtered_eligible_count_not_the_raw_sheet_count(
    tmp_path: Path,
):
    # 5 rows in the sheet, but only 2 are eligible (have both a platform
    # reference and a target price) — on_products_loaded must report 2, the
    # count the run will actually attempt, not 5.
    client = FakePlatformClient()
    products = [
        _product(internal_id="a", target_price=1_000_000),
        _product(internal_id="b", target_price=1_000_000),
        _product(internal_id="no_target", target_price=None),
        _product(internal_id="no_ref", target_price=1_000_000, platform_refs={}),
        _product(internal_id="no_ref_and_no_target", target_price=None, platform_refs={}),
    ]
    service = _service(tmp_path, products, client)

    totals: list[int] = []
    service.run(["digikala"], triggered_by="test", on_products_loaded=totals.append)
    assert totals == [2]


def test_on_product_start_only_fires_for_eligible_products(tmp_path: Path):
    # Products filtered out (no target price / no platform ref) never reach
    # _update_one at all, so on_product_start never fires for them either.
    client = FakePlatformClient()
    products = [
        _product(internal_id="ok", target_price=1_000_000),
        _product(internal_id="no_target", target_price=None),
        _product(internal_id="no_ref", target_price=1_000_000, platform_refs={}),
    ]
    service = _service(tmp_path, products, client)

    started = []
    service.run(["digikala"], triggered_by="test", on_product_start=lambda p: started.append(p.internal_id))
    assert started == ["ok"]


def test_on_price_resolved_only_fires_once_variant_and_reference_price_are_known(tmp_path: Path):
    client = FakePlatformClient()
    products = [
        _product(internal_id="ok", target_price=1_000_000, reference_price=900_000),
        _product(internal_id="invalid", target_price=None),
    ]
    service = _service(tmp_path, products, client)

    resolved: list[tuple[str, str, int]] = []
    service.run(
        ["digikala"],
        triggered_by="test",
        on_price_resolved=lambda p, variant_id, ref_price: resolved.append(
            (p.internal_id, variant_id, ref_price)
        ),
    )
    assert resolved == [("ok", "V-ok", 900_000)]


def test_on_result_fires_once_per_eligible_product_with_the_final_result(tmp_path: Path):
    # "b" has no target price and is filtered out before any processing, so
    # on_result never fires for it — only "a" (eligible) gets a result.
    client = FakePlatformClient()
    products = [
        _product(internal_id="a", target_price=1_000_000, reference_price=900_000),
        _product(internal_id="b", target_price=None),
    ]
    service = _service(tmp_path, products, client)

    results: list[Any] = []
    service.run(["digikala"], triggered_by="test", on_result=results.append)
    assert [r.product.internal_id for r in results] == ["a"]
    assert results[0].status == OutcomeStatus.SUCCESS


def test_should_cancel_stops_before_the_next_product_not_mid_product(tmp_path: Path):
    client = FakePlatformClient()
    products = [
        _product(internal_id="a", target_price=1_000_000, reference_price=900_000),
        _product(internal_id="b", target_price=1_000_000, reference_price=900_000),
        _product(internal_id="c", target_price=1_000_000, reference_price=900_000),
    ]
    service = _service(tmp_path, products, client)

    processed_ids: list[str] = []

    def on_result(result):
        processed_ids.append(result.product.internal_id)

    # Cancel is only requested once product "a" is already done, so "a"
    # must complete (graceful cancellation: current product finishes safely)
    # while "b" and "c" must never start at all.
    def should_cancel():
        return "a" in processed_ids

    summary = service.run(
        ["digikala"], triggered_by="test", on_result=on_result, should_cancel=should_cancel
    )["digikala"]

    assert processed_ids == ["a"]
    assert len(summary.results) == 1
    assert client.update_calls == [("V-a", 1_000_000)]  # "b"/"c" never even attempted


def test_hooks_default_to_none_and_do_not_change_behavior(tmp_path: Path):
    # Same run, with and without hooks, must produce identical results —
    # proves the hooks are purely observational.
    client_a = FakePlatformClient()
    client_b = FakePlatformClient()
    products = [_product(reference_price=1_000_000, target_price=1_100_000)]

    plain = _service(tmp_path, products, client_a).run(["digikala"], triggered_by="test")
    with_hooks = _service(tmp_path, products, client_b).run(
        ["digikala"],
        triggered_by="test",
        on_products_loaded=lambda n: None,
        on_product_start=lambda p: None,
        on_price_resolved=lambda p, v, r: None,
        on_result=lambda r: None,
        should_cancel=lambda: False,
    )

    assert plain["digikala"].results[0].status == with_hooks["digikala"].results[0].status
    assert plain["digikala"].results[0].applied_price == with_hooks["digikala"].results[0].applied_price


def test_recheck_capped_products_does_not_require_or_use_hooks(tmp_path: Path):
    # recheck_capped_products() has no hook parameters at all — confirms the
    # hourly recheck's call signature/behavior is completely untouched.
    client = FakePlatformClient()
    product = _product(reference_price=1_000_000, target_price=2_000_000)
    client.update_price_errors["V-p1"] = [
        DigikalaReferencePriceRejected("rejected", retryable=False)
    ]
    service = _service(tmp_path, [product], client)

    service.run(["digikala"], triggered_by="test")  # first pass -> capped, eligible for recheck
    result = service.recheck_capped_products(["digikala"])["digikala"].results[0]
    assert result.status == OutcomeStatus.SUCCESS


class _MutableProductSource(ProductSource):
    """Returns whatever the current `products` list is at call time, so a
    test can simulate the sheet changing between the initial run and a
    later recheck (e.g. an operator clearing a cell)."""

    def __init__(self, products: list[Product]):
        self.products = products

    def fetch_products(self) -> list[Product]:
        return list(self.products)


def test_recheck_skips_a_capped_product_that_lost_its_reference_or_target_price(tmp_path: Path):
    # Matches the same eligibility rule used by run(): if the sheet no
    # longer has a target price (or platform reference) for a product that
    # was previously capped, the hourly recheck must not look it up either.
    client = FakePlatformClient()
    product = _product(reference_price=1_000_000, target_price=2_000_000)
    client.update_price_errors["V-p1"] = [
        DigikalaReferencePriceRejected("rejected", retryable=False)
    ]
    source = _MutableProductSource([product])
    service = PriceUpdateService(
        product_source=source,
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    service.run(["digikala"], triggered_by="test")  # capped, eligible for recheck
    assert client.update_calls == [("V-p1", 2_000_000)]

    # The operator clears the target price in the sheet before the next
    # hourly tick.
    source.products = [_product(reference_price=1_000_000, target_price=None)]
    summary = service.recheck_capped_products(["digikala"])["digikala"]

    assert summary.results == []
    assert client.update_calls == [("V-p1", 2_000_000)]  # unchanged — no second lookup/update


def test_digikala_and_digikala2_are_matched_against_their_own_sheet_column_only(tmp_path: Path):
    # One sheet, both DigiKala accounts. Proves: each platform is only ever
    # matched against its own reference column ("دیجی کالا اصلی" for
    # "digikala", "دیجی کالا سریع" for "digikala2"); a product with a value
    # only in the OTHER platform's column is invisible to this one; and a
    # missing "قیمت اصلی" excludes a product from BOTH platforms with zero API
    # calls for either.
    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "اسم,اسم جدید,قیمت,قیمت اصلی,دیجی کالا اصلی,دیجی کالا سریع\n"
        "P1,P1,500000,100000,ref-main-1,\n"  # digikala only
        "P2,P2,500000,100000,,ref-fast-2\n"  # digikala2 only
        "P3,P3,500000,,ref-main-3,ref-fast-3\n"  # both refs, no قیمت اصلی -> excluded from both
        "P4,P4,500000,100000,ref-main-4,ref-fast-4\n",  # both refs + valid price -> eligible for both
        encoding="utf-8",
    )
    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column="قیمت اصلی",
        platform_ref_columns={"digikala": "دیجی کالا اصلی", "digikala2": "دیجی کالا سریع"},
    )
    source = _AllGreenCSVSource(csv_path, mapping)

    digikala_client = FakePlatformClient()  # name defaults to "digikala"
    digikala_client.reference_price_override.update({"P1": 1_000_000, "P4": 1_000_000})
    digikala2_client = FakePlatformClient()
    digikala2_client.name = "digikala2"  # matches the real DigikalaClient(name=...) contract
    digikala2_client.reference_price_override.update({"P2": 1_000_000, "P4": 1_000_000})

    service = PriceUpdateService(
        product_source=source,
        platform_registry={"digikala": digikala_client, "digikala2": digikala2_client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    digikala_started: list[str] = []
    digikala_summary = service.run(
        ["digikala"],
        triggered_by="test",
        on_product_start=lambda p: digikala_started.append(p.internal_id),
    )["digikala"]

    digikala2_started: list[str] = []
    digikala2_summary = service.run(
        ["digikala2"],
        triggered_by="test",
        on_product_start=lambda p: digikala2_started.append(p.internal_id),
    )["digikala2"]

    # "digikala" only ever sees P1 and P4 — never P2 (digikala2-only ref) or
    # P3 (missing قیمت اصلی).
    assert digikala_started == ["P1", "P4"]
    assert [r.product.internal_id for r in digikala_summary.results] == ["P1", "P4"]
    assert digikala_client.update_calls == [("V-P1", 1_000_000), ("V-P4", 1_000_000)]

    # "digikala2" only ever sees P2 and P4 — never P1 (digikala-only ref) or P3.
    assert digikala2_started == ["P2", "P4"]
    assert [r.product.internal_id for r in digikala2_summary.results] == ["P2", "P4"]
    assert digikala2_client.update_calls == [("V-P2", 1_000_000), ("V-P4", 1_000_000)]

    # P3 (empty قیمت اصلی) never started and made zero requests on either
    # platform's client.
    assert "P3" not in digikala_started
    assert "P3" not in digikala2_started


# -- rerun_specific_products (spec section 3: "retry failed only") ----------


def test_rerun_specific_products_only_processes_the_given_ids(tmp_path: Path):
    client = FakePlatformClient()
    p1 = _product(internal_id="p1", reference_price=1_000_000, target_price=1_100_000)
    p2 = _product(internal_id="p2", reference_price=1_000_000, target_price=1_100_000)
    p3 = _product(internal_id="p3", reference_price=1_000_000, target_price=1_100_000)
    service = _service(tmp_path, [p1, p2, p3], client)

    summary = service.rerun_specific_products("digikala", ["p2"], triggered_by="test")

    assert [r.product.internal_id for r in summary.results] == ["p2"]
    assert client.update_calls == [("V-p2", 1_100_000)]


def test_rerun_specific_products_skips_an_id_no_longer_in_the_sheet(tmp_path: Path):
    client = FakePlatformClient()
    p1 = _product(internal_id="p1", reference_price=1_000_000, target_price=1_100_000)
    source = _MutableProductSource([p1])
    service = PriceUpdateService(
        product_source=source,
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.rerun_specific_products(
        "digikala", ["p1", "gone-from-sheet"], triggered_by="test"
    )

    assert [r.product.internal_id for r in summary.results] == ["p1"]
    assert client.update_calls == [("V-p1", 1_100_000)]


def test_rerun_specific_products_skips_an_id_no_longer_eligible(tmp_path: Path):
    # Same eligibility rule as run()/recheck_capped_products(): a product
    # that lost its target price (or platform reference) since the failed
    # run must not be retried either.
    client = FakePlatformClient()
    p1 = _product(internal_id="p1", reference_price=1_000_000, target_price=1_100_000)
    p2 = _product(internal_id="p2", reference_price=1_000_000, target_price=None)
    service = _service(tmp_path, [p1, p2], client)

    summary = service.rerun_specific_products("digikala", ["p1", "p2"], triggered_by="test")

    assert [r.product.internal_id for r in summary.results] == ["p1"]
    assert client.update_calls == [("V-p1", 1_100_000)]


def test_rerun_specific_products_records_a_new_independent_run(tmp_path: Path):
    client = FakePlatformClient()
    p1 = _product(internal_id="p1", reference_price=1_000_000, target_price=1_100_000)
    repo = StateRepository(tmp_path / "state.db")
    service = PriceUpdateService(
        product_source=FakeProductSource([p1]),
        platform_registry={"digikala": client},
        state_repository=repo,
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    service.run(["digikala"], triggered_by="test:first")
    service.rerun_specific_products("digikala", ["p1"], triggered_by="test:retry")

    runs = repo.list_recent_runs("digikala", limit=10)
    assert len(runs) == 2
    assert {r.triggered_by for r in runs} == {"test:first", "test:retry"}


def test_rerun_specific_products_on_products_loaded_reports_the_filtered_count(tmp_path: Path):
    client = FakePlatformClient()
    p1 = _product(internal_id="p1", reference_price=1_000_000, target_price=1_100_000)
    p2 = _product(internal_id="p2", reference_price=1_000_000, target_price=None)  # ineligible
    service = _service(tmp_path, [p1, p2], client)

    loaded: list[int] = []
    service.rerun_specific_products(
        "digikala",
        ["p1", "p2", "missing"],
        triggered_by="test",
        on_products_loaded=loaded.append,
    )

    assert loaded == [1]


# -- compute_platform_backlog -------------------------------------------------------


def _bp(pid: str, platform: str = "digikala", target: int | None = 1_100_000) -> Product:
    return Product(
        internal_id=pid,
        name=pid,
        reference_price=1_000_000,
        target_price=target,
        platform_refs={platform: f"ref-{pid}"}, display_name=pid, green_platforms=frozenset({platform}),
    )


def _row(pid: str, status: OutcomeStatus, platform: str = "digikala"):
    from faraz_honar.state.repository import ProductStateRow

    return ProductStateRow(
        product_id=pid,
        platform=platform,
        product_name=pid,
        reference_price=None,
        target_price=None,
        applied_price=None,
        is_capped=False,
        variant_id=None,
        status=status.value,
        retry_count=0,
        last_error=None,
        updated_at="",
    )


def test_backlog_splits_failed_unchecked_and_done_in_sheet_order():
    from faraz_honar.update.service import compute_platform_backlog

    products = [_bp("a"), _bp("b"), _bp("c"), _bp("d"), _bp("e")]
    states = [
        _row("a", OutcomeStatus.SUCCESS),
        _row("c", OutcomeStatus.CAPPED_12PCT),
        _row("d", OutcomeStatus.FAILED_AUTH),
        _row("e", OutcomeStatus.SKIPPED_NO_CHANGE),
    ]
    backlog = compute_platform_backlog(products, states, "digikala")

    assert backlog.failed_ids == ("c", "d")
    assert backlog.unchecked_ids == ("b",)
    assert backlog.retry_ids == ("b", "c", "d")  # sheet order, not failed-first
    assert backlog.eligible_total == 5
    assert backlog.has_any_recorded_state is True
    assert backlog.is_empty is False


def test_backlog_on_a_never_processed_platform_has_no_recorded_state():
    from faraz_honar.update.service import compute_platform_backlog

    backlog = compute_platform_backlog([_bp("a"), _bp("b")], [], "digikala")

    assert backlog.unchecked_ids == ("a", "b")
    assert backlog.has_any_recorded_state is False


def test_backlog_ignores_ineligible_products_and_other_platforms():
    from faraz_honar.update.service import compute_platform_backlog

    products = [_bp("a"), _bp("b", target=None), _bp("c", platform="basalam")]
    states = [
        _row("a", OutcomeStatus.SUCCESS),
        _row("b", OutcomeStatus.FAILED_API),
        _row("a", OutcomeStatus.FAILED_API, platform="basalam"),
    ]
    backlog = compute_platform_backlog(products, states, "digikala")

    assert backlog.is_empty
    assert backlog.eligible_total == 1


def test_on_round_start_reports_each_round_and_its_product_count(tmp_path: Path):
    client = FakePlatformClient()
    products = [
        _product(internal_id="a", target_price=1_000_000, reference_price=900_000),
        _product(internal_id="b", target_price=1_000_000, reference_price=900_000),
        _product(internal_id="c", target_price=1_000_000, reference_price=900_000),
    ]
    rate_limited = lambda: PlatformAPIError("HTTP 429", status_code=429, retryable=False)  # noqa: E731
    client.update_price_errors["V-b"] = [rate_limited(), None]  # fails once, then ok
    client.update_price_errors["V-c"] = [rate_limited(), rate_limited(), None]  # one round longer
    service = PriceUpdateService(
        product_source=FakeProductSource(products),
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
        error_rounds_cooldown_seconds=0,
    )

    rounds: list[tuple[int, int]] = []
    service.run(
        ["digikala"],
        triggered_by="test",
        on_round_start=lambda round_no, count: rounds.append((round_no, count)),
    )
    # Round 1: all 3. Round 2: b and c (2 errors, was 2 -> first comparison
    # has no previous count, so it continues). Round 3: only c.
    assert rounds == [(1, 3), (2, 2), (3, 1)]


def test_on_round_start_is_called_once_when_there_are_no_errors(tmp_path: Path):
    client = FakePlatformClient()
    service = _service(tmp_path, [_product(target_price=1_000_000, reference_price=900_000)], client)

    rounds: list[tuple[int, int]] = []
    service.run(
        ["digikala"],
        triggered_by="test",
        on_round_start=lambda round_no, count: rounds.append((round_no, count)),
    )
    assert rounds == [(1, 1)]


# ---------------------------------------------------------------------------
# Error rounds ("دور مجدد برای خطاهای اتفاقی")
#
# Every test builds the service with error_rounds_enabled=True and cooldown 0
# (the legacy tests above use _service(), which keeps error rounds OFF so they
# keep exercising a single pass). All of them are driven through
# FakePlatformClient, whose update_price_errors queue is consumed one entry per
# update_price call: HTTP 429 with retryable=False is one attempt per round, so
# "[err, None]" means "fails in round 1, succeeds in round 2".
# ---------------------------------------------------------------------------


def _rounds_service(
    tmp_path: Path,
    products: list[Product],
    client: FakePlatformClient,
    *,
    enabled: bool = True,
    cooldown: float = 0.0,
) -> PriceUpdateService:
    return PriceUpdateService(
        product_source=FakeProductSource(products),
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
        error_rounds_enabled=enabled,
        error_rounds_cooldown_seconds=cooldown,
    )


def _eligible(*ids: str) -> list[Product]:
    return [
        _product(internal_id=i, target_price=1_000_000, reference_price=900_000) for i in ids
    ]


def _err429() -> PlatformAPIError:
    return PlatformAPIError("DigiKala price update failed: HTTP 429", status_code=429, retryable=False)


def _run_with_rounds(service: PriceUpdateService, **kwargs: Any):
    rounds: list[tuple[int, int]] = []
    summary = service.run(
        ["digikala"],
        triggered_by="test",
        on_round_start=lambda round_no, count: rounds.append((round_no, count)),
        **kwargs,
    )["digikala"]
    return summary, rounds


def _ids(results: list[Any]) -> list[str]:
    return [r.product.internal_id for r in results]


def _db_rows(tmp_path: Path) -> tuple[list[Any], list[Any]]:
    with session(tmp_path / "state.db") as conn:
        runs = conn.execute(
            "SELECT id, platform, finished_at, success_count, capped_count, failed_count "
            "FROM update_runs"
        ).fetchall()
        rows = conn.execute(
            "SELECT run_id, product_id, status FROM run_product_results ORDER BY id"
        ).fetchall()
    return runs, rows


def test_error_rounds_scenario_5_to_2_to_0_everything_ends_successful(tmp_path: Path):
    # 8 products, 5 hit a transient error in round 1 (E0=5). Round 2 re-runs
    # just those 5: three recover, two fail again (E1=2 < 5 -> keep going).
    # Round 3 re-runs just those 2: both recover (E2=0 -> done).
    client = FakePlatformClient()
    for pid in ("a", "b", "c"):
        client.update_price_errors[f"V-{pid}"] = [_err429(), None]
    for pid in ("d", "e"):
        client.update_price_errors[f"V-{pid}"] = [_err429(), _err429(), None]
    service = _rounds_service(tmp_path, _eligible(*"abcdefgh"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 8), (2, 5), (3, 2)]
    assert len(summary.results) == 8
    assert all(r.status == OutcomeStatus.SUCCESS for r in summary.results)
    assert summary.failed == []
    assert summary.error_rounds == 2
    # 8 + 5 + 2 update calls in total.
    assert len(client.update_calls) == 15


def test_error_rounds_stop_when_the_error_count_stops_shrinking_5_to_3_to_3(tmp_path: Path):
    # E0=5, E1=3 (shrank -> continue), E2=3 (did not shrink -> stop right
    # there). The long error queues would keep failing forever, so a 4th round
    # would show up as extra calls.
    client = FakePlatformClient()
    for pid in ("a", "b"):
        client.update_price_errors[f"V-{pid}"] = [_err429(), None]
    for pid in ("c", "d", "e"):
        client.update_price_errors[f"V-{pid}"] = [_err429() for _ in range(10)]
    service = _rounds_service(tmp_path, _eligible(*"abcde"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 5), (2, 5), (3, 3)]
    assert summary.error_rounds == 2
    assert sorted(_ids(summary.successful)) == ["a", "b"]
    assert sorted(_ids(summary.failed)) == ["c", "d", "e"]
    # The leftovers stay in the report with their real failure status.
    assert all(r.status == OutcomeStatus.FAILED_API and r.transient for r in summary.failed)
    assert len(client.update_calls) == 5 + 5 + 3


def test_error_rounds_run_only_one_extra_round_when_there_is_no_improvement_4_to_4(tmp_path: Path):
    client = FakePlatformClient()
    for pid in "abcd":
        client.update_price_errors[f"V-{pid}"] = [_err429() for _ in range(10)]
    service = _rounds_service(tmp_path, _eligible(*"abcd"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 4), (2, 4)]
    assert summary.error_rounds == 1
    assert len(summary.failed) == 4
    assert len(client.update_calls) == 8


def test_error_rounds_never_loop_forever_on_products_that_always_fail(tmp_path: Path):
    # No round cap exists, so termination rests entirely on "count did not go
    # down". 50 permanently-rate-limited products must still finish in 2 rounds.
    client = FakePlatformClient()
    ids = [f"p{i}" for i in range(50)]
    for pid in ids:
        client.update_price_errors[f"V-{pid}"] = [_err429() for _ in range(200)]
    service = _rounds_service(tmp_path, _eligible(*ids), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 50), (2, 50)]
    assert len(summary.failed) == 50


# Errors that must count as "accidental" and get a second round. `site` is
# where the fake client raises it; `attempts_per_round` is how many times
# _update_one calls the client per round (its own in-product retry loop is
# untouched: only errors flagged retryable get a second immediate attempt).
_TRANSIENT_CASES = [
    pytest.param(
        lambda: PlatformAPIError("HTTP 429", status_code=429, retryable=False),
        "update",
        id="http-429",
    ),
    pytest.param(
        lambda: PlatformAPIError("HTTP 503", status_code=503, retryable=True),
        "update",
        id="http-503",
    ),
    pytest.param(
        lambda: PlatformAPIError("HTTP 500", status_code=500, retryable=False),
        "update",
        id="http-500-not-flagged-retryable",
    ),
    pytest.param(
        lambda: PlatformAPIError("network", retryable=True),
        "update",
        id="wrapped-network-error",
    ),
    pytest.param(lambda: TimeoutError("timed out"), "update", id="builtin-timeout"),
    pytest.param(lambda: ConnectionError("reset"), "update", id="builtin-connection-error"),
    pytest.param(
        lambda: requests.exceptions.ConnectionError("dropped"),
        "update",
        id="requests-connection-error",
    ),
    pytest.param(
        lambda: requests.exceptions.Timeout("slow"),
        "update",
        id="requests-timeout",
    ),
    pytest.param(
        # DigiKala logs "variant search failed: HTTP 429": the decision is on
        # status_code, not on the exception class.
        lambda: VariantResolutionError("variant search failed: HTTP 429", status_code=429),
        "resolve",
        id="variant-resolution-error-with-429",
    ),
    pytest.param(
        lambda: PlatformAPIError("DigiKala variant search failed: HTTP 429", status_code=429),
        "resolve",
        id="variant-search-429",
    ),
]

_PERMANENT_CASES = [
    pytest.param(lambda: PlatformAPIError("HTTP 400", status_code=400), "update", id="http-400"),
    pytest.param(lambda: PlatformAPIError("HTTP 404", status_code=404), "update", id="http-404"),
    pytest.param(lambda: PlatformAPIError("HTTP 422", status_code=422), "update", id="http-422"),
    pytest.param(
        # A client that wrongly flags a 400 as retryable must not enter rounds.
        lambda: PlatformAPIError("HTTP 400", status_code=400, retryable=True),
        "update",
        id="http-400-wrongly-retryable",
    ),
    pytest.param(lambda: PlatformAuthError("bad token"), "update", id="auth-error"),
    pytest.param(
        lambda: MissingProductIdentifierError("no DKP code"), "resolve", id="missing-identifier"
    ),
    pytest.param(
        lambda: VariantResolutionError("variant not found"), "resolve", id="variant-not-found"
    ),
    pytest.param(lambda: ProductDataError("bad row"), "resolve", id="product-data-error"),
    pytest.param(lambda: ConfigurationError("no credentials"), "update", id="configuration-error"),
]


def _client_failing_with(error_factory: Any, site: str) -> FakePlatformClient:
    client = FakePlatformClient()
    if site == "resolve":
        client.resolve_errors["x"] = error_factory()
    else:
        client.update_price_errors["V-x"] = [error_factory() for _ in range(50)]
    return client


@pytest.mark.parametrize(("error_factory", "site"), _TRANSIENT_CASES)
def test_accidental_errors_get_a_second_round(tmp_path: Path, error_factory: Any, site: str):
    client = _client_failing_with(error_factory, site)
    service = _rounds_service(tmp_path, _eligible("x"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 1), (2, 1)]  # retried once, then stopped: no improvement
    assert summary.results[0].is_failure
    assert summary.results[0].transient is True


@pytest.mark.parametrize(("error_factory", "site"), _PERMANENT_CASES)
def test_permanent_errors_never_enter_a_second_round(tmp_path: Path, error_factory: Any, site: str):
    client = _client_failing_with(error_factory, site)
    service = _rounds_service(tmp_path, _eligible("x"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 1)]
    assert summary.error_rounds == 0
    assert summary.results[0].is_failure
    assert summary.results[0].transient is False


def test_capped_products_never_enter_a_second_round(tmp_path: Path):
    # CAPPED_12PCT is the hourly recheck's job, not the error rounds'.
    client = FakePlatformClient()
    client.update_price_errors["V-x"] = [
        DigikalaReferencePriceRejected("rejected", retryable=False) for _ in range(5)
    ]
    service = _rounds_service(tmp_path, _eligible("x"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 1)]
    assert summary.results[0].status == OutcomeStatus.CAPPED_12PCT
    assert len(client.update_calls) == 1


def test_a_permanent_failure_is_left_alone_while_a_transient_one_is_retried(tmp_path: Path):
    client = FakePlatformClient()
    client.update_price_errors["V-bad"] = [PlatformAPIError("HTTP 404", status_code=404)] * 5
    client.update_price_errors["V-flaky"] = [_err429(), None]
    service = _rounds_service(tmp_path, _eligible("ok", "bad", "flaky"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 3), (2, 1)]
    by_id = {r.product.internal_id: r for r in summary.results}
    assert by_id["ok"].status == OutcomeStatus.SUCCESS
    assert by_id["flaky"].status == OutcomeStatus.SUCCESS
    assert by_id["bad"].status == OutcomeStatus.FAILED_API
    # "bad" was called once in total; "flaky" twice.
    assert [v for v, _ in client.update_calls].count("V-bad") == 1
    assert [v for v, _ in client.update_calls].count("V-flaky") == 2


def test_retry_rounds_only_call_the_failed_products_not_the_whole_list(tmp_path: Path):
    client = FakePlatformClient()
    for pid in ("b", "e"):
        client.update_price_errors[f"V-{pid}"] = [_err429(), None]
    service = _rounds_service(tmp_path, _eligible(*"abcdef"), client)

    started: list[str] = []
    summary, rounds = _run_with_rounds(
        service, on_product_start=lambda p: started.append(p.internal_id)
    )

    assert rounds == [(1, 6), (2, 2)]
    # Round 1 is the full list in sheet order; round 2 is just b and e.
    assert started == ["a", "b", "c", "d", "e", "f", "b", "e"]
    assert [v for v, _ in client.update_calls] == [
        "V-a",
        "V-b",
        "V-c",
        "V-d",
        "V-e",
        "V-f",
        "V-b",
        "V-e",
    ]
    assert len(summary.results) == 6


def test_cancel_during_the_cooldown_between_rounds_stops_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Cooldown is 60s but is slept in 1s slices with should_cancel checked
    # before each one. The fake sleep records every slice (and never really
    # sleeps); cancel flips after two slices.
    client = FakePlatformClient()
    for pid in ("a", "b"):
        client.update_price_errors[f"V-{pid}"] = [_err429(), None]
    service = _rounds_service(tmp_path, _eligible("a", "b", "c"), client, cooldown=60)

    sleeps: list[float] = []
    monkeypatch.setattr("faraz_honar.update.service.time.sleep", sleeps.append)

    summary, rounds = _run_with_rounds(service, should_cancel=lambda: len(sleeps) >= 2)

    assert sleeps == [1.0, 1.0]  # sliced, and it did not wait out the other 58s
    assert rounds == [(1, 3)]  # round 2 never started
    assert len(client.update_calls) == 3
    # What round 1 reached is still reported (a and b stay failed), and the run
    # is closed out properly.
    assert sorted(_ids(summary.failed)) == ["a", "b"]
    assert _ids(summary.successful) == ["c"]
    runs, rows = _db_rows(tmp_path)
    assert len(runs) == 1 and runs[0]["finished_at"] is not None
    assert len(rows) == 3


def test_cancel_already_requested_skips_the_cooldown_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = FakePlatformClient()
    client.update_price_errors["V-a"] = [_err429(), None]
    service = _rounds_service(tmp_path, _eligible("a"), client, cooldown=60)

    sleeps: list[float] = []
    monkeypatch.setattr("faraz_honar.update.service.time.sleep", sleeps.append)
    done: list[str] = []

    summary, rounds = _run_with_rounds(
        service,
        on_result=lambda r: done.append(r.product.internal_id),
        should_cancel=lambda: bool(done),  # cancel as soon as round 1 is over
    )

    assert sleeps == []
    assert rounds == [(1, 1)]
    assert summary.results[0].status == OutcomeStatus.FAILED_API


def test_cancel_between_products_still_works_inside_a_retry_round(tmp_path: Path):
    client = FakePlatformClient()
    client.update_price_errors["V-a"] = [_err429(), None]
    client.update_price_errors["V-b"] = [_err429(), None]
    client.update_price_errors["V-c"] = [_err429(), None]
    service = _rounds_service(tmp_path, _eligible("a", "b", "c"), client)

    seen: list[str] = []
    summary, rounds = _run_with_rounds(
        service,
        on_result=lambda r: seen.append(r.product.internal_id),
        # Stop once "a" has been processed a second time (in round 2): "b" and
        # "c" must not be retried, but "a" finishes.
        should_cancel=lambda: seen.count("a") >= 2,
    )

    assert rounds == [(1, 3), (2, 3)]
    assert [v for v, _ in client.update_calls] == ["V-a", "V-b", "V-c", "V-a"]
    by_id = {r.product.internal_id: r for r in summary.results}
    assert by_id["a"].status == OutcomeStatus.SUCCESS
    assert by_id["b"].status == OutcomeStatus.FAILED_API
    assert by_id["c"].status == OutcomeStatus.FAILED_API
    runs, rows = _db_rows(tmp_path)
    assert len(runs) == 1 and runs[0]["finished_at"] is not None
    assert len(rows) == 3


def test_error_rounds_write_one_run_and_one_result_row_per_product(tmp_path: Path):
    # Same shape as the 5 -> 2 -> 0 scenario: 3 rounds, but the database only
    # ever sees ONE update_runs row and ONE run_product_results row per product,
    # holding the final (successful) outcome - not one row per attempt.
    client = FakePlatformClient()
    for pid in ("a", "b", "c"):
        client.update_price_errors[f"V-{pid}"] = [_err429(), None]
    for pid in ("d", "e"):
        client.update_price_errors[f"V-{pid}"] = [_err429(), _err429(), None]
    service = _rounds_service(tmp_path, _eligible(*"abcdefgh"), client)

    summary, rounds = _run_with_rounds(service)

    assert len(rounds) == 3
    runs, rows = _db_rows(tmp_path)
    assert len(runs) == 1
    assert runs[0]["platform"] == "digikala"
    assert runs[0]["finished_at"] is not None
    assert (runs[0]["success_count"], runs[0]["capped_count"], runs[0]["failed_count"]) == (8, 0, 0)
    assert sorted(r["product_id"] for r in rows) == sorted("abcdefgh")  # no duplicates
    assert {r["run_id"] for r in rows} == {runs[0]["id"]}
    assert {r["status"] for r in rows} == {OutcomeStatus.SUCCESS.value}


def test_run_rows_hold_the_final_failure_for_products_that_stay_broken(tmp_path: Path):
    client = FakePlatformClient()
    client.update_price_errors["V-a"] = [_err429(), None]
    for pid in ("b", "c"):
        client.update_price_errors[f"V-{pid}"] = [_err429() for _ in range(10)]
    service = _rounds_service(tmp_path, _eligible("a", "b", "c"), client)

    summary, _ = _run_with_rounds(service)

    runs, rows = _db_rows(tmp_path)
    assert len(runs) == 1
    assert (runs[0]["success_count"], runs[0]["failed_count"]) == (1, 2)
    assert {r["product_id"]: r["status"] for r in rows} == {
        "a": OutcomeStatus.SUCCESS.value,
        "b": OutcomeStatus.FAILED_API.value,
        "c": OutcomeStatus.FAILED_API.value,
    }
    assert len(summary.results) == 3
    # product_state always holds the latest outcome, too.
    repo = StateRepository(tmp_path / "state.db")
    assert repo.get_product_state("a", "digikala").status == OutcomeStatus.SUCCESS.value
    assert repo.get_product_state("b", "digikala").status == OutcomeStatus.FAILED_API.value


def test_two_products_sharing_the_same_internal_id_keep_separate_results(tmp_path: Path):
    # internal_id is the sheet's name cell and is not guaranteed unique; both
    # rows must still produce their own result across rounds.
    client = FakePlatformClient()
    client.update_price_errors["V-x"] = [_err429(), _err429(), None, None]
    service = _rounds_service(tmp_path, _eligible("x", "x"), client)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 2), (2, 2)]
    assert len(summary.results) == 2
    assert all(r.status == OutcomeStatus.SUCCESS for r in summary.results)
    _, rows = _db_rows(tmp_path)
    assert len(rows) == 2


def test_a_crash_mid_round_still_records_one_row_per_product_and_closes_the_run(tmp_path: Path):
    client = FakePlatformClient()
    client.update_price_errors["V-a"] = [_err429(), None]
    service = _rounds_service(tmp_path, _eligible("a", "b"), client)

    def boom(result: Any) -> None:
        if result.product.internal_id == "a" and result.is_success:
            raise RuntimeError("hook crashed")

    with pytest.raises(RuntimeError, match="hook crashed"):
        service.run(["digikala"], triggered_by="test", on_result=boom)

    runs, rows = _db_rows(tmp_path)
    assert len(runs) == 1 and runs[0]["finished_at"] is not None
    assert sorted(r["product_id"] for r in rows) == ["a", "b"]


def test_error_rounds_disabled_is_a_single_pass_like_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = FakePlatformClient()
    client.update_price_errors["V-a"] = [_err429(), None]  # would recover in round 2
    service = _rounds_service(tmp_path, _eligible("a", "b"), client, enabled=False, cooldown=60)

    def no_sleep(_: float) -> None:
        raise AssertionError("no cooldown may happen when error rounds are disabled")

    monkeypatch.setattr("faraz_honar.update.service.time.sleep", no_sleep)

    summary, rounds = _run_with_rounds(service)

    assert rounds == [(1, 2)]
    assert summary.error_rounds == 0
    assert len(client.update_calls) == 2  # one call per product, no retry round
    by_id = {r.product.internal_id: r for r in summary.results}
    assert by_id["a"].status == OutcomeStatus.FAILED_API
    assert by_id["b"].status == OutcomeStatus.SUCCESS
    runs, rows = _db_rows(tmp_path)
    assert len(runs) == 1 and len(rows) == 2


def test_error_rounds_are_enabled_by_default(tmp_path: Path):
    client = FakePlatformClient()
    client.update_price_errors["V-a"] = [_err429(), None]
    service = PriceUpdateService(
        product_source=FakeProductSource(_eligible("a")),
        platform_registry={"digikala": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        error_rounds_cooldown_seconds=0,  # only the cooldown is overridden
    )

    summary = service.run(["digikala"], triggered_by="test")["digikala"]

    assert summary.results[0].status == OutcomeStatus.SUCCESS
    assert summary.error_rounds == 1


def test_rerun_specific_products_also_gets_error_rounds(tmp_path: Path):
    # The "only failed" button goes through the same _run_products.
    client = FakePlatformClient()
    client.update_price_errors["V-b"] = [_err429(), None]
    service = _rounds_service(tmp_path, _eligible("a", "b", "c"), client)

    summaries = service.rerun_specific_products("digikala", ["b"], triggered_by="test")

    assert [r.status for r in summaries.results] == [OutcomeStatus.SUCCESS]
    assert summaries.error_rounds == 1


# ---------------------------------------------------------------------------
# Read-only price check: plan_price_check / check_prices
# ---------------------------------------------------------------------------


class FakeCheckPlatformClient(PlatformClient):
    """Read-only fake for the price check. Unlike FakePlatformClient its name
    is per instance, so two of them can sit in one registry. ``update_price``
    always fails and is recorded: the check must never reach it."""

    def __init__(self, name: str, prices: dict[str, int] | None = None):
        self.name = name
        self.prices = prices or {}
        self.price_errors: dict[str, Exception] = {}
        self.prepare_error: Exception | None = None
        self.update_calls: list[tuple[str, int]] = []
        self.threads: set[str] = set()
        # When set, every price read waits here until BOTH platforms are
        # reading at the same moment -- impossible if they ran one after another.
        self.rendezvous: threading.Barrier | None = None

    def prepare_price_check(self) -> None:
        if self.prepare_error is not None:
            raise self.prepare_error

    def resolve_variant_id(self, product: Product) -> str:
        return "V-" + product.internal_id

    def get_reference_price(self, product: Product, variant_id: str) -> int:
        self.threads.add(threading.current_thread().name)
        if self.rendezvous is not None:
            self.rendezvous.wait(timeout=5)
        error = self.price_errors.get(product.internal_id)
        if error is not None:
            raise error
        return self.prices[product.internal_id]

    def update_price(self, variant_id: str, new_price: int) -> None:
        self.update_calls.append((variant_id, new_price))
        raise AssertionError("a price check must never call update_price")


def _check_product(internal_id: str, *platforms: str, target: int | None = 1_100_000) -> Product:
    return Product(
        internal_id=internal_id,
        name=f"Product {internal_id}",
        reference_price=1_000_000,
        target_price=target,
        platform_refs={p: f"{p}-{internal_id}" for p in platforms},
        # Price-checked only with "اسم جدید" filled and a GREEN platform cell.
        display_name=f"Product {internal_id}",
        green_platforms=frozenset(platforms),
    )


def _check_service(
    tmp_path: Path,
    clients: dict[str, PlatformClient],
    *,
    state_repository: Any = None,
    product_source: Any = None,
) -> PriceUpdateService:
    return PriceUpdateService(
        product_source=product_source or FakeProductSource([]),
        platform_registry=clients,
        state_repository=state_repository or StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        error_rounds_enabled=False,
    )


def _selections(
    service: PriceUpdateService, products: list[Product], platforms: list[str]
) -> dict[str, CheckSelection]:
    return service.plan_price_check(platforms, products)


def test_check_prices_runs_two_platforms_at_the_same_time(tmp_path: Path):
    products = [_check_product("a", "digikala", "basalam"), _check_product("b", "digikala", "basalam")]
    prices = {"a": 1_100_000, "b": 1_100_000}
    digikala = FakeCheckPlatformClient("digikala", prices)
    basalam = FakeCheckPlatformClient("basalam", prices)
    barrier = threading.Barrier(2)
    digikala.rendezvous = basalam.rendezvous = barrier
    service = _check_service(tmp_path, {"digikala": digikala, "basalam": basalam})

    results = service.check_prices(_selections(service, products, ["digikala", "basalam"]))

    # Had they run one after another, the first read would have timed out
    # waiting for the other platform and become an ERROR entry.
    assert [e.status for e in results["digikala"].entries] == [CheckStatus.MATCH] * 2
    assert [e.status for e in results["basalam"].entries] == [CheckStatus.MATCH] * 2
    assert digikala.threads.isdisjoint(basalam.threads)  # one thread per platform


def test_check_prices_one_platform_failing_does_not_affect_the_other(tmp_path: Path):
    products = [_check_product("a", "digikala", "basalam"), _check_product("b", "digikala", "basalam")]
    prices = {"a": 1_100_000, "b": 1_100_000}
    digikala = FakeCheckPlatformClient("digikala", prices)
    basalam = FakeCheckPlatformClient("basalam", prices)
    digikala.prepare_error = RuntimeError("digikala catalog unreachable")
    service = _check_service(tmp_path, {"digikala": digikala, "basalam": basalam})

    results = service.check_prices(_selections(service, products, ["digikala", "basalam"]))

    assert results["digikala"].platform_error == "digikala catalog unreachable"
    assert results["digikala"].entries == []
    assert results["basalam"].platform_error is None
    assert [e.status for e in results["basalam"].entries] == [CheckStatus.MATCH] * 2


def test_check_prices_a_product_error_on_one_platform_stays_on_that_platform(tmp_path: Path):
    products = [_check_product("a", "digikala", "basalam"), _check_product("b", "digikala", "basalam")]
    prices = {"a": 1_100_000, "b": 1_100_000}
    digikala = FakeCheckPlatformClient("digikala", prices)
    basalam = FakeCheckPlatformClient("basalam", prices)
    digikala.price_errors["a"] = PlatformAPIError("HTTP 429", status_code=429, retryable=False)
    service = _check_service(tmp_path, {"digikala": digikala, "basalam": basalam})

    results = service.check_prices(_selections(service, products, ["digikala", "basalam"]))

    assert [e.product.internal_id for e in results["digikala"].errors] == ["a"]
    assert [e.product.internal_id for e in results["digikala"].matched] == ["b"]
    assert results["basalam"].errors == []
    assert len(results["basalam"].matched) == 2


def test_check_prices_cancel_is_decided_per_platform(tmp_path: Path):
    products = [_check_product(str(i), "digikala", "basalam") for i in range(3)]
    prices = {p.internal_id: 1_100_000 for p in products}
    digikala = FakeCheckPlatformClient("digikala", prices)
    basalam = FakeCheckPlatformClient("basalam", prices)
    service = _check_service(tmp_path, {"digikala": digikala, "basalam": basalam})

    # Only digikala is stopped; basalam must carry on to the end.
    results = service.check_prices(
        _selections(service, products, ["digikala", "basalam"]),
        should_cancel=lambda platform: platform == "digikala",
    )

    assert results["digikala"].cancelled is True
    assert results["digikala"].entries == []
    assert results["basalam"].cancelled is False
    assert len(results["basalam"].entries) == 3


def test_check_prices_routes_progress_callbacks_with_the_right_platform(tmp_path: Path):
    only_digikala = _check_product("a", "digikala")
    only_basalam = _check_product("b", "basalam")
    digikala = FakeCheckPlatformClient("digikala", {"a": 1_100_000})
    basalam = FakeCheckPlatformClient("basalam", {"b": 1_100_000})
    service = _check_service(tmp_path, {"digikala": digikala, "basalam": basalam})
    started: list[tuple[str, str]] = []
    entries: list[tuple[str, str]] = []

    service.check_prices(
        _selections(service, [only_digikala, only_basalam], ["digikala", "basalam"]),
        on_product_start=lambda platform, product: started.append((platform, product.internal_id)),
        on_entry=lambda entry: entries.append((entry.platform, entry.product.internal_id)),
    )

    assert sorted(started) == [("basalam", "b"), ("digikala", "a")]
    assert sorted(entries) == [("basalam", "b"), ("digikala", "a")]


def test_check_prices_with_no_selections_returns_nothing_and_touches_no_platform(tmp_path: Path):
    digikala = FakeCheckPlatformClient("digikala")
    digikala.prepare_error = RuntimeError("must not even be prepared")
    service = _check_service(tmp_path, {"digikala": digikala})

    assert service.check_prices({}) == {}


def _state_by_product(repository: StateRepository, platform: str = "digikala") -> dict[str, Any]:
    return {r.product_id: r for r in repository.list_all_product_states() if r.platform == platform}


def test_check_prices_records_each_result_in_product_state(tmp_path: Path):
    from faraz_honar.core.exceptions import VariantResolutionError

    products = [
        _check_product("ok", "digikala"),
        _check_product("diff", "digikala"),
        _check_product("unreadable", "digikala"),
        _check_product("missing", "digikala"),
    ]
    digikala = FakeCheckPlatformClient(
        "digikala", {"ok": 1_100_000, "diff": 7_000_000, "unreadable": 1, "missing": 1}
    )
    digikala.price_errors["unreadable"] = PlatformAPIError("HTTP 429: Too Many Requests", retryable=False)
    digikala.price_errors["missing"] = VariantResolutionError("No variant found", retryable=False)
    repository = StateRepository(tmp_path / "state.db")
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)

    service.check_prices(service.plan_price_check(["digikala"], products))

    states = _state_by_product(repository)
    assert states["ok"].status == OutcomeStatus.SKIPPED_NO_CHANGE.value
    assert states["diff"].status == OutcomeStatus.FAILED_PRICE_MISMATCH.value
    assert "700,000 Toman" in states["diff"].last_error and "110,000 Toman" in states["diff"].last_error
    assert states["unreadable"].status == OutcomeStatus.FAILED_API.value
    assert "429" in states["unreadable"].last_error
    assert states["missing"].status == OutcomeStatus.FAILED_MISSING_IDENTIFIER.value
    assert digikala.update_calls == []  # still never writes a price to the platform


def test_check_prices_does_not_create_a_run_or_touch_run_history(tmp_path: Path):
    products = [_check_product("a", "digikala")]
    digikala = FakeCheckPlatformClient("digikala", {"a": 5})
    repository = StateRepository(tmp_path / "state.db")
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)

    service.check_prices(service.plan_price_check(["digikala"], products))

    runs, rows = _db_rows(tmp_path)
    assert runs == [] and rows == []  # update_runs / run_product_results untouched
    assert repository.list_recent_runs("digikala") == []
    assert len(repository.list_all_product_states()) == 1  # only product_state follows the check


def test_check_overrides_an_earlier_update_success_when_the_live_price_differs(tmp_path: Path):
    # The WooCommerce case: the update run recorded SUCCESS (request accepted)
    # but the real price on the platform is wrong.
    from faraz_honar.core.models import UpdateResult

    product = _check_product("a", "digikala")
    repository = StateRepository(tmp_path / "state.db")
    repository.record_result(
        UpdateResult(product=product, platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=1, applied_price=1_100_000, target_price=1_100_000)
    )
    digikala = FakeCheckPlatformClient("digikala", {"a": 1})
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)

    service.check_prices(service.plan_price_check(["digikala"], [product]))

    assert _state_by_product(repository)["a"].status == OutcomeStatus.FAILED_PRICE_MISMATCH.value


def test_check_keeps_a_capped_product_capped_until_its_price_matches(tmp_path: Path):
    from faraz_honar.core.models import UpdateResult

    still_capped = _check_product("capped", "digikala")
    now_fine = _check_product("fine", "digikala")
    repository = StateRepository(tmp_path / "state.db")
    for product in (still_capped, now_fine):
        repository.record_result(
            UpdateResult(product=product, platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
                         previous_price=1_000_000, applied_price=1_120_000, target_price=2_000_000)
        )
    digikala = FakeCheckPlatformClient("digikala", {"capped": 1_120_000, "fine": 1_100_000})
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)

    service.check_prices(service.plan_price_check(["digikala"], [still_capped, now_fine]))

    states = _state_by_product(repository)
    assert states["capped"].status == OutcomeStatus.CAPPED_12PCT.value  # still needs the ticket
    assert states["capped"].is_capped is True
    assert states["fine"].status == OutcomeStatus.SKIPPED_NO_CHANGE.value
    assert states["fine"].is_capped is False


def test_check_with_duplicate_product_names_records_the_worst_outcome(tmp_path: Path):
    first = _check_product("same", "digikala")
    second = dataclasses.replace(first)  # a second sheet row with the same name
    digikala = FakeCheckPlatformClient("digikala", {"same": 1_100_000})
    repository = StateRepository(tmp_path / "state.db")
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)
    selections = service.plan_price_check(["digikala"], [first, second])

    # One row matches, the other does not: the failure must not be hidden.
    prices = iter([1_100_000, 5])
    digikala.get_reference_price = lambda product, variant_id: next(prices)  # type: ignore[method-assign]
    service.check_prices(selections)

    assert _state_by_product(repository)["same"].status == OutcomeStatus.FAILED_PRICE_MISMATCH.value


def test_check_of_a_platform_that_could_not_be_read_leaves_its_state_untouched(tmp_path: Path):
    from faraz_honar.core.models import UpdateResult

    product = _check_product("a", "digikala")
    repository = StateRepository(tmp_path / "state.db")
    repository.record_result(
        UpdateResult(product=product, platform="digikala", status=OutcomeStatus.SUCCESS,
                     previous_price=1, applied_price=1_100_000, target_price=1_100_000)
    )
    digikala = FakeCheckPlatformClient("digikala", {"a": 5})
    digikala.prepare_error = RuntimeError("catalog unreachable")
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)

    results = service.check_prices(service.plan_price_check(["digikala"], [product]))

    assert results["digikala"].platform_error
    assert _state_by_product(repository)["a"].status == OutcomeStatus.SUCCESS.value


def test_a_state_storage_failure_never_breaks_the_check(tmp_path: Path):
    products = [_check_product("a", "digikala")]
    digikala = FakeCheckPlatformClient("digikala", {"a": 1_100_000})
    repository = MagicMock(spec=StateRepository)
    repository.record_result.side_effect = RuntimeError("database is locked")
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)

    results = service.check_prices(service.plan_price_check(["digikala"], products))

    assert [e.status for e in results["digikala"].entries] == [CheckStatus.MATCH]


def test_excel_summary_counts_equal_the_check_report_counts(tmp_path: Path):
    # After a check, "📊 آخرین وضعیت" shows exactly the check's موفق / ناموفق.
    from faraz_honar.reporting.status_matrix import build_status_matrix
    from faraz_honar.reporting.xlsx_export import _counts_for_platform

    products = [_check_product(name, "digikala") for name in ("a", "b", "c", "d", "e")]
    prices = {"a": 1_100_000, "b": 1_100_000, "c": 5, "d": 9, "e": 1_100_000}
    digikala = FakeCheckPlatformClient("digikala", prices)
    digikala.price_errors["e"] = PlatformAPIError("HTTP 503", retryable=False)
    repository = StateRepository(tmp_path / "state.db")
    service = _check_service(tmp_path, {"digikala": digikala}, state_repository=repository)

    result = service.check_prices(service.plan_price_check(["digikala"], products))["digikala"]
    matrix = build_status_matrix(products, repository.list_all_product_states(), platforms=["digikala"])
    success, failure, not_run = _counts_for_platform(matrix, "digikala")

    assert (success, failure, not_run) == (len(result.matched), len(result.mismatched) + len(result.errors), 0)
    assert success + failure == len(result.entries)


def test_plan_price_check_drops_a_platform_with_nothing_checkable(tmp_path: Path):
    products = [
        _check_product("a", "digikala"),  # checkable on digikala
        _check_product("b", "basalam", target=None),  # has a code but no sheet price
        Product(  # code present but its cell is NOT green (here: shaded purple/red)
            internal_id="c",
            name="Product c",
            reference_price=1,
            target_price=1_100_000,
            platform_refs={"digikala2": "digikala2-c"},
            display_name="Product c",
            color_excluded_platforms=frozenset({"digikala2"}),
        ),
    ]
    clients = {
        name: FakeCheckPlatformClient(name) for name in ("digikala", "basalam", "digikala2")
    }
    service = _check_service(tmp_path, clients)

    selections = service.plan_price_check(["digikala", "basalam", "digikala2"], products)

    assert list(selections) == ["digikala"]
    assert [p.internal_id for p in selections["digikala"].products] == ["a"]


def test_plan_price_check_keeps_the_requested_platform_order_and_per_platform_products(
    tmp_path: Path,
):
    products = [
        _check_product("a", "digikala", "basalam"),
        _check_product("b", "basalam"),
        _check_product("c", "digikala"),
    ]
    clients = {name: FakeCheckPlatformClient(name) for name in ("digikala", "basalam")}
    service = _check_service(tmp_path, clients)

    selections = service.plan_price_check(["basalam", "digikala"], products)

    assert list(selections) == ["basalam", "digikala"]
    assert [p.internal_id for p in selections["basalam"].products] == ["a", "b"]
    assert [p.internal_id for p in selections["digikala"].products] == ["a", "c"]


def test_plan_price_check_returns_nothing_when_no_platform_has_a_checkable_product(
    tmp_path: Path,
):
    service = _check_service(tmp_path, {"digikala": FakeCheckPlatformClient("digikala")})

    assert service.plan_price_check(["digikala"], []) == {}
    assert service.plan_price_check(["digikala"], [_check_product("a", "digikala", target=None)]) == {}


def test_plan_price_check_rejects_an_unknown_platform(tmp_path: Path):
    service = _check_service(tmp_path, {"digikala": FakeCheckPlatformClient("digikala")})

    with pytest.raises(ConfigurationError):
        service.plan_price_check(["nope"], [_check_product("a", "digikala")])
