"""Abstraction over "where product rows come from".

Two implementations exist: CSVProductSource (local file, used for dev/tests
and as a fallback while Google credentials aren't available yet) and
GoogleSheetsProductSource (the real thing). Both return the same
core.models.Product objects, so nothing downstream cares which one is used.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from faraz_honar.core.models import Product


class ProductSource(ABC):
    @abstractmethod
    def fetch_products(self) -> list[Product]:
        """Return all products currently defined in the source."""

    def fetch_product(self, internal_id: str) -> Product | None:
        for product in self.fetch_products():
            if product.internal_id == internal_id:
                return product
        return None
