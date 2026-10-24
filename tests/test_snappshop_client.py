import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from faraz_honar.core.exceptions import PlatformAPIError, PlatformAuthError, VariantResolutionError
from faraz_honar.core.models import Product
from faraz_honar.platforms.snappshop.client import (
    SnappShopClient,
    _chunked,
    _index_by_key,
    _rial_to_toman,
    _toman_to_rial,
)


def _client(**overrides: Any) -> SnappShopClient:
    defaults: dict[str, Any] = dict(
        base_url="https://apix.snappshop.ir/automation/v1",
        token="the-jwt-token",
        vendor_code="0dPKQ4",
        agent_user="SNV_test-agent",
        dry_run=False,
        # Delays default to 0 so tests not specifically about throttling
        # stay fast and deterministic (same convention as test_digikala_client.py).
        read_delay_seconds=0.0,
        write_delay_seconds=0.0,
    )
    defaults.update(overrides)
    return SnappShopClient(**defaults)


def _product(**overrides: Any) -> Product:
    defaults: dict[str, Any] = dict(
        internal_id="p1",
        name="Test",
        reference_price=5_000_000,
        target_price=5_500_000,
        platform_refs={"snappshop_tehran": "PN-123"},
    )
    defaults.update(overrides)
    # Eligibility needs "اسم جدید" and a GREEN code cell per platform.
    defaults.setdefault("display_name", defaults["name"])
    defaults.setdefault("green_platforms", frozenset(defaults["platform_refs"]))
    return Product(**defaults)


def _page_response(items: list[dict], *, total_pages: int = 1) -> MagicMock:
    # meta.pagination.total_pages is CONFIRMED as the real API's pagination
    # signal (see _extract_total_pages) — every page response carries it.
    return MagicMock(
        status_code=200,
        json=lambda: {"products": items, "meta": {"pagination": {"total_pages": total_pages}}},
    )


def _item(**overrides: Any) -> dict:
    # price/stock are in Toman per the official API; 570_000 Toman == 5,700,000 Rial.
    defaults = dict(id="65M571", product_number="PN-123", title="Product Title", price=570_000, stock=15)
    defaults.update(overrides)
    return defaults


# -- Credentials / headers / URL (official Automation API) --------------------------


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_uses_correct_base_url_and_vendor_path(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()])
    client = _client(
        base_url="https://apix.snappshop.ir/automation/v1",
        vendor_code="0dPKQ4",
    )

    client.resolve_variant_id(_product())

    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    assert method == "GET"
    # The official /automation/v1/vendors/{vendor_id}/products path — never
    # the deprecated /vendors/v1/{vendor}/inventory/products... shape.
    assert url == "https://apix.snappshop.ir/automation/v1/vendors/0dPKQ4/products"


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_authorization_header_is_correct(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()])
    client = _client(token="secret-token-value")

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret-token-value"


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_user_agent_header_is_the_real_agent_user(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()])
    client = _client(agent_user="SNV_real-agent-string")

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    assert headers["User-Agent"] == "SNV_real-agent-string"


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_no_obsolete_headers_are_sent(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()])
    client = _client()

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    # Only Authorization/User-Agent/Accept/Content-Type, per the official spec.
    assert set(headers.keys()) == {"Authorization", "User-Agent", "Accept", "Content-Type"}
    assert "uuid" not in headers
    assert "x-client-type" not in headers
    assert "snappshop-seller-code" not in headers


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_accept_and_content_type_headers(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()])
    client = _client()

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


def test_missing_vendor_code_raises_configuration_error():
    client = _client(vendor_code=None)
    with patch("faraz_honar.platforms.snappshop.client.requests.request") as mock_request:
        with pytest.raises(Exception, match="SnappShop Tehran is not configured"):
            client.resolve_variant_id(_product())
        mock_request.assert_not_called()


def test_missing_agent_user_raises_configuration_error():
    client = _client(agent_user=None)
    with patch("faraz_honar.platforms.snappshop.client.requests.request") as mock_request:
        with pytest.raises(Exception, match="SnappShop Tehran is not configured"):
            client.resolve_variant_id(_product())
        mock_request.assert_not_called()


def test_missing_token_raises_configuration_error():
    client = _client(token=None)
    with patch("faraz_honar.platforms.snappshop.client.requests.request") as mock_request:
        with pytest.raises(Exception, match="SnappShop Tehran is not configured"):
            client.resolve_variant_id(_product())
        mock_request.assert_not_called()


# -- Pagination (meta.pagination.total_pages) + product_number -> id mapping --------


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_pagination_uses_total_pages_not_an_empty_page(mock_request: MagicMock, tmp_path: Path):
    # CONFIRMED real-API behavior: pagination is driven by
    # meta.pagination.total_pages, not by waiting for an empty page.
    mock_request.side_effect = [
        _page_response([_item(id="a1", product_number="A1")], total_pages=3),
        _page_response([_item(id="a2", product_number="A2")], total_pages=3),
        _page_response([_item(id="a3", product_number="A3")], total_pages=3),
    ]
    client = _client()
    with pytest.raises(VariantResolutionError):
        client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "does-not-exist"}))

    assert mock_request.call_count == 3
    pages_requested = [call.kwargs["params"]["page"] for call in mock_request.call_args_list]
    assert pages_requested == [1, 2, 3]


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_does_not_assume_page_1_contains_all_products(mock_request: MagicMock, tmp_path: Path):
    # The target product is only on page 2 of 3 total pages — page 1 alone
    # must never be treated as the full catalog.
    mock_request.side_effect = [
        _page_response([_item(id="other", product_number="A1")], total_pages=3),
        _page_response([_item(id="65M571", product_number="PN-123")], total_pages=3),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "PN-123"}))
    assert variant_id == "65M571"
    # Stops fetching as soon as the match is found -> 2 requests, not 3.
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_missing_total_pages_raises_clear_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=200, json=lambda: {"products": [_item()]})
    client = _client()
    with pytest.raises(PlatformAPIError, match="meta.pagination.total_pages"):
        client.resolve_variant_id(_product())


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_product_number_maps_to_internal_id(mock_request: MagicMock, tmp_path: Path):
    # The sheet stores "PN-123" (a product_number); the internal SnappShop
    # `id` used for writes ("65M571") is a different value entirely.
    mock_request.return_value = _page_response(
        [
            _item(id="other-id", product_number="PN-999"),
            _item(id="65M571", product_number="PN-123"),
        ]
    )
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "PN-123"}))
    assert variant_id == "65M571"


def test_index_by_key_builds_product_number_to_item_mapping():
    items = [_item(id="a1", product_number="PN-1"), _item(id="a2", product_number="PN-2")]
    index = _index_by_key(items, "product_number")
    assert index["PN-1"]["id"] == "a1"
    assert index["PN-2"]["id"] == "a2"


def test_index_by_key_skips_items_missing_the_key():
    assert _index_by_key([{"id": "a1"}], "product_number") == {}


# -- Shared catalog-page cache across DIFFERENT products' lookups -------------------
# Regression coverage for the live incident (2026-09-23): a product on page 33 of 46
# meant every per-product lookup independently re-walked pagination from page 1,
# which could make a full run take extremely long and leave late-catalog products
# never updated. These prove pages are fetched once and reused for later lookups.


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_second_products_lookup_reuses_pages_already_fetched_for_the_first(
    mock_request: MagicMock, tmp_path: Path
):
    # Product A is on page 2; product B is on page 1. Resolving A first
    # fetches pages 1-2 (caching both). Resolving B next must be served
    # entirely from that cache -> zero additional HTTP requests.
    mock_request.side_effect = [
        _page_response([_item(id="b-id", product_number="B")], total_pages=2),
        _page_response([_item(id="a-id", product_number="A")], total_pages=2),
    ]
    client = _client()

    variant_a = client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "A"}))
    assert variant_a == "a-id"
    assert mock_request.call_count == 2  # had to walk pages 1 and 2 to find A

    variant_b = client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "B"}))
    assert variant_b == "b-id"
    assert mock_request.call_count == 2  # B was on page 1, already cached -> no new requests


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_later_products_lookup_continues_from_the_cache_not_from_page_1(
    mock_request: MagicMock, tmp_path: Path
):
    # Product A is on page 2 (cache ends up holding pages 1-2). Product C is
    # on page 4. Resolving C next must continue from page 3, not restart at
    # page 1 — only 2 more requests (pages 3 and 4), not 4.
    mock_request.side_effect = [
        _page_response([_item(id="other-1", product_number="X1")], total_pages=4),
        _page_response([_item(id="a-id", product_number="A")], total_pages=4),
        _page_response([_item(id="other-3", product_number="X3")], total_pages=4),
        _page_response([_item(id="c-id", product_number="C")], total_pages=4),
    ]
    client = _client()

    client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "A"}))
    assert mock_request.call_count == 2  # pages 1-2

    variant_c = client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "C"}))
    assert variant_c == "c-id"
    assert mock_request.call_count == 4  # continued with pages 3-4 only, never restarted at 1
    pages_requested = [call.kwargs["params"]["page"] for call in mock_request.call_args_list]
    assert pages_requested == [1, 2, 3, 4]


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_id_fallback_uses_the_direct_single_product_endpoint_not_pagination(
    mock_request: MagicMock, tmp_path: Path
):
    # CONFIRMED against the official API doc (v2.1.2, section 2-2-2):
    # GET /vendors/{vendor_id}/products/{id} is a direct single-product
    # lookup by the opaque SnappShop id. get_reference_price's cache-miss
    # fallback path (_find_product_by_id_fallback) must use this directly
    # instead of paginating the whole catalog to find one item.
    mock_request.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": True, "data": _item(id="other-id", product_number="OTHER")},
    )
    client = _client()

    item = client._find_product_by_id_fallback("other-id", _product())  # noqa: SLF001

    assert item["product_number"] == "OTHER"
    assert mock_request.call_count == 1
    call = mock_request.call_args
    assert call.args[0] == "GET"
    assert call.args[1].endswith("/vendors/0dPKQ4/products/other-id")


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_id_fallback_raises_on_404(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=404, text="not found")
    client = _client()

    with pytest.raises(PlatformAPIError, match="could not be re-fetched"):
        client._find_product_by_id_fallback("missing-id", _product())  # noqa: SLF001


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_catalog_cache_expires_after_its_ttl_and_refetches_from_page_1(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [
        _page_response([_item(id="a-id", product_number="A")]),
        _page_response([_item(id="a-id-2", product_number="A")]),  # after TTL: fresh fetch
    ]
    client = _client()

    with patch("faraz_honar.platforms.snappshop.client.time.monotonic", return_value=1_000.0):
        client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "A"}))
    assert mock_request.call_count == 1

    # Same product, but "now" is well past _CATALOG_CACHE_TTL_SECONDS later
    # -> the cache must be treated as stale and re-fetched, not reused.
    with patch("faraz_honar.platforms.snappshop.client.time.monotonic", return_value=10_000.0):
        variant_id = client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "A"}))
    assert variant_id == "a-id-2"
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_catalog_cache_within_ttl_is_not_reset(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(id="a-id", product_number="A")]),
    ]
    client = _client()

    with patch("faraz_honar.platforms.snappshop.client.time.monotonic", return_value=1_000.0):
        client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "A"}))
    assert mock_request.call_count == 1

    # A few seconds later (well within the TTL) -> still served from cache.
    with patch("faraz_honar.platforms.snappshop.client.time.monotonic", return_value=1_030.0):
        client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "A"}))
    assert mock_request.call_count == 1


# -- sku is confirmed unreliable ("0" for every real product): never used -----------


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_sku_field_is_never_used_for_matching_or_identification(mock_request: MagicMock, tmp_path: Path):
    # Real API returns sku="0" for every product; matching/identification
    # must rely on product_number/id only, never sku.
    item = _item(id="65M571", product_number="PN-123", sku="0")
    mock_request.return_value = _page_response([item])
    client = _client()
    assert client.resolve_variant_id(_product()) == "65M571"


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_id_missing_raises_instead_of_falling_back_to_sku(mock_request: MagicMock, tmp_path: Path):
    item = _item(product_number="PN-123", sku="0")
    del item["id"]
    mock_request.return_value = _page_response([item])
    client = _client()
    with pytest.raises(VariantResolutionError, match="no 'id'"):
        client.resolve_variant_id(_product())


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_resolve_variant_id_raises_when_no_sheet_reference(mock_request: MagicMock, tmp_path: Path):
    client = _client()
    with pytest.raises(VariantResolutionError, match="no SnappShop Tehran reference"):
        client.resolve_variant_id(_product(platform_refs={}))
    mock_request.assert_not_called()


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_data_key_fallback_still_supported(mock_request: MagicMock, tmp_path: Path):
    # Defensive fallback for the old reverse-engineered API's {"data": [...]}
    # shape, in case some responses happen to use that envelope.
    mock_request.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": [_item()], "meta": {"pagination": {"total_pages": 1}}},
    )
    client = _client()
    assert client.resolve_variant_id(_product()) == "65M571"


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_unrecognized_response_shape_raises_clear_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=200, json=lambda: {"unexpected": "shape"})
    client = _client()
    with pytest.raises(PlatformAPIError, match="unrecognized shape"):
        client.resolve_variant_id(_product())


# -- Sheet code == parent_product_number (live catalogs: ~47 items per account whose own
# -- product_number differs from the code shown on the product page / kept in the sheet) --


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_resolves_via_parent_product_number_when_own_product_number_differs(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item(id="GnApv6", product_number=129986320, parent_product_number=593205305)
    mock_request.return_value = _page_response([item])
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "593205305"}))

    assert variant_id == "GnApv6"
    assert client.get_reference_price(_product(), "GnApv6") == 5_700_000


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_product_number_match_wins_over_parent_product_number(
    mock_request: MagicMock, tmp_path: Path
):
    # Never observed in live data, but if one code were item A's product_number
    # and item B's parent_product_number, product_number (the documented
    # identifier) must win.
    a = _item(id="AAAAAA", product_number=111, parent_product_number=111)
    b = _item(id="BBBBBB", product_number=222, parent_product_number=111)
    mock_request.return_value = _page_response([b, a])
    client = _client()

    assert client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "111"})) == "AAAAAA"


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_parent_match_is_found_even_when_it_is_on_an_early_page(
    mock_request: MagicMock, tmp_path: Path
):
    # The parent match sits on page 1 while the catalog has 2 pages: the whole
    # catalog must still be read first (a product_number match could exist on
    # page 2 and takes precedence), then the parent index is used.
    early = _item(id="EARLY1", product_number=1, parent_product_number=999)
    late = _item(id="LATE01", product_number=2, parent_product_number=2)
    mock_request.side_effect = [
        _page_response([early], total_pages=2),
        _page_response([late], total_pages=2),
    ]
    client = _client()

    assert client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "999"})) == "EARLY1"
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_code_in_neither_product_number_nor_parent_still_raises(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _page_response(
        [_item(id="X", product_number=1, parent_product_number=2)]
    )
    client = _client()

    with pytest.raises(VariantResolutionError, match="No SnappShop Tehran product found"):
        client.resolve_variant_id(_product(platform_refs={"snappshop_tehran": "3"}))


# -- Sheet column mapping --------------------------------------------------------------


def test_sheet_mapping_yaml_maps_snappshop_tehran_to_platform_ref_column():
    from faraz_honar.sheets.mapping import SheetMapping

    mapping_path = Path(__file__).parents[1] / "config" / "sheet_mapping.yaml"
    mapping = SheetMapping.load(mapping_path)
    assert "snappshop_tehran" in mapping.platform_ref_columns


# -- Empty/whitespace قیمت اصلی -> zero SnappShop API calls (eligibility filter) -----


def test_empty_target_price_means_zero_snappshop_requests_via_is_eligible():
    from faraz_honar.update.service import _is_eligible

    product = _product(target_price=None)
    assert _is_eligible(product, "snappshop_tehran") is False


def test_whitespace_target_price_row_has_no_target_price_via_csv_parsing(tmp_path: Path):
    # Proves the SAME sheet-parsing path used for DigiKala also applies to
    # SnappShop: an empty/whitespace "قیمت اصلی" cell parses to
    # target_price=None (see sheets/mapping.py), which _is_eligible then
    # excludes — no SnappShop-specific parsing logic was introduced.
    from faraz_honar.sheets.csv_source import CSVProductSource
    from faraz_honar.sheets.mapping import SheetMapping

    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "اسم,اسم جدید,قیمت,قیمت اصلی,اسنپ شاپ تهران\n"
        "P1,P1,500000,   ,PN-123\n",
        encoding="utf-8",
    )
    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column="قیمت اصلی",
        platform_ref_columns={"snappshop_tehran": "اسنپ شاپ تهران"},
    )
    products = CSVProductSource(csv_path, mapping).fetch_products()
    assert products[0].target_price is None

    from faraz_honar.update.service import _is_eligible

    assert _is_eligible(products[0], "snappshop_tehran") is False


# -- Pricing algorithm reused, not duplicated ------------------------------------------


def test_snappshop_does_not_define_its_own_pricing_module():
    # The SnappShop client module must not import/define any 12%-cap
    # business logic of its own — only core.pricing (used generically by
    # update/service.py) computes that.
    import faraz_honar.platforms.snappshop.client as snappshop_client_module

    assert not hasattr(snappshop_client_module, "compute_price_to_apply")
    assert "core.pricing" not in getattr(snappshop_client_module, "__file__", "")


# -- Toman <-> Rial conversion helpers ---------------------------------------------------


def test_toman_to_rial_multiplies_by_ten():
    assert _toman_to_rial(570_000) == 5_700_000


def test_rial_to_toman_divides_by_ten():
    assert _rial_to_toman(4_700_000) == 470_000


def test_rial_to_toman_rounds_a_non_exact_multiple():
    # 12%-cap rounding in core/pricing.py can produce a Rial value that
    # isn't an exact multiple of 10; this must round rather than truncate.
    assert _rial_to_toman(4_700_007) == 470_001


# -- get_reference_price: read as Toman, returned as Rial ------------------------------


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_get_reference_price_converts_toman_to_rial(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item(price=234_500)])  # Toman
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 2_345_000  # Rial
    assert mock_request.call_count == 1  # no extra fetch needed, cached item reused


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_get_reference_price_makes_real_read_even_under_dry_run(mock_request: MagicMock, tmp_path: Path):
    # No prior resolve_variant_id call -> hits _find_product_by_id_fallback,
    # which now uses the direct single-product endpoint (see the official
    # API doc, v2.1.2 section 2-2-2), not pagination.
    mock_request.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "status": True,
            "data": _item(id="65M571", product_number="PN-123", price=450_000),
        },
    )
    client = _client(dry_run=True)

    price = client.get_reference_price(_product(), "65M571")

    assert price == 4_500_000  # Rial
    mock_request.assert_called_once()
    assert mock_request.call_args.args[0] == "GET"


# -- update_price: bulk PATCH, Toman price, real stock preserved, DRY_RUN --------------


def test_dry_run_does_not_call_requests(tmp_path: Path):
    client = _client(dry_run=True)
    with patch("faraz_honar.platforms.snappshop.client.requests.request") as mock_request:
        client.update_price("65M571", 4_700_000)
        mock_request.assert_not_called()


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_sends_correct_patch_payload(mock_request: MagicMock, tmp_path: Path):
    original_item = _item(id="65M571", product_number="PN-123", price=570_000, stock=15)
    mock_request.side_effect = [
        _page_response([original_item]),  # resolve_variant_id's list fetch
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),  # the PATCH
    ]
    client = _client(vendor_code="0dPKQ4")

    client.resolve_variant_id(_product())
    client.update_price("65M571", 4_700_000)  # Rial -> 470,000 Toman

    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    payload = mock_request.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url == "https://apix.snappshop.ir/automation/v1/vendors/0dPKQ4/products"
    # Only id/stock/price, price in Toman, stock preserved from the GET read.
    assert payload == {"products": [{"id": "65M571", "stock": 15, "price": 470_000}]}


# -- Active discount -> special_price must be kept in sync (else the customer-visible ----
# -- price never changes) — CONFIRMED against the official API doc, v2.1.2 section 2-2-3.


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_includes_special_price_when_product_has_an_active_discount(
    mock_request: MagicMock, tmp_path: Path
):
    discounted_item = _item(
        id="65M571",
        product_number="PN-123",
        stock=15,
        discount={
            "id": "cdXw34",
            "special_price": 400_000,
            "stock": 8,
            "percent": 8,
            "start_at": "2023-05-06",
            "end_at": "2023-05-11",
        },
    )
    mock_request.side_effect = [
        _page_response([discounted_item]),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),
    ]
    client = _client()

    client.resolve_variant_id(_product())
    client.update_price("65M571", 4_700_000)  # Rial -> 470,000 Toman

    payload = mock_request.call_args.kwargs["json"]
    assert payload == {
        "products": [
            {
                "id": "65M571",
                "stock": 15,
                "price": 470_000,
                "special_price": 470_000,
                "special_price_start_at": "2023-05-06",
                "special_price_end_at": "2023-05-11",
                "special_price_stock": 8,
            }
        ]
    }


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_omits_special_price_when_product_has_no_discount(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item(id="65M571", product_number="PN-123", stock=15, discount=None)
    mock_request.side_effect = [
        _page_response([item]),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),
    ]
    client = _client()

    client.resolve_variant_id(_product())
    client.update_price("65M571", 4_700_000)

    payload = mock_request.call_args.kwargs["json"]
    assert "special_price" not in payload["products"][0]
    assert "special_price_start_at" not in payload["products"][0]
    assert "special_price_end_at" not in payload["products"][0]
    assert "special_price_stock" not in payload["products"][0]


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_sends_todays_date_as_start_at_when_discount_has_no_start_at(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item(
        id="65M571",
        product_number="PN-123",
        stock=15,
        discount={"id": "cdXw34", "special_price": 400_000, "stock": 8, "end_at": "2023-05-11"},
    )
    mock_request.side_effect = [
        _page_response([item]),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),
    ]
    client = _client()

    client.resolve_variant_id(_product())
    client.update_price("65M571", 4_700_000)

    sent = mock_request.call_args.kwargs["json"]["products"][0]
    assert sent["special_price"] == 470_000
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", sent["special_price_start_at"])
    assert sent["special_price_end_at"] == "2023-05-11"
    assert sent["special_price_stock"] == 8  # the discount's own stock, not the product's


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_raises_when_discount_has_no_end_at_instead_of_guessing_one(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item(
        id="65M571",
        product_number="PN-123",
        stock=15,
        discount={"id": "cdXw34", "special_price": 400_000, "stock": 8, "start_at": "2023-05-06"},
    )
    mock_request.side_effect = [_page_response([item])]
    client = _client()

    client.resolve_variant_id(_product())
    with pytest.raises(PlatformAPIError, match="end_at"):
        client.update_price("65M571", 4_700_000)

    # Only the product-list GET happened; no PATCH was sent.
    assert mock_request.call_count == 1


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_price_never_invents_a_discount_that_did_not_exist(
    mock_request: MagicMock, tmp_path: Path
):
    # _item()'s default has no 'discount' key at all (not even None) -> must
    # behave identically to an explicit discount=None, never raise and
    # never add special_price.
    item = _item(id="65M571", product_number="PN-123", stock=15)
    assert "discount" not in item
    mock_request.side_effect = [
        _page_response([item]),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),
    ]
    client = _client()

    client.resolve_variant_id(_product())
    client.update_price("65M571", 4_700_000)

    payload = mock_request.call_args.kwargs["json"]
    assert "special_price" not in payload["products"][0]


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_preserves_actual_stock_not_a_default(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(id="65M571", product_number="PN-123", stock=273)]),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),
    ]
    client = _client()

    client.resolve_variant_id(_product())
    client.update_price("65M571", 1_000_000)

    payload = mock_request.call_args.kwargs["json"]
    assert payload["products"][0]["stock"] == 273


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_without_stock_field_raises_instead_of_inventing_one(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item(id="65M571", product_number="PN-123")
    del item["stock"]
    mock_request.return_value = _page_response([item])
    client = _client()
    client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="no 'stock' field"):
        client.update_price("65M571", 1_000_000)


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_update_without_prior_resolve_raises_instead_of_sending_a_request(
    mock_request: MagicMock, tmp_path: Path
):
    client = _client()
    with pytest.raises(PlatformAPIError, match="no previously-fetched product data"):
        client.update_price("never-resolved", 1_000_000)
    mock_request.assert_not_called()


# -- Maximum 50 products per PATCH request ----------------------------------------------


def test_chunked_splits_into_batches_of_at_most_fifty():
    items = [{"id": str(i)} for i in range(120)]
    batches = _chunked(items, 50)
    assert [len(b) for b in batches] == [50, 50, 20]
    assert sum(len(b) for b in batches) == 120


def test_chunked_single_item_is_one_batch():
    assert _chunked([{"id": "x"}], 50) == [[{"id": "x"}]]


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_a_single_update_price_call_sends_exactly_one_patch_with_one_item(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [
        _page_response([_item(id="65M571", product_number="PN-123")]),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),
    ]
    client = _client()
    client.resolve_variant_id(_product())
    client.update_price("65M571", 1_000_000)

    patch_calls = [c for c in mock_request.call_args_list if c.args[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert len(patch_calls[0].kwargs["json"]["products"]) == 1


# -- Iran-IP / VPN restriction (HTTP 403 + HTML body) --------------------------------


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_403_html_body_is_not_treated_as_success(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(
        status_code=403, text="<!DOCTYPE html><html><body>Access denied</body></html>"
    )
    client = _client()
    with pytest.raises(PlatformAPIError, match="Iran-IP/VPN"):
        client.resolve_variant_id(_product())


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_403_json_body_is_a_normal_error_not_iran_ip(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=403, text='{"message": "forbidden"}')
    client = _client()
    with pytest.raises(PlatformAPIError) as excinfo:
        client.resolve_variant_id(_product())
    assert "Iran-IP" not in str(excinfo.value)


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_proxy_url_is_optional_and_passed_through_when_set(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()])
    client = _client(proxy_url="http://user:pass@1.2.3.4:8080")

    client.resolve_variant_id(_product())

    proxies = mock_request.call_args.kwargs["proxies"]
    assert proxies == {"http": "http://user:pass@1.2.3.4:8080", "https": "http://user:pass@1.2.3.4:8080"}


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_no_proxy_is_required_by_default(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()])
    client = _client()  # no proxy_url given

    client.resolve_variant_id(_product())

    assert mock_request.call_args.kwargs["proxies"] is None


# -- Auth / retry classification -------------------------------------------------------


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_401_raises_auth_error_not_retryable(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=401, text="invalid token")
    client = _client()
    with pytest.raises(PlatformAuthError) as excinfo:
        client.resolve_variant_id(_product())
    assert excinfo.value.retryable is False
    assert mock_request.call_count == 1  # no retry loop inside the client itself


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_400_is_not_retryable(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=400, text="bad request")
    client = _client()
    with pytest.raises(PlatformAPIError) as excinfo:
        client.resolve_variant_id(_product())
    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_5xx_is_retryable(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=503, text="upstream unavailable")
    client = _client()
    with pytest.raises(PlatformAPIError) as excinfo:
        client.resolve_variant_id(_product())
    assert excinfo.value.retryable is True


# -- SnappShop Isfahan: same SnappShopClient class, its own credentials -------------
#
# Isfahan is just a second SnappShopClient instance (see
# platforms/registry.py) — all pagination/matching/conversion/stock/PATCH
# behavior above is exercised generically by the Tehran tests and applies
# identically here since it's the same code path. These tests only prove
# the Isfahan-specific wiring: its own name/credentials/sheet column, and
# that a full resolve->read->update flow works end-to-end under that
# separate configuration.


def _isfahan_client(**overrides: Any) -> SnappShopClient:
    defaults: dict[str, Any] = dict(
        name="snappshop_isfahan",
        display_name="SnappShop Isfahan",
        config_env_hint=(
            "SNAPPSHOP_ISFAHAN_TOKEN, SNAPPSHOP_ISFAHAN_VENDOR_CODE, "
            "and SNAPPSHOP_ISFAHAN_AGENT_USER"
        ),
        token="isfahan-token",
        vendor_code="ISFHN1",
        agent_user="SNV_isfahan-agent",
    )
    defaults.update(overrides)
    return _client(**defaults)


def _isfahan_product(**overrides: Any) -> Product:
    defaults: dict[str, Any] = dict(platform_refs={"snappshop_isfahan": "PN-ISF-1"})
    defaults.update(overrides)
    return _product(**defaults)


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_isfahan_full_flow_uses_product_number_ignores_sku_paginates_and_converts_units(
    mock_request: MagicMock, tmp_path: Path
):
    # sku="0" on every item (confirmed real-API behavior), product only on
    # page 2 of 2 (total_pages), price/stock in Toman.
    mock_request.side_effect = [
        _page_response(
            [_item(id="other", product_number="PN-ISF-OTHER", sku="0")], total_pages=2
        ),
        _page_response(
            [_item(id="ISF-65M571", product_number="PN-ISF-1", sku="0", price=300_000, stock=42)],
            total_pages=2,
        ),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),  # the PATCH
    ]
    client = _isfahan_client()
    product = _isfahan_product()

    variant_id = client.resolve_variant_id(product)
    assert variant_id == "ISF-65M571"

    reference_price = client.get_reference_price(product, variant_id)
    assert reference_price == 3_000_000  # 300,000 Toman -> Rial

    client.update_price(variant_id, 3_100_000)  # Rial -> 310,000 Toman

    get_calls = [c for c in mock_request.call_args_list if c.args[0] == "GET"]
    patch_calls = [c for c in mock_request.call_args_list if c.args[0] == "PATCH"]
    assert [c.kwargs["params"]["page"] for c in get_calls] == [1, 2]  # both pages fetched
    assert patch_calls[0].kwargs["json"] == {
        "products": [{"id": "ISF-65M571", "stock": 42, "price": 310_000}]
    }


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_isfahan_uses_its_own_credentials_in_headers_and_url(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item(product_number="PN-ISF-1")])
    client = _isfahan_client(vendor_code="ISFHN1", token="isfahan-secret", agent_user="SNV_isfahan-x")

    client.resolve_variant_id(_isfahan_product())

    url = mock_request.call_args.args[1]
    headers = mock_request.call_args.kwargs["headers"]
    assert url == "https://apix.snappshop.ir/automation/v1/vendors/ISFHN1/products"
    assert headers["Authorization"] == "Bearer isfahan-secret"
    assert headers["User-Agent"] == "SNV_isfahan-x"


def test_isfahan_sheet_column_is_separate_from_tehran():
    from faraz_honar.sheets.mapping import SheetMapping

    mapping_path = Path(__file__).parents[1] / "config" / "sheet_mapping.yaml"
    mapping = SheetMapping.load(mapping_path)
    isfahan_col = mapping.platform_ref_columns["snappshop_isfahan"]
    tehran_col = mapping.platform_ref_columns["snappshop_tehran"]
    assert isfahan_col == "اسنپ شاپ اصفهان"
    assert isfahan_col != tehran_col


def test_isfahan_product_with_only_tehran_ref_is_not_resolvable():
    # A product's snappshop_isfahan column is empty (it only has a Tehran
    # ref) -> the Isfahan client must not find any reference to resolve.
    client = _isfahan_client()
    product = _product(platform_refs={"snappshop_tehran": "PN-123"})
    with patch("faraz_honar.platforms.snappshop.client.requests.request") as mock_request:
        with pytest.raises(VariantResolutionError, match="no SnappShop Isfahan reference"):
            client.resolve_variant_id(product)
        mock_request.assert_not_called()


def test_isfahan_error_messages_never_say_tehran(tmp_path: Path):
    # Regression check: the client used to hardcode "SnappShop Tehran" in
    # every error/log message regardless of which account was calling —
    # now driven by display_name, so an Isfahan failure never mentions
    # Tehran (and vice versa; see test_resolve_variant_id_raises_when_no_
    # sheet_reference above for the Tehran-default equivalent).
    client = _isfahan_client(token=None, vendor_code=None, agent_user=None)
    with patch("faraz_honar.platforms.snappshop.client.requests.request"):
        with pytest.raises(Exception, match=r"SnappShop Isfahan is not configured") as excinfo:
            client.resolve_variant_id(_isfahan_product())
        assert "Tehran" not in str(excinfo.value)
        assert "SNAPPSHOP_ISFAHAN_TOKEN" in str(excinfo.value)


def test_empty_target_price_means_zero_isfahan_requests_via_is_eligible():
    from faraz_honar.update.service import _is_eligible

    product = _isfahan_product(target_price=None)
    assert _is_eligible(product, "snappshop_isfahan") is False


# -- The 12% cap does NOT apply to SnappShop, exercised via the real service ----------
#
# core/pricing.py's CAPPED_PLATFORMS restricts the 12%-max-increase rule to
# the two DigiKala accounts only ("digikala", "digikala2"). Neither
# SnappShop account is in that set, so a target price is always applied
# directly here, no matter how large an increase it is over the reference
# price.


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_snappshop_tehran_does_not_cap_a_large_increase(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            # +100% over the 570,000 Toman (5,700,000 Rial) reference below.
            return [_product(target_price=11_400_000)]

    mock_request.side_effect = [
        _page_response([_item(id="65M571", product_number="PN-123", price=570_000, stock=15)]),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),  # the PATCH
    ]
    client = _client()
    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry={"snappshop_tehran": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.run(["snappshop_tehran"], triggered_by="test")["snappshop_tehran"]

    # Not capped: snappshop_tehran is not in CAPPED_PLATFORMS, so the full
    # +100% target price applies directly in one run.
    assert len(summary.capped) == 0
    assert len(summary.successful) == 1
    assert summary.successful[0].applied_price == 11_400_000
    patch_call = next(c for c in mock_request.call_args_list if c.args[0] == "PATCH")
    assert patch_call.kwargs["json"]["products"][0]["price"] == 1_140_000  # Rial -> Toman


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_snappshop_isfahan_does_not_cap_a_large_increase(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            # +100% over the 300,000 Toman (3,000,000 Rial) reference below.
            return [_isfahan_product(target_price=6_000_000)]

    mock_request.side_effect = [
        _page_response(
            [_item(id="ISF-65M571", product_number="PN-ISF-1", sku="0", price=300_000, stock=42)]
        ),
        MagicMock(status_code=200, json=lambda: {"status": "ok"}),  # the PATCH
    ]
    client = _isfahan_client()
    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry={"snappshop_isfahan": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.run(["snappshop_isfahan"], triggered_by="test")["snappshop_isfahan"]

    # Not capped: snappshop_isfahan is not in CAPPED_PLATFORMS, so the full
    # +100% target price applies directly in one run.
    assert len(summary.capped) == 0
    assert len(summary.successful) == 1
    assert summary.successful[0].applied_price == 6_000_000
    patch_call = next(c for c in mock_request.call_args_list if c.args[0] == "PATCH")
    assert patch_call.kwargs["json"]["products"][0]["price"] == 600_000  # Rial -> Toman


# -- Price check (read-only "🔍 چک و بررسی قیمت‌ها" button) -----------------------------


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_get_current_price_ignores_discount_and_uses_base_price_in_rial(mock_request: MagicMock):
    # 570,000 Toman base price with a 500,000 Toman special price: the price
    # check compares the sheet's main price with the BASE price, not the discount.
    mock_request.return_value = _page_response(
        [_item(price=570_000, discount={"special_price": 500_000})]
    )
    client = _client()
    product = _product()

    variant_id = client.resolve_variant_id(product)
    current = client.get_current_price(product, variant_id)

    assert current == 5_700_000  # Toman -> Rial, base price
    assert mock_request.call_count == 1  # no extra request


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_get_current_price_without_discount_uses_the_base_price(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item(price=570_000)])
    client = _client()
    product = _product()

    variant_id = client.resolve_variant_id(product)

    assert client.get_current_price(product, variant_id) == 5_700_000


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_get_current_price_ignores_a_discount_without_special_price(mock_request: MagicMock):
    for discount in (None, {}, {"special_price": None}):
        mock_request.return_value = _page_response([_item(price=570_000, discount=discount)])
        client = _client()
        product = _product()

        variant_id = client.resolve_variant_id(product)

        assert client.get_current_price(product, variant_id) == 5_700_000


@patch("faraz_honar.platforms.snappshop.client.requests.request")
def test_prepare_price_check_resets_the_catalog_cache_so_fresh_prices_are_read(
    mock_request: MagicMock,
):
    mock_request.return_value = _page_response([_item(price=570_000)])
    client = _client()
    product = _product()

    variant_id = client.resolve_variant_id(product)
    assert client.get_current_price(product, variant_id) == 5_700_000
    assert mock_request.call_count == 1

    # The price changes on the platform. Within the cache window a second
    # lookup still serves the old catalog page (no new request)...
    mock_request.return_value = _page_response([_item(price=600_000)])
    variant_id = client.resolve_variant_id(product)
    assert client.get_current_price(product, variant_id) == 5_700_000
    assert mock_request.call_count == 1

    # ...which is exactly what a price check must not do.
    client.prepare_price_check()
    assert mock_request.call_count == 1  # preparing itself sends nothing

    variant_id = client.resolve_variant_id(product)
    assert client.get_current_price(product, variant_id) == 6_000_000
    assert mock_request.call_count == 2


def test_price_step_is_10_rial_and_expected_price_rounds_down():
    client = _client()

    assert client.price_step_rial == 10
    assert client.expected_price_for_target(1_234_567) == 1_234_560
    assert client.expected_price_for_target(1_234_560) == 1_234_560  # already a multiple
