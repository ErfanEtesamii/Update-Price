"""End-to-end proof of the full documented flow (spec: Sheet -> DigiKala link
-> VARIANT_ID -> live reference price -> 12% cap -> price update), wired
through the real PriceUpdateService + real DigikalaClient, with only the
HTTP layer mocked. No real network call is made.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

from faraz_honar.core.models import OutcomeStatus, Product
from faraz_honar.platforms.digikala.auth import TokenManager
from faraz_honar.platforms.digikala.client import DigikalaClient
from faraz_honar.sheets.base import ProductSource
from faraz_honar.state.repository import StateRepository
from faraz_honar.update.service import PriceUpdateService


class _OneProductSource(ProductSource):
    def __init__(self, product: Product):
        self._product = product

    def fetch_products(self) -> list[Product]:
        return [self._product]


def _search_response(items: list[dict]) -> MagicMock:
    return MagicMock(
        status_code=200,
        json=lambda: {"status": "success", "data": {"pager": {"total_pages": 1}, "items": items}},
    )


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_full_flow_within_12pct_updates_directly(mock_request: MagicMock, tmp_path: Path):
    product = Product(
        internal_id="آجیل خوری 11",
        name="آجیل خوری 11 الماس تراش",
        reference_price=None,  # sheet price is irrelevant for DigiKala now
        target_price=1_100_000,
        platform_refs={"digikala": "20589157"}, display_name="آجیل خوری 11 الماس تراش", green_platforms=frozenset({"digikala"}),
    )

    # 1st call: GET /variants (search) -> match + live price_list
    # 2nd call: PUT /variants/{id} -> price update
    mock_request.side_effect = [
        _search_response(
            [
                {
                    "id": 83841925,
                    "product_id": 20589157,
                    "product_url": "https://www.digikala.com/product/dkp-20589157/x/",
                    "price_list": 1_000_000,
                }
            ]
        ),
        MagicMock(status_code=200, json=lambda: {"status": "success", "data": {"price_sale": 1_100_000}}),
    ]

    token_manager = TokenManager(
        base_url="https://seller.digikala.com",
        tokens_file=tmp_path / "tokens.json",
        bootstrap_access_token="access-1",
        bootstrap_refresh_token="refresh-1",
        enable_refresh=False,
    )
    digikala = DigikalaClient(
        base_url="https://seller.digikala.com", token_manager=token_manager, dry_run=False
    )

    service = PriceUpdateService(
        product_source=_OneProductSource(product),
        platform_registry={"digikala": digikala},
        state_repository=StateRepository(tmp_path / "state.db"),
        max_increase_pct=0.12,
        max_retry_attempts=3,
    )

    summaries = service.run(["digikala"], triggered_by="test")
    result = summaries["digikala"].results[0]

    assert result.status == OutcomeStatus.SUCCESS
    assert result.variant_id == "83841925"
    assert result.previous_price == 1_000_000  # live price_list, not the (None) sheet value
    assert result.applied_price == 1_100_000

    put_call = mock_request.call_args_list[1]
    assert put_call.args[0] == "PUT"
    assert put_call.args[1] == "https://seller.digikala.com/open-api/v1/variants/83841925"
    assert put_call.kwargs["json"] == {"selling_price": 1_100_000}

    # State persisted correctly for reporting/hourly-recheck purposes.
    state = service._state_repository.get_product_state("آجیل خوری 11", "digikala")  # noqa: SLF001
    assert state is not None
    assert state.status == "success"
    assert state.is_capped is False


@patch("faraz_honar.platforms.digikala.client.requests.request")
def test_full_flow_exceeding_digikala_reference_price_range_caps_and_marks_for_recheck(
    mock_request: MagicMock, tmp_path: Path
):
    product = Product(
        internal_id="p1",
        name="Product 1",
        reference_price=None,
        target_price=2_000_000,
        platform_refs={"digikala": "20589157"}, display_name="Product 1", green_platforms=frozenset({"digikala"}),
    )
    mock_request.side_effect = [
        _search_response(
            [{"id": 1, "product_id": 20589157, "product_url": "", "price_list": 1_000_000}]
        ),
        # DigiKala itself rejects the full (uncapped) target_price via its
        # own reference-price-range validation.
        MagicMock(
            status_code=400,
            text='{"errors":{"selling_price":["قیمت فروش باید در بازه قیمت مرجع باشد"]}}',
            json=lambda: {
                "errors": {"selling_price": ["قیمت فروش باید در بازه قیمت مرجع باشد"]}
            },
        ),
    ]

    token_manager = TokenManager(
        base_url="https://seller.digikala.com",
        tokens_file=tmp_path / "tokens.json",
        bootstrap_access_token="access-1",
        bootstrap_refresh_token="refresh-1",
        enable_refresh=False,
    )
    digikala = DigikalaClient(
        base_url="https://seller.digikala.com", token_manager=token_manager, dry_run=False
    )
    state_repo = StateRepository(tmp_path / "state.db")
    service = PriceUpdateService(
        product_source=_OneProductSource(product),
        platform_registry={"digikala": digikala},
        state_repository=state_repo,
    )

    summaries = service.run(["digikala"], triggered_by="test")
    result = summaries["digikala"].results[0]

    assert result.status == OutcomeStatus.CAPPED_12PCT
    assert result.applied_price is None
    assert result.target_price == 2_000_000

    capped = state_repo.list_capped_products("digikala")
    assert len(capped) == 1
    assert capped[0].product_id == "p1"
