"""WooCommerce REST API integration for the Faraz Honar store
(https://farazhonar.com), served by WordPress/WooCommerce.

Endpoints used:
  - `GET /products` — there is no by-id lookup used here: the sheet's
    "وردپرس" column already stores the WooCommerce product id directly
    (confirmed against the live sheet — 5-digit numeric ids), but this
    client still finds it via the paginated product list rather than a
    direct `GET /products/{id}`, matching each page's `id` field against
    the sheet value (see resolve_variant_id/_find_product_by_id) — the
    same list-and-match pattern as platforms/tapsishop/client.py, adapted
    to WooCommerce's `page`/`per_page` query params. Pagination continues
    until a page returns zero items (see _fetch_page/_find_product_by_id),
    never by assuming page 1 holds every product and never by trusting a
    single fixed page count.
  - `POST /products/{id}` — WooCommerce's own documented way to update an
    existing product; PUT is intentionally never used (see module-level
    note below). update_price is called once per product by the existing,
    unchanged per-product orchestration loop in update/service.py, so each
    call here updates exactly one product.
    Payload — CONFIRMED against a real, successful call against
    farazhonar.com: a price update needs only `{"regular_price": "<str>"}`;
    `stock_quantity`/`manage_stock` are NOT required for a price update to
    succeed and are never invented. `stock_quantity` is included in the
    payload (alongside `manage_stock: true`) only when the real current
    value is already known — i.e. already cached from a prior
    resolve_variant_id/get_reference_price call for this product (see
    `_last_resolved_item_by_variant`) — and omitted entirely otherwise.
    `regular_price` is always sent as a string, per WooCommerce's own
    documented field type.

Auth — IMPORTANT, and unlike every other platform client in this project:
the farazhonar.com WordPress firewall blocks WooCommerce's standard HTTP
Basic Auth (`requests.auth.HTTPBasicAuth`) and any `Authorization` header,
so `consumer_key`/`consumer_secret` are instead sent as URL query
parameters on every request (GET and POST alike) — see _request. A
browser-like `User-Agent` header is also required for requests to pass the
firewall; `Accept`/`Content-Type` are `application/json` as usual. Never
add an `Authorization` header or use `requests.auth.HTTPBasicAuth` here —
both are blocked by the firewall and would break every request.

PUT vs POST — IMPORTANT: the firewall also blocks standard PUT requests.
WooCommerce's REST API documents `POST /products/{id}` as the way to
update an existing product (its normal `PUT` alias is never required), so
this client always uses POST for updates and never PUT.

Units — IMPORTANT, and unlike every other platform client in this project:
farazhonar.com's own WooCommerce store prices products in TOMAN, not
Rial — CONFIRMED live on 2026-09-20 against real product 17817: a POST of
regular_price=60500000 was echoed back unchanged by an immediate follow-up
GET (`regular_price`/`price` both exactly "60500000", `sale_price` empty,
`type` "simple") and the site's own storefront displayed it as-is with no
×10/÷10 reinterpretation — i.e. the site stores and shows whatever number
is sent as Toman, verbatim. This is the OPPOSITE situation from
TapsiShop/Basalam (see their client modules), which really are
Rial-native.

The rest of this application (Product.target_price, core.pricing,
UpdateResult.previous_price, Telegram reports) is uniformly in Rial — the
sheet's Toman->Rial ×10 conversion happens exactly once, in
sheets/mapping.py::toman_to_rial, at the point the sheet's Toman value is
first parsed into Product.target_price — so this module converts at its
own API boundary, and nowhere else: get_reference_price multiplies the
API's raw Toman value by 10 before returning it (_toman_to_rial), so it
stays comparable to the rest of the app's Rial-based values, and
update_price divides the incoming Rial price by 10 (_rial_to_toman) right
before sending it as `regular_price`, so the site receives the Toman value
it actually expects. Both helpers are private to this module — never
imported from or into sheets/mapping.py — so no other platform or file is
affected by this conversion.

12% cap — WooCommerce is deliberately NOT included in core.pricing's
CAPPED_PLATFORMS: it always receives the full computed target price
uncapped, same as every platform other than the two DigiKala accounts.

Auth failures: a 401 or 403 (WooCommerce/WordPress can use either for a
rejected consumer key/secret) is a hard, non-retryable auth failure — no
refresh-token flow is documented for this API, so there is no
TokenManager/auth module here.

DRY_RUN semantics (same policy as every other platform in this app): reads
(the product-list pagination, used by both resolve_variant_id and
get_reference_price) are real GET requests regardless of DRY_RUN, so the
12% calculation can be verified against real WooCommerce data. update_price
(the only write, via POST) is the one thing DRY_RUN blocks.
"""
from __future__ import annotations

import logging
import time

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

_DEFAULT_PER_PAGE = 100
_MAX_SEARCH_PAGES = 200  # hard safety cap, same convention as platforms/tapsishop/client.py

# Same write/read classification convention as the other platform clients,
# kept as its own module-level constant rather than shared/imported so
# every platform client stays fully independent of the others.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Required to pass the farazhonar.com WordPress firewall (see module
# docstring) — a plain `requests` default User-Agent is blocked. CONFIRMED
# against a real, successful update call — exact string, do not shorten it.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _index_by_id(items: list[dict]) -> dict[str, dict]:
    """Pure id->item indexer, built from one page's items. Values missing
    `id` are skipped rather than indexed under a synthetic key."""
    index: dict[str, dict] = {}
    for item in items:
        value = item.get("id")
        if value is not None:
            index[str(value)] = item
    return index


def _parse_price(raw: object) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


_RIAL_PER_TOMAN = 10


def _toman_to_rial(toman: int) -> int:
    # farazhonar.com's WooCommerce store is Toman-native (see module
    # docstring's Units section) — used to convert a value just read from
    # the API into this app's internal Rial unit.
    return toman * _RIAL_PER_TOMAN


def _rial_to_toman(rial: int) -> int:
    # Inverse of _toman_to_rial above — used right before sending a price
    # (already in this app's internal Rial unit) to the API. Floor
    # division: this app's internal Rial values are always an exact
    # multiple of 10 (see sheets/mapping.py::toman_to_rial), so this never
    # loses precision; if that ever weren't true, rounding down never
    # sends a higher price than intended.
    return rial // _RIAL_PER_TOMAN


def _item_id(item: dict, product: Product) -> str:
    raw_id = item.get("id")
    if raw_id is None:
        raise VariantResolutionError(
            f"WooCommerce product for '{product.internal_id}' has no 'id' in the product list "
            "response.",
            retryable=False,
        )
    return str(raw_id)


class WooCommerceClient(PlatformClient):
    """The WooCommerce store at farazhonar.com. Structurally mirrors
    TapsiShopClient's list-and-match product resolution and
    Basalam/TapsiShop's throttle/error-classification/DRY_RUN pattern,
    adapted only where WooCommerce's actual API/deployment genuinely
    differs: URL-query-parameter auth and POST-only updates (both required
    by the site's firewall — see module docstring), a browser-like
    User-Agent header, and a Rial<->Toman unit conversion at this client's
    own API boundary only, since farazhonar.com's store is Toman-native
    (the opposite of TapsiShop/Basalam) — see module docstring's Units
    section.
    """

    # The store is Toman-native; update_price sends rial // 10, so a Rial value
    # is only representable in steps of 10.
    price_step_rial = 10

    def __init__(
        self,
        *,
        name: str = "woocommerce",
        base_url: str,
        consumer_key: str | None,
        consumer_secret: str | None,
        dry_run: bool,
        timeout_seconds: float = 15.0,
        read_delay_seconds: float = 1.0,
        write_delay_seconds: float = 2.0,
        per_page: int = _DEFAULT_PER_PAGE,
    ):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._dry_run = dry_run
        self._timeout = timeout_seconds
        self._per_page = per_page
        # id -> item for the WHOLE catalog, built once by prepare_price_check
        # and only set while a price check is running. None (the normal state)
        # leaves resolve_variant_id's page-walking lookup completely unchanged.
        self._check_index_by_id: dict[str, dict] | None = None
        # Short-lived, per-process cache of the last product item resolved
        # for a given id, so get_reference_price/update_price don't need a
        # second full-catalog pagination right after resolve_variant_id
        # found it, and so update_price can read back the real
        # `stock_quantity` rather than inventing a stock value. Always
        # re-resolved fresh on the next resolve_variant_id call (e.g. each
        # hourly recheck), so this never serves a stale price/stock across
        # runs — same contract as the other platform clients' equivalent
        # cache.
        self._last_resolved_item_by_variant: dict[str, dict] = {}
        self._read_delay_seconds = read_delay_seconds
        self._write_delay_seconds = write_delay_seconds
        self._last_read_request_at: float | None = None
        self._last_write_request_at: float | None = None

    def _products_path(self) -> str:
        return "/products"

    def _update_path(self, variant_id: str) -> str:
        return f"/products/{variant_id}"

    # -- Product resolution (paginated list, matched by id) ------------------------
    def resolve_variant_id(self, product: Product) -> str:
        raw_ref = product.platform_ref(self.name)
        if not raw_ref:
            raise VariantResolutionError(
                f"Product '{product.internal_id}' has no WooCommerce reference in the sheet's "
                f"'{self.name}' column.",
                retryable=False,
            )

        # Reads are allowed under DRY_RUN (DRY_RUN only blocks writes — see
        # update_price). This makes real GET requests regardless of DRY_RUN.
        item = self._find_product_by_id(raw_ref.strip(), product)
        variant_id = _item_id(item, product)
        self._last_resolved_item_by_variant[variant_id] = item
        return variant_id

    def _fetch_page(self, page: int) -> list[dict]:
        response = self._request(
            "GET", self._products_path(), params={"page": page, "per_page": self._per_page}
        )
        self._raise_for_error_status(response, context="product list fetch")
        items = response.json()
        if not isinstance(items, list):
            raise PlatformAPIError(
                "WooCommerce product list response has an unrecognized shape (expected a "
                "JSON array).",
                retryable=False,
            )
        return items

    def prepare_price_check(self) -> None:
        """Read the whole catalog ONCE so the price check does not re-walk it
        from page 1 for every single product."""
        index: dict[str, dict] = {}
        page = 1
        items = self._fetch_page(page)
        index.update(_index_by_id(items))
        while items and page < _MAX_SEARCH_PAGES:
            page += 1
            items = self._fetch_page(page)
            index.update(_index_by_id(items))
        self._check_index_by_id = index

    def finish_price_check(self) -> None:
        self._check_index_by_id = None

    def estimate_check_seconds(self, product_count: int) -> float:
        return self._pages_estimate_seconds(product_count, self._per_page)

    def _find_product_by_id(self, wc_id: str, product: Product) -> dict:
        if self._check_index_by_id is not None:
            item = self._check_index_by_id.get(wc_id)
            if item is None:
                raise VariantResolutionError(
                    f"No WooCommerce product found with id '{wc_id}' (product "
                    f"'{product.internal_id}') in the full catalog read for the price check.",
                    retryable=False,
                )
            return item
        page = 1
        items = self._fetch_page(page)
        index = _index_by_id(items)
        # Fetch page by page until a page comes back empty (no more
        # products) or the id is found — never assumes page 1 holds every
        # product, capped by _MAX_SEARCH_PAGES as a hard safety limit.
        while wc_id not in index and items and page < _MAX_SEARCH_PAGES:
            page += 1
            items = self._fetch_page(page)
            index.update(_index_by_id(items))

        item = index.get(wc_id)
        if item is None:
            raise VariantResolutionError(
                f"No WooCommerce product found with id '{wc_id}' (product "
                f"'{product.internal_id}') after checking {page} page(s) of "
                f"{self._products_path()}.",
                retryable=False,
            )
        return item

    # -- Reference price (regular_price, falling back to price) --------------------
    def get_reference_price(self, product: Product, variant_id: str) -> int:
        item = self._last_resolved_item_by_variant.get(variant_id)
        if item is None:
            item = self._find_product_by_id(variant_id, product)
            self._last_resolved_item_by_variant[variant_id] = item

        # regular_price can be an empty string when unset (WooCommerce's own
        # convention, not null) — not an error case: fall back to 'price'
        # whenever 'regular_price' is empty/missing, only raising if
        # neither field has a usable value. Same fallback pattern as
        # platforms/basalam/client.py's primary_price -> price fallback.
        price = _parse_price(item.get("regular_price"))
        if price is None:
            price = _parse_price(item.get("price"))
        if price is None:
            raise PlatformAPIError(
                f"WooCommerce product {variant_id} response has no usable 'regular_price' or "
                "'price' field.",
                retryable=False,
            )
        # The API's raw value is Toman (see module docstring's Units
        # section) — converted to Rial here, at this client's own read
        # boundary, so the returned value stays comparable to the rest of
        # this app's internal (Rial) pipeline, for either field.
        return _toman_to_rial(price)

    # -- Price update (POST, Toman units as a string; stock included only if known) --
    def update_price(self, variant_id: str, new_price: int) -> None:
        # new_price arrives in Rial (this app's internal unit, derived from
        # Product.target_price via the pricing pipeline — unchanged by this
        # client), but farazhonar.com's store itself is Toman-native (see
        # module docstring's Units section) — converted here, at this
        # client's own write boundary, right before sending.
        price_toman = _rial_to_toman(new_price)

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would POST regular_price=%s (Toman) to WooCommerce product %s "
                "(no request sent)",
                price_toman,
                variant_id,
            )
            return

        # WooCommerce expects regular_price as a string, per its documented
        # field type. A price update needs nothing else — CONFIRMED against
        # a real, successful call against farazhonar.com.
        payload: dict[str, object] = {"regular_price": str(price_toman)}

        # stock_quantity/manage_stock are included only when the real
        # current stock is already known (cached from a prior
        # resolve_variant_id/get_reference_price call for this product) —
        # never invented, and never a reason to block a price-only update
        # when it isn't known.
        item = self._last_resolved_item_by_variant.get(variant_id)
        stock = item.get("stock_quantity") if item is not None else None
        if stock is not None:
            payload["manage_stock"] = True
            payload["stock_quantity"] = int(stock)

        response = self._request("POST", self._update_path(variant_id), json_body=payload)
        self._raise_for_error_status(response, context="price update")

        logger.info(
            "WooCommerce product %s price update sent: regular_price=%s Toman%s",
            variant_id,
            price_toman,
            f", stock_quantity={stock}" if stock is not None else "",
        )

    # -- Shared request plumbing -----------------------------------------------------
    def _request(
        self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> requests.Response:
        if not self._consumer_key or not self._consumer_secret:
            raise ConfigurationError(
                "WooCommerce is not configured: set WOOCOMMERCE_CONSUMER_KEY and "
                "WOOCOMMERCE_CONSUMER_SECRET."
            )

        url = f"{self._base_url}{path}"
        # The farazhonar.com firewall blocks HTTPBasicAuth/Authorization
        # headers, so the consumer key/secret are sent as URL query
        # parameters instead, on every request (GET and POST alike) — see
        # module docstring. Never switch this to requests.auth.HTTPBasicAuth
        # or an Authorization header.
        query = dict(params or {})
        query["consumer_key"] = self._consumer_key
        query["consumer_secret"] = self._consumer_secret

        # A browser-like User-Agent is required to pass the firewall — see
        # module docstring. Exactly these three headers, no Authorization.
        headers = {
            "User-Agent": _BROWSER_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        self._throttle(method)
        try:
            response = requests.request(
                method, url, headers=headers, params=query, json=json_body, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise PlatformAPIError(
                f"WooCommerce request failed ({method} {path}): {exc}", retryable=True
            ) from exc

        return response

    def _throttle(self, method: str) -> None:
        # Identical throttling strategy to the other platform clients — see
        # DigikalaClient._throttle's docstring for the full rationale. Kept
        # as separate, independent per-instance state so WooCommerce
        # requests never wait on any other platform's requests.
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
        if response.status_code in (401, 403):
            # WooCommerce/WordPress can reject a bad consumer key/secret
            # with either 401 or 403 depending on where the rejection
            # happens (REST API itself vs. the firewall/WAF in front of
            # it) — both are treated as the same hard, non-retryable auth
            # failure.
            raise PlatformAuthError(
                f"WooCommerce rejected the request ({context}): {response.text[:500]}",
                status_code=response.status_code,
                retryable=False,
            )

        if response.status_code not in (200, 201):
            # 4xx (validation/not-found/rate-limit) errors are never
            # retried — retrying an invalid request or a 429 immediately
            # only wastes requests / worsens rate-limiting, same policy as
            # every other platform client here. 5xx/network errors remain
            # retryable, capped at PriceUpdateService's max_retry_attempts.
            retryable = response.status_code >= 500
            raise PlatformAPIError(
                f"WooCommerce {context} failed: HTTP {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
                retryable=retryable,
            )
