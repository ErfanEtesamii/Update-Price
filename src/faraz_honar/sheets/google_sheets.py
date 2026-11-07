"""Live Google Sheets product source (gspread + a service-account key).

NOT executable yet in this environment: it needs a service-account JSON key
file (GOOGLE_SERVICE_ACCOUNT_FILE) that has been granted read access to the
sheet. Until that file exists, constructing this class raises a clear
ConfigurationError rather than failing deep inside a network call.
"""
from __future__ import annotations

import logging
import ssl
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.core.models import Product
from faraz_honar.sheets.base import ProductSource
from faraz_honar.sheets.mapping import RGB, SheetMapping, rows_to_products

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class _Tls12Adapter(HTTPAdapter):
    """Pins TLS 1.2 for connections to Google.

    On this network the larger TLS 1.3 ClientHello that Python/OpenSSL sends is
    cut mid-handshake (``SSLEOFError: UNEXPECTED_EOF_WHILE_READING``) while
    TLS 1.2 passes, so Google API sessions are restricted to TLS 1.2.
    """

    def _tls12_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._tls12_context()
        return super().init_poolmanager(*args, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request, verify, cert
        )
        # requests >= 2.32 injects its own default SSLContext when verify=True,
        # which would override the TLS 1.2 pin. Force ours.
        if "ssl_context" in pool_kwargs:
            ctx = self._tls12_context()
            try:
                import certifi

                ctx.load_verify_locations(certifi.where())
            except Exception:  # noqa: BLE001 - fall back to the system store
                pass
            pool_kwargs["ssl_context"] = ctx
        return host_params, pool_kwargs

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._tls12_context()
        return super().proxy_manager_for(*args, **kwargs)


def _tls12_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", _Tls12Adapter())
    return session


class GoogleSheetsProductSource(ProductSource):
    def __init__(
        self,
        sheet_id: str,
        mapping: SheetMapping,
        service_account_file: Path | None,
        worksheet_gid: int = 0,
        worksheet_name: str | None = None,
    ):
        if not sheet_id:
            raise ConfigurationError("GOOGLE_SHEET_ID is not configured")
        if not service_account_file:
            raise ConfigurationError(
                "GOOGLE_SERVICE_ACCOUNT_FILE is not configured. Create a Google Cloud "
                "service account, download its JSON key, share the target sheet with "
                "the service account's client_email (Viewer is enough), and point "
                "GOOGLE_SERVICE_ACCOUNT_FILE at the key file."
            )
        if not service_account_file.exists():
            raise ConfigurationError(f"Service account key file not found: {service_account_file}")

        self._sheet_id = sheet_id
        self._mapping = mapping
        self._service_account_file = service_account_file
        self._worksheet_gid = worksheet_gid
        # Selecting by name (e.g. a specific tab like "محصولات سایت جدید")
        # takes precedence over gid when both would otherwise apply, since a
        # human-chosen tab name is a more deliberate selection than the
        # default gid=0.
        self._worksheet_name = worksheet_name

    def _open_spreadsheet(self):
        # Imported lazily so the rest of the app can run without gspread
        # installed/configured (e.g. CSV-only local dev).
        import gspread
        from google.oauth2.service_account import Credentials

        from google.auth.transport.requests import AuthorizedSession, Request

        creds = Credentials.from_service_account_file(str(self._service_account_file), scopes=_SCOPES)
        # Both the API session and the token-refresh session go through the
        # TLS 1.2 adapter (these sessions only ever talk to Google).
        session = AuthorizedSession(creds, auth_request=Request(session=_tls12_session()))
        session.mount("https://", _Tls12Adapter())
        client = gspread.authorize(creds, session=session)
        return client.open_by_key(self._sheet_id)

    def _open_worksheet(self):
        spreadsheet = self._open_spreadsheet()

        if self._worksheet_name:
            for ws in spreadsheet.worksheets():
                if ws.title == self._worksheet_name:
                    return ws
            raise ConfigurationError(
                f"Worksheet named '{self._worksheet_name}' not found in spreadsheet "
                f"{self._sheet_id}. Available tabs: "
                f"{', '.join(w.title for w in spreadsheet.worksheets())}"
            )

        for ws in spreadsheet.worksheets():
            if ws.id == self._worksheet_gid:
                return ws
        raise ConfigurationError(
            f"Worksheet with gid={self._worksheet_gid} not found in spreadsheet {self._sheet_id}"
        )

    def fetch_products(self) -> list[Product]:
        worksheet = self._open_worksheet()
        all_values: list[list[str]] = worksheet.get_all_values()
        header_row_idx = _find_header_row(all_values)
        header = all_values[header_row_idx]
        data_rows = all_values[header_row_idx + 1 :]
        cell_colors = _fetch_cell_colors(worksheet, all_values)
        if cell_colors is not None:
            cell_colors = cell_colors[header_row_idx + 1 :]
        return rows_to_products(header, data_rows, self._mapping, cell_colors=cell_colors)


def _fetch_cell_colors(worksheet, all_values: list[list[str]]) -> list[list[RGB | None]] | None:
    """Background color of every cell, aligned with `all_values` (row 0 = sheet
    row 1, column 0 = column A), or None when the colors could not be read.

    get_all_values() carries no formatting, so a second, formatting-only
    Sheets API call is needed. It is best-effort: the colors only affect the
    status report's "هنوز اجرا نشده" count, so a failure here must never stop
    product loading -- it is logged and the products load without color
    information.
    """
    try:
        n_rows = len(all_values)
        n_cols = max((len(r) for r in all_values), default=0)
        if not n_rows or not n_cols:
            return None
        title = worksheet.title.replace("'", "''")
        metadata = worksheet.spreadsheet.fetch_sheet_metadata(
            params={
                "includeGridData": "true",
                "ranges": f"'{title}'!A1:{_column_letters(n_cols)}{n_rows}",
                "fields": "sheets(data(startRow,startColumn,rowData(values(effectiveFormat(backgroundColor)))))",
            }
        )
        grid = metadata["sheets"][0]["data"][0]
        start_row = grid.get("startRow", 0)
        start_col = grid.get("startColumn", 0)

        colors: list[list[RGB | None]] = [[None] * n_cols for _ in range(n_rows)]
        for r_off, row_data in enumerate(grid.get("rowData", [])):
            r = start_row + r_off
            if r >= n_rows:
                break
            for c_off, cell in enumerate(row_data.get("values", [])):
                c = start_col + c_off
                if c >= n_cols:
                    break
                bg = (cell.get("effectiveFormat") or {}).get("backgroundColor")
                if bg:
                    # The API omits channels that are 0.
                    colors[r][c] = (
                        float(bg.get("red", 0.0)),
                        float(bg.get("green", 0.0)),
                        float(bg.get("blue", 0.0)),
                    )
        return colors
    except Exception as exc:  # noqa: BLE001 - best-effort, see docstring
        logger.warning(
            "Could not read cell background colors from the sheet (%s: %s); "
            "continuing without color information (no product will be price-checked, "
            "since a check needs a green platform cell).",
            type(exc).__name__,
            exc,
        )
        return None


def _find_header_row(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows):
        if any(cell.strip() for cell in row):
            return i
    raise ConfigurationError("Worksheet appears to be entirely empty")


def _column_letters(n: int) -> str:
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA' (A1-notation column, no gspread import)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters
