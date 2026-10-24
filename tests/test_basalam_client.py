from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from faraz_honar.core.exceptions import PlatformAPIError, VariantResolutionError
from faraz_honar.core.models import Product
from faraz_honar.platforms.basalam.client import VENDOR_ID, BasalamClient, _chunked


def _client(**overrides: Any) -> BasalamClient:
    defaults: dict[str, Any] = dict(
        base_url="https://openapi.basalam.com/v1",
        token="the-basalam-token",
        dry_run=False,
        # Delays default to 0 so tests not specifically about throttling
        # stay fast and deterministic (same convention as the other
        # platform client test files).
        read_delay_seconds=0.0,
        write_delay_seconds=0.0,
    )
    defaults.update(overrides)
    return BasalamClient(**defaults)


def _product(**overrides: Any) -> Product:
    # The "باسلام" sheet column stores the Basalam product id directly
    # (confirmed by the verified API spec) — not a human-facing sku/code.
    defaults: dict[str, Any] = dict(
        internal_id="p1",
        name="Test",
        reference_price=50_000_000,
        target_price=52_000_000,
        platform_refs={"basalam": "58964699"},
    )
    defaults.update(overrides)
    # Eligibility needs "اسم جدید" and a GREEN code cell per platform.
    defaults.setdefault("display_name", defaults["name"])
    defaults.setdefault("green_platforms", frozenset(defaults["platform_refs"]))
    return Product(**defaults)


def _item(**overrides: Any) -> dict:
    # Matches the documented real API response shape (both the original
    # implementation spec and Basalam's own OpenAPI ReadProductResponse
    # schema for GET /v1/products/{product_id}).
    defaults: dict[str, Any] = dict(
        id=58964699,
        title="Product Title",
        primary_price=52_800_000,
        inventory=30,
        sku="vendor_sku",
    )
    defaults.update(overrides)
    return defaults


def _product_response(item: dict) -> MagicMock:
    # CONFIRMED against Basalam's own OpenAPI spec: GET /v1/products/
    # {product_id}'s 200 body IS the product object itself at the top
    # level — not wrapped in a "data"/list envelope like the old vendor
    # product-list endpoint was.
    return MagicMock(status_code=200, json=lambda: item)


def _patch_ack() -> MagicMock:
    # CONFIRMED against a real response (product id 28317496): the
    # batch-updates endpoint responds HTTP 202, with the real per-item
    # outcome in openapi_raw_data[] (see BasalamClient._raise_for_item_
    # failure). The id here doesn't need to match every test's specific
    # variant_id — an entry for a different id is treated as "no
    # information for this item", never as a failure (see
    # test_no_matching_id_in_openapi_raw_data_does_not_raise below).
    return MagicMock(
        status_code=202,
        json=lambda: {
            "openapi_raw_data": [
                {"id": 0, "has_error": False, "error_message": None, "is_product_for_revision": False}
            ]
        },
    )


# -- Authentication: exact headers -----------------------------------------------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_authorization_header_is_bearer_token(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item())
    client = _client(token="secret-basalam-token")

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret-basalam-token"


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_other_required_headers_are_exact(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item())
    client = _client()

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    # No 'prefer' header: the OpenAPI spec's only documented value is its
    # own default ("return=minimal"); no alternative value is documented
    # for retrieving the full field set, so this client omits the header
    # entirely rather than inventing an undocumented value (see module
    # docstring).
    assert set(headers.keys()) == {"Authorization", "Accept", "Content-Type"}


def test_missing_token_raises_configuration_error():
    client = _client(token=None)
    with patch("faraz_honar.platforms.basalam.client.requests.request") as mock_request:
        with pytest.raises(Exception, match="Basalam is not configured"):
            client.resolve_variant_id(_product())
        mock_request.assert_not_called()


# -- Vendor: PATCH always uses 1058040; GET-by-id needs no vendor id at all -----------
#
# CONFIRMED against Basalam's own OpenAPI spec: GET /v1/products/{product_id}
# has no vendor scoping in its path at all (unlike the old vendor
# product-list endpoint) — the vendor id is never part of the GET request.
# The batch-updates PATCH endpoint is unchanged and still vendor-scoped.


def test_vendor_id_constant_is_the_store_id_not_the_jwt_sub():
    assert VENDOR_ID == 1058040
    assert VENDOR_ID != 17351469


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_get_url_contains_product_id_not_vendor_id(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(id=28317496))
    client = _client()

    client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    url = mock_request.call_args.args[1]
    assert url == "https://openapi.basalam.com/v1/products/28317496"
    assert "vendors" not in url
    assert "1058040" not in url
    assert "17351469" not in url


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_patch_uses_correct_vendor_in_url(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [_product_response(_item()), _patch_ack()]
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    client.update_price(variant_id, 55_000_000)

    patch_call = next(c for c in mock_request.call_args_list if c.args[0] == "PATCH")
    url = patch_call.args[1]
    assert url == "https://openapi.basalam.com/v1/vendors/1058040/products/batch-updates"
    assert "17351469" not in url


# -- GET /v1/products/{product_id}: direct lookup, no pagination ----------------------
#
# CONFIRMED against Basalam's own OpenAPI spec (basalam/python-sdk's
# openapi_data/core.json): a direct single-product-by-id endpoint exists,
# replacing the earlier vendor product-list pagination entirely.


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_get_uses_correct_endpoint_with_product_id_in_path(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(id=28317496))
    client = _client()

    client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    assert method == "GET"
    assert url == "https://openapi.basalam.com/v1/products/28317496"


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_get_sends_no_query_params(mock_request: MagicMock, tmp_path: Path):
    # No page/per_page/variants_flatting -- those belonged to the old
    # list-endpoint implementation and no longer apply to a direct lookup.
    mock_request.return_value = _product_response(_item())
    client = _client()

    client.resolve_variant_id(_product())

    assert mock_request.call_args.kwargs["params"] is None


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_resolution_makes_exactly_one_get_request_no_pagination(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _product_response(_item(id=28317496))
    client = _client()

    client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    assert mock_request.call_count == 1


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_product_not_found_raises_variant_resolution_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=404, text="not found")
    client = _client()

    with pytest.raises(VariantResolutionError):
        client.resolve_variant_id(_product(platform_refs={"basalam": "does-not-exist"}))
    assert mock_request.call_count == 1  # a single GET, never a pagination search


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_unrecognized_response_shape_raises_clear_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=200, json=lambda: {"unexpected": "shape"})
    client = _client()
    with pytest.raises(PlatformAPIError, match="unrecognized shape"):
        client.resolve_variant_id(_product())


# -- Product parsing: id, title, primary_price, inventory, sku ------------------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_product_fields_are_parsed_correctly(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(
        _item(
            id=58964699,
            title="Product Title",
            primary_price=52_800_000,
            inventory=30,
            sku="vendor_sku",
        )
    )
    client = _client()
    product = _product(platform_refs={"basalam": "58964699"})

    variant_id = client.resolve_variant_id(product)
    reference_price = client.get_reference_price(product, variant_id)

    assert variant_id == "58964699"
    assert reference_price == 52_800_000
    item = client._last_resolved_item_by_variant[variant_id]
    assert item["title"] == "Product Title"
    assert item["inventory"] == 30
    assert item["sku"] == "vendor_sku"


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_null_sku_does_not_break_resolution(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(id=58964699, sku=None))
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "58964699"}))
    assert variant_id == "58964699"


# -- Product identification: the sheet's Basalam id is used directly, never sku/title --


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_sheet_value_is_used_directly_as_the_product_id_path_parameter(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _product_response(_item(id=28317496))
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    assert variant_id == "28317496"
    url = mock_request.call_args.args[1]
    assert url.endswith("/products/28317496")


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_sheet_value_maps_directly_to_the_matched_items_id(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(id=58964699))
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "58964699"}))
    assert variant_id == "58964699"


# -- Reference price: primary_price only, never sku/inventory/title -------------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_uses_primary_price(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(primary_price=200_000_000))
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 200_000_000


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_missing_raises_clear_error(mock_request: MagicMock, tmp_path: Path):
    item = _item()
    del item["primary_price"]
    mock_request.return_value = _product_response(item)
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="primary_price"):
        client.get_reference_price(_product(), variant_id)


# -- Reference price fallback: primary_price can be null, price is the real value -----
#
# CONFIRMED against a real Basalam response (product id 28317496):
# primary_price can be explicitly null (not just absent) while the actual
# current price is available under a sibling 'price' field. This is NOT an
# error — get_reference_price must fall back to 'price' in that case.


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_uses_primary_price_when_present_and_not_null(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _product_response(_item(primary_price=52_800_000, price=50_000_000))
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    # primary_price is preferred over price when both are present.
    assert price == 52_800_000


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_falls_back_to_price_when_primary_price_is_null(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _product_response(_item(primary_price=None, price=50_000_000))
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 50_000_000


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_falls_back_to_price_when_primary_price_is_missing(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item(price=50_000_000)
    del item["primary_price"]
    mock_request.return_value = _product_response(item)
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 50_000_000


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_raises_when_both_primary_price_and_price_are_null(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _product_response(_item(primary_price=None, price=None))
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="primary_price.*price"):
        client.get_reference_price(_product(), variant_id)


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_raises_when_both_primary_price_and_price_are_missing(
    mock_request: MagicMock, tmp_path: Path
):
    item = _item()
    del item["primary_price"]
    mock_request.return_value = _product_response(item)  # no 'price' key either
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="primary_price.*price"):
        client.get_reference_price(_product(), variant_id)


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_fallback_value_is_not_converted(mock_request: MagicMock, tmp_path: Path):
    # The 'price' fallback must obey the exact same no-×10/÷10 rule as
    # primary_price (see currency section of the module docstring).
    mock_request.return_value = _product_response(_item(primary_price=None, price=50_000_000))
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 50_000_000
    assert price != 500_000_000
    assert price != 5_000_000


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reference_price_is_returned_unchanged_no_conversion(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(primary_price=52_800_000))
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 52_800_000  # exact passthrough — no ×10 or ÷10


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_get_reference_price_fallback_refetches_by_id_when_not_cached(
    mock_request: MagicMock, tmp_path: Path
):
    # get_reference_price's own fallback path (cache miss) also uses the
    # direct by-id GET, never a pagination search.
    mock_request.return_value = _product_response(_item(id=28317496, primary_price=99_000_000))
    client = _client()

    price = client.get_reference_price(_product(), "28317496")

    assert price == 99_000_000
    assert mock_request.call_count == 1
    assert mock_request.call_args.args[1].endswith("/products/28317496")


# -- Stock: GET inventory -> PATCH stock, never sent as 'inventory' -------------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_stock_preserved_from_inventory_sent_as_stock(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [_product_response(_item(inventory=273)), _patch_ack()]
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    client.update_price(variant_id, 100_000_000)

    payload = mock_request.call_args.kwargs["json"]
    sent = payload["data"][0]
    assert sent["stock"] == 273
    assert "inventory" not in sent


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_missing_inventory_raises_clear_error_instead_of_zero(mock_request: MagicMock, tmp_path: Path):
    item = _item()
    del item["inventory"]
    mock_request.return_value = _product_response(item)
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    with pytest.raises(PlatformAPIError, match="inventory"):
        client.update_price(variant_id, 100_000_000)


# -- PATCH /vendors/1058040/products/batch-updates payload shape ----------------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_patch_sends_correct_endpoint_and_payload(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _product_response(_item(id=58964699, inventory=30)),
        _patch_ack(),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    client.update_price(variant_id, 52_800_000)

    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    payload = mock_request.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url == "https://openapi.basalam.com/v1/vendors/1058040/products/batch-updates"
    assert payload == {
        "data": [{"id": 58964699, "stock": 30, "primary_price": 52_800_000}]
    }


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_patch_payload_id_is_an_integer_not_a_string(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [_product_response(_item(id=58964699, inventory=30)), _patch_ack()]
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    client.update_price(variant_id, 52_800_000)

    sent = mock_request.call_args.kwargs["json"]["data"][0]
    assert isinstance(sent["id"], int)


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_patch_never_includes_unrelated_fields(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _product_response(_item(id=58964699, inventory=30, title="X", sku="Y")),
        _patch_ack(),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    client.update_price(variant_id, 52_800_000)

    sent = mock_request.call_args.kwargs["json"]["data"][0]
    assert set(sent.keys()) == {"id", "stock", "primary_price"}


# -- PATCH response validation: HTTP 202 + per-item has_error/error_message -----------
#
# CONFIRMED against a real response (product id 28317496): the batch-updates
# endpoint responds HTTP 202 Accepted (not 200/201), with the body
# {"openapi_raw_data": [{"id", "has_error", "error_message",
# "is_product_for_revision"}]}. 202 alone is not a success/failure signal —
# the real per-item outcome is has_error/error_message. Unaffected by the
# GET-by-id change (PATCH is untouched) — only the preceding GET mock below
# is adapted to the new single-object response shape.


def test_202_is_not_treated_as_an_error_status():
    response = MagicMock(status_code=202, text="{}")
    BasalamClient._raise_for_error_status(response, context="price update")  # must not raise


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_http_202_with_has_error_false_is_treated_as_success(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _product_response(_item(id=28317496, inventory=15)),
        MagicMock(
            status_code=202,
            json=lambda: {
                "openapi_raw_data": [
                    {
                        "id": 28317496,
                        "has_error": False,
                        "error_message": None,
                        "is_product_for_revision": False,
                    }
                ]
            },
        ),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    client.update_price(variant_id, 50_000_000)  # must not raise


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_http_202_with_has_error_true_raises_with_error_message(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _product_response(_item(id=28317496, inventory=15)),
        MagicMock(
            status_code=202,
            json=lambda: {
                "openapi_raw_data": [
                    {
                        "id": 28317496,
                        "has_error": True,
                        "error_message": "price below minimum allowed",
                        "is_product_for_revision": False,
                    }
                ]
            },
        ),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    with pytest.raises(PlatformAPIError) as excinfo:
        client.update_price(variant_id, 50_000_000)

    assert "price below minimum allowed" in str(excinfo.value)
    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_missing_openapi_raw_data_does_not_raise(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _product_response(_item(id=28317496, inventory=15)),
        MagicMock(status_code=202, json=lambda: {"success": True}),  # unexpected/legacy shape
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    client.update_price(variant_id, 50_000_000)  # must not raise or crash


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_openapi_raw_data_not_a_list_does_not_raise(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _product_response(_item(id=28317496, inventory=15)),
        MagicMock(status_code=202, json=lambda: {"openapi_raw_data": {"unexpected": "shape"}}),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    client.update_price(variant_id, 50_000_000)  # must not raise or crash


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_openapi_raw_data_entries_not_dicts_does_not_raise(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _product_response(_item(id=28317496, inventory=15)),
        MagicMock(status_code=202, json=lambda: {"openapi_raw_data": ["unexpected", 123, None]}),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    client.update_price(variant_id, 50_000_000)  # must not raise or crash


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_non_json_patch_body_does_not_raise(mock_request: MagicMock, tmp_path: Path):
    non_json_response = MagicMock(status_code=202, text="not json")
    non_json_response.json.side_effect = ValueError("not json")
    mock_request.side_effect = [
        _product_response(_item(id=28317496, inventory=15)),
        non_json_response,
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    client.update_price(variant_id, 50_000_000)  # must not raise or crash


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_no_matching_id_in_openapi_raw_data_does_not_raise(mock_request: MagicMock, tmp_path: Path):
    # An entry exists but for a different product id -> treated as "no
    # information for this item", never as a failure.
    mock_request.side_effect = [
        _product_response(_item(id=28317496, inventory=15)),
        MagicMock(
            status_code=202,
            json=lambda: {
                "openapi_raw_data": [
                    {"id": 999999, "has_error": True, "error_message": "unrelated failure"}
                ]
            },
        ),
    ]
    client = _client()
    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "28317496"}))

    client.update_price(variant_id, 50_000_000)  # must not raise or crash


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_update_without_prior_resolve_raises_instead_of_sending_a_request(
    mock_request: MagicMock, tmp_path: Path
):
    client = _client()
    with pytest.raises(PlatformAPIError, match="no previously-fetched product"):
        client.update_price("never-resolved", 1_000_000)
    mock_request.assert_not_called()


# -- Currency: CRITICAL — no second ×10 conversion in the Basalam client --------------
#
# sheets/mapping.py already converts the sheet's Toman target price to Rial
# (×10) before it ever reaches this client, or the generic pricing service.
# This client must send that already-converted Rial value to `primary_price`
# completely unchanged — multiplying it by 10 again here would silently
# produce an effective ×100 conversion.


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_no_second_currency_conversion_in_basalam_client(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [_product_response(_item(inventory=30)), _patch_ack()]
    client = _client()
    variant_id = client.resolve_variant_id(_product())

    # An already-converted internal Rial price (as the generic pricing
    # service would compute and pass in) — must be sent byte-for-byte.
    client.update_price(variant_id, 52_800_000)

    sent_price = mock_request.call_args.kwargs["json"]["data"][0]["primary_price"]
    assert sent_price == 52_800_000
    assert sent_price != 528_000_000  # would be the incorrect ×10-again result
    assert sent_price != 5_280_000  # would be the incorrect ÷10 result


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_get_reference_price_no_conversion_either_direction(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(primary_price=52_800_000))
    client = _client()

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 52_800_000
    assert price != 528_000_000
    assert price != 5_280_000


# -- Batch chunking: large update sets are split into multiple PATCH requests ---------


def test_chunked_splits_into_batches_of_the_given_size():
    items = [{"id": i} for i in range(120)]
    batches = _chunked(items, 50)
    assert [len(b) for b in batches] == [50, 50, 20]


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_small_batch_size_splits_a_single_products_update_call(mock_request: MagicMock, tmp_path: Path):
    # update_price is called once per product by the orchestration service,
    # so this proves the chunking machinery is real (not a no-op) by using
    # an artificially small batch_size — with 1 item and batch_size=1, this
    # sends exactly one PATCH request with exactly one item.
    mock_request.side_effect = [_product_response(_item(id=58964699, inventory=30)), _patch_ack()]
    client = _client(batch_size=1)
    variant_id = client.resolve_variant_id(_product())

    client.update_price(variant_id, 52_800_000)

    patch_calls = [c for c in mock_request.call_args_list if c.args[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert len(patch_calls[0].kwargs["json"]["data"]) == 1


# -- Eligibility: empty/whitespace قیمت اصلی -> zero Basalam API calls ----------------


def test_empty_target_price_means_zero_basalam_requests_via_is_eligible():
    from faraz_honar.update.service import _is_eligible

    product = _product(target_price=None)
    assert _is_eligible(product, "basalam") is False


def test_whitespace_target_price_row_has_no_target_price_via_csv_parsing(tmp_path: Path):
    from faraz_honar.sheets.csv_source import CSVProductSource
    from faraz_honar.sheets.mapping import SheetMapping
    from faraz_honar.update.service import _is_eligible

    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "اسم,اسم جدید,قیمت,قیمت اصلی,باسلام\nP1,P1,500000,   ,58964699\n",
        encoding="utf-8",
    )
    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column="قیمت اصلی",
        platform_ref_columns={"basalam": "باسلام"},
    )
    products = CSVProductSource(csv_path, mapping).fetch_products()
    assert products[0].target_price is None
    assert _is_eligible(products[0], "basalam") is False


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_ineligible_product_never_reaches_basalam_client(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            return [_product(target_price=None)]

    client = _client()
    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry={"basalam": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.run(["basalam"], triggered_by="test")["basalam"]

    assert len(summary.results) == 0
    mock_request.assert_not_called()


# -- Sheet mapping ------------------------------------------------------------------------


def test_sheet_mapping_yaml_maps_basalam_to_the_existing_platform_ref_column():
    from faraz_honar.sheets.mapping import SheetMapping

    mapping_path = Path(__file__).parents[1] / "config" / "sheet_mapping.yaml"
    mapping = SheetMapping.load(mapping_path)
    assert mapping.platform_ref_columns["basalam"] == "باسلام"


# -- Pricing algorithm reused, not duplicated ----------------------------------------------


def test_basalam_does_not_define_its_own_pricing_module():
    import faraz_honar.platforms.basalam.client as basalam_client_module

    assert not hasattr(basalam_client_module, "compute_price_to_apply")
    assert "core.pricing" not in getattr(basalam_client_module, "__file__", "")


# -- 12% rule (DigiKala-only cap does not apply, but the SAME generic service/
#    algorithm is still used and correctly decides the reachable price) --------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_generic_12pct_rule_within_range_updates_to_target(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            return [_product(target_price=52_500_000)]  # +5% over the 50M reference below

    mock_request.side_effect = [
        _product_response(_item(id=58964699, primary_price=50_000_000, inventory=30)),
        _patch_ack(),
    ]
    client = _client()
    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry={"basalam": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.run(["basalam"], triggered_by="test")["basalam"]

    assert len(summary.successful) == 1
    assert summary.successful[0].applied_price == 52_500_000
    patch_call = next(c for c in mock_request.call_args_list if c.args[0] == "PATCH")
    assert patch_call.kwargs["json"]["data"][0]["primary_price"] == 52_500_000


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_basalam_does_not_cap_a_large_increase(mock_request: MagicMock, tmp_path: Path):
    # Basalam is not in core/pricing.py's CAPPED_PLATFORMS (that rule is
    # DigiKala-only), so the SAME generic PriceUpdateService/compute_price_
    # to_apply call simply decides not to cap here — proving the existing
    # 12% machinery is reused, not bypassed or duplicated.
    from faraz_honar.sheets.base import ProductSource
    from faraz_honar.state.repository import StateRepository
    from faraz_honar.update.service import PriceUpdateService

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            return [_product(target_price=100_000_000)]  # +100% over the 50M reference below

    mock_request.side_effect = [
        _product_response(_item(id=58964699, primary_price=50_000_000, inventory=30)),
        _patch_ack(),
    ]
    client = _client()
    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry={"basalam": client},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    summary = service.run(["basalam"], triggered_by="test")["basalam"]

    assert len(summary.capped) == 0
    assert len(summary.successful) == 1
    assert summary.successful[0].applied_price == 100_000_000
    patch_call = next(c for c in mock_request.call_args_list if c.args[0] == "PATCH")
    assert patch_call.kwargs["json"]["data"][0]["primary_price"] == 100_000_000


# -- DRY_RUN ----------------------------------------------------------------------------


def test_dry_run_does_not_call_requests():
    client = _client(dry_run=True)
    with patch("faraz_honar.platforms.basalam.client.requests.request") as mock_request:
        client.update_price("58964699", 100_000_000)
        mock_request.assert_not_called()


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_dry_run_still_allows_real_reads(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _product_response(_item(primary_price=100_000_000))
    client = _client(dry_run=True)

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 100_000_000
    mock_request.assert_called_once()
    assert mock_request.call_args.args[0] == "GET"


# -- Isolation: Basalam never contaminates the other platforms' results ---------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_basalam_run_does_not_contaminate_other_platforms(mock_request: MagicMock, tmp_path: Path):
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

    other_platforms = ["digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan", "tapsi_shop"]
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
    basalam_product = _product(internal_id="basalam-item", name="basalam-item", target_price=52_500_000)
    all_products = [*other_products, basalam_product]

    class _Source(ProductSource):
        def fetch_products(self) -> list[Product]:
            return list(all_products)

    mock_request.side_effect = [
        _product_response(_item(id=58964699, primary_price=50_000_000, inventory=30)),
        _patch_ack(),
    ]
    registry: dict[str, PlatformClient] = {p: _FakeClient(p) for p in other_platforms}
    registry["basalam"] = _client()

    service = PriceUpdateService(
        product_source=_Source(),
        platform_registry=registry,
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=2,
    )

    basalam_summary = service.run(["basalam"], triggered_by="test")["basalam"]

    basalam_ids = {r.product.internal_id for r in basalam_summary.results}
    assert basalam_ids == {"basalam-item"}
    assert all(r.platform == "basalam" for r in basalam_summary.results)

    for platform in other_platforms:
        other_summary = service.run([platform], triggered_by="test")[platform]
        other_ids = {r.product.internal_id for r in other_summary.results}
        assert other_ids == {f"{platform}-item"}
        assert "basalam-item" not in other_ids
        assert all(r.platform == platform for r in other_summary.results)


# -- Registry / Telegram wiring ----------------------------------------------------------


def test_basalam_is_registered_in_the_platform_registry():
    from faraz_honar.config import Settings
    from faraz_honar.platforms.registry import build_platform_registry

    settings = Settings(_env_file=None)
    registry = build_platform_registry(settings)
    assert "basalam" in registry
    assert registry["basalam"].name == "basalam"


def test_basalam_has_a_telegram_button_and_display_name():
    from faraz_honar.telegram_bot.keyboards import (
        platform_button_label,
        platform_display_name,
        platform_from_button_label,
    )

    label = platform_button_label("basalam")
    # Real entry (with the cart emoji and the blue-circle platform-family
    # marker), not the generic .title() fallback (which would return the
    # bare "Basalam" with no emoji at all).
    assert label == "🛒 🔵 Basalam"
    assert platform_from_button_label(label) == "basalam"
    # Finglish, not Persian, per the 2026-09 platform-name rename; blue
    # circle marks Basalam per the 2026-09 platform-color convention.
    assert platform_display_name("basalam") == "🔵 Basalam"


# -- Retry/error classification: same conventions as the other platforms --------------


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_401_is_auth_error_not_retryable(mock_request: MagicMock, tmp_path: Path):
    from faraz_honar.core.exceptions import PlatformAuthError

    mock_request.return_value = MagicMock(status_code=401, text="unauthorized")
    client = _client()
    with pytest.raises(PlatformAuthError) as excinfo:
        client.resolve_variant_id(_product())
    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_400_is_not_retryable(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=400, text="bad request")
    client = _client()
    with pytest.raises(PlatformAPIError) as excinfo:
        client.resolve_variant_id(_product())
    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_5xx_is_retryable(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=503, text="upstream unavailable")
    client = _client()
    with pytest.raises(PlatformAPIError) as excinfo:
        client.resolve_variant_id(_product())
    assert excinfo.value.retryable is True


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_network_error_is_retryable(mock_request: MagicMock, tmp_path: Path):
    import requests

    mock_request.side_effect = requests.ConnectionError("boom")
    client = _client()
    with pytest.raises(PlatformAPIError) as excinfo:
        client.resolve_variant_id(_product())
    assert excinfo.value.retryable is True


# -- Throttling: all Basalam requests pass through the shared throttle mechanism -------


@patch("faraz_honar.platforms.basalam.client.time.sleep")
@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_reads_are_throttled_independently_from_writes(
    mock_request: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [_product_response(_item(id=58964699, inventory=30)), _patch_ack()]
    client = _client(read_delay_seconds=5.0, write_delay_seconds=5.0)

    # First GET and first PATCH are each the "first request of their kind"
    # -> never delayed (see _throttle's documented contract).
    client.resolve_variant_id(_product())
    client.update_price("58964699", 100_000_000)

    mock_sleep.assert_not_called()


# -- Price check (read-only "🔍 چک و بررسی قیمت‌ها" button) -----------------------------


def test_price_step_is_1_rial_so_the_expected_price_is_never_rounded():
    client = _client()

    assert client.price_step_rial == 1
    assert client.expected_price_for_target(1_234_567) == 1_234_567


# -- Read-only price check: bulk catalog read (prepare_price_check) -------------------


def _list_response(items: list[dict], *, total_page: int | None, page: int = 1) -> MagicMock:
    body: dict[str, Any] = {"data": items, "page": page}
    if total_page is not None:
        body["total_page"] = total_page
    return MagicMock(status_code=200, json=lambda: body)


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_prepare_price_check_reads_the_catalog_in_pages_and_resolves_without_extra_gets(
    mock_request: MagicMock,
):
    mock_request.side_effect = [
        _list_response([_item(id=1, primary_price=100)], total_page=2, page=1),
        _list_response([_item(id=2, primary_price=200)], total_page=2, page=2),
    ]
    client = _client(list_page_size=1)
    client.prepare_price_check()
    assert mock_request.call_count == 2
    first = mock_request.call_args_list[0]
    assert first.args[0] == "GET"
    assert first.args[1].endswith(f"/vendors/{VENDOR_ID}/products")
    assert first.kwargs["params"] == {"page": 1, "per_page": 1, "variants_flatting": "false"}

    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "2"}))
    assert variant_id == "2"
    assert client.get_current_price(_product(), variant_id) == 200
    assert mock_request.call_count == 2  # nothing fetched per product


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_price_check_falls_back_to_a_single_get_for_a_product_missing_from_the_bulk_read(
    mock_request: MagicMock,
):
    mock_request.side_effect = [
        _list_response([_item(id=1, primary_price=100)], total_page=1),
        _product_response(_item(id=77, primary_price=700)),
    ]
    client = _client()
    client.prepare_price_check()

    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "77"}))

    assert variant_id == "77"
    assert client.get_current_price(_product(), variant_id) == 700
    assert mock_request.call_args_list[1].args[1].endswith("/products/77")


@patch("faraz_honar.platforms.basalam.client.time.sleep")
@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_failed_bulk_read_falls_back_to_one_get_per_product(
    mock_request: MagicMock, _sleep: MagicMock
):
    mock_request.side_effect = [
        MagicMock(status_code=500, text="boom", json=lambda: {}),
        MagicMock(status_code=500, text="boom", json=lambda: {}),
        MagicMock(status_code=500, text="boom", json=lambda: {}),
        _product_response(_item(id=5, primary_price=500)),
    ]
    client = _client()
    client.prepare_price_check()  # must not raise

    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "5"}))

    assert client.get_current_price(_product(), variant_id) == 500
    assert mock_request.call_args_list[3].args[1].endswith("/products/5")


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_without_total_page_an_empty_page_ends_the_bulk_read(mock_request: MagicMock):
    mock_request.side_effect = [
        _list_response([_item(id=1)], total_page=None, page=1),
        _list_response([], total_page=None, page=2),
    ]
    client = _client()
    client.prepare_price_check()
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_finish_price_check_returns_to_one_get_per_product(mock_request: MagicMock):
    mock_request.side_effect = [
        _list_response([_item(id=1)], total_page=1),
        _product_response(_item(id=1)),
    ]
    client = _client()
    client.prepare_price_check()
    client.finish_price_check()

    client.resolve_variant_id(_product(platform_refs={"basalam": "1"}))

    assert mock_request.call_args_list[1].args[1].endswith("/products/1")
    assert mock_request.call_args_list[1].kwargs.get("params") is None



@patch("faraz_honar.platforms.basalam.client.time.sleep")
@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_bulk_read_retries_a_timed_out_page_instead_of_abandoning_the_catalog(
    mock_request: MagicMock, _sleep: MagicMock
):
    import requests as _requests

    mock_request.side_effect = [
        _requests.exceptions.ReadTimeout("slow"),
        _list_response([_item(id=1, primary_price=100)], total_page=1),
    ]
    client = _client()
    client.prepare_price_check()

    variant_id = client.resolve_variant_id(_product(platform_refs={"basalam": "1"}))
    assert client.get_current_price(_product(), variant_id) == 100
    assert mock_request.call_count == 2  # page retried; no per-product GET


@patch("faraz_honar.platforms.basalam.client.time.sleep")
@patch("faraz_honar.platforms.basalam.client.requests.request")
def test_pages_read_before_a_failure_are_kept(mock_request: MagicMock, _sleep: MagicMock):
    import requests as _requests

    mock_request.side_effect = [
        _list_response([_item(id=1, primary_price=100)], total_page=2, page=1),
        _requests.exceptions.ReadTimeout("slow"),
        _requests.exceptions.ReadTimeout("slow"),
        _requests.exceptions.ReadTimeout("slow"),
        _product_response(_item(id=2, primary_price=200)),
    ]
    client = _client(list_page_size=1)
    client.prepare_price_check()

    first = client.resolve_variant_id(_product(platform_refs={"basalam": "1"}))
    assert client.get_current_price(_product(), first) == 100
    calls_before = mock_request.call_count
    second = client.resolve_variant_id(_product(platform_refs={"basalam": "2"}))
    assert client.get_current_price(_product(), second) == 200
    assert mock_request.call_count == calls_before + 1  # only product 2 fetched singly


def test_default_timeout_and_page_size_are_relaxed():
    client = _client()
    assert client._timeout == (10.0, 60.0)
    assert client._list_page_size == 20