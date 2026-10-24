"""Tests for update/price_check.py -- the read-only "چک و بررسی قیمت‌ها" engine.

No network and no real platform client: every test drives a scriptable fake,
so each branch (match / mismatch / retry / platform failure / cancel) is
exercised in isolation.
"""
from __future__ import annotations

from typing import Any

import pytest

from faraz_honar.core.exceptions import PlatformAPIError, VariantResolutionError
from faraz_honar.core.models import Product
from faraz_honar.platforms.base import PlatformClient
from faraz_honar.update.price_check import (
    CheckSelection,
    CheckStatus,
    check_one,
    check_platform,
    select_products_for_check,
)

PLATFORM = "digikala"


class FakeCheckClient(PlatformClient):
    """Scriptable read-only fake.

    * ``prices``: internal_id -> live platform price (Rial).
    * ``resolve_errors`` / ``price_errors``: internal_id -> a queue of
      exceptions raised one per call (``None`` = succeed on that call).
    * ``update_price`` ALWAYS fails and is recorded: a price check must never
      reach it, so any call shows up both in ``update_calls`` and as an
      unexpected ERROR entry.
    """

    name = PLATFORM

    def __init__(self, *, step: int = 1) -> None:
        self.price_step_rial = step
        self.prices: dict[str, int] = {}
        self.resolve_errors: dict[str, list[Exception | None]] = {}
        self.price_errors: dict[str, list[Exception | None]] = {}
        self.prepare_error: Exception | None = None
        self.finish_error: Exception | None = None
        self.resolve_calls: list[str] = []
        self.price_calls: list[str] = []
        self.update_calls: list[tuple[str, int]] = []
        self.prepare_calls = 0
        self.finish_calls = 0

    def resolve_variant_id(self, product: Product) -> str:
        self.resolve_calls.append(product.internal_id)
        queue = self.resolve_errors.get(product.internal_id)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error
        return "V-" + product.internal_id

    def get_reference_price(self, product: Product, variant_id: str) -> int:
        self.price_calls.append(product.internal_id)
        queue = self.price_errors.get(product.internal_id)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error
        return self.prices[product.internal_id]

    def update_price(self, variant_id: str, new_price: int) -> None:
        self.update_calls.append((variant_id, new_price))
        raise AssertionError("a price check must never call update_price")

    def prepare_price_check(self) -> None:
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error

    def finish_price_check(self) -> None:
        self.finish_calls += 1
        if self.finish_error is not None:
            raise self.finish_error


def _product(internal_id: str = "p1", **overrides: Any) -> Product:
    defaults: dict[str, Any] = dict(
        internal_id=internal_id,
        name=f"Product {internal_id}",
        reference_price=1_000_000,
        target_price=1_100_000,
        platform_refs={PLATFORM: f"ref-{internal_id}"},
        # A product is only price-checked with "اسم جدید" filled and a GREEN
        # platform cell (Product.is_check_eligible).
        display_name=f"Product {internal_id}",
        green_platforms=frozenset({PLATFORM}),
    )
    defaults.update(overrides)
    return Product(**defaults)


def _selection(
    *products: Product, no_target: int = 0, not_green: int = 0, no_display_name: int = 0
) -> CheckSelection:
    return CheckSelection(
        products=tuple(products),
        skipped_no_target=no_target,
        skipped_not_green=not_green,
        skipped_no_display_name=no_display_name,
    )


def _err5xx() -> PlatformAPIError:
    return PlatformAPIError("HTTP 503: service unavailable", status_code=503, retryable=True)


def _err429() -> PlatformAPIError:
    return PlatformAPIError("HTTP 429: rate limited", status_code=429, retryable=False)


def _not_found() -> VariantResolutionError:
    return VariantResolutionError("variant not found for this product")


# ---------------------------------------------------------------------------
# select_products_for_check
# ---------------------------------------------------------------------------


def test_select_skips_a_product_without_a_platform_code_and_does_not_count_it():
    no_ref = _product("a", platform_refs={})
    empty_ref = _product("b", platform_refs={PLATFORM: ""})
    other_platform_only = _product("c", platform_refs={"basalam": "x"})
    ok = _product("d")

    selection = select_products_for_check([no_ref, empty_ref, other_platform_only, ok], PLATFORM)

    assert [p.internal_id for p in selection.products] == ["d"]
    # Not having a code on this platform is "not part of this platform", not a
    # skip worth reporting.
    assert selection.skipped_no_target == 0
    assert selection.skipped_not_green == 0
    assert selection.skipped_no_display_name == 0


def test_select_puts_a_product_without_a_sheet_price_into_skipped_no_target():
    no_target = _product("a", target_price=None)
    ok = _product("b")

    selection = select_products_for_check([no_target, ok], PLATFORM)

    assert [p.internal_id for p in selection.products] == ["b"]
    assert selection.skipped_no_target == 1
    assert selection.skipped_not_green == 0


def test_select_puts_a_product_without_a_green_cell_into_skipped_not_green():
    not_green = _product("a", green_platforms=frozenset())
    ok = _product("b")

    selection = select_products_for_check([not_green, ok], PLATFORM)

    assert [p.internal_id for p in selection.products] == ["b"]
    assert selection.skipped_not_green == 1
    assert selection.skipped_no_target == 0


def test_select_does_not_check_a_product_without_a_new_name():
    no_name = _product("a", display_name=None)
    blank_name = _product("b", display_name="")
    ok = _product("c")

    selection = select_products_for_check([no_name, blank_name, ok], PLATFORM)

    assert [p.internal_id for p in selection.products] == ["c"]
    assert selection.skipped_no_display_name == 2


def test_select_green_applies_only_to_its_own_platform():
    green_elsewhere = _product(
        "a",
        platform_refs={PLATFORM: "ref-a", "basalam": "ref-a"},
        green_platforms=frozenset({"basalam"}),
    )

    on_digikala = select_products_for_check([green_elsewhere], PLATFORM)
    on_basalam = select_products_for_check([green_elsewhere], "basalam")

    assert on_digikala.products == ()
    assert on_digikala.skipped_not_green == 1
    assert [p.internal_id for p in on_basalam.products] == ["a"]
    assert on_basalam.skipped_not_green == 0


def test_select_a_purple_or_red_cell_is_simply_not_green():
    shaded = _product("a", green_platforms=frozenset(), color_excluded_platforms=frozenset({PLATFORM}))

    selection = select_products_for_check([shaded], PLATFORM)

    assert selection.products == ()
    assert selection.skipped_not_green == 1


def test_select_keeps_sheet_order_and_counts_every_skip_kind_once():
    products = [
        _product("1"),
        _product("2", target_price=None),
        _product("3", platform_refs={}),
        _product("4", green_platforms=frozenset()),
        _product("5", target_price=None),
        _product("6"),
        _product("7", display_name=None, target_price=None, green_platforms=frozenset()),
    ]

    selection = select_products_for_check(products, PLATFORM)

    assert [p.internal_id for p in selection.products] == ["1", "6"]
    assert selection.skipped_no_target == 2
    assert selection.skipped_not_green == 1
    assert selection.skipped_no_display_name == 1  # product 7: counted under its first failed rule


def test_select_returns_a_selection_with_nothing_when_no_product_qualifies():
    selection = select_products_for_check([], PLATFORM)

    assert selection.products == ()
    assert selection.skipped_no_target == 0
    assert selection.skipped_not_green == 0
    assert selection.skipped_no_display_name == 0


# ---------------------------------------------------------------------------
# check_one -- comparison
# ---------------------------------------------------------------------------


def test_check_one_equal_price_is_a_match():
    client = FakeCheckClient()
    client.prices["p1"] = 1_100_000

    entry = check_one(client, _product("p1", target_price=1_100_000))

    assert entry.status is CheckStatus.MATCH
    assert entry.current_price == 1_100_000
    assert entry.expected_price == 1_100_000
    assert entry.target_price == 1_100_000
    assert entry.variant_id == "V-p1"
    assert entry.platform == PLATFORM
    assert entry.difference == 0
    assert entry.error_message is None


def test_check_one_different_price_is_a_mismatch_with_a_signed_difference():
    client = FakeCheckClient()
    client.prices["low"] = 1_000_000
    client.prices["high"] = 1_250_000

    low = check_one(client, _product("low", target_price=1_100_000))
    high = check_one(client, _product("high", target_price=1_100_000))

    assert low.status is CheckStatus.MISMATCH
    assert low.current_price == 1_000_000
    assert low.expected_price == 1_100_000
    assert low.difference == -100_000  # platform is lower than the sheet
    assert high.status is CheckStatus.MISMATCH
    assert high.difference == 150_000  # platform is higher than the sheet


def test_check_one_rounds_the_sheet_price_to_the_platform_step_before_comparing():
    # DigiKala only stores multiples of 100 Rial: a sheet price of 1,234,567
    # is applied as 1,234,500, so a platform already at 1,234,500 is up to date.
    client = FakeCheckClient(step=100)
    client.prices["p1"] = 1_234_500

    entry = check_one(client, _product("p1", target_price=1_234_567))

    assert entry.status is CheckStatus.MATCH
    assert entry.expected_price == 1_234_500
    assert entry.target_price == 1_234_567  # the raw sheet value is kept for reporting


def test_check_one_step_rounding_still_flags_a_real_difference():
    client = FakeCheckClient(step=100)
    client.prices["up"] = 1_234_600  # one step above the rounded sheet price
    client.prices["down"] = 1_234_400

    up = check_one(client, _product("up", target_price=1_234_567))
    down = check_one(client, _product("down", target_price=1_234_567))

    assert up.status is CheckStatus.MISMATCH
    assert up.difference == 100
    assert down.status is CheckStatus.MISMATCH
    assert down.difference == -100


def test_check_one_without_a_target_price_is_an_error_and_never_reads_the_platform():
    client = FakeCheckClient()

    entry = check_one(client, _product("p1", target_price=None))

    assert entry.status is CheckStatus.ERROR
    assert entry.error_message
    assert entry.difference is None
    assert client.resolve_calls == []
    assert client.price_calls == []


# ---------------------------------------------------------------------------
# check_one -- retry policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("site", ["resolve", "price"])
def test_check_one_retryable_error_gets_two_attempts_then_is_an_error(site: str):
    client = FakeCheckClient()
    client.prices["p1"] = 1_100_000
    errors = client.resolve_errors if site == "resolve" else client.price_errors
    errors["p1"] = [_err5xx(), _err5xx(), _err5xx()]  # a third call would still fail

    entry = check_one(client, _product("p1"))

    assert entry.status is CheckStatus.ERROR
    assert "503" in (entry.error_message or "")
    assert entry.current_price is None
    assert entry.expected_price == 1_100_000  # still known: it only depends on the sheet
    assert entry.target_price == 1_100_000
    assert client.resolve_calls == ["p1", "p1"]  # exactly two attempts, whichever step failed


def test_check_one_retryable_error_that_clears_on_the_second_attempt_is_a_match():
    client = FakeCheckClient()
    client.prices["p1"] = 1_100_000
    client.price_errors["p1"] = [_err5xx(), None]

    entry = check_one(client, _product("p1"))

    assert entry.status is CheckStatus.MATCH
    assert client.price_calls == ["p1", "p1"]
    assert entry.error_message is None


@pytest.mark.parametrize(
    "error_factory",
    [
        _err429,
        _not_found,
        lambda: PlatformAPIError("HTTP 400: bad request", status_code=400, retryable=False),
        lambda: ValueError("unexpected payload"),
    ],
    ids=["429", "not-found", "400", "plain-exception"],
)
@pytest.mark.parametrize("site", ["resolve", "price"])
def test_check_one_non_retryable_error_gets_one_attempt_and_is_an_error(
    error_factory: Any, site: str
):
    client = FakeCheckClient()
    client.prices["p1"] = 1_100_000
    errors = client.resolve_errors if site == "resolve" else client.price_errors
    # A second queued error would be consumed by a wrong retry.
    errors["p1"] = [error_factory(), error_factory()]

    entry = check_one(client, _product("p1"))

    assert entry.status is CheckStatus.ERROR
    assert entry.error_message
    assert client.resolve_calls == ["p1"]  # one attempt only


def test_check_one_error_entry_keeps_the_variant_id_when_only_the_price_read_failed():
    client = FakeCheckClient()
    client.price_errors["p1"] = [_err429()]

    entry = check_one(client, _product("p1"))

    assert entry.status is CheckStatus.ERROR
    assert entry.variant_id == "V-p1"
    assert "429" in (entry.error_message or "")


def test_check_one_error_entry_has_no_variant_id_when_resolving_failed():
    client = FakeCheckClient()
    client.resolve_errors["p1"] = [_not_found()]

    entry = check_one(client, _product("p1"))

    assert entry.status is CheckStatus.ERROR
    assert entry.variant_id is None
    assert "not found" in (entry.error_message or "")


def test_check_one_honors_a_custom_max_attempts():
    client = FakeCheckClient()
    client.resolve_errors["p1"] = [_err5xx() for _ in range(5)]

    entry = check_one(client, _product("p1"), max_attempts=3)

    assert entry.status is CheckStatus.ERROR
    assert client.resolve_calls == ["p1", "p1", "p1"]


# ---------------------------------------------------------------------------
# check_platform -- whole-platform behaviour
# ---------------------------------------------------------------------------


def test_check_platform_reads_every_product_in_order_and_counts_the_skips():
    client = FakeCheckClient()
    products = [_product("a"), _product("b"), _product("c")]
    client.prices.update({"a": 1_100_000, "b": 900_000, "c": 1_100_000})
    selection = _selection(*products, no_target=2, not_green=1)

    result = check_platform(client, selection)

    assert result.platform == PLATFORM
    assert result.planned == 3
    assert result.skipped_no_target == 2
    assert result.skipped_not_green == 1
    assert [e.product.internal_id for e in result.entries] == ["a", "b", "c"]
    assert [e.status for e in result.entries] == [
        CheckStatus.MATCH,
        CheckStatus.MISMATCH,
        CheckStatus.MATCH,
    ]
    assert [e.product.internal_id for e in result.matched] == ["a", "c"]
    assert [e.product.internal_id for e in result.mismatched] == ["b"]
    assert result.errors == []
    assert result.cancelled is False
    assert result.platform_error is None
    assert (client.prepare_calls, client.finish_calls) == (1, 1)
    assert result.elapsed_seconds >= 0.0


def test_check_platform_a_failing_product_does_not_stop_the_others():
    client = FakeCheckClient()
    client.prices.update({"a": 1_100_000, "c": 1_100_000})
    client.resolve_errors["b"] = [_not_found()]

    result = check_platform(client, _selection(_product("a"), _product("b"), _product("c")))

    assert [e.status for e in result.entries] == [
        CheckStatus.MATCH,
        CheckStatus.ERROR,
        CheckStatus.MATCH,
    ]
    assert [e.product.internal_id for e in result.errors] == ["b"]
    assert result.platform_error is None  # one bad product is not a platform failure


def test_check_platform_prepare_failure_sets_platform_error_and_still_finishes():
    client = FakeCheckClient()
    client.prepare_error = RuntimeError("catalog request failed")
    products = [_product("a"), _product("b")]

    result = check_platform(client, _selection(*products))

    assert result.platform_error == "catalog request failed"
    assert result.entries == []
    assert result.planned == 2  # what the check SET OUT to read is still reported
    assert result.cancelled is False
    assert client.resolve_calls == []  # nothing was read after the failed prepare
    assert client.finish_calls == 1  # cleanup runs even though prepare failed


def test_check_platform_prepare_failure_without_a_message_falls_back_to_the_type_name():
    client = FakeCheckClient()
    client.prepare_error = RuntimeError()

    result = check_platform(client, _selection(_product("a")))

    assert result.platform_error == "RuntimeError"
    assert client.finish_calls == 1


def test_check_platform_cancel_in_the_middle_keeps_only_the_products_read_so_far():
    client = FakeCheckClient()
    products = [_product(str(i)) for i in range(5)]
    client.prices.update({p.internal_id: 1_100_000 for p in products})
    entries_seen: list[str] = []

    def on_entry(entry: Any) -> None:
        entries_seen.append(entry.product.internal_id)

    # The "stop" button is pressed right after the second product is read.
    result = check_platform(
        client,
        _selection(*products),
        on_entry=on_entry,
        should_cancel=lambda: len(entries_seen) >= 2,
    )

    assert result.cancelled is True
    assert [e.product.internal_id for e in result.entries] == ["0", "1"]
    assert result.planned == 5
    assert client.price_calls == ["0", "1"]  # products 2-4 were never requested
    assert result.platform_error is None
    assert client.finish_calls == 1


def test_check_platform_cancel_requested_before_the_first_product_reads_nothing():
    client = FakeCheckClient()
    client.prices["a"] = 1_100_000

    result = check_platform(client, _selection(_product("a")), should_cancel=lambda: True)

    assert result.cancelled is True
    assert result.entries == []
    assert client.resolve_calls == []
    assert (client.prepare_calls, client.finish_calls) == (1, 1)


def test_check_platform_without_cancel_callback_is_never_cancelled():
    client = FakeCheckClient()
    client.prices["a"] = 1_100_000

    result = check_platform(client, _selection(_product("a")), should_cancel=None)

    assert result.cancelled is False
    assert len(result.entries) == 1


def test_check_platform_never_calls_update_price():
    # update_price on the fake always fails AND is recorded, so a check that
    # "fixed" a mismatch would leave a trace in both places.
    client = FakeCheckClient()
    products = [_product("match"), _product("mismatch"), _product("error")]
    client.prices.update({"match": 1_100_000, "mismatch": 5})
    client.price_errors["error"] = [_err429()]

    result = check_platform(client, _selection(*products))

    assert client.update_calls == []
    assert [e.status for e in result.entries] == [
        CheckStatus.MATCH,
        CheckStatus.MISMATCH,
        CheckStatus.ERROR,
    ]
    # An AssertionError escaping from update_price would have been recorded as
    # the error text of an entry; the only error here is the scripted 429.
    assert [e.error_message for e in result.errors] == ["HTTP 429: rate limited"]


def test_check_platform_reports_progress_through_the_callbacks_in_order():
    client = FakeCheckClient()
    client.prices.update({"a": 1_100_000, "b": 1_100_000})
    events: list[tuple[str, str]] = []

    check_platform(
        client,
        _selection(_product("a"), _product("b")),
        on_product_start=lambda p: events.append(("start", p.internal_id)),
        on_entry=lambda e: events.append(("entry", e.product.internal_id)),
    )

    assert events == [("start", "a"), ("entry", "a"), ("start", "b"), ("entry", "b")]


def test_check_platform_a_crashing_callback_keeps_what_was_read_and_still_finishes():
    client = FakeCheckClient()
    client.prices.update({"a": 1_100_000, "b": 1_100_000})

    def on_entry(entry: Any) -> None:
        if entry.product.internal_id == "b":
            raise RuntimeError("telegram edit failed")

    result = check_platform(client, _selection(_product("a"), _product("b")), on_entry=on_entry)

    assert result.platform_error == "telegram edit failed"
    assert [e.product.internal_id for e in result.entries] == ["a", "b"]
    assert client.finish_calls == 1


def test_check_platform_a_failing_finish_hook_never_hides_the_result():
    client = FakeCheckClient()
    client.prices["a"] = 1_100_000
    client.finish_error = RuntimeError("cache release failed")

    result = check_platform(client, _selection(_product("a")))

    assert result.platform_error is None
    assert [e.status for e in result.entries] == [CheckStatus.MATCH]
    assert client.finish_calls == 1


def test_error_entries_carry_the_failed_status_they_should_be_recorded_as():
    from faraz_honar.core.exceptions import PlatformAuthError
    from faraz_honar.core.models import OutcomeStatus

    client = FakeCheckClient()
    client.prices.update({"a": 1, "b": 1, "c": 1})
    client.resolve_errors["a"] = [VariantResolutionError("no variant", retryable=False)]
    client.price_errors["b"] = [PlatformAuthError("token expired")]
    client.price_errors["c"] = [PlatformAPIError("HTTP 500", retryable=False)]

    statuses = {
        pid: check_one(client, _product(pid), max_attempts=1).error_status for pid in ("a", "b", "c")
    }

    assert statuses == {
        "a": OutcomeStatus.FAILED_MISSING_IDENTIFIER,
        "b": OutcomeStatus.FAILED_AUTH,
        "c": OutcomeStatus.FAILED_API,
    }
