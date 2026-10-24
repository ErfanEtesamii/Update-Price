from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from faraz_honar.core.exceptions import (
    DigikalaReferencePriceRejected,
    PlatformAPIError,
    PlatformAuthError,
    VariantResolutionError,
)
from faraz_honar.core.models import Product
from faraz_honar.platforms.digikala.auth import TokenManager
from faraz_honar.platforms.digikala.client import DigikalaClient


def _client(
    tmp_path: Path,
    *,
    dry_run: bool,
    enable_refresh: bool = False,
    bootstrap_access_token: str = "access-1",
    read_delay_seconds: float = 0.0,
    write_delay_seconds: float = 0.0,
    rate_limit_max_retries: int = 0,
    rate_limit_backoff_seconds: float = 5.0,
) -> DigikalaClient:
    # Delays default to 0 here (unlike the client's own production defaults
    # of 1/2 seconds) so that every test not specifically about throttling
    # stays fast and deterministic; see test_*_delay_* below for the
    # dedicated throttling tests, which pass non-zero values explicitly.
    token_manager = TokenManager(
        base_url="https://seller.digikala.com",
        tokens_file=tmp_path / "tokens.json",
        bootstrap_access_token=bootstrap_access_token,
        bootstrap_refresh_token="refresh-1",
        enable_refresh=enable_refresh,
    )
    return DigikalaClient(
        base_url="https://seller.digikala.com",
        token_manager=token_manager,
        dry_run=dry_run,
        read_delay_seconds=read_delay_seconds,
        write_delay_seconds=write_delay_seconds,
        rate_limit_max_retries=rate_limit_max_retries,
        rate_limit_backoff_seconds=rate_limit_backoff_seconds,
    )


def _product(**overrides: Any) -> Product:
    defaults: dict[str, Any] = dict(
        internal_id="p1",
        name="Test",
        reference_price=1_000_000,
        target_price=1_100_000,
        platform_refs={"digikala": "20589157"},
    )
    defaults.update(overrides)
    return Product(**defaults)


def _search_response(items: list[dict], *, total_pages: int = 1) -> MagicMock:
    return MagicMock(
        status_code=200,
        json=lambda: {
            "status": "success",
            "data": {"pager": {"total_pages": total_pages}, "items": items},
        },
    )


def _variant_item(**overrides) -> dict:
    defaults = dict(
        id=83841925,
        product_id=20589157,
        product_url="https://www.digikala.com/product/dkp-20589157/some-slug/",
        price_list=1_000_000,
        price_sale=1_000_000,
    )
    defaults.update(overrides)
    return defaults


# -- resolve_variant_id ------------------------------------------------------------


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_resolve_variant_id_makes_real_read_even_under_dry_run(mock_request: MagicMock, tmp_path: Path):
    # DRY_RUN only blocks writes (update_price) — reads must go through so
    # the 12% calculation can be verified against real DigiKala data.
    mock_request.return_value = _search_response([_variant_item()])
    client = _client(tmp_path, dry_run=True)

    variant_id = client.resolve_variant_id(_product())

    assert variant_id == "83841925"
    mock_request.assert_called_once()
    assert mock_request.call_args.args[0] == "GET"


def test_resolve_variant_id_raises_when_no_sheet_reference(tmp_path: Path):
    client = _client(tmp_path, dry_run=True)
    with pytest.raises(VariantResolutionError, match="no DigiKala reference"):
        client.resolve_variant_id(_product(platform_refs={}))


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_resolve_variant_id_matches_by_product_id(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _search_response([_variant_item(product_url="")])
    client = _client(tmp_path, dry_run=False)

    variant_id = client.resolve_variant_id(_product())

    assert variant_id == "83841925"
    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    assert method == "GET"
    assert url == "https://seller.digikala.com/open-api/v1/variants"
    assert mock_request.call_args.kwargs["params"]["search[search_term]"] == "20589157"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_resolve_variant_id_matches_by_product_url_when_product_id_differs(
    mock_request: MagicMock, tmp_path: Path
):
    # product_id is some unrelated internal id; only the URL's dkp code matches.
    mock_request.return_value = _search_response(
        [_variant_item(product_id=999, product_url="https://www.digikala.com/product/dkp-20589157/x/")]
    )
    client = _client(tmp_path, dry_run=False)

    assert client.resolve_variant_id(_product()) == "83841925"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_resolve_variant_id_ignores_non_matching_items(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _search_response(
        [_variant_item(id=1, product_id=111, product_url=""), _variant_item(id=2, product_id=20589157)]
    )
    client = _client(tmp_path, dry_run=False)
    assert client.resolve_variant_id(_product()) == "2"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_resolve_variant_id_paginates_until_match_or_last_page(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _search_response([_variant_item(id=1, product_id=1, product_url="")], total_pages=2),
        _search_response([_variant_item(id=2, product_id=20589157)], total_pages=2),
    ]
    client = _client(tmp_path, dry_run=False)
    assert client.resolve_variant_id(_product()) == "2"
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_resolve_variant_id_no_match_raises(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _search_response([_variant_item(id=1, product_id=111, product_url="")])
    client = _client(tmp_path, dry_run=False)
    with pytest.raises(VariantResolutionError, match="No DigiKala variant found"):
        client.resolve_variant_id(_product())


# -- get_reference_price -------------------------------------------------------------


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_get_reference_price_makes_real_read_even_under_dry_run(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_list": 4_500_000}}
    )
    client = _client(tmp_path, dry_run=True)

    price = client.get_reference_price(_product(), "83841925")

    assert price == 4_500_000
    mock_request.assert_called_once()
    assert mock_request.call_args.args[0] == "GET"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_get_reference_price_uses_cached_item_from_resolve(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _search_response([_variant_item(price_list=2_345_000)])
    client = _client(tmp_path, dry_run=False)

    variant_id = client.resolve_variant_id(_product())
    price = client.get_reference_price(_product(), variant_id)

    assert price == 2_345_000
    assert mock_request.call_count == 1  # no extra detail fetch needed


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_get_reference_price_fetches_detail_when_not_cached(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_list": 3_000_000}}
    )
    client = _client(tmp_path, dry_run=False)

    price = client.get_reference_price(_product(), "83841925")

    assert price == 3_000_000
    called_url = mock_request.call_args.args[1]
    assert called_url == "https://seller.digikala.com/open-api/v1/variants/83841925"


# -- update_price ---------------------------------------------------------------------


def test_dry_run_does_not_call_requests(tmp_path: Path):
    client = _client(tmp_path, dry_run=True)
    with patch("faraz_honar.platforms.digikala.client.requests.request") as mock_request:
        client.update_price("83841925", 1_200_000)
        mock_request.assert_not_called()


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_successful_update_sends_expected_request(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_200_000}}
    )
    client = _client(tmp_path, dry_run=False)

    client.update_price("83841925", 1_200_000)

    mock_request.assert_called_once()
    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    kwargs = mock_request.call_args.kwargs
    assert method == "PUT"
    assert url == "https://seller.digikala.com/open-api/v1/variants/83841925"
    assert kwargs["json"] == {"selling_price": 1_200_000}
    assert kwargs["headers"]["Authorization"] == "Bearer access-1"


# -- Price rounding: DigiKala requires selling_price to be a multiple of 100 ----------
#
# CONFIRMED against a real HTTP 400 response: DigiKala rejects a
# selling_price whose last two digits aren't "00"
# ("دو رقم آخر قیمت باید ۰۰ باشد"). The 12%-cap computation
# (floor(reference_price * 1.12) in core/pricing.py) can produce such a
# value even from a clean target price, so update_price must round down
# (never up) before sending.


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_non_multiple_of_hundred_price_is_rounded_down_before_sending(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_200_000}}
    )
    client = _client(tmp_path, dry_run=False)

    client.update_price("83841925", 1_200_099)

    kwargs = mock_request.call_args.kwargs
    assert kwargs["json"] == {"selling_price": 1_200_000}


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_capped_price_not_ending_in_00_is_rounded_down(mock_request: MagicMock, tmp_path: Path):
    # Reproduces the real-world failure: a 12%-capped price
    # (floor(reference_price * 1.12)) that doesn't land on a clean
    # multiple of 100.
    from faraz_honar.core.pricing import compute_price_to_apply

    decision = compute_price_to_apply(37_812_345, 100_000_000, apply_cap=True)
    assert decision.is_capped is True
    assert decision.price_to_apply % 100 != 0  # the exact failure condition

    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 0}}
    )
    client = _client(tmp_path, dry_run=False)

    client.update_price("83841925", decision.price_to_apply)

    sent_price = mock_request.call_args.kwargs["json"]["selling_price"]
    assert sent_price % 100 == 0
    assert sent_price <= decision.price_to_apply  # rounded down, never up


def test_already_multiple_of_hundred_price_is_sent_unchanged(tmp_path: Path):
    with patch("faraz_honar.platforms.digikala.client.requests.request") as mock_request:
        mock_request.return_value = MagicMock(
            status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_200_000}}
        )
        client = _client(tmp_path, dry_run=False)

        client.update_price("83841925", 1_200_000)

        assert mock_request.call_args.kwargs["json"] == {"selling_price": 1_200_000}


def test_dry_run_log_reflects_rounded_price(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    client = _client(tmp_path, dry_run=True)
    with patch("faraz_honar.platforms.digikala.client.requests.request") as mock_request:
        with caplog.at_level("INFO"):
            client.update_price("83841925", 1_200_099)
        mock_request.assert_not_called()
    assert "1200000" in caplog.text
    assert "1200099" not in caplog.text


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_server_error_is_retryable(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=503, text="upstream unavailable")
    client = _client(tmp_path, dry_run=False)
    with pytest.raises(PlatformAPIError) as excinfo:
        client.update_price("V1", 1_200_000)
    assert excinfo.value.retryable is True


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_bad_request_is_not_retryable(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=400, text="invalid selling_price")
    client = _client(tmp_path, dry_run=False)
    with pytest.raises(PlatformAPIError) as excinfo:
        client.update_price("V1", 1_200_000)
    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_reference_price_range_rejection_raises_specific_exception(
    mock_request: MagicMock, tmp_path: Path
):
    # Exact real-world DigiKala response body when selling_price is outside
    # its own reference-price bracket.
    mock_request.return_value = MagicMock(
        status_code=400,
        text='{"errors":{"selling_price":["قیمت فروش باید در بازه قیمت مرجع باشد"]}}',
        json=lambda: {"errors": {"selling_price": ["قیمت فروش باید در بازه قیمت مرجع باشد"]}},
    )
    client = _client(tmp_path, dry_run=False)
    with pytest.raises(DigikalaReferencePriceRejected) as excinfo:
        client.update_price("V1", 1_200_000)
    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_other_400_selling_price_message_is_generic_error(mock_request: MagicMock, tmp_path: Path):
    # A different selling_price validation message must NOT be misclassified
    # as a reference-price rejection.
    mock_request.return_value = MagicMock(
        status_code=400,
        text='{"errors":{"selling_price":["دو رقم آخر قیمت باید ۰۰ باشد"]}}',
        json=lambda: {"errors": {"selling_price": ["دو رقم آخر قیمت باید ۰۰ باشد"]}},
    )
    client = _client(tmp_path, dry_run=False)
    with pytest.raises(PlatformAPIError) as excinfo:
        client.update_price("V1", 1_200_000)
    assert not isinstance(excinfo.value, DigikalaReferencePriceRejected)
    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_rate_limited_429_is_not_retryable(mock_request: MagicMock, tmp_path: Path):
    # Retrying a 429 immediately only makes the rate-limiting worse, so it
    # must be classified as non-retryable (unlike 5xx, see
    # test_server_error_is_retryable above) and the request is made exactly
    # once — the client itself never retries; PriceUpdateService's retry
    # loop is what would skip a second attempt based on this flag.
    mock_request.return_value = MagicMock(status_code=429, text="Too Many Requests")
    client = _client(tmp_path, dry_run=False)
    with pytest.raises(PlatformAPIError) as excinfo:
        client.update_price("V1", 1_200_000)
    assert excinfo.value.retryable is False
    assert excinfo.value.status_code == 429
    assert "HTTP 429" in str(excinfo.value)
    mock_request.assert_called_once()


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_401_without_refresh_enabled_raises_auth_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=401, text="token expired")
    client = _client(tmp_path, dry_run=False, enable_refresh=False)
    with pytest.raises(PlatformAuthError):
        client.update_price("V1", 1_200_000)
    assert mock_request.call_count == 1


@patch("faraz_honar.platforms.digikala.auth.requests.post")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_401_with_refresh_enabled_retries_once_after_refresh(
    mock_request: MagicMock, mock_post: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [
        MagicMock(status_code=401, text="token expired"),
        MagicMock(status_code=200, json=lambda: {"status": "success", "data": {}}),
    ]
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": {"access_token": "access-2", "refresh_token": "refresh-2"}},
    )
    client = _client(tmp_path, dry_run=False, enable_refresh=True)

    client.update_price("V1", 1_200_000)

    assert mock_request.call_count == 2
    assert mock_post.call_count == 1
    second_call_headers = mock_request.call_args_list[1].kwargs["headers"]
    assert second_call_headers["Authorization"] == "Bearer access-2"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_401_during_search_raises_auth_error_not_variant_error(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = MagicMock(status_code=401, text="token expired")
    client = _client(tmp_path, dry_run=False, enable_refresh=False)
    with pytest.raises(PlatformAuthError):
        client.resolve_variant_id(_product())


# -- Proactive refresh (local JWT exp check, before wasting a request) --------------


def _expired_jwt() -> str:
    import base64
    import json
    import time

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header = b64({"typ": "JWT", "alg": "HS256"})
    payload = b64({"exp": time.time() - 3600})
    return f"{header}.{payload}.fake-signature"


@patch("faraz_honar.platforms.digikala.auth.requests.post")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_expired_token_triggers_proactive_refresh_before_request(
    mock_request: MagicMock, mock_post: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(status_code=200, json=lambda: {"status": "success", "data": {}})
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": {"access_token": "fresh-access", "refresh_token": "fresh-refresh"}},
    )
    client = _client(
        tmp_path, dry_run=False, enable_refresh=True, bootstrap_access_token=_expired_jwt()
    )

    client.update_price("V1", 1_200_000)

    # Refreshed once, BEFORE the actual request, and the request used the new token.
    assert mock_post.call_count == 1
    assert mock_request.call_count == 1
    assert mock_request.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh-access"


@patch("faraz_honar.platforms.digikala.auth.requests.post")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_proactive_refresh_disabled_raises_clean_error_without_wasting_a_request(
    mock_request: MagicMock, mock_post: MagicMock, tmp_path: Path
):
    client = _client(
        tmp_path, dry_run=False, enable_refresh=False, bootstrap_access_token=_expired_jwt()
    )

    with pytest.raises(PlatformAuthError, match="refresh could not be completed"):
        client.update_price("V1", 1_200_000)

    # No real HTTP call was made at all: we already knew locally the token was dead.
    mock_request.assert_not_called()
    mock_post.assert_not_called()


# -- Infinite-loop prevention: at most ONE refresh attempt per request --------------


@patch("faraz_honar.platforms.digikala.auth.requests.post")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_second_401_after_refresh_does_not_loop(
    mock_request: MagicMock, mock_post: MagicMock, tmp_path: Path
):
    # Even the refreshed token gets rejected (e.g. still somehow invalid server-side).
    mock_request.return_value = MagicMock(status_code=401, text="still unauthorized")
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": {"access_token": "access-2", "refresh_token": "refresh-2"}},
    )
    client = _client(tmp_path, dry_run=False, enable_refresh=True)

    with pytest.raises(PlatformAuthError, match="even after refresh"):
        client.update_price("V1", 1_200_000)

    # Exactly one refresh call and exactly two HTTP attempts (original + one retry) — no loop.
    assert mock_post.call_count == 1
    assert mock_request.call_count == 2


# -- No token values ever appear in logs --------------------------------------------


@patch("faraz_honar.platforms.digikala.auth.requests.post")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_client_never_logs_token_values(
    mock_request: MagicMock, mock_post: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    mock_request.side_effect = [
        MagicMock(status_code=401, text="token expired"),
        MagicMock(status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_200_000}}),
    ]
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": {"access_token": "brand-new-secret", "refresh_token": "brand-new-refresh"}},
    )
    client = _client(
        tmp_path, dry_run=False, enable_refresh=True, bootstrap_access_token="original-secret-token"
    )

    with caplog.at_level("DEBUG"):
        client.update_price("V1", 1_200_000)

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "original-secret-token" not in all_log_text
    assert "brand-new-secret" not in all_log_text
    assert "brand-new-refresh" not in all_log_text


# -- DRY_RUN write-block still enforced regardless of read/refresh behavior ---------


@patch("faraz_honar.platforms.digikala.auth.requests.post")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_dry_run_blocks_update_price_even_with_expired_token_and_refresh_enabled(
    mock_request: MagicMock, mock_post: MagicMock, tmp_path: Path
):
    # Belt-and-suspenders: prove that allowing reads under DRY_RUN did not
    # accidentally loosen the write gate, even in the trickiest case (a
    # token that would otherwise trigger a proactive refresh).
    client = _client(
        tmp_path, dry_run=True, enable_refresh=True, bootstrap_access_token=_expired_jwt()
    )

    client.update_price("V1", 1_200_000)

    mock_request.assert_not_called()
    mock_post.assert_not_called()


# -- Rate-limit throttling between consecutive DigiKala requests -------------------


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.time.monotonic")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_no_delay_before_the_very_first_request(
    mock_request: MagicMock, mock_monotonic: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_list": 1_000_000}}
    )
    mock_monotonic.return_value = 0.0
    client = _client(tmp_path, dry_run=False, read_delay_seconds=5.0, write_delay_seconds=5.0)

    client.get_reference_price(_product(), "83841925")

    mock_sleep.assert_not_called()


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.time.monotonic")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_read_delay_applied_between_consecutive_read_requests(
    mock_request: MagicMock, mock_monotonic: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_list": 1_000_000}}
    )
    # 1st read at t=0.0 (no prior read -> no sleep). 2nd read at t=0.2, only
    # 0.2s after the 1st, so it must wait the remaining 1.0 - 0.2 = 0.8s.
    mock_monotonic.side_effect = [0.0, 0.2]
    client = _client(tmp_path, dry_run=False, read_delay_seconds=1.0, write_delay_seconds=5.0)

    client.get_reference_price(_product(), "V1")
    client.get_reference_price(_product(), "V2")

    mock_sleep.assert_called_once_with(pytest.approx(0.8))


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.time.monotonic")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_write_delay_applied_between_consecutive_write_requests(
    mock_request: MagicMock, mock_monotonic: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_200_000}}
    )
    # 1st write at t=0.0 (no prior write -> no sleep). 2nd write at t=0.5,
    # only 0.5s after the 1st, so it must wait the remaining 2.0 - 0.5 = 1.5s.
    mock_monotonic.side_effect = [0.0, 0.5]
    client = _client(tmp_path, dry_run=False, read_delay_seconds=5.0, write_delay_seconds=2.0)

    client.update_price("V1", 1_200_000)
    client.update_price("V2", 1_300_000)

    mock_sleep.assert_called_once_with(pytest.approx(1.5))


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.time.monotonic")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_no_delay_when_enough_time_already_elapsed(
    mock_request: MagicMock, mock_monotonic: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_list": 1_000_000}}
    )
    # 2nd read at t=10.0, well past the 1.0s read delay since the 1st (t=0.0).
    mock_monotonic.side_effect = [0.0, 10.0]
    client = _client(tmp_path, dry_run=False, read_delay_seconds=1.0)

    client.get_reference_price(_product(), "V1")
    client.get_reference_price(_product(), "V2")

    mock_sleep.assert_not_called()


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.time.monotonic")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_read_and_write_delays_are_independent(
    mock_request: MagicMock, mock_monotonic: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    # A write immediately following a read (different "kind") is never
    # delayed by the read delay — only same-kind requests throttle each
    # other.
    mock_request.side_effect = [
        MagicMock(status_code=200, json=lambda: {"status": "success", "data": {"price_list": 1_000_000}}),
        MagicMock(status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_200_000}}),
    ]
    mock_monotonic.return_value = 0.0
    client = _client(tmp_path, dry_run=False, read_delay_seconds=5.0, write_delay_seconds=5.0)

    client.get_reference_price(_product(), "V1")  # read
    client.update_price("V1", 1_200_000)  # write, immediately after

    mock_sleep.assert_not_called()


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.time.monotonic")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_zero_delay_never_sleeps(
    mock_request: MagicMock, mock_monotonic: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_list": 1_000_000}}
    )
    mock_monotonic.side_effect = [0.0, 0.0]
    client = _client(tmp_path, dry_run=False, read_delay_seconds=0.0)

    client.get_reference_price(_product(), "V1")
    client.get_reference_price(_product(), "V2")

    mock_sleep.assert_not_called()


def _channel(active: bool) -> dict:
    return {"active_digikala": active, "active_digistyle": False}


def test_resolve_prefers_active_variant_over_stale_one(tmp_path: Path) -> None:
    client = _client(tmp_path, dry_run=True)
    stale = _variant_item(id=1, price_list=900_000, active=False, variant_selling_chanel=_channel(False))
    live = _variant_item(id=2, price_list=1_200_000, active=True, variant_selling_chanel=_channel(True))
    with patch("requests.request", return_value=_search_response([stale, live])) as mock_req:
        assert client.resolve_variant_id(_product()) == "2"
        assert client.get_reference_price(_product(), "2") == 1_200_000
    assert mock_req.call_count == 1


def test_resolve_skips_variant_with_channel_off_even_if_active(tmp_path: Path) -> None:
    client = _client(tmp_path, dry_run=True)
    off = _variant_item(id=1, active=True, variant_selling_chanel=_channel(False))
    on = _variant_item(id=2, active=True, variant_selling_chanel=_channel(True))
    with patch("requests.request", return_value=_search_response([off, on])):
        assert client.resolve_variant_id(_product()) == "2"


def test_resolve_falls_back_to_inactive_when_nothing_else_matches(tmp_path: Path) -> None:
    client = _client(tmp_path, dry_run=True)
    stale = _variant_item(id=1, active=False, variant_selling_chanel=_channel(False))
    with patch("requests.request", return_value=_search_response([stale])):
        assert client.resolve_variant_id(_product()) == "1"


def test_resolve_keeps_paging_when_only_inactive_match_on_first_page(tmp_path: Path) -> None:
    client = _client(tmp_path, dry_run=True)
    stale = _variant_item(id=1, active=False, variant_selling_chanel=_channel(False))
    live = _variant_item(id=2, active=True, variant_selling_chanel=_channel(True))
    pages = [_search_response([stale], total_pages=2), _search_response([live], total_pages=2)]
    with patch("requests.request", side_effect=pages):
        assert client.resolve_variant_id(_product()) == "2"


# -- Price check (read-only "🔍 چک و بررسی قیمت‌ها" button) -----------------------------


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_get_current_price_returns_price_sale_not_price_list(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _search_response(
        [_variant_item(price_list=9_000_000, price_sale=1_234_500)]
    )
    client = _client(tmp_path, dry_run=False)
    product = _product()

    variant_id = client.resolve_variant_id(product)
    current = client.get_current_price(product, variant_id)

    assert current == 1_234_500
    # price_list (the reference bracket) is a different field and a different number.
    assert client.get_reference_price(product, variant_id) == 9_000_000
    assert mock_request.call_count == 1  # served from the item resolve_variant_id cached


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_get_current_price_fetches_variant_detail_when_list_item_has_no_price_sale(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [
        _search_response([_variant_item(price_list=9_000_000, price_sale=None)]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "status": "success",
                "data": {"price_list": 9_000_000, "price_sale": 2_000_000},
            },
        ),
    ]
    client = _client(tmp_path, dry_run=False)
    product = _product()

    variant_id = client.resolve_variant_id(product)
    current = client.get_current_price(product, variant_id)

    assert current == 2_000_000
    assert mock_request.call_count == 2
    method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
    assert method == "GET"
    assert url == "https://seller.digikala.com/open-api/v1/variants/83841925"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_get_current_price_fetches_variant_detail_when_nothing_is_cached(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": "success", "data": {"price_list": 1, "price_sale": 3_000_000}},
    )
    client = _client(tmp_path, dry_run=False)

    price = client.get_current_price(_product(), "83841925")

    assert price == 3_000_000
    mock_request.assert_called_once()
    assert mock_request.call_args.args[1] == "https://seller.digikala.com/open-api/v1/variants/83841925"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_get_current_price_without_price_sale_anywhere_is_a_non_retryable_error(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_list": 4_500_000}}
    )
    client = _client(tmp_path, dry_run=False)

    with pytest.raises(PlatformAPIError, match="price_sale") as excinfo:
        client.get_current_price(_product(), "83841925")

    assert excinfo.value.retryable is False


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_price_check_reads_work_under_dry_run_and_only_ever_send_get(
    mock_request: MagicMock, tmp_path: Path
):
    # The check is read-only, so DRY_RUN must not stop it, and it must never
    # reach a write request either.
    mock_request.return_value = _search_response([_variant_item(price_sale=1_100_000)])
    client = _client(tmp_path, dry_run=True)
    product = _product()

    variant_id = client.resolve_variant_id(product)
    price = client.get_current_price(product, variant_id)

    assert price == 1_100_000
    assert [c.args[0] for c in mock_request.call_args_list] == ["GET"]


def test_price_step_is_100_rial_and_expected_price_rounds_down(tmp_path: Path):
    client = _client(tmp_path, dry_run=True)

    assert client.price_step_rial == 100
    assert client.expected_price_for_target(1_234_567) == 1_234_500
    assert client.expected_price_for_target(1_234_500) == 1_234_500  # already a multiple


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_expected_price_for_target_equals_what_update_price_really_sends(
    mock_request: MagicMock, tmp_path: Path
):
    # The whole point of price_step_rial: the check compares against exactly
    # the value update_price would have written.
    mock_request.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_234_500}}
    )
    client = _client(tmp_path, dry_run=False)

    client.update_price("83841925", 1_234_567)

    sent = mock_request.call_args.kwargs["json"]["selling_price"]
    assert sent == client.expected_price_for_target(1_234_567) == 1_234_500


# -- HTTP 429: wait, retry the same request, slow down, pause both accounts -------


def _429(retry_after: str | None = None) -> MagicMock:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return MagicMock(status_code=429, text="Too Many Requests", headers=headers)


def _detail_response(price_list: int = 1_000_000) -> MagicMock:
    return MagicMock(status_code=200, json=lambda: {"status": "success", "data": {"price_list": price_list}})


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_429_waits_for_retry_after_then_retries_the_same_request(
    mock_request: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [_429("7"), _detail_response(1_234_000)]
    client = _client(tmp_path, dry_run=False, rate_limit_max_retries=2)

    assert client.get_reference_price(_product(), "83841925") == 1_234_000

    assert mock_request.call_count == 2
    assert mock_request.call_args_list[0] == mock_request.call_args_list[1]  # identical request
    # (sleep is mocked, so the small read-spacing sleeps are ignored here)
    long_waits = [c.args[0] for c in mock_sleep.call_args_list if c.args[0] > 2]
    assert len(long_waits) == 1
    assert long_waits[0] == pytest.approx(7, abs=0.5)


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_429_without_retry_after_backs_off_exponentially(
    mock_request: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [_429(), _429(), _detail_response()]
    client = _client(
        tmp_path, dry_run=False, rate_limit_max_retries=3, rate_limit_backoff_seconds=5.0
    )

    client.get_reference_price(_product(), "83841925")

    waits = [call.args[0] for call in mock_sleep.call_args_list if call.args[0] > 4]
    assert len(waits) == 2  # (the smaller read-spacing sleeps are ignored)
    assert waits[0] == pytest.approx(5, abs=0.5)
    assert waits[1] == pytest.approx(10, abs=0.5)


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_429_gives_up_after_the_retry_budget_and_still_fails_the_request(
    mock_request: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    mock_request.return_value = _429("1")
    client = _client(tmp_path, dry_run=False, rate_limit_max_retries=2)

    with pytest.raises(PlatformAPIError) as excinfo:
        client.get_reference_price(_product(), "83841925")

    assert excinfo.value.status_code == 429
    assert mock_request.call_count == 3  # first try + 2 retries


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_429_on_one_account_pauses_the_other_account_too(
    mock_request: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    main = _client(tmp_path / "a", dry_run=False, rate_limit_max_retries=2)
    saree = _client(tmp_path / "b", dry_run=False, rate_limit_max_retries=2)
    mock_request.side_effect = [_429("3"), _detail_response(), _detail_response()]

    main.get_reference_price(_product(), "1")  # gets the 429, waits, succeeds
    mock_sleep.reset_mock()
    saree.get_reference_price(_product(), "2")  # never saw a 429 itself

    mock_sleep.assert_called_once()  # ...but still waited out the shared pause
    assert mock_sleep.call_args.args[0] == pytest.approx(3, abs=0.5)


@patch("faraz_honar.platforms.digikala.client.time.sleep")
@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_read_spacing_grows_after_429_and_recovers_after_a_clean_streak(
    mock_request: MagicMock, mock_sleep: MagicMock, tmp_path: Path
):
    client = _client(tmp_path, dry_run=False, read_delay_seconds=1.0, rate_limit_max_retries=2)
    mock_request.side_effect = [_429("1"), *[_detail_response() for _ in range(20)]]

    client.get_reference_price(_product(), "1")  # 429 then success (streak = 1)
    assert client._read_delay_seconds == pytest.approx(1.5)

    for _ in range(19):  # streak reaches 20 -> spacing eases (1.5 * 0.8)
        client.get_reference_price(_product(), "1")
    assert client._read_delay_seconds == pytest.approx(1.2)


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_no_internal_429_retry_when_disabled(mock_request: MagicMock, tmp_path: Path):
    mock_request.return_value = _429("30")
    client = _client(tmp_path, dry_run=False, rate_limit_max_retries=0)
    with pytest.raises(PlatformAPIError):
        client.get_reference_price(_product(), "1")
    mock_request.assert_called_once()


# -- Price check: read the whole catalog once instead of searching per product -----


def _catalog_item(variant_id: int, dkp: int, price_sale: int = 5_000_000, **extra) -> dict:
    return _variant_item(
        id=variant_id,
        product_id=dkp,
        product_url=f"https://www.digikala.com/product/dkp-{dkp}/slug/",
        price_sale=price_sale,
        **extra,
    )


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_price_check_preload_resolves_many_products_with_one_listing_request(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.return_value = _search_response([_catalog_item(1, 111, 7_000_000), _catalog_item(2, 222)])
    client = _client(tmp_path, dry_run=True)

    client.prepare_price_check()
    v1 = client.resolve_variant_id(_product(platform_refs={"digikala": "111"}))
    v2 = client.resolve_variant_id(_product(platform_refs={"digikala": "222"}))

    assert (v1, v2) == ("1", "2")
    assert client.get_current_price(_product(), v1) == 7_000_000  # from the cached item
    assert mock_request.call_count == 1  # only the listing; no per-product search
    assert "search[search_term]" not in mock_request.call_args.kwargs["params"]


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_price_check_preload_walks_every_page(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _search_response([_catalog_item(1, 111)], total_pages=2),
        _search_response([_catalog_item(2, 222)], total_pages=2),
    ]
    client = _client(tmp_path, dry_run=True)

    client.prepare_price_check()

    assert mock_request.call_count == 2
    assert client.resolve_variant_id(_product(platform_refs={"digikala": "222"})) == "2"
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_price_check_falls_back_to_search_when_the_listing_fails(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        MagicMock(status_code=500, text="boom"),  # catalog listing fails -> never raises
        _search_response([_catalog_item(1, 111)]),  # per-product search still works
    ]
    client = _client(tmp_path, dry_run=True)

    client.prepare_price_check()
    assert client.resolve_variant_id(_product(platform_refs={"digikala": "111"})) == "1"
    assert mock_request.call_count == 2
    assert mock_request.call_args.kwargs["params"]["search[search_term]"] == "111"


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_product_missing_from_the_preloaded_catalog_is_still_searched(
    mock_request: MagicMock, tmp_path: Path
):
    mock_request.side_effect = [
        _search_response([_catalog_item(1, 111)]),  # listing
        _search_response([_catalog_item(2, 222)]),  # fallback search for 222
    ]
    client = _client(tmp_path, dry_run=True)

    client.prepare_price_check()
    assert client.resolve_variant_id(_product(platform_refs={"digikala": "222"})) == "2"
    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_finish_price_check_drops_the_catalog(mock_request: MagicMock, tmp_path: Path):
    mock_request.side_effect = [
        _search_response([_catalog_item(1, 111)]),  # listing
        _search_response([_catalog_item(1, 111)]),  # search after finish
    ]
    client = _client(tmp_path, dry_run=True)

    client.prepare_price_check()
    client.finish_price_check()
    client.resolve_variant_id(_product(platform_refs={"digikala": "111"}))

    assert mock_request.call_count == 2


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_preloaded_inactive_only_variant_still_goes_through_the_normal_search(
    mock_request: MagicMock, tmp_path: Path
):
    inactive = _catalog_item(1, 111, active=False)
    mock_request.side_effect = [
        _search_response([inactive]),  # listing
        _search_response([_catalog_item(9, 111)]),  # search finds the live variant
    ]
    client = _client(tmp_path, dry_run=True)

    client.prepare_price_check()
    assert client.resolve_variant_id(_product(platform_refs={"digikala": "111"})) == "9"


def test_digikala_requests_send_the_configured_user_agent(monkeypatch):
    """DigiKala's Open API rejects ordinary User-Agents (401 "Invalid
    User-Agent!") since 2026-10-11; the client must send the one it is given."""
    from types import SimpleNamespace

    from faraz_honar.platforms.digikala import client as client_module
    from faraz_honar.platforms.digikala.client import DigikalaClient

    seen = {}

    class _Tokens:
        def is_expiring(self):
            return False

        def current_tokens(self):
            return SimpleNamespace(access_token="tok", refresh_token="ref")

    class _Resp:
        status_code = 200
        headers = {}
        text = "{}"

        def json(self):
            return {}

    def fake_request(method, url, headers=None, **kwargs):
        seen["headers"] = headers
        return _Resp()

    monkeypatch.setattr(client_module.requests, "request", fake_request)
    client = DigikalaClient(
        name="digikala2",
        base_url="https://seller.digikala.com",
        token_manager=_Tokens(),
        dry_run=True,
        read_delay_seconds=0,
        write_delay_seconds=0,
        user_agent="DigikalaTrustedBot/SelfToken/1385180",
    )
    client._request("GET", "/open-api/v1/variants")
    assert seen["headers"]["User-Agent"] == "DigikalaTrustedBot/SelfToken/1385180"
