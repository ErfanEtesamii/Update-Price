"""The 12%-max-increase business rule (spec sections 7 and 8).

Pure functions, no I/O, so this is exhaustively unit-testable and is the one
place the rule is implemented — nobody else should recompute it.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor

# Platforms the max-increase rule applies to. EMPTY as of 2026-09-27:
# DigiKala's actual reference-price tolerance was measured (probing a live
# product) to be far higher than the previous 12% guess, and varies
# per-product — so we no longer pre-emptively cap on our side at all.
# DigiKala's own API is now the sole source of truth for this limit (see
# platforms/digikala/client.py::DigikalaReferencePriceRejected and
# update/service.py's handling of it). compute_price_to_apply below is kept
# for its unit tests / potential future reuse, but is never called with
# apply_cap=True in production anymore.
CAPPED_PLATFORMS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PricingDecision:
    price_to_apply: int
    is_capped: bool
    reference_price: int
    target_price: int

    @property
    def reaches_target(self) -> bool:
        return self.price_to_apply >= self.target_price


def compute_price_to_apply(
    reference_price: int,
    target_price: int,
    *,
    max_increase_pct: float = 0.12,
    apply_cap: bool = True,
) -> PricingDecision:
    """Decide what price to actually set this run.

    - If `apply_cap` is False (non-DigiKala platforms — see
      CAPPED_PLATFORMS), the target price always applies directly,
      uncapped, regardless of how large an increase it is.
    - If the target is not an increase (equal or a decrease) relative to the
      reference price, the limitation does not apply (spec section 7 only
      restricts increases) and the target is used directly.
    - If the target is an increase of more than `max_increase_pct` over the
      reference price, cap this run's price at reference * (1 + pct).
    - Otherwise the target is reachable in one step.
    """
    if reference_price < 0 or target_price < 0:
        raise ValueError("Prices must be non-negative")

    if not apply_cap or target_price <= reference_price:
        return PricingDecision(
            price_to_apply=target_price,
            is_capped=False,
            reference_price=reference_price,
            target_price=target_price,
        )

    max_allowed = floor(reference_price * (1 + max_increase_pct))
    if target_price > max_allowed:
        return PricingDecision(
            price_to_apply=max_allowed,
            is_capped=True,
            reference_price=reference_price,
            target_price=target_price,
        )

    return PricingDecision(
        price_to_apply=target_price,
        is_capped=False,
        reference_price=reference_price,
        target_price=target_price,
    )
