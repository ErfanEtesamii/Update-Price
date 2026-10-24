"""Regression tests proving every platform's execution/result/report state
is strictly isolated from every other platform's — the exact guarantee
requested after a reported (but, after thorough investigation, not
reproduced in this codebase) cross-platform leak between digikala and
digikala2.

Two layers are tested:
  1. Unit tests on the new hard invariant guards themselves
     (RunSummary.add_result / JobProgress.record — core/models.py,
     telegram_bot/state.py) — proving a mismatched platform is REJECTED,
     never silently accepted.
  2. Integration tests through the real PriceUpdateService (sequential AND
     genuinely concurrent, via real threads) across all four platforms
     (digikala, digikala2, snappshop_tehran, snappshop_isfahan),
     proving results/changed-product lists/manual-review lists/final
     reports never mix between platforms, and that same-platform
     duplicate-job protection is preserved while different platforms
     remain independently runnable.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from faraz_honar.core.exceptions import CrossPlatformStateError
from faraz_honar.core.models import OutcomeStatus, Product, RunSummary, UpdateResult
from faraz_honar.core.pricing import CAPPED_PLATFORMS
from faraz_honar.platforms.base import PlatformClient
from faraz_honar.sheets.base import ProductSource
from faraz_honar.state.repository import StateRepository
from faraz_honar.telegram_bot.reporting import format_final_digikala_report
from faraz_honar.telegram_bot.state import JobProgress, PlatformJobManager
from faraz_honar.update.service import PriceUpdateService

PLATFORMS = ["digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan"]


# -- Layer 1: the hard invariant guards themselves ----------------------------------


def _result(platform: str, name: str = "p1") -> UpdateResult:
    return UpdateResult(
        product=Product(internal_id=name, name=name, reference_price=1_000_000, target_price=1_100_000),
        platform=platform,
        status=OutcomeStatus.SUCCESS,
    )


def test_run_summary_rejects_a_result_from_a_different_platform():
    summary = RunSummary(platform="digikala", triggered_by="test")
    with pytest.raises(CrossPlatformStateError):
        summary.add_result(_result("digikala2"))
    assert summary.results == []  # rejected, never partially added


def test_run_summary_accepts_a_matching_result():
    summary = RunSummary(platform="digikala2", triggered_by="test")
    summary.add_result(_result("digikala2"))
    assert len(summary.results) == 1


def test_job_progress_rejects_a_result_from_a_different_platform():
    progress = JobProgress(platform="snappshop_tehran", triggered_by="test")
    with pytest.raises(CrossPlatformStateError):
        progress.record(_result("snappshop_isfahan"))
    assert progress.processed == 0  # rejected, counters untouched


def test_job_progress_accepts_a_matching_result():
    progress = JobProgress(platform="snappshop_isfahan", triggered_by="test")
    progress.record(_result("snappshop_isfahan"))
    assert progress.processed == 1
    assert progress.success == 1


# -- Layer 2: real PriceUpdateService, all four platforms ---------------------------


class FakeProductSource(ProductSource):
    def __init__(self, products: list[Product]):
        self._products = products

    def fetch_products(self) -> list[Product]:
        return list(self._products)


class FakePlatformClient(PlatformClient):
    """Deterministic, per-platform fake — proves nothing about a DIFFERENT
    platform's client instance is ever consulted for this one's products.
    """

    def __init__(self, name: str, *, delay_seconds: float = 0.0, fail: bool = False):
        self.name = name
        self._delay = delay_seconds
        self._fail = fail
        self.update_calls: list[tuple[str, int]] = []

    def resolve_variant_id(self, product: Product) -> str:
        import time

        time.sleep(self._delay)
        return f"V-{self.name}-{product.internal_id}"

    def get_reference_price(self, product: Product, variant_id: str) -> int:
        assert product.reference_price is not None
        return product.reference_price

    def update_price(self, variant_id: str, new_price: int) -> None:
        import time

        time.sleep(self._delay)
        if self._fail:
            # Not retryable per core.retry.is_retryable (a plain ValueError
            # isn't a PlatformAPIError/TimeoutError/ConnectionError), so this
            # yields exactly one FAILED_UNEXPECTED result per product —
            # still eligible (has a target price and a platform ref) and
            # still reaches the client, unlike a missing target price, which
            # PriceUpdateService.run() filters out before any client call.
            raise ValueError(f"simulated failure for {variant_id}")
        self.update_calls.append((variant_id, new_price))


def _product(platform: str, idx: int | str, **overrides: Any) -> Product:
    defaults: dict[str, Any] = dict(
        internal_id=f"{platform}-item-{idx}",
        name=f"{platform.title()} Item {idx}",
        reference_price=1_000_000,
        target_price=1_100_000,
        platform_refs={platform: f"ref-{platform}-{idx}"},
    )
    defaults.update(overrides)
    # Eligibility needs "اسم جدید" and a GREEN code cell per platform.
    defaults.setdefault("display_name", defaults["name"])
    defaults.setdefault("green_platforms", frozenset(defaults["platform_refs"]))
    return Product(**defaults)


def _all_platform_products(count_per_platform: int = 5) -> list[Product]:
    products: list[Product] = []
    for platform in PLATFORMS:
        for i in range(count_per_platform):
            products.append(_product(platform, i))
    return products


def _service(
    tmp_path: Path, clients: dict[str, FakePlatformClient], products: list[Product]
) -> PriceUpdateService:
    return PriceUpdateService(
        product_source=FakeProductSource(products),
        platform_registry=clients,  # type: ignore[arg-type]
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )


def _clients(**delays: float) -> dict[str, FakePlatformClient]:
    return {p: FakePlatformClient(p, delay_seconds=delays.get(p, 0.0)) for p in PLATFORMS}


def _failing_clients() -> dict[str, FakePlatformClient]:
    return {p: FakePlatformClient(p, fail=True) for p in PLATFORMS}


# -- 1/2: digikala cannot add to digikala2's report and vice versa (sequential) --------------


def test_digikala_run_cannot_add_results_to_digikala2_report(tmp_path: Path):
    clients = _clients()
    products = _all_platform_products()
    service = _service(tmp_path, clients, products)

    digikala_summary = service.run(["digikala"], triggered_by="test")["digikala"]
    digikala2_summary = service.run(["digikala2"], triggered_by="test")["digikala2"]

    digikala_ids = {r.product.internal_id for r in digikala_summary.results}
    digikala2_ids = {r.product.internal_id for r in digikala2_summary.results}
    assert digikala_ids == {f"digikala-item-{i}" for i in range(5)}
    assert digikala2_ids == {f"digikala2-item-{i}" for i in range(5)}
    assert digikala_ids.isdisjoint(digikala2_ids)
    assert all(r.platform == "digikala" for r in digikala_summary.results)
    assert all(r.platform == "digikala2" for r in digikala2_summary.results)


def test_digikala2_run_cannot_add_results_to_digikala_report(tmp_path: Path):
    # Same proof, reversed run order — the earlier-run platform's report
    # must stay untouched by whichever platform runs next, in either order.
    clients = _clients()
    products = _all_platform_products()
    service = _service(tmp_path, clients, products)

    digikala2_summary = service.run(["digikala2"], triggered_by="test")["digikala2"]
    digikala_summary = service.run(["digikala"], triggered_by="test")["digikala"]

    assert {r.product.internal_id for r in digikala2_summary.results} == {
        f"digikala2-item-{i}" for i in range(5)
    }
    assert {r.product.internal_id for r in digikala_summary.results} == {
        f"digikala-item-{i}" for i in range(5)
    }


# -- 3: SnappShop Tehran and Isfahan remain isolated ---------------------------------


def test_snappshop_tehran_and_isfahan_results_remain_isolated(tmp_path: Path):
    clients = _clients()
    products = _all_platform_products()
    service = _service(tmp_path, clients, products)

    tehran_summary = service.run(["snappshop_tehran"], triggered_by="test")["snappshop_tehran"]
    isfahan_summary = service.run(["snappshop_isfahan"], triggered_by="test")["snappshop_isfahan"]

    tehran_ids = {r.product.internal_id for r in tehran_summary.results}
    isfahan_ids = {r.product.internal_id for r in isfahan_summary.results}
    assert tehran_ids == {f"snappshop_tehran-item-{i}" for i in range(5)}
    assert isfahan_ids == {f"snappshop_isfahan-item-{i}" for i in range(5)}
    assert tehran_ids.isdisjoint(isfahan_ids)


# -- 4/5: changed-product (successful) and manual-review (capped) lists isolated ----


def test_changed_and_manual_review_lists_are_isolated_per_platform(tmp_path: Path):
    clients = _clients()
    # One product per platform reachable within 12% (-> successful/"changed"),
    # one product per platform requiring >12% (-> capped/"manual review" for
    # DigiKala; the 12% cap only applies to DigiKala platforms, per
    # core/pricing.py's CAPPED_PLATFORMS, so for SnappShop this second
    # product also lands in successful/"changed" instead).
    products = []
    for platform in PLATFORMS:
        products.append(
            _product(platform, "ok", reference_price=1_000_000, target_price=1_050_000)
        )
        products.append(
            _product(platform, "capped", reference_price=1_000_000, target_price=2_000_000)
        )
    service = _service(tmp_path, clients, products)

    summaries = {p: service.run([p], triggered_by="test")[p] for p in PLATFORMS}

    for platform in PLATFORMS:
        changed_names = {r.product.internal_id for r in summaries[platform].successful}
        capped_names = {r.product.internal_id for r in summaries[platform].capped}
        if platform in CAPPED_PLATFORMS:
            assert changed_names == {f"{platform}-item-ok"}
            assert capped_names == {f"{platform}-item-capped"}
        else:
            assert changed_names == {f"{platform}-item-ok", f"{platform}-item-capped"}
            assert capped_names == set()
        # Never contains another platform's item, in either list.
        for other in PLATFORMS:
            if other == platform:
                continue
            assert f"{other}-item-ok" not in changed_names
            assert f"{other}-item-capped" not in changed_names
            assert f"{other}-item-ok" not in capped_names
            assert f"{other}-item-capped" not in capped_names


# -- 6: a second job starts with empty result state ----------------------------------


def test_second_job_for_a_different_platform_starts_with_empty_state(tmp_path: Path):
    clients = _clients()
    products = _all_platform_products()
    service = _service(tmp_path, clients, products)

    first = service.run(["digikala"], triggered_by="test")["digikala"]
    assert len(first.results) == 5

    # A fresh RunSummary/JobProgress for a different platform never inherits
    # anything from the first job's accumulated state.
    second = service.run(["digikala2"], triggered_by="test")["digikala2"]
    assert len(second.results) == 5
    assert second is not first
    assert {r.product.internal_id for r in second.results}.isdisjoint(
        {r.product.internal_id for r in first.results}
    )

    progress_manager = PlatformJobManager()
    p1 = progress_manager.try_start("digikala", triggered_by="test")
    assert p1 is not None
    p1.record(_result("digikala", "x"))
    assert p1.processed == 1
    progress_manager.finish("digikala")

    p2 = progress_manager.try_start("digikala", triggered_by="test")
    assert p2 is not None
    assert p2 is not p1
    assert p2.processed == 0  # brand-new JobProgress, not a continuation of p1


# -- 7: final reporting uses only the current job's results -------------------------


def test_final_report_uses_only_the_current_summary(tmp_path: Path):
    # The unified final report (reporting.format_final_digikala_report)
    # deliberately shows successful product names only as a count, so
    # isolation can't be observed through the successful-products path here.
    # Using clients whose update_price always raises forces a
    # FAILED_UNEXPECTED result per product instead — the product stays
    # ELIGIBLE (has both a target price and a platform ref, so it still
    # reaches the client; a missing target price would instead be filtered
    # out by PriceUpdateService.run()'s eligibility check before ever
    # producing a result) and its name IS listed (by name only) in the
    # report's "❌ کالاهای ناموفق" section, so the cross-platform leak this
    # test guards against is still fully observable in the rendered text.
    clients = _failing_clients()
    products = _all_platform_products()
    service = _service(tmp_path, clients, products)

    digikala_summary = service.run(["digikala"], triggered_by="test")["digikala"]
    digikala2_summary = service.run(["digikala2"], triggered_by="test")["digikala2"]

    digikala_report = format_final_digikala_report(digikala_summary, dry_run=False, platform_label="Digikala")
    digikala2_report = format_final_digikala_report(
        digikala2_summary, dry_run=False, platform_label="Digikala Saree"
    )

    digikala_names = {p.name for p in products if p.internal_id.startswith("digikala-item-")}
    digikala2_names = {p.name for p in products if p.internal_id.startswith("digikala2-item-")}
    for name in digikala_names:
        assert name in digikala_report
        assert name not in digikala2_report
    for name in digikala2_names:
        assert name in digikala2_report
        assert name not in digikala_report


# -- 8: same-platform duplicate protection preserved; different platforms free ------


def test_same_platform_duplicate_job_is_still_rejected():
    manager = PlatformJobManager()
    first = manager.try_start("digikala", triggered_by="a")
    assert first is not None
    second = manager.try_start("digikala", triggered_by="b")
    assert second is None  # duplicate rejected, unchanged behavior
    manager.finish("digikala")
    third = manager.try_start("digikala", triggered_by="c")
    assert third is not None  # free again after finish()


def test_different_platforms_run_independently_without_blocking_each_other():
    manager = PlatformJobManager()
    started = [manager.try_start(p, triggered_by="test") for p in PLATFORMS]
    assert all(p is not None for p in started)  # none blocked by another platform's lock
    assert len({id(p) for p in started}) == len(PLATFORMS)  # four distinct JobProgress objects
    for platform in PLATFORMS:
        manager.finish(platform)


# -- Real concurrency: all four platforms actually running at once ------------------


def test_all_four_platforms_running_concurrently_never_cross_contaminate(tmp_path: Path):
    # Deliberately different per-platform delays so completion order is
    # scrambled relative to start order — a genuine race, not just an
    # interleaved-by-luck sequence.
    clients = _clients(digikala=0.02, digikala2=0.005, snappshop_tehran=0.015, snappshop_isfahan=0.001)
    products = _all_platform_products(count_per_platform=10)
    service = _service(tmp_path, clients, products)

    results: dict[str, RunSummary] = {}
    errors: list[Exception] = []

    def run_platform(platform: str) -> None:
        try:
            results[platform] = service.run([platform], triggered_by="test")[platform]
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run_platform, args=(p,)) for p in PLATFORMS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    for platform in PLATFORMS:
        ids = {r.product.internal_id for r in results[platform].results}
        assert ids == {f"{platform}-item-{i}" for i in range(10)}
        assert all(r.platform == platform for r in results[platform].results)
        for other in PLATFORMS:
            if other == platform:
                continue
            assert ids.isdisjoint({f"{other}-item-{i}" for i in range(10)})
