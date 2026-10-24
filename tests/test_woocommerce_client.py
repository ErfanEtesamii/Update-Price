"""Tests for the WooCommerce platform client (farazhonar.com).

Covers, in order: (1) URL-param auth (never HTTPBasicAuth/Authorization
header — required by the farazhonar.com firewall), (2) the required
browser User-Agent header, (3) paginated product-list resolution, (4) POST
(never PUT) updates with the exact documented payload shape, (5) no
Toman->Rial ×10 conversion (WooCommerce is already Rial-native, like
TapsiShop/Basalam), (6) WooCommerce's exclusion from the 12% cap, (7)
DRY_RUN blocking writes only, and (8) error/retry classification, matching
the conventions already established in test_tapsishop_client.py and
test_basalam_client.py.
"""
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from faraz_honar.core.exceptions import (
    ConfigurationError,
    PlatformAPIError,
    PlatformAuthError,
    VariantResolutionError,
)
from faraz_honar.core.models import Product
from faraz_honar.core.pricing import CAPPED_PLATFORMS, compute_price_to_apply
from faraz_honar.platforms.woocommerce.client import WooCommerceClient, _index_by_id


def _client(**overrides: Any) -> WooCommerceClient:
    defaults: dict[str, Any] = dict(
        base_url="https://farazhonar.com/wp-json/wc/v3",
        consumer_key="the-consumer-key",
        consumer_secret="the-consumer-secret",
        dry_run=False,
        # Delays default to 0 so tests not specifically about throttling
        # stay fast and deterministic (same convention as the other
        # platform client test files).
        read_delay_seconds=0.0,
        write_delay_seconds=0.0,
    )
    defaults.update(overrides)
    return WooCommerceClient(**defaults)


def _product(**overrides: Any) -> Product:
    # CONFIRMED against the live sheet's sample fixture: the "وردپرس"
    # column stores plain numeric WooCommerce product ids.
    defaults: dict[str, Any] = dict(
        internal_id="p1",
        name="Test Product",
        reference_price=3_500_000,
        target_price=3_800_000,
        platform_refs={"woocommerce": "17817"},
    )
    defaults.update(overrides)
    return Product(**defaults)


def _item(**overrides: Any) -> dict:
    defaults: dict[str, Any] = dict(
        id=17817,
        regular_price="3500000",
        price="3500000",
        stock_quantity=10,
        manage_stock=True,
    )
    defaults.update(overrides)
    return defaults


def _page_response(items: list[dict], *, status_code: int = 200) -> MagicMock:
    # WooCommerce's GET /products returns a plain JSON array (the product
    # list itself), not wrapped in a 'data' envelope like TapsiShop's.
    return MagicMock(status_code=status_code, json=lambda: items, text=str(items))


def _update_ack(item: dict | None = None, *, status_code: int = 200) -> MagicMock:
    body = item if item is not None else _item()
    return MagicMock(status_code=status_code, json=lambda: body, text=str(body))


# -- 1/2/8: Authentication uses URL params, never HTTPBasicAuth/headers ------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_get_sends_consumer_key_and_secret_as_url_params(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item()])
    client = _client(consumer_key="ck_123", consumer_secret="cs_456")

    client.resolve_variant_id(_product())

    params = mock_request.call_args.kwargs["params"]
    assert params["consumer_key"] == "ck_123"
    assert params["consumer_secret"] == "cs_456"


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_get_does_not_use_authorization_header_or_auth_kwarg(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item()])
    client = _client()

    client.resolve_variant_id(_product())

    call_kwargs = mock_request.call_args.kwargs
    headers = call_kwargs["headers"]
    assert "Authorization" not in headers
    # requests.request's `auth` kwarg (which is what HTTPBasicAuth would be
    # passed through) must never be used — confirms this client never
    # constructs requests.auth.HTTPBasicAuth anywhere.
    assert call_kwargs.get("auth") is None


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_sends_consumer_key_and_secret_as_url_params(mock_request: MagicMock):
    mock_request.side_effect = [_page_response([_item()]), _update_ack()]
    client = _client(consumer_key="ck_abc", consumer_secret="cs_xyz")
    product = _product()

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)
    client.update_price(variant_id, 3_800_000)

    update_call = mock_request.call_args_list[1]
    params = update_call.kwargs["params"]
    assert params["consumer_key"] == "ck_abc"
    assert params["consumer_secret"] == "cs_xyz"
    assert "Authorization" not in update_call.kwargs["headers"]
    assert update_call.kwargs.get("auth") is None


# -- 3: Required browser User-Agent header ------------------------------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_required_headers_are_exact(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item()])
    client = _client()

    client.resolve_variant_id(_product())

    headers = mock_request.call_args.kwargs["headers"]
    # Exact string confirmed against a real, successful update call against
    # farazhonar.com — required to pass the site's WAF.
    assert headers["User-Agent"] == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


# -- 4/5: Product pagination -----------------------------------------------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_resolve_variant_id_finds_product_on_first_page(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item(id=17817), _item(id=17824)])
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"woocommerce": "17817"}))

    assert variant_id == "17817"
    assert mock_request.call_count == 1


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_pagination_fetches_multiple_pages_until_match_is_found(mock_request: MagicMock):
    page1 = [_item(id=1), _item(id=2)]
    page2 = [_item(id=3), _item(id=17817)]
    mock_request.side_effect = [_page_response(page1), _page_response(page2)]
    client = _client()

    variant_id = client.resolve_variant_id(_product(platform_refs={"woocommerce": "17817"}))

    assert variant_id == "17817"
    assert mock_request.call_count == 2
    first_params = mock_request.call_args_list[0].kwargs["params"]
    second_params = mock_request.call_args_list[1].kwargs["params"]
    assert first_params["page"] == 1
    assert second_params["page"] == 2


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_pagination_stops_when_a_page_comes_back_empty(mock_request: MagicMock):
    page1 = [_item(id=1)]
    mock_request.side_effect = [_page_response(page1), _page_response([])]
    client = _client()

    with pytest.raises(VariantResolutionError, match="No WooCommerce product found"):
        client.resolve_variant_id(_product(platform_refs={"woocommerce": "99999"}))

    assert mock_request.call_count == 2  # page 1, then the empty page 2 — then stops


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_missing_platform_ref_raises_variant_resolution_error_without_any_request(
    mock_request: MagicMock,
):
    client = _client()
    with pytest.raises(VariantResolutionError, match="no WooCommerce reference"):
        client.resolve_variant_id(_product(platform_refs={}))
    mock_request.assert_not_called()


def test_index_by_id_skips_items_without_id():
    items = [{"id": 1, "x": "a"}, {"x": "b"}, {"id": 3, "x": "c"}]
    index = _index_by_id(items)
    assert set(index.keys()) == {"1", "3"}


# -- 6/7: POST used for updates, PUT never used --------------------------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_price_uses_post(mock_request: MagicMock):
    mock_request.side_effect = [_page_response([_item()]), _update_ack()]
    client = _client()
    product = _product()

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)
    client.update_price(variant_id, 3_800_000)

    update_call = mock_request.call_args_list[1]
    # requests.request(method, url, ...) — method is the first positional arg.
    assert update_call.args[0] == "POST"


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_put_is_never_used_across_the_full_resolve_price_update_flow(mock_request: MagicMock):
    mock_request.side_effect = [_page_response([_item()]), _update_ack()]
    client = _client()
    product = _product()

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)
    client.update_price(variant_id, 3_800_000)

    methods_used = {call.args[0] for call in mock_request.call_args_list}
    assert "PUT" not in methods_used
    assert methods_used == {"GET", "POST"}


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_price_posts_to_the_product_id_path(mock_request: MagicMock):
    mock_request.side_effect = [_page_response([_item(id=17817)]), _update_ack()]
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"})

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)
    client.update_price(variant_id, 3_800_000)

    update_call = mock_request.call_args_list[1]
    assert update_call.args[1] == "https://farazhonar.com/wp-json/wc/v3/products/17817"


# -- 9: Update payload shape -----------------------------------------------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_payload_contains_required_fields(mock_request: MagicMock):
    mock_request.side_effect = [
        _page_response([_item(id=17817, stock_quantity=10)]),
        _update_ack(),
    ]
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"})

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)
    client.update_price(variant_id, 3_800_000)  # Rial (this app's internal unit)

    update_call = mock_request.call_args_list[1]
    payload = update_call.kwargs["json"]
    # farazhonar.com is Toman-native — 3,800,000 Rial -> "380000" Toman.
    assert payload["regular_price"] == "380000"
    assert isinstance(payload["regular_price"], str)
    assert payload["manage_stock"] is True
    assert payload["stock_quantity"] == 10


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_price_without_prior_lookup_still_sends_a_price_only_update(
    mock_request: MagicMock,
):
    # Stock is never required for a price update — CONFIRMED against a
    # real, successful call against farazhonar.com that sent only
    # regular_price. Calling update_price with no prior resolve_variant_id/
    # get_reference_price call (so nothing is cached) must still succeed,
    # sending a price-only payload rather than refusing.
    mock_request.return_value = _update_ack()
    client = _client()

    client.update_price("17817", 3_800_000)  # Rial in -> Toman out

    payload = mock_request.call_args.kwargs["json"]
    assert payload == {"regular_price": "380000"}


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_price_omits_stock_fields_when_stock_quantity_is_missing(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item(id=17817, stock_quantity=None)])
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"})

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)

    # Reset the mock so the next call's payload is the update, not the GET.
    mock_request.reset_mock()
    mock_request.return_value = _update_ack()
    client.update_price(variant_id, 3_800_000)

    payload = mock_request.call_args.kwargs["json"]
    assert payload == {"regular_price": "380000"}
    assert "manage_stock" not in payload
    assert "stock_quantity" not in payload


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_price_includes_stock_fields_when_stock_quantity_is_known(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item(id=17817, stock_quantity=10)])
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"})

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)

    mock_request.reset_mock()
    mock_request.return_value = _update_ack()
    client.update_price(variant_id, 3_800_000)

    payload = mock_request.call_args.kwargs["json"]
    assert payload == {"regular_price": "380000", "manage_stock": True, "stock_quantity": 10}


# -- 10: farazhonar.com's own store is Toman-native (CONFIRMED live) --------------------
# WooCommerce itself is NOT multiplied/divided a second time anywhere else in the app —
# this client's own Rial<->Toman boundary conversion is the only place it happens.


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_get_reference_price_converts_the_apis_toman_value_to_rial(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item(id=17817, regular_price="3500000")])
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"})

    variant_id = client.resolve_variant_id(product)
    price = client.get_reference_price(product, variant_id)

    # API's raw "3500000" is Toman -> returned as 35,000,000 Rial, so it
    # stays comparable to the rest of the app's internal (Rial) pipeline.
    assert price == 35_000_000


def test_this_module_defines_its_own_conversion_not_sheets_mappings():
    # Regression guard: unlike Basalam/TapsiShop (which really are
    # Rial-native and apply no conversion at all), this module DOES
    # convert — but only with its own small, symmetric, locally-scoped
    # helpers (_toman_to_rial/_rial_to_toman). It must never import or
    # reuse sheets/mapping.py's one-way toman_to_rial, which exists only
    # for the sheet-parsing boundary and is applied exactly once, globally,
    # for every platform.
    import faraz_honar.platforms.woocommerce.client as wc_module

    assert "toman_to_rial" not in wc_module.__dict__
    assert wc_module._toman_to_rial(1) == 10  # noqa: SLF001
    assert wc_module._rial_to_toman(10) == 1  # noqa: SLF001


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_update_price_sends_the_toman_equivalent_of_the_computed_rial_price(
    mock_request: MagicMock,
):
    mock_request.side_effect = [
        _page_response([_item(id=17817, regular_price="350000", stock_quantity=10)]),
        _update_ack(),
    ]
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"}, target_price=3_800_000)

    variant_id = client.resolve_variant_id(product)
    reference_price = client.get_reference_price(product, variant_id)
    assert reference_price == 3_500_000  # 350,000 Toman -> 3,500,000 Rial
    decision = compute_price_to_apply(
        reference_price, product.target_price, apply_cap="woocommerce" in CAPPED_PLATFORMS
    )
    assert decision.price_to_apply == 3_800_000  # uncapped, full target (Rial)
    client.update_price(variant_id, decision.price_to_apply)

    payload = mock_request.call_args_list[1].kwargs["json"]
    # 3,800,000 Rial -> "380000" Toman actually sent to farazhonar.com.
    assert payload["regular_price"] == "380000"


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_get_reference_price_falls_back_to_price_when_regular_price_is_empty(
    mock_request: MagicMock,
):
    mock_request.return_value = _page_response(
        [_item(id=17817, regular_price="", price="320000")]
    )
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"})

    variant_id = client.resolve_variant_id(product)
    price = client.get_reference_price(product, variant_id)

    assert price == 3_200_000  # 320,000 Toman -> 3,200,000 Rial


# -- 11: WooCommerce is NOT subject to the 12% cap ---------------------------------------


def test_woocommerce_is_not_in_capped_platforms():
    assert "woocommerce" not in CAPPED_PLATFORMS
    # CAPPED_PLATFORMS is empty now (DigiKala's own API is the sole source
    # of truth for its reference-price limit — see DigikalaReferencePriceRejected).
    assert CAPPED_PLATFORMS == frozenset()


def test_woocommerce_pricing_decision_receives_full_target_uncapped_even_above_12pct():
    # A target more than 12% above the reference price would be capped for
    # DigiKala, but WooCommerce must receive it in full.
    decision = compute_price_to_apply(
        reference_price=1_000_000,
        target_price=2_000_000,  # +100%, far more than 12%
        apply_cap="woocommerce" in CAPPED_PLATFORMS,
    )
    assert decision.is_capped is False
    assert decision.price_to_apply == 2_000_000


# -- Regression: the exact real-world bug this fixed --------------------------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_regression_60_5m_rial_target_sends_6_05m_toman_not_60_5m(mock_request: MagicMock):
    # Live incident (2026-09-20, product 17817): the app computed a
    # 60,500,000 Rial target price and sent regular_price="60500000"
    # unconverted — 10x too high for farazhonar.com's Toman-native store.
    # A follow-up GET confirmed the API accepted and stored that value
    # verbatim (no server-side reinterpretation), so the fix has to be on
    # this client's side. This pins the corrected behavior: the exact same
    # 60,500,000 Rial target must now produce "6050000" Toman.
    mock_request.side_effect = [_page_response([_item(id=17817, stock_quantity=None)]), _update_ack()]
    client = _client()
    product = _product(platform_refs={"woocommerce": "17817"}, target_price=60_500_000)

    variant_id = client.resolve_variant_id(product)
    client.get_reference_price(product, variant_id)
    client.update_price(variant_id, product.target_price)

    payload = mock_request.call_args_list[1].kwargs["json"]
    assert payload["regular_price"] == "6050000"


# -- 12: DRY_RUN prevents POST updates ---------------------------------------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_dry_run_blocks_update_price_but_allows_reads(mock_request: MagicMock):
    mock_request.return_value = _page_response([_item(id=17817)])
    client = _client(dry_run=True)
    product = _product(platform_refs={"woocommerce": "17817"})

    variant_id = client.resolve_variant_id(product)  # a real GET, even under DRY_RUN
    client.get_reference_price(product, variant_id)
    client.update_price(variant_id, 3_800_000)  # must NOT send a request

    methods_used = [call.args[0] for call in mock_request.call_args_list]
    assert "POST" not in methods_used
    assert "PUT" not in methods_used
    assert all(m == "GET" for m in methods_used)


# -- 13: Network errors follow the existing retry conventions ----------------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_network_error_is_wrapped_as_retryable_platform_api_error(mock_request: MagicMock):
    mock_request.side_effect = requests.ConnectionError("boom")
    client = _client()

    with pytest.raises(PlatformAPIError) as exc_info:
        client.resolve_variant_id(_product())

    assert exc_info.value.retryable is True


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_timeout_error_is_wrapped_as_retryable_platform_api_error(mock_request: MagicMock):
    mock_request.side_effect = requests.Timeout("timed out")
    client = _client()

    with pytest.raises(PlatformAPIError) as exc_info:
        client.resolve_variant_id(_product())

    assert exc_info.value.retryable is True


# -- 14: HTTP error responses mapped per existing exception conventions ------------------


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_401_raises_non_retryable_platform_auth_error(mock_request: MagicMock):
    mock_request.return_value = _page_response([], status_code=401)
    client = _client()

    with pytest.raises(PlatformAuthError) as exc_info:
        client.resolve_variant_id(_product())

    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == 401


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_403_raises_non_retryable_platform_auth_error(mock_request: MagicMock):
    mock_request.return_value = _page_response([], status_code=403)
    client = _client()

    with pytest.raises(PlatformAuthError) as exc_info:
        client.resolve_variant_id(_product())

    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == 403


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_4xx_other_than_401_403_is_not_retryable(mock_request: MagicMock):
    mock_request.return_value = _page_response([], status_code=400)
    client = _client()

    with pytest.raises(PlatformAPIError) as exc_info:
        client.resolve_variant_id(_product())

    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == 400


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_429_is_not_retryable(mock_request: MagicMock):
    mock_request.return_value = _page_response([], status_code=429)
    client = _client()

    with pytest.raises(PlatformAPIError) as exc_info:
        client.resolve_variant_id(_product())

    assert exc_info.value.retryable is False


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_5xx_is_retryable(mock_request: MagicMock):
    mock_request.return_value = _page_response([], status_code=503)
    client = _client()

    with pytest.raises(PlatformAPIError) as exc_info:
        client.resolve_variant_id(_product())

    assert exc_info.value.retryable is True


def test_missing_consumer_key_raises_configuration_error():
    client = _client(consumer_key=None)
    with patch("faraz_honar.platforms.woocommerce.client.requests.request") as mock_request:
        with pytest.raises(ConfigurationError, match="WOOCOMMERCE_CONSUMER_KEY"):
            client.resolve_variant_id(_product())
        mock_request.assert_not_called()


def test_missing_consumer_secret_raises_configuration_error():
    client = _client(consumer_secret=None)
    with patch("faraz_honar.platforms.woocommerce.client.requests.request") as mock_request:
        with pytest.raises(ConfigurationError, match="WOOCOMMERCE_CONSUMER_SECRET"):
            client.resolve_variant_id(_product())
        mock_request.assert_not_called()


# -- Price check (read-only "🔍 چک و بررسی قیمت‌ها" button) -----------------------------


def _wc_product(n: int) -> Product:
    return _product(internal_id=f"p{n}", platform_refs={"woocommerce": str(100 + n)})


def _three_item_catalog() -> list[MagicMock]:
    # per_page=2: two products, one product, then the empty page that ends the walk.
    return [
        _page_response([_item(id=101, regular_price="1000"), _item(id=102, regular_price="2000")]),
        _page_response([_item(id=103, regular_price="3000")]),
        _page_response([]),
    ]


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_prepare_price_check_reads_every_page_once_and_resolving_needs_no_more_requests(
    mock_request: MagicMock,
):
    mock_request.side_effect = _three_item_catalog()
    client = _client(per_page=2)

    client.prepare_price_check()
    assert mock_request.call_count == 3  # pages 1, 2 and the empty page 3

    # Products from BOTH pages resolve and price without any further request.
    for n in (1, 2, 3):
        product = _wc_product(n)
        variant_id = client.resolve_variant_id(product)
        assert variant_id == str(100 + n)
        # The store is Toman-native: 1000 Toman -> 10,000 Rial.
        assert client.get_current_price(product, variant_id) == n * 10_000
    assert mock_request.call_count == 3


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_unknown_id_during_a_check_raises_variant_resolution_error_without_requests(
    mock_request: MagicMock,
):
    mock_request.side_effect = _three_item_catalog()
    client = _client(per_page=2)
    client.prepare_price_check()

    with pytest.raises(VariantResolutionError, match="999") as excinfo:
        client.resolve_variant_id(_product(platform_refs={"woocommerce": "999"}))

    assert excinfo.value.retryable is False
    assert mock_request.call_count == 3  # did not go looking page by page


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_finish_price_check_restores_the_page_by_page_lookup(mock_request: MagicMock):
    mock_request.side_effect = _three_item_catalog()
    client = _client(per_page=2)
    client.prepare_price_check()
    client.finish_price_check()
    mock_request.reset_mock()

    # Old behavior: a product on page 1 costs one request (stops once found)...
    mock_request.side_effect = _three_item_catalog()
    assert client.resolve_variant_id(_wc_product(1)) == "101"
    assert mock_request.call_count == 1

    # ...and one on page 2 costs two (walks from page 1 each time).
    mock_request.reset_mock()
    mock_request.side_effect = _three_item_catalog()
    assert client.resolve_variant_id(_wc_product(3)) == "103"
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.woocommerce.client.requests.request")
def test_finish_price_check_makes_an_unknown_id_walk_every_page_again(mock_request: MagicMock):
    mock_request.side_effect = _three_item_catalog()
    client = _client(per_page=2)
    client.prepare_price_check()
    client.finish_price_check()
    mock_request.reset_mock()
    mock_request.side_effect = _three_item_catalog()

    with pytest.raises(VariantResolutionError, match="after checking"):
        client.resolve_variant_id(_product(platform_refs={"woocommerce": "999"}))

    assert mock_request.call_count == 3


def test_estimate_check_seconds_is_per_page_not_per_product():
    client = _client(per_page=100, read_delay_seconds=1.0)

    # 250 products = 3 pages of one throttled read each; not 250 reads.
    assert client.estimate_check_seconds(250) == 3 * (1.0 + 0.5)


def test_price_step_is_10_rial_and_expected_price_rounds_down():
    client = _client()

    assert client.price_step_rial == 10
    assert client.expected_price_for_target(1_234_567) == 1_234_560
    assert client.expected_price_for_target(1_234_560) == 1_234_560  # already a multiple
