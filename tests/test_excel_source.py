"""ExcelProductSource reads the uploaded .xlsx exactly like
GoogleSheetsProductSource reads the live sheet: same header/mapping rules,
same purple-red "skip" / green "checked" cell-color convention."""
from __future__ import annotations

import openpyxl
import pytest
from openpyxl.styles import PatternFill

from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.sheets.excel_source import ExcelProductSource
from faraz_honar.sheets.mapping import SheetMapping

_MAPPING = SheetMapping(
    name_column="اسم",
    display_name_column="اسم جدید",
    reference_price_column="قیمت",
    target_price_column="قیمت اصلی",
    platform_ref_columns={"digikala": "دیجی کالا اصلی"},
    note_column="شرح",
)

_HEADER = ["اسم", "اسم جدید", "قیمت", "قیمت اصلی", "دیجی کالا اصلی", "شرح"]
_SHEET_NAME = "محصولات سایت جدید"


def _write_workbook(path, rows, green_cell=None, red_cell=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = _SHEET_NAME
    ws.append(_HEADER)
    for row in rows:
        ws.append(row)
    if green_cell:
        ws[green_cell].fill = PatternFill(start_color="FF00FF00", end_color="FF00FF00", fill_type="solid")
    if red_cell:
        ws[red_cell].fill = PatternFill(start_color="FFFF0000", end_color="FFFF0000", fill_type="solid")
    wb.save(path)


def test_fetch_products_reads_header_rows_and_prices(tmp_path):
    path = tmp_path / "products.xlsx"
    _write_workbook(
        path,
        rows=[
            ["پایه چوبی", "پایه چوبی جدید", 100000, 120000, "123456", None],
        ],
        green_cell="E2",
    )
    source = ExcelProductSource(path, _MAPPING, _SHEET_NAME)
    products = source.fetch_products()

    assert len(products) == 1
    product = products[0]
    assert product.internal_id == "پایه چوبی"
    assert product.name == "پایه چوبی جدید"
    assert product.reference_price == 100000
    assert product.target_price == 1_200_000  # Toman -> Rial
    assert product.platform_refs == {"digikala": "123456"}
    assert "digikala" in product.green_platforms


def test_fetch_products_marks_red_cell_as_excluded(tmp_path):
    path = tmp_path / "products.xlsx"
    _write_workbook(
        path,
        rows=[["x", "x جدید", 1000, 2000, "999", None]],
        red_cell="E2",
    )
    source = ExcelProductSource(path, _MAPPING, _SHEET_NAME)
    products = source.fetch_products()
    assert "digikala" in products[0].color_excluded_platforms


def test_missing_file_raises_clear_error(tmp_path):
    source = ExcelProductSource(tmp_path / "missing.xlsx", _MAPPING, _SHEET_NAME)
    with pytest.raises(ConfigurationError, match="not found"):
        source.fetch_products()


def test_missing_worksheet_raises_clear_error(tmp_path):
    path = tmp_path / "products.xlsx"
    _write_workbook(path, rows=[["x", "y", 1, 2, "3", None]])
    source = ExcelProductSource(path, _MAPPING, "یک شیت دیگر")
    with pytest.raises(ConfigurationError, match="یک شیت دیگر"):
        source.fetch_products()
