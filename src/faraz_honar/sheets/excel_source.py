"""Local .xlsx product source: reads whatever file was last sent to the bot
through "📤 ارسال اکسل محصولات" (see telegram_bot/handlers.py::
document_message_handler), instead of the live Google Sheet.

The file is read fresh on every fetch_products() call — exactly like
CSVProductSource — so overwriting excel_product_source_path with a newly
uploaded file is the only thing needed for the next run/check to pick up the
new prices; nothing else needs to be rebuilt or restarted.

Implements the same ProductSource interface as GoogleSheetsProductSource, so
the rest of the app can't tell them apart. Background cell colors are read
too (same purple/red "skip" and green "checked" convention as the Google
Sheet), so nothing about the color-based rules in mapping.rows_to_products
is lost by switching source.
"""
from __future__ import annotations

from pathlib import Path

from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.core.models import Product
from faraz_honar.sheets.base import ProductSource
from faraz_honar.sheets.mapping import RGB, SheetMapping, rows_to_products


class ExcelProductSource(ProductSource):
    def __init__(self, path: Path, mapping: SheetMapping, worksheet_name: str):
        self._path = path
        self._mapping = mapping
        self._worksheet_name = worksheet_name

    def fetch_products(self) -> list[Product]:
        if not self._path.exists():
            raise ConfigurationError(
                f"Excel product source file not found: {self._path}. Send the product "
                "Excel file to the bot first (📤 ارسال اکسل محصولات)."
            )

        # Imported lazily, matching google_sheets.py's lazy-gspread-import
        # style, so the rest of the app can run without openpyxl installed.
        import openpyxl

        try:
            workbook = openpyxl.load_workbook(self._path, data_only=True)
        except Exception as exc:  # noqa: BLE001 - surfaced as a clear config error
            raise ConfigurationError(
                f"Could not open Excel product source file {self._path}: {exc}"
            ) from exc

        if self._worksheet_name not in workbook.sheetnames:
            raise ConfigurationError(
                f"Worksheet named '{self._worksheet_name}' not found in {self._path}. "
                f"Available sheets: {', '.join(workbook.sheetnames)}"
            )
        worksheet = workbook[self._worksheet_name]

        all_rows: list[list[str]] = []
        all_colors: list[list[RGB | None]] = []
        for row in worksheet.iter_rows():
            all_rows.append([_cell_to_str(cell.value) for cell in row])
            all_colors.append([_cell_background(cell) for cell in row])

        header_row_idx = _find_header_row(all_rows)
        header = all_rows[header_row_idx]
        data_rows = all_rows[header_row_idx + 1 :]
        data_colors = all_colors[header_row_idx + 1 :]
        return rows_to_products(header, data_rows, self._mapping, cell_colors=data_colors)


def _cell_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        # Excel stores whole numbers (prices, codes, ...) as floats; avoid
        # "5450000.0" in favor of "5450000" to match what the Google Sheets
        # API returns for the same cell.
        return str(int(value))
    return str(value).strip()


def _cell_background(cell) -> RGB | None:
    """Best-effort background color as (r, g, b) in 0.0-1.0, matching the
    Google Sheets API's Color scale (see google_sheets._fetch_cell_colors).
    Only a plain, directly-specified RGB/ARGB fill is understood; a
    theme-based or indexed color (no hex value) yields None, same as an
    unreadable Google Sheets color -- the row is simply treated as having no
    color information for that cell.
    """
    fill = getattr(cell, "fill", None)
    if fill is None or fill.patternType != "solid":
        return None
    color = fill.fgColor
    rgb = getattr(color, "rgb", None)
    if not isinstance(rgb, str) or len(rgb) < 6:
        return None
    hex_part = rgb[-6:]
    try:
        r = int(hex_part[0:2], 16) / 255.0
        g = int(hex_part[2:4], 16) / 255.0
        b = int(hex_part[4:6], 16) / 255.0
    except ValueError:
        return None
    return (r, g, b)


def _find_header_row(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows):
        if any(cell.strip() for cell in row):
            return i
    raise ConfigurationError("Excel worksheet appears to be entirely empty")
