from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from faraz_honar.core.exceptions import PlatformAPIError, VariantResolutionError
from faraz_honar.core.models import Product
from faraz_honar.platforms.tapsishop.client import TapsiShopClient, _index_by_key, _total_pages


def _client(**overrides: Any) -> TapsiShopClient:
    defaults: dict[str, Any] = dict(
        base_url="https://vendorgw.tapsi.shop",
        token="the-tapsi-token",
        dry_run=False,
        # Delays default to 0 so tests not specifically about throttling
        # stay fast and deterministic (same convention as the other
        # platform client test files).
        read_delay_seconds=0.0,
        write_delay_seconds=0.0,
    )
    defaults.update(overrides)
    return TapsiShopClient(**defaults)


def _product(**overrides: Any) -> Product:
    # CONFIRMED against real, live Google Sheet data (see client.py's module
    # docstring): the "تپسی شاپ" column stores 19-digit numeric HSIN values,
    # not sku text — this default matches that real-world shape.
    defaults: dict[str, Any] = dict(
        internal_id="p1",
        name="Test",
        reference_price=100_000_000,
        target_price=110_000_000,
        platform_refs={"tapsi_shop": "1475748894590844929"},
    )
    defaults.update(overrides)
    # Eligibility needs "اسم جدید" and a GREEN code cell per platform.
    defaults.setdefault("display_name", defaults["name"])
    defaults.setdefault("green_platforms", frozenset(defaults["platform_refs"]))
    return Product(**defaults)


def _item(**overrides: Any) -> dict:
    # Matches the documented real API response shape exactly.
    defaults: dict[str, Any] = dict(
        id="1475748894590844928",
        hsin="1475748894590844929",
        sku="تخته نرد - شطرنج 40 خاتم نقاشی اسلیمی کد 01",
        originalPrice=150_000_000.0,
        finalPrice=142_500_000.0,
        minimalPerOrder=1,
        maximalPerOrder=None,
        onHandQuantity=30,
        availableQuantity=30,
    )
    defaults.update(overrides)
    return defaults


def _page_response(
    items: list[dict], *, total_count: int, page: int = 1, page_size: int = 100
) -> MagicMock:
    return MagicMock(
        status_code=200,
        json=lambda: {
            "data": {"page": page, "pageSize": page_size, "totalCount": total_count, "items": items},
            "success": True,
            "messages": [],
        },
    )


def _put_ack() -> MagicMock:
    return MagicMock(status_code=200, json=lambda: {"success": True, "messages": []})


# -- Authentication: exact header, no Bearer ------------------------------------------


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_authorization_header_is_the_token_itself_no_bearer_prefix(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _page_response([_item()], total_count=1)
    client = _client(token="secret-tapsi-token")

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    assert headers["Tapsi-Shop-Hub-Authorization"] == "secret-tapsi-token"
    assert "Bearer" not in headers["Tapsi-Shop-Hub-Authorization"]
    assert "Authorization" not in headers  # never DigiKala's/SnappShop's header name


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_other_required_headers_are_exact(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()], total_count=1)
    client = _client()

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    assert headers["Accept"] == "text/plain"
    assert headers["Content-Type"] == "application/json"
    assert headers["client-name"] == "Swagger on HIT.Hastim.Hub.Endpoints.WebApi"
    assert headers["client-version"] == "1.0.0.0"
    assert set(headers.keys()) == {
        "Tapsi-Shop-Hub-Authorization",
        "Accept",
        "Content-Type",
        "client-name",
        "client-version",
    }


def test_missing_token_raises_configuration_error():
    client = _client(token=None)
    with patch("faraz_honar.platforms.tapsishop.client.requests.request") as mock_request:
        with pytest.raises(Exception, match="TapsiShop is not configured"):
            client.resolve_variant_id(_product())
        mock_request.assert_not_called()


# -- GET /Web/Hub/vendors/v1/products/{page}/{pageSize} -------------------------------


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_get_uses_correct_endpoint_page_and_page_size(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item()], total_count=1)
    client = _client()

    client.resolve_variant_id(_product())

    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    assert method == "GET"
    assert url == "https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/products/1/100"


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_parses_data_items_and_matches_correct_one(mock_request: MagicMock, tmp_path: Path):
    items = [
        _item(id="a", hsin="HSIN-A", sku="SKU-A"),
        _item(id="b", hsin="HSIN-B", sku="SKU-B"),
    ]
    mock_request.return_value = _page_response(items, total_count=2)
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "HSIN-B"}))
    assert variant_id == "SKU-B"


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_missing_data_key_raises_clear_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=200, json=lambda: {"unexpected": "shape"})
    client = _client()
    with pytest.raises(PlatformAPIError, match="unrecognized shape"):
        client.resolve_variant_id(_product())


# -- Pagination via ceil(totalCount / pageSize) ----------------------------------------


def test_total_pages_computes_ceiling_from_total_count():
    assert _total_pages(3050, 100) == 31
    assert _total_pages(100, 100) == 1
    assert _total_pages(101, 100) == 2
    assert _total_pages(1, 100) == 1
    assert _total_pages(0, 100) == 0


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_pagination_fetches_all_pages_derived_from_total_count(mock_request: MagicMock, tmp_path: Path):
    # totalCount=250, pageSize=100 -> ceil(250/100) = 3 pages; target only on page 3.
    mock_request.side_effect = [
        _page_response([_item(id="p1", hsin="HSIN-OTHER-1", sku="SKU-1")], total_count=250, page=1),
        _page_response([_item(id="p2", hsin="HSIN-OTHER-2", sku="SKU-2")], total_count=250, page=2),
        _page_response([_item(id="p3", hsin="HSIN-TARGET", sku="SKU-3")], total_count=250, page=3),
    ]
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "HSIN-TARGET"}))

    assert variant_id == "SKU-3"
    assert mock_request.call_count == 3
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/products/1/100",
        "https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/products/2/100",
        "https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/products/3/100",
    ]


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_does_not_assume_page_1_has_every_product(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(id="p1", hsin="HSIN-OTHER", sku="SKU-1")], total_count=250, page=1),
        _page_response([_item(id="p2", hsin="HSIN-TARGET", sku="SKU-2")], total_count=250, page=2),
    ]
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "HSIN-TARGET"}))

    assert variant_id == "SKU-2"
    assert mock_request.call_count == 2  # stopped once found — never assumed done after page 1


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_not_found_hsin_raises_variant_resolution_error_without_crashing(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _page_response([_item(hsin="HSIN-OTHER")], total_count=1)
    client = _client()

    with pytest.raises(VariantResolutionError):
        client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "HSIN-MISSING"}))


# -- Mapping: sheet "تپسی شاپ" -> API hsin -> matched API item -----------------------
#
# CONFIRMED against real, live data (both the checked-in CSV fixture and a
# live read of the production Google Sheet — 548 real "تپسی شاپ" values
# inspected): every value is a 19-digit numeric string in the same
# digit-length/leading-prefix family as the documented `hsin` example
# ("1475748894590844929"), never the Persian product-title text the
# documented `sku` example shows ("تخته نرد - شطرنج 40 خاتم نقاشی اسلیمی
# کد 01") — a sheet-value == sku match is structurally impossible. This is
# also the user's own confirmed source of truth: the column holds HSIN
# values. Product resolution (this section) is unchanged by the id/sku PUT
# fix below: matching is, and remains, hsin-only.
#
# Per the official TapsiShop API documentation, the PUT payload's `id` is
# the vendor-side "sku" (NOT TapsiShop's own internal `id` field) — see the
# "ID used for PUT" section further down. `resolve_variant_id` therefore
# now returns the matched item's `sku`, not its `id`.


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_sheet_hsin_value_resolves_the_matched_items_sku(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response(
        [_item(id="internal-id-xyz", hsin="1475748894590844929", sku="vendor-sku-abc")],
        total_count=1,
    )
    client = _client()

    variant_id = client.resolve_variant_id(
        _product(platform_refs={"tapsi_shop": "1475748894590844929"})
    )
    assert variant_id == "vendor-sku-abc"
    assert variant_id != "internal-id-xyz"


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_missing_sku_after_hsin_match_raises_clear_error(mock_request: MagicMock, tmp_path: Path):
    # HSIN matching itself never needs sku, but the matched item's sku is
    # now required to identify it to the write endpoint — a product found
    # by hsin with no sku at all can't be resolved to a usable variant id.
    item = _item(id="internal-id-xyz", hsin="1475748894590844929")
    del item["sku"]
    mock_request.return_value = _page_response([item], total_count=1)
    client = _client()

    with pytest.raises(VariantResolutionError, match="sku"):
        client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "1475748894590844929"}))


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_sku_is_never_used_for_matching(mock_request: MagicMock, tmp_path: Path):
    # The sheet value happens to equal the item's sku, not its hsin -> must NOT match.
    mock_request.return_value = _page_response(
        [_item(id="should-not-match", hsin="REAL-HSIN", sku="SHEET-VALUE")], total_count=1
    )
    client = _client()

    with pytest.raises(VariantResolutionError):
        client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "SHEET-VALUE"}))


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_wrong_hsin_leaves_product_unresolved(mock_request: MagicMock, tmp_path: Path):
    # The sheet value matches neither hsin nor sku nor id of any real item.
    mock_request.return_value = _page_response(
        [_item(id="unrelated-id", hsin="1111111111111111111", sku="unrelated title")],
        total_count=1,
    )
    client = _client()

    with pytest.raises(VariantResolutionError):
        client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "9999999999999999999"}))


def test_index_by_key_indexes_by_hsin_not_sku():
    items = [_item(id="x", hsin="H1", sku="S1")]
    index = _index_by_key(items, "hsin")
    assert index == {"H1": items[0]}
    assert "S1" not in index


# -- ID used for PUT: resolved via hsin, write payload uses item["sku"] ---------------
#
# Per the official TapsiShop API documentation, the PUT payload's `id` field
# is documented as "the sku of your product" (the vendor-side identifier) —
# NOT TapsiShop's own internal `id` field returned by the GET list. This
# section proves both halves of that: the payload `id` equals the matched
# item's `sku`, and it is never the item's internal `id`.


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_put_uses_matched_items_sku_as_payload_id(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response(
            [
                _item(
                    id="1475748894590844928",  # TapsiShop's own internal id — never sent
                    hsin="1475748894590844929",  # the matching key from the sheet
                    sku="vendor-sku-abc",  # the vendor-side identifier — must become the PUT id
                    onHandQuantity=30,
                )
            ],
            total_count=1,
        ),
        _put_ack(),
    ]
    client = _client()

    variant_id = client.resolve_variant_id(
        _product(platform_refs={"tapsi_shop": "1475748894590844929"})
    )
    assert variant_id == "vendor-sku-abc"  # sku, not the hsin used to find it

    client.update_price(variant_id, 100_000_000)

    put_payload = mock_request.call_args.kwargs["json"]
    sent_id = put_payload["products"][0]["id"]
    assert sent_id == "vendor-sku-abc"  # sku
    assert sent_id != "1475748894590844929"  # never the hsin itself


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_put_id_is_never_the_internal_tapsi_id(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response(
            [
                _item(
                    id="internal-tapsi-id-999",
                    hsin="1475748894590844929",
                    sku="vendor-sku-abc",
                    onHandQuantity=30,
                )
            ],
            total_count=1,
        ),
        _put_ack(),
    ]
    client = _client()

    variant_id = client.resolve_variant_id(
        _product(platform_refs={"tapsi_shop": "1475748894590844929"})
    )
    client.update_price(variant_id, 100_000_000)

    sent_id = mock_request.call_args.kwargs["json"]["products"][0]["id"]
    assert sent_id != "internal-tapsi-id-999"  # the internal Tapsi id must never be used
    assert sent_id == "vendor-sku-abc"


# -- Reference price: originalPrice, never finalPrice ----------------------------------


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_reference_price_uses_original_price_not_final_price(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response(
        [_item(originalPrice=200_000_000.0, finalPrice=180_000_000.0)], total_count=1
    )
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 200_000_000
    assert price != 180_000_000


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_reference_price_missing_raises_clear_error(mock_request: MagicMock, tmp_path: Path):
    item = _item()
    del item["originalPrice"]
    mock_request.return_value = _page_response([item], total_count=1)
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="originalPrice"):
        client.get_reference_price(_product(), variant_id)


# -- Currency: Rial end-to-end, no double ×10 conversion --------------------------------


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_reference_price_is_returned_unchanged_no_conversion(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item(originalPrice=150_000_000.0)], total_count=1)
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 150_000_000  # exact passthrough — no ×10 or ÷10


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_update_sends_the_exact_rial_price_with_no_conversion(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", originalPrice=150_000_000.0, onHandQuantity=30)], total_count=1),
        _put_ack(),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    client.update_price("V1", 142_500_000)

    payload = mock_request.call_args.kwargs["json"]
    assert payload["products"][0]["price"] == 142_500_000
    assert payload["products"][0]["specialPrice"] == 142_500_000


# -- Stock: onHandQuantity preserved; missing -> clear error, never zero ---------------


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_stock_preserved_from_on_hand_quantity(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=273)], total_count=1),
        _put_ack(),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    client.update_price("V1", 100_000_000)

    payload = mock_request.call_args.kwargs["json"]
    assert payload["products"][0]["stock"] == 273


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_missing_on_hand_quantity_raises_clear_error_instead_of_zero(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item(sku="V1")
    del item["onHandQuantity"]
    mock_request.return_value = _page_response([item], total_count=1)
    client = _client()
    client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="onHandQuantity"):
        client.update_price("V1", 100_000_000)


# -- PUT /web/hub/vendors/v1/products payload shape -------------------------------------


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_update_sends_correct_endpoint_and_payload(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response(
            [_item(id="internal-tapsi-id", sku="vendor-sku-xyz", onHandQuantity=30)],
            total_count=1,
        ),
        _put_ack(),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    client.update_price(variant_id, 150_000_000)

    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    payload = mock_request.call_args.kwargs["json"]
    assert method == "PUT"
    assert url == "https://vendorgw.tapsi.shop/web/hub/vendors/v1/products"
    products = payload["products"]
    assert len(products) == 1
    sent = products[0]
    assert sent["id"] == "vendor-sku-xyz"
    assert sent["id"] != "internal-tapsi-id"
    assert sent["stock"] == 30
    assert sent["price"] == 150_000_000
    assert sent["specialPrice"] == 150_000_000
    assert isinstance(sent["referenceCode"], str) and sent["referenceCode"]


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_price_and_special_price_are_identical_when_no_discount(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=5)], total_count=1),
        _put_ack(),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    client.update_price("V1", 99_000_000)

    payload = mock_request.call_args.kwargs["json"]
    assert payload["products"][0]["price"] == payload["products"][0]["specialPrice"]


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_update_without_prior_resolve_raises_instead_of_sending_a_request(
    mock_request: MagicMock, tmp_path: Path
):
    client = _client()
    with pytest.raises(PlatformAPIError, match="no previously-fetched product"):
        client.update_price("never-resolved", 1_000_000)
    mock_request.assert_not_called()


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_business_level_failure_envelope_is_raised_as_an_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=30)], total_count=1),
        MagicMock(
            status_code=200,
            json=lambda: {"success": False, "messages": ["invalid price"]},
        ),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="invalid price"):
        client.update_price("V1", 100_000_000)


# -- PUT response validation: data.status and each data.data[] item's status ----------
#
# Per the official API documentation, the PUT response envelope is doubly
# nested: {"success", "messages", "data": {"status", "data": [{"id", "sku",
# "status", "messages", ...}, ...]}}. A top-level success=true does not
# guarantee data.status or any individual item's status is also true — an
# individual item can fail even when the request as a whole succeeded.


def _put_response(*, data_status: bool = True, items: list[dict] | None = None) -> MagicMock:
    return MagicMock(
        status_code=200,
        json=lambda: {
            "success": True,
            "messages": [],
            "data": {
                "status": data_status,
                "data": items if items is not None else [],
            },
        },
    )


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_per_item_status_false_is_detected_as_failure(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=30)], total_count=1),
        _put_response(
            items=[
                {
                    "id": "internal-tapsi-id",
                    "sku": "V1",
                    "status": False,
                    "messages": ["price below minimum allowed"],
                }
            ]
        ),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="price below minimum allowed"):
        client.update_price("V1", 100_000_000)


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_nested_data_status_false_is_detected_as_failure(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=30)], total_count=1),
        _put_response(data_status=False, items=[]),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="data.status"):
        client.update_price("V1", 100_000_000)


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_per_item_status_true_does_not_raise(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=30)], total_count=1),
        _put_response(
            items=[
                {
                    "id": "internal-tapsi-id",
                    "sku": "V1",
                    "status": True,
                    "messages": [],
                }
            ]
        ),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    client.update_price("V1", 100_000_000)  # must not raise


# -- Price/specialPrice must be a multiple of 10 Rial ----------------------------------
#
# Documented API requirement: "the entered price must be a multiple of 10
# and in Rial." The generic 12%-cap computation can produce a non-multiple
# of 10 even from a clean reference price, so this client rounds the
# computed price DOWN to the nearest multiple of 10 before sending it —
# never up, so it never sends a price higher than what the pricing
# algorithm decided.


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_non_multiple_of_ten_price_is_rounded_down_before_sending(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=30)], total_count=1),
        _put_ack(),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    client.update_price("V1", 100_000_007)

    payload = mock_request.call_args.kwargs["json"]
    assert payload["products"][0]["price"] == 100_000_000
    assert payload["products"][0]["specialPrice"] == 100_000_000


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_already_multiple_of_ten_price_is_sent_unchanged(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _page_response([_item(sku="V1", onHandQuantity=30)], total_count=1),
        _put_ack(),
    ]
    client = _client()
    client.resolve_variant_id(_product())

    client.update_price("V1", 100_000_030)

    payload = mock_request.call_args.kwargs["json"]
    assert payload["products"][0]["price"] == 100_000_030
    assert payload["products"][0]["specialPrice"] == 100_000_030


def test_dry_run_log_reflects_rounded_price(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(dry_run=True)
    with patch("faraz_honar.platforms.tapsishop.client.requests.request") as mock_request:
        with caplog.at_level("INFO"):
            client.update_price("some-id", 100_000_007)
        mock_request.assert_not_called()
    assert "100000000" in caplog.text
    assert "100000007" not in caplog.text


# -- DRY_RUN ----------------------------------------------------------------------------


def test_dry_run_does_not_call_requests():
    client = _client(dry_run=True)
    with patch("faraz_honar.platforms.tapsishop.client.requests.request") as mock_request:
        client.update_price("some-id", 100_000_000)
        mock_request.assert_not_called()


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_dry_run_still_allows_real_reads(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _page_response([_item(originalPrice=100_000_000.0)], total_count=1)
    client = _client(dry_run=True)

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 100_000_000
    mock_request.assert_called_once()
    assert mock_request.call_args.args[0] == "GET"


# -- Eligibility: empty/whitespace قیمت اصلی -> zero TapsiShop API calls --------------


def test_empty_target_price_means_zero_tapsi_requests_via_is_eligible():
    from faraz_honar.update.service import _is_eligible

    product = _product(target_price=None)
    assert _is_eligible(product, "tapsi_shop") is False


def test_whitespace_target_price_row_has_no_target_price_via_csv_parsing(tmp_path: Path):
    from faraz_honar.sheets.csv_source import CSVProductSource
    from faraz_honar.sheets.mapping import SheetMapping
    from faraz_honar.update.service import _is_eligible

    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "اسم,اسم جدید,قیمت,قیمت اصلی,تپسی شاپ\nP1,P1,500000,   ,1475748894590844929\n",
        encoding="utf-8",
    )
    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column="قیمت اصلی",
        platform_ref_columns={"tapsi_shop": "تپسی شاپ"},
    )
    products = CSVProductSource(csv_path, mapping).fetch_products()
    assert products[0].target_price is None
    assert _is_eligible(products[0], "tapsi_shop") is False


# -- Sheet mapping ------------------------------------------------------------------------


def test_sheet_mapping_yaml_maps_tapsi_shop_to_platform_ref_column():
    from faraz_honar.sheets.mapping import SheetMapping

    mapping_path = Path(__file__).parents[1] / "config" / "sheet_mapping.yaml"
    mapping = SheetMapping.load(mapping_path)
    assert mapping.platform_ref_columns["tapsi_shop"] == "تپسی شاپ"


# -- Pricing algorithm reused, not duplicated ----------------------------------------------


def test_tapsishop_does_not_define_its_own_pricing_module():
    import faraz_honar.platforms.tapsishop.client as tapsishop_client_module

    assert not hasattr(tapsishop_client_module, "compute_price_to_apply")
    assert "core.pricing" not in getattr(tapsishop_client_module, "__file__", "")


# -- The 12% cap does NOT apply to TapsiShop, exercised via the real service ----------
#
# core/pricing.py's CAPPED_PLATFORMS restricts the 12%-max-increase rule to
# the two DigiKala accounts only ("digikala", "digikala2"). TapsiShop is
# not in that set, so a target price is always applied directly here, no
# matter how large an increase it is over the reference price.


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_generic_12pct_rule_within_range_updates_to_target(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            return [_product(target_price=105_000_000)]  # +5% over the 100M reference below

    mock_request.side_effect = [
        _page_response([_item(id="V1", originalPrice=100_000_000.0, onHandQuantity=30)], total_count=1),
        _put_ack(),
    ]
    client = _client()
    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry={"tapsi_shop": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.run(["tapsi_shop"], triggered_by="test")["tapsi_shop"]

    assert len(summary.successful) == 1
    assert summary.successful[0].applied_price == 105_000_000
    put_call = next(c for c in mock_request.call_args_list if c.args[0] == "PUT")
    assert put_call.kwargs["json"]["products"][0]["price"] == 105_000_000


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_tapsi_shop_does_not_cap_a_large_increase(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            return [_product(target_price=200_000_000)]  # +100% over the 100M reference below

    mock_request.side_effect = [
        _page_response([_item(id="V1", originalPrice=100_000_000.0, onHandQuantity=30)], total_count=1),
        _put_ack(),
    ]
    client = _client()
    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry={"tapsi_shop": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.run(["tapsi_shop"], triggered_by="test")["tapsi_shop"]

    # Not capped: TapsiShop is not in CAPPED_PLATFORMS, so the full +100%
    # target price applies directly in one run.
    assert len(summary.capped) == 0
    assert len(summary.successful) == 1
    assert summary.successful[0].applied_price == 200_000_000
    put_call = next(c for c in mock_request.call_args_list if c.args[0] == "PUT")
    assert put_call.kwargs["json"]["products"][0]["price"] == 200_000_000


# -- Isolation: TapsiShop never contaminates the other platforms' results ------------


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_tapsi_shop_run_does_not_contaminate_other_platforms(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.platforms.base import PlatformClient
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _FakeClient(PlatformClient):
        def __init__(self, name: str):
            self.name = name

        def resolve_variant_id(self, product: Product) -> str:
            return f"V-{self.name}-{product.internal_id}"

        def get_reference_price(self, product: Product, variant_id: str) -> int:
            assert product.reference_price is not None
            return product.reference_price

        def update_price(self, variant_id: str, new_price: int) -> None:
            pass

    other_platforms = ["digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan"]
    other_products = [
        Product(
            internal_id=f"{platform}-item",
            name=f"{platform}-item",
            reference_price=1_000_000,
            target_price=1_050_000,
            platform_refs={platform: f"ref-{platform}"}, display_name=f"{platform}-item", green_platforms=frozenset({platform}),
        )
        for platform in other_platforms
    ]
    tapsi_product = _product(
        internal_id="tapsi-item", name="tapsi-item", target_price=105_000_000
    )
    all_products = [*other_products, tapsi_product]

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            return list(all_products)

    mock_request.side_effect = [
        _page_response(
            [_item(id="V1", hsin="1475748894590844929", originalPrice=100_000_000.0, onHandQuantity=30)],
            total_count=1,
        ),
        _put_ack(),
    ]
    registry: dict[str, PlatformClient] = {p: _FakeClient(p) for p in other_platforms}
    registry["tapsi_shop"] = _client()

    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry=registry,
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    tapsi_summary = service.run(["tapsi_shop"], triggered_by="test")["tapsi_shop"]

    tapsi_ids = {r.product.internal_id for r in tapsi_summary.results}
    assert tapsi_ids == {"tapsi-item"}
    assert all(r.platform == "tapsi_shop" for r in tapsi_summary.results)

    for platform in other_platforms:
        other_summary = service.run([platform], triggered_by="test")[platform]
        other_ids = {r.product.internal_id for r in other_summary.results}
        assert other_ids == {f"{platform}-item"}
        assert "tapsi-item" not in other_ids
        assert all(r.platform == platform for r in other_summary.results)


# -- Price check (read-only "🔍 چک و بررسی قیمت‌ها" button) -----------------------------


def _catalog_item(n: int, **overrides: Any) -> dict:
    return _item(
        id=f"id-{n}",
        hsin=f"hsin-{n}",
        sku=f"sku-{n}",
        originalPrice=float(n * 1_000_000),
        **overrides,
    )


def _catalog_product(n: int) -> Product:
    return _product(internal_id=f"p{n}", platform_refs={"tapsi_shop": f"hsin-{n}"})


def _three_item_catalog() -> list[MagicMock]:
    # page_size=2 and totalCount=3 -> exactly two pages.
    return [
        _page_response([_catalog_item(1), _catalog_item(2)], total_count=3, page=1, page_size=2),
        _page_response([_catalog_item(3)], total_count=3, page=2, page_size=2),
    ]


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_prepare_price_check_reads_every_page_once_and_resolving_needs_no_more_requests(
    mock_request: MagicMock,
):
    mock_request.side_effect = _three_item_catalog()
    client = _client(page_size=2)

    client.prepare_price_check()
    assert mock_request.call_count == 2  # the whole catalog, one pass

    # Products from BOTH pages resolve and price without any further request.
    for n in (1, 2, 3):
        product = _catalog_product(n)
        variant_id = client.resolve_variant_id(product)
        assert variant_id == f"sku-{n}"
        assert client.get_current_price(product, variant_id) == n * 1_000_000
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_unknown_code_during_a_check_raises_variant_resolution_error_without_requests(
    mock_request: MagicMock,
):
    mock_request.side_effect = _three_item_catalog()
    client = _client(page_size=2)
    client.prepare_price_check()

    with pytest.raises(VariantResolutionError, match="hsin-999") as excinfo:
        client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "hsin-999"}))

    assert excinfo.value.retryable is False
    assert mock_request.call_count == 2  # did not go looking page by page


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_finish_price_check_restores_the_page_by_page_lookup(mock_request: MagicMock):
    mock_request.side_effect = _three_item_catalog()
    client = _client(page_size=2)
    client.prepare_price_check()
    client.finish_price_check()
    mock_request.reset_mock()

    # Old behavior: a product on page 1 costs one request (stops once found)...
    mock_request.side_effect = _three_item_catalog()
    assert client.resolve_variant_id(_catalog_product(1)) == "sku-1"
    assert mock_request.call_count == 1

    # ...and one on page 2 costs two (walks from page 1 each time).
    mock_request.reset_mock()
    mock_request.side_effect = _three_item_catalog()
    assert client.resolve_variant_id(_catalog_product(3)) == "sku-3"
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.tapsishop.client.requests.request")
def test_finish_price_check_makes_an_unknown_code_walk_every_page_again(mock_request: MagicMock):
    mock_request.side_effect = _three_item_catalog()
    client = _client(page_size=2)
    client.prepare_price_check()
    client.finish_price_check()
    mock_request.reset_mock()
    mock_request.side_effect = _three_item_catalog()

    with pytest.raises(VariantResolutionError, match="after checking all"):
        client.resolve_variant_id(_product(platform_refs={"tapsi_shop": "hsin-999"}))

    assert mock_request.call_count == 2


def test_estimate_check_seconds_is_per_page_not_per_product():
    client = _client(page_size=100, read_delay_seconds=1.0)

    # 250 products = 3 pages of one throttled read each; not 250 reads.
    assert client.estimate_check_seconds(250) == 3 * (1.0 + 0.5)


def test_price_step_is_10_rial_and_expected_price_rounds_down():
    client = _client()

    assert client.price_step_rial == 10
    assert client.expected_price_for_target(1_234_567) == 1_234_560
    assert client.expected_price_for_target(1_234_560) == 1_234_560  # already a multiple
