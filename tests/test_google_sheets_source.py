"""Proves the production Google Sheets integration actually works: opening
the spreadsheet, selecting a worksheet by gid or by name, finding the header
row, and parsing products — all via a mocked gspread client (no real network
call, no real credentials needed). This is the one integration point that
had zero test coverage before.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.sheets.google_sheets import GoogleSheetsProductSource
from faraz_honar.sheets.mapping import SheetMapping

MAPPING_PATH = Path(__file__).parents[1] / "config" / "sheet_mapping.yaml"


def _mapping() -> SheetMapping:
    return SheetMapping.load(MAPPING_PATH)


def _fake_key_file(tmp_path: Path) -> Path:
    key_file = tmp_path / "service_account.json"
    key_file.write_text("{}")  # content is irrelevant; Credentials loading is mocked
    return key_file


def _fake_worksheet(gid: int, title: str, values: list[list[str]]) -> MagicMock:
    ws = MagicMock()
    ws.id = gid
    ws.title = title
    ws.get_all_values.return_value = values
    return ws


SAMPLE_VALUES = [
    ["", ""],  # blank spacer row, as in the real sheet export
    ["اسم", "اسم جدید", "قیمت", "قیمت اصلی", "دیجی کالا سریع"],
    ["محصول یک", "محصول یک جدید", "1,000,000", "1,200,000", "12345"],
    ["محصول دو", "محصول دو جدید", "2,000,000", "", "67890"],
]


def _patched_client(fake_worksheets: list[MagicMock]):
    fake_spreadsheet = MagicMock()
    fake_spreadsheet.worksheets.return_value = fake_worksheets
    fake_client = MagicMock()
    fake_client.open_by_key.return_value = fake_spreadsheet
    return fake_client


# -- Constructor-time configuration errors (no network attempted) ---------------


def test_missing_sheet_id_raises_without_any_network_call():
    with pytest.raises(ConfigurationError, match="GOOGLE_SHEET_ID"):
        GoogleSheetsProductSource("", _mapping(), service_account_file=None)


def test_missing_service_account_file_raises_clear_error():
    with pytest.raises(ConfigurationError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        GoogleSheetsProductSource("sheet123", _mapping(), service_account_file=None)


def test_nonexistent_service_account_file_raises_clear_error(tmp_path: Path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(ConfigurationError, match="not found"):
        GoogleSheetsProductSource("sheet123", _mapping(), service_account_file=missing)


# -- Real fetch path, gspread fully mocked --------------------------------------


@patch("gspread.authorize")
@patch("google.oauth2.service_account.Credentials.from_service_account_file")
def test_fetch_products_via_gid_selection(mock_creds, mock_authorize, tmp_path: Path):
    ws = _fake_worksheet(0, "Sheet1", SAMPLE_VALUES)
    mock_authorize.return_value = _patched_client([ws])

    source = GoogleSheetsProductSource(
        "sheet123", _mapping(), service_account_file=_fake_key_file(tmp_path), worksheet_gid=0
    )
    products = source.fetch_products()

    assert len(products) == 2
    assert products[0].internal_id == "محصول یک"
    assert products[0].reference_price == 1_000_000
    # "قیمت اصلی" cell is "1,200,000" Toman -> Rial value is ×10.
    assert products[0].target_price == 12_000_000
    assert products[1].target_price is None  # blank "قیمت اصلی" cell -> None, not invented
    # SAMPLE_VALUES' header is "دیجی کالا سریع", which sheet_mapping.yaml maps
    # to the "digikala2" key (the fast account) — NOT "digikala" (the main
    # account, "دیجی کالا اصلی"), since the 2026-09 platform-key rename.
    assert products[0].platform_refs["digikala2"] == "12345"
    mock_creds.assert_called_once()
    mock_authorize.assert_called_once()


@patch("gspread.authorize")
@patch("google.oauth2.service_account.Credentials.from_service_account_file")
def test_fetch_products_via_name_selection_takes_precedence_over_gid(
    mock_creds, mock_authorize, tmp_path: Path
):
    wrong_ws = _fake_worksheet(0, "Sheet1", [["اسم", "اسم جدید", "قیمت", "دیجی کالا سریع"]])
    target_ws = _fake_worksheet(999, "محصولات سایت جدید", SAMPLE_VALUES)
    mock_authorize.return_value = _patched_client([wrong_ws, target_ws])

    source = GoogleSheetsProductSource(
        "sheet123",
        _mapping(),
        service_account_file=_fake_key_file(tmp_path),
        worksheet_gid=0,  # would select wrong_ws if name weren't given precedence
        worksheet_name="محصولات سایت جدید",
    )
    products = source.fetch_products()

    assert len(products) == 2  # came from target_ws, not the empty wrong_ws


@patch("gspread.authorize")
@patch("google.oauth2.service_account.Credentials.from_service_account_file")
def test_worksheet_name_not_found_lists_available_tabs(mock_creds, mock_authorize, tmp_path: Path):
    ws = _fake_worksheet(0, "Sheet1", SAMPLE_VALUES)
    mock_authorize.return_value = _patched_client([ws])

    source = GoogleSheetsProductSource(
        "sheet123",
        _mapping(),
        service_account_file=_fake_key_file(tmp_path),
        worksheet_name="does-not-exist",
    )
    with pytest.raises(ConfigurationError, match="Sheet1"):
        source.fetch_products()


@patch("gspread.authorize")
@patch("google.oauth2.service_account.Credentials.from_service_account_file")
def test_worksheet_gid_not_found_raises_clear_error(mock_creds, mock_authorize, tmp_path: Path):
    ws = _fake_worksheet(5, "Sheet1", SAMPLE_VALUES)
    mock_authorize.return_value = _patched_client([ws])

    source = GoogleSheetsProductSource(
        "sheet123", _mapping(), service_account_file=_fake_key_file(tmp_path), worksheet_gid=999
    )
    with pytest.raises(ConfigurationError, match="gid=999"):
        source.fetch_products()


@patch("gspread.authorize")
@patch("google.oauth2.service_account.Credentials.from_service_account_file")
def test_empty_worksheet_raises_clear_error(mock_creds, mock_authorize, tmp_path: Path):
    ws = _fake_worksheet(0, "Sheet1", [["", ""], ["", ""]])
    mock_authorize.return_value = _patched_client([ws])

    source = GoogleSheetsProductSource(
        "sheet123", _mapping(), service_account_file=_fake_key_file(tmp_path)
    )
    with pytest.raises(ConfigurationError, match="empty"):
        source.fetch_products()


# -- Cell background colors (purple/red code cells) ------------------------------


def _metadata(row_data: list[dict]) -> dict:
    return {"sheets": [{"data": [{"rowData": row_data}]}]}


def _bg(red=0.0, green=0.0, blue=0.0) -> dict:
    # The real API omits channels that are 0, so build the dict the same way.
    color = {k: v for k, v in (("red", red), ("green", green), ("blue", blue)) if v}
    return {"effectiveFormat": {"backgroundColor": color}}


@patch("gspread.authorize")
@patch("google.oauth2.service_account.Credentials.from_service_account_file")
def test_fetch_products_marks_purple_and_red_code_cells(mock_creds, mock_authorize, tmp_path: Path):
    ws = _fake_worksheet(0, "Sheet1", SAMPLE_VALUES)
    # SAMPLE_VALUES: row 0 blank spacer, row 1 header, rows 2-3 products;
    # column 4 is "دیجی کالا سریع" (digikala2).
    ws.spreadsheet.fetch_sheet_metadata.return_value = _metadata(
        [
            {},
            {},
            {"values": [{}, {}, {}, {}, _bg(0.6, 0.0, 1.0)]},  # product 1: purple
            {"values": [{}, {}, {}, {}, _bg(1.0, 1.0, 1.0)]},  # product 2: white
        ]
    )
    mock_authorize.return_value = _patched_client([ws])

    source = GoogleSheetsProductSource(
        "sheet123", _mapping(), service_account_file=_fake_key_file(tmp_path), worksheet_gid=0
    )
    products = source.fetch_products()

    assert products[0].color_excluded_platforms == frozenset({"digikala2"})
    assert products[1].color_excluded_platforms == frozenset()
    params = ws.spreadsheet.fetch_sheet_metadata.call_args.kwargs["params"]
    assert params["ranges"] == "'Sheet1'!A1:E4"


@patch("gspread.authorize")
@patch("google.oauth2.service_account.Credentials.from_service_account_file")
def test_color_read_failure_does_not_break_product_loading(mock_creds, mock_authorize, tmp_path: Path):
    ws = _fake_worksheet(0, "Sheet1", SAMPLE_VALUES)
    ws.spreadsheet.fetch_sheet_metadata.side_effect = RuntimeError("quota exceeded")
    mock_authorize.return_value = _patched_client([ws])

    source = GoogleSheetsProductSource(
        "sheet123", _mapping(), service_account_file=_fake_key_file(tmp_path), worksheet_gid=0
    )
    products = source.fetch_products()

    assert len(products) == 2
    assert all(p.color_excluded_platforms == frozenset() for p in products)
