"""Interface every platform integration (DigiKala, and future platforms)
must implement. The update orchestration service only ever talks to this
interface, never to a platform-specific client directly — this is what
keeps adding a new platform from requiring changes to pricing/retry/
Telegram/state code (spec section 14).
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod

from faraz_honar.core.models import Product


class PlatformClient(ABC):
    #: short machine-readable name, used as the key everywhere (state DB,
    #: Telegram platform-selection buttons, sheet platform_ref_columns key)
    name: str

    #: Smallest Rial step the platform actually stores for a price. update_price
    #: rounds the sheet's price DOWN to this step before sending (e.g. DigiKala
    #: only accepts multiples of 100 Rial), so the price-check button compares
    #: against the rounded value, not the raw sheet value. 1 = no rounding.
    price_step_rial: int = 1

    @abstractmethod
    def resolve_variant_id(self, product: Product) -> str:
        """Return the platform-specific identifier required to update price.

        Must raise VariantResolutionError (with a clear message) if it
        cannot be determined — including when the lookup mechanism itself
        isn't configured yet.
        """

    @abstractmethod
    def get_reference_price(self, product: Product, variant_id: str) -> int:
        """Return the platform's current live price for this product.

        Used as the "Reference Price" the 12% rule compares against
        (spec section 7) when the sheet itself isn't the source of truth
        for the current price. Implementations that instead trust the
        sheet's price column may return product.reference_price unchanged.
        """

    @abstractmethod
    def update_price(self, variant_id: str, new_price: int) -> None:
        """Set the price for a platform-specific product identifier.

        Must raise PlatformAPIError/PlatformAuthError on failure with
        `retryable` set appropriately.
        """

    # -- Read-only price check (the "🔍 چک و بررسی قیمت‌ها" button) --------------
    # Everything below is optional and NEVER writes to the platform. The
    # defaults make any client work with the price check out of the box; a
    # client only overrides what differs for it.

    def get_current_price(self, product: Product, variant_id: str) -> int:
        """The price the platform is CURRENTLY selling at, in Rial — the value
        that update_price is supposed to have changed. Defaults to
        get_reference_price; override when the reference price is a different
        field from the one update_price writes (DigiKala: `price_list` is a
        reference bracket, `price_sale` is the real selling price).
        """
        return self.get_reference_price(product, variant_id)

    def expected_price_for_target(self, target_price: int) -> int:
        """What the sheet's price becomes on this platform after update_price's
        own rounding (floor to `price_step_rial`)."""
        step = max(1, self.price_step_rial)
        return (target_price // step) * step

    def prepare_price_check(self) -> None:
        """Called once before a platform's price check starts. Override to load
        the platform's catalog in bulk (one pass) instead of searching it once
        per product, or to drop caches so the check reads fresh prices."""

    def finish_price_check(self) -> None:
        """Called once after a platform's price check ends (also on cancel or
        failure). Override to release whatever prepare_price_check built."""

    def estimate_check_seconds(self, product_count: int) -> float:
        """Rough upper bound for checking `product_count` products, used only
        for the "this may take a while" heads-up. Default: one throttled read
        request per product."""
        delay = float(getattr(self, "_read_delay_seconds", 1.0))
        return product_count * (delay + 0.5)

    def _pages_estimate_seconds(self, product_count: int, page_size: int) -> float:
        """Helper for clients whose price check walks the catalog page by page."""
        delay = float(getattr(self, "_read_delay_seconds", 1.0))
        pages = max(1, math.ceil(product_count / max(1, page_size)))
        return pages * (delay + 0.5)
