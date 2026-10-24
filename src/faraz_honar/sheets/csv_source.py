"""Local-file product source: reads a CSV export of the product sheet.

Used for development/testing without live Google API access, and as an
explicit fallback (never implicit/automatic in a production run) when
PRODUCT_SOURCE_CSV_PATH is set. Implements the same ProductSource interface
as GoogleSheetsProductSource so the rest of the app can't tell them apart.
"""
from __future__ import annotations

import csv
from pathlib import Path

from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.core.models import Product
from faraz_honar.sheets.base import ProductSource
from faraz_honar.sheets.mapping import SheetMapping, rows_to_products


class CSVProductSource(ProductSource):
    def __init__(self, csv_path: Path, mapping: SheetMapping):
        self._csv_path = csv_path
        self._mapping = mapping

    def fetch_products(self) -> list[Product]:
        if not self._csv_path.exists():
            raise ConfigurationError(f"CSV product source file not found: {self._csv_path}")

        with self._csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            all_rows = [row for row in reader]

        header_row_idx = _find_header_row(all_rows)
        header = all_rows[header_row_idx]
        data_rows = all_rows[header_row_idx + 1 :]
        return rows_to_products(header, data_rows, self._mapping)


def _find_header_row(rows: list[list[str]]) -> int:
    """The sample export has a fully blank first row before the real header."""
    for i, row in enumerate(rows):
        if any(cell.strip() for cell in row):
            return i
    raise ConfigurationError("CSV file appears to be entirely empty")
