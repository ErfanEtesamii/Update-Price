"""Basalam Open API integration (https://developers.basalam.com/docs), for
the store "فراز هنر" (vendor_id 1058040).

Endpoints used:
  - `GET /v1/products/{product_id}` — CONFIRMED against Basalam's own
    published OpenAPI spec (basalam/python-sdk's openapi_data/core.json,
    operationId read_product_v4_products__product_id__get): a direct
    single-product lookup by id, no vendor scoping in the path at all. The
    sheet's "باسلام" column already stores the Basalam product id directly
    (confirmed by the project's own verified API spec), so this is used as
    the `product_id` path parameter exactly as given — never a search or a
    match against a separate human-facing field like `sku` (see item 3 of
    the implementation spec: "Do not use sku as the primary identifier").
    This REPLACES an earlier implementation that paginated the vendor
    product list (`GET /vendors/{VENDOR_ID}/products?page=...`) and matched
    each page's items by `id` — that was O(pages-until-match) real HTTP
    requests per product (confirmed as the cause of very slow/seemingly
    "stuck" runs against a real catalog), whereas this endpoint is exactly
    one request per product, no pagination, no per-run re-walking. See
    _fetch_product_by_id.
    The spec's `prefer` header defaults to `"return=minimal"`; no
    alternative documented value was found for retrieving the full field
    set, so this client omits the header entirely and relies on the
    endpoint's normal (default) response rather than inventing an
    undocumented value.
    Response shape — CONFIRMED against the same OpenAPI spec
    (ReadProductResponse): the product object itself is the top-level JSON
    body (not wrapped in a `data`/list envelope like the old list endpoint
    was) — see _extract_product.
  - `PATCH /vendors/{VENDOR_ID}/products/batch-updates` — takes `{"data":
    [{"id", "stock", "primary_price"}, ...]}`. update_price is called once
    per product by the existing, unchanged per-product orchestration loop in
    update/service.py, so each call here sends exactly one item — well
    within any batch limit — rather than accumulating multiple products'
    updates into one call. The chunking machinery (_chunked/_MAX_BATCH_SIZE)
    is still real (not a no-op shim) so it is correct by construction if a
    future caller ever passes more items; see __init__'s batch_size
    docstring for where the limit itself comes from.

    `stock` is REQUIRED by the API and is never invented: it is always the
    real value most recently read back from `GET /v1/products/{product_id}`
    for that product (cached alongside the item — see
    `_last_resolved_item_by_variant`), never a guessed/default value.

    Response — CONFIRMED against a real response (product id 28317496): this
    endpoint responds HTTP 202 Accepted (not 200/201), with the real
    per-item outcome given immediately in the body as `{"openapi_raw_data":
    [{"id", "has_error", "error_message", "is_product_for_revision"}, ...]}`.
    202 is therefore accepted as a non-error status (see
    _raise_for_error_status), and the actual accept/reject decision for the
    item this call sent is made by inspecting `has_error`/`error_message`
    for the matching `id` (see _raise_for_item_failure) — the same
    "HTTP-status-alone is not enough" pattern already used for TapsiShop's
    PUT response.

Field-name asymmetry — IMPORTANT: the GET response reports current stock as
`inventory`; the PATCH payload's field for the SAME value is named `stock`.
This client reads `inventory` from the cached GET item and sends it back
under the PATCH's `stock` key (see update_price) — `inventory` itself is
never sent in the PATCH payload.

Units/currency — VERY IMPORTANT: `primary_price` and `price` (both in the GET
response, and `primary_price` in the PATCH payload) are already in RIAL,
exactly like the rest of this application's internal pricing pipeline
(Product.target_price, DigikalaClient, TapsiShopClient). The Toman->Rial ×10
conversion happens exactly once, in sheets/mapping.py::toman_to_rial, at the
point the sheet's Toman value is first parsed into Product.target_price.
This module deliberately does NOT apply any ×10/÷10 conversion anywhere:
get_reference_price returns `primary_price` (or `price`, see below)
completely unchanged, and update_price sends the Rial price the generic
pricing algorithm computed completely unchanged as `primary_price`. Applying
a conversion here would double-convert an already-Rial value into an
incorrect x100 effective factor — see
test_no_second_currency_conversion_in_basalam_client for the regression
test proving this.

Reference price — IMPORTANT: `primary_price` is preferred as the reference
price for the generic 12% rule (core/pricing.py), but CONFIRMED against a
real response (product id 28317496): `primary_price` can be present but
explicitly `null` — not merely absent from the payload — while the actual
current price is available under a sibling `price` field. This is not an
error condition, so get_reference_price falls back to `price` whenever
`primary_price` is null/missing, and only raises if neither field has a
usable value. `sku`, `inventory`, and `title` are never used as a price
reference.

Auth: a single static access token (BASALAM_ACCESS_TOKEN), sent as
"Bearer <token>" in the standard Authorization header — no refresh-token
flow is documented for this API, so there is no TokenManager/auth module
here; a 401 is just a hard, non-retryable auth failure.

DRY_RUN semantics (same policy as every other platform in this app): reads
(the single-product GET, used by both resolve_variant_id and
get_reference_price) are real GET requests regardless of DRY_RUN, so the
12% calculation can be verified against real Basalam data. update_price
(the only write, via PATCH) is the one thing DRY_RUN blocks.
"""
from __future__ import annotations

import logging
import time
from typing import Any

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

# The store's real vendor id ("فراز هنر"), used directly per the verified
# API spec — NEVER the JWT's `sub` account id (17351469), which is a
# different, unrelated identifier. Hardcoded (not env-configurable) since
# this implementation is for this one specific store.
VENDOR_ID = 1058040

# Basalam's own documented per-request batch-update limit is not part of
# this project's verified spec yet (unlike SnappShop's confirmed 50-item
# PATCH limit) — 50 is used here only as a conservative default matching
# the rest of this project's established batching convention, and is
# configurable via the `batch_size` constructor parameter rather than
# hardcoded as an unexplained large payload.
_DEFAULT_MAX_BATCH_SIZE = 50

# Read-only price check: the vendor product list endpoint
# (GET /vendors/{vendor_id}/products, `page`/`per_page`) is read ONCE up front
# instead of one GET per product. Its documented `per_page` default is 10 and
# no maximum is documented, so 50 is a request; the real page count always
# comes from the response's own `total_page`, so a lower server-side cap only
# means more pages, never missing products.
_DEFAULT_LIST_PAGE_SIZE = 20
# Why 20 and a longer timeout: from 2026-10-07 openapi.basalam.com started
# answering the 50-per-page catalog read (and single-product GETs) slower than
# the old 15s read timeout, so the bulk read died on page 1 and the check fell
# back to ~900 single GETs x 2 attempts x 15s (~2.3h, 100+ false "errors").
# Smaller pages are cheaper for Basalam to serve, each page is retried on its
# own, and the read timeout leaves room for a slow response.
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_READ_TIMEOUT = 60.0
_LIST_PAGE_ATTEMPTS = 3
_LIST_PAGE_BACKOFF_SECONDS = 3.0
_MAX_LIST_PAGES = 500  # hard safety limit against a runaway paging loop

# Same write/read classification convention as the other platform clients,
# kept as its own module-level constant rather than shared/imported so
# every platform client stays fully independent of the others.
_WRITE_METHODS = frozenset({"PUT", "PATCH", "POST", "DELETE"})


def _extract_product(payload: object) -> dict:
    # CONFIRMED against Basalam's own OpenAPI spec: GET /v1/products/
    # {product_id}'s 200 response IS the product object itself at the
    # top level (id/title/price/primary_price/inventory/sku/variants/...),
    # unlike the old list endpoint's {"data": [...]} envelope.
    if isinstance(payload, dict) and payload.get("id") is not None:
        return payload
    raise PlatformAPIError(
        "Basalam single-product response has an unrecognized shape (expected an object with "
        "an 'id' field).",
        retryable=False,
    )


def _chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class BasalamClient(PlatformClient):
    """The Basalam store "فراز هنر" (vendor_id 1058040). Structurally mirrors
    SnappShopClient/TapsiShopClient: same throttle/error-classification/
    DRY_RUN pattern, same per-run "last resolved item" cache — adapted only
    where Basalam's actual API shape genuinely differs (a direct single-
    product GET by id — no vendor scoping, no pagination, no matching
    against a separate human-facing field — a `stock`/`primary_price` batch
    PATCH instead of DigiKala's price-only PUT, and no unit conversion since
    the API is already Rial-native).
    """

    def __init__(
        self,
        *,
        name: str = "basalam",
        base_url: str,
        token: str | None,
        dry_run: bool,
        timeout_seconds: float = _DEFAULT_READ_TIMEOUT,
        read_delay_seconds: float = 1.0,
        write_delay_seconds: float = 2.0,
        batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        list_page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    ):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._dry_run = dry_run
        self._timeout = (_DEFAULT_CONNECT_TIMEOUT, timeout_seconds)
        self._batch_size = batch_size
        self._list_page_size = max(1, list_page_size)
        # Price-check-only state (see prepare_price_check): the whole vendor
        # catalog indexed by product id. None = normal mode (one GET per product).
        self._check_index: dict[str, dict] | None = None
        self._check_fallback_lookups = 0
        # Short-lived, per-process cache of the last product item resolved
        # for a given id, so get_reference_price/update_price don't need a
        # second GET right after resolve_variant_id found it, and so
        # update_price can read back the real `inventory` rather than
        # inventing a stock value. Always re-resolved fresh on the next
        # resolve_variant_id call (e.g. each hourly recheck), so this never
        # serves a stale price/stock across runs — same contract as the
        # other platform clients' equivalent cache. Independent of every
        # other platform's cache (its own instance attribute on this
        # object), so Basalam state can never leak into/from DigiKala,
        # SnappShop, or TapsiShop.
        self._last_resolved_item_by_variant: dict[str, dict] = {}
        self._read_delay_seconds = read_delay_seconds
        self._write_delay_seconds = write_delay_seconds
        self._last_read_request_at: float | None = None
        self._last_write_request_at: float | None = None

    def _product_path(self, basalam_id: str) -> str:
        return f"/products/{basalam_id}"

    def _update_path(self) -> str:
        return f"/vendors/{VENDOR_ID}/products/batch-updates"

    # -- Product resolution (direct GET by id — no pagination, no search) ----------
    def resolve_variant_id(self, product: Product) -> str:
        raw_ref = product.platform_ref(self.name)
        if not raw_ref:
            raise VariantResolutionError(
                f"Product '{product.internal_id}' has no Basalam reference in the sheet's "
                f"'{self.name}' column.",
                retryable=False,
            )

        # Reads are allowed under DRY_RUN (DRY_RUN only blocks writes — see
        # update_price). This makes a real GET request regardless of
        # DRY_RUN. The sheet's raw_ref IS the Basalam product id (see
        # module docstring) — used directly as the path parameter, never
        # searched/matched against sku or title.
        ref = raw_ref.strip()
        item = self._check_index.get(ref) if self._check_index is not None else None
        if item is None:
            if self._check_index is not None:
                # Not in the bulk catalog read (e.g. a hidden/other-status
                # product): never report "not found" from the bulk read alone —
                # ask for that one product directly, exactly like a normal run.
                self._check_fallback_lookups += 1
            item = self._fetch_product_by_id(ref, product)
        variant_id = _item_id(item, product)
        self._last_resolved_item_by_variant[variant_id] = item
        return variant_id

    # -- Read-only price check: read the vendor catalog in bulk ---------------------
    def _list_path(self) -> str:
        return f"/vendors/{VENDOR_ID}/products"

    def _fetch_list_page(self, page: int) -> tuple[list[dict], int | None]:
        response = self._request(
            "GET",
            self._list_path(),
            params={
                "page": page,
                "per_page": self._list_page_size,
                # one row per product (not per variant), so ids stay unique
                "variants_flatting": "false",
            },
        )
        self._raise_for_error_status(response, context="product list fetch")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise PlatformAPIError(
                "Basalam product list response is missing a 'data' array.", retryable=False
            )
        total_page = payload.get("total_page")
        return data, total_page if isinstance(total_page, int) and total_page >= 1 else None

    def _fetch_list_page_with_retry(self, page: int) -> tuple[list[dict], int | None]:
        """One catalog page, retried on its own so a single slow response does not
        abandon the whole bulk read. Non-retryable errors are raised at once."""
        last_exc: PlatformAPIError | None = None
        for attempt in range(1, _LIST_PAGE_ATTEMPTS + 1):
            try:
                return self._fetch_list_page(page)
            except PlatformAPIError as exc:
                last_exc = exc
                if not exc.retryable or attempt == _LIST_PAGE_ATTEMPTS:
                    raise
                logger.warning(
                    "Basalam price check: catalog page %d failed on attempt %d/%d (%s); retrying.",
                    page,
                    attempt,
                    _LIST_PAGE_ATTEMPTS,
                    exc,
                )
                time.sleep(_LIST_PAGE_BACKOFF_SECONDS * attempt)
        raise last_exc  # pragma: no cover - loop always returns or raises

    def prepare_price_check(self) -> None:
        """Reads the whole vendor catalog ONCE (about N/50 requests) and indexes it by
        product id, so a check of N products does not cost N throttled GETs. Any
        failure here is NOT fatal: the check falls back to one GET per product."""
        self._check_index = None
        self._check_fallback_lookups = 0
        started = time.monotonic()
        index: dict[str, dict] = {}
        page = 1
        try:
            while True:
                items, total_pages = self._fetch_list_page_with_retry(page)
                for item in items:
                    if isinstance(item, dict) and item.get("id") is not None:
                        index[str(item["id"])] = item
                if total_pages is not None:
                    if page >= total_pages:
                        break
                if not items or page >= _MAX_LIST_PAGES:  # no total_page: an empty page is the end
                    break
                page += 1
        except Exception as exc:  # noqa: BLE001 - keep what was read, fetch the rest singly
            # Pages already read are kept: products missing from the index are
            # fetched individually by resolve_variant_id, so a failure on page N
            # costs only the remaining products, not the whole catalog.
            logger.warning(
                "Basalam price check: bulk catalog read failed on page %d (%s); %d product(s) "
                "were read before that, the rest fall back to one request per product.",
                page,
                exc,
                len(index),
            )
            if not index:
                return
        self._check_index = index
        logger.info(
            "Basalam price check: preloaded %d product(s) from the vendor catalog in %d page(s) "
            "(per_page=%d) in %.1fs.",
            len(index),
            page,
            self._list_page_size,
            time.monotonic() - started,
        )

    def finish_price_check(self) -> None:
        if self._check_index is not None:
            logger.info(
                "Basalam price check: %d product(s) were not in the bulk catalog read and "
                "were fetched individually.",
                self._check_fallback_lookups,
            )
        self._check_index = None
        self._check_fallback_lookups = 0

    def estimate_check_seconds(self, product_count: int) -> float:
        return self._pages_estimate_seconds(product_count, self._list_page_size)

    def _fetch_product_by_id(self, basalam_id: str, product: Product) -> dict:
        response = self._request("GET", self._product_path(basalam_id))
        if response.status_code == 404:
            raise VariantResolutionError(
                f"No Basalam product found with id '{basalam_id}' (product "
                f"'{product.internal_id}') at {self._product_path(basalam_id)}.",
                retryable=False,
            )
        self._raise_for_error_status(response, context="product fetch")
        return _extract_product(response.json())

    # -- Reference price (primary_price, falling back to price — never sku/inventory/title) --
    def get_reference_price(self, product: Product, variant_id: str) -> int:
        item = self._last_resolved_item_by_variant.get(variant_id)
        if item is None:
            item = self._fetch_product_by_id(variant_id, product)
            self._last_resolved_item_by_variant[variant_id] = item

        # CONFIRMED against a real response (product id 28317496):
        # 'primary_price' can be present but null — not merely absent — while
        # the actual current price is available under 'price'. This is not
        # an error case: fall back to 'price' whenever 'primary_price' is
        # null/missing, only raising if neither field has a usable value.
        # See module docstring's reference-price section.
        price = item.get("primary_price")
        if price is None:
            price = item.get("price")
        if price is None:
            raise PlatformAPIError(
                f"Basalam product {variant_id} response has no usable 'primary_price' or "
                "'price' field.",
                retryable=False,
            )
        # Rial already — no ×10/÷10 conversion (see module docstring's
        # currency section), for either field.
        return int(price)

    # -- Price update (chunked batch PATCH, Rial units, real stock preserved) ------
    def update_price(self, variant_id: str, new_price: int) -> None:
        if self._dry_run:
            logger.info(
                "[DRY RUN] Would PATCH primary_price=%s (Rial) to Basalam product %s "
                "(no request sent)",
                new_price,
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
                f"Cannot update Basalam product {variant_id}: no previously-fetched product "
                "data (needed for its real 'inventory') available.",
                retryable=False,
            )

        stock = item.get("inventory")
        if stock is None:
            raise PlatformAPIError(
                f"Basalam product {variant_id} response has no 'inventory' field — refusing "
                "to PATCH without the real current stock.",
                retryable=False,
            )

        # GET reports stock as 'inventory'; the PATCH field for the same
        # value is named 'stock' (see module docstring's field-name-
        # asymmetry note) — 'inventory' itself is never sent.
        patch_items = [{"id": int(variant_id), "stock": int(stock), "primary_price": new_price}]
        for batch in _chunked(patch_items, self._batch_size):
            payload = {"data": batch}
            response = self._request("PATCH", self._update_path(), json_body=payload)
            self._raise_for_error_status(response, context="price update")
            self._raise_for_item_failure(response, variant_id, context="price update")

        logger.info(
            "Basalam product %s price update sent: primary_price=%s Rial, stock=%s",
            variant_id,
            new_price,
            stock,
        )

    # -- Shared request plumbing -----------------------------------------------------
    def _request(
        self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> requests.Response:
        if not self._token:
            raise ConfigurationError("Basalam is not configured: set BASALAM_ACCESS_TOKEN.")

        url = f"{self._base_url}{path}"
        # Exactly these three headers, per the documented contract.
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        self._throttle(method)
        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, params=params, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise PlatformAPIError(
                f"Basalam request failed ({method} {path}): {exc}", retryable=True
            ) from exc

        return response

    def _throttle(self, method: str) -> None:
        # Identical throttling strategy to the other platform clients — see
        # DigikalaClient._throttle's docstring for the full rationale. Kept
        # as separate, independent per-instance state so Basalam requests
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
                f"Basalam rejected the access token ({context}): {response.text[:500]}",
                status_code=401,
                retryable=False,
            )

        if response.status_code not in (200, 201, 202):
            # CONFIRMED against a real PATCH response (product id 28317496):
            # the batch-updates endpoint responds HTTP 202 Accepted, with the
            # real per-item outcome in the body (see _raise_for_item_failure)
            # — 202 is therefore not itself an error status, for GET or PATCH.
            # 4xx (auth/validation/not-found/rate-limit) errors are never
            # retried — retrying an invalid request or a 429 immediately
            # only wastes requests / worsens rate-limiting, same policy as
            # every other platform client here. 5xx/network errors remain
            # retryable, capped at PriceUpdateService's max_retry_attempts.
            retryable = response.status_code >= 500
            raise PlatformAPIError(
                f"Basalam {context} failed: HTTP {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
                retryable=retryable,
            )

    @staticmethod
    def _raise_for_item_failure(
        response: requests.Response, variant_id: str, *, context: str
    ) -> None:
        # PATCH-only: CONFIRMED against a real response (product id
        # 28317496), the batch-updates endpoint's real per-item outcome is
        # in the body's `openapi_raw_data[]` array (each entry: `id`,
        # `has_error`, `error_message`), not in the HTTP status alone (see
        # _raise_for_error_status — 202 by itself is not a success/failure
        # signal). Same pattern as TapsiShopClient._raise_for_item_failure:
        # a top-level-OK HTTP status does not guarantee any individual item
        # actually succeeded, so this is checked separately and only for
        # PATCH. Written defensively — an unexpected/missing shape, or no
        # entry matching this variant_id, is never treated as a failure by
        # itself (only an explicit has_error=true is), so a future response
        # shape this doesn't recognize can never silently raise a KeyError.
        try:
            body = response.json()
        except ValueError:
            return

        if not isinstance(body, dict):
            return
        items = body.get("openapi_raw_data")
        if not isinstance(items, list):
            return

        for entry in items:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id")) != variant_id:
                continue
            if entry.get("has_error") is True:
                error_message = entry.get("error_message") or "no error message provided"
                raise PlatformAPIError(
                    f"Basalam {context} failed for product {variant_id}: {error_message}",
                    status_code=response.status_code,
                    retryable=False,
                )
            return  # matching entry found, has_error is not true -> success


def _item_id(item: dict, product: Product) -> str:
    raw_id = item.get("id")
    if raw_id is None:
        raise VariantResolutionError(
            f"Basalam product for '{product.internal_id}' has no 'id' in the product response.",
            retryable=False,
        )
    return str(raw_id)