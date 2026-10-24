import pytest

from faraz_honar.core.pricing import CAPPED_PLATFORMS, compute_price_to_apply


def test_target_within_12pct_is_reached_directly():
    decision = compute_price_to_apply(1_000_000, 1_100_000)
    assert decision.price_to_apply == 1_100_000
    assert decision.is_capped is False
    assert decision.reaches_target is True


def test_target_exceeding_12pct_is_capped():
    decision = compute_price_to_apply(1_000_000, 2_000_000)
    assert decision.price_to_apply == 1_120_000
    assert decision.is_capped is True
    assert decision.reaches_target is False


def test_target_at_exactly_12pct_is_not_capped():
    decision = compute_price_to_apply(1_000_000, 1_120_000)
    assert decision.price_to_apply == 1_120_000
    assert decision.is_capped is False


def test_target_equal_to_reference_passes_through():
    decision = compute_price_to_apply(1_000_000, 1_000_000)
    assert decision.price_to_apply == 1_000_000
    assert decision.is_capped is False


def test_price_decrease_is_not_limited():
    decision = compute_price_to_apply(2_000_000, 1_000_000)
    assert decision.price_to_apply == 1_000_000
    assert decision.is_capped is False


def test_repeated_capping_converges_towards_target():
    # Simulates spec section 8: hourly re-checks, reference price unchanged.
    reference = 1_000_000
    target = 2_000_000
    for _ in range(20):
        decision = compute_price_to_apply(reference, target)
        reference = decision.price_to_apply
        if decision.reaches_target:
            break
    assert reference == target


def test_negative_prices_rejected():
    with pytest.raises(ValueError):
        compute_price_to_apply(-1, 100)


# -- The 12% cap only applies to DigiKala platforms (apply_cap) -----------------------


def test_apply_cap_true_still_caps_a_large_increase():
    decision = compute_price_to_apply(1_000_000, 2_000_000, apply_cap=True)
    assert decision.price_to_apply == 1_120_000
    assert decision.is_capped is True


def test_apply_cap_false_lets_a_large_increase_through_uncapped():
    decision = compute_price_to_apply(1_000_000, 2_000_000, apply_cap=False)
    assert decision.price_to_apply == 2_000_000
    assert decision.is_capped is False
    assert decision.reaches_target is True


def test_apply_cap_false_default_max_increase_pct_is_ignored():
    # Even the default 12% max_increase_pct value is irrelevant once
    # apply_cap is False — the target always applies directly.
    decision = compute_price_to_apply(1_000_000, 10_000_000, apply_cap=False)
    assert decision.price_to_apply == 10_000_000
    assert decision.is_capped is False


def test_capped_platforms_is_empty():
    # DigiKala's real reference-price tolerance turned out to be far higher
    # than the previous 12% guess, and varies per-product, so we no longer
    # pre-emptively cap on our side for any platform. DigiKala's own API is
    # now the sole source of truth (see DigikalaReferencePriceRejected).
    assert CAPPED_PLATFORMS == frozenset()


def test_capped_platforms_excludes_every_platform():
    for platform in ("digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan", "tapsi_shop", "basalam"):
        assert platform not in CAPPED_PLATFORMS
