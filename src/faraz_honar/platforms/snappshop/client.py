"""SnappShop seller-API integration, against the OFFICIAL SnappShop
Automation API. Reused as-is for every SnappShop account (Tehran, Isfahan,
...) — see platforms/registry.py; only credentials/vendor code/sheet column
differ per account, passed in at construction (name/display_name/
config_env_hint/token/vendor_code/agent_user). The earlier reverse-engineered
endpoints (`/vendors/v1/{vendor}/inventory/products...`) are deprecated and
no longer used anywhere in this module.

Endpoints used, both under `/automation/v1/vendors/{VENDOR_ID}`:
  - `GET /products?page=N` — the only documented way to find a product:
    there is no by-ID lookup/search endpoint, so finding one product means
    paginating this list and matching each item's `product_number` against
    the sheet's "اسنپ شاپ تهران" value. Matched items are cached by their
    internal SnappShop `id` (see resolve_variant_id/_find_product_by_number)
    — `product_number` is what the sheet stores and what a human identifies
    a product by; `id` is the opaque identifier the write endpoint actually
    requires. CONFIRMED against the real API: pagination is driven by
    `meta.pagination.total_pages` in the response, not by waiting for an
    empty page — page 1 alone is never assumed to hold every product, and
    the client fetches (at most) exactly that many pages before giving up
    on a match (see _extract_total_pages/_fetch_page).
    Also CONFIRMED: a product's `sku` field is unreliable (the real API
    returns `"0"` for every product) and is NEVER used for matching or as
    an identifier — only `id` (see _item_id).
    Performance — IMPORTANT: pages fetched while resolving ONE product are
    cached and reused for every OTHER product looked up soon after (see
    _catalog_by_number/_catalog_by_id, _ensure_catalog_cache_fresh,
    _fetch_catalog_pages_until_found), for up to _CATALOG_CACHE_TTL_SECONDS.
    Without this, a product near the end of a large catalog (CONFIRMED
    live: one seller account had 46 pages, with a product found only on
    page 33) would force a fresh page-1-to-N walk for every single product
    looked up, independently — for a run touching many products this could
    take an extremely long time, and is very likely why some products'
    prices previously appeared to never get applied within a run.
    The product list items themselves are returned under a `"products"`
    key, e.g. `{"products": [...], "meta": {"pagination": {...}}}` —
    matching the PATCH endpoint's own `{"products": [...]}` request shape.
    A `"data"` key (the old reverse-engineered API's shape) is also
    accepted as a fallback. If the real response uses neither key,
    _extract_products raises a clear PlatformAPIError rather than silently
    treating an unrecognized shape as "no more products" (which would look
    like a product-not-found error instead of a real integration bug).
  - `PATCH /products` (bulk) — takes `{"products": [{"id", "stock",
    "price"}, ...]}`, at most 50 items per call (see _MAX_PATCH_BATCH_SIZE /
    _chunked). update_price is called once per product by the existing,
    unchanged per-product orchestration loop in update/service.py, so each
    call here sends exactly one item — trivially within the 50-item cap —
    rather than accumulating multiple products' updates into one call. True
    cross-product batching would require restructuring that per-product
    retry/result loop (each product's outcome is currently retried and
    reported independently), which is out of scope: this module changes
    only the SnappShop-specific API adapter, not the shared architecture.
    The batching/chunking machinery is still real (not a no-op shim) so it
    is correct by construction if a future caller ever passes more items.

    `stock` is REQUIRED by the API and is never invented: it is always the
    real value most recently read back from `GET /products` for that
    product (cached alongside the item — see
    `_last_resolved_item_by_variant`), never a guessed/default value.

    Active discounts — IMPORTANT: CONFIRMED against the official Snappshop
    Vendor Automation API doc (v2.1.2, section 2-2-3): a product with an
    active discount carries a `discount` object in the GET response (with
    its own `special_price`, `percent`, `start_at`/`end_at`, `stock`), and
    the PATCH endpoint accepts an optional sibling `special_price` field —
    the discounted price actually shown to customers while the discount is
    active, separate from the base `price` this method otherwise updates.
    Only ever updating `price` therefore never changes what a customer
    sees for a currently-discounted product until its discount expires on
    its own — this matches the originally reported symptom ("prices don't
    apply for discounted items"). See update_price: whenever the resolved
    item currently has a `discount`, `special_price` is included in the
    PATCH, set to the same value as `price`, so the target price actually
    becomes visible; a product with no discount is unaffected.
  - `GET /products/{id}` — a direct single-product lookup BY THE OPAQUE
    SNAPPSHOP `id` (not by `product_number` — there's still no way to
    search/filter by that, see the pagination note above). Used by
    _find_product_by_id_fallback instead of paginating the whole catalog
    again, for the (normally unreachable — see that method's own comment)
    case where get_reference_price's cache was somehow empty.

Units — IMPORTANT: the official API's PATCH `price` is in TOMAN, unlike
DigiKala's Rial API. Every other part of this system (the sheet's converted
target_price, PricingDecision, DigikalaClient, the retry/report layer)
operates in Rial throughout — that convention is NOT changed. This module
is the one boundary that knows about SnappShop's Toman units: GET responses
are read as Toman and converted to Rial (×10) before being returned from
get_reference_price, and the Rial price the generic pricing algorithm hands
to update_price is converted back to Toman (÷10) only right before being
sent in the PATCH body. See _toman_to_rial/_rial_to_toman.

Auth: a permanent Bearer JWT (SNAPPSHOP_TOKEN) issued to an agent user
(SNAPPSHOP_AGENT_USER, an "SNV_..." string from the SnappShop panel), plus
the store's 6-character vendor id (SNAPPSHOP_VENDOR_CODE, e.g. "0dPKQ4") —
no refresh-token flow is documented for this API, unlike DigiKala, so there
is no TokenManager/auth module here; a 401 is just a hard, non-retryable
auth failure. Per the official Automation API spec, only four headers are
sent: Authorization, User-Agent, Accept, Content-Type — the previous
reverse-engineered API's `uuid`, `x-client-type`, and
`snappshop-seller-code` headers are gone; the vendor id is only ever used
in the URL now.

IMPORTANT — vendor id vs. agent user are NOT interchangeable: the agent
user string (SNV_...) identifies the API caller/agent and belongs only in
the User-Agent header; the 6-character vendor id identifies the store and
belongs only in the URL path. Putting the agent string in the URL in place
of the vendor id is what previously produced SnappShop error 301008
("عدم دسترسی" — access denied).

Iran-IP restriction: SnappShop's API may answer with HTTP 403 and an HTML
page (not JSON) when called from outside Iran. That is NOT treated as a
successful or even a normal-error response — see _looks_like_html — and is
raised as a distinct, clearly-labeled PlatformAPIError so it's never
confused with a real 403 permission error. An optional SNAPPSHOP_PROXY_URL
is supported (passed straight to `requests`' own `proxies=`) for routing
through an Iranian exit IP, reusing `requests`' existing proxy support
rather than a new networking layer. It stays optional — nothing requires a
proxy unless the API actually rejects the current IP.

DRY_RUN semantics (same policy as DigiKala): reads (the product-list
pagination, used by both resolve_variant_id and get_reference_price) are
real GET requests regardless of DRY_RUN, so the 12% calculation can be
verified against real SnappShop data. update_price (the only write, via
PATCH) is the one thing DRY_RUN blocks.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
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

_TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))  # fixed offset; Iran has no DST since 2022
_MAX_SEARCH_PAGES = 200  # hard safety cap on top of the API's own meta.pagination.total_pages
_MAX_PATCH_BATCH_SIZE = 50  # official API limit: at most 50 products per PATCH request

# How long the shared catalog-page cache (see _catalog_by_number/_catalog_by_id
# below) stays valid before a resolve/lookup call starts a fresh walk from
# page 1 again. CONFIRMED live (2026-09-23): a real seller account had 46
# pages, and one product was found only on page 33 — i.e. up to ~46
# sequential, throttled GET requests just to locate ONE product. Without
# sharing pages fetched while resolving one product with every other
# product's lookup, a run touching many products could take an extremely
# long time (each late-catalog product re-walking from page 1), which is
# very likely why some products' prices never got applied within a run.
# 5 minutes comfortably covers a single run's worth of per-product lookups
# while still being short enough that the next run (e.g. the hourly
# recheck, default 60 minutes later) reads genuinely fresh data — not an
# externally configurable value since this is a pure internal performance
# optimization, not a business-rule knob.
_CATALOG_CACHE_TTL_SECONDS = 300.0

# Same write/read classification convention as platforms/digikala/client.py,
# kept as its own module-level constant rather than shared/imported so the
# two platform clients stay fully independent of each other.
_WRITE_METHODS = frozenset({"PUT", "PATCH", "POST", "DELETE"})


def _toman_to_rial(toman: int) -> int:
    # Same factor as sheets/mapping.py::toman_to_rial, kept as a separate,
    # SnappShop-local copy on purpose: this conversion is specifically about
    # the SnappShop API's response units, not the sheet's — the two must
    # stay independent so a change to one never silently affects the other.
    return toman * 10


def _rial_to_toman(rial: int) -> int:
    # round(), not floor()/int(), so a Rial value that isn't an exact
    # multiple of 10 (e.g. from the 12%-cap rounding in core/pricing.py)
    # loses at most half a Toman instead of being silently truncated down.
    return round(rial / 10)


def _chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _extract_products(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("products"), list):
            return payload["products"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
    raise PlatformAPIError(
        "SnappShop product list response has an unrecognized shape "
        "(expected a 'products' or 'data' array, or a bare JSON array).",
        retryable=False,
    )


def _extract_total_pages(payload: object) -> int:
    # Confirmed against the real API: meta.pagination.total_pages is the
    # authoritative page count — pagination must never assume page 1 holds
    # everything, and must never guess "done" from an empty page instead.
    if isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict):
            pagination = meta.get("pagination")
            if isinstance(pagination, dict):
                total_pages = pagination.get("total_pages")
                if isinstance(total_pages, int) and total_pages >= 0:
                    return total_pages
    raise PlatformAPIError(
        "SnappShop product list response is missing "
        "'meta.pagination.total_pages'.",
        retryable=False,
    )


def _index_by_key(items: list[dict], key: str) -> dict[str, dict]:
    """Pure product_number->item (or id->item) indexer, built from one
    page's items. Values that are missing `key` are skipped rather than
    indexed under a synthetic key.
    """
    index: dict[str, dict] = {}
    for item in items:
        value = item.get(key)
        if value is not None:
            index[str(value)] = item
    return index


class SnappShopClient(PlatformClient):
    """One SnappShop seller account, against the official Automation API.
    Reused as-is for every SnappShop account (Tehran, Isfahan, ...) — only
    credentials/vendor code/sheet column differ per account (see
    platforms/registry.py), exactly like DigikalaClient is reused for the
    digikala/digikala2 DigiKala accounts. Structurally mirrors DigikalaClient: same
    throttle/error-classification/DRY_RUN pattern, same per-run "last
    resolved item" cache — adapted only where SnappShop's actual API shape
    genuinely differs (pagination instead of a search endpoint, sharing
    fetched catalog pages across different products' lookups for a bounded
    time rather than re-walking from page 1 for each one — see module
    docstring's Performance section, a bulk id/stock/price PATCH instead of
    DigiKala's price-only PUT, Toman units instead of Rial, a single static
    token instead of an access/refresh pair).
    """

    # The API works in Toman; update_price converts Rial -> Toman, so a Rial
    # value is only representable in steps of 10 (see _rial_to_toman).
    price_step_rial = 10

    def __init__(
        self,
        *,
        name: str = "snappshop_tehran",
        display_name: str = "SnappShop Tehran",
        config_env_hint: str = "SNAPPSHOP_TOKEN, SNAPPSHOP_VENDOR_CODE, and SNAPPSHOP_AGENT_USER",
        base_url: str,
        token: str | None,
        vendor_code: str | None,
        agent_user: str | None,
        dry_run: bool,
        timeout_seconds: float = 15.0,
        read_delay_seconds: float = 1.0,
        write_delay_seconds: float = 2.0,
        proxy_url: str | None = None,
    ):
        self.name = name
        # Human-readable label used in every log/error message below, and
        # the exact env var names to mention in the "not configured" error
        # — both differ per account (e.g. Isfahan's are
        # SNAPPSHOP_ISFAHAN_*), so a message must never hardcode Tehran's.
        self._display_name = display_name
        self._config_env_hint = config_env_hint
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._vendor_code = vendor_code
        self._agent_user = agent_user
        self._dry_run = dry_run
        self._timeout = timeout_seconds
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        # Short-lived, per-process cache of the last product item resolved
        # for a given id, so get_reference_price/update_price don't need a
        # second full-catalog pagination right after resolve_variant_id
        # found it, and so update_price can read back the real `stock`
        # value rather than inventing one. Re-populated on every
        # resolve_variant_id call — see _catalog_by_number/_catalog_by_id
        # below for the bounded staleness of the underlying page data it's
        # built from.
        self._last_resolved_item_by_variant: dict[str, dict] = {}
        # Shared catalog-page cache, reused across DIFFERENT products'
        # lookups within _CATALOG_CACHE_TTL_SECONDS (see that constant's
        # docstring) — turns "walk from page 1 for every single product"
        # into "walk the catalog once, reuse for everyone else looked up
        # soon after." Bounded staleness tradeoff: a product's `price`/
        # `stock` read from here can be up to _CATALOG_CACHE_TTL_SECONDS
        # old. This never affects what price is actually applied (SnappShop
        # is never in core.pricing.CAPPED_PLATFORMS, so the full target
        # price is always sent regardless of reference_price), only the
        # logged/reported reference price and the stock value sent
        # alongside a price update.
        self._catalog_by_number: dict[str, dict] = {}
        self._catalog_by_id: dict[str, dict] = {}
        self._catalog_by_parent_number: dict[str, dict] = {}
        self._catalog_pages_fetched: int = 0
        self._catalog_total_pages: int | None = None
        self._catalog_cache_started_at: float | None = None
        self._read_delay_seconds = read_delay_seconds
        self._write_delay_seconds = write_delay_seconds
        self._last_read_request_at: float | None = None
        self._last_write_request_at: float | None = None

    def _products_path(self) -> str:
        return f"/vendors/{self._vendor_code}/products"

    def _product_path(self, item_id: str) -> str:
        return f"{self._products_path()}/{item_id}"

    # -- Product resolution --------------------------------------------------------
    def resolve_variant_id(self, product: Product) -> str:
        raw_ref = product.platform_ref(self.name)
        if not raw_ref:
            raise VariantResolutionError(
                f"Product '{product.internal_id}' has no {self._display_name} reference in the "
                f"sheet's '{self.name}' column.",
                retryable=False,
            )

        # Reads are allowed under DRY_RUN (DRY_RUN only blocks writes — see
        # update_price). This makes real GET requests regardless of DRY_RUN.
        item = self._find_product_by_number(raw_ref.strip(), product)
        variant_id = _item_id(item, product, self._display_name)
        self._last_resolved_item_by_variant[variant_id] = item
        return variant_id

    def _fetch_page(self, page: int) -> tuple[list[dict], int]:
        response = self._request("GET", self._products_path(), params={"page": page})
        self._raise_for_error_status(response, context="product list fetch")
        payload = response.json()
        items = _extract_products(payload)
        total_pages = min(_extract_total_pages(payload), _MAX_SEARCH_PAGES)
        return items, total_pages

    def _ensure_catalog_cache_fresh(self) -> None:
        # Starts a brand-new cache generation (discarding whatever was
        # cached before) only when there's no cache yet or the current one
        # has outlived _CATALOG_CACHE_TTL_SECONDS. Otherwise a no-op, so
        # calling this at the start of every lookup is cheap and never
        # resets a still-valid cache mid-run.
        now = time.monotonic()
        is_stale = (
            self._catalog_cache_started_at is None
            or now - self._catalog_cache_started_at > _CATALOG_CACHE_TTL_SECONDS
        )
        if is_stale:
            self._catalog_by_number = {}
            self._catalog_by_id = {}
            self._catalog_by_parent_number = {}
            self._catalog_pages_fetched = 0
            self._catalog_total_pages = None
            self._catalog_cache_started_at = now

    def _fetch_catalog_pages_until_found(self, index: dict[str, dict], key: str) -> None:
        # Continues fetching pages from wherever the current cache
        # generation left off (never restarts from page 1) until `key` is
        # in `index` or the whole catalog has been read. Both
        # _catalog_by_number and _catalog_by_id are updated from the same
        # page fetch, so resolving one product by number also warms the
        # id-keyed index (and vice versa) for later lookups within this
        # cache generation.
        while key not in index:
            if (
                self._catalog_total_pages is not None
                and self._catalog_pages_fetched >= self._catalog_total_pages
            ):
                return  # whole catalog already read this generation; nothing more to fetch
            next_page = self._catalog_pages_fetched + 1
            items, total_pages = self._fetch_page(next_page)
            self._catalog_by_number.update(_index_by_key(items, "product_number"))
            self._catalog_by_id.update(_index_by_key(items, "id"))
            self._catalog_by_parent_number.update(_index_by_key(items, "parent_product_number"))
            self._catalog_pages_fetched = next_page
            self._catalog_total_pages = total_pages

    def _find_product_by_number(self, product_number: str, product: Product) -> dict:
        self._ensure_catalog_cache_fresh()
        self._fetch_catalog_pages_until_found(self._catalog_by_number, product_number)

        item = self._catalog_by_number.get(product_number)
        if item is None:
            # CONFIRMED against live catalogs (2026-10-01): the code kept in
            # the sheet is the product's `parent_product_number` (the code
            # shown on the SnappShop product page). For most items it equals
            # the item's own `product_number`, but for some (e.g. ~47 items
            # per account, such as the "دستبند الکتروکالچر" / "پک هدیه" /
            # "قلمکار" products) the API's own `product_number` is a
            # DIFFERENT number, so a product_number-only lookup never finds
            # them. We only get here after the whole catalog was read (the
            # fetch loop above stops early only when the key is found), so
            # the parent index is complete. `product_number` always wins;
            # parent_product_number is a fallback only, and in every
            # observed catalog it is unique per item (no ambiguity).
            item = self._catalog_by_parent_number.get(product_number)
            if item is not None:
                logger.info(
                    "%s: sheet code %s matched via parent_product_number "
                    "(item id=%s, its own product_number=%s)",
                    self._display_name,
                    product_number,
                    item.get("id"),
                    item.get("product_number"),
                )
        if item is None:
            raise VariantResolutionError(
                f"No {self._display_name} product found with product_number '{product_number}' "
                f"(product '{product.internal_id}') after checking all "
                f"{self._catalog_pages_fetched} page(s) of {self._products_path()}.",
                retryable=False,
            )
        return item

    # -- Reference price (read as Toman, returned as Rial) -------------------------
    def get_reference_price(self, product: Product, variant_id: str) -> int:
        item = self._last_resolved_item_by_variant.get(variant_id)
        if item is None:
            # variant_id here is the same opaque `id` resolve_variant_id
            # returned; re-searching by it directly (rather than by
            # product_number, which we no longer have in this code path) is
            # fine since the cache normally makes this branch unreachable in
            # the real call flow — see update_price's identical fallback
            # comment.
            item = self._find_product_by_id_fallback(variant_id, product)
            self._last_resolved_item_by_variant[variant_id] = item

        price_toman = item.get("price")
        if price_toman is None:
            raise PlatformAPIError(
                f"{self._display_name} product {variant_id} response has no 'price' field.",
                retryable=False,
            )
        return _toman_to_rial(int(price_toman))

    def get_current_price(self, product: Product, variant_id: str) -> int:
        """Read-only (price-check button): the product's BASE price (`price`).

        The check compares the sheet's "قیمت اصلی" with the platform's main,
        non-discounted price, so a product with an active discount is still
        checked against its base `price`, never `discount.special_price`.
        """
        return self.get_reference_price(product, variant_id)  # also fills the cache

    def prepare_price_check(self) -> None:
        # Drop the shared catalog-page cache (up to 5 minutes old) so the check
        # never reports prices from before an update that just ran. The
        # per-product lookups afterwards still share pages with each other.
        self._catalog_cache_started_at = None

    def _find_product_by_id_fallback(self, variant_id: str, product: Product) -> dict:
        # CONFIRMED against the official API doc (v2.1.2, section 2-2-2): a
        # direct single-product GET-by-id endpoint exists, unlike matching
        # by product_number (see _find_product_by_number above, which
        # still has no search endpoint and must paginate).
        response = self._request("GET", self._product_path(variant_id))
        if response.status_code == 404:
            raise PlatformAPIError(
                f"{self._display_name} product {variant_id} could not be re-fetched (product "
                f"'{product.internal_id}').",
                retryable=False,
            )
        self._raise_for_error_status(response, context="product re-fetch")
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            item = payload["data"]
            self._catalog_by_id[variant_id] = item  # keep the shared cache consistent
            return item
        raise PlatformAPIError(
            f"{self._display_name} single-product response has an unrecognized shape "
            "(expected a 'data' object).",
            retryable=False,
        )

    # -- Price update (bulk PATCH, Toman units, real stock preserved) --------------
    def update_price(self, variant_id: str, new_price: int) -> None:
        if self._dry_run:
            logger.info(
                "[DRY RUN] Would PATCH price=%s (Rial) to %s product %s (no request sent)",
                new_price,
                self._display_name,
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
                f"Cannot update {self._display_name} product {variant_id}: no previously-fetched "
                "product data (needed for its real 'stock') available.",
                retryable=False,
            )

        stock = item.get("stock")
        if stock is None:
            raise PlatformAPIError(
                f"{self._display_name} product {variant_id} response has no 'stock' field — "
                "refusing to PATCH without the real current stock.",
                retryable=False,
            )

        price_toman = _rial_to_toman(new_price)
        patch_item: dict[str, Any] = {"id": variant_id, "stock": int(stock), "price": price_toman}

        # CONFIRMED against the official Snappshop Vendor Automation API
        # doc (v2.1.2, section 2-2-3 "آپدیت محصول"): when a product has an
        # active discount, the GET response's `discount.special_price` is
        # the price actually shown to customers — `price` (the base price
        # this method otherwise updates) is not what's displayed until the
        # discount itself expires. Only updating `price` therefore never
        # visibly changes a discounted product's price, which matches the
        # exact symptom originally reported ("prices don't apply for
        # discounted items"). Whenever the resolved item currently has a
        # discount, special_price is kept in sync with the same target
        # price being applied here — never invented when there's no
        # discount, and the discount's own percent/dates/its own stock are
        # left untouched (this only aligns the customer-visible price with
        # the target price, it doesn't create, remove, or reconfigure a
        # discount campaign).
        discount = item.get("discount")
        has_active_discount = discount is not None
        if has_active_discount:
            patch_item["special_price"] = price_toman
            # CONFIRMED live (HTTP 422, code 101002, field
            # `products.N.special_price_start_at`): whenever `special_price`
            # is sent, the API also REQUIRES `special_price_start_at`. Reuse
            # the discount's own existing `start_at` so the campaign's start
            # is left untouched; only if it is missing/empty fall back to
            # today's date (Tehran time).
            patch_item["special_price_start_at"] = _discount_start_at(discount)
            # CONFIRMED live (HTTP 422, code 101002, field
            # `products.N.special_price_end_at`, first seen 2026-10-01):
            # the API now ALSO requires `special_price_end_at` whenever
            # `special_price` is sent. Reuse the discount's own existing
            # `end_at` so the campaign's end is left untouched. It is never
            # invented: if the discount has no end_at we fail with a clear,
            # non-retryable error instead of guessing a date.
            patch_item["special_price_end_at"] = _discount_end_at(discount, variant_id, self._display_name)
            # CONFIRMED live (HTTP 422, code 101002, field
            # `products.N.special_price_stock`, first seen 2026-10-01 15:55):
            # the API ALSO requires the discount's own stock whenever
            # `special_price` is sent. Reuse the discount's existing `stock`
            # so the campaign's allocation is left untouched; if it is
            # missing, fall back to the product's real stock (never invented).
            patch_item["special_price_stock"] = _discount_stock(discount, int(stock))

        patch_items = [patch_item]
        for batch in _chunked(patch_items, _MAX_PATCH_BATCH_SIZE):
            payload = {"products": batch}
            response = self._request("PATCH", self._products_path(), json_body=payload)
            self._raise_for_error_status(response, context="price update")

        logger.info(
            "%s product %s price update sent: price=%s Rial (%s Toman)%s, stock=%s",
            self._display_name,
            variant_id,
            new_price,
            price_toman,
            " (product has an active discount -> special_price updated too)"
            if has_active_discount
            else "",
            stock,
        )

    # -- Shared request plumbing -----------------------------------------------------
    def _request(
        self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> requests.Response:
        if not self._token or not self._vendor_code or not self._agent_user:
            raise ConfigurationError(
                f"{self._display_name} is not configured: set {self._config_env_hint}."
            )

        url = f"{self._base_url}{path}"
        # Official Automation API header set — exactly these four. No
        # uuid / x-client-type / snappshop-seller-code (the previous
        # reverse-engineered API's headers, now removed); the vendor id is
        # only ever used in the URL (see _products_path).
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._agent_user,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        self._throttle(method)
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=self._timeout,
                proxies=self._proxies,
            )
        except requests.RequestException as exc:
            raise PlatformAPIError(
                f"SnappShop request failed ({method} {path}): {exc}", retryable=True
            ) from exc

        return response

    def _throttle(self, method: str) -> None:
        # Identical throttling strategy to DigikalaClient._throttle — see
        # that method's docstring for the full rationale. Kept as separate,
        # independent per-instance state so SnappShop and DigiKala requests
        # never wait on each other.
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
        if response.status_code in (200, 201):
            return

        if response.status_code == 403 and _looks_like_html(response.text or ""):
            # Do NOT treat this as a normal error, let alone a success: it's
            # the documented signature of SnappShop's Iran-IP/VPN
            # restriction (an HTML page instead of a JSON error body), not
            # a real permission failure — surfaced distinctly so it's never
            # mistaken for one in logs/reports.
            raise PlatformAPIError(
                f"SnappShop {context} failed: HTTP 403 with a non-JSON (HTML) body — this is "
                "SnappShop's Iran-IP/VPN access restriction page, not a normal API error. "
                "A proxy with an Iranian exit IP is required (see SNAPPSHOP_PROXY_URL).",
                status_code=403,
                retryable=False,
            )

        if response.status_code == 401:
            raise PlatformAuthError(
                f"SnappShop rejected the access token ({context}): {response.text[:500]}",
                status_code=401,
                retryable=False,
            )

        # 4xx (auth/validation/not-found/rate-limit) errors are never
        # retried — retrying an invalid request or a 429 immediately only
        # wastes requests / worsens rate-limiting, same policy as DigiKala's
        # 429 handling. 5xx/network errors remain retryable, capped at
        # PriceUpdateService's max_retry_attempts.
        retryable = response.status_code >= 500
        raise PlatformAPIError(
            f"SnappShop {context} failed: HTTP {response.status_code}: {response.text[:500]}",
            status_code=response.status_code,
            retryable=retryable,
        )


def _item_id(item: dict, product: Product, display_name: str = "SnappShop") -> str:
    # CONFIRMED against the real API: 'sku' is unreliable (returns "0" for
    # every real product), so it is NEVER used as a fallback identifier —
    # only 'id' is ever used.
    raw_id = item.get("id")
    if raw_id is None:
        raise VariantResolutionError(
            f"{display_name} product for '{product.internal_id}' has no 'id' in the "
            "product list response.",
            retryable=False,
        )
    return str(raw_id)


def _discount_start_at(discount: object) -> str:
    if isinstance(discount, dict):
        start_at = discount.get("start_at")
        if isinstance(start_at, str) and start_at.strip():
            return start_at.strip()
    return datetime.now(_TEHRAN_TZ).strftime("%Y-%m-%d")


def _discount_end_at(discount: object, variant_id: str, display_name: str) -> str:
    if isinstance(discount, dict):
        end_at = discount.get("end_at")
        if isinstance(end_at, str) and end_at.strip():
            return end_at.strip()
    raise PlatformAPIError(
        f"{display_name} product {variant_id} has an active discount without an 'end_at' — "
        "the API requires special_price_end_at together with special_price, and it is not "
        "guessed. Fix/end the discount in the SnappShop panel, then retry.",
        retryable=False,
    )


def _discount_stock(discount: object, product_stock: int) -> int:
    if isinstance(discount, dict):
        raw = discount.get("stock")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return product_stock


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip()[:100].lower()
    return stripped.startswith(("<!doctype", "<html", "<?xml"))