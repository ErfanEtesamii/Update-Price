"""TapsiShop vendor-gateway API integration.

Endpoints used:
  - `GET /Web/Hub/vendors/v1/products/{page}/{pageSize}` — the only
    documented way to find a product: there is no by-HSIN lookup endpoint,
    so finding one product means paginating this list (page 1..N,
    pageSize=100 — see _DEFAULT_PAGE_SIZE) and matching each item's `hsin`
    against the sheet's "تپسی شاپ" value. Matched items are cached by their
    `sku` (see resolve_variant_id/_find_product_by_hsin) — `hsin` is what
    the sheet actually stores and is used ONLY for matching; `sku` (the
    vendor-side product identifier, per the official API documentation) is
    what the write endpoint actually requires as its `id`. TapsiShop's own
    internal `id` field is a different, TapsiShop-side identifier that is
    NEVER sent to the write endpoint and is not used anywhere in this
    module.
    CONFIRMED against real, live data (both the checked-in sheet fixture
    and a live read of the production Google Sheet, 548 real rows): every
    "تپسی شاپ" cell is a 19-digit numeric string in the same digit-length/
    leading-prefix family as the documented `hsin` example value
    ("1475748894590844929") — never the descriptive Persian product-title
    text the documented `sku` example shows ("تخته نرد - شطرنج 40 خاتم
    نقاشی اسلیمی کد 01"), which makes a sheet-value == sku match
    structurally impossible regardless of which real product is checked.
    This is also the user's own confirmed source-of-truth: the sheet column
    holds TapsiShop HSIN values. `sku` is never used for matching — only
    for identifying the item to the write endpoint (see below).
    Pagination is driven by `data.totalCount` (`total_pages =
    ceil(totalCount / pageSize)`), never by assuming page 1 holds every
    product and never by guessing "done" from an empty page — see
    _total_pages/_fetch_page.
  - `PUT /web/hub/vendors/v1/products` (bulk-shaped, one item per call from
    this codebase) — takes `{"products": [{"id", "stock", "price",
    "specialPrice", "referenceCode"}, ...]}`. Per the official API
    documentation, the payload's `id` field is documented as "the sku of
    your product" (i.e. the vendor-side product identifier) — NOT
    TapsiShop's own internal `id` field from the GET response. This module
    sends the matched item's `sku` as the payload `id` (see _item_sku).
    update_price is called once per product by the existing, unchanged
    per-product orchestration loop in update/service.py, so each call here
    sends exactly one item.
    `stock` is REQUIRED by the API and is never invented: it is always the
    real `onHandQuantity` most recently read back from the GET list for
    that product (cached alongside the item — see
    `_last_resolved_item_by_variant`); if a product's response has no
    `onHandQuantity`, update_price fails clearly for that product rather
    than silently sending 0. `referenceCode` is a fresh random value per
    request (see _generate_reference_code) — TapsiShop's own idempotency/
    tracing key, unrelated to any product identifier.
    `price`/`specialPrice` — IMPORTANT: the API documentation requires both
    to be a multiple of 10 Rial. This module rounds the computed price DOWN
    to the nearest multiple of 10 before sending it (see
    _round_down_to_multiple_of_ten), which never sends a price higher than
    what the pricing algorithm decided.
    The PUT response is validated beyond the shared top-level `success`
    check: `data.status` and each item's `status` in `data.data[]` are also
    checked (see _raise_for_item_failure), since an individual item can
    fail (`status: false`) even when the top-level request succeeded.

Units — IMPORTANT: TapsiShop's API already operates in RIAL end to end
(`originalPrice`, `finalPrice`, and the PUT `price`/`specialPrice` are all
Rial), exactly like the rest of this application (the sheet's converted
target_price, PricingDecision, DigikalaClient) — UNLIKE SnappShop, which
needed a Toman<->Rial boundary conversion. This module deliberately does
NOT apply any ×10/÷10 conversion anywhere: get_reference_price returns
`originalPrice` completely unchanged, and update_price sends the Rial price
the generic pricing algorithm computed completely unchanged. Applying a
conversion here would double-convert an already-Rial value.

Reference price / discount fields — IMPORTANT: `originalPrice` is the
reference price the generic 12% rule compares against; `finalPrice` (the
API's OWN already-discounted price) is never used as the reference price
and is not read by this client at all. On update, `price` and
`specialPrice` are both set to the same new target price (no discount is
ever introduced by this application).

Auth: a single static token (TAPSI_TOKEN), sent as-is (NOT prefixed with
"Bearer ") in the `Tapsi-Shop-Hub-Authorization` header — TapsiShop's
header contract is unrelated to DigiKala's/SnappShop's Authorization
headers and must never be confused with them. No refresh-token flow is
documented for this API, so there is no TokenManager/auth module here; a
401 is just a hard, non-retryable auth failure.

DRY_RUN semantics (same policy as every other platform in this app): reads
(the product-list pagination, used by both resolve_variant_id and
get_reference_price) are real GET requests regardless of DRY_RUN, so the
12% calculation can be verified against real TapsiShop data. update_price
(the only write, via PUT) is the one thing DRY_RUN blocks.
"""
from __future__ import annotations

import logging
import math
import time
import uuid

import requests

from faraz_honar.core.exceptions import (
    ConfigurationError,
    PlatformAPIError,
    PlatformAuthError,
    VariantResolutionError,
)
from faraz_honar.core.models import Product
from faraz_honar.platforms.base import PlatformClient

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 100
_MAX_SEARCH_PAGES = 200  # hard safety cap on top of the API's own totalCount-derived page count

# Same write/read classification convention as the other platform clients,
# kept as its own module-level constant rather than shared/imported so
# every platform client stays fully independent of the others.
_WRITE_METHODS = frozenset({"PUT", "PATCH", "POST", "DELETE"})


def _total_pages(total_count: int, page_size: int) -> int:
    if page_size <= 0 or total_count <= 0:
        return 0
    return math.ceil(total_count / page_size)


def _extract_data(payload: object) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    raise PlatformAPIError(
        "TapsiShop product list response has an unrecognized shape (expected a 'data' object).",
        retryable=False,
    )


def _index_by_key(items: list[dict], key: str) -> dict[str, dict]:
    """Pure hsin->item (or id->item) indexer, built from one page's items.
    Values missing `key` are skipped rather than indexed under a synthetic
    key.
    """
    index: dict[str, dict] = {}
    for item in items:
        value = item.get(key)
        if value is not None:
            index[str(value)] = item
    return index


def _generate_reference_code() -> str:
    # TapsiShop's own per-request tracing/idempotency key — has nothing to
    # do with any product identifier, so a fresh random value per call is
    # exactly "unique and appropriate" without needing any external state.
    return f"faraz-honar-{uuid.uuid4().hex}"


class TapsiShopClient(PlatformClient):
    """One TapsiShop vendor account. Structurally mirrors
    SnappShopClient/DigikalaClient: same throttle/error-classification/
    DRY_RUN pattern, same per-run "last resolved item" cache — adapted only
    where TapsiShop's actual API shape genuinely differs (pageSize/page
    pagination sized from `totalCount` rather than a `total_pages` field or
    an empty-page signal, a single-item PUT payload shape with `stock`/
    `price`/`specialPrice`/`referenceCode`, an unprefixed static token
    header, and no unit conversion since the API is already Rial-native).
    """

    # update_price rounds DOWN to a multiple of 10 Rial (API requirement).
    price_step_rial = 10

    def __init__(
        self,
        *,
        name: str = "tapsi_shop",
        base_url: str,
        token: str | None,
        dry_run: bool,
        timeout_seconds: float = 15.0,
        read_delay_seconds: float = 1.0,
        write_delay_seconds: float = 2.0,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._dry_run = dry_run
        self._timeout = timeout_seconds
        self._page_size = page_size
        # hsin -> item for the WHOLE catalog, built once by prepare_price_check
        # and only set while a price check is running. None (the normal state)
        # leaves resolve_variant_id's page-walking lookup completely unchanged.
        self._check_index_by_hsin: dict[str, dict] | None = None
        # Short-lived, per-process cache of the last product item resolved
        # for a given id, so get_reference_price/update_price don't need a
        # second full-catalog pagination right after resolve_variant_id
        # found it, and so update_price can read back the real
        # `onHandQuantity` rather than inventing a stock value. Always
        # re-resolved fresh on the next resolve_variant_id call (e.g. each
        # hourly recheck), so this never serves a stale price/stock across
        # runs — same contract as the other platform clients' equivalent
        # cache.
        self._last_resolved_item_by_variant: dict[str, dict] = {}
        self._read_delay_seconds = read_delay_seconds
        self._write_delay_seconds = write_delay_seconds
        self._last_read_request_at: float | None = None
        self._last_write_request_at: float | None = None

    def _products_page_path(self, page: int) -> str:
        return f"/Web/Hub/vendors/v1/products/{page}/{self._page_size}"

    def _update_path(self) -> str:
        return "/web/hub/vendors/v1/products"

    # -- Product resolution --------------------------------------------------------
    def resolve_variant_id(self, product: Product) -> str:
        raw_ref = product.platform_ref(self.name)
        if not raw_ref:
            raise VariantResolutionError(
                f"Product '{product.internal_id}' has no TapsiShop reference in the sheet's "
                f"'{self.name}' column.",
                retryable=False,
            )

        # Reads are allowed under DRY_RUN (DRY_RUN only blocks writes — see
        # update_price). This makes real GET requests regardless of DRY_RUN.
        item = self._find_product_by_hsin(raw_ref.strip(), product)
        variant_id = _item_sku(item, product)
        self._last_resolved_item_by_variant[variant_id] = item
        return variant_id

    def _fetch_page(self, page: int) -> tuple[list[dict], int]:
        response = self._request("GET", self._products_page_path(page))
        self._raise_for_error_status(response, context="product list fetch")
        data = _extract_data(response.json())

        items = data.get("items")
        if not isinstance(items, list):
            raise PlatformAPIError(
                "TapsiShop product list response is missing a 'data.items' array.",
                retryable=False,
            )
        total_count = data.get("totalCount")
        if not isinstance(total_count, int):
            raise PlatformAPIError(
                "TapsiShop product list response is missing 'data.totalCount'.",
                retryable=False,
            )
        total_pages = min(_total_pages(total_count, self._page_size), _MAX_SEARCH_PAGES)
        return items, total_pages

    def prepare_price_check(self) -> None:
        """Read the whole catalog ONCE (a handful of pages) so the price check
        does not re-walk it from page 1 for every single product."""
        index: dict[str, dict] = {}
        page = 1
        items, total_pages = self._fetch_page(page)
        index.update(_index_by_key(items, "hsin"))
        while page < total_pages:
            page += 1
            items, total_pages = self._fetch_page(page)
            index.update(_index_by_key(items, "hsin"))
        self._check_index_by_hsin = index

    def finish_price_check(self) -> None:
        self._check_index_by_hsin = None

    def estimate_check_seconds(self, product_count: int) -> float:
        return self._pages_estimate_seconds(product_count, self._page_size)

    def _find_product_by_hsin(self, hsin: str, product: Product) -> dict:
        if self._check_index_by_hsin is not None:
            item = self._check_index_by_hsin.get(hsin)
            if item is None:
                raise VariantResolutionError(
                    f"No TapsiShop product found with hsin '{hsin}' (product "
                    f"'{product.internal_id}') in the full catalog read for the price check.",
                    retryable=False,
                )
            return item
        page = 1
        items, total_pages = self._fetch_page(page)
        index = _index_by_key(items, "hsin")
        while hsin not in index and page < total_pages:
            page += 1
            items, total_pages = self._fetch_page(page)
            index.update(_index_by_key(items, "hsin"))

        item = index.get(hsin)
        if item is None:
            raise VariantResolutionError(
                f"No TapsiShop product found with hsin '{hsin}' (product "
                f"'{product.internal_id}') after checking all {total_pages} page(s) of "
                f"{self._products_page_path(1)}.",
                retryable=False,
            )
        return item

    # -- Reference price (originalPrice, already Rial — never finalPrice) ----------
    def get_reference_price(self, product: Product, variant_id: str) -> int:
        item = self._last_resolved_item_by_variant.get(variant_id)
        if item is None:
            item = self._find_product_by_sku_fallback(variant_id, product)
            self._last_resolved_item_by_variant[variant_id] = item

        price = item.get("originalPrice")
        if price is None:
            raise PlatformAPIError(
                f"TapsiShop product {variant_id} response has no 'originalPrice' field.",
                retryable=False,
            )
        # originalPrice is Rial already — no ×10/÷10 conversion (see module
        # docstring). Comes back as a float in the documented response
        # (e.g. 150000000.0); round() rather than a bare int() truncation.
        return int(round(float(price)))

    def _find_product_by_sku_fallback(self, variant_id: str, product: Product) -> dict:
        page = 1
        items, total_pages = self._fetch_page(page)
        index = _index_by_key(items, "sku")
        while variant_id not in index and page < total_pages:
            page += 1
            items, total_pages = self._fetch_page(page)
            index.update(_index_by_key(items, "sku"))

        item = index.get(variant_id)
        if item is None:
            raise PlatformAPIError(
                f"TapsiShop product {variant_id} could not be re-fetched (product "
                f"'{product.internal_id}').",
                retryable=False,
            )
        return item

    # -- Price update (single-item PUT, Rial units, real stock preserved) ----------
    def update_price(self, variant_id: str, new_price: int) -> None:
        # The API requires price/specialPrice to be a multiple of 10 Rial.
        # Rounding DOWN never sends a price higher than what the pricing
        # algorithm (core/pricing.py) decided.
        price = _round_down_to_multiple_of_ten(new_price)

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would PUT price=%s (Rial) to TapsiShop product %s (no request sent)",
                price,
                variant_id,
            )
            return

        item = self._last_resolved_item_by_variant.get(variant_id)
        if item is None:
            # Should never happen in the real call flow — update/service.py
            # always calls resolve_variant_id then get_reference_price
            # first, which populate this cache. This guards against ever
            # sending a request with an invented/default stock value if
            # that invariant were somehow violated by a future caller.
            raise PlatformAPIError(
                f"Cannot update TapsiShop product {variant_id}: no previously-fetched product "
                "data (needed for its real 'onHandQuantity') available.",
                retryable=False,
            )

        stock = item.get("onHandQuantity")
        if stock is None:
            raise PlatformAPIError(
                f"TapsiShop product {variant_id} response has no 'onHandQuantity' field — "
                "refusing to PUT without the real current stock.",
                retryable=False,
            )

        payload = {
            "products": [
                {
                    "id": variant_id,
                    "stock": int(stock),
                    "price": price,
                    "specialPrice": price,
                    "referenceCode": _generate_reference_code(),
                }
            ]
        }
        response = self._request("PUT", self._update_path(), json_body=payload)
        self._raise_for_error_status(response, context="price update")
        self._raise_for_item_failure(response, variant_id, context="price update")

        logger.info(
            "TapsiShop product %s price update sent: price=%s Rial, stock=%s",
            variant_id,
            price,
            stock,
        )

    # -- Shared request plumbing -----------------------------------------------------
    def _request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> requests.Response:
        if not self._token:
            raise ConfigurationError("TapsiShop is not configured: set TAPSI_TOKEN.")

        url = f"{self._base_url}{path}"
        # Exactly these five headers, per the documented contract — NOT
        # "Bearer <token>": the token is sent as-is under
        # Tapsi-Shop-Hub-Authorization, a header unrelated to (and never
        # reused from) DigiKala's or SnappShop's auth headers.
        headers = {
            "Tapsi-Shop-Hub-Authorization": self._token,
            "Accept": "text/plain",
            "Content-Type": "application/json",
            "client-name": "Swagger on HIT.Hastim.Hub.Endpoints.WebApi",
            "client-version": "1.0.0.0",
        }

        self._throttle(method)
        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise PlatformAPIError(
                f"TapsiShop request failed ({method} {path}): {exc}", retryable=True
            ) from exc

        return response

    def _throttle(self, method: str) -> None:
        # Identical throttling strategy to the other platform clients — see
        # DigikalaClient._throttle's docstring for the full rationale. Kept
        # as separate, independent per-instance state so TapsiShop requests
        # never wait on any other platform's requests.
        is_write = method in _WRITE_METHODS
        delay = self._write_delay_seconds if is_write else self._read_delay_seconds
        last_at = self._last_write_request_at if is_write else self._last_read_request_at

        now = time.monotonic()
        if last_at is None or delay <= 0:
            new_last_at = now
        else:
            remaining = delay - (now - last_at)
            if remaining > 0:
                time.sleep(remaining)
            new_last_at = max(now, last_at + delay)

        if is_write:
            self._last_write_request_at = new_last_at
        else:
            self._last_read_request_at = new_last_at

    @staticmethod
    def _raise_for_error_status(response: requests.Response, *, context: str) -> None:
        if response.status_code == 401:
            raise PlatformAuthError(
                f"TapsiShop rejected the access token ({context}): {response.text[:500]}",
                status_code=401,
                retryable=False,
            )

        if response.status_code not in (200, 201):
            # 4xx (auth/validation/not-found/rate-limit) errors are never
            # retried — retrying an invalid request or a 429 immediately
            # only wastes requests / worsens rate-limiting, same policy as
            # every other platform client here. 5xx/network errors remain
            # retryable, capped at PriceUpdateService's max_retry_attempts.
            retryable = response.status_code >= 500
            raise PlatformAPIError(
                f"TapsiShop {context} failed: HTTP {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
                retryable=retryable,
            )

        # HTTP 200/201 can still carry a business-level failure in the
        # documented {"success": false, "messages": [...]} envelope — an
        # HTTP-status-only check would silently treat that as a success.
        try:
            body = response.json()
        except ValueError:
            return  # non-JSON 200 body (e.g. a plain-text ack) — nothing further to check
        if isinstance(body, dict) and body.get("success") is False:
            messages = body.get("messages") or []
            detail = "; ".join(str(m) for m in messages) or "no message provided"
            raise PlatformAPIError(
                f"TapsiShop {context} reported failure: {detail}",
                status_code=response.status_code,
                retryable=False,
            )

    @staticmethod
    def _raise_for_item_failure(
        response: requests.Response, variant_id: str, *, context: str
    ) -> None:
        # PUT-specific: the documented response envelope nests a per-item
        # result under data.data[], each with its own `status`/`messages`.
        # A top-level success=true (already checked by
        # _raise_for_error_status, which is shared with GET and stays
        # generic) does not guarantee any individual item actually
        # succeeded, so this is checked separately and only for PUT.
        try:
            body = response.json()
        except ValueError:
            return

        if not isinstance(body, dict):
            return
        data = body.get("data")
        if not isinstance(data, dict):
            return
        if data.get("status") is False:
            raise PlatformAPIError(
                f"TapsiShop {context} reported data.status=false for product {variant_id}.",
                status_code=response.status_code,
                retryable=False,
            )

        items = data.get("data")
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and item.get("status") is False:
                messages = item.get("messages") or []
                detail = "; ".join(str(m) for m in messages) or "no message provided"
                raise PlatformAPIError(
                    f"TapsiShop {context} failed for product {variant_id}: {detail}",
                    status_code=response.status_code,
                    retryable=False,
                )


def _round_down_to_multiple_of_ten(rial: int) -> int:
    # The API requires price/specialPrice to be a multiple of 10 Rial.
    # Rounding down (never up) guarantees the sent price never exceeds what
    # the pricing algorithm decided.
    return (rial // 10) * 10


def _item_sku(item: dict, product: Product) -> str:
    # 'hsin' is the matching key (see _find_product_by_hsin) but is NEVER
    # the identifier sent to the write endpoint. Per the official API
    # documentation, the PUT payload's `id` field is "the sku of your
    # product" (the vendor-side product identifier) — NOT TapsiShop's own
    # internal `id` field from the GET response, which is never used here.
    raw_sku = item.get("sku")
    if raw_sku is None:
        raise VariantResolutionError(
            f"TapsiShop product for '{product.internal_id}' has no 'sku' in the product list "
            "response.",
            retryable=False,
        )
    return str(raw_sku)
